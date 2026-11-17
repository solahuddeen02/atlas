"""Compose acceptance test for Phase 5b-3b: the `kanidm` IdentityAdmin adapter against the
real Kanidm, from inside atlas-api (so `load_settings()`, the mounted CA and groups.json,
and the console's token are the real ones).

One lifecycle: create (a Thai display name), read, every role, app access, a reset link
(its host is ATLAS_IDP_PUBLIC_URL, it opens through the proxy), the person enrolling with it
(a password for a child, a password and a second factor for an owner), disable and enable
(and what that does to a session: probe P9), the refusals for a person with two role groups
or none, a service account's UUID never a person, the lifetime bounds, `check()` with a
stray member, delete. Then the logs of every service are searched for each secret handled.
"""

import json
from collections.abc import Callable

from tests.compose.stack import IDP_HOST, Stack
from tests.compose.test_idp_console_rights import PRELUDE, rights_stack, run_script

MakeStack = Callable[..., Stack]

LIFECYCLE_SCRIPT = """
import asyncio, base64, hashlib, hmac, http.client, io, json, logging, struct, sys, time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

from atlas.kernel.config import load_settings
from atlas.kernel.identity.admin import (
    App, IdentityAdmin, InvalidResetTtl, PartialFailure, PersonDisabled, PersonNotFound,
    Role, RoleInvariantError,
)
from atlas.kernel.identity.kanidm.admin import KanidmIdentityAdmin

console_uuid, idm_admin_uuid = sys.argv[1], sys.argv[2]
log = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(log))
logging.getLogger().setLevel(logging.DEBUG)
settings = load_settings()
INTERNAL, CA = settings.idp_internal_url, str(settings.idp_ca_file)
TOKEN = settings.idp_admin_token.get_secret_value()
handled = [TOKEN, *[p for p in TOKEN.split(".") if len(p) > 12]]
checks, texts = [], []


def check(label, ok):
    checks.append({"label": label, "ok": bool(ok)})


intents = []


def secret(value):
    handled.append(value)
    return value


def intent(value):
    # A reset token: a secret, and the one kind Kanidm's own log records (see the test).
    intents.append(value)
    return secret(value)


def anonymous():
    return httpx.Client(base_url=INTERNAL, verify=CA, timeout=20, trust_env=False)


def totp(key, step, digits, algo):
    mac = hmac.new(key, struct.pack(">Q", int(time.time() // step)), getattr(hashlib, algo)).digest()
    offset = mac[-1] & 15
    return "%0*d" % (digits, (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits)


def find_totp(node):
    if isinstance(node, dict):
        if "secret" in node and isinstance(node["secret"], (list, str)):
            raw = node["secret"]
            key = bytes(raw) if isinstance(raw, list) else raw.encode()
            return key, node.get("step", 30), node.get("digits", 6), str(node.get("algo", "sha1")).lower()
        for value in node.values():
            found = find_totp(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_totp(value)
            if found:
                return found
    return None


def enrol(url, password, second_factor):
    \"\"\"What the person does with the link: exchange it, set a credential, commit.\"\"\"
    token = parse_qs(urlsplit(url).query)["token"][0]
    intent(token)
    client = anonymous()
    reply = client.post("/v1/credential/_exchange_intent", json=token)
    session = reply.json()[0]
    secret(session["token"])
    update = lambda action: client.post("/v1/credential/_update", json=[action, session])
    assert update({"password": password}).status_code == 200
    key = None
    if second_factor:
        status = update("totpgenerate").json()
        key, step, digits, algo = find_totp(status)
        secret(key.hex())
        code = int(totp(key, step, digits, algo))
        assert update({"totpverify": [code, "life"]}).status_code == 200
    assert client.post("/v1/credential/_commit", json=session).status_code == 200
    return key and (key, step, digits, algo)


def login(name, password, factor=None):
    \"\"\"A sign-in; the session token, or the reason it was refused.\"\"\"
    client = anonymous()
    first = {"step": {"init2": {"username": name, "issue": "token", "privileged": False}}}
    reply = client.post("/v1/auth", json=first)
    state = reply.json()["state"]
    if "denied" in state:
        return None, state["denied"]
    headers = {"X-KANIDM-AUTH-SESSION-ID": reply.headers["X-KANIDM-AUTH-SESSION-ID"]}
    method = "passwordmfa" if "passwordmfa" in state.get("choose", []) else "password"
    client.post("/v1/auth", json={"step": {"begin": method}}, headers=headers)
    creds = [{"password": password}]
    if factor is not None:
        creds = [{"totp": int(totp(*factor))}, {"password": password}]
    for cred in creds:
        reply = client.post("/v1/auth", json={"step": {"cred": cred}}, headers=headers)
        state = reply.json()["state"]
        if "denied" in state:
            return None, state["denied"]
    token = secret(state["success"])
    return token, None


def alive(token):
    reply = anonymous().get("/v1/self", headers={"Authorization": "Bearer " + token})
    return reply.status_code


def through_the_proxy(url):
    \"\"\"The browser's request for the link, through atlas-proxy, with the IdP's Host header.\"\"\"
    parts = urlsplit(url)
    conn = http.client.HTTPConnection("atlas-proxy", 8080, timeout=10)
    conn.request("GET", parts.path + "?" + parts.query, headers={"Host": parts.hostname})
    return conn.getresponse().status


async def main():
    async with KanidmIdentityAdmin.from_settings(settings) as admin:
        check("the adapter is an IdentityAdmin", isinstance(admin, IdentityAdmin))
        check("check() is clean at the start", (await admin.check()).ok)
        groups = admin._groups
        console = httpx.Client(
            base_url=INTERNAL, verify=CA, timeout=20, trust_env=False,
            headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json"},
        )

        child = await admin.create_person("life-child", "\\u0e2a\\u0e21\\u0e0a\\u0e32\\u0e22 \\u0e43\\u0e08\\u0e14\\u0e35", Role.CHILD, apps=[App.IMMICH])
        check("a created person reads back", (await admin.get_person(child.ref)) == child)
        check("the Thai display name round-trips", child.display_name == "\\u0e2a\\u0e21\\u0e0a\\u0e32\\u0e22 \\u0e43\\u0e08\\u0e14\\u0e35")
        check("the person has the role and the app asked for", child.role is Role.CHILD and child.apps == {App.IMMICH})
        for role in Role:
            moved = await admin.set_role(child.ref, role)
            check("set_role to %s" % role.value, moved.role is role)
        await admin.set_role(child.ref, Role.CHILD)
        off = await admin.set_app_access(child.ref, App.IMMICH, False)
        on = await admin.set_app_access(child.ref, App.IMMICH, True)
        check("app access goes off and on", off.apps == frozenset() and on.apps == {App.IMMICH})

        # --- a reset link, and the person enrolling with it -------------------------------------
        link = await admin.issue_credential_reset(child.ref)
        url = secret(link.reveal())
        intent(parse_qs(urlsplit(url).query)["token"][0])
        parts = urlsplit(url)
        check("the link is for the public host", (parts.scheme, parts.netloc, parts.path) == ("https", "__IDP_HOST__", "/ui/reset"))
        check("the link opens through the proxy", through_the_proxy(url) == 200)
        check("the link's repr shows nothing of it", url not in repr(link) and "token" not in repr(link))
        password = secret("Zq" + base64.urlsafe_b64encode(hashlib.sha256(b"life-child-1").digest()).decode()[:24] + "!9")
        enrol(url, password, second_factor=False)
        session, why = login("life-child", password)
        check("the child signs in with a password", session is not None and alive(session) == 200)

        # --- disable and enable (probe P9) -------------------------------------------------------
        disabled = await admin.disable(child.ref)
        check("disable shows in the person", disabled.enabled is False and disabled.account_expire is not None)
        check("a disabled person's session is refused", alive(session) == 401)
        check("a disabled person cannot sign in", login("life-child", password)[1] == "account expired")
        try:
            await admin.issue_credential_reset(child.ref)
            check("a disabled person gets no link", False)
        except PersonDisabled:
            check("a disabled person gets no link", True)
        enabled = await admin.enable(child.ref)
        check("enable shows in the person", enabled.enabled is True)
        check("enabling makes the old session valid again (the documented gap)", alive(session) == 200)
        link2 = await admin.issue_credential_reset(child.ref)
        password2 = secret("Zq" + base64.urlsafe_b64encode(hashlib.sha256(b"life-child-2").digest()).decode()[:24] + "!9")
        enrol(link2.reveal(), password2, second_factor=False)
        check("a credential change ends the old session for good", alive(session) == 401)
        check("and the new credential works", login("life-child", password2)[0] is not None)

        # --- an owner needs a second factor ----------------------------------------------------------
        owner = await admin.create_person("life-owner", "Owner", Role.OWNER)
        owner_password = secret("Zq" + base64.urlsafe_b64encode(hashlib.sha256(b"life-owner").digest()).decode()[:24] + "!9")
        factor = enrol((await admin.issue_credential_reset(owner.ref)).reveal(), owner_password, second_factor=True)
        check("an owner signs in with a password and a second factor", login("life-owner", owner_password, factor)[0] is not None)

        # --- lifetimes: 5 minutes to 1 hour, never clamped --------------------------------------------
        for seconds in (299, 3601):
            try:
                await admin.issue_credential_reset(child.ref, ttl=timedelta(seconds=seconds))
                check("a lifetime of %d s is refused" % seconds, False)
            except InvalidResetTtl:
                check("a lifetime of %d s is refused" % seconds, True)
        short = await admin.issue_credential_reset(child.ref, ttl=timedelta(minutes=5))
        check("5 minutes is accepted and exact", 295 <= (short.expires_at - __import__("datetime").datetime.now(short.expires_at.tzinfo)).total_seconds() <= 301)

        # --- a person with two role groups, or none: refused until repaired -----------------------------
        adult_uuid, child_uuid = groups.roles[Role.ADULT], groups.roles[Role.CHILD]
        console.post("/v1/group/%s/_attr/member" % adult_uuid, json=["life-child"])
        refused = 0
        for call in (
            lambda: admin.get_person(child.ref),
            lambda: admin.set_role(child.ref, Role.OWNER),
            lambda: admin.set_app_access(child.ref, App.IMMICH, False),
            lambda: admin.issue_credential_reset(child.ref),
            lambda: admin.disable(child.ref),
        ):
            try:
                await call()
            except RoleInvariantError as exc:
                texts.append(str(exc) + repr(exc))
                refused += 1
        check("a person with two role groups is refused by every method", refused == 5)
        check("set_role with repair puts them right", (await admin.set_role(child.ref, Role.ADULT, repair=True)).role is Role.ADULT)
        console.request("DELETE", "/v1/group/%s/_attr/member" % adult_uuid, json=["life-child"])
        try:
            await admin.get_person(child.ref)
            check("a person with no role group is refused", False)
        except RoleInvariantError as exc:
            check("a person with no role group is refused", exc.roles == ())
        check("repair works from no role too", (await admin.set_role(child.ref, Role.CHILD, repair=True)).role is Role.CHILD)

        # --- a service account is never a person -------------------------------------------------------------
        for label, uuid in (("atlas-console", console_uuid), ("idm_admin", idm_admin_uuid)):
            from atlas.kernel.identity.admin import PersonRef
            ref = PersonRef(uuid)
            try:
                found = await admin.get_person(ref)
                gone = await admin.delete(ref)
                await admin.set_role(ref, Role.OWNER)
                check("%s is not a person" % label, False)
            except PersonNotFound:
                check("%s is not a person" % label, found is None and gone is False)

        # --- delete ---------------------------------------------------------------------------------------------
        check("delete removes the person", await admin.delete(child.ref) is True and await admin.get_person(child.ref) is None)
        check("delete again is False", await admin.delete(child.ref) is False)
        check("an old link no longer exchanges", anonymous().post("/v1/credential/_exchange_intent", json=short.reveal().split("token=")[1]).status_code != 200)
        await admin.delete(owner.ref)


asyncio.run(main())
text = log.getvalue()
check("no log line holds a token, a link or a password", all(s not in text for s in handled if len(s) >= 12))
check("no error text holds one either", all(s not in "".join(texts) for s in handled if len(s) >= 12))
print(json.dumps({"checks": checks, "secrets": [s for s in handled if len(s) >= 12], "intents": intents}))
"""  # noqa: E501

IDS_SCRIPT = (
    PRELUDE
    + """
admin = admin_client()
ids = {
    name: attrs(send(admin, "GET", "/v1/service_account/" + name)[1])["uuid"][0]
    for name in ("atlas-console", "idm_admin")
}
finish_admin()
print(json.dumps(ids))
"""
)

STRAY_SCRIPT = (
    PRELUDE
    + """
admin = admin_client()
guest = GROUPS["atlas_role_guest"]
print(send(admin, "POST", "/v1/group/%s/_attr/member" % guest, ["atlas-console"])[0], flush=True)
finish_admin()
print(json.dumps({"ok": True}))
"""
)

CHECK_SCRIPT = """
import asyncio, json
from atlas.kernel.config import load_settings
from atlas.kernel.identity.kanidm.admin import KanidmIdentityAdmin


async def main():
    async with KanidmIdentityAdmin.from_settings(load_settings()) as admin:
        return (await admin.check()).problems


print(json.dumps(asyncio.run(main())))
"""


def exec_in_api(stack: Stack, script: str, *args: str) -> str:
    result = stack.compose("exec", "-T", "atlas-api", "python", "-c", script, *args, timeout=300)
    return result.stdout.strip().splitlines()[-1]


def test_the_kanidm_adapter_runs_a_whole_lifecycle_against_the_real_idp(
    make_stack: MakeStack,
) -> None:
    stack = rights_stack(make_stack)
    ids = json.loads(run_script(stack, IDS_SCRIPT))

    out = json.loads(
        exec_in_api(
            stack,
            LIFECYCLE_SCRIPT.replace("__IDP_HOST__", IDP_HOST),
            ids["atlas-console"],
            ids["idm_admin"],
        )
    )

    checks = out["checks"]
    assert len(checks) >= 36, [c["label"] for c in checks]
    failed = [c["label"] for c in checks if not c["ok"]]
    assert failed == [], failed

    # check() reports a member of a role group that is not a person, by name and UUID.
    assert json.loads(exec_in_api(stack, CHECK_SCRIPT)) == []
    run_script(stack, STRAY_SCRIPT)
    problems = json.loads(exec_in_api(stack, CHECK_SCRIPT))
    assert problems == [
        "the group atlas_role_guest has a member that is not a person: atlas-console "
        f"({ids['atlas-console']})"
    ]

    # No secret the run handled is in any service's log. The secrets are never printed: the
    # assertion messages hold only counts.
    services = (
        "atlas-proxy", "atlas-api", "atlas-migrate", "atlas-db-init", "atlas-init", "postgres",
        "atlas-idp-init", "idp", "atlas-idp-bootstrap",
    )  # fmt: skip
    logs = {service: stack.logs(service) for service in services}
    assert len(out["secrets"]) >= 8 and len(out["intents"]) >= 3
    tokens = set(out["intents"])
    for service, text in logs.items():
        # A reset token is in Kanidm's own log, and only there, and only because the person
        # opened the link: the request line holds the link's query string. That is the one
        # place Atlas cannot clean (SELF_HOSTING says so). Everything else, and every other
        # service, must be clean: the console token, passwords, sessions, whole links.
        pool = (
            [s for s in out["secrets"] if s not in tokens] if service == "idp" else out["secrets"]
        )
        leaked = [i for i, s in enumerate(pool) if s in text]
        assert leaked == [], f"{len(leaked)} secret(s) appear in the log of {service}"
