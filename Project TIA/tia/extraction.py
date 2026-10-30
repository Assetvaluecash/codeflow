"""Incremental extraction (UC1-11, AC-021): per-file facts are cached by git blob id, so a commit that changes one
file re-extracts only that file. The repository extract is assembled from per-file facts by the same function a full
rescan uses, so the incremental result equals a full rescan."""
from __future__ import annotations

from typing import Optional

from . import EXTRACTOR_VERSION, gitutil
from .extract_python import assemble, extract_source, is_extractable, is_service_config, read_service_map
from .models import RepoExtract
from .store import Store


def _file_key(repo: str, service: str, strip: tuple, blob: str, path: str) -> str:
    return f"{repo}|{service}|{','.join(strip)}|{EXTRACTOR_VERSION}|{blob}|{path}"


def extract_commit(repo: str, path: str, commit: str, store: Optional[Store], strip: tuple = (), force: bool = False):
    """Returns (RepoExtract, stats). Without a store nothing is cached and every file is parsed.
    force=True is the full-rescan path: the cache is ignored and every file fact is re-derived and stored again."""
    tree = gitutil.list_tree(path, commit)
    wanted = [p for p in tree if p in ("tia-services.yaml", "tia-services.yml") or is_service_config(p)
              or (p.startswith("contracts/") and p.endswith(".json"))]
    raw = gitutil.read_blobs(path, [tree[p] for p in wanted])
    small = {p: raw[tree[p]].decode("utf-8", "replace") for p in wanted}
    smap = read_service_map(small)
    service = smap.get("service", repo)

    py = sorted(p for p in tree if is_extractable(p))
    keys = {p: _file_key(repo, service, strip, tree[p], p) for p in py}
    have = store.get_file_extracts(list(keys.values())) if store and not force else {}
    missing = [p for p in py if keys[p] not in have]
    blobs = gitutil.read_blobs(path, sorted({tree[p] for p in missing}))
    fresh = {}
    for p in missing:
        try:
            text = blobs[tree[p]].decode("utf-8")
            part = extract_source(repo, service, p, text, strip)
        except UnicodeDecodeError:
            part = RepoExtract(repo=repo, service=service, commit="", extractor_version=EXTRACTOR_VERSION,
                               files_failed=[{"file": p, "error": "not valid UTF-8"}])
        fresh[keys[p]] = part
    if store:
        store.put_file_extracts(fresh, overwrite=force)
    per_file = {p: have.get(keys[p]) or fresh[keys[p]] for p in py}
    ex = assemble(repo, commit, list(tree), smap, service, per_file, small)
    return ex, {"files": len(py), "reused": len(py) - len(missing), "parsed": len(missing)}
