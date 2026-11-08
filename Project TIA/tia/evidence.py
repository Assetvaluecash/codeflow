"""Ingested evidence (PRD FR-022 CI results, FR-023 coverage, FR-026/027 runtime, FR-028/029 deployments): parsers and the tenant store.

Evidence is pushed to TIA (by a CI step, a telemetry export, a deployment pipeline) and kept in `evidence_records`. An analysis reads it
from there and records which records it used, so it never depends on an external system being reachable and its reproduction reads
the same evidence. Every parser is defensive: size limits, no DTDs or entities in XML, credential-shaped values withheld.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from psycopg.types.json import Jsonb

from .models import normalize_path
from .scrub import scrub_json

KINDS = ("test_run", "coverage", "runtime_edge", "deployment", "incident", "verdict", "approval", "acknowledgement", "notification",
         "itsm_attachment", "release", "incident_link")
MAX_BYTES = 25 * 1024 * 1024
MAX_TESTS = 200_000
RESULTS = ("passed", "failed", "error", "skipped")


class EvidenceError(ValueError):
    pass


def _xml(text: str):
    if len(text) > MAX_BYTES:
        raise EvidenceError(f"larger than {MAX_BYTES // 1048576} MB")
    head = text[:4096].lower()
    if "<!doctype" in head or "<!entity" in text.lower():
        raise EvidenceError("XML with a DOCTYPE or entities is refused")
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise EvidenceError(f"not valid XML: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(v) -> Optional[str]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1e9 if v > 1e14 else v, timezone.utc).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        raise EvidenceError(f"not a timestamp: {str(v)[:40]}")


# ---- CI test results (FR-022) ---------------------------------------------------------------------------------------------------------
def parse_junit(text: str) -> list:
    """JUnit XML (JUnit, Surefire, pytest --junitxml, most CI tools) -> [{name, classname, file, result, duration_s, message}]."""
    root = _xml(text)
    out = []
    for tc in root.iter("testcase"):
        res = "passed"
        msg = None
        for child in tc:
            tag = child.tag.split("}")[-1]
            if tag in ("failure", "error"):
                res, msg = ("failed" if tag == "failure" else "error"), (child.get("message") or (child.text or ""))[:300]
                break
            if tag == "skipped":
                res, msg = "skipped", (child.get("message") or "")[:300]
        try:
            dur = round(float(tc.get("time") or 0), 3)
        except ValueError:
            dur = None
        out.append({"name": tc.get("name") or "", "classname": tc.get("classname") or "", "file": tc.get("file"), "result": res,
                    "duration_s": dur, **({"message": msg} if msg else {})})
        if len(out) > MAX_TESTS:
            raise EvidenceError(f"more than {MAX_TESTS} test cases")
    if not out:
        raise EvidenceError("no <testcase> elements found")
    return out


def parse_test_json(text: str) -> list:
    """pytest-json-report ({"tests": [{"nodeid", "outcome", "duration"}]}) or a plain list of {name, classname, file, result, duration_s}."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise EvidenceError(f"not valid JSON: {e}")
    items = data.get("tests") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise EvidenceError("expected a list of tests or {\"tests\": [...]}")
    out = []
    for t in items[:MAX_TESTS]:
        if not isinstance(t, dict):
            continue
        if "nodeid" in t:
            file, _, rest = t["nodeid"].partition("::")
            parts = rest.split("::")
            res = {"passed": "passed", "failed": "failed", "error": "error", "skipped": "skipped", "xfailed": "skipped", "xpassed": "passed"}.get(t.get("outcome"), "error")
            out.append({"name": parts[-1].split("[")[0], "classname": ".".join([file[:-3].replace("/", ".")] + parts[:-1]) if file.endswith(".py") else file,
                        "file": file, "result": res, "duration_s": t.get("duration") or (t.get("call") or {}).get("duration")})
        else:
            if t.get("result") not in RESULTS:
                raise EvidenceError(f"result must be one of {RESULTS}")
            out.append({"name": str(t.get("name", "")), "classname": str(t.get("classname", "")), "file": t.get("file"), "result": t["result"],
                        "duration_s": t.get("duration_s")})
    if not out:
        raise EvidenceError("no tests found")
    return out


def test_keys(t: dict) -> set:
    """Identities a CI result can be matched by: name + module (pytest: tests.test_x / tests/test_x.py) or name + class (JUnit)."""
    name, cls, file = t.get("name") or "", t.get("classname") or "", t.get("file") or ""
    keys = {f"{cls}.{name}".lower()}
    if file:
        keys.add(f"{file[:-3].replace('/', '.') if file.endswith('.py') else file}.{name}".lower())
    if cls:
        keys.add(f"{cls.rsplit('.', 1)[-1]}.{name}".lower())
    return keys


def tia_test_keys(file: str, name: str, qualname: Optional[str] = None) -> set:
    """The identities of a test TIA extracted (repository file + function), comparable with test_keys()."""
    mod = file[:-3].replace("/", ".") if file.endswith(".py") else file
    keys = {f"{mod}.{name}".lower()}
    if file.endswith(".java"):
        cls = file.rsplit("/", 1)[-1][:-5]
        keys.add(f"{cls}.{name}".lower())
        m = re.search(r"src/test/java/(.+)\.java$", file)
        if m:
            keys.add(f"{m.group(1).replace('/', '.')}.{name}".lower())
    if qualname and "." in qualname:
        keys.add(f"{mod}.{qualname}".lower())
        keys.add(qualname.lower())
    return keys


# ---- coverage (FR-023) ------------------------------------------------------------------------------------------------------------------
def _ranges(lines) -> list:
    lines = sorted({int(x) for x in lines})
    out = []
    for n in lines:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def in_ranges(ranges: list, lo: int, hi: int) -> bool:
    return any(a <= hi and b >= lo for a, b in ranges)


def parse_coverage(text: str, fmt: Optional[str] = None) -> dict:
    """Cobertura XML (coverage.py `coverage xml`, most tools), JaCoCo XML, or coverage.py JSON (`coverage json --show-contexts`, which
    also says WHICH test executed each line) -> {"format", "files": {path: {"covered": ranges, "lines": n}}, "per_test": {test: {path: ranges}}}."""
    t = text.lstrip()
    if fmt == "coveragepy-json" or (fmt is None and t.startswith("{")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise EvidenceError(f"not valid JSON: {e}")
        files, per_test = {}, {}
        for path, f in (data.get("files") or {}).items():
            ex = f.get("executed_lines") or []
            files[path] = {"covered": _ranges(ex), "lines": len(ex) + len(f.get("missing_lines") or [])}
            for line, ctxs in (f.get("contexts") or {}).items():
                for ctx in ctxs:
                    test = ctx.split("|", 1)[0]
                    if test:
                        per_test.setdefault(test, {}).setdefault(path, []).append(int(line))
        if not files:
            raise EvidenceError("no files in the coverage report")
        return {"format": "coveragepy-json", "files": files,
                "per_test": {t: {p: _ranges(ls) for p, ls in fs.items()} for t, fs in per_test.items()}}
    root = _xml(text)
    files = {}
    if root.tag == "report":                                             # JaCoCo
        for pkg in root.iter("package"):
            for sf in pkg.iter("sourcefile"):
                path = f"{pkg.get('name')}/{sf.get('name')}".lstrip("/")
                cov = [int(ln.get("nr")) for ln in sf.iter("line") if int(ln.get("ci") or 0) > 0]
                files[path] = {"covered": _ranges(cov), "lines": sum(1 for _ in sf.iter("line"))}
        fmt_ = "jacoco"
    elif root.tag == "coverage":                                          # Cobertura
        for cl in root.iter("class"):
            path = cl.get("filename") or ""
            lines = list(cl.iter("line"))
            cov = [int(ln.get("number")) for ln in lines if int(ln.get("hits") or 0) > 0]
            f = files.setdefault(path, {"covered": [], "lines": 0})
            f["covered"] = _ranges([n for a, b in f["covered"] for n in range(a, b + 1)] + cov)
            f["lines"] += len(lines)
        fmt_ = "cobertura"
    else:
        raise EvidenceError("not a Cobertura, JaCoCo or coverage.py JSON report")
    if not files:
        raise EvidenceError("no files in the coverage report")
    return {"format": fmt_, "files": files, "per_test": {}}


def match_file(cov_files: dict, repo_file: str) -> Optional[str]:
    """The coverage entry for a repository file: coverage paths may be absolute, or relative to another root (JaCoCo: package path)."""
    if repo_file in cov_files:
        return repo_file
    best = None
    for p in cov_files:
        q = p.replace("\\", "/")
        if q.endswith("/" + repo_file) or repo_file.endswith("/" + q) or (repo_file.endswith(q) and "/" in q):
            if best is None or len(q) > len(best):
                best = p
    return best


# ---- runtime dependencies (FR-026, FR-027) ------------------------------------------------------------------------------------------------
def _attrs(lst) -> dict:
    out = {}
    for a in lst or []:
        v = a.get("value") or {}
        out[a.get("key")] = next((v[k] for k in ("stringValue", "intValue", "boolValue", "doubleValue") if k in v), None)
    return out


SERVER_KINDS = {2, "SPAN_KIND_SERVER", "SERVER"}


def parse_otlp(text: str) -> list:
    """OpenTelemetry traces in OTLP/JSON -> observed service-to-service calls: a SERVER span whose parent span belongs to another service is
    one call from that service to the server's route. Aggregated per (caller, callee, method, route, environment) with the time window."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise EvidenceError(f"not valid JSON: {e}")
    spans = {}
    for rs in data.get("resourceSpans") or []:
        res = _attrs((rs.get("resource") or {}).get("attributes"))
        svc = res.get("service.name")
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans") or []:
                spans[sp.get("spanId")] = {"service": svc, "env": res.get("deployment.environment.name") or res.get("deployment.environment"),
                                           "version": res.get("service.version"), "kind": sp.get("kind"), "parent": sp.get("parentSpanId"),
                                           "start": int(sp.get("startTimeUnixNano") or 0), "end": int(sp.get("endTimeUnixNano") or 0),
                                           "attrs": _attrs(sp.get("attributes"))}
    if not spans:
        raise EvidenceError("no spans found (expected OTLP/JSON resourceSpans)")
    agg = {}
    for s in spans.values():
        p = spans.get(s["parent"])
        if s["kind"] not in SERVER_KINDS or not p or not p["service"] or p["service"] == s["service"]:
            continue
        a = s["attrs"]
        route = a.get("http.route") or a.get("url.path") or a.get("http.target") or ""
        method = (a.get("http.request.method") or a.get("http.method") or "").upper()
        if not route.startswith("/") or not method:
            continue
        key = (p["service"], s["service"], method, normalize_path(route), s["env"] or p["env"])
        e = agg.setdefault(key, {"from_service": key[0], "to_service": key[1], "method": method, "path": normalize_path(route), "route": route,
                                 "environment": key[4], "count": 0, "from_ts": s["start"], "to_ts": s["end"],
                                 "from_versions": set(), "to_versions": set()})
        e["count"] += 1
        e["from_ts"], e["to_ts"] = min(e["from_ts"], s["start"]), max(e["to_ts"], s["end"])
        if p["version"]:
            e["from_versions"].add(str(p["version"]))
        if s["version"]:
            e["to_versions"].add(str(s["version"]))
    return [{**e, "from_ts": _iso(e["from_ts"]), "to_ts": _iso(e["to_ts"]), "from_versions": sorted(e["from_versions"]),
             "to_versions": sorted(e["to_versions"])} for e in agg.values()]


def parse_edges(items) -> list:
    """Runtime edges already aggregated by an exporter: [{from_service, to_service, method, path, environment, from_ts, to_ts, count}]."""
    if not isinstance(items, list) or not items:
        raise EvidenceError("expected a non-empty list of edges")
    out = []
    for e in items:
        if not isinstance(e, dict) or not all(e.get(k) for k in ("from_service", "to_service", "method", "path")):
            raise EvidenceError("each edge needs from_service, to_service, method and path")
        out.append({"from_service": str(e["from_service"]), "to_service": str(e["to_service"]), "method": str(e["method"]).upper(),
                    "path": normalize_path(str(e["path"])), "route": str(e["path"]), "environment": e.get("environment"),
                    "count": int(e.get("count") or 1), "from_ts": _iso(e.get("from_ts")), "to_ts": _iso(e.get("to_ts")),
                    "from_versions": list(e.get("from_versions") or []), "to_versions": list(e.get("to_versions") or [])})
    return out


# ---- deployments (FR-028) ------------------------------------------------------------------------------------------------------------------
def parse_deployments(items) -> list:
    if not isinstance(items, list) or not items:
        raise EvidenceError("expected a non-empty list of deployments")
    out = []
    for d in items:
        if not isinstance(d, dict) or not (d.get("repo") or d.get("service")) or not d.get("environment") or not d.get("revision"):
            raise EvidenceError("each deployment needs repo or service, environment and revision")
        rev = str(d["revision"]).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,40}", rev):
            raise EvidenceError("revision must be a commit id (7 to 40 hex characters)")
        out.append({"repo": d.get("repo"), "service": d.get("service"), "environment": str(d["environment"]).strip().lower(), "revision": rev,
                    "artifact": d.get("artifact"), "deployed_at": _iso(d.get("deployed_at")) or _now(), "pipeline": d.get("pipeline")})
    return out


# ---- the store ---------------------------------------------------------------------------------------------------------------------------
def digest_of(kind: str, data) -> str:
    return hashlib.sha256(json.dumps([kind, data], sort_keys=True, default=str).encode()).hexdigest()


class EvidenceStore:
    def __init__(self, db, tenant_id: str):
        self.db, self.tenant_id = db, str(tenant_id)

    def put(self, kind: str, source: str, data: dict, repo=None, service=None, environment=None, revision=None,
            observed_from=None, observed_to=None, actor=None) -> dict:
        """-> {"id", "created"}; the same evidence twice is one record."""
        if kind not in KINDS:
            raise EvidenceError(f"unknown evidence kind {kind}")
        data, _ = scrub_json(data)
        dg = digest_of(kind, [source, repo, service, environment, revision, data])
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("insert into evidence_records (tenant_id, kind, source, repo, service, environment, revision, observed_from, observed_to, "
                          "data, digest, ingested_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (tenant_id, kind, digest) do nothing "
                          "returning id", (self.tenant_id, kind, source[:200], repo, service, environment, revision, observed_from, observed_to,
                                           Jsonb(data), dg, actor)).fetchone()
            if r:
                return {"id": r[0], "created": True}
            r = c.execute("select id from evidence_records where kind = %s and digest = %s", (kind, dg)).fetchone()
            return {"id": r[0], "created": False}

    def _rows(self, where: str, args: tuple, limit: int = 500) -> list:
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select id, kind, source, repo, service, environment, revision, observed_from, observed_to, data, ingested_at "
                             f"from evidence_records where {where} order by observed_to desc nulls last, id desc limit %s", (*args, limit)).fetchall()
        return [{"id": r[0], "kind": r[1], "source": r[2], "repo": r[3], "service": r[4], "environment": r[5], "revision": r[6],
                 "observed_from": r[7].isoformat() if r[7] else None, "observed_to": r[8].isoformat() if r[8] else None, "data": r[9],
                 "ingested_at": r[10].isoformat()} for r in rows]

    def list(self, kind: str, repo: Optional[str] = None, service: Optional[str] = None, environment: Optional[str] = None,
             limit: int = 500) -> list:
        where, args = "kind = %s", [kind]
        for col, v in (("repo", repo), ("service", service), ("environment", environment)):
            if v is not None:
                where += f" and {col} = %s"
                args.append(v)
        return self._rows(where, tuple(args), limit)

    def get_many(self, ids: list) -> list:
        return self._rows("id = any(%s)", (list(ids),), max(len(ids), 1)) if ids else []

    def delete(self, rid: int) -> bool:
        with self.db.tx(self.tenant_id) as c:
            return c.execute("delete from evidence_records where id = %s", (rid,)).rowcount == 1

    def for_analysis(self, repos, target: str = None, head: str = None) -> dict:
        """The evidence an analysis of these repositories uses: per repository its most recent test runs and coverage reports, and the
        tenant's runtime observations and deployments (matched to services during the analysis). -> {kind: [records]}; their ids go into
        the evidence snapshot, so a reproduction reads exactly these records."""
        names = sorted(repos)
        return {"test_run": self._rows("kind = 'test_run' and repo = any(%s)", (names,), 50 * max(len(names), 1)),
                "coverage": self._rows("kind = 'coverage' and repo = any(%s)", (names,), 10 * max(len(names), 1)),
                "runtime_edge": self._rows("kind = 'runtime_edge'", (), 5000),
                "deployment": self._rows("kind = 'deployment'", (), 5000),
                "approval": self._rows("kind = 'approval' and repo = %s and revision = %s", (target, head), 500) if target and head else []}

    def approvals(self, repo: str, revision: str) -> list:
        """Owner approvals recorded for one proposed revision of a repository (FR-033 owner approval)."""
        return [{**r["data"], "evidence_id": r["id"]} for r in self._rows("kind = 'approval' and repo = %s and revision = %s", (repo, revision), 500)]

    def pinned(self, ids_by_kind: dict) -> dict:
        """The records an earlier analysis used, by kind (a record deleted since then is simply absent: the reproduction reports a difference)."""
        rows = self.get_many(sorted({i for ids in ids_by_kind.values() for i in ids}))
        return {k: [r for r in rows if r["kind"] == k] for k in ids_by_kind}
