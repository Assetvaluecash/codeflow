"""docs/DEMO-WALKTHROUGH.html is generated from docs/DEMO-WALKTHROUGH.md (tools/gen_walkthrough_html.py): current, complete, self-contained, safe."""
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tools import gen_walkthrough_html as gw

DOCS = Path(__file__).resolve().parent.parent / "docs"
MD = (DOCS / "DEMO-WALKTHROUGH.md").read_text(encoding="utf-8")
HTML = (DOCS / "DEMO-WALKTHROUGH.html").read_text(encoding="utf-8")


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.text, self.rows, self._row, self._tbl = [], [], [], [], None, 0

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, k, v) for k, v in attrs]
        if tag == "table":
            self._tbl += 1
            self.rows.append([])
        if tag == "tr":
            self._row = 0
        if tag in ("td", "th") and self._row is not None:
            self._row += 1

    def handle_endtag(self, tag):
        if tag == "tr" and self._row is not None:
            self.rows[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        self.text.append(data)


def parse(html):
    p = Page()
    p.feed(html)
    return p


def md_counts():
    inf, fences, h2, h3, tables, prev = False, 0, 0, 0, 0, False
    for line in MD.split("\n"):
        if line.startswith("```"):
            fences += 0 if inf else 1
            inf = not inf
            continue
        if inf:
            continue
        h2 += line.startswith("## ")
        h3 += line.startswith("### ")
        t = line.startswith("|")
        tables += t and not prev
        prev = t
    return {"fences": fences, "h2": h2, "h3": h3, "tables": tables}


def test_the_html_is_current():
    assert HTML == gw.build(MD), "docs/DEMO-WALKTHROUGH.html is out of date: run  python tools/gen_walkthrough_html.py"


def test_everything_in_the_markdown_is_in_the_page():
    p, want = parse(HTML), md_counts()
    assert p.tags.count("pre") == want["fences"] and p.tags.count("table") == want["tables"] and p.tags.count("h1") == 1
    ids = [v for t, k, v in p.attrs if k == "id"]
    assert len(set(ids)) == len(ids)
    heads = [t for t in p.tags if t in ("h2", "h3")]
    assert heads.count("h2") == want["h2"] and heads.count("h3") == want["h3"]
    hrefs = [v for t, k, v in p.attrs if t == "a" and k == "href" and v.startswith("#") and "anchor" not in v]
    assert all(h[1:] in ids for h in hrefs)                                               # the contents and the heading anchors resolve
    for table_rows in p.rows:
        assert len(set(table_rows)) == 1, table_rows                                      # every row has as many cells as its header
    text = "".join(p.text)
    assert "**" not in text and "```" not in text and "|---" not in text and "](" not in text      # no Markdown leaked into the page


def test_the_page_is_self_contained():
    p = parse(HTML)
    assert not [1 for t, k, v in p.attrs if k in ("src", "srcset", "action")]
    assert "link" not in p.tags and "iframe" not in p.tags and "img" not in p.tags
    assert not [v for t, k, v in p.attrs if k == "href" and re.match(r"^(https?:)?//", v)]    # no address leaves the machine
    assert "@import" not in HTML and "url(" not in HTML.replace("url(#", "")
    scripts = [i for i, t in enumerate(p.tags) if t == "script"]
    assert len(scripts) == 1                                                                # the page's own script only


def test_highlighting_never_changes_the_code():
    blocks = re.findall(r"```(\w*)\n(.*?)\n```", MD, re.S)
    assert len(blocks) == md_counts()["fences"]
    for lang, code in blocks:
        out = gw.highlight(code, lang)
        assert unescape(re.sub(r"<[^>]+>", "", out)) == code, lang


def test_nothing_in_the_markdown_can_inject_markup():
    page = gw.build("# T\n\n---\n\n## S\n\n<script>alert(1)</script> and `<b>x</b>` and [x](javascript:alert(1)) and [ok](DEMO-DATA.md)\n\n"
                    "| a | b |\n|---|---|\n| <img src=x onerror=alert(2)> | `p|q` |\n\n```python\nx = '</code><script>alert(3)</script>'\n```\n")
    body = page.split("<main", 1)[1].split("</main>")[0]               # the document's content, not the page's own script after it
    assert body.count("<script") == 0 and "<img" not in body and "<b>x" not in body and "javascript:" not in body.replace("(javascript:alert(1))", "")
    assert 'href="DEMO-DATA.md"' in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "<td><code>p|q</code></td>" in body


def test_a_malformed_table_is_refused_not_rendered_wrong():
    with pytest.raises(SystemExit):
        gw.build("# T\n\n---\n\n| a | b |\n|---|---|\n| only one |\n")
