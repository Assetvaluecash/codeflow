from tia.extract_python import extract_repo
from tia.github import find_marker_comment
from tia.models import normalize_path
from tia.severity import compute, is_material


def test_normalize_path():
    assert normalize_path("/Payments/{id}/Status/") == "/payments/{}/status"
    assert normalize_path("/a/:id/b?x=1") == "/a/{}/b"
    assert normalize_path("/a/<int:id>") == "/a/{}"
    assert normalize_path("/a/${id}") == "/a/{}"
    assert normalize_path("/api/v1/x", ("/api/v1",)) == "/x"


def test_extract_fastapi_router_prefix_and_flask_methods():
    ex = extract_repo("r", "c", {"a.py": (
        "from fastapi import APIRouter\nfrom flask import Flask\n"
        "router = APIRouter(prefix='/v1')\n"
        "@router.get('/things/{id}')\ndef get_thing(id): pass\n"
        "app = Flask(__name__)\n"
        "@app.route('/old', methods=['POST','PUT'])\ndef old(): pass\n")})
    ids = sorted(e.id for e in ex.endpoints)
    assert ids == ["r:GET /v1/things/{}", "r:POST /old", "r:PUT /old"]


def test_extract_callsite_variants():
    src = ("import os, requests, httpx\n"
           "BASE = os.getenv('PAY_URL')\n"
           "def a(i): return requests.get(f'{BASE}/p/{i}')\n"
           "def b(i): return httpx.post('http://pay-svc/p/' + i)\n"
           "def c(u): return requests.get(u)\n")
    cs = {c.function_id.split('::')[1]: c for c in extract_repo("r", "c", {"m.py": src}).callsites}
    assert (cs["a"].base_kind, cs["a"].base_value, cs["a"].norm_path) == ("config_key", "PAY_URL", "/p/{}")
    assert (cs["b"].base_kind, cs["b"].base_value, cs["b"].norm_path) == ("host", "pay-svc", "/p/{}")
    assert cs["c"].norm_path is None and cs["c"].reason


def test_extract_syntax_error_is_reported_not_swallowed():
    ex = extract_repo("r", "c", {"ok.py": "x = 1\n", "bad.py": "def (:\n"})
    assert ex.files_analysed == 1 and ex.files_failed[0]["file"] == "bad.py"


def test_severity_table():
    assert compute("breaking", "source_confirmed", True, "critical") == ("critical", "SEV-BREAKING-CRITICAL")
    assert compute("breaking", "source_confirmed", True, None) == ("high", "SEV-BREAKING-CONFIRMED")
    assert compute("breaking", "inferred", True, "critical") == ("medium", "SEV-INFERRED-CAP")
    assert compute("nonbreaking", "contract_confirmed", True, "critical") == ("high", "SEV-NONBREAKING-CRITICAL")
    assert compute("nonbreaking", "source_confirmed", True, None) == ("medium", "SEV-NONBREAKING-CONFIRMED")
    assert compute("breaking", "source_confirmed", False, "critical") == ("low", "SEV-LOCAL")
    assert compute("unclassified", "source_confirmed", True, None) == ("unknown", "SEV-UNCLASSIFIED")
    assert is_material("low", True) and not is_material("low", False)


def test_github_marker_lookup():
    cs = [{"id": 1, "body": "hi"}, {"id": 2, "body": "x <!-- tia-analysis --> y"}]
    assert find_marker_comment(cs)["id"] == 2 and find_marker_comment(cs[:1]) is None


def test_a_base_url_read_into_a_local_variable_resolves():
    """base = os.getenv(...) inside the function resolves like a module constant; a local given two different values stays unresolved."""
    src = ("import os\nimport requests\n\n\ndef status(pid):\n    base = os.getenv('PAYMENTS_URL', 'http://payments')\n"
           "    return requests.get(f'{base}/payments/{pid}', timeout=3)\n\n\n"
           "def either(pid, eu):\n    base = os.getenv('EU_URL')\n    if not eu:\n        base = os.getenv('US_URL')\n"
           "    return requests.get(base + '/payments/' + pid)\n")
    ex = extract_repo("r", "c", {"client.py": src})
    by = {c.function_id.rsplit("::", 1)[-1]: c for c in ex.callsites}
    assert (by["status"].base_kind, by["status"].base_value, by["status"].norm_path) == ("config_key", "PAYMENTS_URL", "/payments/{}")
    assert by["either"].norm_path is None and "not resolvable" in by["either"].reason
