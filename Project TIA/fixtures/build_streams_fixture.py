"""Event-stream lineage landscape (PRD FR-042): multi-hop topics and pipelines declared in source control.

orders-service         Python producer of orders.created (Avro schema declared in tia-services.yaml)
enrichment-streams     Java Kafka Streams topology: stream(ORDERS_TOPIC) -> mapValues -> to("orders.enriched")
fraud-service          Python: consumes orders.enriched and re-publishes fraud.alerts from the same handler
notification-service   Java @KafkaListener(topics = "fraud.alerts")
analytics-spark        PySpark job script (module level): readStream subscribe orders.enriched -> writeStream toTable analytics.orders_daily;
                       reads dim_customers with spark.table
warehouse-connect      Kafka Connect: a JDBC sink of fraud.alerts into fraud_alerts, a Debezium source of crm.customers
Branch on orders-service: field_removed (amount removed from the orders.created schema).
"""
from __future__ import annotations

import json
from pathlib import Path

from fixtures.build_fixture import _commit, _run, _write

SCHEMA = {"type": "record", "name": "OrderCreated", "namespace": "acme", "fields": [
    {"name": "order_id", "type": "string"}, {"name": "amount", "type": "double"}, {"name": "customer_id", "type": "string"}]}

PRODUCER = '''from kafka import KafkaProducer

producer = KafkaProducer(bootstrap_servers="kafka:9092")


def publish(order):
    producer.send("orders.created", {"order_id": order["id"], "amount": order["amount"]})
'''

STREAMS = '''package com.acme.enrich;

import org.apache.kafka.streams.StreamsBuilder;
import org.apache.kafka.streams.kstream.KStream;

public class EnrichmentTopology {
    static final String ORDERS_TOPIC = "orders.created";

    public void build(StreamsBuilder builder) {
        KStream<String, String> orders = builder.stream(ORDERS_TOPIC);
        orders.mapValues(v -> enrich(v)).to("orders.enriched");
    }

    String enrich(String v) { return v; }
}
'''

FRAUD = '''from kafka import KafkaConsumer, KafkaProducer

producer = KafkaProducer(bootstrap_servers="kafka:9092")


def watch():
    consumer = KafkaConsumer("orders.enriched", group_id="fraud")
    for msg in consumer:
        if msg.value.get("amount", 0) > 10000:
            producer.send("fraud.alerts", msg.value)
'''

NOTIFY = '''package com.acme.notify;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class AlertListener {
    @KafkaListener(topics = "fraud.alerts", groupId = "notify")
    public void onAlert(String payload) { }
}
'''

SPARK = '''from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("orders-daily").getOrCreate()
customers = spark.table("dim_customers")
orders = (spark.readStream.format("kafka")
          .option("kafka.bootstrap.servers", "kafka:9092")
          .option("subscribe", "orders.enriched")
          .load())
(orders.join(customers, "customer_id").writeStream
       .option("checkpointLocation", "/chk/orders_daily")
       .toTable("analytics.orders_daily"))
'''

SINK = {"name": "fraud-alerts-sink", "config": {"connector.class": "io.confluent.connect.jdbc.JdbcSinkConnector", "topics": "fraud.alerts",
                                                "table.name.format": "fraud_alerts", "connection.url": "jdbc:postgresql://dwh/warehouse"}}
SOURCE = {"name": "crm-cdc", "config": {"connector.class": "io.debezium.connector.postgresql.PostgresConnector", "topic.prefix": "crm",
                                        "table.include.list": "public.customers"}}


def build(dest: str) -> dict:
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    out = {"paths": {}, "shas": {}}

    def repo(name, files):
        p = root / name
        p.mkdir(parents=True, exist_ok=True)
        _run(p, "init", "-q", "-b", "main")
        _write(p, files)
        out["paths"][name] = str(p)
        return p, _commit(p, "initial")

    p, base = repo("orders-service", {"producer.py": PRODUCER, "schemas/orders-created.avsc": json.dumps(SCHEMA, indent=2),
                                      "tia-services.yaml": "service: orders-service\nevents:\n  produces:\n"
                                                           "    - topic: orders.created\n      schema: schemas/orders-created.avsc\n"})
    out["shas"]["base"] = base
    repo("enrichment-streams", {"src/main/java/com/acme/enrich/EnrichmentTopology.java": STREAMS, "tia-services.yaml": "service: enrichment-streams\n"})
    repo("fraud-service", {"fraud.py": FRAUD, "tia-services.yaml": "service: fraud-service\n"})
    repo("notification-service", {"src/main/java/com/acme/notify/AlertListener.java": NOTIFY, "tia-services.yaml": "service: notification-service\n"})
    repo("analytics-spark", {"jobs/orders_daily.py": SPARK, "tia-services.yaml": "service: analytics-spark\ndatabase: warehouse\n"})
    repo("warehouse-connect", {"connect/fraud-alerts-sink.json": json.dumps(SINK, indent=2), "connect/crm-cdc.json": json.dumps(SOURCE, indent=2),
                               "tia-services.yaml": "service: warehouse-connect\ndatabase: warehouse\n"})
    _run(p, "checkout", "-q", "-b", "field_removed", base)
    _write(p, {"schemas/orders-created.avsc": json.dumps({**SCHEMA, "fields": [f for f in SCHEMA["fields"] if f["name"] != "amount"]}, indent=2)})
    out["shas"]["field_removed"] = _commit(p, "amount removed")
    _run(p, "checkout", "-q", "main")
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(build(sys.argv[1]), indent=2))
