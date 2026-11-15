"""Django and Django REST framework routes (PRD FR-004): url modules, include(), routers, viewsets, @action, @api_view, APIView,
generic views, regex routes, plain Django views; unresolvable routes become unknowns; a change behind a router reaches its consumer."""
import subprocess

from tia.extract_python import assemble, extract_repo, extract_source, is_extractable
from tia.impact import AnalysisRequest, analyze
from tia.models import RepoExtract

ORDERS = {
    "tia-services.yaml": "service: orders-api\nowner: team-orders\n",
    "shop/settings.py": 'ROOT_URLCONF = "shop.urls"\nINSTALLED_APPS = ["rest_framework", "orders"]\n',
    "shop/urls.py": (
        "from django.urls import include, path, re_path\n"
        "from orders import views as legacy\n"
        "urlpatterns = [\n"
        "    path('api/', include('orders.urls')),\n"
        "    re_path(r'^legacy/receipts/(?P<receipt_id>[0-9]+)/$', legacy.receipt),\n"
        "    path('broken/', include('nowhere.urls')),\n"
        "]\n"),
    "orders/__init__.py": "",
    "orders/urls.py": (
        "from django.urls import path\n"
        "from rest_framework.routers import DefaultRouter\n"
        "from . import views\n"
        "from .views import OrderViewSet, ShipmentList, Health\n"
        "router = DefaultRouter()\n"
        "router.register(r'orders', OrderViewSet, basename='order')\n"
        "router.register(r'audits', views.AuditViewSet)\n"
        "urlpatterns = [\n"
        "    path('summary/', views.summary),\n"
        "    path('shipments/', ShipmentList.as_view()),\n"
        "    path('health/<str:part>/', Health.as_view()),\n"
        "    path('ghost/', views.does_not_exist),\n"
        "]\n"
        "urlpatterns += router.urls\n"),
    "orders/views.py": (
        "from django.http import JsonResponse\n"
        "from rest_framework import generics, mixins, viewsets\n"
        "from rest_framework.decorators import action, api_view, permission_classes\n"
        "from rest_framework.permissions import IsAuthenticated, AllowAny\n"
        "from rest_framework.views import APIView\n"
        "from .serializers import OrderSerializer\n\n"
        "class OrderViewSet(viewsets.ModelViewSet):\n"
        "    queryset = None\n"
        "    serializer_class = OrderSerializer\n"
        "    permission_classes = [IsAuthenticated]\n"
        "    lookup_url_kwarg = 'order_id'\n\n"
        "    def retrieve(self, request, order_id=None):\n"
        "        return JsonResponse({'id': order_id, 'status': 'open'})\n\n"
        "    @action(detail=True, methods=['post'], url_path='cancel')\n"
        "    def cancel(self, request, order_id=None):\n"
        "        return JsonResponse({'cancelled': True})\n\n"
        "class AuditViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):\n"
        "    permission_classes = [AllowAny]\n\n"
        "class ShipmentList(generics.ListCreateAPIView):\n"
        "    serializer_class = OrderSerializer\n\n"
        "class Health(APIView):\n"
        "    def get(self, request, part):\n"
        "        return JsonResponse({'ok': True})\n"
        "    def delete(self, request, part):\n"
        "        return JsonResponse({})\n\n"
        "@api_view(['GET', 'POST'])\n"
        "@permission_classes([IsAuthenticated])\n"
        "def summary(request):\n"
        "    return JsonResponse({})\n\n"
        "def receipt(request, receipt_id):\n"
        "    if request.method in ('PUT',):\n"
        "        return JsonResponse({})\n"
        "    if request.method == 'POST':\n"
        "        return JsonResponse({})\n"
        "    return JsonResponse({})\n"),
    "orders/serializers.py": (
        "from rest_framework import serializers\n"
        "class BaseOrderSerializer(serializers.Serializer):\n"
        "    id = serializers.IntegerField(read_only=True)\n"
        "class OrderSerializer(BaseOrderSerializer):\n"
        "    status = serializers.CharField(max_length=20)\n"
        "    note = serializers.CharField(required=False, allow_null=True)\n"
        "    lines = LineSerializer(many=True)\n"
        "    class Meta:\n"
        "        fields = ['id', 'status', 'note', 'lines', 'created']\n"),
}


def ids(ex):
    return {e.id for e in ex.endpoints}


def test_every_kind_of_django_route_becomes_an_endpoint():
    ex = extract_repo("orders-api", "c1", ORDERS)
    s = "orders-api:"
    assert ids(ex) >= {
        s + "GET /api/orders", s + "POST /api/orders",                                  # router: ModelViewSet collection
        s + "GET /api/orders/{}", s + "PUT /api/orders/{}", s + "PATCH /api/orders/{}", s + "DELETE /api/orders/{}",
        s + "POST /api/orders/{}/cancel",                                                # @action(detail=True)
        s + "GET /api/audits",                                                           # ListModelMixin only
        s + "GET /api/summary", s + "POST /api/summary",                                 # @api_view
        s + "GET /api/shipments", s + "POST /api/shipments",                             # ListCreateAPIView
        s + "GET /api/health/{}", s + "DELETE /api/health/{}",                           # APIView methods, path converter
        s + "GET /legacy/receipts/{}", s + "POST /legacy/receipts/{}", s + "PUT /legacy/receipts/{}",   # re_path, named group; request.method
    }
    assert s + "POST /api/audits" not in ids(ex) and s + "GET /api/audits/{}" not in ids(ex)
    by = {e.id: e for e in ex.endpoints}
    detail = by[s + "GET /api/orders/{}"]
    assert detail.path == "/api/orders/{order_id}/" and detail.handler == "OrderViewSet.retrieve"
    assert detail.handler_id == "orders-api:orders/views.py::OrderViewSet.retrieve"            # the override handles it
    assert by[s + "PUT /api/orders/{}"].handler_id == "orders-api:orders/views.py::OrderViewSet"  # inherited: the class configuration
    assert by[s + "POST /api/orders"].success_status == 201 and by[s + "GET /api/orders"].response_shape == "list"
    assert detail.auth == ["IsAuthenticated"] and by[s + "GET /api/audits"].auth == []        # AllowAny is no requirement
    assert detail.response_schema == "OrderSerializer"


def test_routes_that_cannot_be_resolved_are_reported_not_dropped():
    ex = extract_repo("orders-api", "c1", ORDERS)
    reasons = " | ".join(u["reason"] for u in ex.route_unknowns)
    assert "include('nowhere.urls')" in reasons and "orders.views.does_not_exist" in reasons
    assert all(u["file"] and u["line"] for u in ex.route_unknowns)


def test_the_per_file_cache_round_trip_gives_the_same_routes():
    full = extract_repo("orders-api", "c1", ORDERS)
    per_file = {p: RepoExtract.from_dict(extract_source("orders-api", "orders-api", p, t).to_dict()) for p, t in ORDERS.items() if is_extractable(p)}
    again = assemble("orders-api", "c1", list(ORDERS), {"service": "orders-api"}, "orders-api", per_file, {})
    assert ids(again) == ids(full) and again.route_unknowns == full.route_unknowns


def test_view_class_configuration_is_a_function_whose_change_is_seen():
    a = {f.id: f for f in extract_repo("orders-api", "c1", ORDERS).functions}
    changed = dict(ORDERS, **{"orders/views.py": ORDERS["orders/views.py"].replace("permission_classes = [IsAuthenticated]\n    lookup",
                                                                                  "permission_classes = []\n    lookup")})
    b = {f.id: f for f in extract_repo("orders-api", "c1", changed).functions}
    cid = "orders-api:orders/views.py::OrderViewSet"
    assert a[cid].hash != b[cid].hash
    assert a["orders-api:orders/views.py::OrderViewSet.retrieve"].hash == b["orders-api:orders/views.py::OrderViewSet.retrieve"].hash
    assert "orders-api:orders/serializers.py::OrderSerializer" not in a              # serializers and models are not views


def test_serializers_are_schemas_and_a_removed_response_field_is_breaking():
    from tia.contracts import diff_contracts
    base = extract_repo("orders-api", "c1", ORDERS)
    s = {x.name: x for x in base.schemas}["OrderSerializer"]
    assert s.fields["status"] == {"type": "str", "required": True, "constraints": {"max_length": 20}}
    assert s.fields["note"]["required"] is False and s.fields["note"]["nullable"] is True
    assert s.fields["lines"]["multi"] is True and s.fields["created"]["type"] == "model"
    assert s.fields["id"] == {"type": "int", "required": False, "read_only": True}                # inherited from the base serializer
    head = extract_repo("orders-api", "c2", dict(ORDERS, **{"orders/serializers.py": ORDERS["orders/serializers.py"].replace(
        "    status = serializers.CharField(max_length=20)\n", "").replace("'status', ", "")}))
    ch = [c for c in diff_contracts(base, head) if c["kind"] == "schema_field_removed"]
    assert ch and ch[0]["breaking"] and "status" in ch[0]["detail"]


def test_method_restricting_decorators_are_exact():
    files = dict(ORDERS, **{"orders/views.py": ORDERS["orders/views.py"].replace(
        "def receipt(request, receipt_id):", "from django.views.decorators.http import require_POST\n@require_POST\ndef receipt(request, receipt_id):")})
    got = {i for i in ids(extract_repo("orders-api", "c1", files)) if "receipts" in i}
    assert got == {"orders-api:POST /legacy/receipts/{}"}


def test_a_file_without_django_is_untouched():
    flask = {"app.py": "from flask import Flask\napp = Flask(__name__)\n@app.route('/x')\ndef x(request):\n    return 1\n"}
    ex = extract_repo("f", "c", flask)
    assert ex.django_facts == [] and ids(ex) == {"f:GET /x"}


def _git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _repo(path, files):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@t")
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return _git(path, "rev-parse", "HEAD")


def test_a_change_behind_a_router_reaches_the_consumer_of_that_route(tmp_path):
    orders, shop = tmp_path / "orders-api", tmp_path / "shop-web"
    base = _repo(orders, ORDERS)
    _repo(shop, {"tia-services.yaml": "service: shop-web\nowner: team-shop\nconfig_keys:\n  ORDERS_URL: orders-api\n",
                 "client.py": "import os\nimport requests\nORDERS_URL = os.getenv('ORDERS_URL')\n\n"
                              "def order_status(order_id):\n"
                              "    return requests.get(f'{ORDERS_URL}/api/orders/{order_id}/', timeout=3).json()['status']\n"})
    views = orders / "orders" / "views.py"
    views.write_text(views.read_text(encoding="utf-8").replace("'status': 'open'", "'state': 'open'"), encoding="utf-8")
    _git(orders, "commit", "-qam", "rename status")
    head = _git(orders, "rev-parse", "HEAD")
    rep = analyze(AnalysisRequest(repos={"orders-api": str(orders), "shop-web": str(shop)}, target="orders-api", base=base, head=head))
    assert rep["status"] != "failed"
    hit = [f for f in rep["findings"] if "shop-web" in (f.get("service") or "") + (f.get("component") or "") + (f.get("repo") or "")]
    assert hit, [(f.get("component"), f.get("service")) for f in rep["findings"]]
    assert any("orders/{}" in str(f) for f in hit)
    assert any(u["area"] == "routes_not_resolved" for u in rep["unknowns"])
