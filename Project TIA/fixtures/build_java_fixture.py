"""Builds the Java / Spring landscape (real git repos) used by the Java adapter tests.

orders-service      Java provider (Spring Web) and consumer: RestTemplate -> payments (config key), Feign -> inventory,
                    WebClient -> notifications; PR branches change the OrderDto contract / path / implementation / tests
inventory-service   Java provider (Spring Web), consumed through the OpenFeign client in orders-service
reporting-service   Java consumer of orders-service: RestTemplate with a literal host (R2) and a shared client library (R1)
checkout-bff        Python consumer of the Java orders-service (config key -> service map)
payments-service    Python provider (reuses the Python demo repo), consumed by the Java orders-service
"""
from __future__ import annotations

from pathlib import Path

from fixtures.build_fixture import PAYMENTS_APP, PAYMENTS_TEST, _commit, _run, _write

ORDER_CONTROLLER = '''package com.acme.orders;

import java.util.List;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    private final OrderService orders;

    public OrderController(OrderService orders) {
        this.orders = orders;
    }

    @GetMapping("/{id}")
    public ResponseEntity<OrderDto> getOrder(@PathVariable String id) {
        return ResponseEntity.ok(orders.find(id));
    }

    @GetMapping
    public List<OrderDto> listOrders() {
        return orders.all();
    }

    @PostMapping
    public OrderDto createOrder(@RequestBody CreateOrderRequest request) {
        return orders.create(request);
    }

    @RequestMapping(value = "/{id}/cancel", method = RequestMethod.POST)
    public void cancelOrder(@PathVariable String id) {
        orders.cancel(id);
    }
}
'''

ORDER_DTO = '''package com.acme.orders;

import jakarta.validation.constraints.NotNull;

public record OrderDto(@NotNull String id, @NotNull String status, double total) {
}
'''

CREATE_REQUEST = '''package com.acme.orders;

import jakarta.validation.constraints.NotNull;

public class CreateOrderRequest {
    @NotNull
    private String customer;
    private int quantity;
    private String note;

    public String getCustomer() {
        return customer;
    }

    public int getQuantity() {
        return quantity;
    }
}
'''

ORDER_SERVICE = '''package com.acme.orders;

import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class OrderService {
    private final PaymentGateway gateway;
    private final StockChecker stock;
    private final NotifyClient notifier;

    public OrderService(PaymentGateway gateway, StockChecker stock, NotifyClient notifier) {
        this.gateway = gateway;
        this.stock = stock;
        this.notifier = notifier;
    }

    public OrderDto find(String id) {
        PaymentStatus paid = gateway.status(id);
        return new OrderDto(id, paid.status(), 10.0);
    }

    public List<OrderDto> all() {
        return List.of();
    }

    public OrderDto create(CreateOrderRequest request) {
        stock.available(request.getCustomer());
        return new OrderDto("new", "OPEN", 0.0);
    }

    public void cancel(String id) {
        notifier.send(id);
    }
}
'''

PAYMENT_STATUS = '''package com.acme.orders;

public record PaymentStatus(String paymentId, String status, double amount) {
}
'''

PAYMENT_GATEWAY = '''package com.acme.orders;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;

@Component
public class PaymentGateway {
    @Value("${payments.base-url}")
    private String paymentsUrl;

    private final RestTemplate restTemplate;

    public PaymentGateway(RestTemplate restTemplate) {
        this.restTemplate = restTemplate;
    }

    public PaymentStatus status(String paymentId) {
        return restTemplate.getForObject(paymentsUrl + "/payments/" + paymentId + "/status", PaymentStatus.class);
    }
}
'''

INVENTORY_CLIENT = '''package com.acme.orders;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;

@FeignClient(name = "inventory-service")
public interface InventoryClient {
    @GetMapping("/inventory/{sku}")
    StockLevel stock(@PathVariable("sku") String sku);
}
'''

STOCK_LEVEL_CLIENT_SIDE = '''package com.acme.orders;

public record StockLevel(String sku, int quantity) {
}
'''

STOCK_CHECKER = '''package com.acme.orders;

import org.springframework.stereotype.Service;

@Service
public class StockChecker {
    private final InventoryClient inventory;

    public StockChecker(InventoryClient inventory) {
        this.inventory = inventory;
    }

    public int available(String sku) {
        return inventory.stock(sku).quantity();
    }
}
'''

NOTIFY_CLIENT = '''package com.acme.orders;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

@Component
public class NotifyClient {
    private final WebClient webClient;

    public NotifyClient(WebClient.Builder builder, @Value("${notify.url}") String url) {
        this.webClient = builder.baseUrl(url).build();
    }

    public void send(String id) {
        webClient.post().uri("/notify/{id}", id).retrieve().toBodilessEntity().block();
    }
}
'''

ORDER_CONTROLLER_TEST = '''package com.acme.orders;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;

import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;

class OrderControllerTest {
    private MockMvc mockMvc;

    @Test
    void getsAnOrder() throws Exception {
        mockMvc.perform(get("/api/orders/1"));
    }
}
'''

ORDER_SERVICE_TEST = '''package com.acme.orders;

import org.junit.jupiter.api.Test;

class OrderServiceTest {
    private OrderService service;

    @Test
    void findsAnOrder() {
        service.find("1");
    }
}
'''

INVENTORY_CONTROLLER = '''package com.acme.inventory;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/inventory")
public class StockController {
    @GetMapping("/{sku}")
    public StockLevel stock(@PathVariable String sku) {
        return new StockLevel(sku, 5);
    }
}
'''

INVENTORY_DTO = '''package com.acme.inventory;

public record StockLevel(String sku, int quantity) {
}
'''

REPORTING_DIRECT = '''package com.acme.reporting;

import org.springframework.web.client.RestTemplate;

public class OrdersReport {
    private final RestTemplate restTemplate = new RestTemplate();

    public Object build(String id) {
        return restTemplate.getForObject("http://orders-service/api/orders/" + id, Object.class);
    }
}
'''

REPORTING_SHARED = '''package com.acme.reporting;

import com.acme.orders.client.OrdersApi;

public class SharedClientReport {
    private final OrdersApi ordersApi;

    public SharedClientReport(OrdersApi ordersApi) {
        this.ordersApi = ordersApi;
    }

    public Object build(String id) {
        return ordersApi.getOrder(id);
    }
}
'''

CHECKOUT_BFF = '''import os

import requests

ORDERS_URL = os.getenv("ORDERS_URL")


def fetch_order(order_id):
    return requests.get(f"{ORDERS_URL}/api/orders/{order_id}", timeout=3).json()
'''


def _java(path: str) -> str:
    return "src/main/java/com/acme/" + path


def build(dest: str) -> dict:
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    out = {"paths": {}, "shas": {}}

    def repo(name, files):
        p = root / name
        p.mkdir(parents=True, exist_ok=True)
        _run(p, "init", "-q", "-b", "main")
        _write(p, files)
        out["paths"][name] = str(p)
        out["shas"][name] = _commit(p, "initial")
        return p

    orders = repo("orders-service", {
        "pom.xml": "<project/>\n",
        _java("orders/OrderController.java"): ORDER_CONTROLLER,
        _java("orders/OrderDto.java"): ORDER_DTO,
        _java("orders/CreateOrderRequest.java"): CREATE_REQUEST,
        _java("orders/OrderService.java"): ORDER_SERVICE,
        _java("orders/PaymentStatus.java"): PAYMENT_STATUS,
        _java("orders/PaymentGateway.java"): PAYMENT_GATEWAY,
        _java("orders/InventoryClient.java"): INVENTORY_CLIENT,
        _java("orders/StockLevel.java"): STOCK_LEVEL_CLIENT_SIDE,
        _java("orders/StockChecker.java"): STOCK_CHECKER,
        _java("orders/NotifyClient.java"): NOTIFY_CLIENT,
        "src/test/java/com/acme/orders/OrderControllerTest.java": ORDER_CONTROLLER_TEST,
        "src/test/java/com/acme/orders/OrderServiceTest.java": ORDER_SERVICE_TEST,
        "tia-services.yaml": "service: orders-service\nowner: team-orders\ncriticality: critical\n"
                             "config_keys:\n  payments.base-url: payments-service\n  notify.url: notifications-java\n"})
    inventory = repo("inventory-service", {
        _java("inventory/StockController.java"): INVENTORY_CONTROLLER,
        _java("inventory/StockLevel.java"): INVENTORY_DTO,
        "tia-services.yaml": "service: inventory-service\nowner: team-inventory\ncriticality: critical\n"})
    repo("reporting-service", {
        _java("reporting/OrdersReport.java"): REPORTING_DIRECT,
        _java("reporting/SharedClientReport.java"): REPORTING_SHARED,
        "tia-services.yaml": "service: reporting-service\nowner: team-reporting\ncriticality: critical\n"
                             "packages:\n  com.acme.orders.client: orders-service\n"})
    repo("checkout-bff", {
        "bff.py": CHECKOUT_BFF,
        "tia-services.yaml": "service: checkout-bff\nowner: team-checkout\ncriticality: critical\n"
                             "config_keys:\n  ORDERS_URL: orders-service\n"})
    repo("payments-service", {
        "app.py": PAYMENTS_APP, "tests/test_payments.py": PAYMENTS_TEST,
        "tia-services.yaml": "service: payments-service\nowner: team-payments\ncriticality: critical\n"})

    def branch(p, name, edits: dict, delete=()):
        _run(p, "checkout", "-q", "-b", name, "main")
        _write(p, edits)
        for d in delete:
            (p / d).unlink()
        _commit(p, name)

    ctl = _java("orders/OrderController.java")
    branch(orders, "breaking", {_java("orders/OrderDto.java"): ORDER_DTO.replace("String status", "String state")})
    branch(orders, "impl", {_java("orders/OrderService.java"): ORDER_SERVICE.replace("10.0", "11.0")})
    branch(orders, "removed_endpoint", {ctl: ORDER_CONTROLLER.replace(
        '''    @GetMapping("/{id}")
    public ResponseEntity<OrderDto> getOrder(@PathVariable String id) {
        return ResponseEntity.ok(orders.find(id));
    }

''', "")})
    branch(orders, "path_change", {ctl: ORDER_CONTROLLER.replace('"/api/orders"', '"/api/v2/orders"')})
    branch(orders, "tests_only", {"src/test/java/com/acme/orders/OrderServiceTest.java":
                                  ORDER_SERVICE_TEST.replace("service.find(\"1\");", "service.find(\"1\");\n        service.all();")})
    branch(orders, "syntax_error", {_java("orders/Broken.java"): "package com.acme.orders;\n\nclass Broken {\n    void f( {\n    }\n}\n"})
    _run(orders, "checkout", "-q", "main")
    branch(inventory, "breaking", {_java("inventory/StockLevel.java"): INVENTORY_DTO.replace("int quantity", "int onHand")})
    _run(inventory, "checkout", "-q", "main")
    pay = Path(out["paths"]["payments-service"])
    branch(pay, "breaking", {"app.py": PAYMENTS_APP.replace("    status: str\n", "    state: str\n")})
    _run(pay, "checkout", "-q", "main")
    return out
