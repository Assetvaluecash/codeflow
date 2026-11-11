"""Direct questions to the code graph, for testing agents and people (no change needed):

  consumers      who depends on this component (callers, consuming services, the entry points that reach it, the tests attached)
  dependencies   what this component depends on (code it calls, endpoints of other services, data, topics)
  tests          which tests exercise this component, directly or through code that uses it
  covers         what one test exercises: the code it is attached to and everything that code depends on

Rules: the component is NAMED by the caller (a node id, "METHOD /path", a function, test, service, table or topic name). A name that
matches several components is answered with the candidates, never with a guess. Evidence status is the weakest link of the path and is
never upgraded; confirmed paths are preferred over inferred ones. The graph passed in holds only repositories the caller may read.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Optional

from .models import normalize_path

QUESTIONS = ("consumers", "dependencies", "tests", "covers")
_RANK = {"stale": 0, "inferred": 1, "unknown": 1, "runtime_observed": 2, "corroborated": 3, "contract_confirmed": 4, "source_confirmed": 5}
_ENDPOINT = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)$", re.I)
DATA_TYPES = ("entity", "table", "column", "procedure")


def _label(nid: str, n: dict) -> str:
    t = n.get("type")
    if t == "endpoint":
        return f"{n.get('method')} {n.get('route') or n.get('path')}"
    if t == "service":
        return str(n.get("service"))
    if t == "callsite":
        return f"call {n.get('file')}:{n.get('line')}"
    return str(n.get("name") or nid)


def _row(g, nid: str, **extra) -> dict:
    n = g.nodes[nid]
    return {"node": nid, "type": n.get("type"), "name": _label(nid, n), "service": n.get("service"), "repo": n.get("repo"),
            "file": n.get("file"), "line": n.get("line"), **extra}


def find(g, component: str, repository: Optional[str] = None, only_tests: bool = False) -> list:
    """Node ids the name may mean, best kind of match first. Never a fuzzy match: equality on id, endpoint, service or simple name."""
    c = (component or "").strip()
    if not c:
        return []
    ok = lambda n: (repository is None or g.nodes[n].get("repo") == repository) and (not only_tests or g.nodes[n].get("type") == "test")   # noqa: E731
    if c in g.nodes:
        return [c] if ok(c) else []
    m = _ENDPOINT.match(c)
    if m:
        method, path = m.group(1).upper(), m.group(2)
        norm = normalize_path(path)
        return sorted(n for n, a in g.nodes.items() if a.get("type") == "endpoint" and a.get("method") == method and ok(n)
                      and (a.get("route") == path or a.get("path") == norm))
    svc = [n for n, a in g.nodes.items() if a.get("type") == "service" and a.get("service") == c and ok(n)]
    if svc:
        return sorted(svc)
    out = [n for n, a in g.nodes.items() if a.get("type") not in ("callsite", "service") and ok(n)
           and (a.get("name") == c or str(a.get("name") or "").split(".")[-1] == c)]
    code = [n for n in out if g.nodes[n].get("type") != "test"]
    return sorted(out if only_tests or not code else code)      # a function and its same-named test: the function is meant, unless a test is asked for


def _reach(g, starts: list, step, limit: int) -> tuple[dict, bool]:
    """Breadth-first from `starts`. Confirmed links first, then inferred ones for what is still unreached, so a node keeps its strongest
    path. Tests are reached but not walked through. Returns ({node: {depth, status, parent}}, truncated)."""
    out: dict = {}
    seen = set(starts)
    truncated = False
    for allow_inferred in (False, True):
        dq = deque([(s, 0, 9) for s in starts] + [(n, v["depth"], _RANK.get(v["status"], 1)) for n, v in out.items()])
        while dq:
            n, d, rank = dq.popleft()
            if g.nodes[n].get("type") == "test" and n not in starts:
                continue                                           # a test met on the way is an answer, not a path to walk on
            for nxt, e in step(n):
                if nxt in seen or (e.status == "inferred" and not allow_inferred) or nxt not in g.nodes:
                    continue
                if len(out) >= limit:
                    truncated = True
                    break
                seen.add(nxt)
                r = min(rank, _RANK.get(e.status, 1))
                status = e.status if r == _RANK.get(e.status, 1) else next(k for k, v in _RANK.items() if v == r and k != "unknown")
                out[nxt] = {"depth": d + 1, "status": status, "parent": n, "rel": e.rel}
                dq.append((nxt, d + 1, r))
    return out, truncated


def _starts(g, nid: str) -> list:
    """A service stands for its endpoints (what others consume and what its code is reached through)."""
    if g.nodes[nid].get("type") != "service":
        return [nid]
    return sorted(e.dst for e in g.edges if e.rel == "EXPOSES" and e.src == nid) or [nid]


def query(g, question: str, component: str, repository: Optional[str] = None, limit: int = 200) -> dict:
    if question not in QUESTIONS:
        raise ValueError(f"question must be one of {', '.join(QUESTIONS)}")
    limit = min(max(int(limit), 1), 1000)
    found = find(g, component, repository, only_tests=question == "covers")
    base = {"question": question, "asked": component, **({"repository": repository} if repository else {})}
    if not found:
        return {**base, "status": "not_found", "results": [],
                "hint": ("No test with that name." if question == "covers" else "No component with that name.") +
                        " Name a node id, an endpoint as 'GET /path', or a function, test, service, table or topic name exactly"
                        " (in a repository you can read)."}
    if len(found) > 1:
        return {**base, "status": "ambiguous", "results": [], "candidates": [_row(g, n) for n in found[:25]], "candidates_total": len(found),
                "hint": "Several components have that name: ask again with one candidate's node id, or add the repository."}
    nid = found[0]
    me = g.nodes[nid]
    starts = _starts(g, nid)
    step = g.depends_on if question in ("dependencies", "covers") else g.impacted_by
    reached, truncated = _reach(g, starts, step, limit)
    rows = [_row(g, n, depth=v["depth"], evidence_status=v["status"], relationship=v["rel"]) for n, v in reached.items() if n not in starts]
    rows.sort(key=lambda r: (r["depth"], r["type"] or "", r["node"]))
    code = [r for r in rows if r["type"] != "test"]
    tests = [r for r in rows if r["type"] == "test"]
    for t in tests:                                                # which code the test is attached to
        p = reached[t["node"]]["parent"]
        t["covers"] = "directly" if p in starts else f"through {_label(p, g.nodes[p])}"
        t.pop("relationship", None)
    other = lambda r: r["service"] and r["service"] != me.get("service")      # noqa: E731
    limits = []
    unresolved = len(getattr(g, "unresolved", []) or [])
    if question == "consumers":
        res = {"consumers": code, "entry_points": [r for r in code if r["type"] in ("endpoint", "listener")],
               "services": sorted({r["service"] for r in code if other(r)}), "tests": tests}
        limits.append("Consumers outside the analysed repositories (or in repositories you cannot read) are not visible.")
        if unresolved:
            limits.append(f"{unresolved} outbound call(s) in the analysed code could not be resolved to a provider: a consumer that reaches this "
                          "component only through one of them is missing here.")
    elif question == "dependencies":
        res = {"dependencies": code, "endpoints_of_other_services": [r for r in code if r["type"] == "endpoint" and other(r)],
               "services": sorted({r["service"] for r in code if other(r)}), "data": [r for r in code if r["type"] in DATA_TYPES],
               "topics": [r for r in code if r["type"] == "topic"]}
        if unresolved:
            limits.append(f"{unresolved} outbound call(s) in the analysed code could not be resolved: what they call is not listed.")
    elif question == "tests":
        res = {"tests": tests, "direct": sum(1 for t in tests if t["covers"] == "directly"),
               "through_callers": sum(1 for t in tests if t["covers"] != "directly")}
        limits.append("Static association: a test is listed because it calls or names this code (or code that uses it), not because it was "
                      "seen to execute it or to pass.")
    else:
        res = {"attached_to": [r for r in code if r["depth"] == 1], "also_exercises": [r for r in code if r["depth"] > 1],
               "entry_points": [r for r in code if r["type"] == "endpoint"], "services": sorted({r["service"] for r in code if r["service"]})}
        limits.append("Static association: what the test calls or names, and what that code depends on; not an execution trace.")
    if any(r["evidence_status"] == "inferred" for r in rows):
        limits.append("Rows marked inferred rest on a guessed link (a name match): treat them as leads, not facts.")
    if truncated:
        limits.append(f"The answer was cut at {limit} components: ask with a larger limit or a narrower component.")
    return {**base, "status": "ok", "component": _row(g, nid), "results": res,
            "summary": {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}, "truncated": truncated, "limits": limits}
