"""`atlas-admin init` with the bundled IdP (PLAN Phase 5a; ADR-001, ADR-004 §3)."""

import os
import stat
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from atlas.kernel.cli import main
from atlas.kernel.cli.idp import IdpOptions, render_server_toml
from atlas.kernel.cli.init import InitError, Ownership, run_init

SUBNET = "10.249.73.0/28"
OPTS = IdpOptions(
    public_url="https://id.example.com",
    front_subnet=SUBNET,
    backup_schedule="0 * * * *",
    backup_versions=24,
)
CA_FILES = ("idp-ca/ca.key", "idp-ca/ca.pem")
SERVER_FILES = ("idp/server.key", "idp/chain.pem")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def owners_self() -> Ownership:
    uid, gid = os.getuid(), os.getgid()
    return Ownership(
        app_uid=uid,
        app_gid=gid,
        postgres_uid=uid,
        idp_uid=uid,
        idp_gid=gid,
        admin_uid=uid,
        admin_gid=gid,
    )


def certs(path: Path) -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates(path.read_bytes())


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    return tmp_path / "secrets"


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    return tmp_path / "backups"


def test_creates_ca_server_cert_and_config(secrets_dir: Path, backups: Path) -> None:
    report = run_init(secrets_dir, backup_dir=backups, idp=OPTS)
    for name in (*CA_FILES, *SERVER_FILES, "idp/server.toml", "idp-trust/ca.pem"):
        assert (secrets_dir / name).is_file(), name
        assert name in report.created
    # Without --chown everything is strict (0700/0600), like the other secrets.
    for d in ("idp-ca", "idp", "idp-trust"):
        assert mode(secrets_dir / d) == 0o700
    for name in (*CA_FILES, *SERVER_FILES, "idp/server.toml", "idp-trust/ca.pem"):
        assert mode(secrets_dir / name) == 0o600
    assert (backups / "idp-online").is_dir() and (backups / "idp-restore").is_dir()


def test_certificates_fit_the_proxy_and_the_healthcheck(secrets_dir: Path) -> None:
    run_init(secrets_dir, idp=OPTS)
    (ca,) = certs(secrets_dir / "idp-ca/ca.pem")
    leaf, chain_ca = certs(secrets_dir / "idp/chain.pem")
    # chain.pem = leaf + CA: `kanidmd scripting healthcheck -v` trusts what follows the leaf.
    assert chain_ca == ca
    assert certs(secrets_dir / "idp-trust/ca.pem") == [ca]
    leaf.verify_directly_issued_by(ca)

    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    # idp: atlas-proxy's proxy_ssl_name. localhost: the healthcheck's URL for [::]:8443.
    assert names.get_values_for_type(x509.DNSName) == ["idp", "localhost"]
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]

    key = serialization.load_pem_private_key(
        (secrets_dir / "idp/server.key").read_bytes(), password=None
    )
    assert isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1"
    assert (secrets_dir / "idp/server.key").read_text().startswith("-----BEGIN PRIVATE KEY-----")
    now = datetime.now(UTC)
    assert leaf.not_valid_after_utc - now > timedelta(days=2 * 365)
    assert ca.not_valid_after_utc - now > timedelta(days=15 * 365)


def test_second_run_overwrites_nothing(secrets_dir: Path, backups: Path) -> None:
    run_init(secrets_dir, backup_dir=backups, idp=OPTS)
    files = [*CA_FILES, *SERVER_FILES, "idp/server.toml", "idp-trust/ca.pem"]
    before = {
        n: ((secrets_dir / n).read_bytes(), (secrets_dir / n).stat().st_mtime_ns) for n in files
    }
    report = run_init(secrets_dir, backup_dir=backups, idp=OPTS)
    after = {
        n: ((secrets_dir / n).read_bytes(), (secrets_dir / n).stat().st_mtime_ns) for n in files
    }
    assert after == before
    assert set(files) <= set(report.kept)
    assert report.warnings == []


def test_rotate_keeps_the_ca(secrets_dir: Path) -> None:
    run_init(secrets_dir, idp=OPTS)
    ca_before = {n: (secrets_dir / n).read_bytes() for n in CA_FILES}
    key_before = (secrets_dir / "idp/server.key").read_bytes()
    leaf_before = certs(secrets_dir / "idp/chain.pem")[0]

    report = run_init(
        secrets_dir,
        idp=IdpOptions(**{**OPTS.__dict__, "rotate_cert": True}),
    )
    assert {n: (secrets_dir / n).read_bytes() for n in CA_FILES} == ca_before
    assert (secrets_dir / "idp/server.key").read_bytes() != key_before
    leaf, chain_ca = certs(secrets_dir / "idp/chain.pem")
    assert leaf.serial_number != leaf_before.serial_number
    (ca,) = certs(secrets_dir / "idp-ca/ca.pem")
    assert chain_ca == ca
    leaf.verify_directly_issued_by(ca)
    assert any("rotated" in line for line in report.lines())
    # And a normal run afterwards keeps the rotated certificate.
    rotated = (secrets_dir / "idp/chain.pem").read_bytes()
    run_init(secrets_dir, idp=OPTS)
    assert (secrets_dir / "idp/chain.pem").read_bytes() == rotated


def test_never_replaces_a_partial_ca(secrets_dir: Path) -> None:
    run_init(secrets_dir, idp=OPTS)
    (secrets_dir / "idp-ca/ca.pem").unlink()
    key = (secrets_dir / "idp-ca/ca.key").read_bytes()
    with pytest.raises(InitError, match="never replaces the internal CA"):
        run_init(secrets_dir, idp=OPTS)
    assert (secrets_dir / "idp-ca/ca.key").read_bytes() == key
    assert not (secrets_dir / "idp-ca/ca.pem").exists()


def test_refuses_a_chain_from_another_ca(secrets_dir: Path, tmp_path: Path) -> None:
    run_init(secrets_dir, idp=OPTS)
    other = tmp_path / "other"
    run_init(other, idp=OPTS)
    for name in SERVER_FILES:
        (secrets_dir / name).write_bytes((other / name).read_bytes())
    with pytest.raises(InitError, match=r"was not issued by idp-ca/ca\.pem"):
        run_init(secrets_dir, idp=OPTS)


def test_missing_server_cert_is_reissued_from_the_ca(secrets_dir: Path) -> None:
    run_init(secrets_dir, idp=OPTS)
    ca = (secrets_dir / "idp-ca/ca.key").read_bytes()
    for name in SERVER_FILES:
        (secrets_dir / name).unlink()
    report = run_init(secrets_dir, idp=OPTS)
    assert set(SERVER_FILES) <= set(report.created)
    assert (secrets_dir / "idp-ca/ca.key").read_bytes() == ca


def test_server_toml(secrets_dir: Path) -> None:
    run_init(secrets_dir, idp=OPTS)
    text = (secrets_dir / "idp/server.toml").read_text()
    config = tomllib.loads(text)
    assert config == {
        "version": "2",
        "domain": "id.example.com",
        "origin": "https://id.example.com",
        "bindaddress": "[::]:8443",
        "db_path": "/data/kanidm.db",
        "tls_chain": "/etc/atlas-idp/chain.pem",
        "tls_key": "/etc/atlas-idp/server.key",
        "log_level": "info",
        "http_client_address_info": {"x-forward-for": [SUBNET]},
        "online_backup": {
            "path": "/var/lib/atlas-idp-backups",
            "schedule": "0 * * * *",
            "versions": 24,
            "compression": "gzip",
        },
    }
    # Never trust-all: neither the legacy key nor the hidden v2 variant.
    for forbidden in ("trust_x_forward_for", "x-forward-for-all-source-trusted"):
        assert forbidden not in text


def test_server_toml_follows_the_settings(secrets_dir: Path) -> None:
    run_init(secrets_dir, idp=OPTS)
    changed = IdpOptions(
        public_url="https://login.example.net:8443",
        front_subnet="10.250.1.16/28",
        backup_schedule="*/5 * * * * * *",
        backup_versions=3,
    )
    report = run_init(secrets_dir, idp=changed)
    config = tomllib.loads((secrets_dir / "idp/server.toml").read_text())
    assert (config["domain"], config["origin"]) == (
        "login.example.net",
        "https://login.example.net:8443",
    )
    assert config["http_client_address_info"] == {"x-forward-for": ["10.250.1.16/28"]}
    assert config["online_backup"]["schedule"] == "*/5 * * * * * *"
    assert config["online_backup"]["versions"] == 3
    assert "idp/server.toml (re-rendered)" in report.appended
    assert render_server_toml(changed) == (secrets_dir / "idp/server.toml").read_text()


def test_chown_modes_and_owners(secrets_dir: Path, backups: Path, tmp_path: Path) -> None:
    data = tmp_path / "idp-data"
    data.mkdir()
    opts = IdpOptions(**{**OPTS.__dict__, "data_dir": data})
    report = run_init(secrets_dir, backup_dir=backups, owners=owners_self(), idp=opts)
    expected = {
        "idp-ca": 0o700,
        "idp-ca/ca.key": 0o600,
        "idp-ca/ca.pem": 0o600,
        # Read-only for everyone; idp reads them through its group.
        "idp": 0o750,
        "idp/server.key": 0o440,
        "idp/chain.pem": 0o440,
        "idp/server.toml": 0o440,
        # Public: only the CA certificate, for atlas-proxy (uid 101).
        "idp-trust": 0o755,
        "idp-trust/ca.pem": 0o644,
    }
    assert {n: mode(secrets_dir / n) for n in expected} == expected
    # Raw Kanidm backups are never world-readable; setgid keeps the reader's group.
    assert stat.S_IMODE((backups / "idp-online").stat().st_mode) == 0o2750
    assert stat.S_IMODE((backups / "idp-restore").stat().st_mode) == 0o2750
    assert mode(data) == 0o700
    assert report.warnings == []
    # Idempotent and quiet, including the re-rendered server.toml at 0440.
    again = run_init(secrets_dir, backup_dir=backups, owners=owners_self(), idp=opts)
    assert again.warnings == []
    assert {n: mode(secrets_dir / n) for n in expected} == expected


def test_chown_sets_the_container_owners(secrets_dir: Path, backups: Path, tmp_path: Path) -> None:
    calls: list[tuple[str, int, int]] = []

    def fake_chown(path: object, uid: int, gid: int) -> None:
        calls.append((str(path), uid, gid))

    data = tmp_path / "idp-data"
    data.mkdir()
    mp = pytest.MonkeyPatch()
    mp.setattr(os, "chown", fake_chown)
    try:
        run_init(
            secrets_dir,
            backup_dir=backups,
            owners=Ownership(),
            idp=IdpOptions(**{**OPTS.__dict__, "data_dir": data}),
        )
    finally:
        mp.undo()
    owner = {Path(p).relative_to(tmp_path).as_posix(): (u, g) for p, u, g in calls}
    assert owner["secrets/idp-ca/ca.key"] == (0, 0)
    assert owner["secrets/idp/server.key"] == (0, 10002)
    assert owner["secrets/idp/server.toml"] == (0, 10002)
    assert owner["secrets/idp-trust/ca.pem"] == (0, 0)
    assert owner["backups/idp-online"] == (10002, 10001)
    assert owner["backups/idp-restore"] == (10001, 10002)
    assert owner["idp-data"] == (10002, 10002)


def test_missing_data_volume_is_an_error(secrets_dir: Path, tmp_path: Path) -> None:
    opts = IdpOptions(**{**OPTS.__dict__, "data_dir": tmp_path / "absent"})
    with pytest.raises(InitError, match="idp data volume"):
        run_init(secrets_dir, owners=owners_self(), idp=opts)


def test_no_key_material_printed(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ATLAS_IDP_PUBLIC_URL", "https://id.home.arpa")
    assert main(["init", "--secrets-dir", str(secrets_dir)]) == 0
    out = capsys.readouterr().out
    assert "idp-ca/ca.key" in out
    assert "PRIVATE KEY" not in out and "CERTIFICATE" not in out
    for name in ("idp-ca/ca.key", "idp/server.key"):
        body = (secrets_dir / name).read_text().splitlines()[1]
        assert body not in out


def test_cli_idp_flags_need_the_bundled_idp(
    secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "--secrets-dir", str(secrets_dir), "--rotate-idp-cert"]) == 2
    assert "ATLAS_IDP_PUBLIC_URL is not set" in capsys.readouterr().err
    assert not secrets_dir.exists()


def test_cli_rotate(secrets_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_IDP_PUBLIC_URL", "https://id.home.arpa")
    assert main(["init", "--secrets-dir", str(secrets_dir)]) == 0
    ca = (secrets_dir / "idp-ca/ca.key").read_bytes()
    chain = (secrets_dir / "idp/chain.pem").read_bytes()
    assert main(["init", "--secrets-dir", str(secrets_dir), "--rotate-idp-cert"]) == 0
    assert (secrets_dir / "idp-ca/ca.key").read_bytes() == ca
    assert (secrets_dir / "idp/chain.pem").read_bytes() != chain


def test_cli_invalid_idp_settings(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ATLAS_IDP_PUBLIC_URL", "https://id.home.arpa")
    monkeypatch.setenv("ATLAS_IDP_FRONT_SUBNET", "8.8.8.0/28")
    assert main(["init", "--secrets-dir", str(secrets_dir)]) == 2
    assert "ATLAS_IDP_FRONT_SUBNET: must be a private range" in capsys.readouterr().err
    assert not secrets_dir.exists()
