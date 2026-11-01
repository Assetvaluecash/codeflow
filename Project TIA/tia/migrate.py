"""Apply database migrations and manage tenants:

  python -m tia.migrate                         apply pending migrations
  python -m tia.migrate --tenant NAME           also ensure a tenant exists and print its id

Environment
  TIA_ADMIN_DATABASE_URL   admin/owner DSN (never given to the running server)
  TIA_APP_PASSWORD         password to set for the API role tia_app
  TIA_WORKER_PASSWORD      password for the worker role tia_worker (default: same as TIA_APP_PASSWORD)
"""
import argparse
import os
import sys

from .db import APP_ROLE, WORKER_ROLE, TenantAdmin, migrate


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tia.migrate")
    ap.add_argument("--tenant", help="ensure a tenant with this name exists")
    a = ap.parse_args(argv)
    from .credentials import SecretError, env_secret
    from .transport import check_db_tls
    try:
        admin, pw = env_secret("TIA_ADMIN_DATABASE_URL"), env_secret("TIA_APP_PASSWORD")
        if not admin or not pw:
            sys.exit("TIA_ADMIN_DATABASE_URL and TIA_APP_PASSWORD are required")
        done = migrate(check_db_tls(admin), pw, env_secret("TIA_WORKER_PASSWORD"))
    except SecretError as e:
        sys.exit(f"configuration error: {e}")
    print(f"applied: {', '.join(done) if done else 'nothing (up to date)'}; runtime roles: {APP_ROLE}, {WORKER_ROLE}")
    if a.tenant:
        print(f"tenant {a.tenant}: {TenantAdmin(admin).ensure(a.tenant)}")


if __name__ == "__main__":
    main()
