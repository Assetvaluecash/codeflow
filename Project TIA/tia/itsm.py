"""Change-management integration: ServiceNow first (PRD FR-038, AC-031).

TIA attaches an Impact Report summary and its permanent link to ONE change record, named by its number (CHG0031234) in the PR, the
release metadata or the API call. It writes nothing else: it never creates, updates, approves or transitions a change record, and it
never touches any other record. The only calls are: look the record up (read), list its attachments (read), and add one attachment
(the summary) - once per analysis: attaching the same analysis again finds the existing attachment and does nothing.

The first line of the attachment says when the analysis is not `complete`. Configuration (tenant): itsm.provider = servicenow,
itsm.instance_url = https://<instance>.service-now.com; credentials in the tenant secret store: itsm/servicenow/username and
itsm/servicenow/password (basic) or itsm/servicenow/token (bearer). Outbound calls go only to the configured instance, over https, to
public addresses, with no redirects (tia.netsafe).
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
from typing import Optional

from . import netsafe

CHANGE_NUMBER = re.compile(r"^[A-Z]{2,6}\d{4,12}$")


class ItsmError(Exception):
    def __init__(self, message: str, permanent: bool = True):
        super().__init__(message)
        self.permanent = permanent


def _private_ok() -> bool:
    return os.environ.get("TIA_ALLOW_PRIVATE_ITSM_HOSTS", "").strip().lower() in ("1", "true", "yes")       # tests and on-premise labs only


def summary(report: dict, link: Optional[str]) -> str:
    """The attachment: first line says when the analysis is not complete; then the id, revisions, status, impact, policy outcomes, unknowns."""
    ch, st = report["change"], report["status"]
    lines = []
    if st != "complete":
        lines.append(f"WARNING: this impact analysis is {st.upper()} - " + {
            "complete_with_gaps": "it finished but has unknowns that limit confidence.",
            "partial": "it is incomplete (a limit was reached, data was stale, or a source failed).",
            "failed": "it failed; no impact conclusions can be drawn.", "running": "it is still running."}.get(st, "it is not complete."))
    lines += [f"TIA impact analysis {report['analysis_id']} ({st})", "",
              f"Repository: {ch['repository']}", f"Revisions: {ch.get('base_revision', '')[:12]} -> {ch.get('proposed_revision', '')[:12]}",
              f"Change type: {ch.get('change_type', 'unknown')}"]
    if link:
        lines.append(f"Report: {link}")
    s = report.get("summary") or {}
    if st != "failed":
        lines += ["", f"Findings: {s.get('findings', 0)} ({s.get('material_findings', 0)} material)"]
        for f in [x for x in report.get("findings", []) if x.get("material")][:15]:
            lines.append(f"- {f['severity'].upper()} {f['component']}: {f['reason'].split('. Change:')[0]}")
    pol = report.get("policy") or {}
    if report.get("policy_results"):
        lines += ["", f"Release policy ({pol.get('mode')}): {pol.get('outcome')}"]
        for r in report["policy_results"]:
            lines.append(f"- {r['outcome']} {r['id']}{'' if r['mandatory'] else ' (optional)'}: {r['reason']}")
    unk = report.get("unknowns") or []
    if unk:
        lines += ["", "Unknowns:"] + [f"- {u['area']}: {u['reason']}" for u in unk if u.get("affects_confidence")][:20]
    lines += ["", "Advisory evidence produced by TIA. This attachment does not approve or change the change record."]
    return "\n".join(lines)


class ServiceNow:
    def __init__(self, instance_url: str, username: Optional[str] = None, password: Optional[str] = None, token: Optional[str] = None):
        u = urllib.parse.urlsplit(instance_url or "")
        if u.scheme != "https" and not (_private_ok() and u.scheme == "http"):
            raise ItsmError("the ServiceNow instance URL must use https")
        if not u.hostname or u.path not in ("", "/"):
            raise ItsmError("the ServiceNow instance URL must be just the instance, e.g. https://acme.service-now.com")
        if not (token or (username and password)):
            raise ItsmError("no ServiceNow credentials are stored (itsm/servicenow/username and password, or itsm/servicenow/token)")
        self.base = f"{u.scheme}://{u.netloc}"
        self.auth = f"Bearer {token}" if token else "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def _call(self, method: str, path: str, data: Optional[bytes] = None, ctype: str = "application/json"):
        try:
            status, body = netsafe.request(method, self.base + path, data=data, private_ok=_private_ok(),
                                           headers={"Authorization": self.auth, "Content-Type": ctype, "User-Agent": "tia"})
        except netsafe.FetchError as e:
            raise ItsmError(f"ServiceNow could not be reached: {e}", permanent=False)
        if status >= 400:
            raise ItsmError(f"ServiceNow answered HTTP {status}" + (" (the account needs read access to change_request and to add attachments)"
                                                                    if status in (401, 403) else ""), permanent=status < 500 and status != 429)
        return json.loads(body or b"null")

    def find_change(self, number: str) -> dict:
        q = urllib.parse.urlencode({"sysparm_query": f"number={number}", "sysparm_fields": "sys_id,number,state", "sysparm_limit": 2})
        rows = (self._call("GET", f"/api/now/table/change_request?{q}") or {}).get("result") or []
        if len(rows) != 1:
            raise ItsmError(f"change record {number} was not found" if not rows else f"change record number {number} is ambiguous")
        return rows[0]

    def attach(self, number: str, report: dict, link: Optional[str]) -> dict:
        """Adds the summary to the change record once per analysis -> {"change_record", "sys_id", "attachment", "created"}."""
        if not CHANGE_NUMBER.match(number or ""):
            raise ItsmError(f"'{number}' is not a change-record number (for example CHG0031234)")
        rec = self.find_change(number)
        name = f"tia-impact-{report['analysis_id']}.txt"
        q = urllib.parse.urlencode({"sysparm_query": f"table_name=change_request^table_sys_id={rec['sys_id']}^file_name={name}", "sysparm_limit": 1})
        existing = (self._call("GET", f"/api/now/attachment?{q}") or {}).get("result") or []
        if existing:
            return {"change_record": number, "sys_id": rec["sys_id"], "attachment": existing[0].get("sys_id"), "created": False}
        q = urllib.parse.urlencode({"table_name": "change_request", "table_sys_id": rec["sys_id"], "file_name": name})
        out = self._call("POST", f"/api/now/attachment/file?{q}", summary(report, link).encode("utf-8"), "text/plain")
        return {"change_record": number, "sys_id": rec["sys_id"], "attachment": ((out or {}).get("result") or {}).get("sys_id"), "created": True}


def _create_task(self, subject: str, text: str, group: str, analysis_id: str) -> dict:
    """FR-045: one task per (analysis, assignment group), found again by its correlation id instead of created twice."""
    corr = f"tia-{analysis_id}-{group}"[:100]
    q = urllib.parse.urlencode({"sysparm_query": f"correlation_id={corr}", "sysparm_fields": "sys_id,number", "sysparm_limit": 1})
    rows = (self._call("GET", f"/api/now/table/task?{q}") or {}).get("result") or []
    if rows:
        return {**rows[0], "created": False}
    body = json.dumps({"short_description": subject[:160], "description": text[:4000], "assignment_group": group, "correlation_id": corr}).encode()
    r = (self._call("POST", "/api/now/table/task?sysparm_input_display_value=true&sysparm_fields=sys_id,number", body) or {}).get("result") or {}
    return {**r, "created": True}


ServiceNow.create_task = _create_task


def client_for(cfg_itsm: dict, secrets) -> ServiceNow:
    if not cfg_itsm or cfg_itsm.get("provider") != "servicenow":
        raise ItsmError("no change-management integration is configured (itsm.provider = servicenow)")
    get = (lambda n: secrets.get_text(n)) if secrets is not None else (lambda n: None)
    return ServiceNow(cfg_itsm.get("instance_url"), username=get("itsm/servicenow/username"), password=get("itsm/servicenow/password"),
                      token=get("itsm/servicenow/token"))
