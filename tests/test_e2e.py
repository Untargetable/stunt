"""End-to-end coverage: a real `mitmdump` process driven by a real HTTP client.

Rules target a host under the `.invalid` TLD (RFC 2606, guaranteed never to
resolve), so serving a mock at all proves mocks are served without an upstream.
"""

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

ADDON_PATH = Path(__file__).resolve().parent.parent / "src" / "stunt" / "addon.py"
PORT_RANGE = range(18300, 18400)
NO_BACKEND_HOST = "e2e.invalid.test"  # .invalid is reserved by RFC 2606: never resolves


def _find_mitmdump() -> str:
    found = shutil.which("mitmdump")
    if found:
        return found
    # Fall back to the interpreter's own venv (this suite is run with a
    # specific venv python, which may not have its bin/ on PATH).
    sibling = Path(sys.executable).parent / "mitmdump"
    if sibling.exists():
        return str(sibling)
    return ""


def _free_port() -> int:
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}")


def _wait_for_listening(port: int, proc: subprocess.Popen, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"mitmdump exited early with code {proc.returncode}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)
    pytest.fail(f"mitmdump did not start listening on {port} within {timeout}s")


def _proxied_request(port: int, host: str, path: str, timeout: float = 8.0):
    """Issue a plain-HTTP request through the proxy. The client never resolves
    `host` itself — that's the proxy's job — so a nonexistent host is only a
    problem for the proxy, never for this client."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"127.0.0.1:{port}"}))
    req = urllib.request.Request(f"http://{host}{path}")
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def live_proxy(tmp_path):
    mitmdump = _find_mitmdump()
    if not mitmdump:
        pytest.skip("mitmdump not found on PATH")

    rules_file = tmp_path / "rules.yaml"
    mocks_dir = tmp_path / "mocks"
    mocks_dir.mkdir()
    confdir = tmp_path / "mitmproxy_confdir"

    rules_file.write_text(
        yaml.dump(
            {
                "rules": [
                    {
                        "name": "e2e-mock",
                        "host": r"e2e\.invalid\.test",
                        "path_regex": "^/mock$",
                        "respond_with": {"status": 200, "body": {"mocked": True}},
                    }
                ]
            }
        )
    )

    port = _free_port()
    cmd = [
        mitmdump,
        "-q",
        "-s",
        str(ADDON_PATH),
        "--set",
        f"stunt_rules={rules_file}",
        "--set",
        f"stunt_mocks_dir={mocks_dir}",
        "--set",
        f"confdir={confdir}",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(port),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _wait_for_listening(port, proc)
        yield port
    finally:
        # Guaranteed teardown by exact PID — never leak a listening proxy.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.e2e
def test_respond_with_serves_mock_with_no_backend_and_control_rule_loaded(live_proxy):
    port = live_proxy

    # 1 & 2: the rule serves its mock through a real mitmdump process, verified
    # with a real HTTP client — against a host under `.invalid` that can never
    # resolve, i.e. there is no backend at all. Before the fix this returned
    # 502 Bad Gateway.
    status, body = _proxied_request(port, NO_BACKEND_HOST, "/mock")
    assert status == 200
    assert json.loads(body) == {"mocked": True}

    # 3: control — a request that does NOT match the rule must fall through to
    # a real (failed) upstream connection attempt against the same nonexistent
    # host, proving the rule genuinely loaded and is matching on path, not
    # blanket-mocking the host.
    status, _ = _proxied_request(port, NO_BACKEND_HOST, "/not-mocked")
    assert status == 502
