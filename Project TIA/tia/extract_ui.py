"""Front-end adapter (UC-3, docs/CASE-STUDY.md): pages, their UI elements, how to locate each one uniquely, whether it is mandatory, its
input rules, the values a dropdown offers, and the API calls a page makes.

Reads Angular (component templates, @Component, reactive-form validators, route tables), React (JSX, React Router <Route>), Vue
templates and plain HTML. Source is scanned, never executed or rendered: what a page shows only at run time (values loaded from an API,
elements built in loops from data, ids computed by code) is reported as dynamic, not guessed. Standard library only.
"""
from __future__ import annotations

import bisect
import posixpath
import re
from typing import Optional

from .models import normalize_path

MARKUP_EXT = (".html", ".htm", ".vue", ".jsx", ".tsx")
SCRIPT_EXT = (".ts", ".js", ".mjs")
HTML_TAGS = {"a", "button", "form", "input", "label", "option", "optgroup", "select", "textarea", "table", "tr", "td", "th", "thead", "tbody",
             "div", "span", "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "nav", "section", "header", "footer", "main",
             "img", "fieldset", "legend", "dialog", "details", "summary", "article", "aside", "datalist", "output", "progress"}
CONTROLS = {"input", "select", "textarea", "button", "a"}
TEST_IDS = ("data-testid", "data-test-id", "data-test", "data-cy", "data-qa")
VOID = {"input", "img", "br", "hr", "meta", "link"}


MAX_UI_FILE = 1_000_000                       # bigger front-end files are bundles or generated code, not pages
GENERATED_DIRS = ("/node_modules/", "/dist/", "/build/", "/.angular/", "/coverage/", "/vendor/", "/.next/", "/out/")


def is_ui_file(path: str) -> bool:
    p = path.lower()
    if any(d in "/" + p for d in GENERATED_DIRS) or p.endswith((".d.ts", ".min.js", ".spec.ts", ".test.ts", ".spec.js", ".test.js", ".spec.tsx", ".test.tsx",
                                                  ".spec.jsx", ".test.jsx", ".config.js", ".config.ts")):
        return False
    return p.endswith(MARKUP_EXT + SCRIPT_EXT)


# ---- markup scanner ----------------------------------------------------------------------------------------------------------------
def _skip_braces(text: str, i: int) -> int:
    """text[i] == '{': the index just after the matching '}', skipping strings and template literals."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def scan(text: str) -> list:
    """Tokens of HTML or JSX: {"open": tag, "attrs", "line", "self_closing"} / {"close": tag} / {"text": str, "line"}."""
    out, i, n = [], 0, len(text)
    nl = [k for k, c in enumerate(text) if c == "\n"]
    line_at = lambda k: bisect.bisect_left(nl, k) + 1
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            break
        if lt > i:
            t = re.sub(r"\s+", " ", text[i:lt]).strip()
            if t:
                out.append({"text": t, "line": line_at(i)})
        if text.startswith("<!--", lt):
            end = text.find("-->", lt)
            i = n if end < 0 else end + 3
            continue
        m = re.match(r"</\s*([A-Za-z][\w.:-]*)\s*>", text[lt:])
        if m:
            out.append({"close": m.group(1), "line": line_at(lt)})
            i = lt + m.end()
            continue
        m = re.match(r"<([A-Za-z][\w.:-]*)", text[lt:])
        if not m:
            i = lt + 1
            continue
        tag, j, attrs, ok = m.group(1), lt + m.end(), {}, False
        while j < n:
            while j < n and text[j].isspace():
                j += 1
            if j >= n:
                break
            if text[j] == ">" or text.startswith("/>", j):
                ok = True
                break
            if text[j] == "{":                                              # JSX spread {...props}
                j = _skip_braces(text, j)
                continue
            am = re.match(r"[^\s=>/]+", text[j:])
            if not am:
                j += 1
                continue
            name, j = am.group(0), j + am.end()
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "=":
                j += 1
                while j < n and text[j].isspace():
                    j += 1
                if j < n and text[j] in "\"'":
                    q, k = text[j], text.find(text[j], j + 1)
                    k = n if k < 0 else k
                    attrs[name] = ("s", text[j + 1:k])
                    j = k + 1
                elif j < n and text[j] == "{":
                    k = _skip_braces(text, j)
                    attrs[name] = ("e", text[j + 1:k - 1].strip())
                    j = k
                else:
                    vm = re.match(r"[^\s>]+", text[j:])
                    attrs[name] = ("s", vm.group(0) if vm else "")
                    j += vm.end() if vm else 1
            else:
                attrs[name] = ("b", True)
        if not ok:
            i = lt + 1
            continue
        self_closing = text.startswith("/>", j)
        i = j + (2 if self_closing else 1)
        out.append({"open": tag, "attrs": attrs, "line": line_at(lt), "self_closing": self_closing})
        if tag.lower() in ("script", "style"):
            end = text.lower().find(f"</{tag.lower()}", i)
            i = n if end < 0 else end
    return out


def static(attrs: dict, *names) -> Optional[str]:
    """The literal value of the first attribute present: "x", {"x"}, {9}, a bare flag; never a bound expression."""
    for nm in names:
        for k, (kind, v) in attrs.items():
            if k.lower() != nm.lower():
                continue
            if kind == "s":
                return v
            if kind == "b":
                return "true"
            lit = re.fullmatch(r"""\s*(?:"([^"]*)"|'([^']*)'|(-?\d+(?:\.\d+)?)|(true|false))\s*""", v)
            if lit:
                return next(g for g in lit.groups() if g is not None)
    return None


def bound(attrs: dict, *names) -> Optional[str]:
    """The expression of a bound attribute: Angular [x]="expr" / [(ngModel)]="expr", JSX x={expr}."""
    for nm in names:
        for k, (kind, v) in attrs.items():
            if k.lower() in (f"[{nm.lower()}]", f"[({nm.lower()})]") or (kind == "e" and k.lower() == nm.lower()):
                return v
    return None


def strip_js_comments(text: str) -> str:
    """Script with // and /* */ comments blanked out (newlines kept, so line numbers do not move). Strings and template literals are
    skipped; JSX comments {/* ... */} are comments too."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            j = i + 1
            while j < n and text[j] != c and not (c != "`" and text[j] == "\n"):
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i) and (i == 0 or text[i - 1] != ":"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ---- templates: elements with labels, rules and options ---------------------------------------------------------------------------
def template_elements(text: str, script: bool = False) -> list:
    """script=True: the markup is JSX inside JavaScript/TypeScript, whose comments are blanked out first."""
    toks = scan(strip_js_comments(text) if script else text)
    labels_for, elements, stack, label_text = {}, [], [], []
    open_label: Optional[dict] = None
    current_select: Optional[dict] = None
    current_option: Optional[dict] = None
    loop_depth, last_text = 0, ""
    for t in toks:
        if "text" in t:
            last_text = t["text"]
            if open_label is not None:
                open_label["text"] = (open_label.get("text", "") + " " + t["text"]).strip()
            if current_option is not None:
                current_option["text"] = (current_option.get("text", "") + " " + t["text"]).strip()
            for el in stack:
                if el.get("_collect"):
                    el["text"] = (el.get("text", "") + " " + t["text"]).strip()
            continue
        if "close" in t:
            tag = t["close"].lower()
            if tag == "label" and open_label is not None:
                if open_label.get("for"):
                    labels_for[open_label["for"]] = open_label.get("text", "")
                for el in open_label.get("wraps", []):
                    el.setdefault("label", open_label.get("text", ""))
                open_label = None
            elif tag == "select":
                current_select = None
            elif tag == "option":
                current_option = None
            if not any(f["tag"] == tag for f in stack):
                continue                                                   # a stray closing tag (or a component's): ignore it
            while stack and stack[-1]["tag"] != tag:
                if stack.pop().get("_loop"):
                    loop_depth -= 1
            if stack:
                el = stack.pop()
                el.pop("_collect", None)
                if el.get("_loop"):
                    loop_depth -= 1
            continue
        tag, a = t["open"], t["attrs"]
        low = tag.lower()
        if low not in HTML_TAGS and "-" not in tag:
            continue                                                       # a component or a generic type: not a DOM element
        loops = "*ngFor" in a or "v-for" in a
        if low == "label":
            open_label = {"for": static(a, "for", "htmlFor"), "text": "", "wraps": []}
            if t["self_closing"]:
                open_label = None
            continue
        if low == "option":
            if current_select is not None:
                opt = {"value": static(a, "value"), "line": t["line"], "text": ""}
                if loops or bound(a, "value"):
                    src = a.get("*ngFor", a.get("v-for", ("e", "")))[1] or (last_text if ".map(" in last_text else bound(a, "value") or "")
                    current_select.setdefault("_dynamic_options", []).append(src)
                else:
                    current_select["options"].append(opt)
                current_option = opt
            continue
        el = None
        is_control = low in CONTROLS and not (low == "a" and not (static(a, "href") or "(click)" in a or "onClick" in a or "routerLink" in a))
        if is_control or any(k in a for k in TEST_IDS) or "role" in a:
            el = {"tag": low, "line": t["line"], "attrs": {k: v for k, (kind, v) in a.items() if kind != "e" and not k.startswith(("(", "*", "["))},
                  "repeated": loop_depth > 0 or loops, "text": ""}
            el["type"] = static(a, "type") or ("text" if low == "input" else None)
            el["name"] = (static(a, "formControlName") or bound(a, "formControl") or static(a, "name") or
                          _model_name(bound(a, "ngModel")) or static(a, "v-model") or static(a, "id"))
            el["dynamic_id"] = bool(bound(a, "id") or bound(a, "attr.id"))
            el["placeholder"] = static(a, "placeholder")
            el["aria_label"] = static(a, "aria-label")
            req = static(a, "required")
            el["mandatory"] = bool(req and req != "false") or (bound(a, "required") or "").strip() == "true"
            if el["mandatory"]:
                el["mandatory_source"] = "required attribute"
            c = {}
            for attr, key, cast in (("maxlength", "max_length", int), ("minlength", "min_length", int), ("max", "maximum", float),
                                    ("min", "minimum", float), ("pattern", "pattern", str), ("step", "step", float)):
                v = static(a, attr)
                if v not in (None, "true"):
                    try:
                        c[key] = cast(v) if cast is str else (int(float(v)) if float(v).is_integer() else float(v))
                    except ValueError:
                        pass
            if el["type"] == "email":
                c["format"] = "email"
            el["constraints"] = c
            el["actions"] = sorted({k.strip("()") for k in a if k.startswith("(") and k.endswith(")")} |
                                   {k for k in a if re.fullmatch(r"on[A-Z]\w*", k)})
            if low == "select":
                el["options"] = []
                current_select = el
            if low in ("button", "a") and not t["self_closing"]:
                el["_collect"] = True
            if open_label is not None:
                open_label["wraps"].append(el)
            elements.append(el)
        if low not in VOID and not t["self_closing"]:
            frame = el if el is not None else {"tag": low}
            if loops:
                frame["_loop"] = True
                loop_depth += 1
            stack.append(frame)
    for el in elements:
        el.pop("_collect", None)
        el.pop("_loop", None)
        if not el.get("label"):
            el["label"] = labels_for.get(el["attrs"].get("id") or "", "") or el.get("aria_label") or ""
    return elements


def _model_name(expr: Optional[str]) -> Optional[str]:
    if not expr:
        return None
    m = re.fullmatch(r"\s*[\w$]+(?:\.([\w$]+))?\s*", expr)
    return (m.group(1) or expr.strip()) if m else None


# ---- scripts: components, form rules, option arrays, API calls, routes ------------------------------------------------------------
def _balanced(text: str, i: int, open_c: str = "{", close_c: str = "}") -> str:
    depth, n, j = 0, len(text), i
    while j < n:
        c = text[j]
        if c in "\"'`":
            k = j + 1
            while k < n and text[k] != c:
                k += 2 if text[k] == "\\" else 1
            j = k + 1
            continue
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
        j += 1
    return text[i + 1:]


def _split_top(text: str) -> list:
    parts, depth, cur, i, n = [], 0, "", 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            k = i + 1
            while k < n and text[k] != c:
                k += 2 if text[k] == "\\" else 1
            cur += text[i:k + 1]
            i = k + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip():
        parts.append(cur)
    return parts


def _js_literal(v: str):
    v = v.strip()
    m = re.fullmatch(r"""(?:"([^"]*)"|'([^']*)'|`([^`$]*)`)""", v)
    if m:
        return next(g for g in m.groups() if g is not None)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", v):
        return int(v) if re.fullmatch(r"-?\d+", v) else float(v)
    if v in ("true", "false"):
        return v == "true"
    return None


def _array_values(body: str) -> Optional[list]:
    vals = []
    for item in _split_top(body):
        item = item.strip()
        if not item:
            continue
        lit = _js_literal(item)
        if lit is not None:
            vals.append(lit)
            continue
        if item.startswith("{"):
            inner = dict(_kv(p) for p in _split_top(_balanced(item, 0)) if ":" in p)
            v = next((inner[k] for k in ("value", "code", "id", "key") if k in inner), None)
            if v is not None:
                vals.append(v)
                continue
        return None                                                        # not a literal list: dynamic
    return vals


def _kv(part: str):
    k, v = part.split(":", 1)
    return k.strip().strip("'\""), _js_literal(v)


def arrays(text: str) -> dict:
    """Literal arrays assigned to a name: `currencies = ['EUR', 'USD']`, `const MONTHS = [...]`, `x: string[] = [...]`."""
    out = {}
    for m in re.finditer(r"(?:\b(?:const|let|var|readonly|public|private|protected)\s+)?\b([A-Za-z_$][\w$]*)\s*(?::\s*[\w<>\[\]\s|'\".]+)?\s*=\s*\[", text):
        vals = _array_values(_balanced(text, m.end() - 1, "[", "]"))
        if vals:
            out.setdefault(m.group(1), vals)
    return out


VALIDATOR = re.compile(r"Validators\.(required|requiredTrue|email|minLength|maxLength|min|max|pattern)\b\s*(?:\(\s*([^)]*)\))?")


def form_controls(text: str) -> dict:
    """Reactive-form controls and their validators: fb.group({name: [init, [Validators.required, ...]]}), new FormControl(init, [...])."""
    out = {}

    def rules(expr: str) -> dict:
        r = {"mandatory": False, "constraints": {}}
        for vm in VALIDATOR.finditer(expr):
            kind, arg = vm.group(1), (vm.group(2) or "").strip()
            if kind in ("required", "requiredTrue"):
                r["mandatory"] = True
            elif kind == "email":
                r["constraints"]["format"] = "email"
            elif kind == "pattern":
                lit = _js_literal(arg) if not arg.startswith("/") else arg.strip("/")
                if lit is not None:
                    r["constraints"]["pattern"] = str(lit)
            else:
                lit = _js_literal(arg)
                if lit is not None:
                    r["constraints"][{"minLength": "min_length", "maxLength": "max_length", "min": "minimum", "max": "maximum"}[kind]] = lit
        return r

    for m in re.finditer(r"\.group\s*\(\s*\{|new\s+FormGroup\s*\(\s*\{", text):
        body = _balanced(text, m.end() - 1)
        for part in _split_top(body):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            out[k.strip().strip("'\"")] = rules(v)
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*[:=]\s*new\s+FormControl\s*(?:<[^>]*>)?\s*\(", text):
        out.setdefault(m.group(1), rules(_balanced(text, m.end() - 1, "(", ")")))
    return out


HTTP_CALL = re.compile(r"\b(?:this\.)?(?:http|httpClient|api|axios)\s*\.\s*(get|post|put|delete|patch)\s*(?:<[^()]*?>)?\s*\(\s*([`'\"])(.*?)\2", re.S)
FETCH = re.compile(r"\bfetch\s*\(\s*([`'\"])(.*?)\1\s*(,\s*\{[^}]*\})?", re.S)


def _api_path(raw: str) -> Optional[str]:
    p = re.sub(r"\$\{[^}]*\}", "{}", raw)
    p = re.sub(r"^https?://[^/]+", "", p).split("?", 1)[0]
    return p if p.startswith("/") else None


def api_calls(text: str) -> list:
    out = []
    for m in HTTP_CALL.finditer(text):
        p = _api_path(m.group(3))
        if p:
            out.append({"method": m.group(1).upper(), "path": p, "line": text.count("\n", 0, m.start()) + 1})
    for m in FETCH.finditer(text):
        p = _api_path(m.group(2))
        if p:
            mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", m.group(3) or "")
            out.append({"method": (mm.group(1) if mm else "GET").upper(), "path": p, "line": text.count("\n", 0, m.start()) + 1})
    return sorted(out, key=lambda c: (c["line"], c["method"], c["path"]))


def routes(text: str, toks: Optional[list] = None) -> list:
    out = []
    for m in re.finditer(r"""path\s*:\s*['"`]([^'"`]*)['"`]\s*,\s*(?:component\s*:\s*([A-Z][\w$]*)|element\s*:\s*<\s*([A-Z][\w$]*))""", text):
        out.append({"path": "/" + m.group(1).lstrip("/"), "component": m.group(2) or m.group(3)})
    for t in toks or []:
        if t.get("open") == "Route":
            p = static(t["attrs"], "path")
            el = t["attrs"].get("element", ("", ""))[1] or t["attrs"].get("component", ("", ""))[1]
            cm = re.search(r"<?\s*([A-Z][\w$]*)", el or "")
            if p is not None and cm:
                out.append({"path": "/" + p.lstrip("/"), "component": cm.group(1)})
    return out


def extract_ui_file(path: str, text: str) -> list:
    """The front-end facts of one file (assembly input)."""
    facts = []
    low = path.lower()
    if len(text) > MAX_UI_FILE:
        return facts
    if low.endswith(SCRIPT_EXT + (".jsx", ".tsx")):
        text = strip_js_comments(text)                                     # commented-out markup, routes or calls are not the page
    markup = low.endswith(MARKUP_EXT)
    jsx = low.endswith((".jsx", ".tsx")) or (low.endswith(".js") and re.search(r"return\s*\(\s*<|=>\s*\(?\s*<[A-Za-z]", text))
    toks = scan(text) if (markup or jsx) else None
    if low.endswith((".html", ".htm", ".vue")):
        framework = "vue" if low.endswith(".vue") else "angular" if re.search(r"formControlName|\*ngFor|\(click\)|\[\(ngModel\)\]|routerLink", text) else "html"
        els = template_elements(text if not low.endswith(".vue") else (re.search(r"<template>(.*)</template>", text, re.S) or [None, text])[1])
        if els:
            facts.append({"kind": "template", "file": path, "framework": framework, "elements": els})
    if low.endswith(SCRIPT_EXT + (".jsx", ".tsx")):
        comp = re.search(r"@Component\s*\(\s*\{", text)
        rts = routes(text, toks)
        if comp:
            meta = _balanced(text, comp.end() - 1)
            tu = re.search(r"templateUrl\s*:\s*['\"`]([^'\"`]+)['\"`]", meta)
            inline = re.search(r"template\s*:\s*`([^`]*)`", meta, re.S)
            cls = re.search(r"export\s+class\s+([A-Z][\w$]*)", text[comp.end():])
            sel = re.search(r"selector\s*:\s*['\"`]([^'\"`]+)['\"`]", meta)
            fact = {"kind": "component", "file": path, "framework": "angular", "class": cls.group(1) if cls else None,
                    "selector": sel.group(1) if sel else None,
                    "template_file": posixpath.normpath(posixpath.join(posixpath.dirname(path), tu.group(1))) if tu else (path if inline else None),
                    "controls": form_controls(text), "arrays": arrays(text), "api_calls": api_calls(text)}
            facts.append(fact)
            if inline:
                els = template_elements(inline.group(1))
                if els:
                    facts.append({"kind": "template", "file": path, "framework": "angular", "elements": els})
        elif jsx:
            name = re.search(r"export\s+default\s+(?:function\s+)?([A-Z][\w$]*)", text) or re.search(r"(?:function|const)\s+([A-Z][\w$]*)", text)
            els = template_elements(text)
            if els:
                facts.append({"kind": "template", "file": path, "framework": "react", "elements": els})
                facts.append({"kind": "component", "file": path, "framework": "react", "class": name.group(1) if name else None,
                              "selector": None, "template_file": path, "controls": {}, "arrays": arrays(text), "api_calls": api_calls(text)})
        for r in rts:
            facts.append({"kind": "route", "file": path, **r})
    return facts


# ---- assembly: pages and locators ---------------------------------------------------------------------------------------------------
def _locators(el: dict, page: list) -> list:
    """Ways to find the element, most robust first, each checked for uniqueness among the page's elements."""
    a, tag, out = el["attrs"], el["tag"], []

    def count(pred):
        return sum(1 for x in page if pred(x))

    for k in TEST_IDS:
        v = a.get(k)
        if v:
            out.append({"strategy": "test id", "css": f'[{k}="{v}"]', "xpath": f"//*[@{k}='{v}']",
                        "playwright": f"getByTestId('{v}')" if k == "data-testid" else f"locator('[{k}=\"{v}\"]')",
                        "unique": count(lambda x: x["attrs"].get(k) == v) == 1})
    if a.get("id") and not el.get("dynamic_id"):
        v = a["id"]
        out.append({"strategy": "id", "css": f"#{v}", "xpath": f"//*[@id='{v}']", "playwright": f"locator('#{v}')",
                    "unique": count(lambda x: x["attrs"].get("id") == v) == 1})
    if a.get("name"):
        v = a["name"]
        out.append({"strategy": "name", "css": f'{tag}[name="{v}"]', "xpath": f"//{tag}[@name='{v}']", "playwright": f"locator('{tag}[name=\"{v}\"]')",
                    "unique": count(lambda x: x["tag"] == tag and x["attrs"].get("name") == v) == 1})
    fcn = a.get("formControlName") or a.get("formcontrolname")
    if fcn:
        out.append({"strategy": "form control", "css": f'[formcontrolname="{fcn}"]', "xpath": f"//*[@formcontrolname='{fcn}']",
                    "playwright": f"locator('[formcontrolname=\"{fcn}\"]')",
                    "unique": count(lambda x: (x["attrs"].get("formControlName") or x["attrs"].get("formcontrolname")) == fcn) == 1})
    if el.get("aria_label"):
        v = el["aria_label"]
        out.append({"strategy": "aria label", "css": f'[aria-label="{v}"]', "xpath": f"//*[@aria-label='{v}']", "playwright": f"getByLabel('{v}')",
                    "unique": count(lambda x: x.get("aria_label") == v) == 1})
    if el.get("label") and tag in ("input", "select", "textarea"):
        v = el["label"]
        out.append({"strategy": "label", "css": None, "xpath": f"//label[normalize-space()='{v}']/following::{tag}[1]", "playwright": f"getByLabel('{v}')",
                    "unique": count(lambda x: x.get("label") == v) == 1})
    if el.get("text") and tag in ("button", "a"):
        v = el["text"]
        role = "link" if tag == "a" else "button"
        out.append({"strategy": "text", "css": None, "xpath": f"//{tag}[normalize-space()='{v}']", "playwright": f"getByRole('{role}', {{ name: '{v}' }})",
                    "unique": count(lambda x: x["tag"] == tag and x.get("text") == v) == 1})
    if el.get("placeholder"):
        v = el["placeholder"]
        out.append({"strategy": "placeholder", "css": f'{tag}[placeholder="{v}"]', "xpath": f"//{tag}[@placeholder='{v}']", "playwright": f"getByPlaceholder('{v}')",
                    "unique": count(lambda x: x.get("placeholder") == v) == 1})
    n = 1 + sum(1 for x in page[:page.index(el)] if x["tag"] == tag)
    out.append({"strategy": "position (fragile)", "css": f"{tag}:nth-of-type({n})", "xpath": f"(//{tag})[{n}]", "playwright": f"locator('{tag}').nth({n - 1})",
                "unique": True})
    return out


def assemble_pages(facts: list, repo: str) -> list:
    templates = {f["file"]: f for f in facts if f["kind"] == "template"}
    comps = [f for f in facts if f["kind"] == "component"]
    route_of = {}
    for f in facts:
        if f["kind"] == "route":
            route_of.setdefault(f["component"], f["path"])
    pages, used = [], set()
    for c in comps:
        t = templates.get(c["template_file"] or "")
        if t is None:
            continue
        used.add(t["file"])
        pages.append(_page(repo, t, c, route_of.get(c["class"] or "")))
    for f, t in sorted(templates.items()):
        if f not in used:
            pages.append(_page(repo, t, None, None))
    pages.sort(key=lambda p: (p["route"] or "~", p["page"]))
    return pages


def _page(repo: str, t: dict, comp: Optional[dict], route: Optional[str]) -> dict:
    els = [dict(e, attrs=dict(e["attrs"]), constraints=dict(e["constraints"])) for e in t["elements"]]
    controls, arrs = (comp or {}).get("controls", {}), (comp or {}).get("arrays", {})
    out = []
    for e in els:
        rule = controls.get(e.get("name") or "")
        if rule:
            if rule["mandatory"] and not e["mandatory"]:
                e["mandatory"], e["mandatory_source"] = True, "form validator (Validators.required)"
            e["constraints"] = {**rule["constraints"], **e["constraints"]}
        if e["tag"] == "select":
            dyn = e.pop("_dynamic_options", [])
            if dyn:
                src = dyn[0]
                m = re.search(r"\bof\s+([\w$.]+)|\bin\s+([\w$.]+)|([\w$]+)\s*\.\s*map\s*\(", src or "")
                name = next((g for g in (m.groups() if m else []) if g), None)
                key = (name or "").split(".")[-1]
                if key in arrs:
                    e["options"] += [{"value": v, "text": str(v), "line": None} for v in arrs[key]]
                    e["options_source"] = f"literal list '{key}' in the component"
                else:
                    e["options_source"] = f"dynamic: {name or src} (filled at run time; values not in the source)"
            else:
                e["options_source"] = "static options in the template"
        out.append(e)
    # JSX lists built with ARRAY.map(...) inside a <select> are expressions, not <option> tags: look for them in the component source
    for e in out:
        e["locators"] = _locators(e, out)
        best = next((l for l in e["locators"] if l["unique"] and l["strategy"] != "position (fragile)"), e["locators"][-1])
        e["locator"] = best
        if e.get("repeated"):
            e["locator_note"] = "repeated for each item of a list: unique only within its row"
        e.pop("aria_label", None)
    name = route or (comp or {}).get("class") or posixpath.basename(t["file"])
    return {"page": name, "route": route, "component": (comp or {}).get("class"), "framework": t["framework"], "repo": repo,
            "files": sorted({t["file"], (comp or {}).get("file") or t["file"]}), "elements": out,
            "api_calls": [{**c, "norm_path": normalize_path(c["path"])} for c in (comp or {}).get("api_calls", [])]}
