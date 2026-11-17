"""The client's person calls (PLAN Phase 5b-3b), as Kanidm 1.12.0 answers them (probed).

Reading a missing or deleted person is `200 null`, also for the UUID of a service account.
A duplicate name is 409. Deleting a missing person is 404. `account_expire` is cleared by
a DELETE that is 200 again when it is not set. A reset intent is `{token, expiry_time}`,
its lifetime clamped by Kanidm to 300..86400 seconds.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from atlas.kernel.identity.kanidm import (
    ENTRY_ATTRIBUTES,
    KanidmClient,
    KanidmInvalidRequest,
    KanidmNotFound,
    KanidmProtocolError,
    ResetIntent,
)
from tests.kernel.identity.kanidm.fakes import (
    BEARER,
    Recorder,
    body_of,
    fake_secret,
    json_response,
    make_client,
    run,
)

PERSON_UUID = "-".join(["0d5c4c2e", "1f0a", "4b5e", "9e55", "3a8b0c6f7d21"])
INTENT = fake_secret("intent", 23)


def call_with(handler: Recorder, ca_file: Path, call):  # type: ignore[no-untyped-def]
    async def scenario() -> object:
        async with make_client(handler, ca_file, token=BEARER) as client:
            return await call(client)

    return run(scenario())


def person_body(**extra: list[str]) -> dict[str, object]:
    attrs = {
        "class": ["account", "memberof", "object", "person"],
        "directmemberof": ["idm_all_persons@id.test.invalid", "atlas_role_child@id.test.invalid"],
        "displayname": ["Somchai"],
        "name": ["somchai"],
        "spn": ["somchai@id.test.invalid"],
        "uuid": [PERSON_UUID],
        # Credentials and reset state a real Kanidm may send; the allowlist drops them.
        "primary_credential": [fake_secret("cred")],
        "credential_update_intent_token_set": [INTENT],
        **extra,
    }
    return {"attrs": attrs}


# --- reading ----------------------------------------------------------------------


def test_a_person_keeps_only_the_allowlisted_attributes() -> None:
    assert "person" in ENTRY_ATTRIBUTES
    keep = ENTRY_ATTRIBUTES["person"]
    assert not [a for a in keep if "credential" in a or "passkey" in a or "intent" in a]
    assert keep == {
        "account_expire",
        "class",
        "directmemberof",
        "displayname",
        "name",
        "spn",
        "uuid",
    }


@pytest.mark.parametrize("ident", ["somchai", "probe.six", PERSON_UUID])
def test_get_person_by_name_or_uuid(ident: str, ca_file: Path) -> None:
    handler = Recorder(lambda r: json_response(200, person_body()))
    entry = call_with(handler, ca_file, lambda c: c.get_person(ident))
    assert (handler.last.method, handler.last.url.path) == ("GET", f"/v1/person/{ident}")
    assert entry.first("uuid") == PERSON_UUID  # type: ignore[attr-defined]
    assert set(entry.attrs) == {  # type: ignore[attr-defined]
        "class",
        "directmemberof",
        "displayname",
        "name",
        "spn",
        "uuid",
    }
    text = repr(entry) + str(entry.attrs)  # type: ignore[attr-defined]
    assert fake_secret("cred") not in text and INTENT not in text


def test_a_missing_person_is_none(ca_file: Path) -> None:
    handler = Recorder(lambda r: json_response(200, None))
    assert call_with(handler, ca_file, lambda c: c.get_person("nobody")) is None


def test_an_entry_that_is_not_a_person_is_none(ca_file: Path) -> None:
    # Whatever the UUID is the UUID of (a service account), it never reads as a person.
    body = person_body()
    body["attrs"]["class"] = ["account", "object", "service_account"]  # type: ignore[index]
    handler = Recorder(lambda r: json_response(200, body))
    assert call_with(handler, ca_file, lambda c: c.get_person(PERSON_UUID)) is None


@pytest.mark.parametrize("bad", ["", "A", "a b", "a/b", "../x", "9x", "x" * 65, "a@b"])
def test_a_bad_person_id_is_refused_before_any_request(bad: str, ca_file: Path) -> None:
    handler = Recorder()
    with pytest.raises(ValueError):
        call_with(handler, ca_file, lambda c: c.get_person(bad))
    assert handler.requests == []


# --- create and delete ----------------------------------------------------------------


def test_create_person_posts_name_and_display_name(ca_file: Path) -> None:
    handler = Recorder()
    call_with(handler, ca_file, lambda c: c.create_person("somchai", display_name="สมชาย ใจดี"))
    assert (handler.last.method, handler.last.url.path) == ("POST", "/v1/person")
    assert body_of(handler.last) == {"attrs": {"name": ["somchai"], "displayname": ["สมชาย ใจดี"]}}


@pytest.mark.parametrize("name", ["", "Somchai", "สมชาย", "9x", "a b", "-x", "_x", "x" * 65])
def test_create_person_refuses_a_name_kanidm_would_refuse(name: str, ca_file: Path) -> None:
    handler = Recorder()
    with pytest.raises(ValueError):
        call_with(handler, ca_file, lambda c: c.create_person(name, display_name="x"))
    assert handler.requests == []


def test_create_person_refuses_an_empty_display_name_and_a_uuid_as_name(ca_file: Path) -> None:
    handler = Recorder()
    for call in (
        lambda c: c.create_person("somchai", display_name=""),
        lambda c: c.create_person("a" + PERSON_UUID[1:], display_name="x"),
    ):
        with pytest.raises(ValueError):
            call_with(handler, ca_file, call)
    assert handler.requests == []


def test_a_rejected_person_carries_kanidms_reason(ca_file: Path) -> None:
    body = {"schemaviolation": {"invalidattributesyntax": "name"}}
    handler = Recorder(lambda r: json_response(400, body))
    with pytest.raises(KanidmInvalidRequest) as raised:
        call_with(handler, ca_file, lambda c: c.create_person("somchai", display_name="x"))
    assert "invalidattributesyntax" in str(raised.value)


def test_delete_person_is_true_then_false(ca_file: Path) -> None:
    answers = iter([json_response(200, None), json_response(404, {"nomatchingentries": None})])
    handler = Recorder(lambda r: next(answers))

    async def calls(client: KanidmClient) -> list[bool]:
        return [await client.delete_person("somchai"), await client.delete_person("somchai")]

    assert call_with(handler, ca_file, calls) == [True, False]
    assert [(r.method, r.url.path) for r in handler.requests] == [
        ("DELETE", "/v1/person/somchai")
    ] * 2
    assert all(r.content == b"" for r in handler.requests)


# --- account_expire ---------------------------------------------------------------------


def test_set_account_expire_is_utc_and_whole_seconds(ca_file: Path) -> None:
    handler = Recorder()
    when = datetime(2026, 11, 17, 12, 30, 5, 987654, tzinfo=UTC)
    call_with(handler, ca_file, lambda c: c.set_person_account_expire("somchai", when))
    assert (handler.last.method, handler.last.url.path) == (
        "PUT",
        "/v1/person/somchai/_attr/account_expire",
    )
    assert body_of(handler.last) == ["2026-11-17T12:30:05Z"]


def test_set_account_expire_refuses_a_naive_time(ca_file: Path) -> None:
    handler = Recorder()
    with pytest.raises(ValueError):
        call_with(
            handler, ca_file, lambda c: c.set_person_account_expire("somchai", datetime(2026, 1, 1))
        )
    assert handler.requests == []


def test_clear_account_expire_is_one_delete_and_touches_only_that_attribute(ca_file: Path) -> None:
    handler = Recorder()
    call_with(handler, ca_file, lambda c: c.clear_person_account_expire("somchai"))
    assert (handler.last.method, handler.last.url.path, handler.last.content) == (
        "DELETE",
        "/v1/person/somchai/_attr/account_expire",
        b"",
    )


# --- reset intent --------------------------------------------------------------------------


def intent_body() -> dict[str, object]:
    return {"token": INTENT, "expiry_time": 1794919434}


def test_a_reset_intent_is_a_secret_with_an_expiry(ca_file: Path) -> None:
    handler = Recorder(lambda r: json_response(200, intent_body()))
    intent = call_with(
        handler, ca_file, lambda c: c.issue_credential_reset_intent("somchai", ttl_seconds=900)
    )
    assert (handler.last.method, handler.last.url.path) == (
        "GET",
        "/v1/person/somchai/_credential/_update_intent/900",
    )
    assert isinstance(intent, ResetIntent)
    assert intent.token.get_secret_value() == INTENT
    assert intent.expiry == datetime.fromtimestamp(1794919434, UTC)
    for text in (repr(intent), str(intent), f"{intent}", f"{intent!r}", repr(vars(intent))):
        assert INTENT not in text


@pytest.mark.parametrize("ttl", [0, -1, 86401, True, 1.5, "900"])
def test_a_ttl_outside_kanidms_range_is_refused_before_any_request(
    ttl: object, ca_file: Path
) -> None:
    handler = Recorder()
    with pytest.raises(ValueError):
        call_with(
            handler,
            ca_file,
            lambda c: c.issue_credential_reset_intent("somchai", ttl_seconds=ttl),  # type: ignore[arg-type]
        )
    assert handler.requests == []


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"token": "", "expiry_time": 1},
        {"token": INTENT},
        {"token": INTENT, "expiry_time": "x"},
        [],
    ],
)
def test_an_answer_that_is_not_a_reset_token_is_a_protocol_error_without_the_body(
    body: object, ca_file: Path
) -> None:
    handler = Recorder(lambda r: json_response(200, body))
    with pytest.raises(KanidmProtocolError) as raised:
        call_with(
            handler, ca_file, lambda c: c.issue_credential_reset_intent("somchai", ttl_seconds=900)
        )
    assert INTENT not in str(raised.value) and INTENT not in repr(raised.value)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_errors_of_the_intent_call_carry_nothing_from_the_response(
    status: int, ca_file: Path
) -> None:
    # Even if an error body held the token, the endpoint is marked sensitive.
    handler = Recorder(lambda r: json_response(status, {"token": INTENT, "detail": INTENT}))
    with pytest.raises(Exception) as raised:
        call_with(
            handler, ca_file, lambda c: c.issue_credential_reset_intent("somchai", ttl_seconds=900)
        )
    assert isinstance(raised.value, KanidmNotFound | Exception)
    assert INTENT not in str(raised.value) and INTENT not in repr(raised.value)
    seen: list[BaseException | None] = [raised.value]
    while seen:
        exc = seen.pop()
        if exc is not None:
            assert INTENT not in repr(vars(exc))
            seen += [exc.__cause__, exc.__context__]


def test_the_intent_call_never_logs_the_token_or_the_response(
    ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = Recorder(lambda r: json_response(200, intent_body()))
    call_with(
        handler, ca_file, lambda c: c.issue_credential_reset_intent("somchai", ttl_seconds=900)
    )
    assert INTENT not in caplog.text and BEARER not in caplog.text
    assert "/v1/person/{id}/_credential/_update_intent/{ttl}" in caplog.text  # a template
