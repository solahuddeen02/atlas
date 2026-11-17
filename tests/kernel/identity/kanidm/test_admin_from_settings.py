"""`KanidmIdentityAdmin.from_settings` (PLAN Phase 5b-3b): the adapter is built from `Settings`
alone, verifies Kanidm against the CA file, and closes its connection pool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from atlas.kernel.identity.admin import IdentityConfigError, Role
from atlas.kernel.identity.kanidm.admin import KanidmIdentityAdmin
from tests.kernel.identity.fake_kanidm_api import FakeKanidmApi

IDP = "https://id.home.arpa"


def groups_file(tmp_path: Path) -> Path:
    path = tmp_path / "groups.json"
    path.write_text(json.dumps(FakeKanidmApi().groups_json()))
    return path


def test_the_adapter_is_built_from_settings(make_settings, ca_file: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        ATLAS_IDP_PUBLIC_URL=IDP,
        ATLAS_IDP_CA_FILE=str(ca_file),
        ATLAS_IDP_GROUPS_FILE=str(groups_file(tmp_path)),
    )

    async def scenario() -> None:
        async with KanidmIdentityAdmin.from_settings(settings) as admin:
            assert admin._groups.roles[Role.OWNER]  # groups.json was read
            assert admin._origin == IDP  # links are for the public origin

    asyncio.run(scenario())


def test_the_token_the_adapter_sends_is_the_one_in_settings(
    make_settings, ca_file: Path, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        ATLAS_IDP_PUBLIC_URL=IDP,
        ATLAS_IDP_ADMIN_TOKEN="console-" * 5,
        ATLAS_IDP_CA_FILE=str(ca_file),
        ATLAS_IDP_GROUPS_FILE=str(groups_file(tmp_path)),
    )
    admin = KanidmIdentityAdmin.from_settings(settings)
    api = admin._api
    assert repr(api) == "KanidmClient('https://idp:8443', authenticated)"
    assert "console-" * 5 not in repr(admin) and "console-" * 5 not in repr(api)
    asyncio.run(admin.aclose())


def test_byo_has_no_kanidm_adapter(make_settings) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(IdentityConfigError, match="bundled IdP"):
        KanidmIdentityAdmin.from_settings(make_settings())


def test_a_missing_ca_file_is_a_config_error_naming_the_setting(
    make_settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        ATLAS_IDP_PUBLIC_URL=IDP,
        ATLAS_IDP_CA_FILE=str(tmp_path / "missing.pem"),
        ATLAS_IDP_GROUPS_FILE=str(groups_file(tmp_path)),
    )
    with pytest.raises(IdentityConfigError, match="ATLAS_IDP_CA_FILE"):
        KanidmIdentityAdmin.from_settings(settings)


def test_a_missing_groups_file_is_a_config_error_that_names_the_bootstrap(
    make_settings, ca_file: Path, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        ATLAS_IDP_PUBLIC_URL=IDP,
        ATLAS_IDP_CA_FILE=str(ca_file),
        ATLAS_IDP_GROUPS_FILE=str(tmp_path / "nope.json"),
    )
    with pytest.raises(IdentityConfigError, match="re-run the IdP bootstrap"):
        KanidmIdentityAdmin.from_settings(settings)
