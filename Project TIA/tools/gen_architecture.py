"""Generates the as-built architecture of TIA from the code itself: docs/ARCHITECTURE-AS-BUILT.md and docs/ARCHITECTURE-AS-BUILT.html.

    python tools/gen_architecture.py            # rewrite both files
    python tools/gen_architecture.py --check    # exit 1 if either is out of date or the code breaks the layer rule (the test suite does this)

What is read from the code (so it cannot drift): every module of tia/ and tia/lang/, its own description (the first sentence of its
docstring), its size, and which TIA modules it imports; every table in tia/migrations/ and whether it has forced row-level security;
the deployment files under deploy/. What is written here by hand: which layer each module belongs to (LAYERS), the few imports allowed to
cross the layer rule and why (EXCEPTIONS), the request flows (FLOWS) and the design-versus-build table (DESIGN). The generator refuses to
build when a module is in no layer or two, when a listed module no longer exists, when an import breaks the layer rule without a recorded
reason, or when a flow names a module or table that does not exist.

docs/ARCHITECTURE.md stays the design (written before the build); this is the system as built.
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter, defaultdict
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import gen_walkthrough_html as gw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PKG, MIGRATIONS, DEPLOY = ROOT / "tia", ROOT / "tia" / "migrations", ROOT / "deploy"
OUT_MD, OUT_HTML = ROOT / "docs" / "ARCHITECTURE-AS-BUILT.md", ROOT / "docs" / "ARCHITECTURE-AS-BUILT.html"

# ---- the hand-written part: layers, top (closest to the user) to bottom. A module may import only modules in its own layer or below. ----
LAYERS = [
    ("entry", "Entry points", "#1f4fd8",
     "The processes and commands that start everything: the API server, the queue worker, the command line and the schema migration.",
     ["server", "worker", "cli", "migrate"]),
    ("surface", "HTTP API, web UI and agent surfaces", "#0b6e4f",
     "Every way in: the REST API and web UI, sign-in, permissions on each request, the shareable report page, MCP for coding assistants "
     "and the Knowledge Fabric adapter. Requests are authenticated, classified and bound to one tenant here, before any work starts.",
     ["api", "authapi", "oidc", "saml", "ratelimit", "gate", "health", "reportpage", "mcp", "kfadapter"]),
    ("orchestration", "Orchestration, ingestion and jobs", "#8e5bd9",
     "Turns a request into work: resolves revisions, registers and syncs repositories, queues jobs and runs an analysis end to end, the same "
     "way for the UI, the API and a pull-request webhook.",
     ["analysis_service", "jobs", "repos", "upload", "relationships", "incidents", "temporal", "calibration"]),
    ("connectors", "Source-control and change-management connectors", "#c2410c",
     "Talks to the outside on the platform's behalf: webhooks in, pull-request comments and check runs out, repository permissions mirrored "
     "from the host, ServiceNow change records. Nothing below this layer calls a git host or ITSM.",
     ["webhooks", "github", "githubapp", "publish", "scmhosts", "ado", "itsm", "access"]),
    ("engine", "Impact engine and tester views", "#b3261e",
     "The deterministic core: diff the change, walk the graph within bounds, attach evidence, score with the rule table, and turn the result "
     "into the report and the tester views (test scope, regression test selection, configuration checks, coverage, automation inventory, "
     "tester pack, graph questions).",
     ["impact", "enrich", "evidence", "report", "findingfilter", "findings_catalog", "structure", "communities", "diagram",
      "testscope", "coverage", "automation", "testerpack", "graphquery", "graphview", "regression", "configkeys"]),
    ("graph", "Graph and identity resolution", "#0e7490",
     "Connects facts from many repositories into one typed graph: who calls which endpoint, which code reads which table, with the "
     "resolution strategy and evidence on every edge. Versioned graphs are stored, never edited.",
     ["graph", "resolve", "graphstore", "data"]),
    ("extraction", "Extraction (language adapters)", "#a86400",
     "Reads code as bytes and parses it into facts (endpoints, call sites, functions, schemas, tables, tests, UI elements). Never imports, "
     "runs or builds customer code. Facts are cached per git blob, so an unchanged file is never parsed twice.",
     ["extraction", "__init__", "adapters", "extract_python", "extract_django", "extract_java", "extract_data", "extract_orm", "extract_openapi", "extract_ui",
      "sqllineage", "streams", "testcases", "registry", "exclude",
      "lang", "lang/common", "lang/csharp", "lang/golang", "lang/kotlin", "lang/typescript"]),
    ("tenancy", "Tenancy, identity and persistence", "#4d7c0f",
     "Everything stored, and who it belongs to: tenant-bound database transactions, the analysis and evidence store with its audit chain, "
     "tenants and their configuration, users, roles, secrets and customer keys, retention and backup.",
     ["db", "store", "tenants", "tenantconfig", "identity", "rbac", "secretstore", "kms", "saas", "retention", "backup"]),
    ("rules", "Rules (deterministic, versioned)", "#be185d",
     "The rule tables and their evaluation: severity, release policy, contract and event compatibility, team coordination. They depend "
     "on nothing but the foundations. Tenants can override them through configuration; every finding records which rule version "
     "produced it. One module here also acts: `coordination` delivers the team notifications its rules call for (chat webhook or "
     "e-mail, through the air-gap and outbound-safety guards).",
     ["severity", "policy", "contracts", "events", "coordination"]),
    ("foundation", "Foundations", "#475569",
     "Shared building blocks with no business logic: the data model, read-only git access, secret scrubbing, safe outbound connections, "
     "air-gap guard, transport security, logging, metrics and tracing.",
     ["models", "gitutil", "scrub", "credentials", "transport", "netsafe", "airgap", "jwtverify", "metrics", "telemetry", "tracing", "obs"]),
]

# imports that cross the layer rule on purpose, with the reason. Anything else that crosses it stops the build.
EXCEPTIONS = {
    ("store", "repos"): "Lazy import of the `Repo` record type (a dataclass) to rebuild registry rows; no repository sync is called from storage.",
    ("analysis_service", "worker"): "Imports the `PermanentError` exception class so a job handler can tell the worker not to retry; no worker code runs.",
}

FLOWS = [
    ("Someone runs an analysis (web UI or REST)", [
        "`api` receives `POST /api/v1/impact-analyses`. The middleware in `authapi` classifies the route with `rbac` (a route with no entry "
        "is refused), `ratelimit` and `gate` check the caller and the tenant, and the database transaction is bound to that tenant (`db`).",
        "`analysis_service` resolves the request into exact commits (`repos`, `gitutil`) and caps its limits with the tenant's configuration "
        "(`tenantconfig`). With `?async=true` it is queued instead (`jobs`) and a `worker` picks it up; the steps below are the same.",
        "`extraction` lists the changed files with `gitutil` and parses only blobs it has not seen (`extract_python`, `extract_java`, "
        "`extract_data`, `extract_ui`, the `lang` adapters); facts are cached per blob in `file_extracts`.",
        "`graph` and `resolve` connect the facts into one typed graph; `graphstore` records its version in `graph_versions`.",
        "`impact` diffs the change (`contracts`, `data`, `events`), walks the graph within bounds, adds evidence (`enrich`, `evidence`), "
        "scores each finding (`severity`), evaluates release policy (`policy`) and builds the tester views (`testscope`, `coverage`, "
        "`automation`).",
        "`report` renders the result; `store` saves it in `analyses` with its evidence snapshot id and appends to the `audit_log` hash chain. "
        "The web UI shows it; `reportpage` serves the shareable page; `testerpack` answers the tester-pack call.",
    ]),
    ("A pull request is opened or updated", [
        "The host's webhook reaches `api`: `webhooks` (GitHub, with `githubapp` for App installations), `scmhosts` (GitLab, Bitbucket) or "
        "`ado` (Azure DevOps). The signature is verified and the repository matched before anything is queued.",
        "`jobs` queues one analysis per pull request; a newer push supersedes the older queued job. `worker` claims it with "
        "`FOR UPDATE SKIP LOCKED` under a lease, and retries with backoff on failure.",
        "`analysis_service` runs exactly the same analysis as above.",
        "`publish` updates one advisory comment and one check run in place (`github`, or the matching connector); `access` keeps findings "
        "from repositories the pull request's readers cannot see out of the comment; `itsm` attaches the report to a ServiceNow change "
        "record when one is configured.",
    ]),
    ("CI, deployments and runtime send evidence", [
        "Pipelines post test results, coverage, deployments and observed calls to `api` (`/api/v1/evidence/*`). `evidence` parses them, "
        "removes secrets (`scrub`) and stores them in `evidence_records`.",
        "Nothing is fetched during an analysis: the next analysis reads those records through `enrich` and pins their ids in its snapshot "
        "(`evidence_snapshots`). Ingest first, then analyse.",
    ]),
    ("An AI agent or another tool asks a question", [
        "Coding assistants and testing agents call `mcp` (JSON-RPC over `POST /mcp`, read-only tools only). Knowledge Fabric calls "
        "`kfadapter` through `POST /query/tia`. Scripts use the Python SDK in `sdk/python`. All of them go through `api`, so the same "
        "sign-in, permissions and tenant binding apply.",
        "Direct questions about the code (who consumes this, what does it depend on, which tests cover it) are answered by `graphquery` from "
        "the stored graph; an ambiguous name returns the candidates instead of a guess.",
    ]),
]

DESIGN = [
    ("API and backend: Python + FastAPI", "Python 3, FastAPI app in `tia/api.py` served by uvicorn (`tia/server.py`)", "Built as designed"),
    ("Web UI: React", "One HTML page with plain JavaScript (`tia/web/index.html`) and Cytoscape.js vendored under `tia/web/vendor/`; no "
     "build step and no CDN, so it works air-gapped. The shareable page (`tia/reportpage.py`) is server-rendered with no script at all.", "Built differently"),
    ("Graph store: PostgreSQL first", "Facts cached per git blob in PostgreSQL; the typed graph is built in memory for each analysis "
     "(`tia/graph.py`) and its versions recorded in PostgreSQL (`tia/graphstore.py`). No separate graph database.", "Built as designed"),
    ("Evidence store: S3-compatible object storage", "Evidence records and snapshots are PostgreSQL tables; source code is kept as bare git "
     "clones and uploads on one volume. There is no object store.", "Built differently"),
    ("Background jobs: PostgreSQL-backed queue", "`jobs` table with leases, retries, per-tenant caps and `FOR UPDATE SKIP LOCKED` "
     "(`tia/jobs.py`, `tia/worker.py`)", "Built as designed"),
    ("Graph writer is the only writer of the graph", "The graph is a pure function of the cached facts and configuration; a version is "
     "written once and never edited", "Built as designed"),
    ("Impact engine never calls external systems", "`tia/impact.py` reads only the stores and the local git clones; connectors sit in a "
     "higher layer and the layer rule below is checked on every test run", "Built as designed"),
    ("API gateway with SSO, tenant resolution and rate limits", "No separate gateway: sign-in, tenant binding and rate limits run inside the "
     "API (`authapi`, `gate`, `ratelimit`); TLS ends at the ingress or reverse proxy", "Built differently"),
    ("Tenant admin service", "A module and command (`tia/tenants.py`, `python -m tia.tenants`) plus admin API routes, not a separate service",
     "Built differently"),
    ("GitHub App + webhooks", "Built (`tia/githubapp.py`, `tia/webhooks.py`); the PR comment verified live on github.com, the App "
     "installation flow against a stand-in only", "Verified (simulated)"),
    ("GitLab, Bitbucket, Azure DevOps, ServiceNow, customer KMS", "Built; verified against stand-ins, not the real services", "Verified (simulated)"),
    ("Containers, customer-controlled deployment", "One image (`deploy/Dockerfile`), Docker Compose (run on Docker Desktop), Helm chart, "
     "Terraform for Azure (written, not applied)", "Built"),
    ("Optional LLM summary, never inventing edges", "No LLM or AI library is used anywhere in `tia/`; every result comes from parsing and "
     "fixed rules", "Built as designed"),
]

PROCESSES = [
    ("API server", "`python -m tia.server`", "`tia_app`", "8080 (HTTP), web UI, REST, `/mcp`, `/query/tia`, `/healthz`, `/metrics`"),
    ("Worker", "`python -m tia.worker`", "`tia_worker`", "9090 (health and metrics only); claims jobs, fetches from git hosts"),
    ("Migration", "`python -m tia.migrate`", "owner (admin DSN)", "none; runs once before every install or upgrade (Helm pre-install hook, "
     "Compose `migrate` service)"),
    ("Command line", "`tia analyze` (`tia.cli:main`)", "`tia_app`, optional", "none; an analysis from a terminal, with or without a database"),
]


# ---- facts read from the code ------------------------------------------------------------------------------------------------------------
def module_files() -> dict[str, Path]:
    files = {p.stem: p for p in PKG.glob("*.py")}
    for p in (PKG / "lang").glob("*.py"):
        files["lang" if p.stem == "__init__" else f"lang/{p.stem}"] = p
    return dict(sorted(files.items()))


def describe(src: str) -> str:
    doc = ast.get_docstring(ast.parse(src)) or ""
    para = " ".join(doc.strip().split("\n\n")[0].split())
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z(`])", para)[0].rstrip(":").strip()
    if len(first) > 230:
        first = first[:230].rsplit(" ", 1)[0] + " ..."
    return first


def imports(name: str, src: str, known: set[str]) -> set[str]:
    in_lang = name.startswith("lang")
    out = set()
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.ImportFrom):
            continue
        if n.level == 0:
            if (n.module or "").startswith("tia."):
                out.add(n.module.split(".")[1])
            continue
        top = n.level == 2 or (n.level == 1 and not in_lang)               # relative to the tia package
        base = "" if top else "lang/"
        if n.module:
            first = n.module.split(".")[0]
            out.add(base + first if base + first in known else (first if first in known else "__init__"))
        else:
            for a in n.names:
                out.add(base + a.name if base + a.name in known else ("__init__" if top else "lang"))
    out.discard(name)
    return {m for m in out if m in known}


def read_modules() -> dict[str, dict]:
    files = module_files()
    known = set(files)
    mods = {}
    for name, path in files.items():
        src = path.read_text(encoding="utf-8")
        mods[name] = {"path": path.relative_to(ROOT).as_posix(), "lines": len(src.splitlines()), "doc": describe(src),
                      "uses": imports(name, src, known)}
    for name, m in mods.items():
        m["used_by"] = sorted(o for o, om in mods.items() if name in om["uses"])
    return mods


def read_tables() -> list[dict]:
    tables, forced, granted = [], set(), set()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        sql = f.read_text(encoding="utf-8")
        for names in re.findall(r"(?is)\bgrant\s+[^;]*?\bon\s+([\w, ]+?)\s+to\s+tia_(?:app|worker)\b", sql):
            granted.update(n.strip() for n in names.split(","))
        for t in re.findall(r"(?im)^\s*create table (?:if not exists )?(\w+)", sql):
            tables.append({"table": t, "migration": f.name})
        forced.update(re.findall(r"(?i)alter table (\w+) force row level security", sql))
        for arr in re.findall(r"(?is)foreach t in array array\[(.*?)\]\s*loop(.*?)end loop", sql):
            if re.search(r"(?i)force row level security", arr[1]):
                forced.update(re.findall(r"'(\w+)'", arr[0]))
    for t in tables:
        t["rls"] = t["table"] in forced
        t["granted"] = t["table"] in granted
    return tables


def isolation(t: dict) -> str:
    if t["rls"]:
        return "forced row-level security"
    if t["granted"]:
        return "no row-level security: holds no tenant data, and the runtime roles are granted it directly"
    return ("no row-level security: a platform registry the runtime roles cannot read at all; they call narrow SECURITY DEFINER "
            "functions instead")


def deploy_files() -> list[str]:
    return sorted(p.relative_to(ROOT).as_posix() for p in DEPLOY.rglob("*") if p.is_file())


# ---- checks: the architecture is enforced, not just described ------------------------------------------------------------------------------
def layer_of() -> dict[str, int]:
    where: dict[str, int] = {}
    for i, (_, _, _, _, members) in enumerate(LAYERS):
        for m in members:
            if m in where:
                raise SystemExit(f"module {m!r} is placed in two layers")
            where[m] = i
    return where


def check(mods: dict, tables: list[dict]) -> list[tuple[str, str]]:
    """-> the imports that cross the layer rule (all of them recorded in EXCEPTIONS). Raises on anything that is not true any more."""
    where = layer_of()
    if set(SHORT) != {k for k, *_ in LAYERS}:
        raise SystemExit("SHORT must name every layer of LAYERS (the figure's labels)")
    missing = sorted(set(mods) - set(where))
    if missing:
        raise SystemExit(f"modules in no layer (add them to LAYERS in tools/gen_architecture.py): {', '.join(missing)}")
    gone = sorted(set(where) - set(mods))
    if gone:
        raise SystemExit(f"LAYERS lists modules that no longer exist: {', '.join(gone)}")
    upward = sorted((a, b) for a, m in mods.items() for b in m["uses"] if where[b] < where[a])
    unexplained = [e for e in upward if e not in EXCEPTIONS]
    if unexplained:
        raise SystemExit("imports that break the layer rule (a module may use only its own layer or those below it): "
                         + ", ".join(f"{a} -> {b}" for a, b in unexplained))
    stale = [e for e in EXCEPTIONS if e not in upward]
    if stale:
        raise SystemExit(f"EXCEPTIONS lists imports that no longer cross the rule (remove them): {stale}")
    names = set(mods) | {t["table"] for t in tables}
    for title, steps in FLOWS:
        for s in steps:
            for tok in re.findall(r"`([^`]+)`", s):
                if re.fullmatch(r"[a-z_]+(/[a-z_]+)?", tok) and tok not in names and tok != "sdk/python":
                    raise SystemExit(f"flow {title!r} names {tok!r}, which is neither a module nor a table")
    return upward


# ---- figures (inline SVG, coloured through the page's own CSS variables so light and dark both work) -----------------------------------------
def _t(x, y, s, cls="", anchor="start"):
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{escape(s)}</text>'


def _box(x, y, w, h, title, lines=(), cls="box"):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" class="{cls}"/>', _t(x + w / 2, y + 22, title, "bt", "middle")]
    out += [_t(x + w / 2, y + 40 + 15 * i, s, "bs", "middle") for i, s in enumerate(lines)]
    return "".join(out)


def _arrow(x1, y1, x2, y2, both=False):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="ar" marker-end="url(#ah)"'
            + (' marker-start="url(#ah)"' if both else "") + "/>")


def svg_system(n_tables: int, n_rls: int, n_migrations: int) -> str:
    W, H = 960, 440
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="TIA system context: who talks to TIA, its three processes, and what it stores">',
         '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
         '<path d="M0,0 L10,5 L0,10 z" class="ah"/></marker></defs>']
    p.append('<rect x="262" y="14" width="436" height="412" rx="12" class="frame"/>')
    p.append(_t(480, 38, "TIA: one container image, three roles", "ft", "middle"))
    p.append(_box(282, 56, 190, 104, "API server", ["python -m tia.server", "FastAPI on uvicorn :8080", "web UI · REST · MCP · /query/tia"], "box hi"))
    p.append(_box(488, 56, 190, 104, "Worker", ["python -m tia.worker", "claims queued jobs", "fetches git · health :9090"], "box hi"))
    p.append(_box(282, 196, 248, 140, "PostgreSQL 16", [f"{n_tables} tables · {n_migrations} migrations",
                                                        f"forced row-level security on {n_rls}", "runtime roles: tia_app, tia_worker",
                                                        "no role bypasses RLS"], "box db"))
    p.append(_box(546, 196, 132, 140, "repos volume", ["bare git clones", "and uploads", "/var/lib/tia/repos", "read, never run"], "box db"))
    p.append(_box(282, 360, 396, 52, "Migration job", ["python -m tia.migrate · runs before every install or upgrade"], "box"))
    # API and worker both use both stores: one shared connector line instead of crossing arrows
    p += ['<line x1="377" y1="160" x2="377" y2="178" class="ar"/>', '<line x1="583" y1="160" x2="583" y2="178" class="ar"/>',
          '<line x1="377" y1="178" x2="612" y2="178" class="ar"/>', _arrow(406, 178, 406, 194), _arrow(612, 178, 612, 194),
          _arrow(406, 360, 406, 338)]
    left = [("People", ["web UI and report page"]), ("Testing agents, assistants", ["MCP · REST · Python SDK"]),
            ("Knowledge Fabric", ["POST /query/tia"]), ("CI/CD, deploys, runtime", ["push test results, coverage,", "deployments, observed calls"])]
    for i, (t, ls) in enumerate(left):
        y = 30 + i * 100
        p.append(_box(10, y, 196, 76, t, ls, "box ext"))
        p.append(_arrow(206, y + 38, 260, y + 38))
    right = [("Git hosts", ["GitHub (App), GitLab,", "Bitbucket, Azure DevOps"]), ("Identity providers", ["OpenID Connect · SAML 2.0"]),
             ("Secrets and keys", ["secret files or Vault,", "customer KMS (BYOK)"]), ("ServiceNow, chat, e-mail", ["change records,", "team notifications"]),
             ("Telemetry collector", ["OTLP traces (optional)"])]
    for i, (t, ls) in enumerate(right):
        y = 22 + i * 82
        p.append(_box(754, y, 196, 70, t, ls, "box ext"))
        p.append(_arrow(700, y + 35, 752, y + 35, both=True))
    p.append("</svg>")
    return "".join(p)


SHORT = {"entry": "Entry points", "surface": "API, web UI, agents", "orchestration": "Orchestration and jobs", "connectors": "Connectors",
         "engine": "Impact engine, tester views", "graph": "Graph and resolution", "extraction": "Extraction", "tenancy": "Tenancy and storage",
         "rules": "Rules", "foundation": "Foundations"}


def svg_layers(mods: dict) -> str:
    W, label_w, x0, pad, chip_h = 960, 232, 244, 8, 22
    rows, y = [], 8
    for i, (key, title, colour, _, members) in enumerate(LAYERS):
        chips, cx, cy = [], x0, 0
        for m in members:
            w = int(len(m) * 7.4 + 16)
            if cx + w > W - pad:
                cx, cy = x0, cy + chip_h + 6
            chips.append((cx, cy, w, m))
            cx += w + 6
        h = cy + chip_h + 2 * pad
        n_lines = sum(mods[m]["lines"] for m in members)
        rows.append(f'<g class="layer" data-layer="{key}"><rect x="2" y="{y}" width="{W - 4}" height="{h}" rx="8" class="band"/>'
                    f'<rect x="2" y="{y}" width="8" height="{h}" rx="3" fill="{colour}"/>'
                    + _t(20, y + 19, f"{i + 1}. {SHORT[key]}", "lt") + _t(20, y + 35, f"{len(members)} modules · {n_lines:,} lines", "ls")
                    + "".join(f'<rect x="{cx}" y="{y + pad + cyy}" width="{w}" height="{chip_h}" rx="5" class="chip" stroke="{colour}"/>'
                              + _t(cx + w / 2, y + pad + cyy + 15, m, "ct", "middle") for cx, cyy, w, m in chips) + "</g>")
        y += h + 6
    return (f'<svg viewBox="0 0 {W} {y + 2}" role="img" aria-label="TIA layers, top to bottom, with every module in each layer">'
            + "".join(rows) + "</svg>")


FIG_CSS = """
.fig{margin:16px 0;padding:10px;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
.fig svg{display:block;width:100%;height:auto;min-width:640px;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.fig .frame{fill:none;stroke:var(--acc);stroke-width:1.5;stroke-dasharray:6 4}.fig .ft{fill:var(--acc);font-size:13px;font-weight:600}
.fig .box{fill:var(--card);stroke:var(--line);stroke-width:1.4}.fig .box.hi{stroke:var(--acc);stroke-width:2}.fig .box.db{fill:var(--code);stroke:var(--mut)}
.fig .box.ext{fill:var(--bg)}.fig .bt{fill:var(--fg);font-size:13px;font-weight:600}.fig .bs{fill:var(--mut);font-size:11.5px}
.fig .ar{stroke:var(--mut);stroke-width:1.4}.fig .ah{fill:var(--mut)}
.fig .band{fill:var(--bg);stroke:var(--line)}.fig .lt{fill:var(--fg);font-size:13px;font-weight:600}.fig .ls{fill:var(--mut);font-size:11.5px}
.fig .chip{fill:var(--card);stroke-width:1.4}.fig .ct{fill:var(--fg);font:12px ui-monospace,SFMono-Regular,Consolas,Menlo,monospace}
@media (max-width:700px){.fig svg{min-width:0}}
@media print{.fig{overflow:visible}.fig svg{min-width:0}}
"""


# ---- the document ------------------------------------------------------------------------------------------------------------------------
def cell(s: str) -> str:
    s = s.replace("|", "/")
    return re.sub(r"(?<!`)\b(\S*\*\S*)", lambda m: m.group(1) if "`" in m.group(1) else f"`{m.group(1)}`", s)


def mlist(names, where=None, limit=10) -> str:
    names = sorted(names)
    if not names:
        return "none"
    if len(names) <= limit or where is None:
        return ", ".join(f"`{n}`" for n in names)
    by = Counter(where[n] for n in names)
    return f"{len(names)} modules (" + ", ".join(f"layer {i + 1}: {c}" for i, c in sorted(by.items())) + ")"


def render(mods: dict, tables: list[dict], upward: list) -> str:
    where = layer_of()
    total = sum(m["lines"] for m in mods.values())
    n_rls = sum(t["rls"] for t in tables)
    n_mig = len(list(MIGRATIONS.glob("*.sql")))
    matrix = defaultdict(Counter)
    for a, m in mods.items():
        for b in m["uses"]:
            matrix[where[a]][where[b]] += 1
    L = []
    w = L.append
    w("# TIA architecture as built")
    w("")
    w(f"The system as it is coded today: {len(mods)} Python modules ({total:,} lines) in {len(LAYERS)} layers, {len(tables)} PostgreSQL "
      f"tables, three processes from one container image. Everything here is read from the code by `tools/gen_architecture.py`; the "
      "design written before the build is [ARCHITECTURE.md](ARCHITECTURE.md), and section 7 says where the two differ.")
    w("")
    w("**Status: Built.** The layer rule in section 2 is checked against the real imports on every test run "
      "(`tests/test_architecture_doc.py`), so this document cannot silently go out of date.")
    w("")
    w("---")
    w("")
    w("## 1. The system in one picture")
    w("")
    w("<!-- figure: system -->")
    w("")
    w("| Process | Command | Database role | Listens on, and does |")
    w("|---|---|---|---|")
    for row in PROCESSES:
        w("| " + " | ".join(row) + " |")
    w("")
    w("All three run the same image (`deploy/Dockerfile`, entry point `python -m`, non-root user, read-only file system). The API and the "
      "worker scale independently. They share two things only: the PostgreSQL database and the `repos` volume. Nothing else holds state.")
    w("")
    w("## 2. Layers")
    w("")
    w("<!-- figure: layers -->")
    w("")
    w("**The rule:** a module may use only modules in its own layer or in the layers below it. Requests come in at the top; data and "
      "rules sit at the bottom. So the impact engine cannot call a git host or ServiceNow (connectors are above it), and storage cannot "
      "call the engine.")
    w("")
    w("| # | Layer | Modules | Lines | Responsible for |")
    w("|---|---|---|---|---|")
    for i, (key, title, _, desc, members) in enumerate(LAYERS):
        w(f"| {i + 1} | {title} | {len(members)} | {sum(mods[m]['lines'] for m in members):,} | {desc} |")
    w("")
    w("### Imports between layers")
    w("")
    w("How many module imports go from each layer (row) to each layer (column), counted from the code. Every count is on or to the "
      "right of the diagonal (a layer using itself or a layer below it), except the recorded exceptions below.")
    w("")
    w("| From \\ to | " + " | ".join(str(i + 1) for i in range(len(LAYERS))) + " |")
    w("|---|" + "---|" * len(LAYERS))
    for i, (_, title, *_r) in enumerate(LAYERS):
        w(f"| {i + 1}. {title.split(' (')[0]} | " + " | ".join(str(matrix[i][j]) if matrix[i][j] else "·" for j in range(len(LAYERS))) + " |")
    w("")
    w("### Recorded exceptions to the rule")
    w("")
    w("| Import | Why it is allowed |")
    w("|---|---|")
    for a, b in upward:
        w(f"| `{a}` uses `{b}` (layer {where[a] + 1} to {where[b] + 1}) | {EXCEPTIONS[(a, b)]} |")
    w("")
    w("Any other import that crosses the rule stops `python tools/gen_architecture.py` and fails the test suite.")
    w("")
    w("## 3. Every module, by layer")
    w("")
    w("What each module does is its own description (the first sentence of its docstring). *Uses* and *Used by* are its imports of other "
      "TIA modules.")
    for i, (key, title, _, desc, members) in enumerate(LAYERS):
        w("")
        w(f"### 3.{i + 1} {title}")
        w("")
        w(desc)
        w("")
        w("| Module | Lines | What it does | Uses | Used by |")
        w("|---|---|---|---|---|")
        for m in members:
            d = mods[m]
            label = "tia (package root)" if m == "__init__" else m
            w(f"| [`{label}`](../{d['path']}) | {d['lines']:,} | {cell(d['doc']) or 'no description'} | {mlist(d['uses'], where)} | "
              f"{mlist(d['used_by'], where)} |")
    w("")
    w("## 4. How work flows through the layers")
    for i, (title, steps) in enumerate(FLOWS):
        w("")
        w(f"### 4.{i + 1} {title}")
        w("")
        for n, s in enumerate(steps):
            w(f"{n + 1}. {s}")
    w("")
    w("## 5. Storage")
    w("")
    w("One PostgreSQL database, shared by all tenants. Every tenant-owned row carries `tenant_id`; forced row-level security means a "
      "query sees only the rows of the tenant its transaction is bound to (`set_config('tia.tenant_id', ..., true)` in `tia/db.py`), "
      "and a transaction bound to no tenant sees nothing. The runtime roles are created without the right to bypass it.")
    w("")
    w("| Table | Created by | Tenant isolation |")
    w("|---|---|---|")
    for t in tables:
        w(f"| `{t['table']}` | `{t['migration']}` | {isolation(t)} |")
    w("")
    w("Outside the database there is one volume, `/var/lib/tia/repos`: a bare git clone per registered repository and each uploaded "
      "folder or zip turned into a git repository. Code there is read as bytes and parsed; it is never imported, executed or built.")
    w("")
    w("## 6. Deployment")
    w("")
    w("| File | What it is |")
    w("|---|---|")
    what = {"Dockerfile": "the one image: Python, TIA and its parsers, non-root, health check",
            "docker-compose.yml": "one machine: PostgreSQL on an internal network, the migration, the API and a worker",
            "README.md": "how to deploy", "Chart.yaml": "the Helm chart", "values.yaml": "default Helm values",
            "values-dedicated.yaml": "values for a dedicated single-tenant deployment",
            "values-region-eu.yaml": "values for an EU data-residency region",
            "api.yaml": "API Deployment and Service", "worker.yaml": "worker Deployment",
            "platform.yaml": "migration hook Job, repos volume claim, Ingress, NetworkPolicy",
            "_helpers.tpl": "shared labels and pod security settings", "main.tf": "Azure resources (Terraform)",
            "variables.tf": "Terraform inputs", "outputs.tf": "Terraform outputs"}
    for f in deploy_files():
        name = f.rsplit("/", 1)[-1]
        if name == "README.md" and "terraform" in f:
            desc = "how to apply the Azure Terraform"
        else:
            desc = what.get(name, "")
        w(f"| [`{f}`](../{f}) | {desc or 'see the file'} |")
    w("")
    w("Verification: the Compose stack was built and run on Docker Desktop (migration, server, worker, tenant creation, an analysis, "
      "upgrade, backup and restore). The Helm chart and the Azure Terraform are written but not deployed to a real cluster or subscription.")
    w("")
    w("## 7. Design versus build")
    w("")
    w("| Design ([ARCHITECTURE.md](ARCHITECTURE.md)) | As built | Status |")
    w("|---|---|---|")
    for row in DESIGN:
        w("| " + " | ".join(row) + " |")
    w("")
    w("## 8. Keeping this document true")
    w("")
    w("```")
    w("python tools/gen_architecture.py            # rewrite docs/ARCHITECTURE-AS-BUILT.md and .html")
    w("python tools/gen_architecture.py --check    # what the test suite runs")
    w("```")
    w("")
    w("Adding a module means placing it in a layer in `tools/gen_architecture.py`; the generator refuses to run until it is placed, and "
      "refuses an import that breaks the layer rule unless the reason is recorded there.")
    w("")
    return "\n".join(L)


def build() -> tuple[str, str]:
    mods, tables = read_modules(), read_tables()
    upward = check(mods, tables)
    md = render(mods, tables, upward)
    figs = {"system": svg_system(len(tables), sum(t["rls"] for t in tables), len(list(MIGRATIONS.glob("*.sql")))), "layers": svg_layers(mods)}
    html = gw.build(md, brand="TIA architecture", source="the code (tia/, tia/migrations/, deploy/)", generator="tools/gen_architecture.py",
                    figures=figs, extra_css=FIG_CSS, note="Do not edit by hand: change the code or the generator, then run it.")
    return md, html


def main() -> int:
    md, html = build()
    if "--check" in sys.argv:
        stale = [p.name for p, want in ((OUT_MD, md), (OUT_HTML, html)) if not p.exists() or p.read_text(encoding="utf-8") != want]
        print("up to date" if not stale else f"out of date: {', '.join(stale)}: run  python tools/gen_architecture.py")
        return 1 if stale else 0
    OUT_MD.write_text(md, encoding="utf-8", newline="\n")
    OUT_HTML.write_text(html, encoding="utf-8", newline="\n")
    print(f"wrote {OUT_MD.name} ({len(md) // 1024} KB) and {OUT_HTML.name} ({len(html) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
