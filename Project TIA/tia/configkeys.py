"""Configuration and environment-variable changes (docs/MEETING-PAWAN.md, ask 1a: "he also added a new environment variable that other
methods use - test these too").

Two kinds of change are found between the base and the proposed revision of the changed repository:

* a configuration file changes a key (.env, .properties, YAML incl. docker-compose `environment:` and Kubernetes `env:`, INI/CFG, TOML,
  appsettings/config/settings JSON): every function that reads the key is reached, so its entry points are retested;
* the code starts or stops reading a key (os.getenv, os.environ, System.getenv, @Value("${...}"), getProperty, process.env, Go os.Getenv,
  .NET GetEnvironmentVariable / Configuration[...]): the tester is told which key, whether any configuration file in the repository sets
  it, and which other functions read the same key.

Keys are compared with Spring's relaxed binding in mind: `payments.base-url`, `PAYMENTS_BASEURL` and `payments:base_url` are the same key
(case, dots, dashes, colons and underscores are ignored). VALUES ARE NEVER REPORTED: configuration holds secrets, so only "added",
"removed" or "value changed" is said. Read-only: git grep / git show, nothing is executed. A read that happens outside a function (a
module constant, a Java field) is followed to the functions of the same file that use that name; uses in other files are not followed.
"""
from __future__ import annotations

import configparser
import json
import posixpath
import re
import subprocess
import tomllib
from typing import Optional

import yaml

from . import gitutil
from .exclude import should_exclude

MAX_CONFIG_BYTES = 1_000_000
MAX_CONFIG_FILES = 300
MAX_HITS = 20_000

# what counts as a configuration file (by name); YAML/JSON that are not configuration are left out
ENV_NAME = re.compile(r"(^|/)\.env(\.[A-Za-z0-9_-]+)?$|\.env$")
CONFIG_EXT = (".properties", ".yml", ".yaml", ".ini", ".cfg", ".toml")
JSON_CONFIG = re.compile(r"(^|/)(appsettings|config|settings|env)[A-Za-z0-9_.-]*\.json$", re.I)
NOT_CONFIG = re.compile(r"(^|/)(tia-services\.ya?ml|openapi[^/]*|swagger[^/]*|pnpm-lock\.yaml|\.pre-commit-config\.yaml|mkdocs\.yml|"
                        r"pyproject\.toml|poetry\.lock|cargo\.toml|setup\.cfg|tox\.ini|\.flake8|mypy\.ini|pytest\.ini)$"
                        r"|(^|/)(\.github|\.gitlab|testcases|test-cases|test_cases|contracts|node_modules|\.circleci)/", re.I)

# code that reads a key: (regex over one line, language hint). Group 1 is the key as written.
READS = [
    re.compile(r"""\bgetenv\(\s*["']([^"']+)["']"""),                            # Python os.getenv / getenv, Java System.getenv
    re.compile(r"""\benviron(?:\.get)?\s*[(\[]\s*["']([^"']+)["']"""),          # os.environ["K"], os.environ.get("K")
    re.compile(r"""@Value\(\s*"\$\{([^}:"]+)"""),                                # Spring @Value("${key:default}")
    re.compile(r"""\bgetProperty\(\s*"([^"]+)\""""),                             # environment.getProperty("key")
    re.compile(r"""\bprocess\.env\.([A-Za-z_][A-Za-z0-9_]*)"""),                 # Node process.env.KEY
    re.compile(r"""\bprocess\.env\[\s*["'`]([^"'`]+)["'`]"""),                   # Node process.env["KEY"]
    re.compile(r"""\b(?:Getenv|LookupEnv)\(\s*"([^"]+)\""""),                   # Go os.Getenv / os.LookupEnv
    re.compile(r"""\bGetEnvironmentVariable\(\s*"([^"]+)\""""),                  # .NET Environment.GetEnvironmentVariable
    re.compile(r"""\bConfiguration\[\s*"([^"]+)"\s*\]"""),                       # .NET IConfiguration["Section:Key"]
    re.compile(r"""\bGetValue<[^>]*>\(\s*"([^"]+)\""""),                         # .NET configuration.GetValue<T>("Key")
]
GREP = r"getenv|environ|@Value|getProperty|process\.env|Getenv|LookupEnv|GetEnvironmentVariable|Configuration\[|GetValue<"
CODE_EXT = (".py", ".java", ".kt", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".cs", ".scala", ".groovy")
ASSIGNED = re.compile(r"^\s*(?:(?:public|private|protected|static|final|readonly|const|let|var|export)\s+)*(?:[\w<>\[\],.?]+\s+)?"
                      r"([A-Za-z_]\w*)\s*(?::\s*[\w\[\],. |]+)?\s*(?::=|=)(?!=)")
FIELD = re.compile(r"([A-Za-z_]\w*)\s*(?:=[^;]*)?;\s*$")


def norm_key(key: str) -> str:
    """The comparable form of a key: case, dots, dashes, colons, brackets and underscores ignored (Spring relaxed binding)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(key)).upper()


def is_config_file(path: str) -> bool:
    p = path.replace("\\", "/")
    if NOT_CONFIG.search(p):
        return False
    name = posixpath.basename(p)
    if ENV_NAME.search(p) and not name.endswith((".py", ".js", ".ts", ".java", ".sh", ".md")):
        return True
    return p.lower().endswith(CONFIG_EXT) or bool(JSON_CONFIG.search(p))


def is_code_file(path: str) -> bool:
    return path.lower().endswith(CODE_EXT)


# ---- configuration files -------------------------------------------------------------------------------------------------------------
def _env_block(v, out: dict, src: str):
    """docker-compose `environment:` (mapping or ["K=V"]) and Kubernetes `env:` ([{name, value}]) declare environment variables."""
    if isinstance(v, dict):
        for k, x in v.items():
            out[str(k)] = json.dumps(x, sort_keys=True, default=str)
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, str):
                k, _, val = x.partition("=")
                out[k.strip()] = val
            elif isinstance(x, dict) and "name" in x:
                out[str(x["name"])] = json.dumps({k: x[k] for k in x if k != "name"}, sort_keys=True, default=str)


def _flatten(obj, prefix: str, out: dict):
    if isinstance(obj, dict):
        for k, v in obj.items():
            k = str(k)
            if k in ("environment", "env") and isinstance(v, (dict, list)):
                _env_block(v, out, prefix)
                continue
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list) and any(isinstance(x, (dict, list)) for x in obj):
        for i, x in enumerate(obj):                              # Kubernetes containers[0].env, list-valued settings
            _flatten(x, f"{prefix}[{i}]", out)
    elif prefix:
        out[prefix] = json.dumps(obj, sort_keys=True, default=str)


def parse_config(path: str, text: str) -> dict:
    """key (as written) -> comparable value text. Raises ValueError when the file cannot be read as configuration."""
    p = path.lower()
    out: dict = {}
    if len(text) > MAX_CONFIG_BYTES:
        raise ValueError("file too large")
    try:
        if p.endswith((".yml", ".yaml")):
            for doc in yaml.safe_load_all(text):
                _flatten(doc, "", out)
        elif p.endswith(".json"):
            _flatten(json.loads(text), "", out)
        elif p.endswith(".toml"):
            _flatten(tomllib.loads(text), "", out)
        elif p.endswith((".ini", ".cfg")):
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            cp.read_string(text)
            for sec in cp.sections():
                for k, v in cp.items(sec, raw=True):
                    out[f"{sec}.{k}"] = v
        else:                                                    # .properties and .env: KEY=VALUE / key: value lines
            for line in text.splitlines():
                s = line.strip()
                if not s or s[0] in "#!;":
                    continue
                s = re.sub(r"^export\s+", "", s)
                m = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)$", s)
                if m:
                    out[m.group(1)] = m.group(2).strip()
    except (yaml.YAMLError, json.JSONDecodeError, tomllib.TOMLDecodeError, configparser.Error, UnicodeError) as e:
        raise ValueError(str(e)[:120])
    return out


def config_diff(path: str, base_text: Optional[str], head_text: Optional[str]) -> Optional[list]:
    """[{key, norm, change: added|removed|value_changed, file}] or None when either side cannot be parsed. Values are compared, never kept."""
    try:
        b = parse_config(path, base_text) if base_text is not None else {}
        h = parse_config(path, head_text) if head_text is not None else {}
    except ValueError:
        return None
    out = []
    for k in sorted(set(b) | set(h)):
        if k not in h:
            change = "removed"
        elif k not in b:
            change = "added"
        elif b[k] != h[k]:
            change = "value_changed"
        else:
            continue
        out.append({"key": k, "norm": norm_key(k), "change": change, "file": path})
    return out


# ---- reads in code -------------------------------------------------------------------------------------------------------------------
def keys_in_line(text: str) -> list:
    return [m.group(1) for rx in READS for m in rx.finditer(text)]


def _grep(repo_path: str, rev: str, pattern: str, files: Optional[list] = None) -> list:
    """[(path, line, text)] for lines matching an extended regex at a revision. No match is an empty list, not an error."""
    if files is not None and not files:
        return []
    args = ["git", "-C", repo_path, "grep", "-n", "-I", "-z", "-E", pattern, rev, "--"]
    args += files if files is not None else []
    try:
        r = subprocess.run(args, capture_output=True, check=False)
    except FileNotFoundError as e:
        raise gitutil.GitError(str(e))
    if r.returncode == 1:
        return []
    if r.returncode != 0:
        raise gitutil.GitError(r.stderr.decode("utf-8", "replace").strip() or "git grep failed")
    out = []
    for raw in r.stdout.decode("utf-8", "replace").splitlines():
        parts = raw.split("\0")
        if len(parts) < 3:
            continue
        path = parts[0].split(":", 1)[1] if parts[0].startswith(rev + ":") else parts[0]
        try:
            out.append((path, int(parts[1]), "\0".join(parts[2:])))
        except ValueError:
            continue
        if len(out) >= MAX_HITS:
            break
    return out


class _Spans:
    """Which function a line of a file belongs to (the innermost function whose lines contain it)."""

    def __init__(self, functions):
        self.by_file: dict = {}
        for f in functions:
            self.by_file.setdefault(f.file, []).append((f.line, f.end_line, f.id))

    def at(self, file: str, line: int) -> Optional[str]:
        best = None
        for start, end, fid in self.by_file.get(file, ()):
            if start <= line and (end is None or end >= line) and (best is None or start > best[0]):
                best = (start, fid)
        return best[1] if best else None


def reads_at(repo_path: str, rev: str, functions, files: Optional[list] = None, exclude: tuple = ()) -> list:
    """Every configuration read at a revision: [{key, norm, function (id or None), file, line, via (name of a constant or field, or None)}].
    A read outside any function is followed to the functions of the same file that use the name it is assigned to."""
    spans = _Spans(functions)
    hits = [(p, n, t) for p, n, t in _grep(repo_path, rev, GREP, files)
            if is_code_file(p) and not should_exclude(p, exclude)]
    out, module_level = [], []
    for path, line, text in hits:
        for key in keys_in_line(text):
            fid = spans.at(path, line)
            if fid:
                out.append({"key": key, "norm": norm_key(key), "function": fid, "file": path, "line": line, "via": None})
            else:
                module_level.append((path, line, text, key))
    texts: dict = {}
    for path, line, text, key in module_level:
        if path not in texts:
            try:
                texts[path] = gitutil.read_file(repo_path, rev, path).splitlines()
            except gitutil.GitError:
                texts[path] = []
        lines = texts[path]
        m = ASSIGNED.match(text)
        name = m.group(1) if m and m.group(1) not in ("String", "int", "var", "let", "const") else None
        if name is None and "@Value" in text:                          # a Java field: on the annotation's line or the next one
            rest = text.split(")", 1)[-1]
            fm = FIELD.search(rest) or (FIELD.search(lines[line]) if line < len(lines) else None)
            name = fm.group(1) if fm else None
        uses = []
        if name:
            word = re.compile(rf"\b{re.escape(name)}\b")
            uses = sorted({fid for i, ln in enumerate(lines, 1) if i != line and word.search(ln) and (fid := spans.at(path, i))})
        if not uses:
            out.append({"key": key, "norm": norm_key(key), "function": None, "file": path, "line": line, "via": name})
        for fid in uses:
            out.append({"key": key, "norm": norm_key(key), "function": fid, "file": path, "line": line, "via": name})
    return out


def config_keys_at(repo_path: str, rev: str, exclude: tuple = ()) -> tuple[dict, bool]:
    """norm key -> [configuration files that set it] at a revision, and whether every configuration file could be read."""
    tree = gitutil.list_tree(repo_path, rev)
    files = sorted(p for p in tree if is_config_file(p) and not should_exclude(p, exclude))
    complete = len(files) <= MAX_CONFIG_FILES
    files = files[:MAX_CONFIG_FILES]
    blobs = gitutil.read_blobs(repo_path, sorted({tree[p] for p in files}))
    out: dict = {}
    for p in files:
        try:
            keys = parse_config(p, blobs[tree[p]].decode("utf-8"))
        except (ValueError, UnicodeDecodeError, KeyError):
            complete = False
            continue
        for k in keys:
            out.setdefault(norm_key(k), []).append(p)
    return out, complete


# ---- one change ----------------------------------------------------------------------------------------------------------------------
def analyse(repo_path: str, base: str, head: str, changed_files: list, base_functions, head_functions, exclude: tuple = ()) -> dict:
    """The configuration side of a change.

    -> {"key_changes": [{key, norm, change, file, readers: [read]}],          keys a configuration file adds, removes or changes
        "read_changes": [{key, norm, change: newly_read|no_longer_read, function, file, line, set_in: [files], other_readers: [read]}],
        "parsed_files": [paths read as configuration], "unparsed_files": [paths that look like configuration but could not be read],
        "config_index_complete": bool}"""
    key_changes, parsed, unparsed = [], [], []
    for status, path in changed_files:
        if not is_config_file(path) or should_exclude(path, exclude):
            continue
        try:
            b = gitutil.read_file(repo_path, base, path) if status != "A" else None
            h = gitutil.read_file(repo_path, head, path) if status != "D" else None
        except gitutil.GitError:
            unparsed.append(path)
            continue
        d = config_diff(path, b, h)
        if d is None:
            unparsed.append(path)
            continue
        parsed.append(path)
        key_changes += d

    code_changed = [p for s, p in changed_files if is_code_file(p) and not should_exclude(p, exclude)]
    base_reads = reads_at(repo_path, base, base_functions, exclude=exclude) if (key_changes or code_changed) else []
    by_norm: dict = {}
    for r in base_reads:
        by_norm.setdefault(r["norm"], []).append(r)
    for c in key_changes:
        c["readers"] = _unique(by_norm.get(c["norm"], []))

    read_changes = []
    if code_changed:
        head_reads = reads_at(repo_path, head, head_functions, files=[p for s, p in changed_files if s != "D" and p in code_changed], exclude=exclude)
        before = {(r["function"] or r["file"], r["norm"]): r for r in base_reads if r["file"] in code_changed}
        after = {(r["function"] or r["file"], r["norm"]): r for r in head_reads}
        repo_wide_before = {r["norm"] for r in base_reads}
        added = [after[k] for k in sorted(set(after) - set(before), key=str)]
        dropped = [before[k] for k in sorted(set(before) - set(after), key=str)]
        defined, _complete = config_keys_at(repo_path, head, exclude) if added else ({}, True)
        for r in added:
            others = [x for x in base_reads if x["norm"] == r["norm"] and (x["function"] or x["file"]) != (r["function"] or r["file"])]
            read_changes.append({**r, "change": "newly_read", "new_to_repository": r["norm"] not in repo_wide_before,
                                 "set_in": sorted(set(defined.get(r["norm"], []))), "other_readers": _unique(others)})
        still = {(r["function"] or r["file"], r["norm"]) for r in head_reads}
        for r in dropped:
            if (r["function"] or r["file"], r["norm"]) not in still:
                read_changes.append({**r, "change": "no_longer_read", "set_in": [], "other_readers": []})
        complete = _complete
    else:
        complete = True
    return {"key_changes": key_changes, "read_changes": read_changes, "parsed_files": parsed, "unparsed_files": unparsed,
            "config_index_complete": complete}


CHANGE_TEXT = {"added": "is added to", "removed": "is removed from", "value_changed": "changes value in"}


def _fn_name(g, fid: str | None, read: dict) -> str:
    if fid and fid in g.nodes:
        return str(g.nodes[fid].get("name") or fid.split("::")[-1])
    return f"{read['file']}:{read['line']}" + (f" ({read['via']})" if read.get("via") else "")


def checks(result: dict, g, entry_points: list, repo: str) -> list:
    """The tester's view of the configuration side of a change: one check per changed key or new read, with the entry points that reach
    the reading code (from the test scope rows, whose paths run from the change to the entry point)."""
    def reached_by(fids: set) -> list:
        rows = {(e["entry_point"], e["service"], e["repo"]) for e in entry_points if fids & {p["node"] for p in e.get("path", [])}}
        return [{"entry_point": a, "service": b, "repo": c} for a, b, c in sorted(rows)]

    out = []
    for c in result.get("key_changes", []):
        fids = {r["function"] for r in c["readers"] if r["function"]}
        names = [_fn_name(g, r["function"], r) for r in c["readers"]]
        eps = reached_by(fids)
        if c["readers"]:
            text = (f"Configuration `{c['key']}` {CHANGE_TEXT[c['change']]} {c['file']}. It is read by " + ", ".join(names[:5])
                    + (" ..." if len(names) > 5 else "") + ("; test the entry points listed with the new setting" if eps else "")
                    + (" - the code now falls back to its default or fails: check what it does" if c["change"] == "removed" else "") + ".")
        else:
            text = (f"Configuration `{c['key']}` {CHANGE_TEXT[c['change']]} {c['file']}. No analysed code reads it: it may be read by a "
                    "framework, another service or a deployment script - confirm with the developer what it controls.")
        out.append({"repo": repo, "kind": "config_key_changed", "key": c["key"], "change": c["change"], "file": c["file"],
                    "readers": [{"function": n, "file": r["file"], "line": r["line"]} for n, r in zip(names, c["readers"])],
                    "entry_points": eps, "check": text})
    for r in result.get("read_changes", []):
        name = _fn_name(g, r["function"], r)
        if r["change"] == "no_longer_read":
            out.append({"repo": repo, "kind": "config_no_longer_read", "key": r["key"], "change": "no_longer_read", "file": r["file"],
                        "readers": [{"function": name, "file": r["file"], "line": r["line"]}], "entry_points": [],
                        "check": f"{name} no longer reads configuration `{r['key']}`: whatever that setting controlled there no longer applies."})
            continue
        others = [_fn_name(g, x["function"], x) for x in r["other_readers"]]
        eps = reached_by({r["function"]} if r["function"] else set())
        where = (f"It is set in {', '.join(r['set_in'][:3])}" if r["set_in"] else
                 "No configuration file in the repository sets it: make sure it is set in Dev (and every environment the change is "
                 "deployed to) before testing - values set by the pipeline, a vault or the platform are outside what TIA can see")
        also = (f". {', '.join(others[:5])} already read{'s' if len(others) == 1 else ''} the same key: check they still behave with the "
                "value this change needs" if others else "")
        out.append({"repo": repo, "kind": "config_newly_read", "key": r["key"], "change": "newly_read", "file": r["file"],
                    "new_to_repository": r.get("new_to_repository", False), "set_in": r["set_in"],
                    "readers": [{"function": name, "file": r["file"], "line": r["line"]}],
                    "other_readers": [{"function": n, "file": x["file"], "line": x["line"]} for n, x in zip(others, r["other_readers"])],
                    "entry_points": eps,
                    "check": (f"{name} now reads {'a new ' if r.get('new_to_repository') else ''}configuration `{r['key']}`"
                              + f". {where}{also}.")})
    return out


def redact_checks(items: list, readable: set) -> tuple[list, int]:
    """Configuration checks describe the changed repository: an audience that cannot read it sees none; entry points in repositories the
    audience cannot read are left out of a check it can see. -> (kept checks, number withheld)."""
    kept = []
    for c in items:
        if c["repo"] not in readable:
            continue
        c["entry_points"] = [e for e in c["entry_points"] if e["repo"] in readable]
        kept.append(c)
    return kept, len(items) - len(kept)


def _unique(reads: list) -> list:
    seen, out = set(), []
    for r in sorted(reads, key=lambda r: (r["file"], r["line"], r["function"] or "")):
        k = (r["function"], r["file"]) if r["function"] else (None, r["file"], r["line"])
        if k not in seen:
            seen.add(k)
            out.append({k2: r[k2] for k2 in ("key", "function", "file", "line", "via")})
    return out
