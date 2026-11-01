"""UC1-16: the shareable report page per analysis. Real PostgreSQL, real git fixtures; the page is server-rendered HTML, so it is
checked as HTML (parsed with the standard library) rather than by eye."""
import tempfile
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_data_fixture import build as build_data
from tia.impact import AnalysisRequest, analyze
from tia.report import permalink
from tia.reportpage import render_page


class Page(HTMLParser):
    """Collects what a browser would act on: elements, links, scripts, event handlers and visible text."""

    def __init__(self):
        super().__init__()
        self.tags, self.hrefs, self.attrs, self.text, self.ids = [], [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            self.attrs.append((tag, k, v))
            if k == "href":
                self.hrefs.append(v)
            if k == "id":
                self.ids.append(v)

    def handle_data(self, data):
        self.text.append(data)


def parse(html):
    p = Page()
    p.feed(html)
    p.close()
    return p


@pytest.fixture
def client(fx, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    for n, p in fx["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    return c


def analysis(client, fx, head="breaking", **kw):
    r = client.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": fx["shas"].get(head, head), "base": "main", **kw})
    assert r.status_code == 200, r.text
    return r.json()["report"]


# ---- the page for a real analysis -------------------------------------------------------------------------------------------------
def test_page_shows_the_whole_report(client, fx):
    rep = analysis(client, fx)
    r = client.get(f"/analyses/{rep['analysis_id']}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    p = parse(r.text)
    text = " ".join(p.text)
    assert rep["analysis_id"] in text and "complete_with_gaps" in text and "api_change" in text
    assert "This is not a clean impact assessment" in text                                          # the caveat is on the page
    assert p.tags.count("tr") == 1 + len(rep["findings"]) + 1 + len(rep["coverage"]["repositories_analysed"])   # headers + rows
    for f in rep["findings"]:
        assert f["id"] in p.ids and f["reason"] in text                                              # every finding, in full, linkable
    for u in rep["unknowns"]:
        assert u["area"] in text and u["reason"] in text
    assert "affects confidence" in text and "informational" in text
    assert rep["evidence_snapshot_id"] in text and "/reproduce" in text
    assert "BREAKING" in text and "PaymentStatus.status removed" in text                            # the change itself
    assert "reconciliation-service" in text and "mobile-bff" in text                                # full audience: downstream is named
    assert "Advisory only" in text and rep["configuration_version"] in text


def test_page_links_are_relative_and_resolve(client, fx):
    rep = analysis(client, fx)
    p = parse(client.get(f"/analyses/{rep['analysis_id']}").text)
    assert p.hrefs and all(h.startswith("/api/v1/impact-analyses/") for h in p.hrefs)               # nothing leaves this server
    for h in p.hrefs:
        assert client.get(h).status_code == 200, h
    assert "/comment" in " ".join(p.hrefs) and "/evidence" in " ".join(p.hrefs)


def test_page_is_inert_self_contained_and_hardened(client, fx):
    rep = analysis(client, fx)
    r = client.get(f"/analyses/{rep['analysis_id']}")
    p = parse(r.text)
    assert "script" not in p.tags and "iframe" not in p.tags and "form" not in p.tags and "img" not in p.tags
    assert not [a for a in p.attrs if a[1].startswith("on") or a[1] in ("src", "srcset")]            # no handlers, no external loads
    assert "http://" not in r.text and "https://" not in r.text                                       # no URL at all: nothing to load or leak to
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert "no-cache" in r.headers["cache-control"] and "private" in r.headers["cache-control"]
    assert 'name="robots" content="noindex"' in r.text


def test_tables_stack_into_labelled_cards_on_narrow_screens(client, fx):
    rep = analysis(client, fx)
    html = client.get(f"/analyses/{rep['analysis_id']}").text
    p = parse(html)
    cells = [a for a in p.attrs if a[0] == "td"]
    labelled = [a for a in cells if a[1] == "data-label"]
    assert p.tags.count("td") == len(labelled) and len(labelled) > 0                  # no unlabelled cell: nothing is lost when stacked
    assert {a[2] for a in labelled} >= {"Severity", "Component", "Evidence", "Why", "Repository", "Revision"}
    assert sum(1 for a in p.attrs if a[:2] == ("table", "class") and a[2] == "stack") == 2
    assert "@media (max-width:700px)" in html and 'name="viewport"' in html


def test_audience_redaction_applies_to_the_page_like_the_api(client, fx):
    rep = analysis(client, fx)
    r = client.get(f"/analyses/{rep['analysis_id']}", headers={"X-TIA-Readable-Repos": "payments-service"})
    text = " ".join(parse(r.text).text)
    for other in ("reconciliation-service", "mobile-bff", "ledger-sync", "audit-reporting"):
        assert other not in r.text, other
    assert "finding(s) in repositories you cannot access are omitted" in text and "payments-service" in text


def test_unknown_analysis_is_a_plain_404_that_reveals_nothing(client):
    r = client.get("/analyses/tia-does-not-exist")
    assert r.status_code == 404 and "Analysis not found" in r.text and "tia-does-not-exist" not in r.text
    assert "content-security-policy" in r.headers
    assert client.get("/analyses/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_the_comment_link_resolves_to_the_page(client, fx):
    rep = analysis(client, fx)
    link = permalink("https://tia.example.com", rep["analysis_id"])
    assert link == f"https://tia.example.com/analyses/{rep['analysis_id']}"
    assert client.get(link.removeprefix("https://tia.example.com")).status_code == 200


def test_ui_report_links_to_the_page():
    html = open(__file__.replace("tests\\test_reportpage.py", "tia\\web\\index.html").replace("tests/test_reportpage.py", "tia/web/index.html"),
                encoding="utf-8").read()
    assert '$("permalink").href = `/analyses/${encodeURIComponent(rep.analysis_id)}`' in html


# ---- every report shape renders ---------------------------------------------------------------------------------------------------
def test_failed_partial_clean_and_tests_only_reports_render(client, fx):
    failed = analyze(AnalysisRequest(repos=fx["paths"], target="payments-service", base="main", head="0" * 40), None)
    partial = analysis(client, fx, max_results=2)
    clean = analysis(client, fx, "docs_only")
    for rep, needle in ((failed, "The analysis failed"), (partial, "INCOMPLETE"), (clean, "not proof")):
        text = " ".join(parse(render_page(rep, "/api/v1/impact-analyses/x")).text)
        assert needle in text, rep["status"]
    f = " ".join(parse(render_page(failed)).text)
    assert failed["failure_reason"] in f and "No findings" in f and "No coverage was recorded" in f      # a failed report has no summary or coverage
    assert "truncated" in " ".join(parse(render_page(partial)).text)


def test_nothing_in_a_report_can_inject_markup():
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>"\'&'
    rep = {"analysis_id": "tia-" + evil, "status": "complete_with_gaps", "configuration_version": evil, "generated_at": evil,
           "evidence_snapshot_id": evil, "extractor_versions": {evil: evil},
           "summary": {"findings": 1, "material_findings": 1, "by_severity": {"critical": 1, "high": 0, "medium": 0, "low": 0, "unknown": 0}},
           "change": {"repository": evil, "base_revision": evil, "proposed_revision": evil, "change_type": evil, "pull_request": evil,
                      "changed_components": [{"type": "api_schema", "name": evil, "change": evil, "breaking": True, "detail": evil,
                                              "file": evil, "line": evil}]},
           "findings": [{"id": evil, "severity": "critical", "severity_rule": evil, "component": evil, "repository": evil,
                         "evidence_status": evil, "resolution_strategy": evil, "reason": evil, "limitations": [evil],
                         "required_verification": [evil], "tests": [{"name": evil, "file": evil, "association": evil}],
                         "path": [{"type": evil, "node": evil, "repo": evil, "file": evil, "line": evil}],
                         "data": {"entity": evil, "table": evil, "database": evil, "accessor_count": evil,
                                  "accessors": [{"op": evil, "function": evil, "repo": evil, "file": evil, "line": evil}]}}],
           "unknowns": [{"area": evil, "reason": evil, "affects_confidence": True, "count": evil, "redacted_items": evil}],
           "coverage": {"repositories_analysed": [{"repo": evil, "service": evil, "commit": evil, "files_failed": evil,
                                                    "freshness": {"scan_status": evil}}],
                        "repositories_inaccessible": [{"repo": evil, "error": evil}], "traversal": {"truncated": False}}}
    html = render_page(rep, "/api/v1/impact-analyses/" + evil)
    p = parse(html)
    assert "script" not in p.tags and "img" not in p.tags and not [a for a in p.attrs if a[1].startswith("on")]
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;img src=x onerror=alert(2)&gt;&quot;&#x27;&amp;" in html     # present, but only as text
    assert all(h.startswith("/api/v1/impact-analyses/") for h in p.hrefs)                                       # the id is URL-encoded in links


def test_data_findings_show_entity_table_database_and_accessors():
    fx = build_data_fixture_once()
    rep = analyze(AnalysisRequest(repos=fx["paths"], target="ledger-service", base="main", head="drop_status"), None)
    text = " ".join(parse(render_page(rep)).text)
    assert "column accounts.status dropped" in text and "mapped by 3 entity(ies)" in text
    assert "ledger-db" in text and "ChargeService.charge" in text and "Data access" in text and "SEV-DATA-MAPPING-BROKEN" in text
    assert "database_identity" in text and "data_access_not_analysed" not in text                    # unknowns as recorded, no more


_DATA = {}


def build_data_fixture_once():
    if "fx" not in _DATA:
        _DATA["fx"] = build_data(tempfile.mkdtemp(prefix="page-data-"))
    return _DATA["fx"]
