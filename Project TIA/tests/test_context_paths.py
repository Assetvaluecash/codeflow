"""Servlet context paths (found by the accuracy harness on piggymetrics): a Spring service's own server.servlet.context-path is applied to its routes;
one set in a configuration repository is reported, with the setting that would apply it, instead of being silently ignored."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tia.extract_python import context_path, external_context_paths, extract_repo  # noqa: E402
from tia.impact import AnalysisRequest, analyze  # noqa: E402

CONTROLLER = """package com.acme.accounts;
import org.springframework.web.bind.annotation.*;
@RestController
public class AccountController {
    @RequestMapping(path = "/{name}", method = RequestMethod.GET)
    public Object get(@PathVariable String name) { return null; }
}
"""
TEST = """package com.acme.accounts;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
class AccountControllerTest {
    MockMvc mockMvc;
    @Test void gets() throws Exception { mockMvc.perform(get("/demo")); }
}
"""
CLIENT = """package com.acme.notify;
import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.*;
@FeignClient(name = "account-service")
public interface AccountClient {
    @RequestMapping(method = RequestMethod.GET, value = "/accounts/{accountName}")
    String getAccount(@PathVariable("accountName") String accountName);
}
"""
USER = """package com.acme.notify;
public class Notifier {
    private final AccountClient client;
    public Notifier(AccountClient client) { this.client = client; }
    public String run(String name) { return client.getAccount(name); }
}
"""


@pytest.mark.parametrize("files,want", [
    ({"src/main/resources/application.yml": "server:\n  servlet:\n    context-path: /accounts\n"}, "/accounts"),
    ({"src/main/resources/application.properties": "server.servlet.context-path=/accounts/\n"}, "/accounts"),
    ({"src/main/resources/application.yml": "server:\n  context-path: /uaa\n"}, "/uaa"),                       # Spring Boot 1.x name
    ({"src/main/resources/application.yml": "server:\n  servlet:\n    context-path: ${CTX}\n"}, None),          # a placeholder is not a path
    ({"src/main/resources/application.yml": "server:\n  servlet:\n    context-path: /\n"}, None),
    ({"src/test/resources/application.yml": "server:\n  servlet:\n    context-path: /t\n"}, None),               # test configuration does not count
    ({}, None)])
def test_reading_the_context_path(files, want):
    assert context_path(files) == want


def test_a_services_own_context_path_is_applied_to_its_routes_and_its_mockmvc_tests():
    ex = extract_repo("account-service", "c", {"src/main/java/com/acme/accounts/AccountController.java": CONTROLLER,
                                               "src/test/java/com/acme/accounts/AccountControllerTest.java": TEST,
                                               "src/main/resources/application.yml": "spring:\n  application:\n    name: account-service\nserver:\n  servlet:\n    context-path: /accounts\n"})
    assert ex.context_path == "/accounts"
    assert [e.norm_path for e in ex.endpoints] == ["/accounts/{}"] and ex.endpoints[0].id.endswith("GET /accounts/{}")
    assert ex.tests[0].http_calls == [["GET", "/accounts/demo"]]


def test_external_context_paths_are_read_from_a_configuration_repository():
    files = {"src/main/resources/shared/account-service.yml": "server:\n  servlet:\n    context-path: /accounts\n",
             "src/main/resources/shared/auth-service.properties": "server.servlet.context-path=/uaa\n",
             "src/main/resources/shared/application.yml": "logging:\n  level: INFO\n"}
    assert external_context_paths(files) == {"account-service": "/accounts", "auth-service": "/uaa"}


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


def repo(root, name, files):
    p = root / name
    for rel, text in files.items():
        (p / rel).parent.mkdir(parents=True, exist_ok=True)
        (p / rel).write_text(text)
    git(p, "init", "-q", "-b", "main")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "base")
    return str(p)


def landscape(tmp_path, own_context: bool):
    acct_cfg = "spring:\n  application:\n    name: account-service\n" + ("server:\n  servlet:\n    context-path: /accounts\n" if own_context else "")
    repos = {"account-service": repo(tmp_path, "account-service", {"src/main/java/com/acme/accounts/AccountController.java": CONTROLLER,
                                                                    "src/main/resources/application.yml": acct_cfg}),
             "notification-service": repo(tmp_path, "notification-service", {"src/main/java/com/acme/notify/AccountClient.java": CLIENT, "src/main/java/com/acme/notify/Notifier.java": USER,
                                                                              "src/main/resources/application.yml": "spring:\n  application:\n    name: notification-service\n"})}
    if not own_context:
        repos["config"] = repo(tmp_path, "config", {"src/main/resources/shared/account-service.yml": "server:\n  servlet:\n    context-path: /accounts\n"})
    p = Path(repos["account-service"])
    ctl = p / "src/main/java/com/acme/accounts/AccountController.java"
    git(p, "checkout", "-q", "-b", "rename")
    ctl.write_text(CONTROLLER.replace('"/{name}"', '"/{name}/summary"'))
    git(p, "commit", "-qam", "rename")
    return repos


def test_with_its_own_context_path_the_consumer_is_found_without_any_configuration(tmp_path):
    repos = landscape(tmp_path, own_context=True)
    rep = analyze(AnalysisRequest(repos=repos, target="account-service", base="main", head="rename"), None)
    assert "notification-service" in {f["component"] for f in rep["findings"]}
    assert "context_paths_not_applied" not in {u["area"] for u in rep["unknowns"]}


def test_a_context_path_set_elsewhere_is_reported_not_silently_missed(tmp_path):
    repos = landscape(tmp_path, own_context=False)
    rep = analyze(AnalysisRequest(repos=repos, target="account-service", base="main", head="rename"), None)
    u = next(x for x in rep["unknowns"] if x["area"] == "context_paths_not_applied")
    assert "/accounts" in u["reason"] and "paths.strip_prefixes" in u["reason"] and u["affects_confidence"]
    assert "notification-service" not in {f["component"] for f in rep["findings"]}
    fixed = analyze(AnalysisRequest(repos=repos, target="account-service", base="main", head="rename", strip_prefixes=("/accounts",)), None)
    assert "notification-service" in {f["component"] for f in fixed["findings"]}                          # the suggested setting works
    assert "context_paths_not_applied" not in {x["area"] for x in fixed["unknowns"]}
