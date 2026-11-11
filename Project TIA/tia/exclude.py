"""Tenant-configurable path exclusion for extraction (Pulse exclude-patterns port).

Patterns are path-aware globs. They never invent impact; they only shrink what is analysed.
Excluded paths are recorded in coverage so an analysis stays auditable.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern]:
    return [_compile_one(p) for p in patterns if p]


@lru_cache(maxsize=512)
def _compile_one(pattern: str) -> re.Pattern:
    """Translate a glob to an anchored regex. Regular-expression metacharacters in the pattern are literal."""
    p = pattern.replace("\\", "/").strip()
    if not p:
        return re.compile(r"(?!)")  # never matches
    i, out, n = 0, [], len(p)
    while i < n:
        c = p[i]
        if c == "*" and i + 1 < n and p[i + 1] == "*":
            if i + 2 < n and p[i + 2] == "/":
                out.append("(?:.*/)?")
                i += 3
            else:
                out.append(".*")
                i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(compiled: list[re.Pattern], path: str, name: str | None = None) -> bool:
    path = path.replace("\\", "/")
    name = name or path.rsplit("/", 1)[-1]
    for rx in compiled:
        if rx.search(path) or rx.search(name):
            return True
    return False


def should_exclude(path: str, patterns: tuple | list) -> bool:
    if not patterns:
        return False
    return matches(compile_patterns(tuple(patterns)), path)
