"""Release 4 language adapters (PRD FR-040): Go, C#/.NET, Kotlin, TypeScript/JavaScript, the adapter registry, and impact across
all four languages on real git repositories."""
import pytest

from fixtures.build_polyglot_fixture import build as build_poly
from tia import EXTRACTOR_VERSIONS, adapters
from tia.adapters import Adapter, adapter_for
from tia.extract_python import extract_source, is_extractable
from tia.extraction import extract_commit
from tia.impact import AnalysisRequest, analyze
from tia.lang import csharp, golang, kotlin, typescript


@pytest.fixture(scope="module")
def poly(tmp_path_factory):
    return build_poly(str(tmp_path_factory.mktemp("poly")))


def run(poly, head):
    return analyze(AnalysisRequest(repos=poly["paths"], target="pricing-service", base="main", head=head), None)


# ---- per-language facts ---------------------------------------------------------------------------------------------------------
def test_go_routes_bodies_env_bases_and_tests():
    src = '''package main
import ("encoding/json"; "net/http"; "os")
var ordersURL = os.Getenv("ORDERS_URL")
type Order struct {
    ID string `json:"id" validate:"required,max=18"`
    Note *string `json:"note,omitempty"`
}
func create(w http.ResponseWriter, r *http.Request) {
    var o Order
    json.NewDecoder(r.Body).Decode(&o)
    http.Get(ordersURL + "/orders/" + o.ID)
    json.NewEncoder(w).Encode(o)
}
func list(c *gin.Context) { c.JSON(200, []Order{}) }
func main() {
    http.HandleFunc("POST /orders", create)
    router.GET("/orders", list)
}
'''
    ex = golang.extract_file("r", "svc", "main.go", src)
    eps = {e.id: e for e in ex.endpoints}
    assert set(eps) == {"svc:POST /orders", "svc:GET /orders"}
    assert eps["svc:POST /orders"].request_schemas == ["Order"] and eps["svc:POST /orders"].response_schema == "Order"
    assert (eps["svc:GET /orders"].response_schema, eps["svc:GET /orders"].response_shape) == ("Order", "list")
    (c,) = ex.callsites
    assert (c.method, c.norm_path, c.base_kind, c.base_value) == ("GET", "/orders/{}", "config_key", "ORDERS_URL")
    (s,) = ex.schemas
    assert s.fields["id"]["required"] and s.fields["id"]["constraints"]["max_length"] == 18 and s.fields["note"].get("nullable")
    t = golang.extract_file("r", "svc", "main_test.go", 'package main\nfunc TestX(t *testing.T) { httptest.NewRequest("GET", "/orders?x=1", nil) }\n')
    assert [(x.name, x.http_calls) for x in t.tests] == [("TestX", [["GET", "/orders"]])]


def test_csharp_controllers_minimal_apis_clients_and_tests():
    src = '''[ApiController]
[Route("api/[controller]")]
[Authorize]
public class OrdersController : ControllerBase {
    [HttpGet("{id}")] public ActionResult<OrderDto> Get(string id) { return null; }
    [HttpPost] public ActionResult<OrderDto> Post([FromBody] CreateOrder body) {
        var c = factory.CreateClient("payments");
        c.PostAsJsonAsync("/payments", body);
        return null;
    }
}
public class CreateOrder { [Required][StringLength(18)] public string Reference { get; set; } [Range(1, 100)] public int Quantity { get; set; } }
public class OrderDto { public string Id { get; set; } }
'''
    ex = csharp.extract_file("r", "svc", "Controllers/OrdersController.cs", src)
    eps = {e.id: e for e in ex.endpoints}
    assert set(eps) == {"svc:GET /api/orders/{}", "svc:POST /api/orders"}
    assert eps["svc:POST /api/orders"].request_schemas == ["CreateOrder"] and eps["svc:GET /api/orders/{}"].response_schema == "OrderDto"
    assert eps["svc:GET /api/orders/{}"].auth
    (c,) = ex.callsites
    assert (c.method, c.norm_path, c.base_kind, c.base_value) == ("POST", "/payments", "config_key", "payments")
    co = {s.name: s for s in ex.schemas}["CreateOrder"].fields
    assert co["reference"]["constraints"]["max_length"] == 18 and co["quantity"]["constraints"] == {"minimum": 1, "maximum": 100}
    t = csharp.extract_file("r", "svc", "Tests/OrdersTests.cs", 'public class T { [Fact] public async void Gets() { await client.GetAsync("/api/orders/1"); } }')
    assert [(x.name, x.http_calls) for x in t.tests] == [("Gets", [["GET", "/api/orders/1"]])]


def test_kotlin_spring_controllers_value_bases_and_dtos():
    src = '''@RestController
@RequestMapping("/orders")
@PreAuthorize("hasRole('USER')")
class OrderController(private val rest: RestTemplate) {
    @Value("\\${pricing.url}") lateinit var pricingUrl: String
    @GetMapping("/{id}")
    fun get(@PathVariable id: String): Order = rest.getForObject("$pricingUrl/prices/$id", Order::class.java)!!
    @PostMapping
    fun create(@RequestBody body: CreateOrder): List<Order> = listOf()
}
data class CreateOrder(@field:NotBlank @field:Size(max = 18) val reference: String, @field:Min(1) val amount: Double, val note: String?)
'''
    ex = kotlin.extract_file("r", "svc", "src/main/kotlin/OrderController.kt", src)
    eps = {e.id: e for e in ex.endpoints}
    assert set(eps) == {"svc:GET /orders/{}", "svc:POST /orders"}
    assert eps["svc:POST /orders"].request_schemas == ["CreateOrder"] and eps["svc:POST /orders"].response_shape == "list"
    assert eps["svc:GET /orders/{}"].auth == ["@PreAuthorize(\"hasRole('USER')\")"]
    (c,) = ex.callsites
    assert (c.method, c.norm_path, c.base_kind, c.base_value) == ("GET", "/prices/{}", "config_key", "pricing.url")
    f = {s.name: s for s in ex.schemas}["CreateOrder"].fields
    assert f["reference"]["constraints"]["max_length"] == 18 and f["amount"]["constraints"]["minimum"] == 1 and f["note"]["required"] is False


def test_typescript_nest_express_dtos_clients_and_supertest():
    nest = '''@Controller('orders')
@UseGuards(AuthGuard)
export class OrdersController {
  @Get(':id')
  async get(@Param('id') id: string): Promise<Order> { return fetch(`${process.env.PRICING_URL}/prices/${id}`); }
  @Post()
  create(@Body() dto: CreateOrderDto): Order[] { return axios.post(`${process.env.LEDGER_URL}/entries`, dto); }
}
export class CreateOrderDto {
  @IsString() @MaxLength(18) reference: string;
  @IsIn(['EUR', 'USD']) currency: string;
  @IsOptional() @Min(1) amount?: number;
}
'''
    ex = typescript.extract_file("r", "svc", "src/orders.controller.ts", nest)
    eps = {e.id: e for e in ex.endpoints}
    assert set(eps) == {"svc:GET /orders/{}", "svc:POST /orders"}
    assert eps["svc:POST /orders"].request_schemas == ["CreateOrderDto"] and eps["svc:POST /orders"].response_shape == "list"
    assert eps["svc:GET /orders/{}"].auth == ["@UseGuards(AuthGuard)"] and [p["name"] for p in eps["svc:GET /orders/{}"].params] == ["id"]
    assert sorted((c.method, c.norm_path, c.base_value) for c in ex.callsites) == [("GET", "/prices/{}", "PRICING_URL"), ("POST", "/entries", "LEDGER_URL")]
    dto = {s.name: s for s in ex.schemas}["CreateOrderDto"].fields
    assert dto["currency"]["constraints"] == {"allowed": ["EUR", "USD"]} and dto["amount"]["required"] is False
    express = '''const BASE = process.env.USERS_URL;
app.get('/users/:id', requireAuth, async (req, res) => { res.json(await axios.get(BASE + '/profiles/' + req.params.id)); });
'''
    ex = typescript.extract_file("r", "svc", "server.js", express)
    (e,) = ex.endpoints
    assert (e.method, e.path, e.auth) == ("GET", "/users/:id", ["requireAuth"])
    (c,) = ex.callsites
    assert (c.norm_path, c.base_kind, c.base_value, c.function_id) == ("/profiles/{}", "config_key", "USERS_URL", "r:server.js::get /users/:id")
    t = typescript.extract_file("r", "svc", "test/u.test.js", "it('gets', async () => { await request(app).get('/users/1').expect(200); });")
    assert [(x.name, x.http_calls) for x in t.tests] == [("gets", [["GET", "/users/1"]])]


# ---- registry ------------------------------------------------------------------------------------------------------------------
def test_registry_dispatch_versions_and_generated_files():
    assert {a.name for a in adapters.registry()} >= {"go", "csharp", "kotlin", "typescript"}
    for name, ver in (("go", "go-1"), ("csharp", "cs-1"), ("kotlin", "kt-1"), ("typescript", "ts-1")):
        assert EXTRACTOR_VERSIONS[name] == ver
    assert adapter_for("cmd/main.go").name == "go" and adapter_for("src/App.tsx").name == "typescript" and adapter_for("x.kts").name == "kotlin"
    assert adapter_for("vendor/x/y.go") is None and adapter_for("node_modules/a/b.js") is None and adapter_for("types.d.ts") is None
    assert adapter_for("bin/Debug/X.cs") is None and adapter_for("app.py") is None
    assert is_extractable("svc/handler.go") and is_extractable("Controllers/X.cs")


def test_third_party_adapters_are_registered_but_cannot_take_built_in_extensions(monkeypatch):
    def rb(repo, service, path, text, strip):
        from tia.lang.common import new_extract
        out = new_extract(repo, service)
        out.files_analysed = 1
        return out
    good = Adapter("ruby", "rb-1", (".rb",), rb)
    clash = Adapter("goplus", "gp-1", (".go",), rb)
    reserved = Adapter("python2", "p2-1", (".py",), rb)
    monkeypatch.setattr(adapters, "_discover", lambda: [good, clash, reserved])
    try:
        reg = adapters.registry(refresh=True)
        assert good in reg and clash not in reg and reserved not in reg
        assert adapter_for("app/models/user.rb") is good and adapter_for("main.go").name == "go"
        assert adapters.versions()["ruby"] == "rb-1"
        assert extract_source("r", "svc", "app/models/user.rb", "class User; end").files_analysed == 1
    finally:
        monkeypatch.undo()
        adapters.registry(refresh=True)
    assert adapter_for("app/models/user.rb") is None


def test_a_failing_adapter_reports_the_file_and_does_not_break_extraction():
    def boom(*a):
        raise RuntimeError("parser exploded")
    part = adapters.run(Adapter("bad", "bad-1", (".bad",), boom), "r", "svc", "x.bad", "", ())
    assert part.files_failed == [{"file": "x.bad", "error": "bad adapter: RuntimeError: parser exploded"}]


def test_typescript_front_end_files_keep_their_page_facts():
    src = '''@Component({ selector: 'app-pay', templateUrl: './pay.component.html' })
export class PayComponent { submit() { return this.http.post(`${environment.api}/payments`, {}); } }
'''
    part = extract_source("r", "web", "src/app/pay.component.ts", src)
    assert part.ui_facts and part.files_analysed == 1 and not part.files_failed


# ---- impact across four languages on real repositories ---------------------------------------------------------------------------
def test_polyglot_extraction_links_every_language(poly):
    exs = {n: extract_commit(n, p, "main", None)[0] for n, p in poly["paths"].items()}
    assert all(not e.files_failed and e.non_python_files == 0 for e in exs.values())
    assert [e.id for e in exs["pricing-service"].endpoints] == ["pricing-service:GET /prices/{}"]
    assert exs["pricing-service"].endpoints[0].response_schema == "Price"
    assert [e.id for e in exs["ledger-service"].endpoints] == ["ledger-service:POST /api/entries"]
    assert {(c.norm_path, c.base_value) for c in exs["billing-service"].callsites} == {("/prices/{}", "pricing.url"), ("/api/entries", "ledger.url")}
    assert [(c.norm_path, c.base_value) for c in exs["orders-service"].callsites] == [("/prices/{}", "PRICING_URL")]


def test_a_go_response_change_reaches_typescript_and_kotlin_consumers(poly):
    rep = run(poly, "field_removed")
    by = {}
    for f in rep["findings"]:
        by.setdefault(f["component"], []).append(f)
    assert set(by) == {"orders-service", "billing-service"}
    assert all(f["severity"] == "high" for fs in by.values() for f in fs)
    assert any("BillingService.kt" in f["reason"] for f in by["billing-service"])
    assert any("orders.controller.ts" in f["reason"] for f in by["orders-service"])
    eps = {e["entry_point"]: e for e in rep["test_scope"]["entry_points"]}
    assert eps["POST /orders"]["change_effect"] == "breaking" and eps["POST /orders"]["evidence_status"] == "source_confirmed"
    assert [t["name"] for t in eps["POST /orders"]["tests"]] == ["creates an order"]
    assert "unsupported_language_files" not in {u["area"] for u in rep["unknowns"]}
    assert "ledger-service" not in by                                     # ledger is called BY billing, it does not depend on pricing


def test_an_implementation_change_is_lower_severity_and_deterministic(poly):
    a, b = run(poly, "impl_only"), run(poly, "impl_only")
    assert {f["severity"] for f in a["findings"]} == {"medium"}
    assert [f["component"] for f in a["findings"]] == [f["component"] for f in b["findings"]]


# ---- Entity Framework (FR-040 C#/.NET) -------------------------------------------------------------------------------------------------
EF_ENTITY = '''using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

public class Order {
    [Key] public long OrderNo { get; set; }
    [Column("cust_ref")] public string CustomerRef { get; set; }
    public decimal Amount { get; set; }
    [NotMapped] public string Display { get; set; }
    public List<OrderLine> Lines { get; set; }
}

[Table("order_lines")]
public class OrderLine { public long Id { get; set; } public int Qty { get; set; } }

public class OrderDto { public long Id { get; set; } }
'''
EF_CONTEXT = '''public class ShopContext : DbContext {
    public DbSet<Order> Orders { get; set; }
    public DbSet<OrderLine> Lines { get; set; }
}
'''
EF_SERVICE = '''public class OrderService {
    private readonly ShopContext _db;
    public List<Order> Open() { return _db.Orders.Where(o => o.Amount > 0).ToList(); }
    public void Place(Order o) { _db.Orders.Add(o); _db.SaveChanges(); }
    public int LineCount() { return _db.Set<OrderLine>().Count(); }
}
'''


def test_entity_framework_entities_tables_and_access(tmp_path):
    from fixtures.build_fixture import _commit, _run, _write
    p = tmp_path / "shop"
    p.mkdir()
    _run(p, "init", "-q", "-b", "main")
    _write(p, {"Models/Order.cs": EF_ENTITY, "Data/ShopContext.cs": EF_CONTEXT, "Services/OrderService.cs": EF_SERVICE,
               "tia-services.yaml": "service: shop\ndatabase: shopdb\n"})
    _commit(p, "init")
    ex, _ = extract_commit("shop", str(p), "main", None)
    ents = {e.name: e for e in ex.entities}
    assert set(ents) == {"Order", "OrderLine"}                                      # OrderDto is not in any DbSet: a plain class
    assert (ents["Order"].table, ents["Order"].table_source, ents["Order"].kind) == ("orders", "ef_convention", "ef")
    assert set(ents["Order"].columns) == {"orderno", "cust_ref", "amount"} and ents["Order"].columns["orderno"]["pk"]
    assert (ents["OrderLine"].table, ents["OrderLine"].table_source) == ("order_lines", "declared")
    acc = {(a.function_id.split("::")[1], a.entity.split(":")[-1], a.op) for a in ex.data_access}
    assert acc == {("OrderService.Open", "Order", "read"), ("OrderService.Place", "Order", "write"), ("OrderService.LineCount", "OrderLine", "read")}
