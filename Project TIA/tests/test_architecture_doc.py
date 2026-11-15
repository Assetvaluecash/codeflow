"""docs/ARCHITECTURE-AS-BUILT.md and .html are generated from the code (tools/gen_architecture.py): current, complete, and the layer rule they
describe actually holds for the imports in tia/."""
import copy
import re
from html.parser import HTMLParser

import pytest

from tools import gen_architecture as ga

MODS, TABLES = ga.read_modules(), ga.read_tables()


def test_the_documents_are_current():
    md, html = ga.build()
    assert ga.OUT_MD.read_text(encoding="utf-8") == md, "run  python tools/gen_architecture.py"
    assert ga.OUT_HTML.read_text(encoding="utf-8") == html, "run  python tools/gen_architecture.py"


def test_every_module_is_in_exactly_one_layer_and_the_layer_rule_holds():
    placed = [m for *_, members in ga.LAYERS for m in members]
    assert sorted(placed) == sorted(MODS) and len(placed) == len(set(placed))
    assert {"api", "impact", "lang/typescript", "__init__"} <= set(placed)
    assert ga.check(MODS, TABLES) == sorted(ga.EXCEPTIONS)                    # the only upward imports are the recorded ones


def test_the_impact_engine_cannot_reach_connectors_or_entry_points():
    where = ga.layer_of()
    def reach(start):
        seen, todo = set(), [start]
        while todo:
            for n in MODS[todo.pop()]["uses"]:
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        return seen
    banned = {m for m, i in where.items() if i <= where["webhooks"] and m not in ("worker",)}       # entry, surfaces, orchestration, connectors
    leaked = reach("impact") & banned
    # the only way up is the recorded `store -> repos` record-type import; nothing reaches a git host, ITSM or the API from the engine
    assert leaked <= {"repos", "upload"}, leaked
    assert not reach("impact") & {"api", "github", "githubapp", "publish", "scmhosts", "ado", "itsm", "webhooks", "analysis_service"}


def test_a_new_upward_import_is_refused():
    mods = copy.deepcopy(MODS)
    mods["models"]["uses"].add("api")
    with pytest.raises(SystemExit, match="models -> api"):
        ga.check(mods, TABLES)


def test_an_unplaced_module_is_refused():
    mods = copy.deepcopy(MODS)
    mods["brand_new"] = {"uses": set(), "used_by": [], "lines": 1, "doc": "", "path": "tia/brand_new.py"}
    with pytest.raises(SystemExit, match="brand_new"):
        ga.check(mods, TABLES)


def test_a_flow_naming_something_that_does_not_exist_is_refused(monkeypatch):
    monkeypatch.setattr(ga, "FLOWS", [("x", ["`api` then `no_such_module`"])])
    with pytest.raises(SystemExit, match="no_such_module"):
        ga.check(MODS, TABLES)


def test_storage_and_deployment_are_complete():
    md = ga.OUT_MD.read_text(encoding="utf-8")
    for t in TABLES:
        assert f"| `{t['table']}` | `{t['migration']}` |" in md
    assert sum(t["rls"] for t in TABLES) >= 20 and not [t for t in TABLES if t["table"] == "analyses" and not t["rls"]]
    for f in ga.deploy_files():
        assert f"[`{f}`]" in md
    for name, m in MODS.items():
        assert f"](../{m['path']})" in md


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, k, v) for k, v in attrs]


def test_the_page_is_self_contained_with_both_figures():
    html = ga.OUT_HTML.read_text(encoding="utf-8")
    p = Tags()
    p.feed(html)
    assert p.tags.count("svg") == 2 and p.tags.count("script") == 1
    assert not [1 for t, k, v in p.attrs if k in ("src", "srcset", "action")]
    assert not [v for t, k, v in p.attrs if k == "href" and re.match(r"^(https?:)?//", v)]
    assert set(re.findall(r"url\(([^)]*)\)", html)) == {"#ah"}
    assert "&lt;!-- figure" not in html and "<!-- figure" not in html
