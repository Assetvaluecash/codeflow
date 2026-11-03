"""Technical coverage (UC-2, docs/CASE-STUDY.md): "are my test cases enough?" on the technical side.

For each entry point (API endpoint) and page, the code implies technical aspects a test suite should cover:
  call             the entry point is exercised at all
  missing_input    a mandatory input left out is rejected
  invalid_value    an input that breaks its rule (length, range, pattern, format) is rejected
  allowed_values   each value an input allows is accepted
  error_status     each error status the handler answers with (404 unknown account, 409 duplicate, ...)
  rule             each data rule on the entry point's path (an item filter: "disputed payments are left out")
and, for a page: mandatory_element (left empty is refused), invalid_element, element_options (each dropdown value).

Both kinds of tests are matched against them: automated tests in the repositories (what they call, assert and send) and written test
cases (testcases/*.csv|yaml|json: their entry point, steps and expected result). Matching is STATIC and textual - a test that mentions a
field and asserts a 422 is taken to test that field - so "covered" means "a test addresses it", never "it passes". Functional coverage
against business requirements is not here (that is the functional coverage agent's job).
"""
from __future__ import annotations

import re
from typing import Optional

from .automation import api_inventory, option_values, ui_inventory, violates
from .graph import _path_matches
from .models import normalize_path
from .testscope import dependencies, labels_of, norm_field

REJECT = {400, 422}
REASONS = {400: ("bad request", "invalid request"), 401: ("unauthorized", "unauthorised", "not logged in", "not signed in"),
           403: ("forbidden", "not allowed", "no permission", "access denied"), 404: ("not found", "unknown", "does not exist", "doesn't exist"),
           409: ("conflict", "duplicate", "already exists"), 422: ("validation error", "unprocessable", "rejected", "invalid"),
           429: ("too many requests", "rate limit"), 500: ("server error",), 503: ("unavailable",)}
MISSING_WORDS = re.compile(r"\b(missing|without|empty|blank|omit\w*|leave\w* out|left out|not (?:filled|entered|provided|given)|no value)\b", re.I)
INVALID_WORDS = re.compile(r"\b(invalid|exceed\w*|too (?:long|short|big|small|large)|more than|less than|longer|shorter|wrong|"
                           r"letters|negative|format|over|under|above|below|beyond|not allowed|non-numeric|special characters?)\b", re.I)


def _words(name: str) -> list:
    """Ways a field name appears in prose: account_id, accountId -> 'account id'."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ").replace("-", " ").lower().strip()
    return sorted({name.lower(), spaced})


def _mentions(text: str, name: str) -> bool:
    t = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in _words(name))


# ---- aspects -------------------------------------------------------------------------------------------------------------------------
def api_aspects(item: dict, g, filters: dict) -> list:
    ep, out = item["node"], []
    out.append({"id": f"{ep}#call", "kind": "call", "title": f"{item['entry_point']} is exercised"})
    for i in item["inputs"]:
        where = "" if i["in"] == "body" else f" ({i['in']} parameter)"
        if i["required"] and i["in"] != "path":
            out.append({"id": f"{ep}#missing:{i['name']}", "kind": "missing_input", "field": i["name"], "in": i["in"],
                        "title": f"a request without the mandatory '{i['name']}'{where} is rejected"})
        if i["rules"] and not i["allowed"]:
            out.append({"id": f"{ep}#invalid:{i['name']}", "kind": "invalid_value", "field": i["name"], "in": i["in"], "rules": i["rules"],
                        "title": f"an invalid '{i['name']}'{where} is rejected ({i['rules']})"})
        if i["allowed"]:
            out.append({"id": f"{ep}#values:{i['name']}", "kind": "allowed_values", "field": i["name"], "in": i["in"], "values": [str(v) for v in i["allowed"]],
                        "title": f"each allowed value of '{i['name']}' is accepted ({', '.join(map(str, i['allowed']))})"})
            out.append({"id": f"{ep}#invalid:{i['name']}", "kind": "invalid_value", "field": i["name"], "in": i["in"], "rules": i["rules"],
                        "title": f"a value of '{i['name']}' outside {', '.join(map(str, i['allowed']))} is rejected"})
    for s in item["error_statuses"]:
        out.append({"id": f"{ep}#status:{s['status']}", "kind": "error_status", "status": s["status"],
                    "title": f"answers {s['status']}" + (f" ({s['detail']})" if s.get("detail") else "") + f" - raised in {s['function']}"})
    seen = set()
    for fn in sorted(n for n in dependencies(g, ep) if g.nodes[n]["type"] == "function") if ep in g.nodes else []:
        for flt in filters.get(fn, []):
            key = tuple(sorted(norm_field(x) for x in flt["fields"]))
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": f"{ep}#rule:{','.join(key)}", "kind": "rule", "fields": flt["fields"], "expr": flt["expr"],
                        "function": g.nodes[fn]["name"], "repo": g.nodes[fn]["repo"],
                        "title": f"the rule `{flt['expr']}` in {g.nodes[fn]['name']} ({g.nodes[fn]['service']}): items it leaves out are not shown"})
    return out


def page_aspects(page: dict) -> list:
    pid, out = f"{page['repo']}:page:{page['page']}", []
    out.append({"id": f"{pid}#open", "kind": "call", "title": f"page {page['page']} is opened"})
    for el in page["elements"]:
        if el["tag"] not in ("input", "select", "textarea"):
            continue
        name = el.get("label") or el.get("name") or el["locator"]["css"] or el["tag"]
        if el.get("mandatory"):
            out.append({"id": f"{pid}#missing:{name}", "kind": "missing_input", "field": name, "also": el.get("name"),
                        "title": f"leaving '{name}' empty is refused"})
        if el.get("rules") and el["tag"] != "select":
            out.append({"id": f"{pid}#invalid:{name}", "kind": "invalid_value", "field": name, "also": el.get("name"), "rules": el["rules"],
                        "title": f"an invalid '{name}' is refused ({el['rules']})"})
        opts = [str(v) for v in option_values(el)]
        if opts:
            out.append({"id": f"{pid}#values:{name}", "kind": "allowed_values", "field": name, "also": el.get("name"), "values": opts,
                        "title": f"each option of '{name}' works ({', '.join(opts)})"})
    return out


# ---- matching ------------------------------------------------------------------------------------------------------------------------
def _auto_covers(t, a: dict, ep_key: tuple, linked: bool, ep_reached: bool, rule: Optional[dict] = None) -> Optional[str]:
    """Why automated test `t` addresses aspect `a` of the entry point (method, norm_path), or None. When the test's request bodies are
    visible (Python dict literals), the values it sends are checked against the field's rules; otherwise the field name and an expected
    rejection are taken as evidence."""
    calls = [c for c in t.http_calls if c[0] == ep_key[0] and _path_matches(ep_key[1], c[1])]
    lits = {str(x).lower() for x in t.literals}
    sent = [b for b in t.bodies if b[0] == ep_key[0] and _path_matches(ep_key[1], b[1])]
    keys = {k for b in sent for k in b[2]}
    k = a["kind"]
    if sent and rule is not None and k in ("invalid_value", "allowed_values") and a.get("in") == "body":
        vals = [b[3][a["field"]] for b in sent if len(b) > 3 and a["field"] in b[3]]
        if k == "allowed_values":
            hit = sorted({str(v) for v in vals if str(v) in a["values"]})
            return (f"sends {', '.join(hit)}", hit) if hit else None
        bad = [(v, violates(rule, v)) for v in vals]
        bad = [(v, why) for v, why in bad if why]
        return f"sends '{a['field']}' = {bad[0][0]!r} ({bad[0][1]}) and expects a rejection" if bad and REJECT & set(t.statuses) else None
    if k == "call":
        return "calls it" if calls else ("linked to its code" if linked else None)
    if not calls and not (k == "rule" and ep_reached):
        return None
    if k == "missing_input":
        if not REJECT & set(t.statuses):
            return None
        if a.get("in") == "body":
            sent = [b for b in t.bodies if b[0] == ep_key[0] and _path_matches(ep_key[1], b[1])]
            return f"sends a body without '{a['field']}' and expects a rejection" if sent and a["field"] not in keys else None
        return f"leaves '{a['field']}' out and expects a rejection" if a["field"].lower() not in lits else None
    if k == "invalid_value":
        if a.get("in") == "body" and sent:
            return None                                    # the body is visible: only a value that breaks the rule counts (above)
        return (f"mentions '{a['field']}' and expects a rejection (the value it sends is not visible)"
                if REJECT & set(t.statuses) and a["field"].lower() in lits else None)
    if k == "allowed_values":
        hit = [v for v in a["values"] if v.lower() in lits]
        return (f"uses {', '.join(hit)}", hit) if hit else None
    if k == "error_status":
        return f"expects {a['status']}" if a["status"] in t.statuses else None
    if k == "rule":
        hit = [f for f in a["fields"] if f.lower() in lits or norm_field(f) in {norm_field(x) for x in lits}]
        return f"its data sets '{hit[0]}'" if hit else None
    return None


def _written_covers(tc: dict, a: dict) -> Optional[object]:
    text = " ".join(tc.get(k, "") for k in ("title", "steps", "expected"))
    k = a["kind"]
    if k == "call":
        return "names it"
    if k == "missing_input":
        return "leaves it out" if (_mentions(text, a["field"]) or (a.get("also") and _mentions(text, a["also"]))) and MISSING_WORDS.search(text) else None
    if k == "invalid_value":
        return "tries an invalid value" if (_mentions(text, a["field"]) or (a.get("also") and _mentions(text, a["also"]))) and INVALID_WORDS.search(text) else None
    if k == "allowed_values":
        hit = [v for v in a["values"] if re.search(r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", text, re.I)]
        return (f"uses {', '.join(hit)}", hit) if hit else None
    if k == "error_status":
        low = text.lower()
        return f"expects {a['status']}" if re.search(rf"\b{a['status']}\b", text) or any(w in low for w in REASONS.get(a["status"], ())) else None
    if k == "rule":
        hit = [f for f in a["fields"] if _mentions(text, f)]
        return f"checks '{hit[0]}'" if hit else None
    return None


def _entry_matches(tc: dict, item: dict) -> bool:
    """Does a written test case name this entry point (column or text: method+route, route, plain-language label)?"""
    ep = (tc.get("entry_point") or "").strip()
    text = " ".join(tc.get(k, "") for k in ("title", "steps", "expected"))
    label = (item.get("label") or "").lower()
    if ep:
        parts = ep.split(None, 1)
        if len(parts) == 2 and parts[0].upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return parts[0].upper() == item["method"] and _path_matches(normalize_path(item["path"]), normalize_path(parts[1]))
        if ep.startswith("/"):
            return _path_matches(normalize_path(item["path"]), normalize_path(ep))
        return bool(label) and ep.lower() == label
    for m in re.finditer(r"(?:\b(GET|POST|PUT|PATCH|DELETE)\s+)?(/[A-Za-z0-9_{}:.\-/]+)", text):
        if (m.group(1) is None or m.group(1).upper() == item["method"]) and _path_matches(normalize_path(item["path"]), normalize_path(m.group(2))):
            return True
    return bool(label) and label in text.lower()


def _page_matches(tc: dict, page: dict) -> bool:
    ep = (tc.get("entry_point") or "").strip().lower()
    text = " ".join(tc.get(k, "") for k in ("title", "steps", "expected")).lower()
    names = {n.lower() for n in (page["page"], page.get("route") or "", page.get("component") or "") if n}
    if ep:
        return ep in names or (ep.startswith("/") and page.get("route") and _path_matches(normalize_path(page["route"]), normalize_path(ep)))
    return any(n in text for n in names if len(n) > 3)


def _apply(aspects: list, hits: dict):
    for a in aspects:
        h = hits.get(a["id"], [])
        a["covered_by"] = h
        if a["kind"] == "allowed_values":
            got = sorted({v for x in h for v in x.get("values", [])})
            a["values_covered"] = got
            a["values_missing"] = [v for v in a["values"] if v not in got]
            a["status"] = "covered" if not a["values_missing"] else "partial" if got else "missing"
        else:
            a["status"] = "covered" if h else "missing"


def _record(hits: dict, a: dict, ref: dict, why):
    vals = None
    if isinstance(why, tuple):
        why, vals = why
    hits.setdefault(a["id"], []).append({**ref, "why": why, **({"values": vals} if vals else {})})


def coverage_report(extracts: dict, g, only_written: Optional[list] = None) -> dict:
    """The technical aspects of every entry point and page and the tests that address them. `only_written`: assess these written test
    cases instead of the repositories' own (the "is this test case enough?" query)."""
    labels = labels_of(extracts)
    filters = {f.id: f.filters for ex in extracts.values() for f in ex.functions if f.filters}
    items = api_inventory(extracts, labels)
    pages = ui_inventory(extracts)
    autos = [t for ex in extracts.values() for t in ex.tests] if only_written is None else []
    written = [dict(tc, repo=r) for r, ex in sorted(extracts.items()) for tc in ex.manual_tests] if only_written is None else only_written
    tested = {}                                                    # endpoint -> tests linked to it or its code through TESTS edges
    for e in g.edges:
        if e.rel == "TESTS":
            tested.setdefault(e.dst, set()).add(e.src)
    # a written test case that names a page (route, page or component name) tests the page; only one naming an HTTP method as well is
    # also matched to an API whose path looks the same (page routes and API paths often do)
    on_page = {n for n, tc in enumerate(written) if any(_page_matches(tc, p) for p in pages)}
    entries, matched_written = [], set()
    for item in items:
        asp = api_aspects(item, g, filters)
        rules = {f"{item['node']}#{k}:{i['name']}": i for i in item["inputs"] for k in ("invalid", "values")}
        code = dependencies(g, item["node"]) if item["node"] in g.nodes else {item["node"]}
        # tests linked to the entry point's code count for it only in its own repository: a provider's tests reach the code a consumer
        # endpoint calls, but they do not exercise the consumer
        linked_tests = {t for n in code if g.nodes.get(n, {}).get("repo") == item["repo"] for t in tested.get(n, ())}
        hits: dict = {}
        key = (item["method"], normalize_path(item["path"]))
        for t in autos:
            linked = t.id in linked_tests
            if not (linked or any(c[0] == key[0] and _path_matches(key[1], c[1]) for c in t.http_calls)):
                continue
            ref = {"type": "automated", "name": t.name, "file": t.file, "repo": t.repo}
            for a in asp:
                why = _auto_covers(t, a, key, linked, linked, rules.get(a["id"]))
                if why:
                    _record(hits, a, ref, why)
        for n, tc in enumerate(written):
            if n in on_page and not re.match(r"\s*(GET|POST|PUT|PATCH|DELETE)\s", tc.get("entry_point") or ""):
                continue
            if not _entry_matches(tc, item):
                continue
            matched_written.add(n)
            ref = {"type": "written", "id": tc["id"], "title": tc.get("title", ""), "file": tc.get("file"), "repo": tc.get("repo")}
            for a in asp:
                why = _written_covers(tc, a)
                if why:
                    _record(hits, a, ref, why)
        _apply(asp, hits)
        entries.append(_entry_row(item, "api", asp))
    for page in pages:
        asp = page_aspects(page)
        hits = {}
        for n, tc in enumerate(written):
            if not _page_matches(tc, page):
                continue
            matched_written.add(n)
            ref = {"type": "written", "id": tc["id"], "title": tc.get("title", ""), "file": tc.get("file"), "repo": tc.get("repo")}
            for a in asp:
                why = _written_covers(tc, a)
                if why:
                    _record(hits, a, ref, why)
        _apply(asp, hits)
        entries.append(_entry_row({"entry_point": f"page {page['page']}", "node": f"{page['repo']}:page:{page['page']}", "service": page["repo"],
                                   "repo": page["repo"], "file": page["files"][0], "line": None}, "page", asp))
    unmatched = [{"id": tc["id"], "title": tc.get("title", ""), "file": tc.get("file"), "repo": tc.get("repo")}
                 for n, tc in enumerate(written) if n not in matched_written]
    return {"entry_points": entries, "unmatched_written_tests": unmatched, "summary": summary_of(entries, len(autos), len(written), len(unmatched)),
            "basis": "static, textual matching: 'covered' means a test addresses the aspect, not that it passes"}


def _entry_row(item: dict, kind: str, asp: list) -> dict:
    n = len(asp)
    cov = sum(1 for a in asp if a["status"] == "covered")
    part = sum(1 for a in asp if a["status"] == "partial")
    return {"entry_point": item["entry_point"], "kind": kind, "node": item["node"], "service": item["service"], "repo": item["repo"],
            "file": item.get("file"), **({"label": item["label"]} if item.get("label") else {}), "aspects": asp,
            "covered": cov, "partial": part, "missing": n - cov - part, "total": n,
            "percent": round(100 * (cov + 0.5 * part) / n, 1) if n else 100.0}


def summary_of(entries: list, automated: int, written: int, unmatched: int) -> dict:
    tot = sum(e["total"] for e in entries)
    cov = sum(e["covered"] for e in entries)
    part = sum(e["partial"] for e in entries)
    by_kind = {}
    for e in entries:
        for a in e["aspects"]:
            k = by_kind.setdefault(a["kind"], {"total": 0, "covered": 0, "partial": 0, "missing": 0})
            k["total"] += 1
            k[a["status"]] += 1
    return {"entry_points": len(entries), "aspects": tot, "covered": cov, "partial": part, "missing": tot - cov - part,
            "percent": round(100 * (cov + 0.5 * part) / tot, 1) if tot else 100.0, "by_kind": by_kind,
            "automated_tests": automated, "written_tests": written, "unmatched_written_tests": unmatched}


def assess_test_case(extracts: dict, g, tc: dict) -> dict:
    """One test case as a query: which entry points it addresses, which of their aspects it covers, and which it misses."""
    tc = {"id": tc.get("id") or "query", "title": tc.get("title", ""), "steps": tc.get("steps", ""), "expected": tc.get("expected", ""),
          "entry_point": tc.get("entry_point", ""), "file": None, "repo": None}
    rep = coverage_report(extracts, g, only_written=[tc])
    hit = [e for e in rep["entry_points"] if any(a["covered_by"] for a in e["aspects"])]
    return {"test_case": {k: tc[k] for k in ("id", "title", "entry_point")},
            "matched": [{"entry_point": e["entry_point"], "kind": e["kind"], "service": e["service"], "repo": e["repo"],
                         **({"label": e["label"]} if e.get("label") else {}),
                         "covers": [{"id": a["id"], "title": a["title"], "why": a["covered_by"][0]["why"],
                                     **({"values_missing": a["values_missing"]} if a.get("values_missing") else {})} for a in e["aspects"] if a["covered_by"]],
                         "misses": [{"id": a["id"], "kind": a["kind"], "title": a["title"]} for a in e["aspects"] if not a["covered_by"]],
                         "percent_of_area": e["percent"]} for e in hit],
            "note": ("This test case does not name any analysed entry point or page (give its entry point, e.g. 'GET /accounts/{id}/payments' "
                     "or a page route)." if not hit else "Matching is static and textual: it says what the test case addresses, not that it passes.")}


def redact_coverage(rep: dict, ok: set) -> dict:
    """What a reader may see: entry points and tests of readable repositories only; coverage is recomputed from the tests they can see,
    so a test in an unreadable repository neither shows nor counts."""
    withheld = sum(1 for e in rep["entry_points"] if e["repo"] not in ok)
    kept = []
    for e in rep["entry_points"]:
        if e["repo"] not in ok:
            continue
        hits = {}
        for a in e["aspects"]:
            hits[a["id"]] = [x for x in a["covered_by"] if x.get("repo") in ok or x.get("repo") is None]
        _apply(e["aspects"], hits)
        kept.append({**_entry_row(e, e["kind"], e["aspects"]), "kind": e["kind"]})
    rep["entry_points"] = kept
    rep["unmatched_written_tests"] = [x for x in rep["unmatched_written_tests"] if x.get("repo") in ok]
    s = rep["summary"]
    rep["summary"] = summary_of(kept, s["automated_tests"], s["written_tests"], len(rep["unmatched_written_tests"]))
    rep["withheld_entry_points"] = withheld
    return rep
