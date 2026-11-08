"""TIA - Technical Impact Analysis."""

__version__ = "1.0.0"

from .adapters import versions as _adapter_versions

# python/java/data are built into tia.extract_*; the rest come from the adapter registry (built-in Go, C#, Kotlin, TypeScript adapters
# plus any installed third-party adapter), so installing or upgrading an adapter changes the cache key and re-extracts.
EXTRACTOR_VERSIONS = {"python": "py-5", "java": "java-6", "data": "data-2", **_adapter_versions()}
EXTRACTOR_VERSION = "+".join(EXTRACTOR_VERSIONS.values())      # cache key: any adapter change re-extracts
CONFIG_VERSION = "cfg-1;sev-1;inf-1"
