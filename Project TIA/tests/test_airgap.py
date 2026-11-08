"""Air-gapped operation: with TIA_AIRGAPPED every outbound connection is refused unless its host is local or explicitly allowed, and the
analysis itself needs no network."""
import json
import urllib.request

import pytest
from fastapi.testclient import TestClient

from conftest import fresh_store, make_app
from fixtures.build_polyglot_fixture import build as build_poly
from tia import airgap, github, kms, netsafe
from tia.repos import RepoService
from tia.transport import check_outbound


@pytest.fixture
def gapped(monkeypatch):
    monkeypatch.setenv("TIA_AIRGAPPED", "true")
    monkeypatch.setenv("TIA_AIRGAP_ALLOWED_HOSTS", "git.bank.internal, *.corp.example")


def test_hosts_inside_and_outside_the_network(gapped):
    for ok in ("https://git.bank.internal/x", "https://vault.corp.example:8200", "https://corp.example/", "http://localhost:9000/a",
               "C:\\repos\\orders", "/srv/repos/orders", "file:///srv/repos/orders", "git@git.bank.internal:team/orders.git"):
        assert airgap.guard(ok, "t") == ok
    for bad in ("https://api.github.com/repos", "https://evilcorp.example.com", "git@github.com:acme/orders.git",
                "ssh://git@bitbucket.org/acme/x.git", "https://kms.eu-west-1.amazonaws.com/"):
        with pytest.raises(airgap.AirGapError, match="outside the air-gapped network"):
            airgap.guard(bad, "t")


def test_not_air_gapped_changes_nothing(monkeypatch):
    monkeypatch.delenv("TIA_AIRGAPPED", raising=False)
    assert airgap.guard("https://api.github.com/x", "t") == "https://api.github.com/x"


def test_every_connector_is_refused_before_any_connection_is_made(gapped, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("a connection was attempted")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    with pytest.raises(airgap.AirGapError):
        github.upsert_comment("acme/orders", 1, "body", "token")
    with pytest.raises(airgap.AirGapError):
        netsafe.request("GET", "https://login.microsoftonline.com/x/.well-known/openid-configuration")
    with pytest.raises(airgap.AirGapError):
        kms._http("POST", "https://kms.eu-west-1.amazonaws.com/", {}, b"{}")
    with pytest.raises(airgap.AirGapError):
        check_outbound("https://otlp.vendor.example/v1/traces", "TIA_OTLP_ENDPOINT")


def test_a_public_git_remote_is_refused_but_analysis_of_local_repositories_works(gapped, tmp_path):
    svc = RepoService(fresh_store(), str(tmp_path / "ws"), allow_local=True)
    with pytest.raises(airgap.AirGapError, match="git clone: github.com"):
        svc.register(url="https://github.com/acme/orders.git")
    assert svc.list() == []
    poly = build_poly(str(tmp_path / "land"))
    c = TestClient(make_app(tmp_path / "ws2", allow_local=True))
    for n, p in poly["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    r = c.post("/api/v1/impact-analyses", json={"target": "pricing-service", "head": "field_removed"})
    assert r.status_code in (200, 202), r.text
    body = r.json()
    rep = body if "findings" in body else c.get(f"/api/v1/impact-analyses/{body['analysis_id']}").json()
    assert {f["component"] for f in rep["findings"]} == {"orders-service", "billing-service"}
    assert json.dumps(rep).count("github.com") == 0
