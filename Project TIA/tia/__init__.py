"""TIA - Technical Impact Analysis."""

EXTRACTOR_VERSIONS = {"python": "py-2", "java": "java-3", "data": "data-1"}
EXTRACTOR_VERSION = "+".join(EXTRACTOR_VERSIONS.values())      # cache key: any adapter change re-extracts
CONFIG_VERSION = "cfg-1;sev-1;inf-1"
