"""UC1-07: the Java / Spring adapter - extraction facts, cross-language resolution, and impact analysis on real branches."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import fresh_store, make_app
from fixtures.build_java_fixture import build as build_java
from tia import gitutil
from tia.extract_java import extract_file, module_fqn
from tia.extract_python import extract_repo
from tia.extraction import extract_commit
from tia.graph import build_graph
from tia.impact import AnalysisRequest, _extract, analyze


@pytest.fixture(scope="module")
def jfx(tmp_path_factory):
    return build_java(str(tmp_path_factory.mktemp("jlandscape")))


def run(jfx, target, head, store=None, base="main"):
    return analyze(AnalysisRequest(repos=jfx["paths"], target=target, base=base, head=head), store)


def comps(rep):
    return {f["component"] for f in rep["findings"]}


def parse(src, path="src/main/java/com/acme/X.java", service="svc"):
    return extract_file("repo", service, path, src)


# ---- facts extracted from real Spring code -----------------------------------------------------------------------------
def test_spring_provider_facts(jfx):
    ex, _ = extract_commit("orders-service", jfx["paths"]["orders-service"], "main", None)
    assert not ex.files_failed and ex.files_analysed == 12
    eps = {e.id: e for e in ex.endpoints}
    assert set(eps) == {"orders-service:GET /api/orders", "orders-service:GET /api/orders/{}",
                        "orders-service:POST /api/orders", "orders-service:POST /api/orders/{}/cancel"}
    get = eps["orders-service:GET /api/orders/{}"]
    assert get.handler == "OrderController.getOrder" and get.response_schema == "OrderDto" and get.path == "/api/orders/{id}"
    assert get.file.endswith("OrderController.java") and get.line > 0 and get.handler_id.endswith("::OrderController.getOrder")
    assert eps["orders-service:GET /api/orders"].response_schema == "OrderDto"             # List<OrderDto> -> element
    assert eps["orders-service:POST /api/orders"].request_schemas == ["CreateOrderRequest"]
    assert eps["orders-service:POST /api/orders/{}/cancel"].method == "POST"               # @RequestMapping(method = POST)
    schemas = {s.name: s for s in ex.schemas}
    assert set(schemas) == {"OrderDto", "CreateOrderRequest"}                               # PaymentStatus / StockLevel are not exposed
    assert schemas["OrderDto"].fields == {"id": {"type": "String", "required": True}, "status": {"type": "String", "required": True},
                                          "total": {"type": "double", "required": True}}
    assert schemas["CreateOrderRequest"].fields["note"]["required"] is False and schemas["CreateOrderRequest"].fields["customer"]["required"]
    assert ex.non_python_files == 0                                                          # pom.xml is a build file, Java is supported


def test_outbound_call_facts(jfx):
    ex, _ = extract_commit("orders-service", jfx["paths"]["orders-service"], "main", None)
    by_file = {c.file.rsplit("/", 1)[-1]: c for c in ex.callsites}
    pay = by_file["PaymentGateway.java"]                                                     # RestTemplate + @Value base
    assert (pay.method, pay.norm_path, pay.base_kind, pay.base_value) == ("GET", "/payments/{}/status", "config_key", "payments.base-url")
    feign = by_file["StockChecker.java"]                                                     # OpenFeign interface in another file
    assert (feign.method, feign.norm_path, feign.base_kind, feign.base_value) == ("GET", "/inventory/{}", "host", "inventory-service")
    web = by_file["NotifyClient.java"]                                                       # WebClient base set in the constructor
    assert (web.method, web.norm_path, web.base_kind, web.base_value) == ("POST", "/notify/{}", "config_key", "notify.url")
    assert pay.function_id.endswith("::PaymentGateway.status") and len(ex.callsites) == 3
    assert [c["type"] for c in ex.clients] == ["InventoryClient"] and ex.typed_calls == []

    rep_ex, _ = extract_commit("reporting-service", jfx["paths"]["reporting-service"], "main", None)
    direct = next(c for c in rep_ex.callsites if c.file.endswith("OrdersReport.java"))
    assert (direct.method, direct.norm_path, direct.base_kind, direct.base_value) == ("GET", "/api/orders/{}", "host", "orders-service")
    shared = next(c for c in rep_ex.callsites if c.file.endswith("SharedClientReport.java"))
    assert (shared.client_pkg, shared.client_attr, shared.norm_path) == ("com.acme.orders.client", "getOrder", None)   # R1


def test_tests_and_functions(jfx):
    ex, _ = extract_commit("orders-service", jfx["paths"]["orders-service"], "main", None)
    tests = {t.name: t for t in ex.tests}
    assert tests["getsAnOrder"].http_calls == [["GET", "/api/orders/1"]]                    # MockMvc with a static import
    assert "com.acme.orders.OrderService" in tests["findsAnOrder"].imports and "find" in tests["findsAnOrder"].called
    quals = {f.qualname for f in ex.functions}
    assert {"OrderController.getOrder", "OrderService.find", "PaymentGateway.status", "OrderController.<init>"} <= quals
    assert not any("Test" in f.qualname for f in ex.functions)                               # @Test methods are tests, not functions
    assert module_fqn("src/main/java/com/acme/orders/OrderService.java") == "com.acme.orders.OrderService"


def test_incremental_extraction_equals_a_full_rescan_for_java(jfx):
    store = fresh_store()
    p = jfx["paths"]["orders-service"]
    extract_commit("orders-service", p, "main", store)
    head = gitutil.resolve_rev(p, "impl")                                                    # one Java file differs
    inc, stats = extract_commit("orders-service", p, head, store)
    assert stats["parsed"] == 1 and stats["reused"] == stats["files"] - 1
    full = extract_repo("orders-service", head, gitutil.read_tree(p, head))
    assert inc.to_dict() == full.to_dict()
    gi, gf = build_graph({"orders-service": inc}), build_graph({"orders-service": full})
    assert gi.nodes == gf.nodes and [dataclasses.asdict(e) for e in gi.edges] == [dataclasses.asdict(e) for e in gf.edges]


# ---- resolution across languages --------------------------------------------------------------------------------------------
def test_cross_language_and_cross_repository_relationships(jfx):
    store = fresh_store()
    extracts = {n: _extract(n, p, "main", store, ()) for n, p in jfx["paths"].items()}
    g = build_graph(extracts)
    edges = {(g.nodes[e.src]["repo"], g.nodes[e.dst]["repo"], g.nodes[e.dst]["path"], e.strategy)
             for e in g.edges if e.rel == "CONSUMES_API"}
    assert edges == {("orders-service", "payments-service", "/payments/{}/status", "R2"),        # Java -> Python
                     ("orders-service", "inventory-service", "/inventory/{}", "R2"),             # Java -> Java through OpenFeign
                     ("checkout-bff", "orders-service", "/api/orders/{}", "R2"),                 # Python -> Java
                     ("reporting-service", "orders-service", "/api/orders/{}", "R2"),            # Java -> Java, literal host
                     ("reporting-service", "orders-service", "/api/orders/{}", "R1")}            # Java -> Java, shared client library
    assert [u["file"].rsplit("/", 1)[-1] for u in g.unresolved] == ["NotifyClient.java"]         # notifications-java is not in scope


# ---- impact analysis on real branches ----------------------------------------------------------------------------------------------
def test_breaking_dto_change_reaches_python_and_java_consumers(jfx):
    rep = run(jfx, "orders-service", "breaking")
    assert rep["change"]["change_type"] == "api_change" and rep["status"] == "complete_with_gaps"
    kinds = {c["change"] for c in rep["change"]["changed_components"]}
    assert {"schema_field_removed", "schema_field_added"} <= kinds
    assert any("OrderDto.status" in c["detail"] for c in rep["change"]["changed_components"])
    assert comps(rep) == {"checkout-bff", "reporting-service"}
    for f in rep["findings"]:
        assert f["severity"] == "critical" and f["evidence_status"] in ("source_confirmed",) and f["change_nature"] == "breaking"
    strategies = {f.get("resolution_strategy") for f in rep["findings"]}
    assert strategies == {"R1", "R2"}                                                           # shared client and direct call both found
    assert {f["repository"] for f in rep["findings"] if f.get("resolution_strategy") == "R1"} == {"reporting-service"}
    f0 = next(f for f in rep["findings"] if f["component"] == "checkout-bff")
    assert f0["path"][0]["repo"] == "orders-service" and f0["path"][-1]["file"] == "bff.py" and f0["path"][-1]["line"] == 9


def test_removed_endpoint_and_path_change_are_breaking(jfx):
    rem = run(jfx, "orders-service", "removed_endpoint")
    assert any("removed" in c["detail"] and "/api/orders/{id}" in c["detail"] for c in rem["change"]["changed_components"])
    assert comps(rem) == {"checkout-bff", "reporting-service"} and all(f["severity"] == "critical" for f in rem["findings"])
    mv = run(jfx, "orders-service", "path_change")
    assert any("/api/orders" in c["detail"] and "/api/v2/orders" in c["detail"] for c in mv["change"]["changed_components"])
    assert comps(mv) >= {"checkout-bff", "reporting-service"}


def test_implementation_change_reaches_consumers_through_calls_and_lists_tests(jfx):
    rep = run(jfx, "orders-service", "impl")
    assert rep["change"]["change_type"] == "implementation_change"
    downstream = [f for f in rep["findings"] if f["repository"] != "orders-service"]
    assert {f["component"] for f in downstream} == {"checkout-bff", "reporting-service"}         # OrderService.find -> getOrder -> endpoint
    assert {f["severity"] for f in downstream} == {"high"}                                       # non-breaking x critical consumer
    local = [f for f in rep["findings"] if f["repository"] == "orders-service"]
    assert {f["severity"] for f in local} == {"low"} and any(f["node"].endswith("::OrderController.getOrder") for f in local)
    names = {t["name"] for t in rep["change"]["tests_in_changed_repository"]}
    assert {"findsAnOrder", "getsAnOrder"} <= names                                              # same-package test + MockMvc test


def test_python_provider_change_reaches_the_java_consumer_and_beyond(jfx):
    rep = run(jfx, "payments-service", "breaking")
    assert "orders-service" in comps(rep)
    direct = next(f for f in rep["findings"] if f["component"] == "orders-service" and f["node_type"] == "callsite"
                  and f["repository"] == "orders-service")
    assert direct["path"][-1]["file"].endswith("PaymentGateway.java") and direct["severity"] == "critical"
    # and through the Java call chain to the endpoint orders-service exposes, and on to ITS consumers
    assert {"checkout-bff", "reporting-service"} <= comps(rep)


def test_openfeign_consumer_is_found_through_the_interface(jfx):
    rep = run(jfx, "inventory-service", "breaking")
    f = rep["findings"][0]
    assert f["component"] == "orders-service" and f["resolution_strategy"] == "R2" and f["path"][-1]["file"].endswith("StockChecker.java")
    assert any("quantity" in c["detail"] for c in rep["change"]["changed_components"])


def test_tests_only_change_has_no_downstream_impact(jfx):
    rep = run(jfx, "orders-service", "tests_only")
    assert rep["change"]["change_type"] == "test_only" and rep["findings"] == []


def test_a_java_file_that_does_not_parse_is_reported_not_ignored(jfx):
    rep = run(jfx, "orders-service", "syntax_error")
    assert rep["status"] == "partial"
    pf = next(u for u in rep["unknowns"] if u["area"] == "parse_failures")
    assert [i["file"].rsplit("/", 1)[-1] for i in pf["items"]] == ["Broken.java"] and pf["items"][0]["revision"] == "proposed"
    assert pf["items"][0]["error"].startswith("SyntaxError line")


def test_java_analysis_is_deterministic_and_reproducible(jfx, tmp_path):
    a, b = run(jfx, "orders-service", "breaking"), run(jfx, "orders-service", "breaking")
    for r in (a, b):
        r.pop("generated_at")
    assert a == b
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    for n, p in jfx["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    rep = c.post("/api/v1/impact-analyses", json={"target": "orders-service", "head": "breaking", "base": "main"}).json()["report"]
    assert comps(rep) == {"checkout-bff", "reporting-service"}
    assert c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/reproduce").json()["identical"] is True
    graph = c.get("/api/v1/graph?level=endpoint").json()
    assert any(n["label"] == "GET /api/orders/{}" for n in graph["nodes"])
    assert {r["strategy"] for r in c.get("/api/v1/graph/relationships").json()["relationships"]} == {"R1", "R2"}


# ---- parsing details -------------------------------------------------------------------------------------------------------------------------
def eps(src, **kw):
    ex = parse(src, **kw)
    assert not ex.files_failed, ex.files_failed
    return ex


def test_mapping_forms_paths_methods_and_prefixes():
    ex = eps('''package a;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping(path = "/v1")
class C {
    private static final String BASE = "/items";
    @RequestMapping(path = {"/a", "/b"}, method = {RequestMethod.GET, RequestMethod.POST})
    void multi() {}
    @GetMapping(BASE + "/{id}")
    String one() { return ""; }
    @PutMapping
    void bare() {}
    @DeleteMapping(value = "/x", produces = "application/json")
    void del() {}
    @RequestMapping("/default")
    void defaults() {}
}''')
    got = sorted((e.method, e.path) for e in ex.endpoints)
    assert got == [("DELETE", "/v1/x"), ("GET", "/v1/a"), ("GET", "/v1/b"), ("GET", "/v1/default"), ("GET", "/v1/items/{id}"),
                   ("POST", "/v1/a"), ("POST", "/v1/b"), ("PUT", "/v1")]


def test_response_and_request_type_unwrapping():
    ex = eps('''package a;
@RestController
class C {
    @GetMapping("/1") ResponseEntity<List<Foo>> a() { return null; }
    @GetMapping("/2") Mono<Foo> b() { return null; }
    @GetMapping("/3") Optional<Foo> c() { return null; }
    @GetMapping("/4") Foo[] d() { return null; }
    @GetMapping("/5") Flux<Foo> e() { return null; }
    @GetMapping("/6") void f() {}
    @GetMapping("/7") String g() { return ""; }
    @PostMapping("/8") Foo h(@RequestBody List<Bar> body, @RequestBody Baz one) { return null; }
}
class Foo { String a; }
class Bar { String b; }
class Baz { String c; }''')
    by = {e.path: e for e in ex.endpoints}
    assert [by[p].response_schema for p in ("/1", "/2", "/3", "/4", "/5", "/6")] == ["List[Foo]", "Foo", "Foo", "List[Foo]", "List[Foo]", None]
    assert by["/8"].request_schemas == ["Bar", "Baz"]


def test_interfaces_with_mappings_are_providers_but_feign_interfaces_are_clients():
    ex = eps('''package a;
@RequestMapping("/api")
interface PetsApi {
    @GetMapping("/pets/{id}") Pet getPet(@PathVariable String id);
    default void helper() {}
}
@FeignClient(name = "pets", url = "${pets.url}", path = "/v2")
interface PetsClient {
    @GetMapping("/p/{id}") Pet get(@PathVariable String id);
    @PostMapping("/p") Pet create(@RequestBody Pet p);
}''')
    assert [(e.id, e.handler) for e in ex.endpoints] == [("svc:GET /api/pets/{}", "PetsApi.getPet")]
    c = ex.clients[0]
    assert (c["type"], c["base_kind"], c["base_value"]) == ("PetsClient", "config_key", "pets.url")
    assert c["methods"] == {"get": ["GET", "/v2/p/{}"], "create": ["POST", "/v2/p"]}
    assert not any(f.qualname.startswith("PetsClient") for f in ex.functions)


def test_overloads_get_distinct_ids_and_nested_classes_are_qualified():
    ex = eps('''package a;
class Outer {
    void run(int a) {}
    void run(String a) {}
    static class Inner { void go() {} }
}''')
    ids = [f.id for f in ex.functions if f.qualname == "Outer.run"]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert "Outer.Inner.go" in {f.qualname for f in ex.functions}


def test_http_client_variants():
    ex = eps('''package a;
class C {
    private final WebClient web = WebClient.create("http://catalog");
    private final RestClient rc = RestClient.builder().baseUrl("http://billing").build();
    private final RestTemplate rt;
    @Value("${tax.url}") private String taxUrl;
    void go(String id) {
        web.method(HttpMethod.PUT).uri("/items/{id}", id).retrieve();
        web.get().uri(b -> b.path("/lambda/{id}").build(id)).retrieve();
        rc.delete().uri("/bills/" + id).retrieve();
        rt.exchange(taxUrl + "/tax/" + id, HttpMethod.POST, null, String.class);
        rt.getForObject(String.format("%s/fmt/%s", taxUrl, id), String.class);
        rt.getForObject(System.getenv("OTHER_URL") + "/env", String.class);
        rt.getForObject(UriComponentsBuilder.fromHttpUrl(taxUrl).path("/x").toUriString(), String.class);
        rt.postForObject(someUrl(), null, String.class);
    }
}''')
    got = [(c.method, c.norm_path, c.base_kind, c.base_value) for c in ex.callsites]
    assert got[0] == ("PUT", "/items/{}", "host", "catalog")
    assert got[1] == ("GET", "/lambda/{}", "host", "catalog")
    assert got[2] == ("DELETE", "/bills/{}", "host", "billing")
    assert got[3] == ("POST", "/tax/{}", "config_key", "tax.url")
    assert got[4] == (("GET", "/fmt/{}", "config_key", "tax.url"))
    assert got[5] == ("GET", "/env", "config_key", "OTHER_URL")
    assert got[6][1] is None and "not statically resolvable" in ex.callsites[6].reason            # honest about what it cannot see
    assert got[7][1] is None and len(got) == 8


def test_modern_java_syntax_parses():
    ex = eps('''package a;
sealed interface Shape permits Circle {}
record Circle(double r) implements Shape {}
class M {
    String t = """
        text block
        """;
    int f(Object o) {
        var x = new ArrayList<String>();
        return switch (o) { case String s -> s.length(); case Integer i when i > 3 -> i; default -> 0; };
    }
}''')
    assert ex.files_analysed == 1 and {f.qualname for f in ex.functions} == {"M.f"}


def test_syntax_errors_are_failures_with_a_line_number():
    ex = parse("package a;\nclass C {\n  void f( {\n  }\n}\n")
    assert ex.files_failed == [{"file": "src/main/java/com/acme/X.java", "error": "SyntaxError line 3"}] and ex.files_analysed == 0


def test_junit4_and_webtestclient_tests():
    ex = eps('''package a;
import org.junit.Test;
class PetsIT {
    private WebTestClient client;
    private TestRestTemplate rest;
    @Test public void lists() { client.get().uri("/pets").exchange(); rest.getForObject("/pets/1", String.class); }
    public void helper() {}
}''', path="src/test/java/a/PetsIT.java")
    [t] = ex.tests
    assert t.name == "lists" and sorted(t.http_calls) == [["GET", "/pets"], ["GET", "/pets/{}".replace("{}", "1")]]
    assert [f.qualname for f in ex.functions] == ["PetsIT.helper"]


# ---- regressions found by running the adapter on real third-party code (spring-petclinic-microservices) --------------------------
def test_webclient_builder_chain_with_a_mutable_literal_base_field():
    ex = eps('''package a;
class VisitsClient {
    private String hostname = "http://visits-service/";
    private final WebClient.Builder webClientBuilder;
    VisitsClient(WebClient.Builder b) { this.webClientBuilder = b; }
    Object get(String id) {
        return webClientBuilder.build().get().uri(hostname + "pets/visits?petId={petId}", id).retrieve();
    }
    Object owner(int id) {
        return webClientBuilder.build().get().uri("http://customers-service/owners/{ownerId}", id).retrieve();
    }
}''')
    got = [(c.method, c.norm_path, c.base_kind, c.base_value) for c in ex.callsites]
    assert got == [("GET", "/pets/visits", "host", "visits-service"), ("GET", "/owners/{}", "host", "customers-service")]


def test_rest_template_builder_chain():
    ex = eps('''package a;
class C {
    private final RestTemplateBuilder builder;
    C(RestTemplateBuilder b) { this.builder = b; }
    Object go() { return builder.build().getForObject("http://billing/bills", Object.class); }
}''')
    assert [(c.method, c.norm_path, c.base_value) for c in ex.callsites] == [("GET", "/bills", "billing")]


def test_entities_are_exposed_schemas_with_inherited_fields_and_json_naming():
    ex = extract_repo("repo", "c", {"src/main/java/a/Owners.java": '''package a;
@RestController
class OwnerResource {
    @GetMapping("/owners/{id}") Optional<Owner> find(@PathVariable int id) { return null; }
    @GetMapping("/owners") List<Owner> all() { return null; }
}
@MappedSuperclass class BaseEntity { @Id Integer id; }
@Entity @Table(name = "owners")
class Owner extends BaseEntity {
    @NotBlank private String firstName;
    @JsonProperty("last_name") private String lastName;
    @JsonIgnore private String secret;
    @OneToMany private Set<Pet> pets;
    private static final long serialVersionUID = 1L;
}
@Entity class Pet { private String name; }
class Unrelated { private String x; }'''})
    schemas = {s.name: s for s in ex.schemas}
    assert set(schemas) == {"Owner", "Pet"}                    # the base class is merged into Owner; Unrelated is not reachable from an endpoint
    assert schemas["Owner"].fields == {"id": {"type": "Integer", "required": False}, "firstName": {"type": "String", "required": True},
                                       "last_name": {"type": "String", "required": False}, "pets": {"type": "Set<Pet>", "required": False}}
    assert {e.response_schema for e in ex.endpoints} == {"Owner", "List[Owner]"[5:-1]}


def test_class_and_method_paths_join_with_exactly_one_slash():
    ex = eps('''package a;
@RestController
@RequestMapping("/api/gateway/")
class G {
    @GetMapping(value = "owners/{id}") void a() {}
    @GetMapping("/lead") void b() {}
    @GetMapping void c() {}
}''')
    assert sorted(e.path for e in ex.endpoints) == ["/api/gateway/", "/api/gateway/lead", "/api/gateway/owners/{id}"]


APP_YML = "spring:\n  application:\n    name: customers-service\n"


@pytest.mark.parametrize("path,text,expected", [
    ("src/main/resources/application.yml", APP_YML, "customers-service"),
    ("src/main/resources/application.yaml", "---\nfoo: 1\n---\nspring:\n  application:\n    name: second-doc\n", "second-doc"),
    ("src/main/resources/application.properties", "server.port=1\nspring.application.name = props-service  # c\n", "props-service"),
    ("bootstrap.yml", "spring.application.name: flat-key\n", "flat-key"),
    ("src/main/resources/application.yml", "spring:\n  application:\n    name: ${APP_NAME}\n", None),          # placeholder: unknown
    ("src/test/resources/application.yml", APP_YML, None),                                                       # test resources never name a service
    ("src/main/resources/application.yml", ": : not yaml [", None),
])
def test_spring_application_name_is_the_default_service_name(path, text, expected):
    from tia.extract_python import read_service_map
    assert read_service_map({path: text}).get("service") == expected


def test_tia_services_yaml_wins_over_the_spring_name():
    from tia.extract_python import read_service_map
    got = read_service_map({"tia-services.yaml": "service: from-tia\n", "src/main/resources/application.yml": APP_YML})
    assert got["service"] == "from-tia"


def test_spring_name_flows_into_entities_and_the_incremental_path_equals_a_full_rescan(tmp_path):
    p = tmp_path / "svc"
    p.mkdir()
    conftest.git(p, "init", "-q", "-b", "main")
    (p / "src/main/resources").mkdir(parents=True)
    (p / "src/main/resources/application.yml").write_text(APP_YML)
    (p / "src/main/java/a").mkdir(parents=True)
    (p / "src/main/java/a/C.java").write_text('package a;\n@RestController\nclass C {\n  @GetMapping("/x") String x() { return ""; }\n}\n')
    conftest.git(p, "add", "-A")
    conftest.git(p, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    store = fresh_store()
    sha = gitutil.resolve_rev(str(p), "main")
    ex, _ = extract_commit("svc", str(p), sha, store)
    assert ex.service == "customers-service" and [e.id for e in ex.endpoints] == ["customers-service:GET /x"]
    full = extract_repo("svc", sha, gitutil.read_tree(str(p), sha))
    assert ex.to_dict() == full.to_dict()


def test_unsupported_http_client_libraries_are_disclosed_not_read_as_no_consumers(tmp_path):
    from tia.report import redact

    def mk(name, files):
        d = tmp_path / name
        d.mkdir()
        conftest.git(d, "init", "-q", "-b", "main")
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        conftest.git(d, "add", "-A")
        conftest.git(d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
        return d
    prov = mk("prov", {"src/main/java/a/P.java": 'package a;\n@RestController\nclass P {\n  @GetMapping("/p") String p() { return "1"; }\n}\n',
                       "tia-services.yaml": "service: prov\n"})
    mk("cons", {"src/main/java/b/C.java": "package b;\nimport okhttp3.OkHttpClient;\nimport java.net.http.HttpClient;\nclass C {}\n",
                "tia-services.yaml": "service: cons\n"})
    conftest.git(prov, "checkout", "-qb", "change")
    (prov / "src/main/java/a/P.java").write_text('package a;\n@RestController\nclass P {\n  @GetMapping("/p") String p() { return "2"; }\n}\n')
    conftest.git(prov, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "two")
    rep = analyze(AnalysisRequest(repos={"prov": str(prov), "cons": str(tmp_path / "cons")}, target="prov", base="main", head="change"))
    u = next(x for x in rep["unknowns"] if x["area"] == "unsupported_http_clients")
    assert u["affects_confidence"] and u["count"] == 2 and rep["status"] == "complete_with_gaps"
    assert {(i["repo"], i["library"]) for i in u["items"]} == {("cons", "OkHttp"), ("cons", "java.net.http.HttpClient")}
    assert "OkHttp" in u["reason"] and "cons" not in u["reason"].replace("consumers", "")        # names libraries, never repositories
    assert redact(rep, ["prov"])["unknowns"][[x["area"] for x in rep["unknowns"]].index("unsupported_http_clients")]["items"] == []
