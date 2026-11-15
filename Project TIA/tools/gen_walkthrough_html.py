"""Generates docs/DEMO-WALKTHROUGH.html from docs/DEMO-WALKTHROUGH.md: one self-contained page (no external file, no CDN, works offline and
air-gapped) for presenting the demo: contents sidebar with scroll tracking, light/dark theme, syntax-coloured code with copy buttons,
responsive tables, print layout.

    python tools/gen_walkthrough_html.py            # rewrite docs/DEMO-WALKTHROUGH.html
    python tools/gen_walkthrough_html.py --check    # exit 1 if it is out of date (the test suite does this)

Edit the Markdown, never the HTML. The converter covers exactly what the walkthrough uses (headings, paragraphs, fenced code, tables,
numbered and bulleted lists, one blockquote, rules, inline code / bold / italic / links) and refuses a table whose rows do not match its header.
"""
from __future__ import annotations

import re
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, OUT = ROOT / "docs" / "DEMO-WALKTHROUGH.md", ROOT / "docs" / "DEMO-WALKTHROUGH.html"

# ---- inline -----------------------------------------------------------------------------------------------------------------------------
SAFE_SCHEMES = ("http", "https", "mailto")


def link(m) -> str:
    """A Markdown link. Only relative, http(s) and mailto addresses become links; anything else (javascript:, data:) stays plain text."""
    text, url = m.group(1), m.group(2)
    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", url)
    if scheme and scheme.group(1).lower() not in SAFE_SCHEMES:
        return f"{text} ({url})"
    return f'<a href="{escape(url, quote=True)}">{text}</a>'



def inline(text: str) -> str:
    """Inline Markdown to HTML. Code spans are lifted out first so nothing inside them is interpreted; everything else is escaped."""
    spans: list[str] = []

    def lift(m):
        spans.append(f"<code>{escape(m.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"
    s = escape(re.sub(r"`([^`]+)`", lift, text), quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], s)


def plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)


def slug(text: str, used: dict) -> str:
    s = re.sub(r"[^a-z0-9 -]", "", plain(text).lower()).strip().replace(" ", "-")
    s = re.sub(r"-+", "-", s) or "section"
    n = used.get(s, 0)
    used[s] = n + 1
    return s if n == 0 else f"{s}-{n + 1}"


# ---- code highlighting ------------------------------------------------------------------------------------------------------------------
PY_KW = ("def class return if elif else for while in not and or is import from as with try except raise pass continue break lambda yield "
         "async await None True False").split()
SQL_KW = ("alter table enable force row level security create policy on using with check select set_config from where insert into values "
          "update delete grant role login nosuperuser nobypassrls and or not null as").split()
LANGS = {
    "python": (re.compile(r"(?P<c>#[^\n]*)|(?P<s>\"[^\"\n]*\"|'[^'\n]*')|(?P<n>\b\d+(?:\.\d+)?\b)|(?P<k>\b(?:" + "|".join(PY_KW) + r")\b)"), 1),
    "json": (re.compile(r"(?P<key>\"(?:[^\"\\\n]|\\.)*\"(?=\s*:))|(?P<s>\"(?:[^\"\\\n]|\\.)*\")|(?P<n>-?\b\d+(?:\.\d+)?\b)|(?P<k>\b(?:true|false|null)\b)"), 1),
    "sql": (re.compile(r"(?P<c>--[^\n]*)|(?P<s>'[^'\n]*')|(?P<k>\b(?:" + "|".join(SQL_KW) + r")\b)", re.I), 1),
}
CLASS = {"c": "t-c", "s": "t-s", "n": "t-n", "k": "t-k", "key": "t-key"}


def highlight(code: str, lang: str) -> str:
    if lang not in LANGS:
        return escape(code, quote=False)
    rx, _ = LANGS[lang]
    out, pos = [], 0
    for m in rx.finditer(code):
        out.append(escape(code[pos:m.start()], quote=False))
        g = m.lastgroup
        out.append(f'<span class="{CLASS[g]}">{escape(m.group(0), quote=False)}</span>')
        pos = m.end()
    out.append(escape(code[pos:], quote=False))
    return "".join(out)


# ---- blocks -----------------------------------------------------------------------------------------------------------------------------
SEP = re.compile(r"^\|[\s:|-]+\|\s*$")
STATUS_OK = ("verified (live", "built", "measured")
STATUS_WARN = ("verified (simulated",)
STATUS_BAD = ("not ", "unverified")


def split_row(line: str) -> list[str]:
    """Cells of a table row; a pipe inside a code span does not split."""
    cells, cur, tick = [], [], False
    for ch in line.strip().strip("|") if line.strip().endswith("|") else line.strip().lstrip("|"):
        if ch == "`":
            tick = not tick
        if ch == "|" and not tick:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    cells.append("".join(cur).strip())
    return cells


def status_class(cell_html: str) -> str:
    t = plain(cell_html).strip().lower()
    for words, cls in ((STATUS_BAD, "st-bad"), (STATUS_WARN, "st-warn"), (STATUS_OK, "st-ok")):
        if t.startswith(words):
            return f' class="{cls}"'
    return ""


def table(lines: list[str]) -> str:
    head, rows = split_row(lines[0]), [split_row(x) for x in lines[2:]]
    for r in rows:
        if len(r) != len(head):
            raise SystemExit(f"table row has {len(r)} cells, header has {len(head)}: {r[0][:60]!r}")
    st_col = next((i for i, h in enumerate(head) if h.strip().lower() == "status"), None)
    th = "".join(f"<th>{inline(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td{status_class(inline(c)) if i == st_col else ''}>{inline(c)}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def convert(md: str) -> tuple[str, list[tuple[int, str, str]], str]:
    """-> (body html, toc [(level, id, text html)], title)"""
    lines, i, out, toc, used, title = md.split("\n"), 0, [], [], {}, ""
    hero_open = False
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            lang, i, code = ln[3:].strip(), i + 1, []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f'<div class="code"><div class="bar{" has-lang" if lang else ""}"><span class="lang">{escape(lang)}</span>'
                       f'<button class="copy" type="button" aria-label="Copy this code">Copy</button></div>'
                       f'<pre><code>{highlight(chr(10).join(code), lang)}</code></pre></div>')
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl, text = len(m.group(1)), inline(m.group(2))
            sid = slug(text, used)
            if lvl == 1:
                title = plain(text)
                out.append(f'<header class="hero" id="top"><h1>{text}</h1>')
                hero_open = True
            else:
                if lvl in (2, 3):
                    toc.append((lvl, sid, text))
                mod = re.match(r"MODULE ([ABC])\b", plain(text))
                cls = f' class="mod mod-{mod.group(1).lower()}"' if mod and lvl == 2 else ""
                out.append(f'<h{lvl} id="{sid}"{cls}>{text}<a class="anchor" href="#{sid}" aria-label="Link to this section">#</a></h{lvl}>')
            i += 1
            continue
        if ln.strip() == "---":
            if hero_open:
                out.append("</header>")
                hero_open = False
            else:
                out.append("<hr>")
            i += 1
            continue
        if ln.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i][1:].strip())
                i += 1
            out.append(f'<blockquote><p>{inline(" ".join(buf))}</p></blockquote>')
            continue
        if ln.startswith("|") and i + 1 < len(lines) and SEP.match(lines[i + 1]):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            out.append(table(lines[i:j]))
            i = j
            continue
        m = re.match(r"^(\d+\.|-)\s+(.*)$", ln)
        if m:
            tag, items = ("ol", []) if m.group(1)[0].isdigit() else ("ul", [])
            while i < len(lines):
                mm = re.match(r"^(\d+\.|-)\s+(.*)$", lines[i])
                if not mm or mm.group(1)[0].isdigit() != (tag == "ol"):
                    break
                cur = [mm.group(2)]
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and lines[i].strip():
                    cur.append(lines[i].strip())
                    i += 1
                items.append(" ".join(cur))
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
            continue
        if not ln.strip():
            i += 1
            continue
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|```|>|\||---$|\d+\.\s|-\s)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(buf))}</p>")
    if hero_open:
        out.append("</header>")
    return "\n".join(out), toc, title


# ---- page -------------------------------------------------------------------------------------------------------------------------------
CSS = r"""
:root{--bg:#f6f7f9;--card:#fff;--fg:#1a1d21;--mut:#5b6472;--line:#dfe3e8;--acc:#1f4fd8;--accbg:#e8eefc;--code:#f1f3f6;--ok:#127a3b;--warn:#a86400;--bad:#b3261e;
--ma:#1f4fd8;--mb:#0b6e4f;--mc:#8e5bd9;--t-c:#6b7280;--t-s:#0b6e4f;--t-n:#a86400;--t-k:#8e2bc7;--t-key:#1f4fd8;--side:280px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14171c;--card:#1d2127;--fg:#e8eaed;--mut:#9aa3af;--line:#2d333b;--acc:#7aa2ff;--accbg:#1f2a44;--code:#161a20;
--ok:#5fd08a;--warn:#f0b35a;--bad:#ff8f87;--ma:#7aa2ff;--mb:#4cc9a0;--mc:#b794f6;--t-c:#8b95a5;--t-s:#7fd6a8;--t-n:#f0b35a;--t-k:#c792ea;--t-key:#7aa2ff}}
:root[data-theme=dark]{--bg:#14171c;--card:#1d2127;--fg:#e8eaed;--mut:#9aa3af;--line:#2d333b;--acc:#7aa2ff;--accbg:#1f2a44;--code:#161a20;--ok:#5fd08a;--warn:#f0b35a;--bad:#ff8f87;
--ma:#7aa2ff;--mb:#4cc9a0;--mc:#b794f6;--t-c:#8b95a5;--t-s:#7fd6a8;--t-n:#f0b35a;--t-k:#c792ea;--t-key:#7aa2ff}
*{box-sizing:border-box}html{scroll-padding-top:16px}
body{margin:0;background:var(--bg);color:var(--fg);font:15.5px/1.6 system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--acc)}
.layout{display:grid;grid-template-columns:var(--side) minmax(0,1fr);min-height:100vh}
nav.toc{position:sticky;top:0;align-self:start;height:100vh;overflow-y:auto;padding:18px 14px 28px;border-right:1px solid var(--line);background:var(--card);font-size:13.5px}
nav.toc .brand{font-weight:700;font-size:15px;margin:0 6px 2px}nav.toc .sub{color:var(--mut);margin:0 6px 12px;font-size:12.5px}
nav.toc ol{list-style:none;margin:0;padding:0}nav.toc li{margin:0}
nav.toc a{display:block;padding:4px 8px;border-radius:6px;color:var(--fg);text-decoration:none;border-left:3px solid transparent}
nav.toc a:hover{background:var(--accbg)}nav.toc a.on{background:var(--accbg);border-left-color:var(--acc);font-weight:600}
nav.toc ol ol{margin:0 0 4px 12px}nav.toc ol ol a{color:var(--mut);padding:2px 8px;font-size:12.5px}nav.toc ol ol a.on{color:var(--fg)}
nav.toc .m-a>a{border-left-color:var(--ma)}nav.toc .m-b>a{border-left-color:var(--mb)}nav.toc .m-c>a{border-left-color:var(--mc)}
main{max-width:1020px;width:100%;padding:28px 36px 80px;margin:0 auto}
.hero{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 26px 10px;margin-bottom:22px}
.hero h1{font-size:27px;line-height:1.25;margin:0 0 10px}.hero p{margin:6px 0}
.tools{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 12px}
button,.btn{font:inherit;font-size:13px;padding:5px 12px;border-radius:7px;border:1px solid var(--acc);background:transparent;color:var(--acc);cursor:pointer;text-decoration:none}
button:hover,.btn:hover{background:var(--accbg)}
h2{font-size:22px;margin:42px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--line)}
h2.mod{border-bottom-width:3px}h2.mod-a{border-bottom-color:var(--ma)}h2.mod-b{border-bottom-color:var(--mb)}h2.mod-c{border-bottom-color:var(--mc)}
h3{font-size:17.5px;margin:30px 0 8px}
.anchor{opacity:0;margin-left:8px;color:var(--mut);text-decoration:none;font-weight:400}h2:hover .anchor,h3:hover .anchor{opacity:1}
p{margin:10px 0}hr{border:0;border-top:1px solid var(--line);margin:30px 0}
blockquote{margin:14px 0;padding:2px 16px;border-left:4px solid var(--warn);background:var(--card);border-radius:0 8px 8px 0}
code{font:12.8px/1.45 ui-monospace,SFMono-Regular,Consolas,Menlo,monospace;background:var(--code);padding:1px 3px;border-radius:4px;overflow-wrap:anywhere}
.code{margin:12px 0;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--code)}
.code .bar{display:none;align-items:center;justify-content:space-between;min-height:30px;padding:2px 8px 2px 14px;border-bottom:1px solid var(--line);background:var(--card);font-size:11px;color:var(--mut)}
.js .code .bar,.code .bar.has-lang{display:flex}
.code .lang{text-transform:uppercase;letter-spacing:.05em}
.code .copy{display:none;padding:1px 9px;font-size:12px}.js .code .copy{display:inline-block}
.code pre{margin:0;padding:14px 16px;background:var(--code);overflow-x:auto;tab-size:4}
.code pre code{background:none;padding:0;font-size:12.6px;line-height:1.5;white-space:pre;overflow-wrap:normal;display:block}
.t-c{color:var(--t-c);font-style:italic}.t-s{color:var(--t-s)}.t-n{color:var(--t-n)}.t-k{color:var(--t-k);font-weight:600}.t-key{color:var(--t-key)}
.tw{overflow-x:auto;margin:14px 0;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;vertical-align:top;padding:8px 11px;border-bottom:1px solid var(--line)}
th{background:var(--code);font-size:12.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--mut);white-space:nowrap}
tr:last-child td{border-bottom:0}tbody tr:hover td{background:var(--accbg)}td code{font-size:12.3px}
td.st-ok{color:var(--ok);font-weight:600}td.st-warn{color:var(--warn);font-weight:600}td.st-bad{color:var(--bad);font-weight:600}
ol,ul{padding-left:24px}li{margin:6px 0}
footer{margin-top:50px;color:var(--mut);font-size:12.5px;border-top:1px solid var(--line);padding-top:12px}
.menu{display:none}
@media (max-width:1000px){
  .layout{display:block}nav.toc{position:static;height:auto;max-height:none;border-right:0;border-bottom:1px solid var(--line)}
  .js nav.toc{display:none}.js nav.toc.open{display:block}.js .menu{display:inline-block}
  main{padding:18px 14px 60px}h2{font-size:20px}.hero{padding:16px}.hero h1{font-size:22px}}
@media print{
  :root{--bg:#fff;--card:#fff;--fg:#000;--mut:#444;--line:#bbb;--code:#f3f3f3;--accbg:#fff}
  nav.toc,.tools,.copy,.anchor,.menu{display:none!important}.layout{display:block}main{max-width:none;padding:0}
  body{font-size:11pt;background:#fff}.hero,.tw,.code{border-color:#999}.code .copy{display:none!important}
  h2,h3{break-after:avoid}.tw,.code,blockquote,tr{break-inside:avoid}.code pre{white-space:pre-wrap;overflow:visible}
  a{color:inherit;text-decoration:none}td.st-ok,td.st-warn,td.st-bad{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""

JS = r"""
(function () {
  var root = document.documentElement; root.classList.add("js");
  try { var t = localStorage.getItem("tia-walk-theme"); if (t) root.setAttribute("data-theme", t); } catch (e) {}
  document.getElementById("theme").onclick = function () {
    var dark = root.getAttribute("data-theme") ? root.getAttribute("data-theme") === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    var next = dark ? "light" : "dark"; root.setAttribute("data-theme", next);
    try { localStorage.setItem("tia-walk-theme", next); } catch (e) {}
  };
  document.getElementById("print").onclick = function () { window.print(); };
  var toc = document.getElementById("toc"), menu = document.getElementById("menu");
  menu.onclick = function () { toc.classList.toggle("open"); };
  toc.addEventListener("click", function (e) { if (e.target.tagName === "A") toc.classList.remove("open"); });
  document.querySelectorAll(".copy").forEach(function (b) {
    b.onclick = function () {
      var pre = b.closest(".code").querySelector("pre"), text = pre.innerText;
      var done = function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1400); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () {});
      else { var r = document.createRange(); r.selectNodeContents(pre); var s = getSelection(); s.removeAllRanges(); s.addRange(r); try { document.execCommand("copy"); done(); } catch (e) {} }
    };
  });
  var links = {}; document.querySelectorAll("nav.toc a").forEach(function (a) { links[a.getAttribute("href").slice(1)] = a; });
  var heads = [].slice.call(document.querySelectorAll("main h2[id], main h3[id]"));
  function spy() {
    var cur = heads[0], y = window.scrollY + 90;
    heads.forEach(function (h) { if (h.getBoundingClientRect().top + window.scrollY <= y) cur = h; });
    Object.keys(links).forEach(function (k) { links[k].classList.toggle("on", cur && k === cur.id); });
    var on = cur && links[cur.id];
    if (on && toc.scrollHeight > toc.clientHeight && getComputedStyle(toc).position === "sticky") {      // keep the active entry in view: scroll the sidebar only, never the page
      var tr = toc.getBoundingClientRect(), r = on.getBoundingClientRect();
      if (r.top < tr.top + 40 || r.bottom > tr.bottom - 40) toc.scrollTop += (r.top - tr.top) - toc.clientHeight / 2;
    }
  }
  var tick = false; addEventListener("scroll", function () { if (!tick) { tick = true; requestAnimationFrame(function () { tick = false; spy(); }); } }, {passive: true});
  spy();
})();
"""


def build(md: str, brand: str = "Demo walkthrough", source: str = "docs/DEMO-WALKTHROUGH.md", generator: str = "tools/gen_walkthrough_html.py",
          figures: dict | None = None, extra_css: str = "", note: str = "Edit the Markdown, then run the generator.") -> str:
    """The page for one Markdown document. Other generated documents reuse it: `figures` maps a name to trusted SVG markup that replaces the
    Markdown line  <!-- figure: name -->  (invisible when the Markdown is read on its own); an unused or unknown figure is refused."""
    body, toc, title = convert(md)
    for name, svg in (figures or {}).items():
        marker = f"<p>&lt;!-- figure: {name} --&gt;</p>"
        if body.count(marker) != 1:
            raise SystemExit(f"figure {name!r} must be placed exactly once in the Markdown")
        body = body.replace(marker, f'<figure class="fig" id="fig-{name}">{svg}</figure>')
    if "&lt;!-- figure:" in body:
        raise SystemExit("the Markdown places a figure the generator does not provide")
    items, depth_open = [], False
    for lvl, sid, text in toc:
        mod = re.match(r"MODULE ([ABC])\b", plain(text))
        if lvl == 2:
            if depth_open:
                items.append("</ol></li>")
            items.append(f'<li{" class=m-" + mod.group(1).lower() if mod else ""}><a href="#{sid}">{text}</a><ol>')
            depth_open = True
        else:
            items.append(f'<li><a href="#{sid}">{text}</a></li>')
    if depth_open:
        items.append("</ol></li>")
    nav = f'<nav class="toc" id="toc" aria-label="Contents"><p class="brand">{escape(brand)}</p><p class="sub">Contents</p><ol>{"".join(items)}</ol></nav>'
    tools = ('<div class="tools"><button id="menu" class="menu" type="button">Contents</button>'
             '<button id="theme" type="button">Light / dark</button><button id="print" type="button">Print / save as PDF</button></div>')
    body = body.replace("</header>", tools + "</header>", 1)
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f"<title>{escape(title)}</title><style>{CSS}{extra_css}</style></head>\n"
            f'<body><div class="layout">{nav}<main id="main">\n{body}\n'
            f'<footer>Generated from <code>{escape(source)}</code> by <code>{escape(generator)}</code>. {escape(note)}</footer>'
            f"</main></div><script>{JS}</script></body></html>\n")


def main() -> int:
    page = build(SRC.read_text(encoding="utf-8"))
    if "--check" in sys.argv:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == page
        print("up to date" if ok else "docs/DEMO-WALKTHROUGH.html is out of date: run  python tools/gen_walkthrough_html.py")
        return 0 if ok else 1
    OUT.write_text(page, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(page) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
