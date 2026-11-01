import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixtures.build_fixture import build  # noqa: E402
from tia.api import create_app  # noqa: E402
from tia.db import WORKER_ROLE, Database, TenantAdmin, app_dsn_from_admin, migrate  # noqa: E402
from tia.impact import AnalysisRequest, analyze  # noqa: E402
from tia.store import Store  # noqa: E402

PG = None            # set by the session fixture; helpers below use it from plain functions
APP_PW = "test-app-pw"


@pytest.fixture(scope="session", autouse=True)
def pg(tmp_path_factory):
    """One real PostgreSQL for the whole run (embedded server); tests isolate themselves with a tenant each."""
    global PG
    import pgserver
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    admin = srv.get_uri()
    migrate(admin, APP_PW)
    db = Database(app_dsn_from_admin(admin, APP_PW))
    wdb = Database(app_dsn_from_admin(admin, APP_PW, role=WORKER_ROLE))
    PG = SimpleNamespace(admin=admin, db=db, worker_db=wdb, tenants=TenantAdmin(admin), app_dsn=app_dsn_from_admin(admin, APP_PW),
                         worker_dsn=app_dsn_from_admin(admin, APP_PW, role=WORKER_ROLE))
    yield PG
    db.close()
    wdb.close()
    srv.cleanup()


def new_tenant() -> str:
    return PG.tenants.ensure(f"t-{uuid.uuid4().hex[:12]}")


def fresh_store() -> Store:
    return Store(PG.db, new_tenant())


def make_app(workspace, tenant=None, **kw):
    """An app on the shared PostgreSQL; a fresh tenant unless one is given (two apps of one tenant model two configurations)."""
    return create_app(PG.db, tenant or new_tenant(), str(workspace), **kw)


@pytest.fixture
def store():
    return fresh_store()


@pytest.fixture(scope="session")
def fx(tmp_path_factory):
    return build(str(tmp_path_factory.mktemp("landscape")))


@pytest.fixture
def run(fx, store):
    def _run(branch="breaking", use_store=True, **kw):
        req = AnalysisRequest(repos=kw.pop("repos", fx["paths"]), target="payments-service",
                              base=fx["shas"]["base"], head=fx["shas"].get(branch, branch), **kw)
        return analyze(req, store if use_store else None)
    return _run


def by_component(report, component, node_type=None):
    return [f for f in report["findings"] if f["component"] == component
            and (node_type is None or f["node_type"] == node_type)]


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def landscape(fx, tmp_path):
    """Working copies of the demo repositories that a test may commit to (register them by path)."""
    out = {}
    for n, p in fx["paths"].items():
        dest = tmp_path / "copies" / n
        git(tmp_path, "clone", "-q", p, str(dest))
        git(dest, "config", "user.name", "t")
        git(dest, "config", "user.email", "t@t")
        out[n] = dest
    return out


@pytest.fixture
def remotes(fx, tmp_path):
    """Bare 'remote' copies of the demo repositories that a test may modify or take offline (register by file URL)."""
    out = {}
    for n, p in fx["paths"].items():
        bare = tmp_path / "remotes" / f"{n}.git"
        bare.parent.mkdir(exist_ok=True)
        git(tmp_path, "clone", "-q", "--bare", p, str(bare))
        out[n] = bare
    return out
