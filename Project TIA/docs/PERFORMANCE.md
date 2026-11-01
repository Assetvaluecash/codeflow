# Performance (PRD section 12)

`python tools/benchmark.py --repos 500 --files 40 --prs 30 --burst 50 --workers 8 --report bench.json` generates a landscape of real git repositories (70 % Python /
30 % Java, FastAPI and Spring, each service calling three others), scans it, then measures through the production code paths and a real PostgreSQL.

## Result on the development machine (8 CPUs, one embedded PostgreSQL 16, Windows)

Landscape: **500 repositories, 19 798 files, 605 101 lines** (generated; not real customer code).

| PRD 12 requirement | Target | Measured | Met |
|---|---|---|---|
| Incremental PR analysis, graph already built | P95 <= 120 s (500 repositories) | P50 3.7 s, **P95 5.3 s**, max 5.4 s (30 analyses) | yes |
| Interactive report | P95 <= 5 s | **P95 0.07 s** (200 requests) | yes |
| Concurrent analyses / PR-comment latency | >= 50 concurrent; P95 <= 180 s from push | **50 jobs, 8 worker processes: all completed, P95 163 s, last after 167 s** | yes, with thin margin |
| Initial landscape scan | <= 4 h for 500 repositories / 20 M LOC | 500 repositories / 0.6 M LOC in **260 s** (register 86 s + extract and graph 173 s); peak 153 MB | not demonstrated at 20 M LOC |
| Determinism | same inputs, same findings | re-analysis on the evidence snapshot identical (checked on 5 in each run) | yes |
| Availability 99.9 %, RPO/RTO | proposed | not measured (needs an HA topology) | no |

## What this does and does not show

* Linear extrapolation from 0.6 M to 20 M lines would be about 2.4 h for the scan, but that is an extrapolation, not a measurement; real code has larger files, more dependencies and deeper call graphs.
* The burst number is at the edge of its target: 8 workers on 8 cores with the database on the same machine. A production deployment with more workers and a separate database should have headroom; it has not been measured.
* The landscape is small, regular and generated; findings per analysis are few. Real landscapes with heavy fan-out will cost more per analysis.
* No customer SLA, availability or recovery objective was tested.

## What the benchmark found and fixed

The first full run showed **44 s per PR analysis at 500 repositories** (burst: only 36 of 50 jobs in 15 minutes). Profiling showed it was not extraction but one `git` process per repository
to resolve revisions (tens of milliseconds each, repeated three times per repository), plus three database round trips per repository. Fixes: read `HEAD` and branch refs from the repository's
files instead of starting git; skip re-resolving a commit whose extract is already cached; resolve revisions of different repositories side by side; fetch repository records in one query.
PR analysis P95 fell from 50 s to 5.3 s and the burst went from failing to meeting the target.
