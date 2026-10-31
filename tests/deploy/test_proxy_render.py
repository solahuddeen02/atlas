"""deploy/nginx/40-atlas-render.sh: settings validation and client-IP trust (ADR-003 §1).

The script runs with a stub `nginx` here; the compose job runs it for real
(including `nginx -t`) inside the proxy image.
"""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

Render = Callable[..., tuple[subprocess.CompletedProcess[str], str]]
SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "nginx" / "40-atlas-render.sh"


@pytest.fixture
def render(tmp_path: Path) -> Iterator[Render]:
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "nginx").write_text("#!/bin/sh\nexit 0\n")
    (stub / "nginx").chmod(0o755)
    conf = tmp_path / "conf"
    conf.mkdir()

    def run(**env: str) -> tuple[subprocess.CompletedProcess[str], str]:
        full_env = {
            "PATH": f"{stub}:/usr/local/bin:/usr/bin:/bin",
            "ATLAS_PROXY_CONF_DIR": str(conf),
            "ATLAS_PUBLIC_URL": "https://atlas.home.arpa",
            **env,
        }
        result = subprocess.run(
            ["sh", str(SCRIPT)], env=full_env, capture_output=True, text=True, check=False
        )
        out = conf / "atlas.conf"
        return result, out.read_text() if out.exists() else ""

    yield run


def trusted(conf: str) -> set[str]:
    return {
        line.split()[1].rstrip(";")
        for line in conf.splitlines()
        if line.startswith("set_real_ip_from ")
    }


@pytest.mark.parametrize("bind", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
def test_loopback_bind_trusts_the_gateway(render: Render, bind: str) -> None:
    result, conf = render(ATLAS_BIND_ADDRESS=bind)
    assert result.returncode == 0, result.stderr
    peers = trusted(conf)
    assert {"127.0.0.1/32", "::1/128"} <= peers
    gateway = peers - {"127.0.0.1/32", "::1/128"}
    assert len(gateway) == 1 and next(iter(gateway)).endswith("/32")


def test_loopback_is_the_default(render: Render) -> None:
    result, conf = render()
    assert result.returncode == 0, result.stderr
    assert len(trusted(conf)) == 3


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.5", "::"])
def test_nonloopback_bind_requires_explicit_trusted_cidrs(render: Render, bind: str) -> None:
    result, conf = render(ATLAS_BIND_ADDRESS=bind)
    assert result.returncode != 0
    assert "ATLAS_TRUSTED_PROXY_CIDRS" in result.stderr
    assert "not trusted implicitly" in result.stderr
    assert conf == ""  # nothing rendered, so nginx never starts


def test_nonloopback_bind_trusts_only_explicit_cidrs(render: Render) -> None:
    result, conf = render(
        ATLAS_BIND_ADDRESS="0.0.0.0", ATLAS_TRUSTED_PROXY_CIDRS="192.168.1.10, 10.0.0.0/8,fd00::/8"
    )
    assert result.returncode == 0, result.stderr
    # No auto-detected gateway: loopback plus exactly what the operator listed.
    assert trusted(conf) == {"127.0.0.1/32", "::1/128", "192.168.1.10", "10.0.0.0/8", "fd00::/8"}


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"ATLAS_PUBLIC_URL": ""}, "ATLAS_PUBLIC_URL is not set"),
        ({"ATLAS_PUBLIC_URL": "atlas.home.arpa"}, "absolute http(s) URL"),
        ({"ATLAS_PUBLIC_URL": "https://bad_host;evil/"}, "invalid host name"),
        ({"ATLAS_TRUSTED_PROXY_CIDRS": "10.0.0.0/8;"}, "invalid entry"),
        ({"ATLAS_TRUSTED_PROXY_CIDRS": "*"}, "invalid entry"),
        ({"ATLAS_REAL_IP_HEADER": "X-Real-IP; evil"}, "ATLAS_REAL_IP_HEADER"),
        ({"ATLAS_MAX_BODY_SIZE": "lots"}, "ATLAS_MAX_BODY_SIZE"),
        ({"ATLAS_RATE_LIMIT_AUTH": "fast"}, "ATLAS_RATE_LIMIT_AUTH"),
        ({"ATLAS_RATE_LIMIT_API": "20/s"}, "ATLAS_RATE_LIMIT_API"),
    ],
)
def test_bad_settings_refuse_to_start(render: Render, env: dict[str, str], message: str) -> None:
    result, _ = render(**env)
    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert message in result.stderr


def test_rendered_config(render: Render) -> None:
    result, conf = render(
        ATLAS_REAL_IP_HEADER="CF-Connecting-IP",
        ATLAS_MAX_BODY_SIZE="25m",
        ATLAS_RATE_LIMIT_AUTH="5r/m",
        ATLAS_RATE_LIMIT_API="50r/s",
    )
    assert result.returncode == 0, result.stderr
    assert "server_name atlas.home.arpa;" in conf
    assert "real_ip_header CF-Connecting-IP;" in conf
    assert "client_max_body_size 25m;" in conf
    assert "zone=atlas_auth:10m rate=5r/m" in conf
    assert "zone=atlas_api:10m rate=50r/s" in conf
    assert "return 444;" in conf
    assert "Strict-Transport-Security" in conf
    assert "geo $realip_remote_addr $atlas_trusted_peer" in conf
    assert "server_tokens off;" in conf


def test_no_hsts_for_http(render: Render) -> None:
    result, conf = render(ATLAS_PUBLIC_URL="http://atlas.home.arpa:8080/")
    assert result.returncode == 0, result.stderr
    assert "Strict-Transport-Security" not in conf
    assert "server_name atlas.home.arpa;" in conf
