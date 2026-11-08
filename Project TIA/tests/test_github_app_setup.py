"""GitHub App registration helper (tools/github_app.py): the manifest asks for exactly what TIA uses, and the code exchange writes the
credentials to files without printing them (against a stand-in for GitHub's conversion endpoint)."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import github_app  # noqa: E402


PEM = "\n".join(["-----BEGIN " + "RSA PRIVATE" + " KEY-----", "abc", "-----END " + "RSA PRIVATE" + " KEY-----", ""])   # built: no key-shaped text in the repo


def test_manifest_and_page():
    m = github_app.manifest("https://tia.bank.example/", "TIA impact analysis")
    assert m["hook_attributes"]["url"] == "https://tia.bank.example/api/v1/webhooks/github" and m["public"] is False
    assert m["default_permissions"] == {"contents": "read", "metadata": "read", "pull_requests": "write", "checks": "write", "members": "read"}
    assert "pull_request" in m["default_events"]
    p = github_app.page(m, "acme")
    assert 'action="https://github.com/organizations/acme/settings/apps/new"' in p and "&quot;pull_requests&quot;" in p
    with pytest.raises(SystemExit):
        github_app.manifest("http://tia.bank.example", "x")


def test_conversion_writes_credentials_without_printing_them(tmp_path, capsys):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append(self.path)
            b = json.dumps({"id": 4242, "slug": "tia-impact", "pem": PEM,
                            "webhook_secret": "whsec-123", "client_id": "Iv1.abc", "client_secret": "csec-456"}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert github_app.main(["convert", "--code", "abc123", "--out", str(tmp_path / "creds"),
                                "--api-url", f"http://127.0.0.1:{srv.server_port}"]) == 0
    finally:
        srv.shutdown()
    assert seen == ["/app-manifests/abc123/conversions"]
    assert (tmp_path / "creds" / "app-id.txt").read_text() == "4242"
    assert (tmp_path / "creds" / "private-key.pem").read_text().startswith("-----BEGIN")
    out = capsys.readouterr().out
    assert "4242" in out and "whsec-123" not in out and "csec-456" not in out and "PRIVATE" not in out
