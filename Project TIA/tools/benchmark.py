"""Performance benchmark (UC1-26, PRD section 12) on a generated landscape, through the production code paths and a real PostgreSQL.

    python tools/benchmark.py --repos 500 --files 40 --prs 40 --burst 50 --workers 8 --report bench.json
    python tools/benchmark.py --repos 60 --files 20 --prs 15 --burst 20 --workers 4      # a quick smoke run

What it measures, against the PRD section 12 targets -
  initial landscape scan      wall time to register and extract every repository and build the graph (target: <= 4 h for 500 repositories / 20 M LOC)
  incremental PR analysis     end-to-end `resolve` + `execute` (graph already built, evidence cached, report persisted); P50/P95 (target: P95 <= 2 min, 500 repos)
  interactive report          GET /api/v1/impact-analyses/{id} on the real application; P50/P95 (target: P95 <= 5 s)
  concurrent analyses         a burst of N pull-request jobs through the real queue and N_WORKERS worker processes: time until the last one has
                              published, i.e. the PR-comment latency under that burst (targets: >= 50 concurrent, P95 <= 3 min from push)
  determinism                 re-running an analysis on its evidence snapshot gives an identical report
Resource use (peak resident memory of the scan and of a worker) is reported too.

HONEST LIMITS - read them with any result: the landscape is GENERATED (realistic structure, small files), it is not 20 M lines of real code; the database is a
single embedded PostgreSQL on this machine, not a tuned HA cluster; availability and recovery targets are not measured here. Scale numbers are measured at the
size you ask for and reported with the lines of code actually generated; nothing is extrapolated.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GIT = ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.longpaths=true"]
TARGETS = {"pr_p95_s": 120, "comment_p95_s": 180, "report_p95_s": 5, "scan_h_500": 4, "concurrent": 50}


def git(cwd, *a):
    r = subprocess.run([*GIT, *a], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {a}: {r.stderr[:200]}")
    return r.stdout.strip()


# ------------------------------------------------------------------------------------------------------------------------------------
# the generated landscape
# ------------------------------------------------------------------------------------------------------------------------------------
def py_service(i: int, n: int, files: int, rng: random.Random, endpoints: int = 4) -> dict:
    name = f"svc-{i:04d}"
    peers = rng.sample([j for j in range(n) if j != i], min(3, n - 1))
    out = {"tia-services.yaml": f"service: {name}\nowner: team-{i % 17}\n" + ("criticality: critical\n" if i % 11 == 0 else "") + "config_keys:\n" +
           "".join(f"  PEER_{j:04d}_URL: svc-{j:04d}\n" for j in peers)}
    eps = "".join(f'\n\n@app.get("/{name}/items{k}/{{item_id}}", response_model=Item{k})\ndef get_item_{k}(item_id: str):\n    return helper_{k}(item_id)\n' for k in range(endpoints))
    schemas = "".join(f"\n\nclass Item{k}(BaseModel):\n    id: str\n    name: str\n    qty: int\n" for k in range(endpoints))
    out["app.py"] = "from fastapi import FastAPI\nfrom pydantic import BaseModel\nfrom logic import " + ", ".join(f"helper_{k}" for k in range(endpoints)) + \
                    f"\n\napp = FastAPI(){schemas}{eps}\n"
    out["logic.py"] = "".join(f"\n\ndef helper_{k}(x):\n    return {{'id': x, 'name': 'n{k}', 'qty': {k}}}\n" for k in range(endpoints))
    calls = ""
    for j in peers:
        k = rng.randrange(endpoints)
        calls += (f'\n\ndef fetch_{j:04d}(item_id):\n    base = os.getenv("PEER_{j:04d}_URL", "http://svc-{j:04d}")\n'
                  f'    return requests.get(f"{{base}}/svc-{j:04d}/items{k}/{{item_id}}", timeout=3).json()\n')
    out["clients.py"] = "import os\nimport requests\n" + calls
    for f in range(max(0, files - 6)):
        out[f"mod{f:03d}.py"] = "".join(f"\n\ndef work_{f}_{g}(a, b):\n    total = 0\n    for i in range(a):\n        total += i * b\n    return total\n" for g in range(6))
    out["tests/test_app.py"] = "from logic import helper_0\n\n\ndef test_helper():\n    assert helper_0('1')['id'] == '1'\n"
    return out


def java_service(i: int, n: int, files: int, rng: random.Random, endpoints: int = 4) -> dict:
    name = f"svc-{i:04d}"
    peers = rng.sample([j for j in range(n) if j != i], min(3, n - 1))
    pkg = f"com/acme/s{i:04d}"
    out = {"tia-services.yaml": f"service: {name}\nowner: team-{i % 17}\n"}
    methods = "".join(f'\n    @GetMapping("/items{k}/{{id}}")\n    public Item{k} get{k}(@PathVariable String id) {{ return new Item{k}(id, "n", {k}); }}\n' for k in range(endpoints))
    out[f"src/main/java/{pkg}/Api.java"] = f"package com.acme.s{i:04d};\nimport org.springframework.web.bind.annotation.*;\n\n@RestController\n@RequestMapping(\"/{name}\")\npublic class Api {{{methods}}}\n"
    for k in range(endpoints):
        out[f"src/main/java/{pkg}/Item{k}.java"] = f"package com.acme.s{i:04d};\npublic record Item{k}(String id, String name, int qty) {{}}\n"
    calls = "".join(f'\n    public Object fetch{j:04d}(String id) {{ return rest.getForObject("http://svc-{j:04d}/svc-{j:04d}/items{rng.randrange(endpoints)}/" + id, Object.class); }}\n' for j in peers)
    out[f"src/main/java/{pkg}/Clients.java"] = f"package com.acme.s{i:04d};\nimport org.springframework.web.client.RestTemplate;\n\npublic class Clients {{\n    RestTemplate rest = new RestTemplate();{calls}}}\n"
    for f in range(max(0, files - endpoints - 2)):
        out[f"src/main/java/{pkg}/Work{f:03d}.java"] = f"package com.acme.s{i:04d};\npublic class Work{f:03d} {{\n" + "".join(
            f"    public int run{g}(int a, int b) {{ int t = 0; for (int i = 0; i < a; i++) {{ t += i * b; }} return t; }}\n" for g in range(6)) + "}\n"
    return out


def generate(root: Path, n: int, files: int, java_share: float, seed: int = 7) -> dict:
    rng = random.Random(seed)
    meta = {"repos": {}, "lines": 0, "files": 0}
    for i in range(n):
        lang = "java" if rng.random() < java_share else "python"
        tree = (java_service if lang == "java" else py_service)(i, n, files, rng)
        p = root / f"svc-{i:04d}"
        p.mkdir(parents=True)
        for rel, text in tree.items():
            f = p / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8", newline="\n")
            meta["lines"] += text.count("\n") + 1
            meta["files"] += 1
        git(p, "init", "-q", "-b", "main")
        git(p, "add", "-A")
        git(p, "commit", "-q", "-m", "initial")
        meta["repos"][f"svc-{i:04d}"] = {"path": str(p), "language": lang}
    return meta


def make_change(meta: dict, name: str, kind: str, rng: random.Random) -> str:
    """A pull-request branch; returns its name. kinds: rename (breaking), impl (behaviour), field (schema)."""
    p = Path(meta["repos"][name]["path"])
    git(p, "checkout", "-q", "-b", f"pr-{kind}", "main")
    lang = meta["repos"][name]["language"]
    if lang == "python":
        f = p / "app.py"
        t = f.read_text()
        t = {"rename": t.replace("/items0/", "/items0v2/"), "impl": t, "field": t.replace("    qty: int\n", "    qty: int\n    extra: str = ''\n", 1)}[kind]
        if kind == "impl":
            g = p / "logic.py"
            g.write_text(g.read_text().replace("'n0'", "'changed'"))
        f.write_text(t)
    else:
        f = next(p.glob("src/main/java/**/Api.java"))
        t = f.read_text()
        t = {"rename": t.replace('"/items0/{id}"', '"/items0v2/{id}"'), "impl": t.replace('"n", 0)', '"changed", 0)'), "field": t}[kind]
        f.write_text(t)
        if kind == "field":
            r = next(p.glob("src/main/java/**/Item0.java"))
            r.write_text(r.read_text().replace("int qty", "int qty, String extra"))
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", kind, "--allow-empty")
    git(p, "checkout", "-q", "main")
    return f"pr-{kind}"


# ------------------------------------------------------------------------------------------------------------------------------------
def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1048576
    except Exception:
        return float("nan")


def worker_proc(wdsn: str, workspace: str, stop_at: float):
    from tia.analysis_service import make_handlers
    from tia.db import Database
    from tia.jobs import JobQueue
    from tia.worker import Worker
    db = Database(wdsn, max_size=4)
    w = Worker(JobQueue(db), make_handlers(db, allow_local=True, workspace=workspace), base_delay_s=0)
    idle = 0
    while time.time() < stop_at and idle < 3:
        idle = 0 if w.run_once() else idle + 1
        if idle:
            time.sleep(0.2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repos", type=int, default=100)
    ap.add_argument("--files", type=int, default=30, help="files per repository")
    ap.add_argument("--java-share", type=float, default=0.3)
    ap.add_argument("--prs", type=int, default=30, help="incremental PR analyses to time")
    ap.add_argument("--burst", type=int, default=50, help="concurrent pull-request jobs in the burst")
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4), help="worker processes for the burst")
    ap.add_argument("--report-calls", type=int, default=200)
    ap.add_argument("--report")
    ap.add_argument("--keep", help="keep the generated data in this directory")
    a = ap.parse_args(argv)

    import pgserver
    import psycopg.conninfo as ci
    from fastapi.testclient import TestClient
    from tia.analysis_service import execute, refresh_landscape, reproduce, resolve
    from tia.api import create_app
    from tia.db import Database, TenantAdmin, migrate
    from tia.jobs import JobQueue
    from tia.repos import RepoService
    from tia.store import Store

    work = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="tia-bench-"))
    work.mkdir(parents=True, exist_ok=True)
    res: dict = {"parameters": vars(a), "machine": {"cpus": os.cpu_count()}}
    t0 = time.perf_counter()
    meta = generate(work / "landscape", a.repos, a.files, a.java_share)
    res["landscape"] = {"repositories": a.repos, "files": meta["files"], "lines_of_code": meta["lines"], "generated_in_s": round(time.perf_counter() - t0, 1),
                        "java_repositories": sum(1 for r in meta["repos"].values() if r["language"] == "java")}
    print(f"generated {a.repos} repositories, {meta['files']} files, {meta['lines']} lines in {res['landscape']['generated_in_s']} s", flush=True)

    srv = pgserver.get_server(str(work / "pg"), cleanup_mode="stop")
    try:
        admin = srv.get_uri()
        migrate(admin, "bench-pw")
        info = ci.conninfo_to_dict(admin)
        app_dsn = ci.make_conninfo(**{**info, "user": "tia_app", "password": "bench-pw"})
        wk_dsn = ci.make_conninfo(**{**info, "user": "tia_worker", "password": "bench-pw"})
        tenant = TenantAdmin(admin).ensure("bench")
        db = Database(app_dsn, max_size=10)
        store = Store(db, tenant)
        ws = work / "ws"
        svc = RepoService(store, str(ws), allow_local=True)

        # ---- initial scan -------------------------------------------------------------------------------------------------------------
        t0 = time.perf_counter()
        for name, m in meta["repos"].items():
            svc.register(path=m["path"], name=name)
        reg = time.perf_counter() - t0
        t1 = time.perf_counter()
        refresh_landscape(svc, store)
        scan = time.perf_counter() - t1
        res["initial_scan"] = {"register_s": round(reg, 1), "extract_and_graph_s": round(scan, 1), "total_s": round(reg + scan, 1),
                               "per_repository_ms": round(1000 * (reg + scan) / a.repos, 1), "peak_rss_mb": round(rss_mb(), 0),
                               "target": "<= 4 h for 500 repositories / 20 M LOC (proposed)"}
        print(f"initial scan: {res['initial_scan']}", flush=True)

        # ---- incremental PR analyses (graph built, evidence cached) ----------------------------------------------------------------
        rng = random.Random(11)
        names = sorted(meta["repos"])
        kinds = ["rename", "impl", "field"]
        prs = []
        for k in range(a.prs):
            nm = rng.choice(names)
            kind = kinds[k % 3]
            try:
                prs.append((nm, make_change(meta, nm, kind, rng), kind))
            except RuntimeError:                                           # the same branch name twice in one repository
                continue
        lat, findings, reports = [], [], []
        cfg = store.effective_config()
        for nm, br, kind in prs:
            t = time.perf_counter()
            c = resolve(svc, {"target": nm, "head": br, "base": "main"}, False, cfg)
            rep = execute(svc, store, c)
            lat.append(time.perf_counter() - t)
            findings.append(len(rep["findings"]))
            reports.append(rep["analysis_id"])
        res["incremental_pr_analysis"] = {"count": len(lat), "p50_s": round(pct(lat, .5), 2), "p95_s": round(pct(lat, .95), 2), "max_s": round(max(lat), 2),
                                          "findings_median": statistics.median(findings), "target": "P95 <= 120 s on 500 repositories",
                                          "met_at_this_size": pct(lat, .95) <= TARGETS["pr_p95_s"]}
        print(f"incremental PR analysis: {res['incremental_pr_analysis']}", flush=True)

        # ---- interactive report (the real application) ------------------------------------------------------------------------------
        app = create_app(db, tenant, str(ws), allow_local=True)
        c = TestClient(app)
        lat_r = []
        for k in range(a.report_calls):
            aid = reports[k % len(reports)]
            t = time.perf_counter()
            r = c.get(f"/api/v1/impact-analyses/{aid}")
            lat_r.append(time.perf_counter() - t)
            assert r.status_code == 200
        res["interactive_report"] = {"calls": len(lat_r), "p50_s": round(pct(lat_r, .5), 3), "p95_s": round(pct(lat_r, .95), 3),
                                     "target": "P95 <= 5 s", "met": pct(lat_r, .95) <= TARGETS["report_p95_s"]}
        print(f"interactive report: {res['interactive_report']}", flush=True)

        # ---- determinism -------------------------------------------------------------------------------------------------------------
        same = sum(1 for aid in reports[:5] if reproduce(svc, store, aid)["identical"])
        res["determinism"] = {"checked": min(5, len(reports)), "identical": same}

        # ---- a burst of concurrent pull-request jobs through the real queue and worker processes ---------------------------------------
        jq = JobQueue(db)
        burst = []
        for k in range(a.burst):
            nm = names[(k * 7) % len(names)]
            br = None
            for kind in kinds:
                br = f"pr-{kind}"
            try:
                git(meta["repos"][nm]["path"], "rev-parse", "--verify", "-q", "pr-impl")
                br = "pr-impl"
            except RuntimeError:
                br = make_change(meta, nm, "impl", rng)
            burst.append((nm, br))
        t_enq = time.time()
        ids = []
        for k, (nm, br) in enumerate(burst):
            conc = resolve(svc, {"target": nm, "head": br, "base": "main", "max_depth": 4 + (k % 3)}, False, cfg)
            from tia.analysis_service import group_key, job_key
            j = jq.enqueue(tenant, "impact_analysis", conc, job_key(conc) + str(k), group_key(conc) + str(k))
            ids.append(j["job_id"])
        procs = [mp.Process(target=worker_proc, args=(wk_dsn, str(ws), time.time() + 900)) for _ in range(a.workers)]
        for p in procs:
            p.start()
        done_at: dict = {}
        deadline = time.time() + 900
        while len(done_at) < len(ids) and time.time() < deadline:
            for jid in ids:
                if jid not in done_at:
                    st = jq.get(tenant, jid)
                    if st and st["status"] in ("succeeded", "failed"):
                        done_at[jid] = (time.time() - t_enq, st["status"])
            time.sleep(0.25)
        for p in procs:
            p.join(timeout=30)
            if p.is_alive():
                p.terminate()
        lats = [v[0] for v in done_at.values()]
        res["concurrent_burst"] = {"jobs": len(ids), "completed": sum(1 for v in done_at.values() if v[1] == "succeeded"), "failed": sum(1 for v in done_at.values() if v[1] == "failed"),
                                   "workers": a.workers, "last_completed_after_s": round(max(lats), 1) if lats else None, "p50_s": round(pct(lats, .5), 1) if lats else None,
                                   "p95_s": round(pct(lats, .95), 1) if lats else None, "target": ">= 50 concurrent; PR comment P95 <= 180 s from push",
                                   "met_at_this_size": bool(lats) and len(done_at) == len(ids) and a.burst >= TARGETS["concurrent"] and pct(lats, .95) <= TARGETS["comment_p95_s"]}
        print(f"concurrent burst: {res['concurrent_burst']}", flush=True)
        res["note"] = ("Generated landscape and a single embedded PostgreSQL on this machine; see the module docstring for what this does and does not show. "
                       "The 20 M LOC scan target is not demonstrated: the lines of code actually generated are in landscape.lines_of_code.")
    finally:
        srv.cleanup()
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)
    if a.report:
        Path(a.report).write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
