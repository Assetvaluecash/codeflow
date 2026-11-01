"""Turn finished work into metrics: what an analysis and an extraction produced. Kept apart from metrics.py (the registry) so the engine
modules import one small function, and from the engine so a metrics bug can never change a report."""
from __future__ import annotations

from . import metrics


def record_analysis(report: dict, seconds: float) -> None:
    try:
        metrics.ANALYSES.labels(status=report["status"], change_type=report.get("change", {}).get("change_type", "unknown")).inc()
        metrics.ANALYSIS_DURATION.observe(seconds)
        if report["status"] == "failed":
            return
        for sev, n in (report.get("summary", {}).get("by_severity") or {}).items():
            if n:
                metrics.FINDINGS.labels(severity=sev).inc(n)
        for u in report.get("unknowns", []):
            metrics.UNKNOWNS.labels(area=u["area"]).inc()
            if u["area"] == "secrets_withheld":
                metrics.SECRETS_WITHHELD.inc(u.get("count", 0))
        cov = report.get("coverage", {})
        tr = cov.get("traversal", {})
        if "reached" in tr:
            metrics.TRAVERSAL_NODES.observe(tr["reached"])
        if tr.get("truncated"):
            metrics.TRUNCATED.inc()
        total, ok = cov.get("outbound_call_sites", 0), cov.get("outbound_call_sites_resolved", 0)
        if total:
            metrics.RESOLUTION.labels(result="resolved").inc(ok)
            metrics.RESOLUTION.labels(result="unresolved").inc(max(total - ok, 0))
        for strat, n in (cov.get("resolution_by_strategy") or {}).items():
            metrics.RESOLUTION_STRATEGY.labels(strategy=strat).inc(n)
    except Exception:                                                        # noqa: BLE001 - telemetry must never break an analysis
        pass


def record_extraction(stats: dict, failed_files: int, seconds: float) -> None:
    try:
        metrics.EXTRACTION_FILES.labels(outcome="parsed").inc(stats.get("parsed", 0))
        metrics.EXTRACTION_FILES.labels(outcome="reused").inc(stats.get("reused", 0))
        if failed_files:
            metrics.EXTRACTION_FILES.labels(outcome="failed").inc(failed_files)
        metrics.EXTRACTION_DURATION.observe(seconds)
    except Exception:                                                        # noqa: BLE001
        pass


def record_graph(graph) -> None:
    try:
        metrics.GRAPH_NODES.set(len(graph.nodes))
        metrics.GRAPH_EDGES.set(len(graph.edges))
    except Exception:                                                        # noqa: BLE001
        pass
