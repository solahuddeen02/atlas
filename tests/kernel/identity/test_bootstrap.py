"""The IdP bootstrap engine against an in-memory Kanidm (PLAN Phase 5b-2).

The real Kanidm is covered by tests/compose/test_idp_bootstrap.py. Here every step is
exercised on its own, including what happens to `idm_admin` when a step fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from atlas.kernel.identity import bootstrap as engine
from atlas.kernel.identity.bootstrap import (
    CONSOLE_TOKEN_LABEL,
    ROLE_POLICIES,
    Bootstrap,
    BootstrapConfig,
    BootstrapError,
    callback_url,
)
from atlas.kernel.identity.kanidm import (
    KanidmAuthError,
    KanidmServerError,
    KanidmUnavailable,
)
from atlas.kernel.identity.record import BOOTSTRAP_VERSION
from tests.kernel.identity.fake_idp import (
    DOMAIN,
    FakeIdp,
    FakeSocket,
    Trace,
    derived,
    make_connect,
    stable_uuid,
)

IDP_URL = f"https://{DOMAIN}"
PUBLIC_URL = "https://atlas.test.invalid"
IMMICH_URL = "https://immich.test.invalid"
CALLBACK = callback_url(PUBLIC_URL)
IMMICH_REDIRECTS = {
    f"{IMMICH_URL}/auth/login",
    f"{IMMICH_URL}/user-settings",
    "app.immich:///oauth-callback",
}
ROLE_GROUPS = [p.group for p in ROLE_POLICIES]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@dataclass
class Rig:
    root: Path
    idp: FakeIdp
    socket: FakeSocket
    trace: Trace
    lines: list[str] = field(default_factory=list)

    @property
    def state_dir(self) -> Path:
        return self.root / "atlas-idp"

    @property
    def clients_dir(self) -> Path:
        return self.root / "idp-clients"

    def config(self, **overrides: Any) -> BootstrapConfig:
        values: dict[str, Any] = {
            "idp_public_url": IDP_URL,
            "callback_url": CALLBACK,
            "immich_url": IMMICH_URL,
            "state_dir": self.state_dir,
            "clients_dir": self.clients_dir,
        }
        return BootstrapConfig(**{**values, **overrides})

    def bootstrap(self, **overrides: Any) -> Bootstrap:
        return Bootstrap(
            self.config(**overrides),
            socket=self.socket,
            connect=make_connect(self.idp),
            emit=self.lines.append,
        )

    def run(self, **overrides: Any) -> engine.BootstrapResult:
        self.lines.clear()
        return run(self.bootstrap(**overrides).run())  # type: ignore[no-any-return]

    def read(self, path: Path) -> str:
        return path.read_text()

    def env(self, path: Path) -> dict[str, str]:
        out = {}
        for line in path.read_text().splitlines():
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                out[key] = value
        return out

    @property
    def state(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "bootstrap.json").read_text())  # type: ignore[no-any-return]

    def snapshot(self) -> dict[str, tuple[bytes, int, int]]:
        files = {}
        for directory in (self.state_dir, self.clients_dir):
            for path in sorted(directory.iterdir()):
                info = path.stat()
                files[f"{directory.name}/{path.name}"] = (
                    path.read_bytes(),
                    stat.S_IMODE(info.st_mode),
                    info.st_mtime_ns,
                )
        return files

    def disables(self) -> int:
        return self.trace.count_prefix("socket.disable")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    (tmp_path / "atlas-idp").mkdir(mode=0o750)
    (tmp_path / "idp-clients").mkdir(mode=0o700)
    trace = Trace()
    idp = FakeIdp(trace)
    return Rig(tmp_path, idp, FakeSocket(idp), trace)


# --- a first run ----------------------------------------------------------------


def test_the_first_run_sets_everything_up(rig: Rig) -> None:
    result = rig.run()
    assert result.skipped is False
    assert result.token_rotated is False  # the first token replaces nothing: no restart needed

    idp = rig.idp
    for name in ["atlas_users", *ROLE_GROUPS, "immich_users"]:
        assert name in idp.groups
    policies = {name: (idp.groups[name].get("credential_type_minimum")) for name in ROLE_GROUPS}
    assert policies == {
        "atlas_role_owner": "mfa",
        "atlas_role_admin": "mfa",
        "atlas_role_adult": "mfa",
        "atlas_role_child": "any",
        "atlas_role_guest": "any",
    }
    assert idp.groups["atlas_role_owner"]["authsession_expiry"] == "28800"
    assert idp.groups["atlas_role_admin"]["authsession_expiry"] == "28800"
    assert "authsession_expiry" not in idp.groups["atlas_role_adult"]  # Kanidm's default
    assert all("account_policy" in idp.groups[g]["class"] for g in ROLE_GROUPS)
    assert idp.groups["atlas_users"]["member"] == {f"{g}@{DOMAIN}" for g in ROLE_GROUPS}
    assert idp.groups["idm_all_persons"]["credential_type_minimum"] == "any"

    account = idp.accounts["atlas-console"]
    assert account["managed_by"] == idp.groups["idm_admins"]["uuid"]
    # Least privilege (version 2): people_admins only, and every one of its six groups
    # managed by the console. It never joins idm_group_admins on a fresh install.
    assert f"atlas-console@{DOMAIN}" in idp.groups["idm_people_admins"]["member"]
    assert f"atlas-console@{DOMAIN}" not in idp.groups["idm_group_admins"]["member"]
    for group in (*ROLE_GROUPS, "immich_users"):
        assert idp.groups[group]["managed_by"] == account["uuid"], group
    for group in ("atlas_users", "idm_all_persons"):
        assert "managed_by" not in idp.groups[group], group


def test_the_first_run_leaves_one_token_and_the_clients(rig: Rig) -> None:
    rig.run()
    idp = rig.idp
    ours = [t for t in idp.tokens.values() if t["label"] == CONSOLE_TOKEN_LABEL]
    assert len(ours) == 1

    env = rig.env(rig.state_dir / "idp.env")
    assert set(env) == {"ATLAS_OIDC_CLIENT_SECRET", "ATLAS_IDP_ADMIN_TOKEN"}
    assert env["ATLAS_OIDC_CLIENT_SECRET"] == idp.client_secrets["atlas"]
    assert idp.token_is_accepted(SecretStr(env["ATLAS_IDP_ADMIN_TOKEN"]))

    atlas = idp.clients["atlas"]
    assert atlas["landing"] == CALLBACK
    assert atlas["origins"] == {CALLBACK}  # the landing URL is the only redirect URL
    assert atlas["strict"] is True
    assert atlas["scope_maps"] == {"atlas_users": frozenset({"openid", "profile", "groups"})}
    assert atlas.get("legacy") in (None, False) and atlas.get("no_pkce") in (None, False)

    immich = idp.clients["immich"]
    assert immich["landing"] == f"{IMMICH_URL}/auth/login"
    assert immich["origins"] == IMMICH_REDIRECTS
    assert immich["scope_maps"] == {"immich_users": frozenset({"openid", "profile", "email"})}
    immich_env = rig.env(rig.clients_dir / "immich.env")
    assert immich_env == {"OAUTH_CLIENT_SECRET": idp.client_secrets["immich"]}


def test_the_first_run_writes_the_three_files_with_the_right_modes(rig: Rig) -> None:
    rig.run()
    modes = {
        p.name: stat.S_IMODE(p.stat().st_mode)
        for d in (rig.state_dir, rig.clients_dir)
        for p in d.iterdir()
    }
    # Owner and the atlas group read; nothing for others. Immich's secret: owner only.
    assert modes == {
        "idp.env": 0o640,
        "groups.json": 0o640,
        "bootstrap.json": 0o640,
        "immich.env": 0o600,
    }
    groups = json.loads((rig.state_dir / "groups.json").read_text())
    assert set(groups) == {"atlas_users", *ROLE_GROUPS, "immich_users"}
    assert groups["atlas_role_owner"] == rig.idp.groups["atlas_role_owner"]["uuid"]
    assert rig.state == {
        "version": BOOTSTRAP_VERSION,
        "digest": rig.config().digest(),
        "complete": True,
    }


def test_the_run_is_ordered_and_idm_admin_is_disabled_last(rig: Rig) -> None:
    rig.run()
    trace = rig.trace
    assert trace[0] == "socket.recover"
    assert trace[-1] == "socket.disable"
    assert rig.disables() == 1 and rig.idp.admin_enabled is False
    # Everything Kanidm does with the session happens before the account is disabled.
    assert [t for t in trace[:-1] if t.startswith("socket.")] == ["socket.recover"]

    def first(prefix: str) -> int:
        return next(i for i, item in enumerate(trace) if item.startswith(prefix))

    # Role groups carry their policy before idm_all_persons is lowered.
    lowered = first("idp.set_group_attr:idm_all_persons")
    for role in ROLE_GROUPS:
        assert first(f"idp.set_group_attr:{role}") < lowered
    # The service account is created before it joins the admin groups.
    assert first("idp.create_service_account") < first("idp.add_group_members:idm_people_admins")
    # The console never joins idm_group_admins; its six groups are managed instead, and
    # that is set after the account exists.
    assert trace.count_prefix("idp.add_group_members:idm_group_admins") == 0
    for group in (*ROLE_GROUPS, "immich_users"):
        assert first("idp.create_service_account") < first(f"idp.set_managed_by:{group}")


def test_every_line_names_what_it_did_and_nothing_secret(rig: Rig) -> None:
    rig.run()
    assert rig.lines[0] == "full run: no bootstrap record yet"
    assert "account idm_admin disabled" in rig.lines
    owner = [ln for ln in rig.lines if ln.startswith("group atlas_role_owner ")]
    assert owner == [f"group atlas_role_owner {rig.idp.groups['atlas_role_owner']['uuid']} created"]
    assert next(ln for ln in rig.lines if ln.startswith("client atlas")).startswith(
        "client atlas created"
    )


# --- the second run: the fast path ----------------------------------------------


def test_a_second_run_changes_nothing_and_does_not_recover(rig: Rig) -> None:
    rig.run()
    before = rig.snapshot()
    recovers = rig.trace.count_prefix("socket.recover")
    mark = len(rig.trace)

    result = rig.run()

    assert result.skipped is True
    assert rig.trace.count_prefix("socket.recover") == recovers  # no recover, no login
    assert not [
        t for t in rig.trace[mark:] if t.startswith("idp.") and not t.startswith("idp.get_group")
    ]
    assert rig.trace[mark:][-1] == "socket.disable"  # E1: still disabled, over the socket
    assert rig.lines == ["up to date: nothing to change", "account idm_admin disabled"]
    after = rig.snapshot()
    # Not one byte or mode anywhere, and no file but the record is touched (mtime too). The
    # record is rewritten, false and then true: the same bytes in the end.
    assert {k: v[:2] for k, v in after.items()} == {k: v[:2] for k, v in before.items()}
    record = "atlas-idp/bootstrap.json"
    assert {k: v for k, v in after.items() if k != record} == {
        k: v for k, v in before.items() if k != record
    }


def test_the_fast_path_still_disables_idm_admin_and_a_failure_fails_the_run(rig: Rig) -> None:
    rig.run()
    rig.socket.fail_on["disable"] = socket_unavailable()
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == "disable"
    assert rig.lines[0] == "up to date: nothing to change"


def socket_unavailable() -> KanidmUnavailable:
    return KanidmUnavailable(
        "cannot use the socket (ENOENT)", operation="admin socket DisableAccount"
    )


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ("version", "the bootstrap changed"),
        ("incomplete", "the last run did not finish"),
        ("digest", "the settings changed"),
        ("remove_idp_env", "idp.env is incomplete"),
        ("remove_groups", "groups.json is missing"),
        ("remove_immich_env", "immich.env is missing"),
        ("recreate_group", "does not match groups.json"),
        ("kill_token", "the stored console token no longer works"),
        ("no_record", "no bootstrap record"),
    ],
)
def test_what_blocks_the_fast_path(rig: Rig, change: str, why: str) -> None:
    rig.run()
    record = rig.state_dir / "bootstrap.json"
    if change == "version":
        record.write_text(json.dumps({**rig.state, "version": BOOTSTRAP_VERSION - 1}))
    elif change == "incomplete":
        record.write_text(json.dumps({**rig.state, "complete": False}))
    elif change == "digest":
        record.write_text(json.dumps({**rig.state, "digest": "0" * 64}))
    elif change == "remove_idp_env":
        (rig.state_dir / "idp.env").write_text("ATLAS_OIDC_CLIENT_SECRET=x\n")
    elif change == "remove_groups":
        (rig.state_dir / "groups.json").unlink()
    elif change == "remove_immich_env":
        (rig.clients_dir / "immich.env").unlink()
    elif change == "recreate_group":
        rig.idp.groups["atlas_role_guest"]["uuid"] = stable_uuid("a group made again")
    elif change == "kill_token":
        rig.idp.tokens.clear()
        rig.idp.in_grace.clear()
    else:
        record.unlink()

    rig.run()

    assert why in rig.lines[0], rig.lines
    assert rig.lines[0].startswith("full run: ")
    assert rig.trace.count_prefix("socket.recover") == 2


def test_changed_settings_change_the_digest(rig: Rig) -> None:
    base = rig.config().digest()
    assert rig.config(callback_url=f"{PUBLIC_URL}/other").digest() != base
    assert rig.config(immich_url=None).digest() != base
    assert rig.config(idp_public_url="https://id2.test.invalid").digest() != base
    # What decides nothing about the IdP's state does not.
    assert rig.config(verify=True, rotate_token=True).digest() == base


def test_changing_a_setting_triggers_a_full_run(rig: Rig) -> None:
    rig.run()
    rig.run(callback_url=f"{PUBLIC_URL}/api/v2/auth/callback")
    assert "the settings changed" in rig.lines[0]
    assert rig.idp.clients["atlas"]["landing"] == f"{PUBLIC_URL}/api/v2/auth/callback"
    assert rig.idp.clients["atlas"]["origins"] == {f"{PUBLIC_URL}/api/v2/auth/callback"}


# --- --verify -------------------------------------------------------------------


def test_verify_does_a_full_run_in_which_everything_is_kept(rig: Rig) -> None:
    rig.run()
    before = rig.snapshot()
    recovers = rig.trace.count_prefix("socket.recover")

    result = rig.run(verify=True)

    assert result.skipped is False and result.token_rotated is False
    assert rig.trace.count_prefix("socket.recover") == recovers + 1
    assert rig.lines[0] == "full run: requested"
    changed = [ln for ln in rig.lines if " created" in ln or " updated" in ln or "removed" in ln]
    assert changed == []
    assert [ln for ln in rig.lines if ln.startswith(("group ", "policy ", "client "))]
    # The files keep their bytes; only the in-progress record is rewritten (same bytes).
    after = rig.snapshot()
    assert {k: v[:2] for k, v in after.items()} == {k: v[:2] for k, v in before.items()}
    assert (
        after["atlas-idp/idp.env"] == before["atlas-idp/idp.env"]
    )  # also the mtime: not rewritten
    assert rig.disables() == 2


def test_a_full_run_removes_a_redirect_url_nobody_set_but_keeps_scope_maps(rig: Rig) -> None:
    rig.run()
    rig.idp.clients["atlas"]["origins"].add("http://evil.test.invalid/cb")
    rig.idp.clients["immich"]["origins"].add("https://immich.test.invalid/other")
    rig.idp.clients["atlas"]["scope_maps"]["atlas_role_guest"] = frozenset({"openid"})
    (rig.state_dir / "bootstrap.json").unlink()  # forces a full run without --verify

    rig.run()

    assert rig.idp.clients["atlas"]["origins"] == {CALLBACK}
    assert rig.idp.clients["immich"]["origins"] == IMMICH_REDIRECTS
    # Scope maps are removed only by --verify (answer 5).
    assert "atlas_role_guest" in rig.idp.clients["atlas"]["scope_maps"]
    assert any("redirect removed http://evil.test.invalid/cb" in ln for ln in rig.lines)


def test_verify_removes_foreign_scope_maps_on_our_two_clients_only(rig: Rig) -> None:
    rig.run()
    rig.idp.clients["atlas"]["scope_maps"]["atlas_role_guest"] = frozenset({"openid"})
    rig.idp.clients["immich"]["scope_maps"]["atlas_role_child"] = frozenset({"openid"})
    rig.idp.clients["other"] = {
        "landing": "https://other.test.invalid/cb",
        "origins": {"https://other.test.invalid/cb"},
        "strict": True,
        "scope_maps": {"atlas_role_guest": frozenset({"openid"})},
    }

    rig.run(verify=True)

    assert set(rig.idp.clients["atlas"]["scope_maps"]) == {"atlas_users"}
    assert set(rig.idp.clients["immich"]["scope_maps"]) == {"immich_users"}
    assert set(rig.idp.clients["other"]["scope_maps"]) == {"atlas_role_guest"}  # not ours
    assert rig.idp.trace.count_prefix("idp.remove_oauth2_scope_map:other") == 0
    removed = [ln for ln in rig.lines if "scope map removed" in ln]
    assert len(removed) == 2


def test_a_client_made_by_hand_is_adopted_and_its_settings_enforced(rig: Rig) -> None:
    idp = rig.idp
    idp.clients["atlas"] = {
        "landing": "https://elsewhere.test.invalid/callback",
        "origins": {"https://elsewhere.test.invalid/callback", "http://plain.test.invalid/cb"},
        "strict": False,
        "legacy": True,
        "no_pkce": True,
        "scope_maps": {},
    }
    idp.client_secrets["atlas"] = derived("hand-made-client")

    rig.run()

    atlas = idp.clients["atlas"]
    assert atlas["landing"] == CALLBACK
    assert atlas["origins"] == {CALLBACK}
    assert atlas["strict"] is True and atlas["legacy"] is False and atlas["no_pkce"] is False
    assert any(ln.startswith("client atlas updated") for ln in rig.lines)
    # Its existing secret is kept, and is the one stored for atlas-api.
    assert rig.env(rig.state_dir / "idp.env")["ATLAS_OIDC_CLIENT_SECRET"] == derived(
        "hand-made-client"
    )


def test_without_immich_nothing_of_immich_is_made(rig: Rig) -> None:
    rig.run(immich_url=None)
    assert "immich" not in rig.idp.clients and "immich_users" not in rig.idp.groups
    assert not (rig.clients_dir / "immich.env").exists()
    assert "immich_users" not in json.loads((rig.state_dir / "groups.json").read_text())

    rig.run()  # now with Immich: a different digest, so a full run
    assert "the settings changed" in rig.lines[0]
    assert rig.idp.clients["immich"]["origins"] == IMMICH_REDIRECTS
    assert (rig.clients_dir / "immich.env").is_file()


def test_the_idp_wins_over_a_stored_client_secret(rig: Rig) -> None:
    rig.run()
    env = rig.state_dir / "idp.env"
    token = rig.env(env)["ATLAS_IDP_ADMIN_TOKEN"]
    env.write_text(f"ATLAS_OIDC_CLIENT_SECRET={derived('stale')}\nATLAS_IDP_ADMIN_TOKEN={token}\n")
    rig.run(verify=True)
    assert rig.env(env)["ATLAS_OIDC_CLIENT_SECRET"] == rig.idp.client_secrets["atlas"]
    assert rig.env(env)["ATLAS_IDP_ADMIN_TOKEN"] == token  # a working token is kept


# --- role groups before idm_all_persons --------------------------------------------


def test_a_role_group_without_its_policy_stops_the_run_before_the_lowering(rig: Rig) -> None:
    real = rig.idp.set_group_attr

    async def silently_ignored(group: str, attr: str, values: Any) -> None:
        if group == "atlas_role_adult":
            await rig.idp._enter("set_group_attr", group, attr)
            return  # a 200 that changed nothing
        await real(group, attr, values)

    rig.idp.set_group_attr = silently_ignored  # type: ignore[method-assign]

    with pytest.raises(BootstrapError) as raised:
        rig.run()

    assert raised.value.step == "all-persons"
    assert "atlas_role_adult" in raised.value.reason
    assert rig.idp.groups["idm_all_persons"]["credential_type_minimum"] == "mfa"  # untouched
    assert rig.trace.count_prefix("idp.set_group_attr:idm_all_persons") == 0
    assert rig.disables() == 1


# --- the console's token --------------------------------------------------------


def our_tokens(rig: Rig) -> list[str]:
    return [i for i, t in rig.idp.tokens.items() if t["label"] == CONSOLE_TOKEN_LABEL]


def test_older_tokens_with_our_label_are_replaced_by_one(rig: Rig) -> None:
    rig.idp.issue_token_by_hand(CONSOLE_TOKEN_LABEL, "left-over-1")
    rig.idp.issue_token_by_hand(CONSOLE_TOKEN_LABEL, "left-over-2")
    rig.idp.issue_token_by_hand("something-else", "someone-elses")
    rig.run()
    assert len(our_tokens(rig)) == 1
    assert len(rig.idp.tokens) == 2  # the other label is not ours to touch
    stored = SecretStr(rig.env(rig.state_dir / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"])
    assert engine.api_token_id(stored) == our_tokens(rig)[0]


def test_a_working_stored_token_is_kept_and_extra_tokens_go(rig: Rig) -> None:
    rig.run()
    kept = rig.env(rig.state_dir / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"]
    rig.idp.issue_token_by_hand(CONSOLE_TOKEN_LABEL, "an-extra-one")
    result = rig.run(verify=True)
    assert result.token_rotated is False
    assert rig.env(rig.state_dir / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"] == kept
    assert len(our_tokens(rig)) == 1
    assert engine.api_token_id(SecretStr(kept)) == our_tokens(rig)[0]


def test_a_stored_token_that_is_gone_is_replaced(rig: Rig) -> None:
    rig.run()
    old = rig.env(rig.state_dir / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"]
    rig.idp.tokens.clear()
    rig.run(verify=True)
    new = rig.env(rig.state_dir / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"]
    assert new != old and len(our_tokens(rig)) == 1


def test_rotation_issues_a_new_token_stores_it_and_then_deletes_the_old_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.run()
    old = rig.env(rig.state_dir / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"]
    real_write = engine.write_atomic

    def traced_write(path: Path, content: str, *, mode: int) -> None:
        rig.trace.append(f"file.write:{path.name}")
        real_write(path, content, mode=mode)

    monkeypatch.setattr(engine, "write_atomic", traced_write)
    mark = len(rig.trace)

    result = rig.run(rotate_token=True)

    assert result.token_rotated is True
    new = rig.env(rig.state_dir / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"]
    assert new != old
    # E3: no waiting. The list shows exactly one token with our label, the new one; the
    # old one is deleted. (Kanidm keeps accepting a deleted token for up to 300 s, which
    # this fake models and the docs describe.)
    assert len(our_tokens(rig)) == 1
    assert engine.api_token_id(SecretStr(new)) == our_tokens(rig)[0]
    assert engine.api_token_id(SecretStr(old)) not in rig.idp.tokens
    assert rig.idp.token_is_accepted(SecretStr(old)) is True  # the grace window
    rig.idp.expire_grace()
    assert rig.idp.token_is_accepted(SecretStr(old)) is False
    assert rig.idp.token_is_accepted(SecretStr(new)) is True
    # Never a moment without a stored working token: written before the old is deleted.
    steps = rig.trace[mark:]
    assert steps.index("file.write:idp.env") < steps.index("idp.delete_api_token:atlas-console")
    assert steps.index("idp.issue_api_token:atlas-console") < steps.index("file.write:idp.env")


def test_a_new_token_that_kanidm_refuses_is_removed_again(rig: Rig) -> None:
    rig.run()
    real = rig.idp.token_is_accepted
    rig.idp.token_is_accepted = lambda token: False  # type: ignore[method-assign]
    with pytest.raises(BootstrapError) as raised:
        rig.run(rotate_token=True)
    rig.idp.token_is_accepted = real  # type: ignore[method-assign]
    assert raised.value.step == "token"
    assert len(our_tokens(rig)) == 1  # the new one was deleted; the stored one is untouched
    stored = SecretStr(rig.env(rig.state_dir / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"])
    assert engine.api_token_id(stored) == our_tokens(rig)[0]
    assert rig.disables() == 2


# --- no secret, no silent failure -------------------------------------------------


def test_no_client_secret_from_kanidm_fails_the_run(rig: Rig) -> None:
    rig.idp.no_secret = True
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == "client-atlas"
    assert "no secret" in raised.value.reason
    # Not stored as if nothing were wrong, and not recorded as finished.
    assert "ATLAS_OIDC_CLIENT_SECRET" not in (
        rig.env(rig.state_dir / "idp.env") if (rig.state_dir / "idp.env").exists() else {}
    )
    assert rig.state["complete"] is False
    assert not (rig.state_dir / "groups.json").exists()
    assert rig.disables() == 1


# --- idm_admin is disabled on every way out ---------------------------------------

STEP_FAILURES = [
    ("login", "login", "login"),
    ("group", "create_group:atlas_role_child", "groups"),
    ("policy", "set_group_attr:atlas_role_adult", "groups"),
    ("members", "add_group_members:atlas_users", "groups"),
    ("immich-group", "create_group:immich_users", "groups"),
    ("all-persons", "set_group_attr:idm_all_persons", "all-persons"),
    ("console", "create_service_account", "console"),
    ("console-group", "add_group_members:idm_people_admins", "console"),
    ("console-manager-first", "set_group_attr:atlas_role_owner:entry_managed_by", "console"),
    ("console-manager-last", "set_group_attr:immich_users:entry_managed_by", "console"),
    ("token", "issue_api_token", "token"),
    ("client-atlas", "create_oauth2_basic_client:atlas", "client-atlas"),
    ("client-atlas-scope", "set_oauth2_scope_map:atlas", "client-atlas"),
    ("client-atlas-secret", "get_oauth2_basic_secret:atlas", "client-atlas"),
    ("client-immich", "create_oauth2_basic_client:immich", "client-immich"),
    ("client-immich-redirect", "add_oauth2_redirect_url:immich", "client-immich"),
]


@pytest.mark.parametrize(("name", "call", "step"), STEP_FAILURES, ids=[s[0] for s in STEP_FAILURES])
def test_a_failing_step_still_disables_idm_admin(rig: Rig, name: str, call: str, step: str) -> None:
    rig.idp.fail_on[call] = KanidmServerError(
        "Kanidm reported an error", operation="POST /x", status=500
    )
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == step
    assert rig.disables() == 1
    assert rig.trace[-1] == "socket.disable"
    assert rig.idp.admin_enabled is False
    assert rig.state["complete"] is False  # the next run is a full one
    assert "account idm_admin disabled" in rig.lines


@pytest.mark.parametrize(("name", "call", "step"), STEP_FAILURES, ids=[s[0] for s in STEP_FAILURES])
def test_a_signal_at_any_step_still_disables_idm_admin(
    rig: Rig, name: str, call: str, step: str
) -> None:
    # SIGTERM cancels the task at whatever it is awaiting.
    rig.idp.fail_on[call] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        rig.run()
    assert rig.disables() == 1
    assert rig.idp.admin_enabled is False
    assert rig.state["complete"] is False


def test_a_failing_recover_still_asks_for_the_disable(rig: Rig) -> None:
    # The reply to RecoverAccount may be lost after Kanidm has already acted on it.
    rig.socket.fail_on["recover"] = socket_unavailable()
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == "recover"
    assert rig.trace == ["socket.recover", "socket.disable"]


def test_a_failing_file_write_still_disables_idm_admin(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Path, content: str, *, mode: int) -> str:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(engine, "write_if_different", refuse)
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step in {"files", "client-immich"}
    assert "PermissionError" in raised.value.reason
    assert rig.disables() == 1 and rig.state["complete"] is False


def test_a_failure_to_record_the_start_happens_before_anything_is_recovered(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Path, content: str, *, mode: int) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(engine, "write_atomic", refuse)
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == "record"
    assert rig.trace == []  # idm_admin was never recovered, so there is nothing to disable


def test_a_failed_disable_after_a_failed_run_is_reported_and_the_first_error_kept(
    rig: Rig,
) -> None:
    rig.idp.fail_on["create_group:atlas_role_guest"] = KanidmServerError(
        "Kanidm reported an error", operation="POST /v1/group", status=500
    )
    rig.socket.fail_on["disable"] = socket_unavailable()
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == "groups"  # not the disable's
    assert any(ln.startswith("WARNING idm_admin could not be disabled") for ln in rig.lines)
    assert rig.state["complete"] is False


def test_a_failed_disable_after_a_good_run_fails_the_run(rig: Rig) -> None:
    rig.socket.fail_on["disable"] = socket_unavailable()
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == "disable"
    assert rig.state["complete"] is False  # never recorded as finished


def test_the_next_run_repairs_what_a_killed_run_left(rig: Rig) -> None:
    # SIGKILL after RecoverAccount: idm_admin stays enabled, the record stays incomplete.
    rig.idp.fail_on["create_oauth2_basic_client:atlas"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        rig.run()
    rig.idp.admin_enabled = True  # as if the disable never ran
    rig.trace.clear()

    rig.run()

    assert rig.state["complete"] is True
    assert rig.idp.admin_enabled is False
    assert rig.trace[0] == "socket.recover" and rig.trace[-1] == "socket.disable"


def test_a_real_cancellation_while_waiting_on_kanidm_disables_idm_admin(rig: Rig) -> None:
    started = asyncio.Event()

    async def scenario() -> None:
        real = rig.idp.create_group

        async def slow(name: str) -> None:
            started.set()
            await asyncio.sleep(60)
            await real(name)

        rig.idp.create_group = slow  # type: ignore[method-assign]
        task = asyncio.create_task(rig.bootstrap().run())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert rig.disables() == 1 and rig.idp.admin_enabled is False


# --- nothing secret leaves -------------------------------------------------------


def secrets_of(rig: Rig) -> list[str]:
    found = [derived(f"admin-password-{n}") for n in range(1, 6)]
    found += list(rig.idp.client_secrets.values())
    for path in (rig.state_dir / "idp.env", rig.clients_dir / "immich.env"):
        if path.exists():
            for value in rig.env(path).values():
                found.append(value)
                found += value.split(".")  # a token's parts, one by one
    return [s for s in found if len(s) >= 16]


def test_no_secret_in_any_output_or_file_but_the_two_that_hold_one(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig.run()
    rig.run()
    rig.run(verify=True)
    rig.run(rotate_token=True)
    found = secrets_of(rig)
    assert len(found) > 6

    texts = {"emitted lines": "\n".join(rig.lines), "log records": caplog.text}
    for name, text in texts.items():
        for secret in found:
            assert secret not in text, f"a secret appears in {name}"
    for directory in (rig.state_dir, rig.clients_dir):
        for path in directory.iterdir():  # also catches a temporary file left behind
            assert path.name in {"idp.env", "groups.json", "bootstrap.json", "immich.env"}
            if path.name not in {"idp.env", "immich.env"}:
                for secret in found:
                    assert secret not in path.read_text(), f"a secret appears in {path.name}"


@pytest.mark.parametrize(("name", "call", "step"), STEP_FAILURES, ids=[s[0] for s in STEP_FAILURES])
def test_no_secret_in_an_error_either(rig: Rig, name: str, call: str, step: str) -> None:
    rig.idp.fail_on[call] = KanidmAuthError(
        "not authenticated", operation="GET /x", status=401, reason="not_authenticated"
    )
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    seen: list[BaseException | None] = [raised.value]
    texts = []
    while seen:
        exc = seen.pop()
        if exc is not None:
            texts += [str(exc), repr(exc), repr(vars(exc))]
            seen += [exc.__cause__, exc.__context__]
    for secret in secrets_of(rig) + [derived(f"admin-password-{n}") for n in (1, 2)]:
        assert all(secret not in text for text in texts)
    assert all(secret not in "\n".join(rig.lines) for secret in secrets_of(rig))


def test_an_env_file_value_that_cannot_be_stored_is_refused_without_naming_it(rig: Rig) -> None:
    rig.idp.client_secrets["atlas"] = "has a space\nand a newline"
    rig.idp.clients["atlas"] = {
        "landing": CALLBACK,
        "origins": {CALLBACK},
        "strict": True,
        "scope_maps": {},
    }
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == "files"
    assert "ATLAS_OIDC_CLIENT_SECRET" in raised.value.reason
    assert "newline" not in raised.value.reason and "space" not in raised.value.reason
    assert rig.disables() == 1


# --- the completion record: atlas-api refuses to start on anything but `complete` -------


@pytest.fixture
def records(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """The rig, with every write of bootstrap.json noted in the trace ("record:False")."""
    real = engine.write_atomic

    def traced(path: Path, content: str, *, mode: int) -> None:
        real(path, content, mode=mode)
        if path.name == "bootstrap.json":
            rig.trace.append(f"record:{json.loads(content)['complete']}")

    monkeypatch.setattr(engine, "write_atomic", traced)
    return rig


def test_a_full_run_is_incomplete_from_its_first_act_until_its_last(records: Rig) -> None:
    records.run()
    trace = records.trace
    assert trace[0] == "record:False"  # before the account is recovered or Kanidm is touched
    assert trace[-1] == "record:True"  # after DisableAccount and every file
    assert trace.index("socket.disable") < trace.index("record:True")
    assert [t for t in trace if t.startswith("record:")] == ["record:False", "record:True"]
    assert records.state["complete"] is True


def test_the_fast_path_flips_the_record_too(records: Rig) -> None:
    records.run()
    records.trace.clear()
    result = records.run()
    assert result.skipped is True
    # False first, then the checks, DisableAccount, and True last.
    assert records.trace[0] == "record:False"
    assert records.trace[-2:] == ["socket.disable", "record:True"]
    assert not [t for t in records.trace if t.startswith("socket.recover")]


@pytest.mark.parametrize(
    "call",
    [
        "login",
        "get_group:atlas_role_child",
        "get_group:idm_all_persons",
        "get_service_account",
        "list_api_tokens",
        "get_oauth2_client:atlas",
        "get_oauth2_client:immich",
        "get_oauth2_basic_secret:atlas",
    ],
)
def test_a_later_failure_after_an_earlier_success_leaves_the_record_incomplete(
    records: Rig, call: str
) -> None:
    records.run()
    assert records.state["complete"] is True  # an earlier run completed; idp.env is on disk
    records.trace.clear()
    records.idp.fail_on[call] = KanidmServerError(
        "Kanidm reported an error", operation="GET /x", status=500
    )

    with pytest.raises(BootstrapError):
        records.run(verify=True)

    assert records.state["complete"] is False  # so atlas-api cannot start on the old files
    assert "record:True" not in records.trace
    assert records.trace[0] == "record:False"
    assert (records.state_dir / "idp.env").is_file()  # the old file is still there


def test_a_failed_disable_on_the_fast_path_leaves_the_record_incomplete(records: Rig) -> None:
    records.run()
    records.trace.clear()
    records.socket.fail_on["disable"] = socket_unavailable()
    with pytest.raises(BootstrapError) as raised:
        records.run()
    assert raised.value.step == "disable"
    assert records.state["complete"] is False
    assert "record:True" not in records.trace


def test_a_signal_on_the_fast_path_leaves_the_record_incomplete(records: Rig) -> None:
    records.run()
    records.socket.fail_on["disable"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        records.run()
    assert records.state["complete"] is False


def test_failing_to_write_the_final_record_fails_the_run(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = engine.write_atomic
    writes: list[str] = []

    def refuse_the_last(path: Path, content: str, *, mode: int) -> None:
        if path.name == "bootstrap.json":
            writes.append(content)
            if len(writes) == 2:
                raise PermissionError(13, "Permission denied", str(path))
        real(path, content, mode=mode)

    monkeypatch.setattr(engine, "write_atomic", refuse_the_last)
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    assert raised.value.step == "record"
    assert rig.state["complete"] is False
    assert rig.disables() == 1


def test_a_fast_path_that_cannot_check_kanidm_falls_back_to_a_full_run(records: Rig) -> None:
    records.run()
    records.trace.clear()
    records.idp.fail_on["get_group:atlas_role_child"] = KanidmServerError(
        "Kanidm reported an error", operation="GET /x", status=500
    )
    result = records.run()
    assert result.skipped is False
    assert "could not be checked" in records.lines[0]
    assert records.state["complete"] is True
