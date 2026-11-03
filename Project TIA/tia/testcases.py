"""Written (manual) test cases kept in a repository: testcases/*.csv | *.yaml | *.yml | *.json (UC-2, docs/CASE-STUDY.md).

CSV is what test-management tools export; the column names are matched loosely (ID / Test Case ID / Key, Title / Summary / Name,
Steps / Description / Procedure, Expected / Expected Result, Entry point / Endpoint / API / Screen / Page, Priority, Type). YAML and
JSON hold a list of objects with the same keys (or {"test_cases": [...]}). Nothing is executed; the text is matched against the code's
technical aspects by tia.coverage.
"""
from __future__ import annotations

import csv
import io
import json
import re

import yaml

TESTCASE_DIRS = ("testcases/", "test-cases/", "test_cases/")
TESTCASE_EXT = (".csv", ".yaml", ".yml", ".json")
COLUMNS = {
    "id": ("id", "test case id", "testcase id", "case id", "key", "tc id", "test id"),
    "title": ("title", "summary", "name", "test case", "test case name"),
    "steps": ("steps", "step", "description", "procedure", "test steps", "action", "actions"),
    "expected": ("expected", "expected result", "expected results", "expected outcome"),
    "entry_point": ("entry point", "entry_point", "endpoint", "api", "screen", "page", "feature"),
    "priority": ("priority",),
    "type": ("type", "test type", "category"),
}
MAX_CASES = 5000


def is_testcase_file(path: str) -> bool:
    p = "/" + path.lower()
    return p.endswith(TESTCASE_EXT) and any("/" + d in p for d in TESTCASE_DIRS)


def _norm_key(k) -> str:
    return re.sub(r"[\s_]+", " ", str(k or "")).strip().lower()


def _row(raw: dict, file: str, line: int, n: int) -> dict:
    keys = {_norm_key(k): v for k, v in raw.items()}
    out = {"file": file, "line": line}
    for field, names in COLUMNS.items():
        v = next((keys[x] for x in names if x in keys and keys[x] not in (None, "")), None)
        if isinstance(v, list):
            v = "\n".join(str(x) for x in v)
        out[field] = str(v).strip() if v is not None else ""
    out["id"] = out["id"] or f"{file}#{n}"
    return out


def parse_testcases(path: str, text: str) -> list:
    """-> [{"id", "title", "steps", "expected", "entry_point", "priority", "type", "file", "line"}]; raises ValueError if unreadable."""
    rows = []
    if path.lower().endswith(".csv"):
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        for n, r in enumerate(reader, 1):
            if any((v or "").strip() for v in r.values() if isinstance(v, str)):
                rows.append(_row(r, path, reader.line_num, n))
            if len(rows) >= MAX_CASES:
                break
    else:
        try:
            data = json.loads(text) if path.lower().endswith(".json") else yaml.safe_load(text)
        except (json.JSONDecodeError, yaml.YAMLError) as e:
            raise ValueError(f"not valid {'JSON' if path.endswith('.json') else 'YAML'}: {str(e)[:80]}")
        if isinstance(data, dict):
            data = data.get("test_cases") or data.get("testcases") or data.get("tests") or []
        if not isinstance(data, list):
            raise ValueError("expected a list of test cases")
        for n, r in enumerate(data[:MAX_CASES], 1):
            if isinstance(r, dict):
                rows.append(_row(r, path, n, n))
    return rows
