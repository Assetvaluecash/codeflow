"""Automation inventory (UC-3, docs/CASE-STUDY.md): what an automation tester needs from the code, without reading it.

* APIs: every endpoint with its path, query and header parameters and request-body fields; for each, whether it is mandatory,
  multi-valued, the values it allows and its limits (lengths, ranges, patterns, formats); generated valid and invalid values; a test
  matrix (one valid baseline, then one case per invalid value and per boundary or allowed value, each with its expected outcome); the
  error statuses the handler can answer with.
* UI: every page (route) with its elements: how to locate each uniquely, whether it is mandatory, its input rules, a dropdown's values,
  and the APIs the page calls, linked to the analysed endpoints.

Values are generated from the declared rules only. A pattern TIA cannot generate a value for is stated, never guessed.
"""
from __future__ import annotations

import itertools
import re
from typing import Optional

from .models import normalize_path

BOOL = {"bool", "boolean", "Boolean"}
INT = {"int", "Integer", "long", "Long", "short", "Short", "byte", "BigInteger", "conint", "PositiveInt", "NegativeInt"}
NUM = {"float", "double", "Double", "Float", "Decimal", "BigDecimal", "confloat", "condecimal", "number"}
DATE = {"date", "LocalDate"}
DATETIME = {"datetime", "LocalDateTime", "OffsetDateTime", "ZonedDateTime", "Instant"}
STR = {"str", "String", "constr", "EmailStr", "UUID", "uuid", "AnyUrl", "HttpUrl", "text", "email", "password", "tel", "search", "url"}
MAX_CASES = 200


def base_type(t: str) -> str:
    """The innermost named type: Optional[List[Status]] -> Status, List<String> -> String, String[] -> String."""
    t = re.sub(r"\[\]$", "", (t or "").strip())
    names = [n for n in re.findall(r"[A-Za-z_][\w.]*", t) if n.split(".")[-1] not in
             ("Optional", "List", "list", "Set", "set", "Sequence", "Annotated", "Union", "None", "Tuple", "tuple", "Collection",
              "Iterable", "FrozenSet", "conlist", "Field", "Query", "Path")]
    return names[0].split(".")[-1] if names else t


def rules_of(f: dict, enums: dict, kind_hint: Optional[str] = None) -> dict:
    """Normalised rules of a field, parameter or form element."""
    t = base_type(f.get("type") or "")
    c = dict(f.get("constraints") or {})
    allowed = c.pop("allowed", None) or enums.get(t)
    kind = ("enum" if allowed else "boolean" if t in BOOL or kind_hint == "checkbox" else "integer" if t in INT
            else "number" if t in NUM or kind_hint in ("number", "range") else "date" if t in DATE or kind_hint == "date"
            else "datetime" if t in DATETIME or kind_hint == "datetime-local" else "string" if t in STR or kind_hint else "object")
    if c.get("format") == "email" or kind_hint == "email":
        kind, c["format"] = "string", "email"
    return {"type": f.get("type") or kind_hint or "", "kind": kind, "required": bool(f.get("required") or f.get("mandatory")),
            "multi": bool(f.get("multi")), "nullable": bool(f.get("nullable")), "allowed": list(allowed) if allowed else None,
            "constraints": c, **({"default": f["default"]} if "default" in f else {})}


# ---- values from a pattern ---------------------------------------------------------------------------------------------------------
_CLASSES = {r"\d": "0", "[0-9]": "0", "[A-Z]": "A", "[a-z]": "a", "[A-Za-z]": "a", "[a-zA-Z]": "a", r"\w": "a", "[A-Z0-9]": "A",
            "[A-Za-z0-9]": "a", "[a-zA-Z0-9]": "a", "[0-9A-Z]": "0", "[a-z0-9]": "a", ".": "a", r"\s": " "}


def sample_for_pattern(p: str, min_len: int = 0, max_len: Optional[int] = None) -> Optional[str]:
    """A value matching a simple pattern (literals, \\d \\w [A-Z0-9]-style classes, {n} {m,n} + * ? quantifiers, top-level
    alternation), checked with re.fullmatch; None when the pattern is beyond that."""
    body = p[1:] if p.startswith("^") else p
    body = body[:-1] if body.endswith("$") and not body.endswith("\\$") else body
    alts = _split_alt(body)
    for alt in alts:
        out, i, ok = "", 0, True
        while i < len(alt):
            unit, i = _unit(alt, i)
            if unit is None:
                ok = False
                break
            lo, i = _quant(alt, i)
            out += unit * lo
        if ok:
            try:
                if out and len(out) < min_len and re.fullmatch(body, out + out[-1] * (min_len - len(out))):
                    out = out + out[-1] * (min_len - len(out))                  # repeat the last character up to the minimum length
                if re.fullmatch(body, out) and (max_len is None or len(out) <= max_len) and len(out) >= min_len:
                    return out
            except re.error:
                return None
    return None


def _split_alt(body: str) -> list:
    if body.startswith("(") and body.endswith(")") and body.count("(") == 1:
        body = body[1:-1]
    depth, cur, out = 0, "", []
    for ch in body:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "|" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def _unit(s: str, i: int):
    for k, v in sorted(_CLASSES.items(), key=lambda kv: -len(kv[0])):
        if s.startswith(k, i):
            return v, i + len(k)
    if s[i] == "\\" and i + 1 < len(s) and not s[i + 1].isalnum():
        return s[i + 1], i + 2
    if s[i] == "[":
        j = s.find("]", i)
        cls = s[i + 1:j] if j > 0 else ""
        if cls and not cls.startswith("^"):
            ch = cls[0] if cls[0] != "\\" else (cls[1] if len(cls) > 1 and not cls[1].isalnum() else None)
            return (ch, j + 1) if ch else (None, i)
        return None, i
    if s[i] in "()|{}*+?^$":
        return None, i
    return s[i], i + 1


def _quant(s: str, i: int):
    if i < len(s) and s[i] in "*?":
        return (0, i + 1)
    if i < len(s) and s[i] == "+":
        return (1, i + 1)
    m = re.match(r"\{(\d+)(?:,(\d*))?\}", s[i:])
    if m:
        return int(m.group(1)), i + m.end()
    return 1, i


def values(r: dict, name: str) -> tuple[list, list]:
    """-> (valid values, invalid values), each {"value", "why"}; "value" is None when it cannot be generated (the "why" says what is needed)."""
    c, kind, valid, invalid = r["constraints"], r["kind"], [], []
    if r["required"]:
        invalid.append({"value": "<missing>", "why": f"'{name}' is mandatory: left out"})
    if kind == "enum":
        valid += [{"value": v, "why": "allowed value"} for v in r["allowed"]]
        bad = 999999 if all(isinstance(v, int) and not isinstance(v, bool) for v in r["allowed"]) else f"NOT_A_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}"
        invalid.append({"value": bad, "why": f"not one of {', '.join(map(str, r['allowed']))}"})
    elif kind == "boolean":
        valid += [{"value": True, "why": "true"}, {"value": False, "why": "false"}]
        invalid.append({"value": "maybe", "why": "not a boolean"})
    elif kind in ("integer", "number"):
        step = 1 if kind == "integer" else 0.01
        lo = c.get("minimum", (c["exclusive_minimum"] + step) if "exclusive_minimum" in c else None)
        hi = c.get("maximum", (c["exclusive_maximum"] - step) if "exclusive_maximum" in c else None)
        rnd = (lambda x: int(x) if kind == "integer" else round(x, 2))
        if lo is not None:
            valid.append({"value": rnd(lo), "why": "lowest allowed"})
            invalid.append({"value": rnd(lo - step), "why": f"below the minimum {rnd(lo)}"})
        if hi is not None:
            valid.append({"value": rnd(hi), "why": "highest allowed"})
            invalid.append({"value": rnd(hi + step), "why": f"above the maximum {rnd(hi)}"})
        if lo is None and hi is None:
            valid.append({"value": 1 if kind == "integer" else 1.5, "why": "a typical value"})
        if kind == "integer":
            invalid.append({"value": 1.5, "why": "not a whole number"})
        invalid.append({"value": "abc", "why": "not a number"})
    elif kind == "date":
        valid.append({"value": "2026-01-31", "why": "a valid date (ISO 8601)"})
        invalid.append({"value": "2026-13-45", "why": "not a valid date"})
    elif kind == "datetime":
        valid.append({"value": "2026-01-31T10:15:00Z", "why": "a valid date-time (ISO 8601)"})
        invalid.append({"value": "yesterday", "why": "not a date-time"})
    elif kind == "string":
        mn, mx, pat = c.get("min_length", 0), c.get("max_length"), c.get("pattern")
        if c.get("format") == "email":
            valid.append({"value": "tester@example.com", "why": "a valid e-mail address"})
            invalid.append({"value": "not-an-email", "why": "not an e-mail address"})
        elif pat:
            s = sample_for_pattern(pat, mn, mx)
            valid.append({"value": s, "why": f"matches {pat}"} if s is not None else {"value": None, "why": f"a value matching {pat} (TIA cannot generate one)"})
            bad = next((x for x in ("!@#", "abc", "123", "A" * 40, "") if not _matches(pat, x)), None)
            if bad is not None:
                invalid.append({"value": bad, "why": f"does not match {pat}"})
            if s is not None and mx and len(s) < mx:
                longer = sample_for_pattern(pat, mx, mx)
                if longer:
                    valid.append({"value": longer, "why": f"longest allowed ({mx})"})
        else:
            if mn:
                valid.append({"value": "a" * mn, "why": f"shortest allowed ({mn})"})
            if mx:
                valid.append({"value": "a" * mx, "why": f"longest allowed ({mx})"})
            if not mn and not mx:
                valid.append({"value": "sample text", "why": "a typical value"})
        if mn:
            invalid.append({"value": "a" * (mn - 1), "why": f"shorter than {mn}" if mn > 1 else "empty"})
        if mx:
            invalid.append({"value": ("A" if pat else "a") * (mx + 1), "why": f"longer than {mx}"})
    else:
        valid.append({"value": None, "why": f"an object of type {r['type']} (see its schema)"})
    if r["multi"]:
        one = next((v["value"] for v in valid if v["value"] is not None), "x")
        valid = [{"value": [one], "why": "a list with one valid item"}] + [v for v in valid if v["why"] != "a typical value"]
        if "min_items" in c:
            invalid.append({"value": [one] * max(c["min_items"] - 1, 0), "why": f"fewer than {c['min_items']} items"})
        if "max_items" in c:
            invalid.append({"value": [one] * (c["max_items"] + 1), "why": f"more than {c['max_items']} items"})
        invalid.append({"value": one, "why": "a single value, not a list"})
    return valid, invalid


def violates(r: dict, v) -> Optional[str]:
    """Why a concrete value breaks a field's rules, or None when it does not (or the rules say nothing about it)."""
    c, kind = r["constraints"], r["kind"]
    if r["multi"]:
        return None if isinstance(v, list) else "not a list"
    if kind == "enum":
        return None if v in r["allowed"] or str(v) in map(str, r["allowed"]) else "not an allowed value"
    if kind == "boolean":
        return None if isinstance(v, bool) else "not a boolean"
    if kind in ("integer", "number"):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "not a number"
        if kind == "integer" and isinstance(v, float) and not v.is_integer():
            return "not a whole number"
        for k, bad in (("minimum", lambda x, b: x < b), ("maximum", lambda x, b: x > b), ("exclusive_minimum", lambda x, b: x <= b),
                       ("exclusive_maximum", lambda x, b: x >= b)):
            if k in c and bad(v, c[k]):
                return f"{k.replace('_', ' ')} {c[k]}"
        return None
    if kind == "string":
        if not isinstance(v, str):
            return "not text"
        if "min_length" in c and len(v) < c["min_length"]:
            return f"shorter than {c['min_length']}"
        if "max_length" in c and len(v) > c["max_length"]:
            return f"longer than {c['max_length']}"
        if c.get("pattern") and not _matches(c["pattern"], v):
            return f"does not match {c['pattern']}"
        if c.get("format") == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            return "not an e-mail address"
    return None


def _matches(p: str, s: str) -> bool:
    try:
        return re.fullmatch(p.lstrip("^").rstrip("$") if not p.endswith("\\$") else p.lstrip("^"), s) is not None
    except re.error:
        return True


def _describe(r: dict) -> str:
    c, bits = r["constraints"], []
    if r["allowed"]:
        bits.append("one of " + ", ".join(map(str, r["allowed"])))
    for k, txt in (("min_length", "at least {} characters"), ("max_length", "at most {} characters"), ("minimum", ">= {}"), ("maximum", "<= {}"),
                   ("exclusive_minimum", "> {}"), ("exclusive_maximum", "< {}"), ("pattern", "matches {}"), ("min_items", "at least {} items"),
                   ("max_items", "at most {} items"), ("format", "{} format")):
        if k in c:
            bits.append(txt.format(c[k]))
    return "; ".join(bits)


# ---- API inventory -------------------------------------------------------------------------------------------------------------------
def _handler_statuses(ex, handler_id: str) -> list:
    by_id = {f.id: f for f in ex.functions}
    by_name: dict = {}
    for f in ex.functions:
        by_name.setdefault(f.qualname.split(".")[-1], []).append(f)
    seen, todo, out = {handler_id}, [handler_id], []
    while todo:
        f = by_id.get(todo.pop())
        if f is None:
            continue
        for s in f.statuses:
            out.append({**s, "function": f.qualname, "file": f.file})
        for c in f.calls:
            cands = by_name.get(c, [])
            if len(cands) == 1 and cands[0].id not in seen:
                seen.add(cands[0].id)
                todo.append(cands[0].id)
    uniq = {}
    for s in sorted(out, key=lambda s: (s["status"], s["file"], s["line"])):
        uniq.setdefault((s["status"], s.get("detail")), s)
    return list(uniq.values())


def endpoint_inventory(ex, e, labels: dict) -> dict:
    schemas = {s.name: s for s in ex.schemas}
    rejected = 400 if e.file.endswith(".java") else 422
    inputs = []
    for p in e.params:
        r = rules_of(p, ex.enums)
        inputs.append({"name": p["name"], "in": p["in"], **r})
    body = next((schemas[n] for n in e.request_schemas if n in schemas), None)
    if body is not None:
        for name, f in body.fields.items():
            inputs.append({"name": name, "in": "body", **rules_of(f, ex.enums)})
    for i in inputs:
        i["valid"], i["invalid"] = values(i, i["name"])
        if i["in"] == "path":                                  # a path without the parameter is another route, not a negative case
            i["invalid"] = [x for x in i["invalid"] if x["value"] != "<missing>"]
        i["rules"] = _describe(i)
    baseline = {}
    for i in inputs:
        v = next((x["value"] for x in i["valid"] if x["value"] is not None), None)
        if i["required"] or v is not None:
            baseline.setdefault(i["in"], {})[i["name"]] = v
    cases = [{"id": "C1", "kind": "valid baseline", "input": baseline, "expect": "accepted",
              "why": "every mandatory input with a valid value"}]
    for i in inputs:
        for x in i["invalid"]:
            inp = {k: dict(v) for k, v in baseline.items()}
            if x["value"] == "<missing>":
                inp.get(i["in"], {}).pop(i["name"], None)
            else:
                inp.setdefault(i["in"], {})[i["name"]] = x["value"]
            cases.append({"id": f"C{len(cases) + 1}", "kind": "invalid", "field": i["name"], "input": inp,
                          "expect": f"rejected ({rejected})" if i["in"] != "path" else "rejected (400 or 404)", "why": x["why"]})
        for x in i["valid"][1:]:
            if x["value"] is None:
                continue
            inp = {k: dict(v) for k, v in baseline.items()}
            inp.setdefault(i["in"], {})[i["name"]] = x["value"]
            cases.append({"id": f"C{len(cases) + 1}", "kind": "boundary or allowed value", "field": i["name"], "input": inp,
                          "expect": "accepted", "why": x["why"]})
    classes = [max(1, sum(1 for x in i["valid"] if x["value"] is not None)) for i in inputs]
    resp = schemas.get(e.response_schema or "")
    return {"entry_point": f"{e.method} {e.path}", "node": e.id, "service": e.service, "repo": e.repo, "file": e.file, "line": e.line,
            **({"label": labels[e.id]} if e.id in labels else {}), "method": e.method, "path": e.path,
            "inputs": inputs, "request_schema": body.name if body else None,
            "response_schema": resp.name if resp else None,
            "response_fields": [{"name": n, "type": f.get("type"), **({"multi": True} if f.get("multi") else {})} for n, f in (resp.fields.items() if resp else [])],
            "error_statuses": _handler_statuses(ex, e.handler_id),
            "cases": cases[:MAX_CASES], "case_count": len(cases), "cases_truncated": len(cases) > MAX_CASES,
            "valid_combinations": _product(classes)}


def _product(xs) -> int:
    n = 1
    for x in xs:
        n *= x
    return n


def api_inventory(extracts: dict, labels: Optional[dict] = None) -> list:
    labels = labels or {}
    out = [endpoint_inventory(ex, e, labels) for _, ex in sorted(extracts.items()) for e in ex.endpoints]
    return sorted(out, key=lambda x: (x["repo"], x["path"], x["method"]))


# ---- UI inventory --------------------------------------------------------------------------------------------------------------------
def option_values(el: dict) -> list:
    """A dropdown's selectable values. An option with an empty value ("Choose a month", "All") is the no-selection placeholder, not a value;
    an option without a value attribute submits its text."""
    out = []
    for o in el.get("options", []):
        v = o.get("value")
        if v == "":
            continue
        v = v if v is not None else o.get("text")
        if v not in (None, "") and v not in out:
            out.append(v)
    return out


def ui_inventory(extracts: dict) -> list:
    eps = {}
    for ex in extracts.values():
        for e in ex.endpoints:
            eps.setdefault((e.method, e.norm_path), []).append(e)
    pages = []
    for repo, ex in sorted(extracts.items()):
        for p in ex.ui_pages:
            page = {**p, "elements": []}
            for el in p["elements"]:
                hint = el.get("type") if el["tag"] == "input" else el["tag"]
                r = rules_of({"type": "", "mandatory": el.get("mandatory"), "constraints": el.get("constraints")}, {}, hint or "text")
                if el["tag"] == "select" and el.get("options"):
                    real = option_values(el)
                    r["kind"], r["allowed"] = "enum", real or None
                    if not real:
                        r["kind"] = "string"
                item = {**el}
                if el["tag"] in ("input", "select", "textarea"):
                    item["rules"] = _describe(r)
                    item["valid"], item["invalid"] = values(r, el.get("label") or el.get("name") or el["tag"])
                page["elements"].append(item)
            page["api_calls"] = []
            for c in p.get("api_calls", []):
                hits = eps.get((c["method"], c["norm_path"]), [])
                page["api_calls"].append({**c, "endpoints": [{"entry_point": f"{e.method} {e.path}", "service": e.service, "repo": e.repo} for e in hits],
                                          **({} if hits else {"note": "not matched to an analysed endpoint (another host, a gateway prefix, or code TIA does not analyse)"})})
            page["summary"] = {"elements": len(page["elements"]), "mandatory": sum(1 for e in page["elements"] if e.get("mandatory")),
                               "without_unique_locator": sum(1 for e in page["elements"] if e["locator"]["strategy"] == "position (fragile)"),
                               "dropdowns": sum(1 for e in page["elements"] if e["tag"] == "select")}
            pages.append(page)
    return pages


def summarise_api(items: list) -> dict:
    return {"endpoints": len(items), "inputs": sum(len(i["inputs"]) for i in items),
            "mandatory_inputs": sum(1 for i in items for x in i["inputs"] if x["required"]),
            "inputs_with_rules": sum(1 for i in items for x in i["inputs"] if x["rules"]),
            "cases": sum(i["case_count"] for i in items)}


def matrix_preview(item: dict, limit: int = 5) -> list:
    """The first cases of an endpoint's matrix as readable lines (for the CLI)."""
    return [f"{c['id']} {c['kind']}{' ' + c['field'] if c.get('field') else ''}: expect {c['expect']} ({c['why']})" for c in itertools.islice(item["cases"], limit)]
