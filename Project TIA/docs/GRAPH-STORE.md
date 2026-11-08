# Graph store: benchmark and decision

Written for architects and the platform team deciding whether TIA needs a dedicated graph database (PRD section 10: "Storage selection must be
validated against the expected workload"; ARCHITECTURE.md: "dedicated graph store only if the Release 2 benchmark shows PostgreSQL is insufficient").

## Decision

**Keep PostgreSQL as the only store, with the dependency graph held as immutable, content-addressed graph versions (JSONB) and traversed in memory
by the analysis worker.** Do not introduce a graph database for Releases 1–4. Revisit when a single tenant's landscape passes about **1 million
graph nodes**, or when a feature needs ad-hoc graph queries across graph versions that the worker cannot answer from one loaded version.

## Why

1. **The workload is not a graph-database workload.** An analysis reads one graph version (pinned in its evidence snapshot), seeds it with the
   changed elements and walks a bounded number of hops (`max_depth`, default 8). There are no ad-hoc multi-hop queries from users, no writes during
   traversal, and every graph version is immutable, so it is cached and shared between analyses.
2. **Measured traversal is milliseconds.** At every size tried, a bounded walk costs about 1–2 ms (table below). The analysis time is dominated
   by git and extraction (incremental and cached), not by the graph.
3. **The PRD targets are met with PostgreSQL** on the 500-repository benchmark (PERFORMANCE.md): PR analysis P95 5.3 s against a 120 s target,
   interactive report P95 0.07 s against 5 s, and a 50-PR burst with comment P95 163 s against 180 s.
4. **A graph database would cost more than it gives here:** a second stateful system to secure (tenant isolation would have to be rebuilt outside
   row-level security), back up, restore and keep consistent with the evidence snapshots; reproducibility (AC-015) relies on immutable versions,
   which PostgreSQL already provides.

## Measurements

In-memory graph (`tia.graph.Graph`) built synthetically with the production classes on the development machine (8 CPUs, Windows, Python 3.12):
each service has functions with 3 intra-service calls each, endpoints, and 4 consumer call sites per endpoint in other services. Traversal = the
bounded impact walk (depth 8) from 50 starting functions, averaged. JSON = the persisted graph version.

| Services | Nodes | Edges | Build | Peak memory | Traversal (avg) | Persisted JSON | Load from JSON |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 29,920 | 77,840 | 2.4 s | 43 MB | 0.5 ms | 15 MB | 1.2 s |
| 500 | 149,920 | 389,840 | 14.8 s | 212 MB | 0.6 ms | 74 MB | 5.5 s |
| 500 (4× functions) | 599,685 | 1,559,370 | 67.1 s | 851 MB | 1.8 ms | 299 MB | 18.9 s |
| 2,000 | 1,199,838 | 3,119,676 | 117 s | 1.7 GB | 1.2 ms | 604 MB | 41.0 s |

Reading the table:

* Traversal never matters. Building and loading do, and both happen **once per graph version**, not per analysis: a version is built when the
  landscape changes (the scan) and cached by the worker (`tia.graphstore`).
* The real 500-repository benchmark landscape (605,000 lines) produced a graph well inside the second row.
* The limit that would force a change is **memory and load time per worker** beyond about 1 M nodes (1.7 GB, 40 s to load a version). At that
  point the options, in order of preference, are: (a) store graph versions as compressed binary rather than JSON (estimated 4–6× smaller and faster
  to load), (b) partition by tenant landscape slice (only the repositories a change can reach), (c) a graph database.

## What is not measured

* Real code at 20 M lines (the generated landscape is 0.6 M lines); the graph size grows with functions and call sites, so the table is indexed
  by graph size, not lines of code.
* Concurrency on one graph version across many workers on separate machines (each worker loads its own copy).
* A comparison run against a graph database: none was set up, so no claim is made about how much faster one would be; the decision rests on
  the targets being met with a large margin without one.

Reproduce: `python tools/graphbench.py out.json` (this table) and `tools/benchmark.py` (the 500-repository end-to-end benchmark).
