"""Run the TIA server:  python -m tia.server

Environment
  TIA_DATABASE_URL        PostgreSQL DSN for the runtime role (tia_app)            [required]
  TIA_TENANT_ID           tenant UUID served by this process (until SSO is built)  [required]
  TIA_WORKSPACE           where remote repositories are cloned (default ./.tia-data/repos)
  TIA_HOST / TIA_PORT     bind address (default 127.0.0.1:8080)
  TIA_ALLOW_LOCAL_PATHS   "true" to allow ingesting local paths / file:// URLs (development only)
  TIA_ALLOWED_GIT_HOSTS   comma-separated host allow-list for remote cloning (default: any)
  TIA_AUDIT_KEY           secret for keyed (HMAC) audit chain; keep it outside the database [strongly recommended]
  TIA_GITHUB_WEBHOOK_SECRET  shared secret for POST /api/v1/webhooks/github (receiver is off without it)
  TIA_GITHUB_TOKEN           token used by the worker to fetch PRs and post comments (interim; GitHub App later)
  TIA_GITHUB_API_URL         GitHub Enterprise API base (default https://api.github.com)
  TIA_ALLOW_UPLOADS          "true" (default) lets users upload a zip or folder; TIA_MAX_UPLOAD_BYTES (default 2 GiB), TIA_MAX_UPLOAD_FILES (200000)
  TIA_COMMENT_AUDIENCE       "target" (default: PR comment names only the PR's repository) or "all" (full detail)

Schema is created with:  python -m tia.migrate   (admin credentials; separate from the runtime role)
"""
import os
import sys

import uvicorn

from .api import create_app
from .db import Database
from .upload import UploadConfig
from .webhooks import ScmConfig


def build():
    dsn, tenant = os.environ.get("TIA_DATABASE_URL"), os.environ.get("TIA_TENANT_ID")
    if not dsn or not tenant:
        sys.exit("TIA_DATABASE_URL and TIA_TENANT_ID are required (see tia/server.py)")
    hosts = [h.strip() for h in os.environ.get("TIA_ALLOWED_GIT_HOSTS", "").split(",") if h.strip()]
    return create_app(Database(dsn), tenant, os.environ.get("TIA_WORKSPACE", ".tia-data/repos"),
                      allow_local=os.environ.get("TIA_ALLOW_LOCAL_PATHS", "").lower() in ("1", "true", "yes"),
                      allowed_hosts=hosts or None,
                      audit_key=os.environ["TIA_AUDIT_KEY"].encode() if os.environ.get("TIA_AUDIT_KEY") else None,
                      scm=ScmConfig.from_env(), uploads=UploadConfig.from_env())


def main():
    uvicorn.run(build(), host=os.environ.get("TIA_HOST", "127.0.0.1"), port=int(os.environ.get("TIA_PORT", "8080")),
                log_level="info")


if __name__ == "__main__":
    main()
