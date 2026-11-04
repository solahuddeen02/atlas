"""Bundled IdP files written by `atlas-admin init` (ADR-001, ADR-004 §3, PLAN Phase 5a).

Only with the bundled IdP (ATLAS_IDP_PUBLIC_URL set). Under the secrets dir:

| Path | What | Mounted into | Owner, mode (with --chown) |
|---|---|---|---|
| `idp-ca/ca.key` | Internal CA private key | **no running service** | root, 0600 (dir 0700) |
| `idp-ca/ca.pem` | Internal CA certificate (master copy) | no running service | root, 0600 |
| `idp/server.key` | The idp service's TLS key | idp, read-only | root:idp, 0440 (dir 0750) |
| `idp/chain.pem` | Its certificate + the CA certificate | idp, read-only | root:idp, 0440 |
| `idp/server.toml` | Kanidm's whole configuration (no secrets) | idp, read-only | root:idp, 0440 |
| `idp-trust/ca.pem` | The CA certificate (public) | atlas-proxy, later api | root, 0644 (0755) |

Under the backups dir:

| Path | What | Owner, mode |
|---|---|---|
| `idp-online/` | Kanidm's online backups (written by idp) | idp:atlas, 2750 |
| `idp-restore/` | A backup staged by `atlas-admin restore` for idp to read | atlas:idp, 2750 |

Rules:
- The CA and the server key/certificate are created only if missing and never
  overwritten. `--rotate-idp-cert` issues a new server key/certificate from the
  existing CA; nothing ever rotates the CA.
- server.toml and idp-trust/ca.pem are derived (from the settings and the CA), so they
  are rewritten whenever they differ from what the settings produce.
- Key material is never printed.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from atlas.kernel.cli.init import (
    DIR_MODE,
    FILE_MODE,
    InitError,
    InitReport,
    Ownership,
    _ensure_dir,
    _set_owner,
    _write_new_file,
)

# Secrets dir layout.
CA_DIR = "idp-ca"
CA_KEY = f"{CA_DIR}/ca.key"
CA_CERT = f"{CA_DIR}/ca.pem"
IDP_DIR = "idp"
SERVER_KEY = f"{IDP_DIR}/server.key"
SERVER_CHAIN = f"{IDP_DIR}/chain.pem"
SERVER_CONFIG = f"{IDP_DIR}/server.toml"
TRUST_DIR = "idp-trust"
TRUST_CERT = f"{TRUST_DIR}/ca.pem"

# Backups dir layout.
ONLINE_BACKUP_DIR = "idp-online"
RESTORE_DIR = "idp-restore"

# Paths inside the idp container (docker-compose.yml mounts them).
CONTAINER_CONFIG_DIR = "/etc/atlas-idp"
CONTAINER_CONFIG = f"{CONTAINER_CONFIG_DIR}/server.toml"
CONTAINER_DATA_DIR = "/data"
CONTAINER_ONLINE_BACKUP_DIR = "/var/lib/atlas-idp-backups"
CONTAINER_RESTORE_DIR = "/restore"
# Kanidm's healthcheck (`kanidmd scripting healthcheck`) connects to its first
# bindaddress with "[::]" replaced by "localhost" (kanidm v1.12.0
# server/daemon/src/main.rs:398-405), so the certificate also names localhost.
BIND_ADDRESS = "[::]:8443"
SERVICE_NAME = "idp"
CERT_NAMES = (SERVICE_NAME, "localhost")

CA_DAYS = 20 * 365
SERVER_CERT_DAYS = 3 * 365
RENEW_WARNING_DAYS = 60
CLOCK_SKEW = timedelta(minutes=5)

ROTATE_HINT = (
    "run `docker compose run --rm atlas-idp-init atlas-admin init --secrets-dir=/secrets "
    "--backup-dir=/backups --chown --idp-data-dir=/idp-data --rotate-idp-cert`"
)


@dataclass(frozen=True)
class IdpOptions:
    public_url: str  # validated origin, e.g. https://id.example.com
    front_subnet: str  # validated IPv4 CIDR
    backup_schedule: str
    backup_versions: int
    # The idp data volume (mounted into atlas-idp-init); prepared only with --chown.
    data_dir: Path | None = None
    rotate_cert: bool = False


# --- Certificates -----------------------------------------------------------------


def _new_key() -> ec.EllipticCurvePrivateKey:
    # P-256: accepted by Kanidm's TLS minimums (kanidm v1.12.0 server/core/src/crypto.rs:62-140).
    return ec.generate_private_key(ec.SECP256R1())


def _key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _cert_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _now() -> datetime:
    return datetime.now(UTC)


def _new_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Atlas internal IdP CA")])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - CLOCK_SKEW)
        .not_valid_after(now + timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _new_server_cert(
    ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = _new_key()
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVICE_NAME)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - CLOCK_SKEW)
        .not_valid_after(now + timedelta(days=SERVER_CERT_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in CERT_NAMES]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _load_key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError):
        raise InitError(f"{path} is not a readable unencrypted private key") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise InitError(f"{path} is not an EC private key")
    return key


def _load_certs(path: Path) -> list[x509.Certificate]:
    try:
        certs = x509.load_pem_x509_certificates(path.read_bytes())
    except ValueError:
        raise InitError(f"{path} does not contain PEM certificates") from None
    return certs


def _same_public_key(key: ec.EllipticCurvePrivateKey, cert: x509.Certificate) -> bool:
    der = serialization.Encoding.DER
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    return key.public_key().public_bytes(der, spki) == cert.public_key().public_bytes(der, spki)


# --- Files ------------------------------------------------------------------------


def _replace_file(path: Path, content: str) -> None:
    """Atomically replace (or create) `path`; the new file starts at mode 0600."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _sync_derived(path: Path, root: Path, content: str, report: InitReport) -> None:
    """Write a derived (non-secret) file if it is missing or differs."""
    label = str(path.relative_to(root))
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        report.kept.append(label)
        return
    existed = path.exists()
    _replace_file(path, content)
    (report.appended if existed else report.created).append(
        f"{label} (re-rendered)" if existed else label
    )


def _ensure_ca(
    root: Path, report: InitReport
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key_path, cert_path = root / CA_KEY, root / CA_CERT
    if key_path.exists() and cert_path.exists():
        key = _load_key(key_path)
        certs = _load_certs(cert_path)
        if len(certs) != 1 or not _same_public_key(key, certs[0]):
            raise InitError(f"{CA_CERT} does not belong to {CA_KEY}; restore both from a backup")
        report.kept += [CA_KEY, CA_CERT]
        return key, certs[0]
    if key_path.exists() or cert_path.exists():
        present, missing = (CA_KEY, CA_CERT) if key_path.exists() else (CA_CERT, CA_KEY)
        raise InitError(
            f"{present} exists but {missing} does not. init never replaces the internal CA: "
            "restore the missing file from a backup"
        )
    key, cert = _new_ca()
    _write_new_file(key_path, _key_pem(key))
    _write_new_file(cert_path, _cert_pem(cert))
    report.created += [CA_KEY, CA_CERT]
    return key, cert


def _write_server_cert(
    root: Path, ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate, *, replace: bool
) -> None:
    key, cert = _new_server_cert(ca_key, ca_cert)
    # Leaf first, then the CA: Kanidm's `healthcheck -v` trusts the certificates after
    # the leaf (kanidm v1.12.0 server/daemon/src/main.rs:448-451).
    chain = _cert_pem(cert) + _cert_pem(ca_cert)
    if replace:
        _replace_file(root / SERVER_KEY, _key_pem(key))
        _replace_file(root / SERVER_CHAIN, chain)
    else:
        _write_new_file(root / SERVER_KEY, _key_pem(key))
        _write_new_file(root / SERVER_CHAIN, chain)


def _ensure_server_cert(
    root: Path,
    ca_key: ec.EllipticCurvePrivateKey,
    ca_cert: x509.Certificate,
    rotate: bool,
    report: InitReport,
) -> None:
    key_path, chain_path = root / SERVER_KEY, root / SERVER_CHAIN
    if rotate:
        _write_server_cert(root, ca_key, ca_cert, replace=True)
        report.appended.append(
            f"{SERVER_KEY}, {SERVER_CHAIN} (rotated; restart idp: docker compose restart idp)"
        )
        return
    if key_path.exists() and chain_path.exists():
        key = _load_key(key_path)
        chain = _load_certs(chain_path)
        if len(chain) != 2 or not _same_public_key(key, chain[0]):
            raise InitError(f"{SERVER_CHAIN} does not match {SERVER_KEY}; {ROTATE_HINT}")
        if chain[1] != ca_cert:
            raise InitError(f"{SERVER_CHAIN} was not issued by {CA_CERT}; {ROTATE_HINT}")
        if chain[0].not_valid_after_utc - _now() < timedelta(days=RENEW_WARNING_DAYS):
            report.warnings.append(
                f"{SERVER_CHAIN} expires on {chain[0].not_valid_after_utc:%Y-%m-%d}; {ROTATE_HINT}"
            )
        report.kept += [SERVER_KEY, SERVER_CHAIN]
        return
    if key_path.exists() or chain_path.exists():
        present = SERVER_KEY if key_path.exists() else SERVER_CHAIN
        raise InitError(f"{present} exists without its pair; {ROTATE_HINT}")
    _write_server_cert(root, ca_key, ca_cert, replace=False)
    report.created += [SERVER_KEY, SERVER_CHAIN]


# --- Kanidm configuration --------------------------------------------------------


def _toml_str(value: str) -> str:
    # Inputs are validated (host names, CIDR, cron fields); JSON quoting is valid TOML.
    return json.dumps(value)


def render_server_toml(opts: IdpOptions) -> str:
    """Kanidm v1.12.0 server config (`version = "2"`). All Kanidm settings live here."""
    parts = urlsplit(opts.public_url)
    host = parts.hostname or ""
    lines = [
        "# Generated by `atlas-admin init` from deploy/.env on every start. Do not edit:",
        "# changes are overwritten. It holds no secrets. All Kanidm settings live in this",
        "# file; docker-compose.yml sets no KANIDM_* variables, because they would override",
        "# it (KANIDM_TRUST_X_FORWARDED_FOR=true would even trust every client).",
        'version = "2"',
        f"domain = {_toml_str(host)}",
        f"origin = {_toml_str(opts.public_url)}",
        f"bindaddress = {_toml_str(BIND_ADDRESS)}",
        f"db_path = {_toml_str(CONTAINER_DATA_DIR + '/kanidm.db')}",
        f"tls_chain = {_toml_str(CONTAINER_CONFIG_DIR + '/chain.pem')}",
        f"tls_key = {_toml_str(CONTAINER_CONFIG_DIR + '/server.key')}",
        'log_level = "info"',
        "",
        "# Trust X-Forwarded-For only from the idp-front network, which only atlas-proxy",
        "# and idp share. atlas-proxy overwrites the header with the verified client address.",
        "[http_client_address_info]",
        f"x-forward-for = [{_toml_str(opts.front_subnet)}]",
        "",
        "# Written gzip-compressed, mode 0644, into the backups dir's idp-online/ (2750).",
        "# atlas-backup copies the newest one into each Atlas backup.",
        "[online_backup]",
        f"path = {_toml_str(CONTAINER_ONLINE_BACKUP_DIR)}",
        f"schedule = {_toml_str(opts.backup_schedule)}",
        f"versions = {opts.backup_versions}",
        'compression = "gzip"',
    ]
    return "\n".join(lines) + "\n"


# --- Entry point -----------------------------------------------------------------


def _ensure_plain_dir(path: Path, report: InitReport, label: str) -> None:
    if path.exists():
        if not path.is_dir():
            raise InitError(f"{path} exists and is not a directory")
        return
    path.mkdir(mode=DIR_MODE)
    os.chmod(path, DIR_MODE)
    report.created.append(label)


def _apply_ownership(
    root: Path, backup_dir: Path | None, opts: IdpOptions, owners: Ownership
) -> None:
    admin = (owners.admin_uid, owners.admin_gid)
    _set_owner(root / CA_DIR, *admin, 0o700)
    for name in (CA_KEY, CA_CERT):
        _set_owner(root / name, *admin, 0o600)
    # Readable by the idp group only, and read-only for everyone: Kanidm warns about
    # config, key and chain files its own uid could change (security_hardening.md).
    _set_owner(root / IDP_DIR, owners.admin_uid, owners.idp_gid, 0o750)
    for name in (SERVER_KEY, SERVER_CHAIN, SERVER_CONFIG):
        _set_owner(root / name, owners.admin_uid, owners.idp_gid, 0o440)
    _set_owner(root / TRUST_DIR, *admin, 0o755)
    _set_owner(root / TRUST_CERT, *admin, 0o644)
    if backup_dir is not None:
        # setgid: new files take the directory's group, so atlas-backup (atlas group)
        # can read Kanidm's backups, and idp (idp group) the staged restore file.
        _set_owner(backup_dir / ONLINE_BACKUP_DIR, owners.idp_uid, owners.app_gid, 0o2750)
        _set_owner(backup_dir / RESTORE_DIR, owners.app_uid, owners.idp_gid, 0o2750)
    if opts.data_dir is not None:
        if not opts.data_dir.is_dir():
            raise InitError(f"{opts.data_dir} (the idp data volume) is not mounted")
        # Only the volume's top directory: what is inside belongs to Kanidm.
        _set_owner(opts.data_dir, owners.idp_uid, owners.idp_gid, 0o700)


def ensure_idp(
    root: Path,
    backup_dir: Path | None,
    opts: IdpOptions,
    owners: Ownership | None,
    report: InitReport,
) -> None:
    """Create or check the bundled IdP's files (see the module docstring)."""
    _ensure_dir(root / CA_DIR, root, report)
    _ensure_dir(root / IDP_DIR, root, report)
    _ensure_dir(root / TRUST_DIR, root, report, public=True)

    ca_key, ca_cert = _ensure_ca(root, report)
    _ensure_server_cert(root, ca_key, ca_cert, opts.rotate_cert, report)
    _sync_derived(root / TRUST_CERT, root, _cert_pem(ca_cert), report)
    _sync_derived(root / SERVER_CONFIG, root, render_server_toml(opts), report)

    if backup_dir is not None:
        _ensure_plain_dir(backup_dir / ONLINE_BACKUP_DIR, report, f"{ONLINE_BACKUP_DIR}/")
        _ensure_plain_dir(backup_dir / RESTORE_DIR, report, f"{RESTORE_DIR}/")
    if owners is not None:
        _apply_ownership(root, backup_dir, opts, owners)
    elif opts.data_dir is not None:
        report.warnings.append("--idp-data-dir is only prepared together with --chown")
