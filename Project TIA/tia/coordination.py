"""Multi-team change coordination (PRD FR-045).

For the material findings of an analysis, the owning team of each affected component (other than the changing team) is notified once per
analysis through the channel configured for it, and its acknowledgement is tracked. The coordination status is part of the policy evaluation
(rule type teams_acknowledged) and of the report view; the stored report itself is never changed.

Configuration (tenant setting "coordination"):
    {"auto_notify": false,
     "channels": {"team-payments": {"type": "slack"}, "team-ledger": {"type": "email", "to": "ledger-oncall@bank.example"},
                  "team-cards": {"type": "servicenow_task", "assignment_group": "Cards Engineering"}},
     "default": {"type": "teams"}}
Channel types: slack / teams / webhook (an incoming-webhook URL stored as the tenant secret coordination/<team>/webhook, or
coordination/default/webhook), email (SMTP: TIA_SMTP_HOST, TIA_SMTP_PORT, TIA_SMTP_USER, TIA_SMTP_PASSWORD, TIA_SMTP_FROM; STARTTLS required
off this machine), servicenow_task (a task in the ServiceNow instance of the itsm setting, assigned to the team's group).
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from . import airgap, netsafe

CHANNEL_TYPES = ("slack", "teams", "webhook", "email", "servicenow_task")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NotifyError(Exception):
    pass


def validate(raw, problems: list) -> Optional[dict]:
    if not isinstance(raw, dict):
        problems.append("coordination must be an object: {auto_notify, channels: {team: channel}, default: channel}")
        return None
    out = {"auto_notify": False, "channels": {}, "default": None}
    for k in raw:
        if k not in ("auto_notify", "channels", "default"):
            problems.append(f"unknown setting 'coordination.{k}'")
    if "auto_notify" in raw:
        if isinstance(raw["auto_notify"], bool):
            out["auto_notify"] = raw["auto_notify"]
        else:
            problems.append("coordination.auto_notify must be true or false")

    def channel(where, c):
        if not isinstance(c, dict) or c.get("type") not in CHANNEL_TYPES:
            problems.append(f"{where}.type must be one of {', '.join(CHANNEL_TYPES)}")
            return None
        ch = {"type": c["type"]}
        if c["type"] == "email":
            if not isinstance(c.get("to"), str) or not EMAIL.match(c["to"]):
                problems.append(f"{where}.to must be an e-mail address")
                return None
            ch["to"] = c["to"]
        if c["type"] == "servicenow_task":
            if not isinstance(c.get("assignment_group"), str) or not c["assignment_group"].strip():
                problems.append(f"{where}.assignment_group is required for a ServiceNow task")
                return None
            ch["assignment_group"] = c["assignment_group"].strip()[:120]
        extra = set(c) - {"type", "to", "assignment_group"}
        if extra:
            problems.append(f"{where}: unknown setting(s) {', '.join(sorted(extra))}")
        return ch
    chans = raw.get("channels") or {}
    if not isinstance(chans, dict):
        problems.append("coordination.channels must map a team to a channel")
    else:
        for team, c in sorted(chans.items()):
            ch = channel(f"coordination.channels.{team}", c)
            if ch:
                out["channels"][str(team)] = ch
    if raw.get("default") is not None:
        out["default"] = channel("coordination.default", raw["default"])
    return out


def teams_to_notify(report: dict, owners: dict) -> dict:
    """team -> [material findings of its components], excluding the changing team."""
    mine = owners.get(report["change"].get("service"))
    out: dict = {}
    for f in report.get("findings", []):
        t = owners.get(f["component"])
        if f.get("material") and t and t != mine:
            out.setdefault(t, []).append(f)
    return dict(sorted(out.items()))


def message(report: dict, team: str, findings: list, link: Optional[str]) -> tuple:
    ch = report["change"]
    subject = (f"[TIA] {ch['repository']} {('PR #' + str(ch['pull_request'])) if ch.get('pull_request') else (ch.get('proposed_revision') or '')[:12]}"
               f" affects {len({f['component'] for f in findings})} component(s) owned by {team}")
    lines = [f"Change: {ch['repository']} ({ch.get('change_type')}), analysis {report['analysis_id']}, status {report['status']}.",
             f"Material findings for {team}:"]
    for f in findings[:20]:
        lines.append(f"- {f['severity']} {f['component']}: {f['reason'][:300]}")
    if len(findings) > 20:
        lines.append(f"- ... and {len(findings) - 20} more")
    lines += ["", "Please review and acknowledge: POST /api/v1/impact-analyses/" + report["analysis_id"] + "/acknowledgements"
              + (f" or open {link}" if link else ""), "Advisory evidence produced by TIA; an acknowledgement records that your team has seen it."]
    return subject, "\n".join(lines)


def channel_for(cfg: dict, team: str) -> Optional[dict]:
    return (cfg.get("channels") or {}).get(team) or cfg.get("default")


def _post_json(url: str, payload: dict):
    airgap.guard(url, "notification webhook")
    private = os.environ.get("TIA_ALLOW_PRIVATE_NOTIFY_HOSTS", "").lower() in ("1", "true", "yes")
    try:
        status, _ = netsafe.request("POST", url, data=json.dumps(payload).encode(), private_ok=private,
                                    headers={"Content-Type": "application/json", "User-Agent": "tia"})
    except netsafe.FetchError as e:
        raise NotifyError(f"webhook not reachable: {e}")
    if status >= 300:
        raise NotifyError(f"webhook answered HTTP {status}")


def _email(to: str, subject: str, text: str):
    host = os.environ.get("TIA_SMTP_HOST")
    if not host:
        raise NotifyError("TIA_SMTP_HOST is not configured")
    airgap.guard(f"smtp://{host}", "e-mail notification")
    port = int(os.environ.get("TIA_SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["From"] = os.environ.get("TIA_SMTP_FROM", "tia@localhost")
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(text)
    local = host in ("localhost", "127.0.0.1", "::1")
    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            elif not local:
                raise NotifyError(f"the SMTP server {host} does not offer STARTTLS; refusing to send in clear text")
            if os.environ.get("TIA_SMTP_USER"):
                s.login(os.environ["TIA_SMTP_USER"], os.environ.get("TIA_SMTP_PASSWORD", ""))
            s.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise NotifyError(f"e-mail not sent: {type(e).__name__}: {str(e)[:120]}")


def send(ch: dict, team: str, subject: str, text: str, report: dict, findings: list, secrets, itsm_client=None) -> dict:
    """Delivers one notification -> {"channel", "status": sent|failed, "detail"}."""
    t = ch["type"]
    try:
        if t in ("slack", "teams", "webhook"):
            url = None
            for name in (f"coordination/{team}/webhook", "coordination/default/webhook"):
                try:
                    url = secrets.get_text(name) if secrets is not None else None
                except Exception:                                                  # noqa: BLE001 - absent secret
                    url = None
                if url:
                    break
            if not url:
                raise NotifyError(f"no webhook URL is stored (tenant secret coordination/{team}/webhook)")
            payload = {"text": f"*{subject}*\n{text}"} if t == "slack" else {"title": subject, "text": text.replace("\n", "\n\n")} if t == "teams" else \
                {"subject": subject, "text": text, "team": team, "analysis_id": report["analysis_id"],
                 "findings": [{"id": f.get("id"), "component": f["component"], "severity": f["severity"]} for f in findings]}
            _post_json(url, payload)
            return {"channel": t, "status": "sent", "detail": "webhook accepted the message"}
        if t == "email":
            _email(ch["to"], subject, text)
            return {"channel": t, "status": "sent", "detail": f"e-mail to {ch['to']}"}
        if t == "servicenow_task":
            if itsm_client is None:
                raise NotifyError("the itsm setting (ServiceNow instance and credentials) is required for a task")
            task = itsm_client.create_task(subject, text, ch["assignment_group"], report["analysis_id"])
            return {"channel": t, "status": "sent", "detail": f"task {task.get('number')}", "task": task.get("number")}
        raise NotifyError(f"unknown channel type {t}")
    except NotifyError as e:
        return {"channel": t, "status": "failed", "detail": str(e)}
    except Exception as e:                                                             # noqa: BLE001 - one channel never breaks the others
        return {"channel": t, "status": "failed", "detail": f"{type(e).__name__}: {str(e)[:160]}"}


def status(report: dict, owners: dict, notifications: list, acks: list) -> dict:
    """Coordination status per team: components, latest notification, acknowledgement."""
    teams = []
    for team, fs in teams_to_notify(report, owners).items():
        sent = sorted([n for n in notifications if n.get("team") == team], key=lambda n: (n.get("at") or "", n.get("evidence_id") or 0))
        ack = sorted([a for a in acks if a.get("team") == team], key=lambda a: (a.get("at") or "", a.get("evidence_id") or 0))
        last = sent[-1] if sent else None
        st = ("acknowledged" if ack else "notification_failed" if last and last["status"] == "failed" else "notified" if last else "pending")
        teams.append({"team": team, "components": sorted({f["component"] for f in fs}), "findings": [f.get("id") for f in fs],
                      "notification": {k: last[k] for k in ("channel", "status", "detail", "at") if k in last} if last else None,
                      "acknowledged": {k: ack[-1].get(k) for k in ("by", "at", "comment")} if ack else None, "status": st})
    return {"teams": teams, "summary": {s: sum(1 for t in teams if t["status"] == s) for s in ("pending", "notified", "notification_failed", "acknowledged")},
            "complete": all(t["status"] == "acknowledged" for t in teams)}
