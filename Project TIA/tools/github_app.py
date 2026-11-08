"""Support script (not production code): register TIA's GitHub App from a manifest (GitHub's "app manifest" flow).

    python tools/github_app.py manifest --url https://tia.bank.example [--org acme] [--name "TIA impact analysis"] --out register.html
        Writes an HTML page; open it in a browser signed in to GitHub and press the button. GitHub shows the App with every permission and
        the webhook already filled in; after you confirm, GitHub redirects to <url>/github-app/registered?code=... - copy the code.
    python tools/github_app.py convert --code <code> --out C:\\secure\\tia-github-app
        Exchanges the one-time code (valid one hour) for the App id, private key, webhook secret and OAuth client credentials, written to
        that directory (owner-only files) - never printed. Then set TIA_GITHUB_APP_ID, TIA_GITHUB_APP_PRIVATE_KEY (a reference to the PEM),
        TIA_GITHUB_WEBHOOK_SECRET, TIA_GITHUB_APP_CLIENT_ID and TIA_GITHUB_APP_CLIENT_SECRET as described in docs/ADMIN-GUIDE.md section 9.

Permissions requested (the minimum for TIA): Contents read, Metadata read, Pull requests write (the comment), Checks write (the check run),
Members read (organisation membership for the access mirror). Events: pull_request, installation, installation_repositories.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PERMISSIONS = {"contents": "read", "metadata": "read", "pull_requests": "write", "checks": "write", "members": "read"}
EVENTS = ["pull_request", "installation", "installation_repositories"]


def manifest(public_url: str, name: str) -> dict:
    u = urllib.parse.urlsplit(public_url)
    if u.scheme != "https" or not u.hostname:
        raise SystemExit("--url must be TIA's public https address")
    base = public_url.rstrip("/")
    return {"name": name[:34], "url": base, "public": False, "description": "Advisory change-impact analysis for pull requests.",
            "hook_attributes": {"url": f"{base}/api/v1/webhooks/github", "active": True},
            "redirect_url": f"{base}/github-app/registered", "callback_urls": [f"{base}/auth/github/callback"],
            "setup_url": f"{base}/", "request_oauth_on_install": False, "default_permissions": PERMISSIONS, "default_events": EVENTS}


def page(m: dict, org: str = "") -> str:
    target = (f"https://github.com/organizations/{urllib.parse.quote(org)}/settings/apps/new" if org else "https://github.com/settings/apps/new")
    return (f"<!doctype html><meta charset=utf-8><title>Register TIA GitHub App</title><h1>Register the TIA GitHub App</h1>"
            f"<p>Permissions: {html.escape(', '.join(f'{k} {v}' for k, v in PERMISSIONS.items()))}. Events: {html.escape(', '.join(EVENTS))}.</p>"
            f"<form action=\"{html.escape(target)}\" method=\"post\"><input type=\"hidden\" name=\"manifest\" value=\"{html.escape(json.dumps(m))}\">"
            f"<button>Create the App on GitHub</button></form>")


def convert(code: str, api_url: str = "https://api.github.com") -> dict:
    req = urllib.request.Request(f"{api_url.rstrip('/')}/app-manifests/{urllib.parse.quote(code)}/conversions", method="POST", data=b"",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "tia-setup"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def write_credentials(d: dict, out: Path) -> list:
    out.mkdir(parents=True, exist_ok=True)
    files = {"app-id.txt": str(d["id"]), "private-key.pem": d["pem"], "webhook-secret.txt": d["webhook_secret"],
             "client-id.txt": d["client_id"], "client-secret.txt": d["client_secret"], "slug.txt": d.get("slug", "")}
    for name, value in files.items():
        p = out / name
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
    return sorted(files)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/github_app.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--url", required=True)
    m.add_argument("--name", default="TIA impact analysis")
    m.add_argument("--org", default="")
    m.add_argument("--out", required=True)
    c = sub.add_parser("convert")
    c.add_argument("--code", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--api-url", default="https://api.github.com")
    a = ap.parse_args(argv)
    if a.cmd == "manifest":
        Path(a.out).write_text(page(manifest(a.url, a.name), a.org), encoding="utf-8")
        print(f"wrote {a.out}: open it in a browser signed in to GitHub")
        return 0
    d = convert(a.code, a.api_url)
    names = write_credentials(d, Path(a.out))
    print(f"GitHub App {d.get('slug')} (id {d['id']}) created; credentials written to {a.out}: {', '.join(names)}")
    print("Next: install the App on the organisation, then set the TIA_GITHUB_APP_* settings (docs/ADMIN-GUIDE.md section 9).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
