"""Language adapter registry (PRD FR-040, adapter SDK).

An adapter turns one source file into a partial RepoExtract. Built-in adapters: Go, C#/.NET, Kotlin, TypeScript/JavaScript (Python and Java
are handled by tia.extract_python / tia.extract_java directly). Third-party adapters are installed packages that declare an entry point in
the group "tia.adapters" whose object is an Adapter (see docs/ADAPTER-SDK.md). A third-party adapter may not take over an extension a
built-in adapter handles, and its version takes part in the extraction cache key, so upgrading it re-extracts affected files.

An adapter that raises is contained: the file is reported in files_failed with the adapter's name, never silently dropped, and the rest of
the repository is still analysed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("tia.adapters")
GROUP = "tia.adapters"


@dataclass(frozen=True)
class Adapter:
    name: str                               # short id, e.g. "go"
    version: str                            # e.g. "go-1"; part of EXTRACTOR_VERSIONS
    extensions: tuple                       # lower-case file suffixes, e.g. (".go",)
    extract_file: Callable                  # (repo, service, path, text, strip) -> RepoExtract
    accepts: Optional[Callable] = None      # optional (path) -> bool refinement (e.g. skip generated files)
    frameworks: tuple = field(default=())   # documentation only: what the adapter recognises

    def handles(self, path: str) -> bool:
        p = path.lower()
        return p.endswith(self.extensions) and (self.accepts is None or self.accepts(path))


GENERATED = ("/node_modules/", "/vendor/", "/bin/", "/obj/", "/dist/", "/build/", "/.next/", "/coverage/", "/target/")


def _not_generated(path: str) -> bool:
    p = "/" + path.lower()
    return not any(d in p for d in GENERATED) and ".min." not in p and ".bundle." not in p and not p.endswith((".d.ts", ".designer.cs", ".g.cs", "_pb.go", ".pb.go"))


def _go(*a):
    from .lang.golang import extract_file
    return extract_file(*a)


def _cs(*a):
    from .lang.csharp import extract_file
    return extract_file(*a)


def _kt(*a):
    from .lang.kotlin import extract_file
    return extract_file(*a)


def _ts(*a):
    from .lang.typescript import extract_file
    return extract_file(*a)


BUILTIN = (
    Adapter("go", "go-1", (".go",), _go, _not_generated, ("net/http", "chi", "gin", "echo", "gorilla/mux", "httptest")),
    Adapter("csharp", "cs-1", (".cs",), _cs, _not_generated, ("ASP.NET Core controllers", "minimal APIs", "HttpClient", "DataAnnotations",
                                                              "xUnit", "NUnit", "MSTest")),
    Adapter("kotlin", "kt-1", (".kt", ".kts"), _kt, _not_generated, ("Spring", "RestTemplate", "WebClient", "Bean Validation", "MockMvc")),
    Adapter("typescript", "ts-1", (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"), _ts, _not_generated,
            ("Express", "NestJS", "fetch", "axios", "class-validator", "jest", "mocha", "supertest")),
)
RESERVED = (".py", ".java")
_registry: Optional[tuple] = None


def _discover() -> list:
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group=GROUP)
    except Exception:                       # noqa: BLE001 - discovery must never break extraction
        return []
    found = []
    for ep in eps:
        try:
            a = ep.load()
        except Exception as e:              # noqa: BLE001
            log.warning("adapter %s could not be loaded: %s", ep.name, e)
            continue
        if not isinstance(a, Adapter):
            log.warning("adapter %s is not a tia.adapters.Adapter; ignored", ep.name)
            continue
        found.append(a)
    return found


def registry(refresh: bool = False) -> tuple:
    """Built-in adapters first, then valid third-party ones whose names and extensions do not clash."""
    global _registry
    if _registry is None or refresh:
        taken = set(RESERVED) | {x for a in BUILTIN for x in a.extensions}
        names = {a.name for a in BUILTIN} | {"python", "java", "data"}
        extra = []
        for a in sorted(_discover(), key=lambda a: a.name):
            clash = set(a.extensions) & taken
            if a.name in names or clash:
                log.warning("adapter %s ignored: name or extensions %s already handled", a.name, sorted(clash))
                continue
            taken |= set(a.extensions)
            names.add(a.name)
            extra.append(a)
        _registry = BUILTIN + tuple(extra)
    return _registry


def adapter_for(path: str) -> Optional[Adapter]:
    return next((a for a in registry() if a.handles(path)), None)


def versions() -> dict:
    return {a.name: a.version for a in registry()}


def run(adapter: Adapter, repo: str, service: str, path: str, text: str, strip: tuple):
    """The adapter's partial extract; an adapter failure is reported for the file rather than raised."""
    from .models import RepoExtract
    try:
        return adapter.extract_file(repo, service, path, text, strip)
    except Exception as e:                  # noqa: BLE001 - contained per file (see module docstring)
        out = RepoExtract(repo=repo, service=service, commit="", extractor_version="")
        out.files_failed.append({"file": path, "error": f"{adapter.name} adapter: {type(e).__name__}: {str(e)[:160]}"})
        return out
