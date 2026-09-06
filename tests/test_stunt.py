import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml
from mitmproxy.http import Request, Response
from mitmproxy.test import tflow

from stunt.addon import CompiledRule, Stunt
from stunt.cli import main as cli_main


@pytest.fixture
async def addon(tmp_path):
    a = Stunt()
    a.rules_file = tmp_path / "rules.yaml"
    a.mocks_dir = tmp_path / "mocks"
    a.mocks_dir.mkdir(parents=True, exist_ok=True)
    await a._reload()
    yield a
    await a.aclose()


def make_flow(
    method="GET",
    url="https://api.example.com",
    content=None,
    status=200,
    response_content=None,
    headers=None,
):
    f = tflow.tflow()
    f.request = Request.make(method, url, content or b"{}", headers=headers or {})
    if response_content is not None:
        f.response = Response.make(
            status,
            response_content,
            {"content-type": "application/json"},
        )
    return f


def response_content(flow):
    assert flow.response is not None
    assert flow.response.content is not None
    return flow.response.content


def response_json(flow):
    return json.loads(response_content(flow))


def request_content(flow):
    assert flow.request is not None
    assert flow.request.content is not None
    return flow.request.content


def request_json(flow):
    return json.loads(request_content(flow))


# ========================================================
# Category: Reload and Mock Handling
# ========================================================


async def test_mock_hot_reload(addon):
    mock_path = addon.mocks_dir / "user.json"
    mock_path.write_text('{"name": "old"}')
    rule = {"name": "mock-test", "respond_with": {"file": "user.json"}}
    addon.rules = (addon._compile_rules({"rules": [rule]})[0],)
    flow = make_flow()
    await addon.request(flow)
    assert b'"old"' in response_content(flow)
    mock_path.write_text('{"name": "new"}')
    # Cache invalidation keys on st_mtime_ns, so the two writes only need to land
    # on different mtime ticks.
    await asyncio.sleep(0.01)
    flow2 = make_flow()
    await addon.request(flow2)
    assert b'"new"' in response_content(flow2)


async def test_load_mock_path_traversal_blocked(addon):
    with pytest.raises(PermissionError, match="Path traversal attempt blocked"):
        await addon._load_mock("../secret.txt")


async def test_reload_deleted_rules_file(addon):
    """Deleting rules.yaml resets the addon completely."""
    addon.rules_file.write_text(yaml.dump({"rules": [{"name": "test-rule", "error": {"body": "hello"}}]}))
    await addon._reload()
    assert len(addon.rules) == 1
    assert addon._hash != ""

    addon.rules_file.unlink(missing_ok=True)

    await addon._reload()

    assert len(addon.rules) == 0
    assert addon._hash == ""
    assert addon.global_delay_fixed is None
    assert addon.global_delay_range is None
    assert addon.quiet is False


async def test_reload_initial_state(addon):
    addon.rules_file.write_text(yaml.dump({"state": {"stage": "init"}, "rules": []}))
    await addon._reload()
    assert addon._state == {"stage": "init"}


async def test_watcher_survives_rename_over_save(addon):
    """Awatch on the rules file's parent dir survives an editor's
    rename-over save (vim/VS Code/`sed -i`), which replaces the inode and
    would orphan a watch registered on the file path directly."""

    def write_marker(n):
        addon.rules_file.write_text(yaml.dump({"rules": [{"name": f"marker-{n}", "error": {"status": 200}}]}))

    async def wait_for_marker(n, timeout=3.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if addon.rules and addon.rules[0].name == f"marker-{n}":
                return
            await asyncio.sleep(0.05)
        pytest.fail(f"marker-{n} was not reloaded within {timeout}s")

    addon._watcher_stop = asyncio.Event()
    watcher_task = asyncio.get_event_loop().create_task(addon._watcher())
    try:
        # 1. truncate-write (control)
        write_marker(1)
        await wait_for_marker(1)

        # 2. atomic rename-over save: the pattern that orphans a file-path watch
        tmp = addon.rules_file.with_suffix(".tmp")
        tmp.write_text(yaml.dump({"rules": [{"name": "marker-2", "error": {"status": 200}}]}))
        os.replace(tmp, addon.rules_file)
        await wait_for_marker(2)

        # 3. a further edit after the rename must still reload
        write_marker(3)
        await wait_for_marker(3)
    finally:
        addon._watcher_stop.set()
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass


# ========================================================
# Category: Basic Actions (Respond With, Inject Error)
# ========================================================


async def test_inject_error(addon):
    rule = {"name": "error", "error": {"status": 429, "body": "rate limited"}}
    addon.rules = (addon._compile_rules({"rules": [rule]})[0],)
    flow = make_flow()
    await addon.request(flow)
    assert flow.response.status_code == 429
    assert b"rate limited" in response_content(flow)


async def test_inject_error_all_fields(addon):
    rule = addon._compile_rules(
        {
            "rules": [
                {"name": "error-full", "error": {"status": 418, "body": {"error": True}, "headers": {"X-Test": "1"}}}
            ]
        }
    )[0]

    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)

    assert flow.response.status_code == 418
    assert flow.response.headers["X-Test"] == "1"


async def test_respond_with_all_fields(addon, tmp_path):
    mock = addon.mocks_dir / "file.json"
    mock.write_text('{"file": true}')

    rule = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "resp",
                    "respond_with": {"status": 201, "file": "file.json", "headers": {"X-Test": "ok"}, "json": True},
                }
            ]
        }
    )[0]

    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)

    assert flow.response.status_code == 201
    assert flow.response.headers["X-Test"] == "ok"
    assert response_json(flow)["file"] is True


async def test_respond_with_inline_json(addon):
    rule = {"name": "inline", "respond_with": {"body": {"success": True}}}
    addon.rules = (addon._compile_rules({"rules": [rule]})[0],)
    flow = make_flow()
    await addon.request(flow)
    assert response_json(flow) == {"success": True}


async def test_respond_with_json_false(addon):
    rule = addon._compile_rules({"rules": [{"name": "raw", "respond_with": {"body": "plain", "json": False}}]})[0]

    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)

    assert response_content(flow).decode() == "plain"


# ========================================================
# Category: Phase and Isolation
# ========================================================

# ========================================================
# Category: Probability and Counts
# ========================================================


async def test_count_multiple_applications(addon):
    rule = CompiledRule(name="count-multi", count=2, inject_error={"body": "hit"})
    addon.rules = (rule,)

    flow1 = make_flow()
    await addon.request(flow1)
    assert flow1.response is not None

    flow2 = make_flow()
    await addon.request(flow2)
    assert flow2.response is not None

    flow3 = make_flow()
    await addon.request(flow3)
    assert flow3.response is None


async def test_once_rule_exhaustion(addon):
    rule = CompiledRule(name="once", once=True, respond_with={"body": {"ok": True}})
    addon.rules = (rule,)
    flow1 = make_flow()
    await addon.request(flow1)
    flow2 = make_flow()
    await addon.request(flow2)
    assert addon._rule_hits["once"] == 1
    assert flow2.response is None


async def test_probability_and_count(addon):
    rule = CompiledRule(name="prob", probability=0.0, count=1)
    addon.rules = (rule,)
    flow = make_flow()
    await addon.response(flow)
    assert addon._rule_hits.get("prob", 0) == 0


async def test_probability_partial_deterministic(monkeypatch, addon):
    # Force random.random() to return 0.3
    monkeypatch.setattr(addon._rand, "random", lambda: 0.3)

    rule = addon._compile_rules({"rules": [{"name": "prob-partial", "probability": 0.5, "error": {"body": "hit"}}]})[0]

    addon.rules = (rule,)

    flow = make_flow()
    await addon.request(flow)

    # 0.3 < 0.5 → should execute
    assert flow.response is not None


async def test_probability_partial_skip(monkeypatch, addon):
    monkeypatch.setattr(addon._rand, "random", lambda: 0.8)

    rule = addon._compile_rules(
        {"rules": [{"name": "prob-partial-skip", "probability": 0.5, "error": {"body": "hit"}}]}
    )[0]

    addon.rules = (rule,)

    flow = make_flow()
    await addon.request(flow)

    # 0.8 > 0.5 → should skip
    assert flow.response is None


# ========================================================
# Category: Body Handling and Limits
# ========================================================


async def test_invalid_json_skipped(addon):
    """An unparsable response body must not fire the rule's action."""
    rule = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "badjson",
                    "response_json": {"$.ok": True},
                    "modify_response_json": {"set": {"$.touched": True}},
                }
            ]
        }
    )[0]
    addon.rules = (rule,)
    flow = make_flow(response_content=b"not json")
    await addon.response(flow)
    assert response_content(flow) == b"not json"


async def test_large_body_skipped(addon):
    rule = CompiledRule(name="large", mod_resp_json={"set": {"$.x": 1}}, max_body_size=10)
    addon.rules = (rule,)
    big_body = b'{"x":0}' * 1000
    flow = make_flow(response_content=big_body)
    await addon.response(flow)
    assert b'"x":0' in response_content(flow)


async def test_modify_response_json_preserves_gzip_content_length(addon):
    """mitmproxy's `.content` setter re-encodes and sets the correct
    wire-length Content-Length itself; nothing should overwrite it with the
    decoded length afterwards."""
    rule = CompiledRule(name="gzip-mod", mod_resp_json={"set": {"$.x": 1}})
    addon.rules = (rule,)

    flow = make_flow()
    flow.response = Response.make(200, b'{"x":0}', {"content-type": "application/json"})
    flow.response.encode("gzip")

    await addon.response(flow)

    assert flow.response.headers.get("content-encoding") == "gzip"
    assert int(flow.response.headers["Content-Length"]) == len(flow.response.raw_content)
    assert response_json(flow)["x"] == 1


async def test_max_body_size_skip(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "limit", "max_body_size": 5, "modify_response_json": {"set": {"$.x": 1}}}]}
    )[0]

    addon.rules = (rule,)
    flow = make_flow(response_content=b'{"x":0}')
    await addon.response(flow)

    assert response_json(flow)["x"] == 0


def test_safe_repr_truncation_and_unrepresentable(addon):
    """_safe_repr truncates long strings and survives unrepresentable objects."""
    long_str = "a" * 300
    result = addon._safe_repr(long_str)

    assert len(result) == 203  # 200 chars + '...'
    assert result.startswith("'aaaaaaaa")
    assert result.endswith("...")  # no closing quote, by design

    class BadRepr:
        def __repr__(self):
            raise RuntimeError("boom")

    assert addon._safe_repr(BadRepr()) == "<unrepresentable value>"


# ========================================================
# Category: Delays (Global and Rule-Specific)
# ========================================================


async def test_global_delay_number(addon, monkeypatch):
    addon.global_delay_fixed = 0.1

    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    flow = make_flow()
    await addon.request(flow)

    fake_sleep.assert_called_once_with(0.1)


async def test_global_delay_zero_from_yaml(addon):
    addon.rules_file.write_text(yaml.dump({"global_delay": 0, "rules": []}))
    await addon._reload()
    assert addon.global_delay_fixed == 0.0


async def test_global_delay_random_array(addon, monkeypatch):
    addon.global_delay_range = (0.1, 0.2)

    monkeypatch.setattr(addon._rand, "uniform", lambda a, b: 0.15)
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    flow = make_flow()
    await addon.request(flow)

    fake_sleep.assert_called_once_with(0.15)


async def test_global_delay_random_single_number_yaml(addon, monkeypatch):
    raw = {"global_delay": {"random": 0.05}, "rules": []}
    await addon._reload()  # ensure clean
    addon.global_delay_fixed = None
    addon.global_delay_range = None

    monkeypatch.setattr(addon._rand, "uniform", lambda a, b: 0.0)
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    addon._compile_rules(raw)
    addon.global_delay_range = (0, 0.05)

    flow = make_flow()
    await addon.request(flow)

    fake_sleep.assert_called_once_with(0.0)


async def test_rule_delay_fixed(addon, monkeypatch):
    rule = CompiledRule(name="delay-fixed", delay_fixed=0.1)
    addon.rules = (rule,)

    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await addon.request(make_flow())

    fake_sleep.assert_called_once_with(0.1)


async def test_rule_delay_fixed_zero(addon):
    rule = addon._compile_rules({"rules": [{"name": "delay-zero", "delay": {"fixed": 0}}]})[0]
    assert rule.delay_fixed == 0.0


async def test_rule_delay_random_range(addon, monkeypatch):
    rule = addon._compile_rules({"rules": [{"name": "delay-range", "delay": {"random": [0.05, 0.1]}}]})[0]
    addon.rules = (rule,)

    monkeypatch.setattr(addon._rand, "uniform", lambda a, b: 0.07)
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    flow = make_flow()
    await addon.request(flow)

    fake_sleep.assert_called_once_with(0.07)


# ========================================================
# Category: Quiet and Logging
# ========================================================


async def test_global_quiet_no_logs(addon, caplog):
    caplog.set_level(logging.WARNING)
    addon.quiet = True
    rule = CompiledRule(name="quiet-global", inject_error={"body": "error"})
    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)
    assert "Injected error response" not in caplog.text


async def test_global_quiet_with_logs(addon, caplog):
    caplog.set_level(logging.WARNING)
    addon.quiet = False
    rule = CompiledRule(name="quiet-global-off", inject_error={"body": "error"})
    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)
    assert "Injected error response" in caplog.text


async def test_per_rule_quiet_no_logs(addon, caplog):
    caplog.set_level(logging.WARNING)
    addon.quiet = False  # Global on
    rule = CompiledRule(name="quiet-rule", quiet=True, inject_error={"body": "error"})
    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)
    assert "Injected error response" not in caplog.text


async def test_per_rule_quiet_override_global(addon, caplog):
    caplog.set_level(logging.WARNING)
    addon.quiet = True  # Global off
    rule = CompiledRule(name="quiet-rule-override", quiet=False, inject_error={"body": "error"})
    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)
    assert "Injected error response" in caplog.text


# ========================================================
# Category: Rule Enabling and Compilation
# ========================================================


async def test_rule_disabled_skipped(addon):
    raw = {"rules": [{"name": "disabled", "enabled": False, "error": {"body": "should not apply"}}]}
    rules = addon._compile_rules(raw)
    assert len(rules) == 0  # Skipped during compilation


async def test_rule_enabled_compiled(addon):
    raw = {"rules": [{"name": "enabled", "enabled": True, "error": {"body": "applies"}}]}
    rules = addon._compile_rules(raw)
    assert len(rules) == 1


# ========================================================
# Category: Matchers (Host, URL, Path, Status, Header, Query, JSON)
# ========================================================


async def test_matcher_header_contains_negative(addon):
    rule = CompiledRule(
        name="header-contains", header_contains=frozenset([("user-agent", "Mozilla")]), inject_error={"body": "matched"}
    )
    addon.rules = (rule,)
    flow = make_flow(headers={"User-Agent": "Chrome"})
    await addon.request(flow)
    assert flow.response is None


async def test_matcher_header_contains_positive(addon):
    rule = CompiledRule(
        name="header-contains", header_contains=frozenset([("user-agent", "Mozilla")]), inject_error={"body": "matched"}
    )
    addon.rules = (rule,)
    flow = make_flow(headers={"User-Agent": "Mozilla/5.0"})
    await addon.request(flow)
    assert flow.response is not None


async def test_matcher_host_regex_negative(addon):
    rule = CompiledRule(name="host-regex", host_regex=re.compile(r"example\.com"), inject_error={"body": "matched"})
    addon.rules = (rule,)
    flow = make_flow(url="https://other.com/path")
    await addon.request(flow)
    assert flow.response is None


async def test_matcher_host_regex_positive(addon):
    rule = CompiledRule(name="host-regex", host_regex=re.compile(r"example\.com"), inject_error={"body": "matched"})
    addon.rules = (rule,)
    flow = make_flow(url="https://api.example.com/path")
    await addon.request(flow)
    assert flow.response is not None


async def test_matcher_json_body_exists_false(addon):
    rule = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "json-not-exists",
                    "json_body": {"$.missing": {"$exists": False}},
                    "error": {"body": "matched"},
                }
            ]
        }
    )[0]

    addon.rules = (rule,)

    flow = make_flow(content=b'{"present":1}')
    await addon.request(flow)
    assert flow.response is not None

    flow_bad = make_flow(content=b'{"missing":1}')
    await addon.request(flow_bad)
    assert flow_bad.response is None


async def test_matcher_json_body_value_equality(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "json-eq", "json_body": {"$.type": "admin"}, "error": {"body": "matched"}}]}
    )[0]

    addon.rules = (rule,)

    flow = make_flow(content=b'{"type":"admin"}')
    await addon.request(flow)
    assert flow.response is not None

    flow_bad = make_flow(content=b'{"type":"user"}')
    await addon.request(flow_bad)
    assert flow_bad.response is None


async def test_matcher_path_contains_negative(addon):
    rule = CompiledRule(name="path-contains", path_contains="users", inject_error={"body": "matched"})
    addon.rules = (rule,)
    flow = make_flow(url="https://example.com/api/other")
    await addon.request(flow)
    assert flow.response is None


async def test_matcher_path_contains_positive(addon):
    rule = CompiledRule(name="path-contains", path_contains="users", inject_error={"body": "matched"})
    addon.rules = (rule,)
    flow = make_flow(url="https://example.com/api/users")
    await addon.request(flow)
    assert flow.response is not None


async def test_matcher_path_regex_negative(addon):
    rule = CompiledRule(name="path-regex", path_regex=re.compile(r"/users/\d+"), inject_error={"body": "matched"})
    addon.rules = (rule,)
    flow = make_flow(url="https://example.com/users/abc")
    await addon.request(flow)
    assert flow.response is None


async def test_matcher_path_regex_positive(addon):
    rule = CompiledRule(name="path-regex", path_regex=re.compile(r"/users/\d+"), inject_error={"body": "matched"})
    addon.rules = (rule,)
    flow = make_flow(url="https://example.com/users/123")
    await addon.request(flow)
    assert flow.response is not None


async def test_matcher_query_contains_negative(addon):
    rule = CompiledRule(
        name="query-contains", query_contains=frozenset([("search", "test")]), inject_error={"body": "matched"}
    )
    addon.rules = (rule,)
    flow = make_flow(url="https://example.com?search=other")
    await addon.request(flow)
    assert flow.response is None


async def test_matcher_query_contains_positive(addon):
    rule = CompiledRule(
        name="query-contains", query_contains=frozenset([("search", "test")]), inject_error={"body": "matched"}
    )
    addon.rules = (rule,)
    flow = make_flow(url="https://example.com?search=test_query")
    await addon.request(flow)
    assert flow.response is not None


async def test_matcher_response_json_exists(addon):
    rule = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "resp-exists",
                    "response_json": {"$.data": {"$exists": True}},
                    "modify_response_json": {"set": {"$.matched": True}},
                }
            ]
        }
    )[0]

    addon.rules = (rule,)

    flow = make_flow(response_content=b'{"data":1}')
    await addon.response(flow)
    assert response_json(flow)["matched"] is True

    flow_bad = make_flow(response_content=b"{}")
    await addon.response(flow_bad)
    assert "matched" not in response_json(flow_bad)


async def test_matcher_status_code_negative(addon):
    rule = CompiledRule(name="status-code", status_codes=frozenset([404]), mod_resp_json={"set": {"$.error": True}})
    addon.rules = (rule,)
    flow = make_flow(status=200, response_content=b"{}")
    await addon.response(flow)
    assert "error" not in response_json(flow)


async def test_matcher_status_code_positive(addon):
    rule = CompiledRule(name="status-code", status_codes=frozenset([404]), mod_resp_json={"set": {"$.error": True}})
    addon.rules = (rule,)
    flow = make_flow(status=404, response_content=b"{}")
    await addon.response(flow)
    assert response_json(flow)["error"] is True


async def test_matcher_status_code_single_int(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "status-single", "status_code": 404, "modify_response_json": {"set": {"$.error": True}}}]}
    )[0]
    addon.rules = (rule,)
    flow = make_flow(status=404, response_content=b"{}")
    await addon.response(flow)
    assert response_json(flow)["error"] is True


async def test_matcher_url_contains_negative(addon):
    rule = CompiledRule(name="url-contains", url_contains="/api/v1", inject_error={"body": "matched"})
    addon.rules = (rule,)
    flow = make_flow(url="https://example.com/other")
    await addon.request(flow)
    assert flow.response is None


async def test_matcher_url_contains_positive(addon):
    rule = CompiledRule(name="url-contains", url_contains="/api/v1", inject_error={"body": "matched"})
    addon.rules = (rule,)
    flow = make_flow(url="https://example.com/api/v1/users")
    await addon.request(flow)
    assert flow.response is not None


async def test_matcher_url_regex_positive(addon):
    rule = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "url-regex",
                    "url_regex": r"/v1/orders/\d+",
                    "modify_response_json": {"set": {"$.matched": True}},
                }
            ]
        }
    )[0]

    addon.rules = (rule,)

    flow = make_flow(url="https://api.example.com/v1/orders/123", response_content=b"{}")

    await addon.response(flow)
    data = response_json(flow)
    assert data["matched"] is True


async def test_request_matcher_with_response_action(addon):
    rule = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "mixed-phase",
                    "path_contains": "/test",
                    "json_body": {"$.sku": "match-sku"},
                    "modify_response_json": {"set": {"$.result": "modified"}},
                }
            ]
        }
    )[0]
    addon.rules = (rule,)
    flow = make_flow(
        url="https://example.com/test", content=b'{"sku": "no-match"}', response_content=b'{"result": "original"}'
    )
    await addon.request(flow)  # Request phase: should skip due to matcher
    await addon.response(flow)  # Response phase: should skip due to matcher
    assert response_json(flow)["result"] == "original"  # Unmodified

    flow_match = make_flow(
        url="https://example.com/test", content=b'{"sku": "match-sku"}', response_content=b'{"result": "original"}'
    )
    await addon.request(flow_match)
    await addon.response(flow_match)
    assert response_json(flow_match)["result"] == "modified"  # Modified


# ========================================================
# Category: JSON Modifications (Request)
# ========================================================


async def test_modify_request_json_filtered_delete(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "req-filter-del", "modify_request_json": {"delete": ["$.items[?(@.id==2)]"]}}]}
    )[0]

    addon.rules = (rule,)

    body = json.dumps({"items": [{"id": 1}, {"id": 2}]}).encode()

    flow = make_flow(content=body)
    await addon.request(flow)

    data = request_json(flow)
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == 1


async def test_modify_request_json_filtered_set(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "req-filter-set", "modify_request_json": {"set": {"$.items[?(@.id==2)].value": 999}}}]}
    )[0]

    addon.rules = (rule,)

    body = json.dumps({"items": [{"id": 1, "value": 10}, {"id": 2, "value": 20}]}).encode()

    flow = make_flow(content=body)
    await addon.request(flow)

    data = request_json(flow)
    assert data["items"][1]["value"] == 999


async def test_set_request_json_filter(addon):
    rule = CompiledRule(name="req-filter", mod_req_json={"set": {"$.filter.active": True}})
    addon.rules = (rule,)
    body = json.dumps({"filter": {"active": False}}).encode()
    flow = make_flow(content=body)
    await addon.request(flow)
    data = request_json(flow)
    assert data["filter"]["active"] is True


# ========================================================
# Category: JSON Modifications (Response - Append)
# ========================================================


async def test_append_line_item_by_productid(addon):
    rule = CompiledRule(
        name="append-line", mod_resp_json={"append": {"$.lineItems": {"productId": 999, "quantity": 1}}}
    )
    addon.rules = (rule,)
    body = json.dumps({"lineItems": [{"productId": 111}]}).encode()
    flow = make_flow(response_content=body)
    await addon.response(flow)
    data = response_json(flow)
    assert len(data["lineItems"]) == 2


# ========================================================
# Category: JSON Modifications (Response - Delete)
# ========================================================


async def test_delete_specific_variant(addon):
    rule = CompiledRule(name="del-variant", mod_resp_json={"delete": ['$.variants[?(@.id=="V456")]']})
    addon.rules = (rule,)
    body = json.dumps({"variants": [{"id": "V123"}, {"id": "V456"}]}).encode()
    flow = make_flow(response_content=body)
    await addon.response(flow)
    data = response_json(flow)
    assert len(data["variants"]) == 1


# ========================================================
# Category: JSON Modifications (Response - Set/Update)
# ========================================================


async def test_combined_primitives_set(addon):
    rule = CompiledRule(
        name="combined-primitives",
        mod_resp_json={"set": {"$.flag": True, "$.count": 42, "$.msg": "done", "$.data": None}},
    )
    addon.rules = (rule,)
    body = json.dumps({}).encode()
    flow = make_flow(response_content=body)
    await addon.response(flow)
    data = response_json(flow)
    assert data == {"flag": True, "count": 42, "msg": "done", "data": None}


async def test_ecom_update_price_by_sku(addon):
    rule = CompiledRule(name="sku-price", mod_resp_json={"set": {'$.items[?(@.sku=="30031880")].price': 10000}})
    addon.rules = (rule,)
    body = json.dumps(
        {"items": [{"sku": "123", "price": 50}, {"sku": "30031880", "price": 99.99}, {"sku": "999", "price": 200}]}
    ).encode()
    flow = make_flow(response_content=body)
    await addon.response(flow)
    data = response_json(flow)
    assert data["items"][1]["price"] == 10000


# ========================================================
# Category: JSON Modifications (Response - Deep/Nested)
# ========================================================


async def test_deep_large_payload_mod(addon):
    rule = CompiledRule(
        name="deep-prod", mod_resp_json={"set": {"$.response.body.data[0].attributes.status": "processed"}}
    )
    addon.rules = (rule,)
    body = json.dumps({"response": {"body": {"data": [{"attributes": {"status": "pending"}}]}}}).encode()
    flow = make_flow(response_content=body)
    await addon.response(flow)
    data = response_json(flow)
    assert data["response"]["body"]["data"][0]["attributes"]["status"] == "processed"


async def test_modify_nested_graphql_connection(addon):
    rule = CompiledRule(
        name="gql-connection", mod_resp_json={"set": {"$.data.user.orders.edges[0].node.status": "shipped"}}
    )
    addon.rules = (rule,)
    body = json.dumps({"data": {"user": {"orders": {"edges": [{"node": {"status": "pending"}}]}}}}).encode()
    flow = make_flow(response_content=body)
    await addon.response(flow)
    data = response_json(flow)
    assert data["data"]["user"]["orders"]["edges"][0]["node"]["status"] == "shipped"


async def test_modify_response_json_without_content_type(addon):
    rule = CompiledRule(name="no-ct", mod_resp_json={"set": {"$.ok": True}})
    addon.rules = (rule,)
    flow = make_flow()
    flow.response = Response.make(200, b'{"ok": false}', {})
    await addon.response(flow)
    data = response_json(flow)
    assert data["ok"] is True


# ========================================================
# Category: Stateful Rules
# ========================================================


async def test_state_require_and_set(addon):
    rules = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "step-1",
                    "state": {"require": {"stage": {"$exists": False}}, "set": {"stage": "one"}},
                    "error": {"body": "first"},
                },
                {
                    "name": "step-2",
                    "state": {"require": {"stage": "one"}, "set": {"stage": "two"}},
                    "error": {"body": "second"},
                },
            ]
        }
    )
    addon.rules = tuple(rules)

    flow1 = make_flow()
    await addon.request(flow1)
    assert addon._state.get("stage") == "one"

    flow2 = make_flow()
    await addon.request(flow2)
    assert addon._state.get("stage") == "two"
    assert flow2.response is not None


async def test_state_shorthand_require(addon):
    addon._state["mode"] = "blue"
    rule = addon._compile_rules(
        {"rules": [{"name": "state-short", "state": {"mode": "blue"}, "error": {"body": "hit"}}]}
    )[0]
    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)
    assert flow.response is not None


# ========================================================
# Category: Cycle and Random Responses
# ========================================================


async def test_cycle_respond_with(addon):
    rule = addon._compile_rules(
        {
            "rules": [
                {"name": "cycle", "cycle": [{"status": 200, "body": {"step": 1}}, {"status": 200, "body": {"step": 2}}]}
            ]
        }
    )[0]
    addon.rules = (rule,)

    flow1 = make_flow()
    await addon.request(flow1)
    assert response_json(flow1)["step"] == 1

    flow2 = make_flow()
    await addon.request(flow2)
    assert response_json(flow2)["step"] == 2

    flow3 = make_flow()
    await addon.request(flow3)
    assert response_json(flow3)["step"] == 1


async def test_random_respond_with(monkeypatch, addon):
    rule = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "random",
                    "random": [{"status": 200, "body": {"step": 1}}, {"status": 200, "body": {"step": 2}}],
                }
            ]
        }
    )[0]
    addon.rules = (rule,)

    monkeypatch.setattr(addon._rand, "choice", lambda seq: seq[1])
    flow = make_flow()
    await addon.request(flow)
    assert response_json(flow)["step"] == 2


# ========================================================
# Category: Watcher and Async Handling
# ========================================================


async def test_watcher_file_change_reload(addon, caplog, monkeypatch):
    """The watcher notices a change to rules.yaml and reloads."""
    caplog.set_level(logging.INFO)

    addon.rules_file.write_text(yaml.dump({"rules": [{"name": "initial", "error": {"body": "old"}}]}))
    await addon._reload()

    # Rewrite before the mock runs, so the watcher reads the new content.
    new_yaml = yaml.dump({"rules": [{"name": "updated", "error": {"body": "new via watcher"}}]})
    addon.rules_file.write_text(new_yaml)

    # One change, then the iterator ends.
    async def mock_awatch(*args, **kwargs):
        yield [(1, str(addon.rules_file.resolve()))]  # simulate "modified"
        await asyncio.sleep(0.01)

    monkeypatch.setattr("watchfiles.awatch", mock_awatch)

    task = asyncio.create_task(addon._watcher())

    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert any("Reloaded" in msg for msg in caplog.messages)
    assert any(r.name == "updated" for r in addon.rules)


async def test_watcher_exception_handling(addon, monkeypatch):
    """_watcher catches an exception from awatch and logs a warning."""

    mock_logger_warning = Mock()
    monkeypatch.setattr("stunt.addon.logger.warning", mock_logger_warning)

    class FailingAsyncIterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ValueError("Watch error")

    # Patch in the stunt namespace: addon.py does `from watchfiles import awatch`.
    with patch("stunt.addon.awatch") as mock_awatch:
        mock_awatch.return_value = FailingAsyncIterator()

        try:
            await asyncio.wait_for(addon._watcher(), timeout=3.0)
        except asyncio.TimeoutError:
            pytest.fail("_watcher() did not return within 3s even with awatch mocked")

    mock_logger_warning.assert_called_once()
    logged_msg = mock_logger_warning.call_args[0][0]
    assert "Stunt watcher stopped: Watch error" in logged_msg


async def test_done_cancel_watcher(addon, caplog, monkeypatch):
    """done() cancels the watcher and logs the cancellation."""
    caplog.set_level(logging.INFO, logger="stunt")

    # Mock awatch before the watcher starts, to avoid a race.
    async def mock_awatch(*args, **kwargs):
        while True:
            await asyncio.sleep(0.5)
            yield []

    monkeypatch.setattr("watchfiles.awatch", mock_awatch)

    if addon._watcher_task is None or addon._watcher_task.done():
        addon.running()

    assert addon._watcher_task is not None
    assert not addon._watcher_task.done(), "Task should be running"

    addon.done()

    try:
        await asyncio.wait_for(addon._watcher_task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass

    assert addon._watcher_task.done()
    assert "Stunt watcher cancelled" in caplog.text


# ========================================================
# Category: Category: Mitmproxy Integration
# ========================================================


def test_load_options():
    """load() registers every mitmproxy option."""
    mock_loader = Mock()
    from stunt.addon import load

    load(mock_loader)

    assert mock_loader.add_option.call_count == 8

    names = [call.kwargs["name"] for call in mock_loader.add_option.call_args_list]
    assert "stunt_rules" in names
    assert "stunt_mocks_dir" in names
    assert "stunt_trace" in names
    assert "stunt_record" in names
    assert "stunt_record_host" in names
    assert "stunt_record_path" in names
    assert "stunt_record_force" in names
    assert "stunt_record_raw" in names


# ========================================================
# Category: CLI Integration
# ========================================================


def test_cli_exit_code(monkeypatch):
    from stunt import cli

    captured = {}

    class Result:
        returncode = 7

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return Result()

    monkeypatch.setattr("stunt.cli.subprocess.run", fake_run)
    monkeypatch.setattr("stunt.cli.sys.argv", ["stunt"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 7
    assert Path(captured["cmd"][0]).name.startswith("mitmweb")


# ========================================================
# Category: `stunt init`
# ========================================================


def test_init_creates_rules_and_mock(tmp_path, monkeypatch, capsys):
    from stunt import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("stunt.cli.sys.argv", ["stunt", "init"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    rules_path = tmp_path / "rules.yaml"
    mock_path = tmp_path / "mocks" / "example_user.json"
    assert rules_path.is_file()
    assert mock_path.is_file()
    assert "$schema=" in rules_path.read_text()
    assert "mock:" in rules_path.read_text()
    out = capsys.readouterr().out
    assert "created" in out
    assert "Next steps" in out


def test_init_refuses_to_clobber_without_force(tmp_path, monkeypatch, capsys):
    from stunt import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules.yaml").write_text("# my precious rules\n")

    monkeypatch.setattr("stunt.cli.sys.argv", ["stunt", "init"])
    with pytest.raises(SystemExit):
        cli.main()

    assert (tmp_path / "rules.yaml").read_text() == "# my precious rules\n"
    out = capsys.readouterr().out
    assert "skipped" in out

    monkeypatch.setattr("stunt.cli.sys.argv", ["stunt", "init", "--force"])
    with pytest.raises(SystemExit):
        cli.main()
    assert (tmp_path / "rules.yaml").read_text() != "# my precious rules\n"


# ========================================================
# Category: `stunt lint`
# ========================================================


def test_lint_exits_nonzero_on_bad_file(tmp_path, monkeypatch, capsys):
    from stunt import cli

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        yaml.dump(
            {
                "rules": [
                    {"name": "typo-key", "respnd_with": {"status": 200, "body": {}}},
                ]
            }
        )
    )
    (tmp_path / "mocks").mkdir()

    monkeypatch.setattr(
        "stunt.cli.sys.argv",
        ["stunt", "lint", "--rules", str(rules_path), "--mocks", str(tmp_path / "mocks")],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0
    out = capsys.readouterr().out
    assert "typo-key" in out or "unknown key" in out


def test_lint_exits_zero_on_good_file(tmp_path, monkeypatch, capsys):
    from stunt import cli

    rules_path = tmp_path / "rules.yaml"
    mocks_dir = tmp_path / "mocks"
    mocks_dir.mkdir()
    rules_path.write_text(
        yaml.dump(
            {
                "rules": [
                    {"name": "ok-rule", "path_regex": "^/api/x$", "respond_with": {"status": 200, "body": {}}},
                ]
            }
        )
    )

    monkeypatch.setattr(
        "stunt.cli.sys.argv",
        ["stunt", "lint", "--rules", str(rules_path), "--mocks", str(mocks_dir)],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "OK" in capsys.readouterr().out


def test_lint_catches_unknown_key_missing_mock_and_matcherless_rule(tmp_path):
    from stunt.addon import lint_rules

    rules_path = tmp_path / "rules.yaml"
    mocks_dir = tmp_path / "mocks"
    mocks_dir.mkdir()
    rules_path.write_text(
        yaml.dump(
            {
                "rules": [
                    {"name": "bad-key", "path_regex": "^/x$", "respnd_with": {"status": 200, "body": {}}},
                    {"name": "missing-mock", "path_regex": "^/y$", "respond_with": {"file": "nope.json"}},
                    {"name": "matcher-less", "respond_with": {"status": 200, "body": {}}},
                ]
            }
        )
    )

    problems = lint_rules(rules_path, mocks_dir)
    joined = "\n".join(problems)
    assert "unknown key" in joined
    assert "mock file not found" in joined
    assert "no matchers" in joined


def test_lint_reports_yaml_syntax_error(tmp_path):
    from stunt.addon import lint_rules

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules:\n  - name: broken\n      bad indent: [1, 2\n")
    problems = lint_rules(rules_path, tmp_path / "mocks")
    assert len(problems) == 1
    assert "YAML syntax error" in problems[0]


# ========================================================
# Category: match-trace log
# ========================================================


async def test_trace_logs_rejection_reason_when_enabled(addon, caplog):
    caplog.set_level(logging.INFO, logger="stunt")
    addon.trace = True
    addon.rules = tuple(
        addon._compile_rules(
            {
                "rules": [
                    {
                        "name": "wants-post",
                        "method": "POST",
                        "respond_with": {"status": 200, "body": {}},
                    }
                ]
            }
        )
    )
    flow = make_flow(method="GET", url="https://api.example.com/api/x")
    await addon.request(flow)
    assert "wants-post" in caplog.text
    assert "method" in caplog.text
    assert "GET" in caplog.text


async def test_trace_silent_when_disabled(addon, caplog):
    caplog.set_level(logging.INFO, logger="stunt")
    addon.trace = False
    addon.rules = tuple(
        addon._compile_rules(
            {
                "rules": [
                    {
                        "name": "wants-post",
                        "method": "POST",
                        "respond_with": {"status": 200, "body": {}},
                    }
                ]
            }
        )
    )
    flow = make_flow(method="GET", url="https://api.example.com/api/x")
    await addon.request(flow)
    assert "trace" not in caplog.text.lower()


# ========================================================
# Category: Phase-split actions
# ========================================================


async def run_cycle(addon, flow=None, upstream=b'{"upstream": true}', status=200):
    """One full request/response cycle through the real mitmproxy hooks.

    Stands in for the proxy core: if nothing mocked the request, an upstream
    response is attached before the response hook, exactly as mitmproxy would.
    """
    flow = flow if flow is not None else make_flow()
    await addon.request(flow)
    mocked_in_request_phase = flow.response is not None
    if flow.response is None:
        flow.response = Response.make(status, upstream, {"content-type": "application/json"})
    await addon.response(flow)
    flow.metadata["mocked_in_request_phase"] = mocked_in_request_phase
    return flow


def _rules(addon, *raw):
    addon.rules = tuple(sorted(addon._compile_rules({"rules": list(raw)}), key=lambda r: r.priority, reverse=True))


async def test_once_serves_exactly_one_mock_over_three_cycles(addon):
    """`once` serves exactly one response: a hit is charged on one phase only."""
    _rules(addon, {"name": "once-mock", "once": True, "respond_with": {"body": {"mock": True}}})

    served = [json.loads(response_content(await run_cycle(addon))).get("mock") is True for _ in range(3)]

    assert served.count(True) == 1
    assert served[0] is True
    assert addon._rule_hits["once-mock"] == 1


async def test_count_three_serves_exactly_three_mocks_over_five_cycles(addon):
    """`count: 3` serves three responses."""
    _rules(addon, {"name": "count-mock", "count": 3, "respond_with": {"body": {"mock": True}}})

    served = [json.loads(response_content(await run_cycle(addon))).get("mock") is True for _ in range(5)]

    assert served == [True, True, True, False, False]
    assert addon._rule_hits["count-mock"] == 3


async def test_state_flow_returns_step_one_then_step_two(addon):
    """A step must not satisfy its own gate mid-cycle and let the next step through."""
    _rules(
        addon,
        {
            "name": "step-1",
            "priority": 10,
            "state": {"require": {"stage": {"$exists": False}}, "set": {"stage": "one"}},
            "respond_with": {"body": {"step": 1}},
        },
        {
            "name": "step-2",
            "priority": 5,
            "state": {"require": {"stage": "one"}, "set": {"stage": "two"}},
            "respond_with": {"body": {"step": 2}},
        },
    )

    assert json.loads(response_content(await run_cycle(addon)))["step"] == 1
    assert json.loads(response_content(await run_cycle(addon)))["step"] == 2
    assert addon._state["stage"] == "two"


async def test_delay_applied_once_per_cycle(addon, monkeypatch):
    """A rule's delay is paid once per flow, not once per phase."""
    _rules(addon, {"name": "slow", "delay": {"fixed": 0.25}, "respond_with": {"body": {"mock": True}}})

    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await run_cycle(addon)

    delays = [c.args[0] for c in fake_sleep.await_args_list]
    assert delays == [0.25]


async def test_respond_with_fires_in_request_phase(addon):
    """The mock must exist before the request is ever forwarded upstream."""
    _rules(addon, {"name": "req-phase", "respond_with": {"status": 201, "body": {"mock": True}}})

    flow = make_flow()
    await addon.request(flow)

    assert flow.response is not None
    assert flow.response.status_code == 201
    assert json.loads(flow.response.content)["mock"] is True


async def test_passthrough_keeps_respond_with_in_response_phase(addon):
    _rules(addon, {"name": "pass", "passthrough": True, "respond_with": {"body": {"mock": True}}})

    flow = make_flow()
    await addon.request(flow)
    assert flow.response is None

    flow = await run_cycle(addon, flow)
    assert json.loads(response_content(flow))["mock"] is True


async def test_response_only_rule_does_not_suppress_request_phase_rule(addon):
    """A rule matching on status_code cannot be decided in the request phase, so it must
    not consume the scan there and hide every lower-priority rule."""
    _rules(
        addon,
        {
            "name": "resp-only",
            "priority": 100,
            "status_code": 200,
            "modify_response_json": {"set": {"$.touched": True}},
        },
        {"name": "mock", "priority": 1, "respond_with": {"body": {"mock": True}}},
    )

    flow = await run_cycle(addon)

    assert flow.metadata["mocked_in_request_phase"] is True
    assert json.loads(response_content(flow))["mock"] is True
    assert addon._rule_hits.get("resp-only", 0) == 0


async def test_rule_with_typoed_action_warns_and_does_not_swallow(addon, caplog):
    """`respond:` instead of `respond_with:` would match everything and do nothing, so
    ended the scan without a word."""
    caplog.set_level(logging.WARNING)
    _rules(
        addon,
        {"name": "typo", "priority": 100, "respond": {"body": {"oops": True}}},
        {"name": "real", "priority": 1, "respond_with": {"body": {"mock": True}}},
    )

    assert [r.name for r in addon.rules] == ["real"]
    assert "typo" in caplog.text and "no recognised action" in caplog.text

    flow = await run_cycle(addon)
    assert json.loads(response_content(flow))["mock"] is True


async def test_unknown_rule_key_warns_with_rule_and_key(addon, caplog):
    """A typo'd rule key (e.g. `path_regexp`) must warn rather than be ignored, since a
    rule with no matchers matches every request, one typo could swallow all traffic."""
    caplog.set_level(logging.WARNING)
    _rules(addon, {"name": "bad-key", "path_regexp": "^/x$", "respond_with": {"body": {"mock": True}}})

    assert "bad-key" in caplog.text
    assert "path_regexp" in caplog.text


async def test_unknown_rule_key_suggests_close_match(addon, caplog):
    """A near-miss typo should name the likely intended key."""
    caplog.set_level(logging.WARNING)
    _rules(addon, {"name": "typo-key", "path_regexp": "^/x$", "respond_with": {"body": {"mock": True}}})

    assert "path_regex" in caplog.text


async def test_valid_rules_file_has_no_unknown_key_warning(addon, caplog):
    """Guard: a rules file using only recognised keys must not trigger the warning."""
    caplog.set_level(logging.WARNING)
    addon.rules_file.write_text(
        yaml.dump(
            {
                "quiet": False,
                "global_delay": 0,
                "state": {"step": 1},
                "rules": [
                    {
                        "name": "clean",
                        "host": "api\\.example\\.com",
                        "path_regex": "^/x$",
                        "method": "GET",
                        "respond_with": {"body": {"mock": True}},
                    }
                ],
            }
        )
    )
    await addon._reload()

    assert "unknown key" not in caplog.text


async def test_missing_mock_file_logs_rule_and_filename(addon, caplog):
    """A missing mock file must be logged, not swallowed with the request going upstream."""
    caplog.set_level(logging.WARNING)
    _rules(addon, {"name": "bad-mock", "respond_with": {"file": "nope.json"}})

    flow = make_flow()
    await addon.request(flow)

    assert flow.response is None
    assert "bad-mock" in caplog.text
    assert "nope.json" in caplog.text


# ========================================================
# Category: config discovery / mocks/ littering
# ========================================================


async def test_running_with_no_rules_file_loads_nothing_warns_creates_no_mocks_dir(tmp_path, caplog):
    """A root with no rules.yaml (e.g. discovery landing in an unrelated repo) must
    load zero rules, warn loudly, and must NOT create a stray mocks/ directory."""
    caplog.set_level(logging.WARNING)
    a = Stunt()
    a.rules_file = tmp_path / "rules.yaml"
    a.mocks_dir = tmp_path / "mocks"

    a.running()
    try:
        assert len(a.rules) == 0
        assert not a.mocks_dir.exists()
        assert str(a.rules_file) in caplog.text
        assert "--rules" in caplog.text or "STUNT_HOME" in caplog.text
    finally:
        await a.aclose()


# ========================================================
# Category: modify_request_headers / query, modify_response_headers
# ========================================================


async def test_modify_request_headers_set(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "req-headers-set", "modify_request_headers": {"set": {"X-Injected": "yes"}}}]}
    )[0]
    addon.rules = (rule,)
    flow = make_flow()
    await addon.request(flow)
    assert flow.request.headers["X-Injected"] == "yes"


async def test_modify_request_headers_remove(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "req-headers-remove", "modify_request_headers": {"remove": ["X-Drop-Me"]}}]}
    )[0]
    addon.rules = (rule,)
    flow = make_flow(headers={"X-Drop-Me": "gone"})
    await addon.request(flow)
    assert "X-Drop-Me" not in flow.request.headers


async def test_modify_request_query_set(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "req-query-set", "modify_request_query": {"set": {"debug": "1"}}}]}
    )[0]
    addon.rules = (rule,)
    flow = make_flow(url="https://api.example.com/path")
    await addon.request(flow)
    assert flow.request.query["debug"] == "1"


async def test_modify_request_query_remove(addon):
    rule = addon._compile_rules(
        {"rules": [{"name": "req-query-remove", "modify_request_query": {"remove": ["token"]}}]}
    )[0]
    addon.rules = (rule,)
    flow = make_flow(url="https://api.example.com/path?token=secret&keep=1")
    await addon.request(flow)
    assert "token" not in flow.request.query
    assert flow.request.query["keep"] == "1"


async def test_modify_response_headers_set(addon):
    rule = CompiledRule(name="resp-headers-set", mod_resp_headers={"set": {"X-Modified": "yes"}})
    addon.rules = (rule,)
    flow = make_flow(response_content=b"{}")
    await addon.response(flow)
    assert flow.response.headers["X-Modified"] == "yes"


async def test_modify_response_headers_remove(addon):
    rule = CompiledRule(name="resp-headers-remove", mod_resp_headers={"remove": ["X-Secret"]})
    addon.rules = (rule,)
    flow = make_flow(response_content=b"{}")
    flow.response.headers["X-Secret"] = "shh"
    await addon.response(flow)
    assert "X-Secret" not in flow.response.headers


# ========================================================
# Category: double-processing guard (stunt_handled)
# ========================================================


async def test_stunt_handled_flow_not_reprocessed_in_response_phase(addon):
    """A flow fully served in the request phase must be skipped entirely by the
    response hook, so a second, lower-priority rule that would otherwise modify
    the response never runs."""
    _rules(
        addon,
        {"name": "mocker", "priority": 10, "respond_with": {"body": {"mock": True}}},
        {"name": "modifier", "priority": 1, "modify_response_json": {"set": {"$.touched": True}}},
    )

    flow = make_flow()
    await addon.request(flow)
    assert flow.metadata.get("stunt_handled") is True
    assert response_json(flow) == {"mock": True}

    # Simulate mitmproxy attaching an upstream response and calling response() —
    # the guard must make this a no-op.
    flow.response = Response.make(200, json.dumps({"mock": True}).encode(), {"content-type": "application/json"})
    flow.metadata["stunt_handled"] = True  # mitmproxy preserves flow.metadata across the cycle
    await addon.response(flow)

    assert response_json(flow) == {"mock": True}
    assert addon._rule_hits.get("modifier", 0) == 0


# ========================================================
# Category: mock-file hot reload via the real watcher
# ========================================================


async def test_watcher_reloads_edited_mock_file(addon):
    """The watcher clears the mtime-keyed mock cache when a file under
    mocks/ changes, so the next request serves the new content — not a stale
    cached copy."""
    mock_path = addon.mocks_dir / "watched.json"
    mock_path.write_text('{"name": "old"}')

    rule = addon._compile_rules({"rules": [{"name": "mock-watched", "respond_with": {"file": "watched.json"}}]})[0]
    addon.rules = (rule,)

    addon._watcher_stop = asyncio.Event()
    watcher_task = asyncio.get_event_loop().create_task(addon._watcher())
    try:
        # Give the underlying (rust-backed) fs watcher a moment to actually
        # register its inotify watch before we act — starting the task alone
        # doesn't guarantee the watch is armed yet.
        await asyncio.sleep(0.3)

        # Prime the cache with the old content.
        flow1 = make_flow()
        await addon.request(flow1)
        assert b'"old"' in response_content(flow1)
        assert "watched.json" in addon._mock_cache

        # Edit the mock file — the watcher must clear the cache entry.
        mock_path.write_text('{"name": "new"}')

        deadline = asyncio.get_event_loop().time() + 3.0
        while "watched.json" in addon._mock_cache:
            if asyncio.get_event_loop().time() > deadline:
                pytest.fail("watcher did not clear the mock cache within 3s")
            await asyncio.sleep(0.02)

        flow2 = make_flow()
        await addon.request(flow2)
        assert b'"new"' in response_content(flow2)
    finally:
        addon._watcher_stop.set()
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass


def test_cli_explicit_rules_flag_honoured_over_discovery(monkeypatch, tmp_path):
    """cli.py's --rules/--mocks must reach the addon as --set stunt_rules=...
    / --set stunt_mocks_dir=..., overriding whatever _default_root() would
    have picked via $STUNT_HOME / cwd walk-up / $VIRTUAL_ENV."""
    from stunt import cli

    captured = {}

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("stunt.cli.subprocess.run", fake_run)
    rules_path = tmp_path / "custom-rules.yaml"
    mocks_path = tmp_path / "custom-mocks"
    monkeypatch.setattr(
        "stunt.cli.sys.argv",
        ["stunt", "--mode", "dump", "--rules", str(rules_path), "--mocks", str(mocks_path)],
    )

    with pytest.raises(SystemExit):
        cli.main()

    cmd = captured["cmd"]
    assert f"stunt_rules={rules_path.resolve()}" in cmd
    assert f"stunt_mocks_dir={mocks_path.resolve()}" in cmd


# ========================================================
# Category: mock: shorthand and defaults:
# ========================================================


async def load(addon, config):
    addon.rules_file.write_text(yaml.dump(config))
    await addon._reload()


async def test_mock_bare_body(addon):
    await load(addon, {"mock": {"/api/users": {"id": 1, "name": "Ada"}}})
    flow = make_flow(url="https://api.example.com/api/users")
    await addon.request(flow)
    assert flow.response.status_code == 200
    assert response_json(flow) == {"id": 1, "name": "Ada"}


async def test_mock_bare_body_list_and_scalar(addon):
    await load(addon, {"mock": {"/list": [1, 2], "/n": 7}})
    flow = make_flow(url="https://api.example.com/list")
    await addon.request(flow)
    assert response_json(flow) == [1, 2]
    flow = make_flow(url="https://api.example.com/n")
    await addon.request(flow)
    assert response_json(flow) == 7


async def test_mock_respond_with_object(addon):
    await load(addon, {"mock": {"POST /api/orders": {"status": 201, "body": {"ok": True}}}})
    flow = make_flow(method="POST", url="https://api.example.com/api/orders")
    await addon.request(flow)
    assert flow.response.status_code == 201
    assert response_json(flow) == {"ok": True}


async def test_mock_body_containing_status_string_is_a_body(addon):
    """A dict is a respond_with object only if every key is a respond_with key
    AND `status` (if present) is an int. {"status": "up"} is a body."""
    await load(addon, {"mock": {"/health": {"status": "up"}}})
    flow = make_flow(url="https://api.example.com/health")
    await addon.request(flow)
    assert flow.response.status_code == 200
    assert response_json(flow) == {"status": "up"}


async def test_mock_body_with_extra_key_is_a_body(addon):
    await load(addon, {"mock": {"/m": {"status": 201, "id": 5}}})
    flow = make_flow(url="https://api.example.com/m")
    await addon.request(flow)
    assert flow.response.status_code == 200
    assert response_json(flow) == {"status": 201, "id": 5}


async def test_mock_method_constrained(addon):
    await load(addon, {"mock": {"POST /api/orders": {"ok": True}}})
    miss = make_flow(method="GET", url="https://api.example.com/api/orders")
    await addon.request(miss)
    assert miss.response is None
    hit = make_flow(method="POST", url="https://api.example.com/api/orders")
    await addon.request(hit)
    assert response_json(hit) == {"ok": True}


async def test_mock_methodless_key_matches_any_method(addon):
    await load(addon, {"mock": {"/any": {"ok": True}}})
    for method in ("GET", "POST", "DELETE"):
        flow = make_flow(method=method, url="https://api.example.com/any")
        await addon.request(flow)
        assert response_json(flow) == {"ok": True}


async def test_mock_path_is_exact_not_substring(addon):
    await load(addon, {"mock": {"/api/users": {"id": 1}}})
    for miss in ("/api/users/1", "/v2/api/users", "/api/user"):
        flow = make_flow(url=f"https://api.example.com{miss}")
        await addon.request(flow)
        assert flow.response is None, miss


async def test_mock_ignores_query_string(addon):
    await load(addon, {"mock": {"/api/users": {"id": 1}}})
    flow = make_flow(url="https://api.example.com/api/users?page=2&q=x")
    await addon.request(flow)
    assert response_json(flow) == {"id": 1}


async def test_mock_rejects_key_without_leading_slash(addon, caplog):
    caplog.set_level(logging.WARNING)
    await load(addon, {"mock": {"api/users": {"id": 1}}})
    assert addon.rules == ()
    assert "must start with '/'" in caplog.text


async def test_rules_win_over_mock(addon):
    """Precedence: mock: rules compile at priority -1, so an explicit rule
    at the default priority 0 always matches first."""
    await load(
        addon,
        {
            "mock": {"/api/users": {"from": "mock"}},
            "rules": [
                {
                    "name": "explicit",
                    "path_contains": "/api/users",
                    "respond_with": {"status": 200, "body": {"from": "rules"}},
                }
            ],
        },
    )
    assert [r.name for r in addon.rules] == ["explicit", "mock /api/users"]
    flow = make_flow(url="https://api.example.com/api/users")
    await addon.request(flow)
    assert response_json(flow) == {"from": "rules"}


async def test_mock_and_rules_coexist(addon):
    await load(
        addon,
        {
            "mock": {"/m": {"from": "mock"}},
            "rules": [
                {
                    "name": "explicit",
                    "path_contains": "/r",
                    "respond_with": {"status": 200, "body": {"from": "rules"}},
                }
            ],
        },
    )
    m = make_flow(url="https://api.example.com/m")
    await addon.request(m)
    assert response_json(m) == {"from": "mock"}
    r = make_flow(url="https://api.example.com/r")
    await addon.request(r)
    assert response_json(r) == {"from": "rules"}


async def test_mock_keeps_downstream_features(addon):
    """Desugared rules are ordinary rules: passthrough still fetches upstream first."""
    await load(
        addon,
        {
            "defaults": {"passthrough": True},
            "mock": {"/api/users": {"id": 1}},
        },
    )
    flow = make_flow(url="https://api.example.com/api/users")
    await addon.request(flow)
    assert flow.response is None
    flow.response = Response.make(200, b'{"id": 999}', {"content-type": "application/json"})
    await addon.response(flow)
    assert response_json(flow) == {"id": 1}


async def test_defaults_merged_into_rules(addon):
    await load(
        addon,
        {
            "defaults": {"host": r"api\.example\.com"},
            "rules": [
                {
                    "name": "a",
                    "path_contains": "/x",
                    "respond_with": {"status": 200, "body": {"ok": True}},
                }
            ],
        },
    )
    hit = make_flow(url="https://api.example.com/x")
    await addon.request(hit)
    assert response_json(hit) == {"ok": True}
    miss = make_flow(url="https://other.example.org/x")
    await addon.request(miss)
    assert miss.response is None


async def test_defaults_merged_into_mock_rules(addon):
    await load(
        addon,
        {
            "defaults": {"host": r"api\.example\.com"},
            "mock": {"/api/users": {"id": 1}},
        },
    )
    hit = make_flow(url="https://api.example.com/api/users")
    await addon.request(hit)
    assert response_json(hit) == {"id": 1}
    miss = make_flow(url="https://other.example.org/api/users")
    await addon.request(miss)
    assert miss.response is None


async def test_defaults_rule_key_wins(addon):
    await load(
        addon,
        {
            "defaults": {"host": r"api\.example\.com", "priority": 5},
            "rules": [
                {
                    "name": "a",
                    "host": r"other\.example\.org",
                    "path_contains": "/x",
                    "respond_with": {"status": 200, "body": {"ok": True}},
                }
            ],
        },
    )
    assert addon.rules[0].priority == 5
    flow = make_flow(url="https://other.example.org/x")
    await addon.request(flow)
    assert response_json(flow) == {"ok": True}
    miss = make_flow(url="https://api.example.com/x")
    await addon.request(miss)
    assert miss.response is None


async def test_delay_clamp_warns(addon, caplog):
    """A delay above the 10s cap names the rule and both values."""
    caplog.set_level(logging.WARNING)
    await load(
        addon,
        {
            "rules": [
                {
                    "name": "slow",
                    "path_contains": "/x",
                    "delay": {"fixed": 50},
                    "respond_with": {"status": 200, "body": {}},
                }
            ]
        },
    )
    assert addon.rules[0].delay_fixed == 10.0
    assert "Rule 'slow' delay.fixed" in caplog.text
    assert "50.0s" in caplog.text and "10.0s" in caplog.text


async def test_delay_within_cap_does_not_warn(addon, caplog):
    caplog.set_level(logging.WARNING)
    await load(
        addon,
        {
            "rules": [
                {
                    "name": "ok",
                    "path_contains": "/x",
                    "delay": {"fixed": 5},
                    "respond_with": {"status": 200, "body": {}},
                }
            ]
        },
    )
    assert addon.rules[0].delay_fixed == 5.0
    assert "using" not in caplog.text


async def test_global_delay_clamp_warns(addon, caplog):
    caplog.set_level(logging.WARNING)
    await load(addon, {"global_delay": 99, "rules": []})
    assert addon.global_delay_fixed == 10.0
    assert "global_delay" in caplog.text and "99.0s" in caplog.text


# ========================================================
# Category: --mode/--runner collision with mitmproxy's own --mode
# ========================================================


def test_cli_runner_flag_selects_binary(monkeypatch):
    """--runner is the primary spelling for choosing web/proxy/dump."""
    from stunt import cli

    captured = {}

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("stunt.cli.subprocess.run", fake_run)
    monkeypatch.setattr("stunt.cli.sys.argv", ["stunt", "--runner", "dump"])

    with pytest.raises(SystemExit):
        cli.main()

    assert Path(captured["cmd"][0]).name.startswith("mitmdump")


def test_cli_mode_deprecated_alias_still_selects_runner(monkeypatch, capsys):
    """Documented usage `stunt --mode dump` must keep working, with a deprecation warning."""
    from stunt import cli

    captured = {}

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("stunt.cli.subprocess.run", fake_run)
    monkeypatch.setattr("stunt.cli.sys.argv", ["stunt", "--mode", "dump"])

    with pytest.raises(SystemExit):
        cli.main()

    assert Path(captured["cmd"][0]).name.startswith("mitmdump")
    assert "deprecated" in capsys.readouterr().err.lower()


def test_cli_mode_mitmproxy_style_value_forwarded(monkeypatch, capsys):
    """A mitmproxy-style --mode value (reverse:/transparent/socks5/upstream:) must reach the
    subprocess command line untouched, and must NOT trigger the deprecation warning since it
    isn't our runner selector."""
    from stunt import cli

    captured = {}

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("stunt.cli.subprocess.run", fake_run)
    monkeypatch.setattr(
        "stunt.cli.sys.argv",
        ["stunt", "--mode", "reverse:https://api.example.com"],
    )

    with pytest.raises(SystemExit):
        cli.main()

    cmd = captured["cmd"]
    assert Path(cmd[0]).name.startswith("mitmweb")  # runner still defaults to web
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "reverse:https://api.example.com"
    assert "deprecated" not in capsys.readouterr().err.lower()


def test_cli_runner_and_mitmproxy_mode_combine(monkeypatch):
    """--runner dump plus a forwarded mitmproxy --mode both land in the same command."""
    from stunt import cli

    captured = {}

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("stunt.cli.subprocess.run", fake_run)
    monkeypatch.setattr(
        "stunt.cli.sys.argv",
        ["stunt", "--runner", "dump", "--mode", "socks5"],
    )

    with pytest.raises(SystemExit):
        cli.main()

    cmd = captured["cmd"]
    assert Path(cmd[0]).name.startswith("mitmdump")
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "socks5"


# ========================================================
# Category: Network conditions — kill and throttle
# ========================================================


async def test_kill_drops_the_connection(addon):
    addon.rules = addon._compile_rules({"rules": [{"name": "offline", "path_contains": "/api", "kill": True}]})
    flow = make_flow(url="https://api.example.com/api/users")
    await addon.request(flow)

    assert flow.error is not None
    assert flow.response is None
    assert flow.metadata["stunt_handled"] is True


async def test_kill_does_not_fire_when_the_rule_does_not_match(addon):
    addon.rules = addon._compile_rules({"rules": [{"name": "offline", "path_contains": "/nope", "kill": True}]})
    flow = make_flow(url="https://api.example.com/api/users")
    await addon.request(flow)

    assert flow.error is None
    assert flow.response is None
    assert "stunt_handled" not in flow.metadata


async def test_kill_is_request_phase_only(addon):
    """A kill rule has no response-phase action, so response() must leave the flow alone."""
    addon.rules = addon._compile_rules({"rules": [{"name": "offline", "path_contains": "/api", "kill": True}]})
    flow = make_flow(url="https://api.example.com/api/users", response_content=b'{"ok": true}')
    await addon.response(flow)

    assert flow.error is None
    assert response_json(flow) == {"ok": True}


async def test_kill_after_delay_is_a_timeout(addon, monkeypatch):
    """Hang-then-drop falls straight out of the existing delay machinery."""
    addon.rules = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "timeout",
                    "path_contains": "/api",
                    "delay": {"fixed": 3},
                    "kill": True,
                }
            ]
        }
    )
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    flow = make_flow(url="https://api.example.com/api/users")
    await addon.request(flow)

    fake_sleep.assert_called_once_with(3.0)
    assert flow.error is not None


async def test_kill_respects_once(addon):
    """Regression guard: a kill-only rule must consume exactly one hit."""
    addon.rules = addon._compile_rules(
        {"rules": [{"name": "flap", "path_contains": "/api", "kill": True, "once": True}]}
    )
    first = make_flow(url="https://api.example.com/api/users")
    await addon.request(first)
    assert first.error is not None

    second = make_flow(url="https://api.example.com/api/users")
    await addon.request(second)
    assert second.error is None
    assert addon._rule_hits["flap"] == 1


# --- throttle -------------------------------------------------------------


def _sleep_calls(fake_sleep):
    return [c.args[0] for c in fake_sleep.await_args_list]


async def _round_trip(addon, url, body):
    """Drive a flow through both real hooks the way mitmproxy does: the request
    goes out with no response yet, the response arrives afterwards."""
    flow = make_flow(url=url)
    await addon.request(flow)
    if flow.response is None:
        flow.response = Response.make(200, body, {"content-type": "application/json"})
    await addon.response(flow)
    return flow


async def test_throttle_preset_parses_to_bytes_per_second(addon):
    rule = addon._compile_rules({"rules": [{"name": "slow", "path_contains": "/api", "throttle": "3g"}]})[0]
    # 1600 kbit/s = 200_000 bytes/s
    assert rule.throttle_bps == 200000.0


async def test_throttle_sleeps_proportionally_to_body_size(addon, monkeypatch):
    addon.rules = addon._compile_rules({"rules": [{"name": "slow", "path_contains": "/api", "throttle": {"kbps": 8}}]})
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # 8 kbit/s == 1000 bytes/s, so 2500 bytes == 2.5s.
    await _round_trip(addon, "https://api.example.com/api/x", b"x" * 2500)

    assert _sleep_calls(fake_sleep) == [2.5]


async def test_throttle_scales_with_the_body(addon, monkeypatch):
    addon.rules = addon._compile_rules({"rules": [{"name": "slow", "path_contains": "/api", "throttle": {"kbps": 8}}]})
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    for size in (1000, 5000):
        await _round_trip(addon, "https://api.example.com/api/x", b"x" * size)

    assert _sleep_calls(fake_sleep) == [1.0, 5.0]


async def test_global_throttle_applies_without_a_rule(addon, monkeypatch):
    addon.rules_file.write_text(yaml.dump({"throttle": {"kbps": 8}, "rules": []}))
    await addon._reload()
    assert addon.global_throttle_bps == 1000.0

    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await addon.response(make_flow(response_content=b"x" * 3000))
    assert _sleep_calls(fake_sleep) == [3.0]


async def test_throttle_composes_with_delay_and_does_not_double_apply(addon, monkeypatch):
    """Regression guard: latency once, transfer time once, across both phases."""
    addon.rules = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "slow-mock",
                    "path_contains": "/api",
                    "delay": {"fixed": 0.5},
                    "throttle": {"kbps": 8},
                    "respond_with": {"status": 200, "body": {"ok": True}},
                }
            ]
        }
    )
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    flow = make_flow(url="https://api.example.com/api/x")
    await addon.request(flow)
    body_len = len(response_content(flow))
    # latency, then transfer time — both exactly once.
    assert _sleep_calls(fake_sleep) == [0.5, body_len / 1000.0]

    # The response hook must not charge for the same body a second time.
    await addon.response(flow)
    assert _sleep_calls(fake_sleep) == [0.5, body_len / 1000.0]


async def test_throttle_cap_clamps_and_warns(addon, monkeypatch, caplog):
    from stunt.addon import MAX_THROTTLE_SECONDS

    addon.rules = addon._compile_rules(
        {"rules": [{"name": "glacial", "path_contains": "/api", "throttle": {"kbps": 8}}]}
    )
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="stunt"):
        await _round_trip(addon, "https://api.example.com/api/x", b"x" * 999_000)

    assert _sleep_calls(fake_sleep) == [MAX_THROTTLE_SECONDS]
    assert any("capped at" in r.getMessage() for r in caplog.records)


async def test_throttle_respects_once(addon, monkeypatch):
    """Regression guard: a throttle-only rule must consume exactly one hit."""
    addon.rules = addon._compile_rules(
        {
            "rules": [
                {
                    "name": "one-slow-request",
                    "path_contains": "/api",
                    "throttle": {"kbps": 8},
                    "once": True,
                }
            ]
        }
    )
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await _round_trip(addon, "https://api.example.com/api/x", b"x" * 2000)
    assert _sleep_calls(fake_sleep) == [2.0]
    assert addon._rule_hits["one-slow-request"] == 1

    await _round_trip(addon, "https://api.example.com/api/x", b"x" * 2000)
    assert _sleep_calls(fake_sleep) == [2.0]


async def test_unknown_throttle_preset_is_ignored_with_a_warning(addon, caplog):
    with caplog.at_level(logging.WARNING, logger="stunt"):
        rules = addon._compile_rules(
            {
                "rules": [
                    {
                        "name": "typo",
                        "path_contains": "/api",
                        "throttle": "4gee",
                        "respond_with": {"body": {}},
                    }
                ]
            }
        )
    assert rules[0].throttle_bps is None
    assert any("unknown throttle preset" in r.getMessage() for r in caplog.records)


async def test_no_throttle_means_no_extra_sleep(addon, monkeypatch):
    addon.rules = addon._compile_rules(
        {"rules": [{"name": "plain", "path_contains": "/api", "respond_with": {"status": 200, "body": {}}}]}
    )
    fake_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await addon.request(make_flow(url="https://api.example.com/api/x"))
    fake_sleep.assert_not_called()


# ========================================================
# Category: merge and containment matching
# ========================================================


def _rule(addon, rule):
    addon.rules = tuple(addon._compile_rules({"rules": [rule]})[0:1])


async def test_merge_deep_merges_nested_objects(addon):
    _rule(
        addon,
        {
            "name": "deep-merge",
            "modify_response_json": {"merge": {"$.user": {"prefs": {"theme": "dark"}, "role": "admin"}}},
        },
    )
    flow = make_flow(
        response_content=json.dumps(
            {"user": {"id": 1, "role": "guest", "prefs": {"theme": "light", "lang": "en"}}}
        ).encode()
    )
    await addon.response(flow)
    assert response_json(flow) == {"user": {"id": 1, "role": "admin", "prefs": {"theme": "dark", "lang": "en"}}}


async def test_merge_where_hits_every_matching_element(addon):
    _rule(
        addon,
        {
            "name": "merge-where",
            "modify_response_json": {
                "merge": {"$.items": {"$where": {"sku": "A"}, "$merge": {"price": 0, "tags": ["sale"]}}}
            },
        },
    )
    flow = make_flow(
        response_content=json.dumps(
            {
                "items": [
                    {"sku": "A", "price": 10},
                    {"sku": "B", "price": 20},
                    {"sku": "A", "price": 30, "tags": ["old"]},
                ]
            }
        ).encode()
    )
    await addon.response(flow)
    items = response_json(flow)["items"]
    assert items[0] == {"sku": "A", "price": 0, "tags": ["sale"]}
    assert items[1] == {"sku": "B", "price": 20}  # untouched
    assert items[2] == {"sku": "A", "price": 0, "tags": ["sale"]}


async def test_merge_where_replaces_whole_element(addon):
    _rule(
        addon,
        {
            "name": "merge-replace",
            "modify_response_json": {"merge": {"$.items": {"$where": {"sku": "B"}, "$replace": {"gone": True}}}},
        },
    )
    flow = make_flow(
        response_content=json.dumps(
            {
                "items": [
                    {"sku": "A", "price": 10},
                    {"sku": "B", "price": 20, "extra": "dropped"},
                ]
            }
        ).encode()
    )
    await addon.response(flow)
    assert response_json(flow)["items"] == [{"sku": "A", "price": 10}, {"gone": True}]


async def test_merge_where_regex_predicate_on_request(addon):
    _rule(
        addon,
        {
            "name": "merge-req",
            "modify_request_json": {
                "merge": {"$.orders": {"$where": {"id": {"$regex": "^ord-"}}, "$merge": {"flagged": True}}}
            },
        },
    )
    flow = make_flow(method="POST", content=json.dumps({"orders": [{"id": "ord-1"}, {"id": "x-2"}]}).encode())
    await addon.request(flow)
    assert request_json(flow)["orders"] == [{"id": "ord-1", "flagged": True}, {"id": "x-2"}]


async def test_merge_on_unparseable_body_does_not_crash(addon):
    _rule(
        addon,
        {
            "name": "merge-garbage",
            "modify_response_json": {"merge": {"$.user": {"role": "admin"}}},
        },
    )
    flow = make_flow(response_content=b"<html>not json</html>")
    await addon.response(flow)
    assert response_content(flow) == b"<html>not json</html>"


async def test_merge_malformed_spec_does_not_crash(addon):
    _rule(
        addon,
        {
            "name": "merge-bad-spec",
            # merge must be a JSONPath->spec mapping; a list is nonsense and must be ignored
            "modify_response_json": {"merge": ["$.user"]},
        },
    )
    flow = make_flow(response_content=json.dumps({"user": {"id": 1}}).encode())
    await addon.response(flow)
    assert response_json(flow) == {"user": {"id": 1}}


async def test_json_contains_matches_nested_shape(addon):
    _rule(
        addon,
        {
            "name": "contains-hit",
            "json_contains": {"user": {"role": "admin"}, "items": [{"sku": "B"}]},
            "error": {"status": 403},
        },
    )
    flow = make_flow(
        method="POST",
        content=json.dumps(
            {
                "user": {"id": 7, "role": "admin"},
                "items": [{"sku": "A"}, {"sku": "B", "qty": 2}],
                "extra": "ignored",
            }
        ).encode(),
    )
    await addon.request(flow)
    assert flow.response.status_code == 403


async def test_json_contains_negative_cases(addon):
    _rule(
        addon,
        {
            "name": "contains-miss",
            "json_contains": {"user": {"role": "admin"}, "items": [{"sku": "Z"}]},
            "error": {"status": 403},
        },
    )
    for body in (
        {"user": {"role": "guest"}, "items": [{"sku": "Z"}]},  # wrong leaf value
        {"user": {"role": "admin"}, "items": [{"sku": "A"}]},  # array element absent
        {"user": {"role": "admin"}},  # key missing entirely
        {"user": "admin", "items": [{"sku": "Z"}]},  # type mismatch
    ):
        flow = make_flow(method="POST", content=json.dumps(body).encode())
        await addon.request(flow)
        assert flow.response is None, body


async def test_json_contains_regex_leaf(addon):
    _rule(
        addon,
        {
            "name": "contains-regex",
            "json_contains": {"email": {"$regex": "@example\\.com$"}},
            "error": {"status": 418},
        },
    )
    hit = make_flow(method="POST", content=b'{"email": "a@example.com"}')
    await addon.request(hit)
    assert hit.response.status_code == 418
    miss = make_flow(method="POST", content=b'{"email": "a@other.org"}')
    await addon.request(miss)
    assert miss.response is None


async def test_response_json_contains_matches_and_is_response_phase(addon):
    _rule(
        addon,
        {
            "name": "resp-contains",
            "response_json_contains": {"errors": [{"code": "E1"}]},
            "modify_response_headers": {"set": {"X-Flagged": "1"}},
        },
    )
    flow = make_flow(
        response_content=json.dumps({"errors": [{"code": "E0"}, {"code": "E1", "detail": "boom"}]}).encode()
    )
    await addon.request(flow)
    assert "X-Flagged" not in flow.response.headers  # request phase must not fire
    await addon.response(flow)
    assert flow.response.headers["X-Flagged"] == "1"


async def test_json_contains_unparseable_body_does_not_crash(addon):
    _rule(
        addon,
        {
            "name": "contains-garbage",
            "json_contains": {"a": 1},
            "error": {"status": 500},
        },
    )
    flow = make_flow(method="POST", content=b"\xff\xfe not json at all")
    await addon.request(flow)
    assert flow.response is None


async def test_json_contains_respects_once(addon):
    """Guard: hits are spent only when the rule actually acts."""
    _rule(
        addon,
        {
            "name": "contains-once",
            "json_contains": {"ping": True},
            "once": True,
            "error": {"status": 429},
        },
    )
    # a non-matching request must not burn the single allowed hit
    miss = make_flow(method="POST", content=b'{"ping": false}')
    await addon.request(miss)
    assert miss.response is None

    first = make_flow(method="POST", content=b'{"ping": true}')
    await addon.request(first)
    assert first.response.status_code == 429

    second = make_flow(method="POST", content=b'{"ping": true}')
    await addon.request(second)
    assert second.response is None


async def test_response_json_contains_respects_count(addon):
    _rule(
        addon,
        {
            "name": "contains-count",
            "response_json_contains": {"status": "pending"},
            "count": 2,
            "modify_response_json": {"merge": {"$": {"status": "done"}}},
        },
    )
    for expected in ("done", "done", "pending"):
        flow = make_flow(response_content=b'{"status": "pending"}')
        await addon.response(flow)
        assert response_json(flow)["status"] == expected


# ---------------------------------------------------------------------------
# Record real traffic into a rules file
# ---------------------------------------------------------------------------

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "stunt_schema.json"


async def _record(addon, flows, **kw):
    """Arm recording, push flows through the REAL hooks, stop. Returns the
    parsed rules file (or None if nothing was written)."""
    out = kw.pop("out", None) or addon.rules_file.parent / "recorded.yaml"
    addon.start_recording(out, **kw)
    for flow in flows:
        await addon.request(flow)
        await addon.response(flow)
    addon._write_recording()
    return yaml.safe_load(out.read_text()) if out.exists() else None


def _recorded_flow(
    method="GET",
    url="https://api.example.com/api/users",
    status=200,
    body=b'{"ok": true}',
    ct="application/json",
    req_headers=None,
):
    f = tflow.tflow()
    f.request = Request.make(method, url, b"", headers=req_headers or {"Authorization": "Bearer secret"})
    f.response = Response.make(
        status,
        body,
        {
            "content-type": ct,
            "set-cookie": "session=deadbeef",
            "connection": "keep-alive",
        },
    )
    return f


async def test_record_writes_a_valid_rules_file(addon, tmp_path):
    data = await _record(addon, [_recorded_flow(body=b'{"id": 1, "name": "Ada"}')])
    assert data["mock"] == {"GET /api/users": {"id": 1, "name": "Ada"}}

    out = addon.rules_file.parent / "recorded.yaml"
    from stunt.addon import lint_rules

    assert lint_rules(out, addon.mocks_dir) == []

    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(data, json.loads(SCHEMA_PATH.read_text()))


async def test_record_inlines_small_json_and_files_out_large_and_binary(addon):
    big = json.dumps({"rows": ["x" * 40] * 100}).encode()
    assert len(big) > 2048
    flows = [
        _recorded_flow(url="https://api.example.com/small", body=b'{"a": 1}'),
        _recorded_flow(url="https://api.example.com/big", body=big),
        _recorded_flow(url="https://api.example.com/logo", body=b"\x89PNG\r\n\x1a\n\x00", ct="image/png"),
    ]
    data = await _record(addon, flows)

    assert data["mock"]["GET /small"] == {"a": 1}
    assert data["mock"]["GET /big"] == {"file": "rec_get_big.json"}
    assert (addon.mocks_dir / "rec_get_big.json").read_bytes() == big

    # image/png needs a Content-Type the `mock:` shorthand's default would clobber,
    # so it falls back to a full rules entry.
    png = [r for r in data["rules"] if "logo" in r["name"]][0]
    assert png["respond_with"]["file"] == "rec_get_logo.png"
    assert png["respond_with"]["headers"] == {"Content-Type": "image/png"}
    assert (addon.mocks_dir / "rec_get_logo.png").read_bytes().startswith(b"\x89PNG")


async def test_record_non_200_becomes_a_full_rule(addon):
    data = await _record(
        addon, [_recorded_flow(url="https://api.example.com/gone", status=404, body=b'{"error": "nope"}')]
    )
    assert "mock" not in data
    rule = data["rules"][0]
    assert rule["method"] == "GET"
    assert rule["path_regex"] == r"^/gone(\?.*)?$"
    assert rule["respond_with"] == {"status": 404, "body": {"error": "nope"}}


async def test_record_filters_by_host_and_path(addon):
    flows = [
        _recorded_flow(url="https://api.example.com/api/keep"),
        _recorded_flow(url="https://api.example.com/other/drop"),
        _recorded_flow(url="https://cdn.example.com/api/drop"),
    ]
    data = await _record(addon, flows, host=r"^api\.example\.com$", path="^/api/")
    assert list(data["mock"]) == ["GET /api/keep"]


async def test_record_omits_sensitive_and_hop_by_hop_headers(addon):
    text = await _record(addon, [_recorded_flow(url="https://api.example.com/logo", body=b"<p>hi</p>", ct="text/html")])
    blob = yaml.safe_dump(text)
    for leak in ("Authorization", "Bearer", "set-cookie", "Set-Cookie", "deadbeef", "keep-alive"):
        assert leak not in blob
    assert text["rules"][0]["respond_with"]["headers"] == {"Content-Type": "text/html"}


async def test_record_refuses_to_clobber_without_force(addon, tmp_path):
    out = tmp_path / "recorded.yaml"
    out.write_text("mock: {}\n")

    assert addon.start_recording(out) is False
    assert addon._recorder is None
    assert out.read_text() == "mock: {}\n"

    assert addon.start_recording(out, force=True) is True
    await _record(addon, [_recorded_flow()], out=out, force=True)
    assert out.read_text() != "mock: {}\n"


async def test_record_off_writes_nothing_and_does_no_work(addon, tmp_path):
    flow = _recorded_flow()
    await addon.request(flow)
    await addon.response(flow)
    addon._write_recording()
    assert addon._recorder is None
    assert list(tmp_path.glob("*.yaml")) == []
    assert list(addon.mocks_dir.iterdir()) == []


async def test_record_does_not_capture_stunt_own_mocks(addon):
    _rule(
        addon,
        {
            "name": "served-by-stunt",
            "path_regex": "^/api/users$",
            "respond_with": {"body": {"mocked": True}},
        },
    )
    data = await _record(addon, [_recorded_flow(body=b'{"real": true}')])
    assert data is None  # nothing real observed -> no file written


async def test_record_multiple_hosts_get_explicit_host_matchers(addon):
    data = await _record(
        addon,
        [
            _recorded_flow(url="https://a.example.com/v1"),
            _recorded_flow(url="https://b.example.com/v1"),
        ],
    )
    assert "mock" not in data
    assert {r["host"] for r in data["rules"]} == {r"^a\.example\.com$", r"^b\.example\.com$"}


async def test_state_gate_admits_one_flow_under_concurrency(addon):
    """A one-shot state gate must admit exactly one of N concurrent flows.

    Regression: the require-check and the state write used to be separate lock
    acquisitions with an awaitable delay between them, so 50 concurrent flows all
    observed the gate open before any of them shut it.
    """
    addon._state = {"gate": False}
    addon.rules = (
        CompiledRule(
            name="gate",
            state_require=(("gate", False),),
            state_set={"gate": True},
            delay_fixed=0.01,
            respond_with={"status": 200, "body": {"unlocked": True}},
        ),
    )
    flows = [make_flow(response_content=b'{"real": true}') for _ in range(50)]

    async def run(f):
        await addon.request(f)
        await addon.response(f)

    await asyncio.gather(*(run(f) for f in flows))

    admitted = sum(1 for f in flows if f.metadata.get("stunt_handled"))
    assert admitted == 1, f"{admitted} flows passed a one-shot gate"
    assert addon._state["gate"] is True


# ========================================================
# Category: recursion depth guard in _json_contains / _deep_merge
# ========================================================


def _nested(depth, leaf):
    """Build a dict nested `depth` levels deep under repeated key "a", bottoming out at `leaf`."""
    d = leaf
    for _ in range(depth):
        d = {"a": d}
    return d


async def test_json_contains_beyond_depth_limit_fails_match_and_warns(addon, caplog):
    caplog.set_level(logging.WARNING)
    depth = 150  # comfortably past MAX_JSON_DEPTH (100)
    _rule(
        addon,
        {
            "name": "too-deep-contains",
            "json_contains": _nested(depth, True),
            "error": {"status": 403},
        },
    )
    flow = make_flow(method="POST", content=json.dumps(_nested(depth, True)).encode())
    await addon.request(flow)
    # guard trips -> containment treated as no match -> rule never fires
    assert flow.response is None
    assert "MAX_JSON_DEPTH" in caplog.text
    assert "_json_contains" in caplog.text


async def test_deep_merge_beyond_depth_limit_leaves_target_and_warns(addon, caplog):
    caplog.set_level(logging.WARNING)
    depth = 150
    _rule(
        addon,
        {
            "name": "too-deep-merge",
            "modify_response_json": {"merge": {"$": _nested(depth, {"changed": True})}},
        },
    )
    original = _nested(depth, {"changed": False})
    flow = make_flow(response_content=json.dumps(original).encode())
    await addon.response(flow)
    # must not raise, and the response body must still be valid JSON
    result = response_json(flow)
    assert isinstance(result, dict)
    assert "MAX_JSON_DEPTH" in caplog.text
    assert "_deep_merge" in caplog.text


async def test_json_contains_just_under_depth_limit_still_matches(addon):
    depth = 90  # under MAX_JSON_DEPTH (100) — guards against setting the limit too low
    _rule(
        addon,
        {
            "name": "legit-deep-contains",
            "json_contains": _nested(depth, True),
            "error": {"status": 403},
        },
    )
    flow = make_flow(method="POST", content=json.dumps(_nested(depth, True)).encode())


# --------------------------------------------------------------------------- #
# mitmproxy command surface (mitmweb command palette / console).
# --------------------------------------------------------------------------- #

PM64_RULES = {
    "state": {"step": 1},
    "rules": [
        {"name": "low", "path_contains": "/low", "error": {"status": 500}},
        {"name": "high", "priority": 10, "path_contains": "/high", "once": True, "error": {"status": 418}},
        {"name": "gated", "path_contains": "/gated", "state": {"require": {"step": 99}}, "error": {"status": 403}},
    ],
}


async def _load_pm64(addon):
    addon.rules_file.write_text(yaml.dump(PM64_RULES))
    await addon._reload()


async def test_commands_registered_with_master():
    """The commands must be reachable through mitmproxy's command manager —
    that is what mitmweb's /commands endpoint and palette read."""
    from mitmproxy import master, options

    m = master.Master(options.Options())
    a = Stunt()
    m.addons.add(a)
    try:
        names = {c for c in m.commands.commands if c.startswith("stunt.")}
        assert names == {
            "stunt.rules.list",
            "stunt.rules.toggle",
            "stunt.state.get",
            "stunt.state.set",
            "stunt.record.start",
            "stunt.record.stop",
            "stunt.reload",
        }
        # Return types must be marshalable, and every command needs help text
        # because that is all the palette shows.
        for name in names:
            cmd = m.commands.commands[name]
            assert cmd.help, f"{name} has no docstring"
            assert cmd.signature_help()
        # Invoking through the manager exercises the real return-type check.
        assert m.commands.call("stunt.rules.list")
    finally:
        await a.aclose()


async def test_rules_list_reports_priority_hits_and_status(addon):
    await _load_pm64(addon)
    flow = make_flow(url="https://api.example.com/high")
    await addon.request(flow)

    out = addon.cmd_rules_list()
    assert out[0].startswith("Stunt: 3 rule(s)")
    body = out[1:]
    assert [line.split()[1] for line in body] == ["high", "low", "gated"]
    assert "hits=1" in body[0] and "exhausted (once)" in body[0]
    assert "hits=0" in body[1] and "active" in body[1]
    assert "gated (state.require)" in body[2]


async def test_rules_list_when_nothing_loaded(addon):
    out = addon.cmd_rules_list()
    assert len(out) == 1
    assert "0 rules loaded" in out[0]


async def test_toggle_prevents_rule_from_firing(addon):
    await _load_pm64(addon)
    msg = addon.cmd_rules_toggle("low")
    assert "DISABLED for this session only" in msg
    assert "rules.yaml unchanged" in msg

    flow = make_flow(url="https://api.example.com/low")
    await addon.request(flow)
    assert flow.response is None

    assert "DISABLED (session)" in "\n".join(addon.cmd_rules_list())
    # ...and it is still listed, in its priority slot.
    assert len(addon.cmd_rules_list()) == 4

    assert "ENABLED" in addon.cmd_rules_toggle("low")
    flow = make_flow(url="https://api.example.com/low")
    await addon.request(flow)
    assert flow.response.status_code == 500
    # Re-enabling must restore priority order, not append.
    assert [line.split()[1] for line in addon.cmd_rules_list()[1:]] == ["high", "low", "gated"]


async def test_toggle_unknown_rule(addon):
    await _load_pm64(addon)
    assert "no rule named 'nope'" in addon.cmd_rules_toggle("nope")


async def test_toggle_is_lost_on_reload(addon):
    await _load_pm64(addon)
    addon.cmd_rules_toggle("low")
    assert len(addon.rules) == 2

    rules = json.loads(json.dumps(PM64_RULES))
    rules["rules"][0]["error"]["status"] = 502
    addon.rules_file.write_text(yaml.dump(rules))
    await addon._reload()

    assert len(addon.rules) == 3
    assert "DISABLED" not in "\n".join(addon.cmd_rules_list())
    flow = make_flow(url="https://api.example.com/low")
    await addon.request(flow)
    assert flow.response.status_code == 502


async def test_state_get_and_set_round_trip(addon):
    await _load_pm64(addon)
    assert addon.cmd_state_get() == ["Stunt state (1 key(s)):", "  step = 1"]

    assert addon.cmd_state_set("step", "99") == "Stunt state: step = 99"
    assert addon._state["step"] == 99
    addon.cmd_state_set("who", "alice")
    addon.cmd_state_set("on", "true")
    assert addon._state["who"] == "alice"
    assert addon._state["on"] is True
    assert addon.cmd_state_get()[1:] == ["  on = true", "  step = 99", '  who = "alice"']


async def test_state_get_when_empty(addon):
    assert addon.cmd_state_get() == ["Stunt state: (empty)"]


async def test_state_set_opens_a_gated_rule(addon):
    """The point of the command: jump a stateful flow to a step without a restart."""
    await _load_pm64(addon)
    flow = make_flow(url="https://api.example.com/gated")
    await addon.request(flow)
    assert flow.response is None

    addon.cmd_state_set("step", "99")
    flow = make_flow(url="https://api.example.com/gated")
    await addon.request(flow)
    assert flow.response.status_code == 403


async def test_deep_merge_just_under_depth_limit_still_merges(addon):
    depth = 90
    _rule(
        addon,
        {
            "name": "legit-deep-merge",
            "modify_response_json": {"merge": {"$": _nested(depth, {"changed": True})}},
        },
    )
    original = _nested(depth, {"changed": False})
    flow = make_flow(response_content=json.dumps(original).encode())
    await addon.response(flow)
    result = response_json(flow)
    # walk down to the leaf and confirm the patch actually applied
    cur = result
    for _ in range(depth):
        cur = cur["a"]
    assert cur == {"changed": True}


async def test_proxy_keeps_serving_other_rules_after_depth_limit_hit(addon, caplog):
    caplog.set_level(logging.WARNING)
    depth = 150
    _rules(
        addon,
        {
            "name": "too-deep-contains",
            "priority": 10,
            "json_contains": _nested(depth, True),
            "error": {"status": 403},
        },
        {
            "name": "normal-rule",
            "priority": 1,
            "json_contains": {"ping": True},
            "error": {"status": 429},
        },
    )
    # Rule 1 trips the depth guard (no match) and the flow falls through to rule 2.
    deep_flow = make_flow(method="POST", content=json.dumps(_nested(depth, True)).encode())
    await addon.request(deep_flow)
    assert deep_flow.response is None
    assert "MAX_JSON_DEPTH" in caplog.text

    # A normal request afterwards must still be served: the guard must leave no
    # corrupted state behind.
    normal_flow = make_flow(method="POST", content=b'{"ping": true}')
    await addon.request(normal_flow)
    assert normal_flow.response.status_code == 429


# ---------------------------------------------------------------------------
# Scrub secrets from recorded bodies
# ---------------------------------------------------------------------------

JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkFkYSJ9"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


async def test_record_redacts_value_under_sensitive_key(addon):
    body = json.dumps({"access_token": "RESP_SECRET_TOKEN_XYZ", "expires_in": 3600}).encode()
    data = await _record(addon, [_recorded_flow(body=body)])
    assert data["mock"]["GET /api/users"] == {"access_token": "<redacted>", "expires_in": 3600}


async def test_record_redacts_jwt_under_an_innocent_key(addon):
    body = json.dumps({"note": f"use {JWT} to log in"}).encode()
    data = await _record(addon, [_recorded_flow(body=body)])
    assert data["mock"]["GET /api/users"] == {"note": "use <redacted> to log in"}


async def test_record_redacts_nested_and_array_embedded_secrets(addon):
    body = json.dumps(
        {
            "user": {"id": 7, "credentials": {"apiKey": "abc123", "refresh_token": "r-1"}},
            "sessions": [{"sessionId": "s-1"}, {"sessionId": "s-2"}],
            "tokens": ["t-1", "t-2"],
        }
    ).encode()
    data = await _record(addon, [_recorded_flow(body=body)])
    assert data["mock"]["GET /api/users"] == {
        "user": {"id": 7, "credentials": {"apiKey": "<redacted>", "refresh_token": "<redacted>"}},
        "sessions": [{"sessionId": "<redacted>"}, {"sessionId": "<redacted>"}],
        "tokens": ["<redacted>", "<redacted>"],
    }


async def test_record_does_not_redact_legitimate_values(addon, capsys):
    """False-positive guard: an over-eager scrubber makes recordings useless."""
    payload = {
        "id": 12345,
        "uuid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "name": "Ada Lovelace",
        "slug": "ada-lovelace-the-first-programmer-of-them-all",
        "price": 19.99,
        "created_at": "2026-08-24T10:00:00Z",
        "avatar": "https://cdn.example.com/avatars/ada-lovelace-profile-picture.png",
        "description": "A perfectly ordinary sentence that happens to be quite long indeed.",
        "enabled": True,
    }
    data = await _record(addon, [_recorded_flow(body=json.dumps(payload).encode())])
    assert data["mock"]["GET /api/users"] == payload
    assert "redacted 0 suspected secret" in capsys.readouterr().err


async def test_record_scrubs_non_json_bodies_via_regex(addon):
    body = f"<html><script>window.TOKEN = '{JWT}';</script></html>".encode()
    await _record(addon, [_recorded_flow(url="https://api.example.com/page", body=body, ct="text/html")])
    written = (addon.mocks_dir / "rec_get_page.html").read_text()
    assert JWT not in written
    assert "<redacted>" in written


async def test_record_raw_preserves_the_original_bytes(addon, capsys):
    body = json.dumps({"access_token": "RESP_SECRET_TOKEN_XYZ"}).encode()
    data = await _record(addon, [_recorded_flow(body=body)], raw=True)
    assert data["mock"]["GET /api/users"] == {"access_token": "RESP_SECRET_TOKEN_XYZ"}

    out = addon.rules_file.parent / "recorded.yaml"
    assert "NOT scrubbed" in out.read_text()
    assert "NOT scrubbed" in capsys.readouterr().err


async def test_record_reports_the_redaction_count(addon, capsys):
    body = json.dumps({"access_token": "a", "password": "b", "id": 1}).encode()
    await _record(addon, [_recorded_flow(body=body)])
    assert "redacted 2 suspected secret value(s)" in capsys.readouterr().err
    out = addon.rules_file.parent / "recorded.yaml"
    assert "# Scrubbed by --record: 2 value(s) replaced" in out.read_text()


async def test_reload_command_reloads_without_touching_the_file(addon):
    await _load_pm64(addon)
    addon.cmd_state_set("step", "42")

    # Same content, same hash — only the forced reload picks it up.
    assert "reloading" in addon.cmd_reload()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert addon._state["step"] == 1
    assert len(addon.rules) == 3


async def test_reload_command_with_no_rules_file(addon):
    addon.rules_file.unlink(missing_ok=True)
    assert "reloading" in addon.cmd_reload()
    await asyncio.sleep(0)
    assert addon.rules == ()


async def test_record_commands_round_trip(addon, tmp_path):
    out = tmp_path / "recorded.yaml"
    msg = addon.cmd_record_start(str(out))
    assert "recording to" in msg and "redacted" in msg
    assert addon._recorder is not None
    assert "already recording" in addon.cmd_record_start(str(out))

    flow = make_flow(url="https://api.example.com/rec", response_content=b'{"ok": true}')
    await addon.response(flow)

    assert "flushed to" in addon.cmd_record_stop()
    assert addon._recorder is None
    assert "/rec" in out.read_text()
    assert addon.cmd_record_stop() == "Stunt: not recording"


async def test_record_start_refuses_to_clobber(addon, tmp_path):
    out = tmp_path / "existing.yaml"
    out.write_text("rules: []\n")
    assert "could not start recording" in addon.cmd_record_start(str(out))
    assert addon._recorder is None


def test_cli_passthrough_flag_with_separate_value(monkeypatch):
    """`stunt --listen-port 8081` must reach mitmproxy.

    Regression: subcommands were always registered, so argparse treated the
    flag's value as a positional and rejected it against {init,lint} —
    breaking the pass-through form the README documents.
    """
    captured = {}

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(sys, "argv", ["stunt", "--runner", "dump", "--listen-port", "8081"])
    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(SystemExit) as e:
        cli_main()
    assert e.value.code == 0
    assert "--listen-port" in captured["cmd"] and "8081" in captured["cmd"]


def test_cli_missing_runner_binary_is_actionable(monkeypatch, capsys):
    """A missing mitmdump must not surface as a raw traceback."""

    def boom(cmd, check=False):
        raise FileNotFoundError(2, "No such file or directory", "mitmdump")

    monkeypatch.setattr(sys, "argv", ["stunt", "--runner", "dump"])
    monkeypatch.setattr("subprocess.run", boom)
    with pytest.raises(SystemExit) as e:
        cli_main()
    assert e.value.code == 127
    err = capsys.readouterr().err
    assert "could not be found" in err and "mitmproxy" in err


# --------------------------------------------------------------------------- #
# Phase, scrub and reload regressions
# --------------------------------------------------------------------------- #


async def test_large_json_body_is_still_scrubbed_key_by_key(addon, tmp_path):
    """A response over RECORD_INLINE_MAX must still get the structure-aware scrub
    entirely and still report 0 redactions — a false all-clear on exactly the
    large token responses that carry secrets."""
    from stunt.addon import RECORD_INLINE_MAX

    body = json.dumps(
        {
            "access_token": "short-secret",
            "padding": ["x" * 64] * 60,
        }
    ).encode()
    assert len(body) > RECORD_INLINE_MAX

    out = tmp_path / "big.yaml"
    addon.cmd_record_start(str(out))
    await addon.response(make_flow(url="https://api.example.com/token", response_content=body))
    addon.cmd_record_stop()

    written = out.read_text() + "".join(p.read_text() for p in (addon.mocks_dir).glob("rec_*"))
    assert "short-secret" not in written
    assert "<redacted>" in written


async def test_reload_clears_session_toggle_even_when_the_file_is_unchanged(addon):
    """stunt.rules.list must not keep reporting DISABLED after a forced
    reload had already put the rule back into service."""
    await _load_pm64(addon)
    name = addon.rules[0].name
    addon.cmd_rules_toggle(name)
    assert any("DISABLED" in row for row in addon.cmd_rules_list())

    addon.cmd_reload()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert addon._disabled == {}
    assert not any("DISABLED" in row for row in addon.cmd_rules_list())
    assert any(r.name == name for r in addon.rules)


async def test_request_action_under_a_response_matcher_is_rejected_at_load(addon, caplog):
    """error/kill/modify_request_* run in the request phase; a response-only
    matcher keeps the rule out of that phase, so the pairing can never fire.
    It used to match in silence and lint reported OK."""
    addon.rules_file.write_text(
        yaml.dump(
            {
                "rules": [
                    {
                        "name": "map-503-to-500",
                        "status_code": [503],
                        "error": {"status": 500, "body": {"message": "nope"}},
                    }
                ]
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        await addon._reload()

    assert addon.rules == ()
    assert "can never fire" in caplog.text or "IGNORED" in caplog.text

    from stunt.addon import lint_rules

    assert lint_rules(addon.rules_file, addon.mocks_dir)


async def test_response_matcher_keeps_its_response_phase_action(addon):
    """The same warning must not throw away the half of the rule that works."""
    addon.rules_file.write_text(
        yaml.dump(
            {
                "rules": [
                    {
                        "name": "half-dead",
                        "status_code": [503],
                        "modify_request_headers": {"set": {"X-Dead": "1"}},
                        "modify_response_json": {"set": {"$.patched": True}},
                    }
                ]
            }
        )
    )
    await addon._reload()
    assert len(addon.rules) == 1

    flow = make_flow(url="https://api.example.com/x", status=503, response_content=b'{"ok": true}')
    await addon.request(flow)
    await addon.response(flow)
    assert response_json(flow)["patched"] is True


async def test_mocks_dir_defaults_beside_the_rules_file(tmp_path, monkeypatch):
    """--rules elsewhere/rules.yaml must resolve file: mocks against
    elsewhere/mocks, not against whatever _default_root() guessed."""
    import stunt.addon as mod

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "mocks").mkdir(parents=True)
    (elsewhere / "rules.yaml").write_text("mock: {}\n")

    discovered = str(tmp_path / "discovered" / "mocks")

    class _Opts:
        stunt_rules = str(elsewhere / "rules.yaml")
        stunt_mocks_dir = discovered  # untouched: still the registered default
        stunt_trace = False
        stunt_record = ""

        @staticmethod
        def default(name):
            return discovered if name == "stunt_mocks_dir" else None

    monkeypatch.setattr(mod.ctx, "options", _Opts, raising=False)

    a = mod.Stunt()
    try:
        a.running()
        assert a.mocks_dir == elsewhere / "mocks"
        assert not (tmp_path / "discovered").exists()
    finally:
        await a.aclose()


async def test_non_json_body_does_not_leak_the_flow_to_a_lower_priority_rule(addon):
    """The hit, the state write and the delay are charged before the body is
    inspected; a body that isn't JSON must stay a no-op, not hand the flow on."""
    addon.rules_file.write_text(
        yaml.dump(
            {
                "rules": [
                    {
                        "name": "mod",
                        "priority": 10,
                        "path_contains": "/x",
                        "once": True,
                        "state": {"set": {"k": "v"}},
                        "modify_request_json": {"set": {"$.a": 1}},
                    },
                    {
                        "name": "second",
                        "priority": 0,
                        "path_contains": "/x",
                        "respond_with": {"status": 418, "body": {"teapot": True}},
                    },
                ]
            }
        )
    )
    await addon._reload()

    flow = make_flow(method="POST", url="https://api.example.com/x", content=b"not json")
    await addon.request(flow)

    assert addon._rule_hits == {"mod": 1}
    assert flow.response is None


async def test_deleting_the_rules_file_clears_the_command_view_too(addon):
    """rules.list must stop reporting rules after the file behind them is
    deleted — the same second-source-of-truth shape as the stale toggle."""
    await _load_pm64(addon)
    assert addon.rules

    addon.rules_file.unlink()
    await addon._reload()

    assert addon.rules == ()
    assert addon._rules_all == ()
    assert "0 rules loaded" in addon.cmd_rules_list()[0]


async def test_scrubbing_does_not_move_a_small_body_out_to_a_file(addon, tmp_path):
    """Re-serialising a scrubbed body must not by itself push it past
    RECORD_INLINE_MAX: whether a secret was found should not decide where the
    body is stored."""
    from stunt.addon import RECORD_INLINE_MAX

    # Sized so the compact body fits inline but a pretty-printed one would not.
    payload = {"access_token": "sk-live-1"}
    payload.update({f"k{i}": "v" for i in range(150)})
    body = json.dumps(payload).encode()
    assert len(body) < RECORD_INLINE_MAX < len(json.dumps(payload, indent=2).encode())

    out = tmp_path / "small.yaml"
    addon.cmd_record_start(str(out))
    await addon.response(make_flow(url="https://api.example.com/t", response_content=body))
    addon.cmd_record_stop()

    text = out.read_text()
    assert "<redacted>" in text
    assert "file:" not in text
    assert not list(addon.mocks_dir.glob("rec_*"))


# ========================================================
# Schema modeline in generated files
# ========================================================


def _schema_ref(text: str) -> str:
    line = text.splitlines()[0]
    assert line.startswith("# yaml-language-server: $schema="), line
    return line.split("$schema=", 1)[1].strip()


def test_init_schema_modeline_resolves_outside_a_checkout(tmp_path, monkeypatch):
    """`stunt init` writes into a directory with no docs/ tree, so a relative
    ./docs/ reference would dangle for everyone who installed from PyPI."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("stunt.cli.sys.argv", ["stunt", "init"])

    with pytest.raises(SystemExit) as exc:
        cli_main()

    assert exc.value.code == 0
    ref = _schema_ref((tmp_path / "rules.yaml").read_text())
    assert ref.startswith("https://"), ref
    assert not (tmp_path / "docs").exists()


async def test_recorded_schema_modeline_resolves_outside_a_checkout(addon):
    """Same for `--record`: the file lands wherever the user pointed it."""
    out = addon.rules_file.parent / "recorded.yaml"
    await _record(addon, [_recorded_flow()], out=out)

    ref = _schema_ref(out.read_text())
    assert ref.startswith("https://"), ref
    assert not (out.parent / "docs").exists()


def test_generated_schema_url_is_the_same_in_both_emitters():
    """cli.py and addon.py each carry the constant; they must not drift apart."""
    from stunt import addon as addon_mod
    from stunt import cli as cli_mod

    assert cli_mod.SCHEMA_URL == addon_mod.SCHEMA_URL


# ========================================================
# Runner resolution under pipx / uv isolation
# ========================================================


def test_runner_is_resolved_next_to_the_interpreter_not_via_path(tmp_path, monkeypatch):
    """pipx and `uv tool install` expose only stunt's own entry point, leaving
    mitmdump installed in the venv but absent from PATH."""
    from stunt import cli

    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "mitmdump"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)

    monkeypatch.setattr(cli.sys, "executable", str(bindir / "python"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # nothing resolvable here

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return Mock(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.sys, "argv", ["stunt", "--runner", "dump"])

    with pytest.raises(SystemExit):
        cli.main()

    assert captured["cmd"][0] == str(fake)
