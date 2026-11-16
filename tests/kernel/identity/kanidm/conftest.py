"""Fixtures for the Kanidm client tests: a throwaway CA and a short unix-socket path."""

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from atlas.kernel.cli.idp import _cert_pem, _key_pem, _new_ca, _new_server_cert
from tests.kernel.identity.kanidm.fakes import TlsMaterial


@pytest.fixture(scope="session")
def tls_material(tmp_path_factory: pytest.TempPathFactory) -> TlsMaterial:
    directory = tmp_path_factory.mktemp("kanidm-tls")
    ca_key, ca_cert = _new_ca()
    _, other_ca_cert = _new_ca()
    key, cert = _new_server_cert(ca_key, ca_cert)
    material = TlsMaterial(
        ca_file=directory / "ca.pem",
        other_ca_file=directory / "other-ca.pem",
        cert_file=directory / "server.pem",
        key_file=directory / "server.key",
    )
    material.ca_file.write_text(_cert_pem(ca_cert))
    material.other_ca_file.write_text(_cert_pem(other_ca_cert))
    material.cert_file.write_text(_cert_pem(cert))
    material.key_file.write_text(_key_pem(key))
    return material


@pytest.fixture
def ca_file(tls_material: TlsMaterial) -> Path:
    return tls_material.ca_file


@pytest.fixture
def socket_path() -> Iterator[Path]:
    # Not tmp_path: a unix socket path is limited to about 100 bytes.
    directory = Path(tempfile.mkdtemp(prefix="ks-"))
    try:
        yield directory / "kanidmd.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)
