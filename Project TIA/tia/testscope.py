"""Test scope (UC-1 in docs/CASE-STUDY.md): what a change means for a tester, in things a tester can test without reading code.

* change effect of a changed function: breaking / behaviour change / compatible change / no functional change, with the reason;
* entry points to test: every API endpoint a change reaches (in the changed service and in each analysed consumer), why it is reached,
  the strongest change effect on its path and the tests linked to it; reached code with no analysed entry point is listed too;
* consistency checks: two or more consumers in other services that take data from the same changed endpoint;
* rule gaps: a rule that filters a shared endpoint's data (a condition on its response fields) applied by some of the endpoint's
  consumers and not by others - the defect class "the PDF excludes people on notice, the screen does not";
* common components: endpoints and functions that many entry points depend on (a change to them needs the widest testing).

Everything here reads TIA's own graph and extracts. Rule gaps and common components use confirmed relationships only (never inferred
ones), and a rule only counts when the fields it tests are fields of the shared endpoint's declared response schema.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Optional

from .models import INFERRED, normalize_path

EFFECTS = ("breaking", "behaviour_change", "compatible_change", "no_functional_change", "unknown")
EFFECT_RANK = {"breaking": 4, "behaviour_change": 3, "unknown": 2, "compatible_change": 1, "no_functional_change": 0}
WALK = ("function", "endpoint", "callsite", "contract_ref")      # node types a business path runs through (not tests, schemas or data)


def strongest(effects) -> str:
    effects = [e for e in effects if e]
    return max(effects, key=lambda e: EFFECT_RANK[e]) if effects else "unknown"


def norm_field(name: str) -> str:
    """Field names compared across languages and conventions: on_notice, onNotice and OnNotice are the same field."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


# ---- change effect of a function -------------------------------------------------------------------------------------------------
def signature_diff(base: Optional[dict], head: Optional[dict]) -> tuple[list, list]:
    """-> (incompatible changes, compatible changes) between two signatures. Java parameters carry a type (names do not matter to
    callers); Python parameters carry whether they have a default (names matter: callers may pass them by keyword)."""
    if not base or not head:
        return [], []
    inc, comp = [], []
    bp, hp = base.get("params") or [], head.get("params") or []
    if any("type" in p for p in bp + hp):
        bt, ht = [p.get("type", "") for p in bp], [p.get("type", "") for p in hp]
        if bt != ht:
            inc.append(f"parameter types ({', '.join(bt)}) -> ({', '.join(ht)})")
    else:
        bn, hn = {p["name"]: p for p in bp}, {p["name"]: p for p in hp}
        for n, p in bn.items():
            if n not in hn:
                inc.append(f"parameter '{n}' removed")
            elif p.get("default") and not hn[n].get("default"):
                inc.append(f"parameter '{n}' is no longer optional")
        for n, p in hn.items():
            if n not in bn:
                (comp if p.get("default") else inc).append(f"parameter '{n}' added" + ("" if p.get("default") else " (required)"))
        bpos = [p["name"] for p in bp if p.get("kind") == "positional" and p["name"] in hn]
        hpos = [p["name"] for p in hp if p.get("kind") == "positional" and p["name"] in bn]
        if bpos != hpos:
            inc.append("positional parameter order changed")
    if base.get("returns") != head.get("returns"):
        inc.append(f"return type {base.get('returns') or 'unspecified'} -> {head.get('returns') or 'unspecified'}")
    return inc, comp


def classify_function(fid: str, base_fn: dict, head_fn: dict) -> tuple[str, str]:
    """Effect of the change to one function between the base and the proposed revision of a repository -> (effect, reason).

    base_fn / head_fn: function id -> Function. Callers are found by name in the same repository (TIA links calls by name), so a caller
    of another function with the same name counts too; the reason says how many were found."""
    b, h = base_fn[fid], head_fn.get(fid)
    name = b.qualname.split(".")[-1]
    callers = sorted(f.id for f in head_fn.values() if f.id != fid and name in (f.calls or []))
    if h is None:
        if callers and not any(f.qualname.split(".")[-1] == name for f in head_fn.values()):
            return "breaking", (f"removed, but {len(callers)} function(s) in the proposed revision still call '{name}' "
                                f"({', '.join(c.split('::')[-1] for c in callers[:3])}{', ...' if len(callers) > 3 else ''})")
        return "compatible_change", f"removed; nothing in the repository calls '{name}' any more"
    if b.body_hash and h.body_hash and b.body_hash == h.body_hash:
        return "no_functional_change", "documentation only (docstring); the code is unchanged"
    inc, comp = signature_diff(b.signature, h.signature)
    if inc:
        not_updated = [c for c in callers if c in base_fn and base_fn[c].hash == head_fn[c].hash]
        if not_updated:
            return "breaking", (f"signature changed ({'; '.join(inc)}) and {len(not_updated)} caller(s) were not changed "
                                f"({', '.join(c.split('::')[-1] for c in not_updated[:3])}{', ...' if len(not_updated) > 3 else ''})")
        return "behaviour_change", f"signature changed ({'; '.join(inc)}); its callers in the repository were updated in the same change"
    if comp:
        return "behaviour_change", f"logic changed; compatible signature change ({'; '.join(comp)})"
    return "behaviour_change", "logic changed (same signature)"


# ---- graph walks (confirmed relationships only) ----------------------------------------------------------------------------------
def _walk(g, start: str, step) -> set:
    seen, dq = {start}, deque([start])
    while dq:
        n = dq.popleft()
        for nxt, e in step(n):
            if nxt in seen or e.status == INFERRED or g.nodes.get(nxt, {}).get("type") not in WALK:
                continue
            seen.add(nxt)
            dq.append(nxt)
    return seen


def dependencies(g, nid: str) -> set:
    """Everything `nid` depends on (itself included): its handler, what that calls, the endpoints those calls reach, and on."""
    return _walk(g, nid, g.depends_on)


def dependents(g, nid: str) -> set:
    """Everything that depends on `nid` (itself included)."""
    return _walk(g, nid, g.impacted_by)


def entry_name(g, nid: str) -> str:
    n = g.nodes[nid]
    if n["type"] == "endpoint":
        return f"{n['method']} {n.get('route') or n['path']}"                 # the route as written; graphs built before it was kept: normalised
    return str(n.get("name") or nid.split("::")[-1])


def labels_of(extracts: dict) -> dict:
    """Plain-language names of entry points declared by their owners (tia-services.yaml `entry_points:`), keyed by endpoint node id."""
    out = {}
    for ex in extracts.values():
        declared = (ex.service_map or {}).get("entry_points") or {}
        if not isinstance(declared, dict):
            continue
        for key, label in declared.items():
            parts = str(key).split(None, 1)
            if len(parts) == 2 and isinstance(label, str) and label.strip():
                out[f"{ex.service}:{parts[0].upper()} {normalize_path(parts[1], tuple(ex.strip_prefixes))}"] = label.strip()[:120]
    return out


def _schema_fields(extracts: dict) -> dict:
    """endpoint node id -> normalised field names of its declared response schema."""
    out = {}
    for ex in extracts.values():
        fields = {s.name: s.fields for s in ex.schemas}
        for e in ex.endpoints:
            if e.response_schema in fields:
                out[e.id] = {norm_field(k) for k in fields[e.response_schema]}
    return out


def _filters(extracts: dict) -> dict:
    return {f.id: f.filters for ex in extracts.values() for f in ex.functions if f.filters}


def _rules(fns, filters: dict, fields: set) -> dict:
    """Rules (keyed by the shared fields they test) that the functions `fns` apply -> {key: (function id, filter)}."""
    out = {}
    for fn in sorted(fns):
        for flt in filters.get(fn, []):
            key = tuple(sorted({norm_field(x) for x in flt["fields"]} & fields))
            if key and key not in out:
                out[key] = (fn, flt)
    return out


# ---- rule gaps -------------------------------------------------------------------------------------------------------------------
def rule_gaps(g, extracts: dict, only: Optional[set] = None, labels: Optional[dict] = None) -> list:
    """Rules on a shared endpoint's data that some of its consumers apply and others do not.

    For each endpoint S with a declared response schema and two or more other entry points (endpoints) depending on it, the rules of
    each entry point's lane are the item filters in the functions between that entry point and S that test fields of S's response.
    A rule S applies itself (in its own handler or below) is applied for everyone and is never a gap."""
    labels = labels or {}
    filters, sfields = _filters(extracts), _schema_fields(extracts)
    out = []
    for s in sorted(n for n, a in g.nodes.items() if a["type"] == "endpoint" and n in sfields and (only is None or n in only)):
        above = dependents(g, s)
        entries = sorted(n for n in above if n != s and g.nodes[n]["type"] == "endpoint")
        if len(entries) < 2:
            continue
        own = _rules(dependencies(g, s), filters, sfields[s])
        lanes = {e: _rules(dependencies(g, e) & above, filters, sfields[s]) for e in entries}
        for key in sorted({k for r in lanes.values() for k in r} - set(own)):
            have = [e for e in entries if key in lanes[e]]
            lack = [e for e in entries if key not in lanes[e]]
            if not have or not lack:
                continue
            fn, flt = lanes[have[0]][key]
            out.append({
                "shared_endpoint": s, "shared_entry_point": entry_name(g, s), "service": g.nodes[s]["service"], "repo": g.nodes[s]["repo"],
                "rule": {"fields": list(key), "expr": flt["expr"]},
                "applied_by": [_lane_ref(g, e, labels, *lanes[e][key]) for e in have],
                "missing_in": [_entry_ref(g, e, labels) for e in lack],
                "evidence_status": "source_confirmed",
                "reason": (f"{len(have)} consumer(s) of {entry_name(g, s)} ({g.nodes[s]['service']}) keep only items where `{flt['expr']}` "
                           f"(fields: {', '.join(key)}); {len(lack)} do not: "
                           + ", ".join(f"{entry_name(g, e)} ({g.nodes[e]['service']})" for e in lack[:5]) + ". If the rule applies to "
                           "every consumer, these show items the others exclude; if it does not, confirm the difference is intended."),
            })
    return out


def _entry_ref(g, e: str, labels: dict) -> dict:
    n = g.nodes[e]
    return {"entry_point": entry_name(g, e), "node": e, "service": n["service"], "repo": n["repo"], "file": n.get("file"),
            "line": n.get("line"), **({"label": labels[e]} if e in labels else {})}


def _lane_ref(g, e: str, labels: dict, fn: str, flt: dict) -> dict:
    return {**_entry_ref(g, e, labels), "function": g.nodes[fn]["name"], "function_file": g.nodes[fn].get("file"), "function_line": flt["line"],
            "expr": flt["expr"]}


def resolved_in_head(gap: dict, head_ex, handler_of: dict) -> bool:
    """True when the proposed revision of the shared endpoint's own repository applies the gap's rule itself (in the endpoint's handler
    or a function it calls, found by name): the change moves the rule to the shared component."""
    start = handler_of.get(gap["shared_endpoint"])
    if start is None:
        return False
    by_id = {f.id: f for f in head_ex.functions}
    by_name: dict = {}
    for f in head_ex.functions:
        by_name.setdefault(f.qualname.split(".")[-1], []).append(f)
    if start not in by_id:
        return False
    seen, dq, key = {start}, deque([start]), set(gap["rule"]["fields"])
    while dq:
        f = by_id[dq.popleft()]
        if any(key <= {norm_field(x) for x in flt["fields"]} for flt in f.filters):
            return True
        for c in f.calls:
            cands = by_name.get(c, [])
            if len(cands) == 1 and cands[0].id not in seen:
                seen.add(cands[0].id)
                dq.append(cands[0].id)
    return False


# ---- common components -----------------------------------------------------------------------------------------------------------
def shared_components(g, extracts: dict, min_entry_points: int = 2, limit: int = 100) -> list:
    """Endpoints and functions ranked by how many entry points (endpoints) depend on them through confirmed relationships."""
    labels = labels_of(extracts)
    users: dict = {}
    for e in sorted(n for n, a in g.nodes.items() if a["type"] == "endpoint"):
        for n in dependencies(g, e):
            if n != e and g.nodes[n]["type"] in ("function", "endpoint"):
                users.setdefault(n, set()).add(e)
    rows = []
    for n, es in users.items():
        if len(es) < min_entry_points:
            continue
        a = g.nodes[n]
        rows.append({"node": n, "type": a["type"], "name": entry_name(g, n), "service": a["service"], "repo": a["repo"], "file": a.get("file"),
                     "line": a.get("line"), "entry_points": len(es), "services": len({g.nodes[e]["service"] for e in es}),
                     "used_by": [_entry_ref(g, e, labels) for e in sorted(es)][:20]})
    rows.sort(key=lambda r: (-r["entry_points"], -r["services"], r["node"]))
    return rows[:limit]


# ---- the test scope of one analysis ----------------------------------------------------------------------------------------------
def _lane_tests(g, n: str, path: list, tests_for) -> list:
    """Tests of the entry point and of the code between it and the change, in the entry point's own repository (a consumer's tests also
    reach a provider's endpoint, but they test the consumer, so they are listed under the consumer)."""
    repo, seen, out = g.nodes[n]["repo"], set(), []
    for x in [n] + [p for p in reversed(path) if g.nodes[p]["repo"] == repo]:
        for t in tests_for(x):
            k = (t["repo"], t["file"], t["name"])
            if t["repo"] == repo and k not in seen:
                seen.add(k)
                out.append({"name": t["name"], "file": t["file"], "repo": t["repo"], "association": t["association"],
                            "evidence_status": t["evidence_status"]})
    return sorted(out, key=lambda t: (t["file"], t["name"]))



def _short(g, n: str) -> dict:
    a = g.nodes[n]
    return {"node": n, "type": a["type"], "name": entry_name(g, n) if a["type"] != "callsite" else f"call at {a.get('file')}:{a.get('line')}",
            "service": a.get("service"), "repo": a["repo"]}


def test_scope(g, extracts: dict, reached: dict, seeds: list, tests_for, path_status, target: str,
               head_ex=None, is_stale=lambda e: False) -> dict:
    """reached: node -> {"path", "edges", "seeds"} from the impact traversal; seeds carry "effect" and "label"."""
    labels = labels_of(extracts)
    entries, top = [], []
    seed_eps = {s["node"] for s in seeds if g.nodes[s["node"]]["type"] == "endpoint"}
    for n in sorted(set(reached) | seed_eps):
        a = g.nodes[n]
        own = [i for i, s in enumerate(seeds) if s["node"] == n]           # the entry point itself changed (e.g. its contract)
        if a["type"] == "endpoint":
            r = reached.get(n) or {"path": [n], "edges": [], "seeds": []}
        elif a["type"] in ("function", "contract_ref") and n in reached and not any(
                g.nodes[x]["type"] in ("function", "endpoint", "callsite") for x, _ in g.impacted_by(n)):
            r = reached[n]                                    # the top of a reached chain with no analysed entry point above it
        else:
            continue
        sds = [seeds[i] for i in sorted(set(r["seeds"]) | set(own))]
        via = [_short(g, x) for x in r["path"][1:-1] if g.nodes[x]["type"] in ("function", "endpoint")]
        row = {"entry_point": entry_name(g, n), "node": n, "type": a["type"], "service": a["service"], "repo": a["repo"],
               "file": a.get("file"), "line": a.get("line"), **({"label": labels[n]} if n in labels else {}),
               "cross_repository": a["repo"] != target, "change_effect": strongest(s.get("effect") for s in sds),
               "changes": sorted({s["label"] for s in sds}), "via": via, "depth": len(r["edges"]),
               "evidence_status": path_status(r["edges"], is_stale) if r["edges"] else "source_confirmed",
               "tests": _lane_tests(g, n, r["path"], tests_for),
               "path": [{"node": x, "type": g.nodes[x]["type"], "repo": g.nodes[x]["repo"], "file": g.nodes[x].get("file"),
                         "line": g.nodes[x].get("line")} for x in r["path"]],
               "path_edges": [{"rel": e.rel, "status": e.status, "strategy": e.strategy} for e in r["edges"]]}
        chain = " -> ".join(v["name"] for v in reversed(via))
        changed = "; ".join(row["changes"][:2]) + ("; ..." if len(row["changes"]) > 2 else "")
        if a["type"] == "contract_ref":
            row["entry_point"] = f"declared contract in {a['repo']}"
            row["why"] = (f"{a['service']} declares a contract with the changed provider ({a.get('file')}) but no analysed code of it calls the "
                          f"provider: the consumer's code is outside the analysed sources. Change: {changed}.")
            top.append(row)
            continue
        if a["type"] == "endpoint":
            row["why"] = (f"{row['entry_point']} ({a['service']}) " + ("is changed directly" if not r["edges"] else
                          f"depends on the change through {chain}" if chain else "is served by the changed code") + f". Change: {changed}.")
            entries.append(row)
        else:
            row["why"] = (f"{row['entry_point']} ({a['service']}) depends on the change" + (f" through {chain}" if chain else "")
                          + " but no analysed API endpoint calls it: it may be used by a UI, a job or a channel TIA does not analyse. "
                          f"Change: {changed}.")
            top.append(row)
    order = lambda r: (-EFFECT_RANK[r["change_effect"]], not r["cross_repository"], r["depth"], r["node"])
    entries.sort(key=order)
    top.sort(key=order)

    checks = []
    for s in sorted({n for n in set(reached) | seed_eps if g.nodes[n]["type"] == "endpoint"}):
        users = [e for e in entries if e["repo"] != g.nodes[s]["repo"] and s in [p["node"] for p in e["path"]]]
        if len(users) >= 2:
            names = [f"{u['entry_point']} ({u['service']})" for u in users]
            checks.append({"shared_endpoint": s, "shared_entry_point": entry_name(g, s), "service": g.nodes[s]["service"], "repo": g.nodes[s]["repo"],
                           "entry_points": [{"entry_point": u["entry_point"], "node": u["node"], "service": u["service"], "repo": u["repo"]} for u in users],
                           "check": (f"{', '.join(names[:-1])} and {names[-1]} all take data from {entry_name(g, s)} ({g.nodes[s]['service']}): "
                                     "call them for the same input and check they show the same records, except where a documented rule says otherwise.")})

    gaps = rule_gaps(g, extracts, only={n for n in set(reached) | seed_eps if g.nodes[n]["type"] == "endpoint"}, labels=labels)
    handler_of = {e.id: e.handler_id for ex in extracts.values() for e in ex.endpoints}
    for gp in gaps:
        if head_ex is not None and gp["repo"] == target and resolved_in_head(gp, head_ex, handler_of):
            gp["resolved_by_this_change"] = True
            gp["reason"] += (f" This change makes {gp['shared_entry_point']} apply the rule itself: test every consumer, including those that "
                             "already applied it, and check that none applies it twice.")
        else:
            gp["resolved_by_this_change"] = False
    out = {"entry_points": entries, "code_without_entry_point": top, "consistency_checks": checks, "rule_gaps": gaps}
    out["summary"] = summary_of(out)
    return out


def summary_of(ts: dict) -> dict:
    entries, top = ts["entry_points"], ts["code_without_entry_point"]
    by_effect = {k: sum(1 for e in entries if e["change_effect"] == k) for k in EFFECTS}
    return {"entry_points": len(entries), "by_change_effect": {k: v for k, v in by_effect.items() if v},
            "code_without_entry_point": len(top), "consistency_checks": len(ts["consistency_checks"]), "rule_gaps": len(ts["rule_gaps"]),
            "tests_linked": len({(t["repo"], t["file"], t["name"]) for e in entries + top for t in e["tests"]})}


def redact_scope(ts: dict, readable: set) -> dict:
    """The test scope an audience may see: rows in unreadable repositories become a count, path steps through them are masked, and a
    consistency check or rule gap that involves any unreadable repository is withheld whole (a partial one would still reveal it)."""
    hidden = 0
    for key in ("entry_points", "code_without_entry_point"):
        rows = []
        for e in ts.get(key, []):
            if e["repo"] not in readable:
                hidden += 1
                continue
            masked = any(v["repo"] not in readable for v in e["via"])
            e["via"] = [v if v["repo"] in readable else {"node": "[redacted]", "type": "redacted", "name": "[redacted]", "service": None, "repo": None}
                        for v in e["via"]]
            e["path"] = [p if p["repo"] in readable else {"node": "[redacted]", "type": "redacted", "repo": None, "file": None, "line": None}
                         for p in e["path"]]
            e["tests"] = [t for t in e["tests"] if t["repo"] in readable]
            if masked:
                e["why"] = (f"{e['entry_point']} ({e['service']}) depends on the change through code in repositories you cannot access. "
                            f"Change: {'; '.join(e['changes'][:2])}.")
            rows.append(e)
        ts[key] = rows
    checks = [c for c in ts.get("consistency_checks", []) if c["repo"] in readable and all(x["repo"] in readable for x in c["entry_points"])]
    gaps = [g for g in ts.get("rule_gaps", []) if g["repo"] in readable and all(x["repo"] in readable for x in g["applied_by"] + g["missing_in"])]
    hidden += len(ts.get("consistency_checks", [])) - len(checks) + len(ts.get("rule_gaps", [])) - len(gaps)
    ts["consistency_checks"], ts["rule_gaps"] = checks, gaps
    ts["summary"] = summary_of(ts)
    if hidden:
        ts["redacted"] = {"items": hidden, "note": f"{hidden} test-scope item(s) involving repositories you cannot access were omitted."}
    return ts
