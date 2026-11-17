"""Compose acceptance tests for Phase 5b-3a: what the console token can and cannot do.

Run by the `compose` CI job (RUN_COMPOSE_TESTS=1) against the real Kanidm.

The console's token (ATLAS_IDP_ADMIN_TOKEN) manages the five role groups and
`immich_users`, and persons. It must not do anything else. `test_the_consoles_rights_*`
pins BOTH lists: what it can do (so a Kanidm upgrade that takes a right away fails here,
not in production) and what it must be refused (so an upgrade that widens the token's
rights fails CI). Every refused write is also checked against an idm_admin snapshot taken
before and after it: Kanidm answers some ignored writes with 200 and some refused ones
with 404, and a status alone would not catch a silent change.

The other two tests cover the upgrade from a 5b-2b install and `--verify` removing
members that are not persons.
"""

import json
from collections.abc import Callable

from tests.compose.stack import IMAGE, Stack, run
from tests.compose.test_idp_bootstrap import (
    IMMICH_URL,
    bootstrap_lines,
    run_bootstrap,
)

MakeStack = Callable[..., Stack]

SIX = [
    "atlas_role_owner",
    "atlas_role_admin",
    "atlas_role_adult",
    "atlas_role_child",
    "atlas_role_guest",
    "immich_users",
]

# Shared by the scripts below. They run in a one-off Atlas container as the idp user, on
# idp-back, with the admin socket, the public CA and the bootstrap's files. Nothing secret
# is printed: the scripts print labels, statuses and booleans.
PRELUDE = """
import asyncio, base64, hashlib, json
from pathlib import Path
import httpx
from dotenv import dotenv_values
from atlas.kernel.identity.kanidm import AdminSocket

BASE = "https://idp:8443"
CA = "/ca/ca.pem"
STATE = Path("/state")
SOCKET = AdminSocket(Path("/run/atlas-idp-admin/kanidmd.sock"))
GROUPS = json.loads((STATE / "groups.json").read_text())
SIX = __SIX__


def new_client(bearer=None):
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    return httpx.Client(base_url=BASE, verify=CA, headers=headers, timeout=20, trust_env=False)


def admin_client():
    password = asyncio.run(SOCKET.recover_account("idm_admin")).get_secret_value()
    client = new_client()
    first = {"step": {"init2": {"username": "idm_admin", "issue": "token", "privileged": True}}}
    reply = client.post("/v1/auth", json=first)
    session = {"X-KANIDM-AUTH-SESSION-ID": reply.headers["X-KANIDM-AUTH-SESSION-ID"]}
    client.post("/v1/auth", json={"step": {"begin": "password"}}, headers=session)
    cred = {"step": {"cred": {"password": password}}}
    reply = client.post("/v1/auth", json=cred, headers=session)
    client.cookies.clear()
    client.headers["Authorization"] = "Bearer " + reply.json()["state"]["success"]
    return client


def finish_admin():
    asyncio.run(SOCKET.disable_account("idm_admin"))


def send(client, method, path, body=None):
    if body is None:
        reply = client.request(method, path)
    else:
        reply = client.request(method, path, json=body)
    try:
        data = reply.json()
    except ValueError:
        data = reply.text
    return reply.status_code, data


def attrs(entry):
    return (entry or {}).get("attrs", {}) if isinstance(entry, dict) else {}
""".replace("__SIX__", json.dumps(SIX))


RIGHTS_SCRIPT = (
    PRELUDE
    + """
console_token = dotenv_values(STATE / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"]
console = new_client(console_token)
admin = admin_client()
results = {"allowed": [], "refused": [], "structure": {}}


# --- what idm_admin must not see change --------------------------------------------

WATCH = {
    "group": ["idm_admins", "system_admins", "idm_people_admins", "idm_group_admins",
              "idm_all_persons", "atlas_users", "rights-ordinary", *SIX],
    "service_account": ["idm_admin", "atlas-console"],
    "oauth2": ["atlas", "immich"],
}
KEEP = ("member", "credential_type_minimum", "authsession_expiry", "account_expire",
        "displayname", "oauth2_rs_origin", "oauth2_rs_scope_map", "entry_managed_by",
        "name", "uuid", "description")


def snapshot():
    snap = {}
    for kind, names in WATCH.items():
        for name in names:
            status, entry = send(admin, "GET", "/v1/%s/%s" % (kind, name))
            have = attrs(entry)
            snap[kind + "/" + name] = (
                {k: sorted(have[k]) for k in KEEP if k in have} if entry else None
            )
            if entry and kind == "service_account":
                tokens = send(admin, "GET", "/v1/service_account/%s/_api_token" % name)[1]
                snap[kind + "/" + name]["tokens"] = len(tokens or [])
            if entry and kind == "oauth2":
                secret = send(admin, "GET", "/v1/oauth2/%s/_basic_secret" % name)[1] or ""
                snap[kind + "/" + name]["secret"] = hashlib.sha256(secret.encode()).hexdigest()
    return snap


def changed(before, after):
    return sorted(k for k in after if before.get(k) != after[k])


# --- setup as idm_admin: an ordinary group and the group SPNs ----------------------

send(admin, "POST", "/v1/group", {"attrs": {"name": ["rights-ordinary"]}})
spn = {}
for name in SIX:
    spn[name] = attrs(send(admin, "GET", "/v1/group/" + GROUPS[name])[1])["spn"][0]
console_uuid = attrs(send(admin, "GET", "/v1/service_account/atlas-console")[1])["uuid"][0]


# --- A. what the console must be able to do ----------------------------------------

def allowed(label, ok, status=None):
    results["allowed"].append({"label": label, "ok": bool(ok), "status": status})


def memberof(person):
    entry = send(console, "GET", "/v1/person/" + person)[1]
    return attrs(entry).get("directmemberof", [])


status, _ = send(console, "POST", "/v1/person", {"attrs": {"name": ["rights-person"], "displayname": ["Rights Person"]}})
allowed("create a person", status == 200, status)
status, entry = send(console, "GET", "/v1/person/rights-person")
person_uuid = attrs(entry).get("uuid", [""])[0]
allowed("read a person", status == 200 and bool(person_uuid), status)

for name in SIX:
    status, _ = send(console, "POST", "/v1/group/%s/_attr/member" % GROUPS[name], ["rights-person"])
    joined = spn[name] in memberof("rights-person")
    allowed("add a person to %s" % name, status == 200 and joined, status)
    status, _ = send(console, "DELETE", "/v1/group/%s/_attr/member" % GROUPS[name], ["rights-person"])
    left = spn[name] not in memberof("rights-person")
    allowed("remove a person from %s" % name, status == 200 and left, status)

status, _ = send(console, "POST", "/v1/group/%s/_attr/member" % GROUPS["atlas_role_guest"], [person_uuid])
allowed("add a person by UUID", status == 200 and spn["atlas_role_guest"] in memberof("rights-person"), status)
send(console, "DELETE", "/v1/group/%s/_attr/member" % GROUPS["atlas_role_guest"], [person_uuid])

send(console, "POST", "/v1/group/%s/_attr/member" % GROUPS["atlas_role_child"], ["rights-person"])
send(console, "POST", "/v1/group/%s/_attr/member" % GROUPS["atlas_role_adult"], ["rights-person"])
send(console, "DELETE", "/v1/group/%s/_attr/member" % GROUPS["atlas_role_child"], ["rights-person"])
roles = [g for g in memberof("rights-person") if g.startswith("atlas_role_")]
allowed("move a person between role groups", roles == [spn["atlas_role_adult"]], None)

status, _ = send(console, "PUT", "/v1/person/rights-person/_attr/account_expire", ["1970-01-01T00:00:00Z"])
expire = attrs(send(console, "GET", "/v1/person/rights-person")[1]).get("account_expire")
allowed("set account_expire", status == 200 and bool(expire), status)
status, _ = send(console, "DELETE", "/v1/person/rights-person/_attr/account_expire")
expire = attrs(send(console, "GET", "/v1/person/rights-person")[1]).get("account_expire")
allowed("clear account_expire", status == 200 and not expire, status)

status, intent = send(console, "GET", "/v1/person/rights-person/_credential/_update_intent/300")
allowed("issue a reset intent", status == 200 and isinstance(intent, dict) and bool(intent.get("token")), status)
del intent

status, _ = send(console, "DELETE", "/v1/person/rights-person/_attr/primary_credential")
allowed("purge a person's primary credential", status == 200, status)

for name in SIX:
    status, entry = send(console, "GET", "/v1/group/" + GROUPS[name])
    allowed("read group %s by UUID" % name, status == 200 and entry is not None, status)
status, listing = send(console, "GET", "/v1/group")
names = sorted(attrs(e).get("name", [""])[0] for e in listing) if isinstance(listing, list) else None
allowed("list shows exactly the six groups", names == sorted(SIX), status)

status, _ = send(console, "DELETE", "/v1/person/rights-person")
allowed("delete a person", status == 200, status)
allowed("a deleted person reads as 200 null", send(console, "GET", "/v1/person/rights-person") == (200, None))
status, _ = send(console, "DELETE", "/v1/person/rights-person")
allowed("deleting again is a 404", status == 404, status)


# --- B. what it must be refused (and must leave unchanged) --------------------------

send(console, "POST", "/v1/person", {"attrs": {"name": ["rights-other"], "displayname": ["Rights Other"]}})
OTHER = "rights-other"
OWNER, CHILD = GROUPS["atlas_role_owner"], GROUPS["atlas_role_child"]
BASIC = "_basic" + "_secret"
TOKEN_BODY = {"label": "rights", "expiry": None, "read_write": True, "compact": False}
IMPORTED = "{SSHA512}" + base64.b64encode(hashlib.sha512(b"x" + b"saltsalt").digest() + b"saltsalt").decode()


def member_add(group, who=OTHER):
    return ("POST", "/v1/group/%s/_attr/member" % group, [who])


REFUSED = [
    # privileged groups: not even visible to the console
    *[("add a person to " + g,) + member_add(g)
      for g in ("idm_admins", "system_admins", "idm_people_admins", "idm_group_admins",
                "idm_oauth2_admins", "idm_account_policy_admins", "idm_service_account_admins",
                "idm_high_privilege", "idm_access_control_admins")],
    ("add atlas-console to idm_admins",) + member_add("idm_admins", "atlas-console"),
    ("add atlas-console to idm_group_admins",) + member_add("idm_group_admins", "atlas-console"),
    # groups it does not manage
    ("add a person to idm_all_persons",) + member_add("idm_all_persons"),
    ("add a person to atlas_users",) + member_add(GROUPS["atlas_users"]),
    ("add a person to an ordinary group",) + member_add("rights-ordinary"),
    ("remove a member of atlas_users", "DELETE", "/v1/group/atlas_users/_attr/member", ["atlas_role_owner"]),
    # credential policy of the role groups and of idm_all_persons
    *[("%s: %s" % (g, attr), "PUT", "/v1/group/%s/_attr/%s" % (GROUPS[g], attr), [value])
      for g in ("atlas_role_owner", "atlas_role_child")
      for attr, value in (("credential_type_minimum", "any"), ("authsession_expiry", "1"),
                          ("privilege_expiry", "86400"), ("auth_password_minimum_length", "1"))],
    ("idm_all_persons: credential_type_minimum", "PUT", "/v1/group/idm_all_persons/_attr/credential_type_minimum", ["mfa"]),
    ("idm_all_persons: authsession_expiry", "PUT", "/v1/group/idm_all_persons/_attr/authsession_expiry", ["1"]),
    # handing a managed group to someone else, or taking the management
    ("role owner -> managed by idm_admins", "PUT", "/v1/group/%s/_attr/entry_managed_by" % OWNER, ["idm_admins"]),
    ("role owner -> managed by an ordinary group", "PUT", "/v1/group/%s/_attr/entry_managed_by" % OWNER, ["rights-ordinary"]),
    ("role owner: purge entry_managed_by", "DELETE", "/v1/group/%s/_attr/entry_managed_by" % OWNER, None),
    ("immich_users -> managed by idm_admins", "PUT", "/v1/group/%s/_attr/entry_managed_by" % GROUPS["immich_users"], ["idm_admins"]),
    ("an ordinary group -> managed by the console", "PUT", "/v1/group/rights-ordinary/_attr/entry_managed_by", ["atlas-console"]),
    ("idm_all_persons -> managed by the console", "PUT", "/v1/group/idm_all_persons/_attr/entry_managed_by", ["atlas-console"]),
    ("atlas-console -> managed by someone else", "PUT", "/v1/service_account/atlas-console/_attr/entry_managed_by", ["atlas_role_owner"]),
    # making, renaming and deleting groups
    ("create a group", "POST", "/v1/group", {"attrs": {"name": ["rights-new"]}}),
    ("create a group by raw create", "POST", "/v1/raw/create", {"entries": [{"attrs": {"class": ["group"], "name": ["rights-raw"]}}]}),
    ("rename a role group", "PUT", "/v1/group/%s/_attr/name" % CHILD, ["renamed"]),
    ("raw modify on a privileged group", "POST", "/v1/raw/modify", {"filter": {"eq": ["name", "idm_admins"]}, "modlist": {"mods": [{"present": ["member", OTHER]}]}}),
    ("raw modify on a role group's policy", "POST", "/v1/raw/modify", {"filter": {"eq": ["name", "atlas_role_owner"]}, "modlist": {"mods": [{"purged": "credential_type_minimum"}]}}),
    # OAuth2 clients: not even visible
    ("atlas client: PATCH", "PATCH", "/v1/oauth2/atlas", {"attrs": {"displayname": ["changed"]}}),
    ("atlas client: add a redirect URL", "POST", "/v1/oauth2/atlas/_attr/oauth2_rs_origin", ["https://evil.test.invalid/cb"]),
    ("atlas client: set the secret", "PUT", "/v1/oauth2/atlas/_attr/oauth2_rs" + BASIC, ["v"]),
    ("atlas client: reset the secret", "DELETE", "/v1/oauth2/atlas/_attr/oauth2_rs" + BASIC, None),
    ("atlas client: scope map", "POST", "/v1/oauth2/atlas/_scopemap/%s" % CHILD, ["openid"]),
    ("immich client: PATCH", "PATCH", "/v1/oauth2/immich", {"attrs": {"displayname": ["changed"]}}),
    ("immich client: add a redirect URL", "POST", "/v1/oauth2/immich/_attr/oauth2_rs_origin", ["https://evil.test.invalid/cb"]),
    ("immich client: reset the secret", "DELETE", "/v1/oauth2/immich/_attr/oauth2_rs" + BASIC, None),
    ("create an OAuth2 client", "POST", "/v1/oauth2/_basic", {"attrs": {"name": ["rights-client"], "displayname": ["x"], "oauth2_rs_origin_landing": ["https://evil.test.invalid/"]}}),
    ("delete the atlas client", "DELETE", "/v1/oauth2/atlas", None),
    # tokens and accounts
    ("a token for atlas-console", "POST", "/v1/service_account/atlas-console/_api_token", TOKEN_BODY),
    ("a token for idm_admin", "POST", "/v1/service_account/idm_admin/_api_token", TOKEN_BODY),
    ("create a service account", "POST", "/v1/service_account", {"attrs": {"name": ["rights-sa"], "displayname": ["x"], "entry_managed_by": ["idm_admins"]}}),
    ("idm_admin: account_expire", "PUT", "/v1/service_account/idm_admin/_attr/account_expire", ["1970-01-01T00:00:00Z"]),
    ("idm_admin: rename", "PUT", "/v1/service_account/idm_admin/_attr/name", ["idm_admin2"]),
    ("idm_admin: delete", "DELETE", "/v1/service_account/idm_admin", None),
    ("atlas-console: account_expire", "PUT", "/v1/service_account/atlas-console/_attr/account_expire", ["1970-01-01T00:00:00Z"]),
    ("atlas-console: delete", "DELETE", "/v1/service_account/atlas-console", None),
    # setting a person's password directly
    ("set a password by import", "POST", "/v1/person/%s/_attr/password_import" % OTHER, [IMPORTED]),
    # deleting the groups Atlas depends on (last: nothing is left to test if one worked)
    ("delete a role group", "DELETE", "/v1/group/%s" % CHILD, None),
    ("delete immich_users", "DELETE", "/v1/group/%s" % GROUPS["immich_users"], None),
    ("delete atlas_users", "DELETE", "/v1/group/%s" % GROUPS["atlas_users"], None),
]

REFUSED_STATUSES = (403, 404, 405)
state = snapshot()
for label, method, path, body in REFUSED:
    status, _ = send(console, method, path, body)
    after = snapshot()
    results["refused"].append({
        "label": label, "status": status, "refused": status in REFUSED_STATUSES,
        "changed": changed(state, after),
    })
    state = after

# reads that must not return anything
for label, path in (
    ("read the atlas client", "/v1/oauth2/atlas"),
    ("read the immich client", "/v1/oauth2/immich"),
    ("read the atlas client secret", "/v1/oauth2/atlas/" + BASIC),
    ("read the immich client secret", "/v1/oauth2/immich/" + BASIC),
    ("read idm_admins", "/v1/group/idm_admins"),
    ("read idm_group_admins", "/v1/group/idm_group_admins"),
    ("read atlas_users", "/v1/group/atlas_users"),
    ("read idm_admin", "/v1/service_account/idm_admin"),
    ("read rights-ordinary", "/v1/group/rights-ordinary"),
):
    status, data = send(console, "GET", path)
    results["refused"].append({"label": label, "status": status, "refused": status in (200, 403, 404) and not data, "changed": []})

# --- C. the structure the bootstrap left --------------------------------------------

people = attrs(send(admin, "GET", "/v1/group/idm_people_admins")[1]).get("member", [])
group_admins = attrs(send(admin, "GET", "/v1/group/idm_group_admins")[1]).get("member", [])
results["structure"] = {
    "in_people_admins": any(m.startswith("atlas-console@") for m in people),
    "in_group_admins": any(m.startswith("atlas-console@") for m in group_admins),
    "managed": {n: attrs(send(admin, "GET", "/v1/group/" + GROUPS[n])[1]).get("entry_managed_by") == [console_uuid] for n in SIX},
    "others_not_managed_by_console": {
        n: attrs(send(admin, "GET", "/v1/group/" + n)[1]).get("entry_managed_by") != [console_uuid]
        for n in ("atlas_users", "idm_all_persons", "rights-ordinary")
    },
    # Kanidm does not show a service account's memberof, so ask the groups: the console is
    # in none of the other groups that give rights over groups, accounts and clients.
    "in_other_admin_groups": sorted(
        g for g in ("idm_admins", "system_admins", "idm_oauth2_admins",
                    "idm_account_policy_admins", "idm_service_account_admins",
                    "idm_high_privilege", "idm_access_control_admins")
        if any(m.startswith("atlas-console@") for m in attrs(send(admin, "GET", "/v1/group/" + g)[1]).get("member", []))
    ),
}

# cleanup, by explicit name
send(admin, "DELETE", "/v1/person/" + OTHER)
send(admin, "DELETE", "/v1/group/rights-ordinary")
finish_admin()
print(json.dumps(results))
"""  # noqa: E501
)


def run_script(stack: Stack, script: str, *, user: str = "10002:10002", timeout: int = 400) -> str:
    """Run a Python script in a one-off atlas container on idp-back; return its last line."""
    result = run(
        [
            "docker", "run", "--rm", "--user", user,
            "--network", f"{stack.project}_idp-back",
            "-v", f"{stack.idp_admin_volume}:/run/atlas-idp-admin:ro",
            "-v", f"{stack.secrets_dir}/idp-trust:/ca:ro",
            "-v", f"{stack.secrets_dir}/atlas-idp:/state:ro",
            "--entrypoint", "python", IMAGE, "-c", script,
        ],
        check=False,
        timeout=timeout,
    )  # fmt: skip
    if result.returncode != 0:
        stack.fail(f"the script exited {result.returncode}\n{result.stderr[-3000:]}")
    return result.stdout.strip().splitlines()[-1]


def rights_stack(make_stack: MakeStack) -> Stack:
    stack = make_stack(idp=True, extra_env={"ATLAS_IMMICH_PUBLIC_URL": IMMICH_URL})
    stack.up()
    stack.wait_healthy()
    return stack


# --- the rights pin ------------------------------------------------------------------


def test_the_consoles_rights_are_pinned_both_ways(make_stack: MakeStack) -> None:
    stack = rights_stack(make_stack)
    results = json.loads(run_script(stack, RIGHTS_SCRIPT))

    # What it must be able to do.
    allowed = {item["label"]: item for item in results["allowed"]}
    assert len(allowed) >= 30, sorted(allowed)
    broken = [(i["label"], i["status"]) for i in results["allowed"] if not i["ok"]]
    assert broken == [], f"the console lost a right it needs: {broken}"

    # What it must be refused: by status, and by an idm_admin snapshot before and after.
    refused = results["refused"]
    assert len(refused) >= 60, len(refused)
    accepted = [(i["label"], i["status"]) for i in refused if not i["refused"]]
    assert accepted == [], f"the console can do what it must not: {accepted}"
    changed = [(i["label"], i["changed"]) for i in refused if i["changed"]]
    assert changed == [], f"a refused write changed Kanidm: {changed}"

    # The arrangement that makes it so.
    structure = results["structure"]
    assert structure["in_people_admins"] is True
    assert structure["in_group_admins"] is False
    assert structure["managed"] == dict.fromkeys(SIX, True)
    assert set(structure["others_not_managed_by_console"].values()) == {True}
    assert structure["in_other_admin_groups"] == []


# --- the upgrade from a 5b-2b install ------------------------------------------------

PRE_3A_SCRIPT = (
    PRELUDE
    + """
admin = admin_client()
console_uuid = attrs(send(admin, "GET", "/v1/service_account/atlas-console")[1])["uuid"][0]
# What 5b-2b left: the console in idm_group_admins and no manager on the six groups.
print(send(admin, "POST", "/v1/group/idm_group_admins/_attr/member", ["atlas-console"])[0], flush=True)
for name in SIX:
    send(admin, "DELETE", "/v1/group/%s/_attr/entry_managed_by" % GROUPS[name])
finish_admin()
print(json.dumps({"console_uuid": console_uuid}))
"""  # noqa: E501
)

STATE_SCRIPT = (
    PRELUDE
    + """
admin = admin_client()
console_uuid = attrs(send(admin, "GET", "/v1/service_account/atlas-console")[1])["uuid"][0]
members = attrs(send(admin, "GET", "/v1/group/idm_group_admins")[1]).get("member", [])
out = {
    "in_group_admins": any(m.startswith("atlas-console@") for m in members),
    "managed": {
        n: attrs(send(admin, "GET", "/v1/group/" + GROUPS[n])[1]).get("entry_managed_by") == [console_uuid]
        for n in SIX
    },
}
finish_admin()
print(json.dumps(out))
"""  # noqa: E501
)

# Rewrites the completion record to version 1, as a 5b-2b install has it (root: the
# secrets are owned by the idp user).
RECORD_V1_SCRIPT = """
import json
path = "/s/atlas-idp/bootstrap.json"
record = json.load(open(path))
record["version"] = 1
json.dump(record, open(path, "w"))
print(json.dumps({"version": record["version"], "complete": record["complete"]}))
"""


def as_root_writable(stack: Stack, script: str) -> object:
    result = run(
        [
            "docker", "run", "--rm", "--user", "0:0", "--network", "none",
            "-v", f"{stack.secrets_dir}:/s",
            "--entrypoint", "python", IMAGE, "-c", script,
        ],
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        stack.fail(f"the root script exited {result.returncode}\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_5b_2b_install_is_upgraded_by_the_next_bootstrap_run(make_stack: MakeStack) -> None:
    stack = rights_stack(make_stack)
    assert json.loads(run_script(stack, STATE_SCRIPT))["in_group_admins"] is False

    # Make it look like a 5b-2b install: version 1, the console in idm_group_admins, no managers.
    run_script(stack, PRE_3A_SCRIPT)
    assert as_root_writable(stack, RECORD_V1_SCRIPT) == {"version": 1, "complete": True}
    before = json.loads(run_script(stack, STATE_SCRIPT))
    assert before["in_group_admins"] is True and not any(before["managed"].values())

    status, lines = run_bootstrap(stack)  # what `docker compose up -d` does on the next start

    assert status == 0, lines
    assert lines[0].startswith("atlas-admin idp-bootstrap: full run: the bootstrap changed")
    assert any("groups managed by atlas-console: " in ln for ln in lines), lines
    assert "atlas-admin idp-bootstrap: group idm_group_admins member atlas-console removed" in lines
    after = json.loads(run_script(stack, STATE_SCRIPT))
    assert after["in_group_admins"] is False
    assert after["managed"] == dict.fromkeys(SIX, True)
    # The order, as the lines of the real run show it: managers first, the removal after.
    managed_at = next(i for i, ln in enumerate(lines) if "groups managed by atlas-console" in ln)
    removed_at = next(i for i, ln in enumerate(lines) if "member atlas-console removed" in ln)
    assert managed_at < removed_at
    assert lines[-1] == "atlas-admin idp-bootstrap: done"

    # And the run after it has nothing left to do.
    status, lines = run_bootstrap(stack)
    assert status == 0 and "up to date: nothing to change" in "\n".join(lines)
    stack.wait_healthy()


# --- --verify removes members that are not persons ----------------------------------

STRAY_SCRIPT = (
    PRELUDE
    + """
admin = admin_client()
console = new_client(dotenv_values(STATE / "idp.env")["ATLAS_IDP_ADMIN_TOKEN"])
print(send(console, "POST", "/v1/person", {"attrs": {"name": ["stray-person"], "displayname": ["Stray Person"]}})[0], flush=True)
guest = GROUPS["atlas_role_guest"]
# A person (stays), a service account and a group (go).
print(send(admin, "POST", "/v1/group/%s/_attr/member" % guest, ["stray-person", "atlas-console", "atlas_role_child"])[0], flush=True)
members = attrs(send(admin, "GET", "/v1/group/" + guest)[1]).get("member", [])
uuids = {
    "atlas-console": attrs(send(admin, "GET", "/v1/service_account/atlas-console")[1])["uuid"][0],
    "atlas_role_child": GROUPS["atlas_role_child"],
}
finish_admin()
print(json.dumps({"members": sorted(m.partition("@")[0] for m in members), "uuids": uuids}))
"""  # noqa: E501
)

AFTER_STRAY_SCRIPT = (
    PRELUDE
    + """
admin = admin_client()
members = attrs(send(admin, "GET", "/v1/group/" + GROUPS["atlas_role_guest"])[1]).get("member", [])
users = attrs(send(admin, "GET", "/v1/group/" + GROUPS["atlas_users"])[1]).get("member", [])
send(admin, "DELETE", "/v1/person/stray-person")
finish_admin()
print(json.dumps({
    "guest": sorted(m.partition("@")[0] for m in members),
    "atlas_users": sorted(m.partition("@")[0] for m in users),
}))
"""
)


def test_verify_removes_members_of_the_six_groups_that_are_not_persons(
    make_stack: MakeStack,
) -> None:
    stack = rights_stack(make_stack)
    before = json.loads(run_script(stack, STRAY_SCRIPT))
    assert before["members"] == ["atlas-console", "atlas_role_child", "stray-person"]

    status, lines = run_bootstrap(stack)  # without --verify nothing is removed
    assert status == 0 and "not a person" not in "\n".join(lines)

    status, lines = run_bootstrap(stack, "--verify")

    assert status == 0, lines
    prefix = "atlas-admin idp-bootstrap: group atlas_role_guest member "
    uuids = before["uuids"]
    assert f"{prefix}atlas-console {uuids['atlas-console']} removed (not a person)" in lines
    assert f"{prefix}atlas_role_child {uuids['atlas_role_child']} removed (not a person)" in lines
    after = json.loads(run_script(stack, AFTER_STRAY_SCRIPT))
    assert after["guest"] == ["stray-person"]  # the person stays
    # atlas_users holds the five role groups by design and is not one of the six.
    assert after["atlas_users"] == sorted(
        [
            "atlas_role_owner",
            "atlas_role_admin",
            "atlas_role_adult",
            "atlas_role_child",
            "atlas_role_guest",
        ]
    )
    # Names and UUIDs only: nothing else in what the run printed.
    assert bootstrap_lines(stack)
