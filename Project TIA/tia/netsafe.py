"""Outbound fetches to addresses a tenant administrator controls (identity-provider discovery, JWKS, token endpoint).

A tenant can point its identity provider anywhere, so the fetch must not become a way to reach this platform's own network:
* https only (http only to this machine, and only when the operator allows private hosts: development and tests),
* every address the name resolves to must be public (no loopback, private, link-local - which covers cloud metadata - or reserved ranges),
* the connection is made to the address that was checked, not to a second lookup (no DNS rebinding between check and use),
* no redirects, a short timeout and a response size cap.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import urllib.parse
from typing import Optional

MAX_BODY = 1_000_000
TIMEOUT = 5.0
DEADLINE = 10.0                      # total wall-clock seconds for one request, whatever the peer does


def _kill(conn):
    try:
        if conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)          # wakes a thread blocked in recv (close alone does not, on every platform)
            except OSError:
                pass
            conn.sock.close()
    except OSError:
        pass


class FetchError(Exception):
    pass


def allow_private() -> bool:
    return os.environ.get("TIA_ALLOW_PRIVATE_IDP_HOSTS", "").strip().lower() in ("1", "true", "yes")


def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def resolve(host: str, port: int, private_ok: bool) -> list[str]:
    """Addresses for host, all of which must be acceptable; raises FetchError otherwise."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve {host}: {e}")
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise FetchError(f"{host} has no address")
    if not private_ok:
        bad = [ip for ip in ips if not _public(ip)]
        if bad:
            raise FetchError(f"{host} resolves to a non-public address ({bad[0]}); identity-provider endpoints must be public")
    return ips


def check_url(url: str, private_ok: Optional[bool] = None) -> urllib.parse.SplitResult:
    private_ok = allow_private() if private_ok is None else private_ok
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        raise FetchError(f"'{url}' is not an acceptable URL (https://host/path, no credentials in the URL)")
    if u.scheme == "http" and not private_ok:
        raise FetchError("identity-provider endpoints must use https")
    resolve(u.hostname, u.port or (443 if u.scheme == "https" else 80), private_ok)
    return u


class _Conn(http.client.HTTPConnection):
    """Connects to a pre-validated address; TLS (for https) is checked against the original host name."""

    def __init__(self, host, port, ips, tls_host=None, ctx=None):
        super().__init__(host, port, timeout=TIMEOUT)
        self._ips, self._tls_host, self._ctx = ips, tls_host, ctx

    def connect(self):
        last = None
        for ip in self._ips:
            try:
                sock = socket.create_connection((ip, self.port), TIMEOUT)
                break
            except OSError as e:
                last = e
        else:
            raise FetchError(f"cannot connect: {last}")
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self._tls_host) if self._ctx else sock


def request(method: str, url: str, *, data: Optional[bytes] = None, headers: Optional[dict] = None, private_ok: Optional[bool] = None,
            ca_file: Optional[str] = None) -> tuple[int, bytes]:
    """One HTTP request. Returns (status, body); redirects are not followed; the body is capped."""
    private_ok = allow_private() if private_ok is None else private_ok
    u = check_url(url, private_ok)
    port = u.port or (443 if u.scheme == "https" else 80)
    ips = resolve(u.hostname, port, private_ok)
    ctx = None
    if u.scheme == "https":
        ctx = ssl.create_default_context(cafile=ca_file or os.environ.get("TIA_IDP_CA_FILE") or None)
    conn = _Conn(u.hostname, port, ips, u.hostname, ctx)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    expired = []
    timer = threading.Timer(DEADLINE, lambda: (expired.append(1), _kill(conn)))                 # a peer that dribbles bytes cannot hold a thread for longer than this
    timer.daemon = True
    timer.start()
    try:
        conn.request(method, path, body=data, headers={"Host": u.netloc, "Accept": "application/json", **(headers or {})})
        r = conn.getresponse()
        body = r.read(MAX_BODY + 1)
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        raise FetchError(f"request to {u.hostname} failed: {e}")
    finally:
        timer.cancel()
        conn.close()
    if expired:
        raise FetchError(f"{u.hostname} did not answer within {DEADLINE:.0f} seconds")
    if len(body) > MAX_BODY:
        raise FetchError("response is too large")
    return r.status, body


def get_json(url: str, **kw) -> dict:
    status, body = request("GET", url, **kw)
    if status != 200:
        raise FetchError(f"{urllib.parse.urlsplit(url).netloc} answered HTTP {status}")
    try:
        out = json.loads(body)
    except ValueError:
        raise FetchError("the response is not JSON")
    if not isinstance(out, dict):
        raise FetchError("the response is not a JSON object")
    return out


def post_form(url: str, form: dict, headers: Optional[dict] = None, **kw) -> tuple[int, dict]:
    status, body = request("POST", url, data=urllib.parse.urlencode(form).encode(),
                           headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}, **kw)
    try:
        out = json.loads(body)
    except ValueError:
        out = {}
    return status, out if isinstance(out, dict) else {}
