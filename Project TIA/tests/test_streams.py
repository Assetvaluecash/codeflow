"""Event-stream lineage (PRD FR-042): events traced across several hops (producer -> topic -> consumer -> re-published topic) and into data
pipelines declared in source control (Kafka Streams, Spark, Kafka Connect), with the same evidence rules."""
import json

import pytest

from fixtures.build_streams_fixture import build as build_streams
from tia.extraction import extract_commit
from tia.impact import AnalysisRequest, analyze
from tia.streams import connector_facts, is_connector_path, stream_facts


def test_kafka_streams_topology_with_constants_and_lists():
    src = '''class T {
  static final String IN = "payments.raw";
  void build(StreamsBuilder b) {
    b.stream(Arrays.asList("cards.raw", IN)).filter((k, v) -> v != null).to("payments.clean");
    b.table("fx.rates");
    b.stream(Pattern.compile("audit\\\\..*"));
  }
}'''
    from types import SimpleNamespace as NS
    ev, acc, jobs, unk = stream_facts("r", "T.java", "import org.apache.kafka.streams.StreamsBuilder;\n" + src, [NS(id="r:T.java::T.build", line=4, end_line=8)])
    assert sorted((e["kind"], e["topic"]) for e in ev) == [("consumes", "cards.raw"), ("consumes", "fx.rates"), ("consumes", "payments.raw"),
                                                         ("produces", "payments.clean")]
    assert all(e["function_id"] == "r:T.java::T.build" for e in ev) and not jobs
    assert len(unk) == 1 and "pattern" in unk[0]["reason"]


def test_spark_jobs_read_and_write_topics_and_tables():
    job = '''from pyspark.sql import SparkSession
spark = SparkSession.builder.getOrCreate()
TOPIC = "clicks"
df = spark.readStream.format("kafka").option("subscribe", TOPIC).load()
dims = spark.table("dim_pages")
df.writeStream.format("kafka").option("topic", "clicks.sessionised").start()
df.write.mode("append").saveAsTable("raw.clicks")
other = spark.readStream.format("kafka").option("subscribePattern", "logs.*").load()
'''
    ev, acc, jobs, unk = stream_facts("r", "jobs/clicks.py", job, [])
    assert sorted((e["kind"], e["topic"]) for e in ev) == [("consumes", "clicks"), ("produces", "clicks.sessionised")]
    assert sorted((a["op"], a["target"]) for a in acc) == [("read", "dim_pages"), ("write", "clicks")]
    assert [j.qualname for j in jobs] == ["<job clicks.py>"] and all(e["function_id"] == jobs[0].id for e in ev)
    assert len(unk) == 1 and "subscribePattern" in unk[0]["reason"]


def test_kafka_connect_sink_and_source_connectors():
    assert is_connector_path("connect/jdbc-sink.json") and is_connector_path("kafka-connect/cdc.properties")
    assert not is_connector_path("web/package.json") and not is_connector_path("src/config.json")
    sink = json.dumps({"name": "s", "config": {"connector.class": "io.confluent.connect.jdbc.JdbcSinkConnector", "topics": "a,b"}})
    ev, acc, fns, _ = connector_facts("r", "connect/s.json", sink)
    assert [(e["kind"], e["topic"]) for e in ev] == [("consumes", "a"), ("consumes", "b")] and [(a["op"], a["target"]) for a in acc] == [("write", "a"), ("write", "b")]
    assert fns[0].qualname == "connector s"
    props = "name=cdc\nconnector.class=io.debezium.connector.mysql.MySqlConnector\ndatabase.server.name=shop\ntable.include.list=inventory.orders\n"
    ev, acc, _, _ = connector_facts("r", "connect/cdc.properties", props)
    assert [(e["kind"], e["topic"]) for e in ev] == [("produces", "shop.inventory.orders")] and [(a["op"], a["target"]) for a in acc] == [("read", "orders")]
    assert connector_facts("r", "connect/x.json", '{"not": "a connector"}') == ([], [], [], [])


@pytest.fixture(scope="module")
def stfx(tmp_path_factory):
    return build_streams(str(tmp_path_factory.mktemp("streams")))


def test_a_schema_change_reaches_every_hop_and_pipeline(stfx):
    rep = analyze(AnalysisRequest(repos=stfx["paths"], target="orders-service", base="main", head="field_removed", max_depth=12), None)
    assert rep["status"] == "complete"
    by = {f["component"]: f for f in rep["findings"]}
    assert set(by) == {"enrichment-streams", "fraud-service", "analytics-spark", "notification-service", "warehouse-connect"}
    assert all(f["impact_type"] == "event" and f["severity"] == "high" for f in rep["findings"])
    assert "consumes topic orders.created" in by["enrichment-streams"]["reason"] and "reached through" not in by["enrichment-streams"]["reason"]
    note = by["notification-service"]["reason"]
    assert "consumes topic fraud.alerts" in note and "orders.created -> orders.enriched -> fraud.alerts" in note
    topics = [p["node"] for p in by["warehouse-connect"]["path"] if p["type"] == "topic"]
    assert topics == ["topic:orders.created", "topic:orders.enriched", "topic:fraud.alerts"]
    assert by["warehouse-connect"]["evidence"]["file"] == "connect/fraud-alerts-sink.json"
    assert by["analytics-spark"]["evidence"]["file"] == "jobs/orders_daily.py"
    areas = {u["area"] for u in rep["unknowns"]}
    assert "unresolved_outbound_calls" not in areas and "file_level_analysis" not in areas      # Kafka sends are events; the schema is analysed


def test_the_depth_limit_is_reported_not_silent(stfx):
    rep = analyze(AnalysisRequest(repos=stfx["paths"], target="orders-service", base="main", head="field_removed", max_depth=3), None)
    assert "notification-service" not in {f["component"] for f in rep["findings"]}
    assert any(u["area"] == "truncated_traversal" for u in rep["unknowns"]) and rep["status"] == "partial"


def test_pipelines_are_graph_components(stfx):
    ex, _ = extract_commit("warehouse-connect", stfx["paths"]["warehouse-connect"], "main", None)
    assert sorted(f.qualname for f in ex.functions) == ["connector crm-cdc", "connector fraud-alerts-sink"]
    assert sorted((a["op"], a["target"]) for a in ex.sql_access) == [("read", "customers"), ("write", "fraud_alerts")]
    sp, _ = extract_commit("analytics-spark", stfx["paths"]["analytics-spark"], "main", None)
    assert [f.qualname for f in sp.functions] == ["<job orders_daily.py>"]
    assert sorted((a["op"], a["target"]) for a in sp.sql_access) == [("read", "dim_customers"), ("write", "orders_daily")]
