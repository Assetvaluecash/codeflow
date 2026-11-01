"""Keep secrets out of TIA's records and reports (PL-011; PRD 11.4 P0: "secrets, credentials ... must not be copied into graph records
or reports"; TFD 11: credentials found in source files are never copied).

Source code under analysis can contain credentials, and parts of it reach TIA's data: the text of an unresolved call, an unparsed SQL
statement, an error message. Scrubbing happens where facts are created (per-file extraction), where the report is finished, and where
audit records are written, so a secret never reaches the database, the graph, the report page, the PR comment or the audit chain.

Two tiers:
  * HIGH-CONFIDENCE formats (GitHub / Slack / Stripe tokens, AWS access key ids, private-key blocks, JWTs, a password in URL userinfo)
    cannot be an identifier, so they are replaced wherever they appear, in any string.
  * GENERIC assignments (password=..., "token": "...", PASSWORD 'abc' in SQL) are replaced only in free-text fields, never in ids,
    paths or names, so that scrubbing cannot damage analysis.
What was withheld is counted, never kept: the report says how many values were withheld so they can be rotated.
"""
from __future__ import annotations

import re
from typing import Any

HIGH = [
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{40,255})\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)")),
    ("slack_token", re.compile(r"\bxox[abprso]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
]
USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]*):([^/\s@]+)@")
GENERIC = [
    re.compile(r"(?i)\b((?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|auth(?:orization)?)"
               r"[\"']?\s*[:=]\s*)([\"']?)([^\s\"';,)}\]]{6,})\2"),
    re.compile(r"(?i)\b(password\s+(?:=\s*)?)(')([^']{3,})'"),
    re.compile(r"(?i)\b(identified\s+by\s+)(')([^']{3,})'"),
    re.compile(r"(?i)\b(bearer\s+)()([A-Za-z0-9._~+/=-]{20,})"),
]
CODE_EXPR = re.compile(r"^(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+|[A-Za-z_][\w.]*\(.*)$")       # settings.AUTH, get_password(...)
PLACEHOLDER = re.compile(r"^(?:\$|\{|<|%|\[REDACTED|\*+$|x+$|\.+$|none$|null$|true$|false$|changeme$|example$|\?)", re.I)
FREE_TEXT = {"reason", "text", "detail", "error", "message", "limitations", "required_verification", "summary", "failure_reason",
             "note", "notes", "sql", "statement"}


def mask_literals(s: str) -> str:
    """The shape of a code or SQL snippet without the contents of its string literals ('abc...' -> '...'). Snippets quoted in reasons and
    unknowns only need to show what kind of expression could not be analysed; a literal in them could be a credential that no pattern
    recognises, so it is not kept. A literal cut off by truncation is masked too. Literals of up to three characters are kept."""
    out, i, n = [], 0, len(s)
    while i < n:
        q = s[i]
        if q not in "'\"":
            out.append(q)
            i += 1
            continue
        j = i + 1
        while j < n:
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == q:
                if q == "'" and j + 1 < n and s[j + 1] == "'":          # SQL: '' is a quote inside the literal
                    j += 2
                    continue
                break
            j += 1
        if j >= n:                                                       # unterminated (the snippet was truncated)
            out.append(q + "...")
            break
        out.append(q + ("..." if j - i - 1 > 3 else s[i + 1:j]) + q)
        i = j + 1
    return "".join(out)


def mark(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def scrub_text(s: str, generic: bool = False) -> tuple:
    """-> (clean text, number of values withheld)."""
    if not s or len(s) < 6:
        return s, 0
    n = 0
    for kind, rx in HIGH:
        s, k = rx.subn(mark(kind), s)
        n += k

    def userinfo(m):
        nonlocal n
        if PLACEHOLDER.match(m.group(3)):
            return m.group(0)
        n += 1
        return f"{m.group(1)}{m.group(2)}:{mark('url_password')}@"
    s = USERINFO.sub(userinfo, s)
    if generic:
        for rx in GENERIC:
            def repl(m):
                nonlocal n
                if PLACEHOLDER.match(m.group(3)) or (not m.group(2) and CODE_EXPR.match(m.group(3))):
                    return m.group(0)                       # an unquoted code expression is not a credential
                n += 1
                return f"{m.group(1)}{m.group(2)}{mark('secret')}{m.group(2)}"
            s = rx.sub(repl, s)
    return s, n


def scrub_json(obj: Any, generic_everywhere: bool = False, _free: bool = False) -> tuple:
    """Scrubbed copy of a JSON-like structure and the count withheld. Identifiers, paths and names get the high-confidence tier only;
    values under free-text keys (reason, text, detail, error, ...) also get the generic tier."""
    if isinstance(obj, str):
        return scrub_text(obj, generic=generic_everywhere or _free)
    if isinstance(obj, dict):
        out, total = {}, 0
        for k, v in obj.items():
            out[k], n = scrub_json(v, generic_everywhere, _free or k in FREE_TEXT)
            total += n
        return out, total
    if isinstance(obj, (list, tuple)):
        out, total = [], 0
        for v in obj:
            c, n = scrub_json(v, generic_everywhere, _free)
            out.append(c)
            total += n
        return out, total
    return obj, 0


def scrub_extract(part) -> int:
    """Scrub the free-text and value fields of a per-file RepoExtract in place; returns the number of values withheld."""
    total = 0

    def fix(obj, attr, generic, mask=False):
        nonlocal total
        v = getattr(obj, attr, None) if not isinstance(obj, dict) else obj.get(attr)
        if isinstance(v, str) and v:
            c, n = scrub_text(v, generic)
            if mask:
                c = mask_literals(c)
            if n or c != v:
                total += n
                if isinstance(obj, dict):
                    obj[attr] = c
                else:
                    setattr(obj, attr, c)
    for cs in part.callsites:
        fix(cs, "reason", True, mask=True)
        fix(cs, "norm_path", False)
        fix(cs, "base_value", False)
    for u in part.data_unknowns:
        fix(u, "reason", True, mask=True)
    for m in part.migrations:
        for u in m.unparsed:
            fix(u, "text", True, mask=True)
            fix(u, "reason", True, mask=True)
    for f in part.files_failed:
        fix(f, "error", True)
    return total
