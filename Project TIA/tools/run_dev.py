"""Support script (not production code): run TIA locally on an embedded PostgreSQL.

    python tools/run_dev.py [--data demo-data/dev] [--port 8080] [--demo]

Creates/starts a PostgreSQL under --data, applies migrations, ensures the tenant "dev", then serves the app
(local paths enabled, single tenant, no authentication - development only). --demo also registers the 6-repo
demo landscape. Data persists between runs; delete the folder for a clean start.
"""
import argparse
import secrets
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dataclasses  # noqa: E402

import pgserver  # noqa: E402
import uvicorn  # noqa: E402

from tia.api import create_app  # noqa: E402
from tia.analysis_service import make_handlers  # noqa: E402
from tia.db import WORKER_ROLE, Database, TenantAdmin, app_dsn_from_admin, migrate  # noqa: E402
from tia.jobs import JobQueue  # noqa: E402
from tia.repos import IngestError, RepoService  # noqa: E402
from tia.store import Store  # noqa: E402
from tia.webhooks import ScmConfig  # noqa: E402
from tia.worker import Worker  # noqa: E402


def secret(path: Path) -> str:
    if not path.exists():
        path.write_text(secrets.token_hex(24))
    return path.read_text().strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "demo-data" / "dev"))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    data = Path(a.data); data.mkdir(parents=True, exist_ok=True)
    srv = pgserver.get_server(str(data / "pg"), cleanup_mode="stop")
    admin = srv.get_uri()
    pw = secret(data / "app-password.txt")
    print("migrations applied:", migrate(admin, pw) or "none (up to date)")
    tenant = TenantAdmin(admin).ensure("dev")
    db = Database(app_dsn_from_admin(admin, pw))
    audit_key = secret(data / "audit.key").encode()
    if a.demo:
        from fixtures.build_fixture import build
        land = data / "landscape"
        fx = build(str(land)) if not land.exists() else {"paths": {p.name: str(p) for p in land.iterdir() if (p / ".git").exists()}}
        svc = RepoService(Store(db, tenant, audit_key), str(data / "repos"), allow_local=True)
        for name, path in fx["paths"].items():
            try:
                svc.register(path=path, name=name)
            except IngestError:
                pass                                   # already registered on a previous run
    # development convenience: one in-process worker (production runs `python -m tia.worker` separately)
    wdb = Database(app_dsn_from_admin(admin, pw, role=WORKER_ROLE))
    scm = ScmConfig.from_env()
    if not scm.github_webhook_secret:
        scm = dataclasses.replace(scm, github_webhook_secret=secret(data / "github-webhook-secret.txt").encode())
    handlers = make_handlers(wdb, allow_local=True, audit_key=audit_key, workspace=str(data / "repos"), scm=scm)
    stop = threading.Event()
    threading.Thread(target=Worker(JobQueue(wdb), handlers).run_forever, args=(stop,), daemon=True).start()
    app = create_app(db, tenant, str(data / "repos"), allow_local=True, audit_key=audit_key, scm=scm)
    print(f"TIA dev server: http://127.0.0.1:{a.port}  (tenant 'dev' = {tenant})")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info")


if __name__ == "__main__":
    main()
