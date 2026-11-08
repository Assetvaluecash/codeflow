"""Event landscape (PRD FR-019, FR-020 events, AC-028): one producer, three kinds of consumer.

orders-service     Python producer: OrderEvents.publish() -> producer.send("orders.created", ...); Avro schema schemas/orders-created.avsc,
                   declared in tia-services.yaml (events.produces with its schema)
billing-service    Java consumer: @KafkaListener(topics = "orders.created")
shipping-service   Python consumer: KafkaConsumer("orders.created") in poll()
audit-service      declares in tia-services.yaml that it consumes orders.created (no code)

Branches on orders-service: field_removed (amount removed: breaking), field_added (optional field: compatible), enum_added (status gains
REFUNDED: breaking for consumers), stopped (the topic is no longer produced: breaking).
"""
from __future__ import annotations

import json
from pathlib import Path

from fixtures.build_fixture import _commit, _run, _write

SCHEMA = {"type": "record", "name": "OrderCreated", "namespace": "acme.orders", "fields": [
    {"name": "order_id", "type": "string"}, {"name": "amount", "type": "double"},
    {"name": "status", "type": {"type": "enum", "name": "Status", "symbols": ["NEW", "PAID"]}},
    {"name": "note", "type": ["null", "string"], "default": None}]}

PRODUCER = '''from kafka import KafkaProducer

TOPIC = "orders.created"
producer = KafkaProducer(bootstrap_servers="kafka:9092")


def publish(order):
    producer.send(TOPIC, {"order_id": order["id"], "amount": order["amount"], "status": "NEW"})


def create_order(order):
    publish(order)
    return order
'''

BILLING = '''package com.acme.billing;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class OrderListener {
    @KafkaListener(topics = "orders.created", groupId = "billing")
    public void onOrder(String payload) {
        bill(payload);
    }

    void bill(String payload) { }
}
'''

SHIPPING = '''from kafka import KafkaConsumer


def poll():
    consumer = KafkaConsumer("orders.created", group_id="shipping")
    for msg in consumer:
        ship(msg.value)


def ship(order):
    return order
'''

SHIPPING_TEST = '''from consumer import ship


def test_ship():
    assert ship({"id": 1}) == {"id": 1}
'''


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

    p, base = repo("orders-service", {
        "producer.py": PRODUCER, "schemas/orders-created.avsc": json.dumps(SCHEMA, indent=2),
        "tia-services.yaml": "service: orders-service\ncriticality: critical\nevents:\n  produces:\n"
                             "    - topic: orders.created\n      schema: schemas/orders-created.avsc\n"})
    out["shas"]["base"] = base
    repo("billing-service", {"src/main/java/com/acme/billing/OrderListener.java": BILLING, "tia-services.yaml": "service: billing-service\n"})
    repo("shipping-service", {"consumer.py": SHIPPING, "tests/test_consumer.py": SHIPPING_TEST, "tia-services.yaml": "service: shipping-service\n"})
    repo("audit-service", {"README.md": "audit\n", "tia-services.yaml": "service: audit-service\nevents:\n  consumes:\n    - topic: orders.created\n"})

    def branch(name, files):
        _run(p, "checkout", "-q", "-b", name, base)
        _write(p, files)
        out["shas"][name] = _commit(p, name)

    removed = {**SCHEMA, "fields": [f for f in SCHEMA["fields"] if f["name"] != "amount"]}
    added = {**SCHEMA, "fields": SCHEMA["fields"] + [{"name": "channel", "type": ["null", "string"], "default": None}]}
    enum = {**SCHEMA, "fields": [f if f["name"] != "status" else {"name": "status", "type": {"type": "enum", "name": "Status",
                                                                                              "symbols": ["NEW", "PAID", "REFUNDED"]}} for f in SCHEMA["fields"]]}
    branch("field_removed", {"schemas/orders-created.avsc": json.dumps(removed, indent=2)})
    branch("field_added", {"schemas/orders-created.avsc": json.dumps(added, indent=2)})
    branch("enum_added", {"schemas/orders-created.avsc": json.dumps(enum, indent=2)})
    branch("stopped", {"producer.py": PRODUCER.replace('    producer.send(TOPIC, {"order_id": order["id"], "amount": order["amount"], "status": "NEW"})',
                                                       '    return {"order_id": order["id"]}'),
                       "tia-services.yaml": "service: orders-service\ncriticality: critical\n"})
    _run(p, "checkout", "-q", "main")
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(build(sys.argv[1]), indent=2))
