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
@media print{details{display:block}summary{display:none}}
@media (max-width:700px){.stack tr:first-child{display:none}.stack,.stack tbody,.stack tr,.stack td{display:block;width:100%}
.stack tr{border:1px solid var(--line);border-radius:6px;margin:8px 0;padding:4px 10px}.stack td{border:0;padding:4px 0}
.stack td::before{content:attr(data-label);display:block;color:var(--mut);font-size:12px}}
"""


def _sev(s: str) -> str:
    return f'<span class="sev s-{_e(s)}">{_e(s)}</span>'


def _loc(repo, file, line) -> str:
    if not file:
        return _e(str(repo or ""))
    return f"{_e(str(repo or ''))}:{_e(str(file))}" + (f":{_e(str(line))}" if line else "")


def _finding_row(f: dict) -> str:
    path = " &rarr; ".join(f"{_e(p['type'])} <code>{_e(str(p['node']).split('::')[-1])}</code>" for p in f.get("path", []))
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
    if path:
        extra.append(f"<details><summary>Evidence path</summary><div>{path}</div></details>")
    lims = "".join(f"<li class=\"mut\">{_e(x)}</li>" for x in f.get("limitations", []))
    ev = _e(f["evidence_status"]) + (f"<br><span class=\"mut\">{_e(f['resolution_strategy'])}</span>" if f.get("resolution_strategy") else "")
    return (f"<tr id=\"{_e(f['id'])}\"><td data-label=\"Severity\">{_sev(f['severity'])}<br><span class=\"mut\">{_e(f['severity_rule'])}</span></td>"
            f"<td data-label=\"Component\">{_e(f['component'])}<br><span class=\"mut\">{_e(f['repository'])}</span></td>"
            f"<td data-label=\"Evidence\">{ev}</td>"
            f"<td data-label=\"Why\">{_e(f['reason'])}<ul>{lims}</ul>{''.join(extra)}</td></tr>")


def _components(report: dict) -> str:
    rows = []
    for c in report.get("change", {}).get("changed_components", []):
        if c["type"] == "function" or c.get("change") == "file_level":
            continue
        mapped = c.get("mapped_by")
        tail = f" &mdash; mapped by {len(mapped)} entity(ies)" if mapped is not None and c["type"].startswith("db_") else ""
        flag = "BREAKING" if c.get("breaking") else "non-breaking"
        rows.append(f"<li><b>{_e(flag)}</b> {_e(c.get('detail') or c['name'])} <span class=\"mut\">({_e(str(c.get('file') or ''))}"
                    f"{':' + _e(str(c['line'])) if c.get('line') else ''}){tail}</span></li>")
    return f"<ul>{''.join(rows)}</ul>" if rows else '<p class="mut">No contract or schema change was detected.</p>'


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
    table = (f'<div class="wrap"><table class="stack"><tr><th>Severity</th><th>Component</th><th>Evidence</th><th>Why</th></tr>{rows}</table></div>'
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
