"""UC1-25: the accuracy harness and what it found. The synthetic landscape always runs (a regression gate); the two independent real-code references
run when their sources are checked out (TIA_REF_PETCLINIC / TIA_REF_PIGGYMETRICS, default C:\\tiaref\\spm and C:\\tiaref\\pm) and are skipped otherwise."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import accuracy  # noqa: E402
from tia.extract_java import extract_file  # noqa: E402
from tia.impact import _without_files  # noqa: E402

PETCLINIC = Path(os.environ.get("TIA_REF_PETCLINIC", r"C:\tiaref\spm"))
PIGGY = Path(os.environ.get("TIA_REF_PIGGYMETRICS", r"C:\tiaref\pm"))


def load(name):
    return json.loads((ROOT / "reference" / name / "reference.json").read_text(encoding="utf-8"))


def run_reference(name, source, tmp_path):
    ref = load(name)
    mat = accuracy.materialize(ref, source, tmp_path)
    return ref, mat, accuracy.evaluate(ref, mat)


# ---- the references themselves ---------------------------------------------------------------------------------------------------
def test_the_synthetic_landscape_meets_every_target(tmp_path):
    ref, mat, r = run_reference("synthetic-landscape", None, tmp_path)
    assert r["verdict"]["all_measured_targets_met"], accuracy.markdown(r)
    assert r["recall"]["value"] == 1.0 and r["precision"]["value"] == 1.0 and r["silent_pass"]["value"] == 0
    assert r["independent"] is False                                          # and it says so


@pytest.mark.skipif(not (PETCLINIC / "spring-petclinic-api-gateway").is_dir(), reason="spring-petclinic-microservices is not checked out")
def test_independent_reference_spring_petclinic_microservices(tmp_path):
    ref, mat, r = run_reference("spring-petclinic-microservices", PETCLINIC, tmp_path)
    assert r["independent"] is True
    assert r["verdict"]["all_measured_targets_met"], accuracy.markdown(r)
    assert r["recall"]["found"] == r["recall"]["of"] == 6
    assert r["edge_precision"]["false_edges"] == []
    assert r["endpoint_recall"]["value"] == 1.0
    segments = r["recall"]["by"]
    assert segments["difficulty=RestClient + service discovery by name"]["found"] == 2        # fixed after the harness found it missing
    assert segments["difficulty=local variable + literal"]["found"] == 1                       # likewise


@pytest.mark.skipif(not (PIGGY / "account-service").is_dir(), reason="piggymetrics is not checked out")
def test_independent_reference_piggymetrics(tmp_path):
    ref, mat, r = run_reference("piggymetrics", PIGGY, tmp_path)
    assert r["independent"] is True and r["verdict"]["all_measured_targets_met"], accuracy.markdown(r)
    assert r["recall"]["found"] == 3
    assert r["configuration"]["dependencies_found_without_it"] == 0                           # context paths NEED the tenant configuration
    assert r["resolution"]["unresolvable_resolved_wrongly"] == []                              # the external rates API is not linked to anything


# ---- the harness must be able to fail --------------------------------------------------------------------------------------------
def test_a_wrong_reference_fails_the_gates(tmp_path):
    ref = load("synthetic-landscape")
    ref["dependencies"].append({"id": "X", "consumer": "notifications", "provider": "payments-service", "method": "DELETE", "path": "/nope", "language": "python"})
    ref["changes"][1]["expected_material"] = []                                                # the 'impl' change now has no expected consumers
    ref["changes"][0]["expected_material"].append({"component": "audit-reporting-does-not-exist"})
    mat = accuracy.materialize(ref, None, tmp_path)
    r = accuracy.evaluate(ref, mat)
    assert r["recall"]["value"] < 1.0 and [d["id"] for d in r["recall"]["missed"]] == ["X"]
    assert r["precision"]["value"] < 0.8 and r["precision"]["false_positives"]
    assert "audit-reporting-does-not-exist" in r["changes"][0]["missing"]
    assert r["verdict"]["all_measured_targets_met"] is False and r["verdict"]["precision"] is False


def test_a_silent_pass_is_counted(tmp_path, monkeypatch):
    ref = load("synthetic-landscape")
    mat = accuracy.materialize(ref, None, tmp_path)
    from tia import impact
    real = impact.analyze

    def lying(req, store=None, persist=True):
        r = real(req, store, persist)
        if req.head == mat["changes"]["syntax_error"]["head"]:
            r = {**r, "status": "complete"}                                                    # a report that hides the unparseable file
        return r
    monkeypatch.setattr(impact, "analyze", lying)
    r = accuracy.evaluate(ref, mat)
    assert r["silent_pass"]["value"] == 1 and r["silent_pass"]["cases"][0]["change"] == "syntax_error" and r["verdict"]["silent_pass"] is False


def test_evidence_that_does_not_exist_is_not_traceable(tmp_path):
    ref = load("synthetic-landscape")
    mat = accuracy.materialize(ref, None, tmp_path)
    revs = dict(mat["head"])
    assert accuracy.evidence_traceable(mat, {"repo": "reconciliation-service", "file": "client.py", "line": 8}, revs)
    assert accuracy.evidence_traceable(mat, {"repo": "audit-reporting", "file": "contracts/payments.json", "line": 0}, revs)      # whole-file evidence
    for bad in ({"repo": "reconciliation-service", "file": "client.py", "line": 999}, {"repo": "reconciliation-service", "file": "gone.py", "line": 1},
                {"repo": "nope", "file": "client.py", "line": 1}, {"repo": "reconciliation-service", "file": "client.py", "line": 3}, {}):
        assert not accuracy.evidence_traceable(mat, bad, revs), bad                            # line 3 is blank


def test_a_patch_that_no_longer_matches_the_source_stops_the_run(tmp_path):
    src = tmp_path / "src" / "svc"
    src.mkdir(parents=True)
    (src / "A.java").write_text("class A { }\nclass A { }\n")
    ref = {"name": "t", "source": {"type": "modules"}, "services": {"svc": "svc"}, "dependencies": [],
           "changes": [{"id": "c", "description": "d", "target": "svc", "patch": [{"file": "A.java", "find": "class A { }", "replace": "x"}], "expected_material": []}]}
    with pytest.raises(SystemExit, match="exactly once"):
        accuracy.materialize(ref, tmp_path / "src", tmp_path / "work")


def test_path_templates_compare_by_shape():
    n = accuracy.norm_path
    assert n("/owners/{ownerId}") == n("owners/{id}/") == "/owners/{}" and n("/owners/*/pets/{petId}") == "/owners/{}/pets/{}" and n("/") == "/" and n("pets/visits?petId=1") == "/pets/visits"


def test_the_command_line(tmp_path):
    out = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "accuracy.py"), "reference/synthetic-landscape", "--enforce", "--report", str(tmp_path / "r.json")],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0 and "Accuracy: synthetic-landscape" in out.stdout and "NOT independent" in out.stdout
    assert json.loads((tmp_path / "r.json").read_text())["verdict"]["all_measured_targets_met"] is True


# ---- what the harness found, kept fixed -----------------------------------------------------------------------------------------
JAVA = """package com.acme;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.client.RestClient;
import org.springframework.cloud.client.discovery.DiscoveryClient;
import java.net.URI;
class Calls {
    private final RestClient restClient = RestClient.builder().build();
    private DiscoveryClient discoveryClient;
    WebClient.Builder b;
    URI customers() { return discoveryClient.getInstances("customers-service").get(0).getUri(); }
    void a() { restClient.get().uri(customers() + "/owners").retrieve(); }
    void c(int id) { restClient.post().uri(customers() + "/owners/" + id + "/pets").retrieve(); }
    void local() {
        String hostname = "http://vets-service/";
        b.build().get().uri(hostname + "vets").retrieve();
    }
}
"""


def calls(src):
    ex = extract_file("repo", "svc", "src/main/java/com/acme/Calls.java", src)
    return {(c.method, c.norm_path, c.base_value, c.reason) for c in ex.callsites}


def test_a_method_that_returns_the_discovered_host_resolves_the_call():
    got = calls(JAVA)
    assert ("GET", "/owners", "customers-service", None) in got and ("POST", "/owners/{}/pets", "customers-service", None) in got


def test_a_local_variable_holding_a_url_resolves_the_call():
    assert ("GET", "/vets", "vets-service", None) in calls(JAVA)


def test_a_local_variable_with_two_different_values_stays_unresolved():
    src = """package com.acme;
import org.springframework.web.reactive.function.client.WebClient;
class C {
    WebClient.Builder b;
    void one() { String host = "http://a-service/"; b.build().get().uri(host + "x").retrieve(); }
    void two() { String host = "http://b-service/"; b.build().get().uri(host + "y").retrieve(); }
}
"""
    got = calls(src)
    assert not any(v == "a-service" or v == "b-service" for _, _, v, _ in got) and any(r for *_, r in got)        # reported, never guessed


def test_a_method_returning_something_unknowable_stays_unresolved_and_recursion_is_safe():
    src = """package com.acme;
import org.springframework.web.reactive.function.client.WebClient;
class C {
    WebClient.Builder b;
    String base() { return other(); }
    String other() { return base(); }
    void go() { b.build().get().uri(base() + "/z").retrieve(); }
}
"""
    got = calls(src)
    assert got and all(base is None for _, _, base, _ in got)


def test_a_file_that_stopped_parsing_is_not_the_same_as_removed_declarations():
    from tia.models import Endpoint, RepoExtract, Schema
    ep = lambda f, p, resp: Endpoint(id=f"s:GET {p}", service="s", repo="r", method="GET", path=p, norm_path=p, handler=f"{f}:h", handler_id=f"{f}:h", file=f, line=1,
                                     response_schema=resp, request_schemas=[])
    sc = lambda n: Schema(id=f"s:schema:{n}", service="s", repo="r", name=n, file="Model.java", line=1, fields={})
    ex = RepoExtract(repo="r", service="s", commit="c", extractor_version="t", endpoints=[ep("A.java", "/a", "Owner"), ep("B.java", "/b", "Pet")], schemas=[sc("Owner"), sc("Pet")])
    kept = _without_files(ex, {"A.java"})
    assert [e.path for e in kept.endpoints] == ["/b"]
    assert [x.name for x in kept.schemas] == ["Pet"]                          # Owner is used only through the unreadable endpoint: its use cannot be told either
    assert _without_files(ex, set()) is ex
