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
            "ATLAS_PUBLIC_URL": "https://atlas.example.com",
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


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.0.2.5", "::"])
def test_nonloopback_bind_requires_explicit_trusted_cidrs(render: Render, bind: str) -> None:
    result, conf = render(ATLAS_BIND_ADDRESS=bind)
    assert result.returncode != 0
    assert "ATLAS_TRUSTED_PROXY_CIDRS" in result.stderr
    assert "not trusted implicitly" in result.stderr
    assert conf == ""  # nothing rendered, so nginx never starts


def test_nonloopback_bind_trusts_only_explicit_cidrs(render: Render) -> None:
    result, conf = render(
        ATLAS_BIND_ADDRESS="0.0.0.0", ATLAS_TRUSTED_PROXY_CIDRS="192.0.2.10, 10.0.0.0/8,fd00::/8"
    )
    assert result.returncode == 0, result.stderr
    # No auto-detected gateway: loopback plus exactly what the operator listed.
    assert trusted(conf) == {"127.0.0.1/32", "::1/128", "192.0.2.10", "10.0.0.0/8", "fd00::/8"}


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"ATLAS_PUBLIC_URL": ""}, "ATLAS_PUBLIC_URL is not set"),
        ({"ATLAS_PUBLIC_URL": "atlas.example.com"}, "absolute http(s) URL"),
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
    assert "server_name atlas.example.com;" in conf
    assert "real_ip_header CF-Connecting-IP;" in conf
    assert "client_max_body_size 25m;" in conf
    assert "zone=atlas_auth:10m rate=5r/m" in conf
    assert "zone=atlas_api:10m rate=50r/s" in conf
    assert "return 444;" in conf
    assert "Strict-Transport-Security" in conf
    assert "geo $realip_remote_addr $atlas_trusted_peer" in conf
    assert "server_tokens off;" in conf


def test_no_hsts_for_http(render: Render) -> None:
    result, conf = render(ATLAS_PUBLIC_URL="http://atlas.example.com:8080/")
    assert result.returncode == 0, result.stderr
    assert "Strict-Transport-Security" not in conf
    assert "server_name atlas.example.com;" in conf


def test_unwritable_config_dir_refuses_clearly(tmp_path: Path) -> None:
    # What happened in CI when the proxy's tmpfs belonged to root: the script must
    # stop at once with a message naming the directory, not carry on half-written.
    import os

    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "nginx").write_text("#!/bin/sh\nexit 0\n")
    (stub / "nginx").chmod(0o755)
    conf = tmp_path / "conf"
    conf.mkdir(mode=0o500)
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        env={
            "PATH": f"{stub}:/usr/local/bin:/usr/bin:/bin",
            "ATLAS_PROXY_CONF_DIR": str(conf),
            "ATLAS_PUBLIC_URL": "https://atlas.example.com",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert f"cannot write {conf}" in result.stderr
    assert "Permission denied" not in result.stderr  # failed before any write
    assert list(conf.iterdir()) == []


# --- Bundled IdP virtual host (Phase 5a; ADR-001) ------------------------------------

IDP_HOST = "id.example.com"


@pytest.fixture
def ca_file(tmp_path: Path) -> Path:
    path = tmp_path / "ca.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\n(test)\n-----END CERTIFICATE-----\n")
    return path


def rendered_files(conf: str, tmp_dir: Path) -> str:
    """atlas.conf plus every include it references."""
    texts = [conf]
    for line in conf.splitlines():
        line = line.strip()
        if line.startswith("include "):
            texts.append(Path(line.split()[1].rstrip(";")).read_text())
    return "\n".join(texts)


def idp_server_block(conf: str) -> str:
    start = conf.rindex("server {", 0, conf.index(f"server_name {IDP_HOST};"))
    return conf[start : conf.index("\n}\n", start)]


def test_no_idp_vhost_without_the_setting(render: Render) -> None:
    result, conf = render()
    assert result.returncode == 0, result.stderr
    assert "atlas_idp" not in conf
    assert IDP_HOST not in conf


def test_idp_vhost_reencrypts_with_verification(
    render: Render, ca_file: Path, tmp_path: Path
) -> None:
    result, conf = render(
        ATLAS_IDP_PUBLIC_URL=f"https://{IDP_HOST}", ATLAS_IDP_CA_FILE=str(ca_file)
    )
    assert result.returncode == 0, result.stderr
    assert f"and the IdP at {IDP_HOST}" in result.stdout
    block = idp_server_block(conf)
    assert "listen 8080;" in block and "default_server" not in block
    inc = (tmp_path / "conf" / "atlas-idp-proxy.inc").read_text()
    assert "proxy_pass https://atlas_idp;" in inc
    assert "server idp:8443;" in conf
    assert "proxy_ssl_verify on;" in inc
    assert f"proxy_ssl_trusted_certificate {ca_file};" in inc
    assert "proxy_ssl_name idp;" in inc
    assert "proxy_set_header Host $host;" in inc
    assert "proxy_set_header X-Forwarded-Proto https;" in inc
    # Kanidm reads the FIRST X-Forwarded-For entry: the proxy overwrites it.
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in inc
    # Unknown hosts are still dropped by the default server.
    assert "return 444;" in conf


def test_no_appended_forwarded_for_anywhere(render: Render, ca_file: Path, tmp_path: Path) -> None:
    result, conf = render(
        ATLAS_IDP_PUBLIC_URL=f"https://{IDP_HOST}", ATLAS_IDP_CA_FILE=str(ca_file)
    )
    assert result.returncode == 0, result.stderr
    everything = rendered_files(conf, tmp_path)
    assert "proxy_add_x_forwarded_for" not in everything
    assert "proxy_add_x_forwarded_for" not in SCRIPT.read_text()
    assert "proxy_ssl_verify off" not in everything


def test_idp_auth_endpoints_rate_limited_per_client(render: Render, ca_file: Path) -> None:
    result, conf = render(
        ATLAS_IDP_PUBLIC_URL=f"https://{IDP_HOST}",
        ATLAS_IDP_CA_FILE=str(ca_file),
        ATLAS_RATE_LIMIT_IDP_AUTH="12r/m",
    )
    assert result.returncode == 0, result.stderr
    assert "limit_req_zone $atlas_idp_auth_key zone=atlas_idp_auth:10m rate=12r/m;" in conf
    assert "POST $binary_remote_addr;" in conf  # credential submissions, per client IP
    block = idp_server_block(conf)
    for location in (
        "location ^~ /ui/login/ {",
        "location = /v1/auth {",
        "location = /v1/reauth {",
    ):
        start = block.index(location)
        assert "limit_req zone=atlas_idp_auth" in block[start : block.index("}", start)], location
    # Discovery, JWKS and the token endpoint go through the unthrottled catch-all.
    catch_all = block[block.index("location / {") :]
    assert "limit_req" not in catch_all


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://id.example.com", "must be an https:// URL"),
        ("id.example.com", "must be an https:// URL"),
        ("https://id.example.com/kanidm", "must have no path"),
        ("https://user@id.example.com", "must not contain a user name"),
        ("https://bad_host;evil", "invalid host name"),
        ("https://atlas.example.com", "different host name than ATLAS_PUBLIC_URL"),
        ("https://ATLAS.example.com:8443/", "different host name than ATLAS_PUBLIC_URL"),
    ],
)
def test_bad_idp_url_refuses_to_start(
    render: Render, ca_file: Path, url: str, message: str
) -> None:
    result, conf = render(ATLAS_IDP_PUBLIC_URL=url, ATLAS_IDP_CA_FILE=str(ca_file))
    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert "ATLAS_IDP_PUBLIC_URL" in result.stderr and message in result.stderr
    assert conf == ""


def test_missing_ca_refuses_to_start(render: Render, tmp_path: Path) -> None:
    result, conf = render(
        ATLAS_IDP_PUBLIC_URL=f"https://{IDP_HOST}", ATLAS_IDP_CA_FILE=str(tmp_path / "none.pem")
    )
    assert result.returncode != 0
    assert "internal CA certificate" in result.stderr and "bundled-idp" in result.stderr
    assert conf == ""


def test_bad_idp_rate_refuses_to_start(render: Render, ca_file: Path) -> None:
    result, _ = render(
        ATLAS_IDP_PUBLIC_URL=f"https://{IDP_HOST}",
        ATLAS_IDP_CA_FILE=str(ca_file),
        ATLAS_RATE_LIMIT_IDP_AUTH="lots",
    )
    assert result.returncode != 0
    assert "ATLAS_RATE_LIMIT_IDP_AUTH" in result.stderr


def test_idp_vhost_keeps_the_trust_model(render: Render, ca_file: Path) -> None:
    # Same trusted peers with and without the IdP (ADR-003 §1 is unchanged).
    _, without = render()
    _, with_idp = render(ATLAS_IDP_PUBLIC_URL=f"https://{IDP_HOST}", ATLAS_IDP_CA_FILE=str(ca_file))
    assert trusted(with_idp) == trusted(without)


def test_switching_back_to_byo_removes_the_idp_include(
    render: Render, ca_file: Path, tmp_path: Path
) -> None:
    render(ATLAS_IDP_PUBLIC_URL=f"https://{IDP_HOST}", ATLAS_IDP_CA_FILE=str(ca_file))
    inc = tmp_path / "conf" / "atlas-idp-proxy.inc"
    assert inc.exists()
    result, _ = render()
    assert result.returncode == 0
    assert not inc.exists()
