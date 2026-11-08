"""Air-gapped operation (PRD deployment option: no connection outside the customer's network).

With TIA_AIRGAPPED=true every outbound connection TIA makes (GitHub and Azure DevOps APIs, GitHub App tokens, git fetches, identity-provider
discovery, ServiceNow, cloud KMS, trace export) is checked here first and refused unless its host is this machine or is listed in
TIA_AIRGAP_ALLOWED_HOSTS (comma-separated host names or *.suffix patterns: the customer's internal Git server, Vault, identity provider).
A refused call fails loudly (AirGapError), it is never silently skipped, so a misconfigured integration is visible. Analysis itself never
needs the network: repositories are fetched from internal remotes or uploaded, and evidence is pushed to TIA.
"""
from __future__ import annotations

import os
import re
import urllib.parse


class AirGapError(Exception):
    pass


def enabled() -> bool:
    return os.environ.get("TIA_AIRGAPPED", "").strip().lower() in ("1", "true", "yes")


def allowed_hosts() -> list:
    return [h.strip().lower() for h in os.environ.get("TIA_AIRGAP_ALLOWED_HOSTS", "").split(",") if h.strip()]


def _host_ok(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        return True
    for pat in allowed_hosts():
        if pat.startswith("*.") and (host.endswith(pat[1:]) or host == pat[2:]):
            return True
        if host == pat:
            return True
    return False


def host_of(url: str) -> str:
    """The host of an http(s) URL, an ssh git URL (ssh://git@host/x or git@host:x), or '' for a local path."""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        return urllib.parse.urlsplit(url).hostname or ""
    m = re.match(r"^[\w.-]+@([\w.-]+):", url)                     # scp-like git syntax
    return m.group(1) if m else ""


def guard(url: str, what: str) -> str:
    """Returns url, or raises AirGapError in air-gapped mode when it leaves the allowed network. Local paths are always allowed."""
    if not enabled():
        return url
    host = host_of(url)
    if host and not _host_ok(host):
        raise AirGapError(f"{what}: {host} is outside the air-gapped network (TIA_AIRGAPPED is set; allow internal hosts with "
                          "TIA_AIRGAP_ALLOWED_HOSTS)")
    return url


def checked(req, what: str):
    """A urllib Request, after guard() on its URL (wrap every urlopen with it)."""
    guard(req.full_url, what)
    return req
