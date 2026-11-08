"""Release 4 intelligence (PRD FR-043 trends, FR-044 calibration / AC-038, FR-045 multi-team coordination) over the real application."""
import json
import os
import smtplib

import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import make_app, new_tenant
from fakesnow import FakeServiceNow
from fixtures.build_polyglot_fixture import build as build_poly
from tia.credentials import LocalKeyWrapper
from tia.secretstore import SecretStore
from tia.tenantconfig import ConfigError, parse
from tlsutil import make_pki

OWNERS = {"pricing-service": "team-pricing", "orders-service": "team-orders", "billing-service": "team-billing", "ledger-service": "team-ledger"}


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def has_extn(self, name):
        return name == "starttls"

    def starttls(self, context=None):
        self.tls = True

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append({"to": msg["To"], "subject": msg["Subject"], "body": msg.get_content(), "tls": getattr(self, "tls", False)})


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    poly = build_poly(str(tmp_path_factory.mktemp("r4")))
    tid = new_tenant()
    store = SecretStore(conftest.PG.db, tid, LocalKeyWrapper({"k1": os.urandom(32)}, "k1"))
    c = TestClient(make_app(tmp_path_factory.mktemp("ws"), tenant=tid, allow_local=True, secrets=store))
    for n, p in poly["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    assert c.put("/api/v1/tenant/config", json={"config": {"ownership": OWNERS}}).status_code == 200
    rep = c.post("/api/v1/impact-analyses", json={"target": "pricing-service", "head": "field_removed"}).json()["report"]
    impl = c.post("/api/v1/impact-analyses", json={"target": "pricing-service", "head": "impl_only"}).json()["report"]
    return {"c": c, "rep": rep, "impl": impl, "poly": poly, "secrets": store, "tid": tid}


# ---- FR-044 / AC-038: verdicts and per-rule precision; stored reports unchanged --------------------------------------------------------
def test_verdicts_give_per_rule_precision_and_never_change_reports(world):
    c, rep = world["c"], world["rep"]
    before = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}").json()
    by = {f["component"]: f for f in rep["findings"]}
    fo, fb = by["orders-service"]["id"], by["billing-service"]["id"]
    assert c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/verdicts", json={"finding_id": fo, "verdict": "confirmed"}).status_code == 200
    assert c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/verdicts", json={"finding_id": fb, "verdict": "confirmed"}).status_code == 200
    r = c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/verdicts", json={"finding_id": fb, "verdict": "false_positive", "comment": "dead code"})
    assert r.status_code == 200 and r.json()["rules"]["severity_rule"] == by["billing-service"]["severity_rule"]
    assert c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/verdicts", json={"finding_id": "F999", "verdict": "confirmed"}).status_code == 404
    assert c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/verdicts", json={"finding_id": fo, "verdict": "maybe"}).status_code == 422
    cal = c.get("/api/v1/calibration").json()
    rule = by["orders-service"]["severity_rule"]
    row = cal["by"]["severity_rule"][rule]
    assert (row["confirmed"], row["false_positive"], row["judged"]) == (1, 1, 2) and row["precision"] == 0.5    # the later verdict wins
    assert row["too_few_to_act_on"] and cal["minimum_sample"] == 10
    assert cal["by"]["resolution_strategy"][by["orders-service"]["resolution_strategy"]]["judged"] >= 1
    # a rule change applies to new analyses only: the stored report is byte-identical
    assert c.put("/api/v1/tenant/config", json={"config": {"ownership": OWNERS, "severity": {"overrides": {rule: "low"}}}}).status_code == 200
    assert c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}").json() == before
    assert c.put("/api/v1/tenant/config", json={"config": {"ownership": OWNERS}}).status_code == 200


def test_incident_outcomes_confirm_findings(world):
    c, impl = world["c"], world["impl"]
    rev = impl["change"]["proposed_revision"]
    inc = {"id": "INC-42", "service": "orders-service", "started_at": "2026-10-30T10:00:00Z", "changes": [{"repo": "pricing-service", "revision": rev}]}
    assert c.post("/api/v1/incidents", content=json.dumps(inc)).status_code == 200
    cal = c.get("/api/v1/calibration").json()
    f = next(f for f in impl["findings"] if f["component"] == "orders-service")
    row = cal["by"]["severity_rule"][f["severity_rule"]]
    assert row["from_incidents"] >= 1 and row["confirmed"] >= 1


# ---- FR-043: descriptive trends ------------------------------------------------------------------------------------------------------
def test_trends_are_descriptive_counts_per_application_and_team(world):
    c = world["c"]
    t = c.get("/api/v1/trends?group=application&period=month").json()
    assert "not probabilities" in t["note"]
    (row,) = [r for r in t["rows"] if r["application"] == "pricing-service"]
    assert row["change_volume"] == 2 and row["breaking_changes"] == 1 and row["breaking_share"] == 0.5
    assert 0 <= row["missing_verification_share"] <= 1 and row["incident_associations"] == 1
    teams = c.get("/api/v1/trends?group=team&period=quarter").json()["rows"]
    assert [r["team"] for r in teams] == ["team-pricing"] and teams[0]["period"].count("-Q") == 1
    assert c.get("/api/v1/trends?group=planet").status_code == 400


# ---- FR-045: notify owning teams, track acknowledgements, policy rule ------------------------------------------------------------------
@pytest.fixture
def snow(tmp_path, monkeypatch):
    pki = make_pki(tmp_path / "pki")
    monkeypatch.setenv("TIA_IDP_CA_FILE", pki["ca"])
    monkeypatch.setenv("TIA_ALLOW_PRIVATE_ITSM_HOSTS", "1")
    monkeypatch.setenv("TIA_ALLOW_PRIVATE_NOTIFY_HOSTS", "1")
    s = FakeServiceNow(pki)
    yield s
    s.close()


def test_coordination_configuration_is_validated():
    assert parse({"coordination": {"channels": {"t": {"type": "email", "to": "x@bank.example"}}}}).coordination["channels"]["t"]["type"] == "email"
    for bad in ({"coordination": {"channels": {"t": {"type": "pager"}}}}, {"coordination": {"channels": {"t": {"type": "email"}}}},
                {"coordination": {"channels": {"t": {"type": "servicenow_task"}}}}, {"coordination": {"auto_notify": "yes"}}):
        with pytest.raises(ConfigError):
            parse(bad)


def test_teams_are_notified_once_acknowledge_and_the_policy_follows(world, snow, monkeypatch):
    c, rep, aid = world["c"], world["rep"], world["rep"]["analysis_id"]
    FakeSMTP.sent.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("TIA_SMTP_HOST", "smtp.bank.example")
    world["secrets"].put("coordination/team-orders/webhook", snow.url + "/hooks/orders")
    cfg = {"ownership": OWNERS, "itsm": {"provider": "servicenow", "instance_url": snow.url},
           "coordination": {"channels": {"team-orders": {"type": "slack"}, "team-billing": {"type": "email", "to": "billing-oncall@bank.example"}}},
           "policies": {"mode": "enforcing", "rules": [{"id": "acks", "type": "teams_acknowledged"}]}}
    assert c.put("/api/v1/tenant/config", json={"config": cfg}).status_code == 200
    rep = c.post("/api/v1/impact-analyses", json={"target": "pricing-service", "head": "field_removed"}).json()["report"]    # under the policy
    aid = rep["analysis_id"]
    assert rep["policy_results"][0]["outcome"] == "requires_review"                          # at analysis time nobody has acknowledged
    st = c.get(f"/api/v1/impact-analyses/{aid}/coordination").json()
    assert [(t["team"], t["status"]) for t in st["teams"]] == [("team-billing", "pending"), ("team-orders", "pending")]
    st = c.post(f"/api/v1/impact-analyses/{aid}/notifications").json()
    assert {t["team"]: t["status"] for t in st["teams"]} == {"team-billing": "notified", "team-orders": "notified"}
    assert len(snow.hooks) == 1 and "affects 1 component(s) owned by team-orders" in snow.hooks[0]["json"]["text"], (snow.hooks, st)
    assert FakeSMTP.sent[0]["to"] == "billing-oncall@bank.example" and FakeSMTP.sent[0]["tls"] and aid in FakeSMTP.sent[0]["body"]
    c.post(f"/api/v1/impact-analyses/{aid}/notifications")                                    # once per team: nothing is sent twice
    assert len(snow.hooks) == 1 and len(FakeSMTP.sent) == 1
    pol = c.get(f"/api/v1/impact-analyses/{aid}/policy").json()
    assert pol["results"][0]["outcome"] == "requires_review" and "team-billing" in pol["results"][0]["reason"]
    assert c.post(f"/api/v1/impact-analyses/{aid}/acknowledgements", json={"team": "team-pricing"}).status_code == 400   # not affected
    c.post(f"/api/v1/impact-analyses/{aid}/acknowledgements", json={"team": "team-orders", "comment": "on it"})
    r = c.post(f"/api/v1/impact-analyses/{aid}/acknowledgements", json={"team": "team-billing"}).json()
    assert r["coordination"]["complete"] and r["policy"]["results"][0]["outcome"] == "pass"
    assert all(t["acknowledged"]["by"] for t in r["coordination"]["teams"])
    events = {e["event"] for e in c.get("/api/v1/audit?limit=50").json()}
    assert {"team_notified", "team_acknowledged"} <= events


def test_a_servicenow_task_and_a_failed_channel(world, snow, monkeypatch):
    c, poly = world["c"], world["poly"]
    rep = c.post("/api/v1/impact-analyses", json={"target": "pricing-service", "head": "impl_only", "max_depth": 7}).json()["report"]
    world["secrets"].put("itsm/servicenow/username", "tia")
    world["secrets"].put("itsm/servicenow/password", "secret-pw")
    cfg = {"ownership": OWNERS, "itsm": {"provider": "servicenow", "instance_url": snow.url},
           "coordination": {"channels": {"team-orders": {"type": "servicenow_task", "assignment_group": "Orders Engineering"}},
                            "default": {"type": "teams"}}}
    assert c.put("/api/v1/tenant/config", json={"config": cfg}).status_code == 200
    world["secrets"].delete("coordination/team-orders/webhook") if hasattr(world["secrets"], "delete") else None
    st = c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/notifications").json()
    by = {t["team"]: t for t in st["teams"]}
    assert by["team-orders"]["status"] == "notified" and by["team-orders"]["notification"]["detail"].startswith("task TASK")
    assert snow.tasks[0]["assignment_group"] == "Orders Engineering" and snow.tasks[0]["correlation_id"].startswith(f"tia-{rep['analysis_id']}")
    assert by["team-billing"]["status"] == "notification_failed" and "webhook" in by["team-billing"]["notification"]["detail"]
    assert not any(r["method"] in ("PUT", "PATCH", "DELETE") for r in snow.requests)          # TIA creates the task and nothing else
    # the failed team is retried once a channel works; a delivered one is never sent again
    world["secrets"].put("coordination/default/webhook", snow.url + "/hooks/default")
    hooks, tasks = len(snow.hooks), len(snow.tasks)
    st = c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/notifications").json()
    assert {t["team"]: t["status"] for t in st["teams"]} == {"team-billing": "notified", "team-orders": "notified"}
    assert len(snow.hooks) == hooks + 1 and len(snow.tasks) == tasks
    c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/notifications")
    assert len(snow.hooks) == hooks + 1 and len(snow.tasks) == tasks
