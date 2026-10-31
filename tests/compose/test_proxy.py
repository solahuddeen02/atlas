"""Compose acceptance tests for Phase 4: atlas-proxy (PLAN-v1; ADR-003).

Run by the `compose` CI job (RUN_COMPOSE_TESTS=1). Each test builds its own
throwaway stack; see tests/compose/stack.py.
"""

import http.client
import json
from collections.abc import Callable

import pytest

from tests.compose.stack import PUBLIC_HOST, Stack, free_port

MakeStack = Callable[..., Stack]

SPOOFED = "203.0.113.77"  # TEST-NET-3: never a real peer here


def published_ports(stack: Stack) -> dict[str, list[dict[str, object]]]:
    rendered = json.loads(stack.compose("config", "--format", "json").stdout)
    return {name: svc["ports"] for name, svc in rendered["services"].items() if svc.get("ports")}


def log_entries_for(stack: Stack, uri: str) -> list[dict[str, object]]:
    return [e for e in stack.proxy_log() if e.get("uri") == uri]


def test_health_ok_through_proxy(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    assert stack.get("/health") == (200, b'{"status":"ok"}')
    # The SELF_HOSTING check: `curl http://127.0.0.1:8080/health` sends Host: 127.0.0.1.
    assert stack.get("/health", host="127.0.0.1") == (200, b'{"status":"ok"}')
    status, headers, body = stack.request("/")
    assert status == 200 and b"Atlas is running" in body
    assert "server" not in headers or "nginx/" not in headers["server"]


def test_single_published_port(make_stack: MakeStack) -> None:
    stack = make_stack(create_volume=False)
    ports = published_ports(stack)
    assert list(ports) == ["atlas-proxy"]
    (port,) = ports["atlas-proxy"]
    assert port["host_ip"] == "127.0.0.1"  # loopback by default
    assert int(port["target"]) == 8080


def test_bind_and_port_configurable(make_stack: MakeStack) -> None:
    port = free_port()
    stack = make_stack(
        create_volume=False,
        extra_env={
            "ATLAS_BIND_ADDRESS": "0.0.0.0",
            "ATLAS_HTTP_PORT": str(port),
            "ATLAS_TRUSTED_PROXY_CIDRS": "192.0.2.10",
        },
    )
    (published,) = published_ports(stack)["atlas-proxy"]
    assert published["host_ip"] == "0.0.0.0"
    assert str(published["published"]) == str(port)

    # And live, on another loopback port than the default.
    live = make_stack("live")
    live.up()
    live.wait_healthy()
    assert live.get("/health") == (200, b'{"status":"ok"}')


def test_nonloopback_bind_requires_explicit_trusted_cidrs(make_stack: MakeStack) -> None:
    # ADR-003 §1: off loopback, the Docker gateway is never trusted implicitly.
    stack = make_stack(extra_env={"ATLAS_BIND_ADDRESS": "0.0.0.0", "ATLAS_TRUSTED_PROXY_CIDRS": ""})
    stack.up(check=False)
    logs = stack.wait_for_log("atlas-proxy", "refusing to start")
    assert "ATLAS_TRUSTED_PROXY_CIDRS" in logs
    assert "not trusted implicitly" in logs
    # nginx never started, so nothing answers on the published port.
    assert stack.port_unreachable()


def test_nonloopback_bind_with_explicit_cidrs_ignores_gateway(make_stack: MakeStack) -> None:
    # Listed peers only: the gateway (every connection from the host) is not trusted,
    # so a forwarded address from the host is ignored.
    stack = make_stack(
        extra_env={"ATLAS_BIND_ADDRESS": "0.0.0.0", "ATLAS_TRUSTED_PROXY_CIDRS": "192.0.2.10"}
    )
    stack.up()
    stack.wait_healthy()
    stack.request("/health?probe=gw", headers={"X-Forwarded-For": SPOOFED})
    entries = log_entries_for(stack, "/health")
    assert entries and all(e["client"] != SPOOFED for e in entries)
    assert stack.request("/health/ready")[0] == 403  # the host is not a trusted peer


def test_unknown_host_dropped(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    for path in ("/", "/api/x", "/docs"):
        with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)):
            stack.request(path, host="evil.example.net")
    # /health answers on any Host: it is exactly {"status":"ok"}.
    assert stack.get("/health", host="evil.example.net") == (200, b'{"status":"ok"}')


def test_real_ip_trusted_peer_header_used(make_stack: MakeStack) -> None:
    # Loopback bind: the host's connections arrive from the gateway, which is trusted.
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    stack.request("/api/real-ip-probe", headers={"X-Forwarded-For": SPOOFED})
    entries = log_entries_for(stack, "/api/real-ip-probe")
    assert entries and entries[-1]["client"] == SPOOFED
    assert entries[-1]["peer"] != SPOOFED


def test_real_ip_untrusted_ignored(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    stack.from_inside("/api/untrusted-probe", headers={"X-Forwarded-For": SPOOFED})
    entries = log_entries_for(stack, "/api/untrusted-probe")
    assert entries
    assert entries[-1]["client"] != SPOOFED
    assert entries[-1]["client"] == entries[-1]["peer"]


def test_health_ready_trusted_only(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    assert stack.get("/health/ready") == (200, b'{"status":"ready"}')
    assert stack.from_inside("/health/ready") == 403
    # A trusted-looking forwarded address doesn't help an untrusted peer.
    assert stack.from_inside("/health/ready", headers={"X-Forwarded-For": "127.0.0.1"}) == 403


@pytest.mark.parametrize("path", ["/", "/health", "/api/nothing-here"])
def test_security_headers_present(make_stack: MakeStack, path: str) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    _, headers, _ = stack.request(path)
    assert headers["content-security-policy"].startswith("default-src 'self'")
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert "camera=()" in headers["permissions-policy"]
    assert headers["strict-transport-security"].startswith("max-age=")


def test_docs_disabled_in_production(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert stack.request(path)[0] == 404, path


def test_auth_rate_limit_returns_429(make_stack: MakeStack) -> None:
    stack = make_stack(extra_env={"ATLAS_RATE_LIMIT_AUTH": "2r/m"})
    stack.up()
    stack.wait_healthy()
    statuses = [stack.request("/api/auth/callback")[0] for _ in range(12)]
    assert 429 in statuses
    assert statuses[0] != 429


def test_body_size_limit_returns_413(make_stack: MakeStack) -> None:
    stack = make_stack(extra_env={"ATLAS_MAX_BODY_SIZE": "1k"})
    stack.up()
    stack.wait_healthy()
    status, _, _ = stack.request("/api/upload", method="POST", body=b"x" * 4096)
    assert status == 413


def test_byo_mode_has_no_idp_service(make_stack: MakeStack) -> None:
    stack = make_stack(create_volume=False)
    services = stack.compose("config", "--services").stdout.split()
    assert "idp" not in services  # TODO(spike-s): the bundled-idp profile adds it
    assert {"atlas-proxy", "atlas-api", "postgres"} <= set(services)


def test_proxy_refuses_bad_config(make_stack: MakeStack) -> None:
    stack = make_stack(extra_env={"ATLAS_TRUSTED_PROXY_CIDRS": "not-a-cidr"})
    stack.up(check=False)
    stack.wait_for_log("atlas-proxy", "ATLAS_TRUSTED_PROXY_CIDRS has an invalid entry")
    assert stack.port_unreachable()


def test_proxy_host_is_the_public_url(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    assert "serving " + PUBLIC_HOST in stack.logs("atlas-proxy")
