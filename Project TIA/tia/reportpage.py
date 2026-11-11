"""The shareable report page for one analysis (UC1-16): what the PR comment and the check run link to.

Server-rendered HTML with no JavaScript and no external resources, so it opens anywhere, prints cleanly and cannot execute anything
a repository put into a finding. Every value is escaped. It shows exactly what the report holds (including the unknowns that limit
confidence) and links to the machine-readable report, the evidence and the comment text. It is built from the report the caller is
allowed to see, so audience redaction has already been applied.
"""
from __future__ import annotations

from html import escape as _e

SEVERITIES = ("critical", "high", "medium", "low", "unknown")
STATUS_NOTE = {
    "complete_with_gaps": "The analysis ran to completion but has unknowns that limit confidence. This is not a clean impact assessment.",
    "partial": "The analysis is INCOMPLETE: a limit was reached, data was stale, or a source failed. This is not a clean impact assessment.",
    "running": "The analysis is still running; this page shows what exists so far.",
    "failed": "The analysis failed. No impact conclusions can be drawn from this result.",
}
CSS = """
:root{--bg:#fff;--fg:#1b1f24;--mut:#5b6572;--line:#d8dde3;--card:#f6f8fa;--warn:#fff4d6;--warnl:#e0b04a;
--critical:#7f1d1d;--high:#d32f2f;--medium:#b7791f;--low:#6b7280;--unknown:#94a3b8;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#0f1317;--fg:#e6e9ee;--mut:#9aa4b2;--line:#2b333d;--card:#171c22;--warn:#3a2f10;--warnl:#8a6a1f;
--critical:#f87171;--high:#fb7185;--medium:#f5b942;--low:#9aa4b2;--unknown:#64748b;--ok:#3fb950}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;
border-bottom:1px solid var(--line);padding-bottom:4px}.mut{color:var(--mut)}code{font:13px ui-monospace,Consolas,monospace;
background:var(--card);padding:1px 4px;border-radius:4px;word-break:break-all}
.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 10px;font-size:13px;margin-left:6px}
.banner{background:var(--warn);border:1px solid var(--warnl);border-radius:6px;padding:10px 14px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:8px 6px;text-align:left;
vertical-align:top}th{color:var(--mut);font-weight:600}.sev{font-weight:700;text-transform:uppercase;font-size:12px}
.s-critical{color:var(--critical)}.s-high{color:var(--high)}.s-medium{color:var(--medium)}.s-low{color:var(--low)}.s-unknown{color:var(--unknown)}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.card{background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:6px 12px}.card b{font-size:18px}details{margin-top:6px}summary{cursor:pointer;color:var(--mut)}ul{margin:4px 0 4px 18px;padding:0}
footer{margin-top:36px;color:var(--mut);font-size:13px}.wrap{overflow-x:auto}a{color:inherit}
table.report{table-layout:fixed;min-width:860px}table.report td{overflow-wrap:anywhere}
dl.why{margin:0 0 6px}dl.why dt{font-size:11px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.03em;margin-top:6px}
dl.why dt:first-child{margin-top:0}dl.why dd{margin:1px 0 0}dl.why ul{margin:2px 0 2px 16px}
.chip{display:inline-block;white-space:nowrap;font-size:13px}.chip i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
.pathsvg{max-width:100%;height:auto;display:block}.legend{font-size:13px;margin:6px 0 0}
@media print{details{display:block}summary{display:none}}
@media (max-width:700px){table.report{min-width:0;table-layout:auto}.stack tr:first-child{display:none}.stack,.stack tbody,.stack tr,.stack td{display:block;width:100%}
.stack tr{border:1px solid var(--line);border-radius:6px;margin:8px 0;padding:4px 10px}.stack td{border:0;padding:4px 0}
.stack td::before{content:attr(data-label);display:block;color:var(--mut);font-size:12px}}
"""


def _sev(s: str) -> str:
    return f'<span class="sev s-{_e(s)}">{_e(s)}</span>'


def _loc(repo, file, line) -> str:
    if not file:
        return _e(str(repo or ""))
    return f"{_e(str(repo or ''))}:{_e(str(file))}" + (f":{_e(str(line))}" if line else "")


# one colour per component type: the same colours as the web UI (tia/web/index.html TYPE_COLOR)
TYPE_COLOR = {"service": "#1f4fd8", "endpoint": "#2a9df4", "schema": "#8e5bd9", "function": "#8b95a5", "callsite": "#12a594", "contract_ref": "#8a9a2e",
              "test": "#3ba55d", "entity": "#b8651b", "table": "#0b6e4f", "column": "#7bc4a4", "migration": "#6b7280", "type": "#c2410c", "topic": "#b83280",
              "listener": "#d53f8c", "event_schema": "#805ad5", "procedure": "#0e7490", "runtime_ref": "#00838f", "redacted": "#9e9e9e"}
TYPE_NAME = {"endpoint": "endpoint (API)", "callsite": "outbound call", "contract_ref": "declared contract", "entity": "data entity", "type": "class",
             "topic": "event topic", "listener": "event listener", "event_schema": "event schema", "procedure": "stored procedure",
             "runtime_ref": "seen at runtime", "redacted": "hidden"}
EDGE_COLOR = {"contract_confirmed": "#1565c0", "runtime_observed": "#00838f", "inferred": "#ef6c00", "stale": "#9e9e9e"}
SEV_FILL = {"critical": "#7f1d1d", "high": "#d32f2f", "medium": "#f5a524", "low": "#9aa3af", "unknown": "#cbd5e1"}


def _short(p: dict) -> str:
    """A path hop's short name: the function, the endpoint, 'call file:line'; a hop the reader may not see stays hidden."""
    node, t = str(p.get("node") or ""), p.get("type")
    if t == "redacted" or not node or node == "[redacted]":
        return "[hidden]"
    if t == "callsite":
        return f"call {p.get('file') or ''}:{p.get('line') or ''}"
    if t == "runtime_ref":
        return node.replace("runtime:", "", 1).split("->")[0] + " (seen at runtime)"
    if "::" in node:
        return node.split("::")[-1]
    return node.split(":", 1)[1] if ":" in node else node


def _chip(t: str, text: str | None = None) -> str:
    return (f'<span class="chip"><i style="background:{TYPE_COLOR.get(t, "#9aa3af")}"></i>'
            f'{_e(TYPE_NAME.get(t, t or "component") if text is None else text)}</span>')


def _split_reason(reason: str) -> tuple[str, str]:
    """('why this is affected', 'what changed'): the report writes them as one sentence, '... . Change: ...'."""
    how, sep, changed = str(reason or "").partition(". Change: ")
    return (how + "." if sep else how), changed.rstrip(".")


def _why(changed: str, how: str, path: list, where: str = "", limits=(), todo: str = "") -> str:
    """'Why' in labelled parts instead of one long sentence."""
    part = lambda k, v: f"<dt>{k}</dt><dd>{v}</dd>" if v else ""         # noqa: E731
    hops = " <span class=\"mut\">&rarr;</span> ".join(_chip(p.get("type"), _short(p)) for p in path or [])
    return ('<dl class="why">' + part("What changed", _e(changed)) + part("Why this is affected", _e(how)) + part("Path from the change", hops)
            + part("Evidence", f"<code>{where}</code>" if where else "") + part("What to do", _e(todo))
            + part("Limits", "<br>".join(_e(x) for x in limits)) + "</dl>")


def _path_svg(path: list, edges: list, severity: str | None = None) -> str:
    """The row's impact path as a drawing (no script: the page allows none). Top to bottom, from the change to what is affected:
    the changed component has a red ring, the affected one is filled by severity, links carry their relationship and evidence colour."""
    if len(path or []) < 2:
        return '<span class="mut">-</span>'
    step, w = 52, 250
    h = step * (len(path) - 1) + 40
    out = [f'<svg class="pathsvg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
           f'aria-label="Impact path: {_e(" to ".join(_short(p) for p in path))}">']
    for i in range(len(path) - 1):
        e = edges[i] if i < len(edges) else {}
        col = EDGE_COLOR.get(e.get("status"), "#2e7d32")
        y1, y2 = 20 + i * step + 11, 20 + (i + 1) * step - 13
        dash = ' stroke-dasharray="5 4"' if e.get("status") == "inferred" else ""
        out.append(f'<line x1="16" y1="{y1}" x2="16" y2="{y2 - 6}" stroke="{col}" stroke-width="2"{dash}/>'
                   f'<polygon points="11,{y2 - 7} 21,{y2 - 7} 16,{y2 + 1}" fill="{col}"/>')      # the arrow head (no marker: ids must stay unique on the page)
        out.append(f'<text x="30" y="{(y1 + y2) // 2 + 3}" font-size="9" fill="currentColor" opacity=".65">{_e(str(e.get("rel") or ""))}'
                   f'{" (inferred)" if e.get("status") == "inferred" else ""}</text>')
    for i, p in enumerate(path):
        y, last = 20 + i * step, i == len(path) - 1
        name = _short(p)
        fill = SEV_FILL.get(severity, TYPE_COLOR.get(p.get("type"), "#9aa3af")) if last and severity else TYPE_COLOR.get(p.get("type"), "#9aa3af")
        ring = ' stroke="#d32f2f" stroke-width="4"' if i == 0 else ' stroke="currentColor" stroke-opacity=".3" stroke-width="1"'
        out.append(f'<circle cx="16" cy="{y}" r="{10 if last else 8}" fill="{fill}"{ring}><title>{_e(TYPE_NAME.get(p.get("type"), str(p.get("type"))))}</title></circle>')
        out.append(f'<text x="34" y="{y + 4}" font-size="11" fill="currentColor"><title>{_e(name)}</title>{_e(name if len(name) <= 34 else name[:33] + "...")}</text>')
    return "".join(out) + "</svg>"


LEGEND = ('<p class="mut legend">Graph: red ring = what changed; arrows follow the impact; the affected component is filled by severity; '
          'a dashed orange link is inferred. Types: ' + " ".join(_chip(t) for t in ("function", "endpoint", "callsite", "test", "type", "entity", "table",
                                                                                 "topic", "runtime_ref")) + "</p>")


def _finding_row(f: dict) -> str:
    extra = []
    d = f.get("data")
    if d:
        acc = "".join(f"<li>{_e(a['op'])}: <code>{_e(a['function'])}</code> ({_loc(a.get('repo'), a.get('file'), a.get('line'))})</li>"
                      for a in d.get("accessors", []))
        extra.append(f"<details><summary>Data access ({_e(str(d.get('accessor_count', 0)))})</summary>"
                     f"<div>entity <code>{_e(d['entity'])}</code>, table <code>{_e(d['table'])}</code>, database <code>{_e(d['database'])}</code>"
                     f"<ul>{acc}</ul></div></details>")
    if f.get("tests"):
        extra.append("<details><summary>Tests</summary><ul>" + "".join(
            f"<li><code>{_e(t['name'])}</code> ({_e(t['file'])}, {_e(t['association'])})</li>" for t in f["tests"]) + "</ul></details>")
    if f.get("required_verification"):
        extra.append("<details><summary>Required verification</summary><ul>" + "".join(
            f"<li>{_e(v)}</li>" for v in f["required_verification"]) + "</ul></details>")
    rt = f.get("runtime")
    if rt:
        extra.append(f"<details><summary>Observed at runtime</summary><div>{_e(', '.join(rt['environments']) or 'environment not reported')}: "
                     f"{_e(str(rt['calls_observed']))} call(s) between {_e(str(rt['window'][0]))} and {_e(str(rt['window'][1]))}"
                     f"{'; caller versions ' + _e(', '.join(rt['caller_versions'])) if rt['caller_versions'] else ''}</div></details>")
    ev = _e(str(f["evidence_status"]).replace("_", " ")) + (f"<br><span class=\"mut\">{_e(f['resolution_strategy'])}</span>" if f.get("resolution_strategy") else "")
    how, changed = _split_reason(f.get("reason"))
    e0 = f.get("evidence") or {}
    why = _why(changed, how, f.get("path", []), _loc(e0.get("repo"), e0.get("file"), e0.get("line")) if e0.get("file") else "",
               f.get("limitations", []), f.get("remediation") or "")
    kind = f"<br>{_chip(f['node_type'])}" if f.get("node_type") else ""
    return (f"<tr id=\"{_e(f['id'])}\"><td data-label=\"Severity\">{_sev(f['severity'])}<br><span class=\"mut\">{_e(f['severity_rule'])}</span></td>"
            f"<td data-label=\"Component\">{_e(f['component'])}<br><span class=\"mut\">{_e(f['repository'])}</span>{kind}</td>"
            f"<td data-label=\"Evidence\">{ev}</td>"
            f"<td data-label=\"Why\">{why}{''.join(extra)}</td>"
            f"<td data-label=\"Graph\">{_path_svg(f.get('path', []), f.get('path_edges', []), f.get('severity'))}</td></tr>")


EFFECT_TEXT = {"breaking": "breaking", "behaviour_change": "behaviour change", "compatible_change": "compatible change",
               "no_functional_change": "no functional change", "unknown": "unknown (file-level)"}
EFFECT_CLASS = {"breaking": "s-high", "behaviour_change": "s-medium", "compatible_change": "s-low", "no_functional_change": "s-low", "unknown": "s-unknown"}


def _states(t: dict) -> str:
    """The distinct evidence states of a test (FR-023): exists, associated, run result, executed, validated."""
    st = t.get("states")
    if not st:
        return ""
    run = t.get("run") or {}
    where = f" in build {_e(str(run.get('build')))}" + ("" if run.get("of_analysed_revision") else " of another revision") if run else ""
    return f' <span class="badge">{_e(" / ".join(st))}</span><span class="mut">{where}</span>'


def _evidence_sections(report: dict) -> str:
    out = ""
    recs = report.get("test_recommendations") or {}
    if recs.get("tests"):
        rows = "".join(f"<tr><td data-label=\"Priority\">{_e(str(r['priority']))}</td><td data-label=\"Test\"><code>{_e(r['test'])}</code><br>"
                       f"<span class=\"mut\">{_e(r['repo'])}:{_e(r['file'])}</span></td><td data-label=\"Why\">{'<br>'.join(_e(w) for w in r['why'])}</td>"
                       f"<td data-label=\"Last result\">{_e(str(r.get('last_result') or ''))}</td></tr>" for r in recs["tests"])
        out += ("<h2>Recommended tests</h2><div class=\"wrap\"><table class=\"stack\"><tr><th>Priority</th><th>Test</th><th>Why</th><th>Last result</th></tr>"
                f"{rows}</table></div><p class=\"mut\">{_e(recs['note'])}</p>")
    gaps = report.get("test_gaps") or []
    if gaps:
        out += "<h3>Without test evidence</h3><ul>" + "".join(f"<li>{_e(g.get('entry_point') or g.get('component'))}: {_e(g['text'])}</li>" for g in gaps) + "</ul>"
    dep = report.get("deployments")
    if dep:
        rows = "".join(f"<tr><td data-label=\"Repository\">{_e(r)}</td><td data-label=\"Environment\">{_e(env)}</td>"
                       f"<td data-label=\"Deployed\"><code>{_e(d['revision'][:12])}</code></td>"
                       f"<td data-label=\"Compared\">{'analysed revision' if d['is_analysed_revision'] else '<b>not the analysed revision</b>'}"
                       f"{'; the proposed change' if d.get('is_proposed_revision') else ''}</td></tr>"
                       for r, envs in sorted(dep["by_repo"].items()) for env, d in sorted(envs.items()))
        out += ("<h2>Deployments</h2><div class=\"wrap\"><table class=\"stack\"><tr><th>Repository</th><th>Environment</th><th>Deployed</th><th>Compared</th></tr>"
                f"{rows}</table></div>") if rows else ""
    return out


def _test_scope(report: dict) -> str:
    ts = report.get("test_scope")
    if not ts:
        return '<p class="mut">No test scope was recorded for this analysis.</p>'
    rows = []
    for e in ts["entry_points"] + ts["code_without_entry_point"]:
        name = (f"{_e(e['label'])}<br><code>{_e(e['entry_point'])}</code>" if e.get("label") else f"<code>{_e(e['entry_point'])}</code>")
        if e["type"] != "endpoint":
            name += '<br><span class="mut">no analysed API endpoint</span>'
        tests = "".join(f"<li><code>{_e(t['name'])}</code> <span class=\"mut\">({_e(t['file'])}, {_e(t['association'])})</span>{_states(t)}</li>" for t in e["tests"])
        ev = "" if e["evidence_status"] in ("source_confirmed", "contract_confirmed", "corroborated") else f"<br><span class=\"mut\">{_e(str(e['evidence_status']).replace('_', ' '))}</span>"
        rows.append(f"<tr><td data-label=\"Entry point\">{name}</td><td data-label=\"Service\">{_e(e['service'])}<br><span class=\"mut\">{_e(e['repo'])}</span></td>"
                    f"<td data-label=\"Change effect\"><span class=\"sev {EFFECT_CLASS[e['change_effect']]}\">{_e(EFFECT_TEXT[e['change_effect']])}</span>{ev}</td>"
                    f"<td data-label=\"Why\">{_why('; '.join(e.get('changes') or []), _split_reason(e.get('why'))[0], e.get('path', []), _loc(e.get('repo'), e.get('file'), e.get('line')) if e.get('file') else '')}"
                    + (f"<dl class=\"why\"><dt>Tests found</dt><dd><ul>{tests}</ul></dd></dl>" if tests else '<dl class="why"><dt>Tests found</dt><dd class="mut">No test found for it.</dd></dl>')
                    + f"</td><td data-label=\"Graph\">{_path_svg(e.get('path', []), e.get('path_edges', []))}</td></tr>")
    out = (f'<div class="wrap"><table class="stack report"><tr><th style="width:19%">Entry point</th><th style="width:13%">Service</th><th style="width:11%">Change effect</th>'
           f'<th style="width:33%">Why, and the tests found</th><th style="width:24%">Graph</th></tr>{"".join(rows)}</table></div>{LEGEND}'
           if rows else '<p class="mut">No entry point is reached by this change.</p>')
    for gp in ts["rule_gaps"]:
        title = "Rule moved into the shared service" if gp.get("resolved_by_this_change") else "Possible rule gap"
        have = "".join(f"<li>applied by <code>{_e(a['entry_point'])}</code> ({_e(a['service'])}) in <code>{_e(a['function'])}</code> "
                       f"<span class=\"mut\">({_loc(a['repo'], a.get('function_file'), a.get('function_line'))})</span>: <code>{_e(a['expr'])}</code></li>"
                       for a in gp["applied_by"])
        lack = "".join(f"<li>not applied by <code>{_e(m['entry_point'])}</code> ({_e(m['service'])})</li>" for m in gp["missing_in"])
        out += f'<div class="banner"><b>{title}</b>: {_e(gp["reason"])}<ul>{have}{lack}</ul></div>'
    for c in ts["consistency_checks"]:
        out += f"<p><b>Consistency check</b>: {_e(c['check'])}</p>"
    if (ts.get("redacted") or {}).get("items"):
        out += f'<p class="mut">{_e(ts["redacted"]["note"])}</p>'
    return out


def _components(report: dict) -> str:
    rows = []
    for c in report.get("change", {}).get("changed_components", []):
        if c["type"] == "function" and c.get("effect"):
            rows.append(f"<li><b>{_e(EFFECT_TEXT[c['effect']])}</b> {_e(c.get('detail') or c['name'])}</li>")
            continue
        if c["type"] == "function" or c.get("change") == "file_level":
            continue
        mapped = c.get("mapped_by")
        tail = f" &mdash; mapped by {len(mapped)} entity(ies)" if mapped is not None and c["type"].startswith("db_") else ""
        flag = "BREAKING" if c.get("breaking") else "non-breaking"
        rows.append(f"<li><b>{_e(flag)}</b> {_e(c.get('detail') or c['name'])} <span class=\"mut\">({_e(str(c.get('file') or ''))}"
                    f"{':' + _e(str(c['line'])) if c.get('line') else ''}){tail}</span></li>")
    return f"<ul>{''.join(rows)}</ul>" if rows else '<p class="mut">No contract, schema or code change was detected.</p>'


def _unknowns(report: dict) -> str:
    unk = report.get("unknowns", [])
    if not unk:
        return '<p class="mut">None recorded.</p>'
    rows = []
    for u in sorted(unk, key=lambda u: (not u.get("affects_confidence"), u["area"])):
        tag = "affects confidence" if u.get("affects_confidence") else "informational"
        n = f" ({_e(str(u['count']))})" if u.get("count") else ""
        red = f" <span class=\"mut\">[{_e(str(u['redacted_items']))} item(s) not shown]</span>" if u.get("redacted_items") else ""
        rows.append(f"<li><b>{_e(u['area'])}</b>{n} <span class=\"mut\">[{tag}]</span><br>{_e(u['reason'])}{red}</li>")
    return f"<ul>{''.join(rows)}</ul>"


def _coverage(report: dict) -> str:
    cov = report.get("coverage")
    if not cov:
        return '<p class="mut">No coverage was recorded for this analysis.</p>'
    rows = "".join(f"<tr><td data-label=\"Repository\">{_e(r['repo'])}</td><td data-label=\"Service\">{_e(r['service'])}</td>"
                   f"<td data-label=\"Revision\"><code>{_e(str(r['commit'])[:12])}</code></td>"
                   f"<td data-label=\"Scan\">{_e(str(r.get('freshness', {}).get('scan_status', 'unknown')))}</td>"
                   f"<td data-label=\"Files failed\">{_e(str(r.get('files_failed', 0)))}</td></tr>"
                   for r in cov.get("repositories_analysed", []))
    bits = []
    if cov.get("outbound_call_sites"):
        bits.append(f"{cov['outbound_call_sites_resolved']}/{cov['outbound_call_sites']} outbound calls resolved "
                    f"({round(100 * (cov.get('resolution_rate') or 0))}%)")
    if cov.get("data"):
        d = cov["data"]
        bits.append(f"{d['entities']} data entities, {d['data_access_relationships']} data-access relationships, {d['migrations']} migrations")
    if cov.get("traversal", {}).get("truncated"):
        bits.append("traversal was truncated")
    inacc = "".join(f"<li>{_e(x['repo'])}: {_e(x['error'])}</li>" for x in cov.get("repositories_inaccessible", []))
    return ((f"<p>{_e('; '.join(bits))}.</p>" if bits else "") +
            f"<div class=\"wrap\"><table class=\"stack\"><tr><th>Repository</th><th>Service</th><th>Revision</th><th>Scan</th><th>Files failed</th></tr>{rows}</table></div>" +
            (f"<p><b>Not readable:</b></p><ul>{inacc}</ul>" if inacc else ""))


def render_page(report: dict, analysis_url: str = "") -> str:
    """analysis_url: the path of this page's API ('/api/v1/impact-analyses/<id>'), used for the machine-readable links."""
    ch, st = report.get("change", {}), report["status"]
    s = report.get("summary")
    cards = ""
    if s:
        sev = s["by_severity"]
        cards = ('<div class="cards">' + f'<div class="card"><b>{_e(str(s["findings"]))}</b> findings</div>'
                 f'<div class="card"><b>{_e(str(s["material_findings"]))}</b> material</div>' +
                 "".join(f'<div class="card"><b class="s-{k}">{_e(str(sev[k]))}</b> {k}</div>' for k in SEVERITIES if sev.get(k)) + "</div>")
        held = report.get("redacted", {}).get("findings", 0)
        if held:
            cards += f'<p class="banner">{_e(str(held))} finding(s) in repositories you cannot access are omitted from this page.</p>'
        if not s["findings"] and not held and s.get("message"):
            cards += f"<p>{_e(s['message'])}</p>"
    rows = "".join(_finding_row(f) for f in report.get("findings", []))
    table = (f'<div class="wrap"><table class="stack report"><tr><th style="width:13%">Severity</th><th style="width:14%">Component</th><th style="width:11%">Evidence</th>'
             f'<th style="width:38%">Why</th><th style="width:24%">Graph</th></tr>{rows}</table></div>{LEGEND}'
             if rows else '<p class="mut">No findings.</p>')
    note = f'<div class="banner">{_e(STATUS_NOTE[st])}</div>' if st in STATUS_NOTE else ""
    if st == "failed" and report.get("failure_reason"):
        note += f'<p><b>Reason:</b> {_e(str(report["failure_reason"]))}</p>'
    pr = f" &middot; pull request #{_e(str(ch['pull_request']))}" if ch.get("pull_request") is not None else ""
    base, head = str(ch.get("base_revision", ""))[:7], str(ch.get("proposed_revision", ""))[:7]
    snap = report.get("evidence_snapshot_id")
    links = ""
    if analysis_url:
        u = _e(analysis_url)
        links = (f'<p><a href="{u}">Report (JSON)</a> &middot; <a href="{u}/comment">Pull request comment (markdown)</a>'
                 + (f' &middot; <a href="{u}/evidence">Evidence</a>' if snap else "") + "</p>")
    ver = report.get("extractor_versions") or {}
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>TIA analysis {_e(report['analysis_id'])}</title><style>{CSS}</style></head>
<body><main>
<h1>Technical impact analysis <span class="badge">{_e(st)}</span></h1>
<p class="mut"><code>{_e(str(ch.get('repository', '')))}</code> {_e(base)} &rarr; {_e(head)}{pr} &middot; change type <b>{_e(str(ch.get('change_type', 'unknown')))}</b>
&middot; analysis <code>{_e(report['analysis_id'])}</code></p>
{note}{cards}{links}
<h2>What to test</h2>{_test_scope(report) if st != "failed" else '<p class="mut">The analysis failed.</p>'}
{_evidence_sections(report) if st != "failed" else ""}
<h2>Findings</h2>{table}
<h2>Changes analysed</h2>{_components(report)}
<h2>Unknowns</h2>{_unknowns(report)}
<h2>Coverage</h2>{_coverage(report)}
<h2>Evidence</h2>
<p>Evidence snapshot <code>{_e(str(snap or 'none'))}</code>. The analysis can be re-run on this snapshot and must give an identical result
(<code>POST {_e(analysis_url or '/api/v1/impact-analyses/&lt;id&gt;')}/reproduce</code>).</p>
<footer>Advisory only &mdash; this report does not block a merge. Generated {_e(str(report.get('generated_at', '')))} &middot;
configuration <code>{_e(str(report.get('configuration_version', '')))}</code> &middot; extractors
<code>{_e(', '.join(f'{k} {v}' for k, v in sorted(ver.items())))}</code></footer>
</main></body></html>
"""


def not_found_page() -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex"><title>Not found</title>'
            f'<style>{CSS}</style></head><body><main><h1>Analysis not found</h1><p class="mut">There is no analysis with this id that you '
            f"can see.</p></main></body></html>")
