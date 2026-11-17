"""Compose acceptance test for Phase 5b-4: does a group's `authsession_expiry` bound the
Kanidm browser session? (ADR-001, Gap 5.)

Run by the `compose` CI job (RUN_COMPOSE_TESTS=1) against the real Kanidm. The script runs in
a one-off Atlas container on idp-back, drives Kanidm the way a browser does (the session
cookie, then the server-rendered `/ui/oauth2` authorisation page), and prints labels and
booleans only.

What it pins, with a short value so the wait stays about a minute:
- which value applies: a person in a 60 s group and a 300 s group gets 60 s, a person in
  the 300 s group alone gets 300 s, and a group value above the 86 400 s default changes
  nothing;
- before the expiry the authorisation request for the `atlas` client gives consent or a
  code; after it `/v1/self` is refused and the same request goes back to the login page;
- people with a longer or no value are untouched by the shorter one's expiry;
- a refresh token obtained before the expiry does not give a usable access token after it
  (the grant itself may answer 200; Kanidm 1.12.0 does once, with an access token that the
  userinfo endpoint refuses).
"""

import json
from collections.abc import Callable

from tests.compose.stack import PUBLIC_HOST, Stack
from tests.compose.test_idp_console_rights import PRELUDE, rights_stack, run_script

MakeStack = Callable[..., Stack]

SHORT, LONG, HUGE = 60, 300, 100000

SCRIPT = (
    PRELUDE
    + """
import re, secrets, time
from urllib.parse import parse_qs, urlsplit

REDIRECT = "https://__PUBLIC_HOST__/api/v1/auth/callback"
SHORT, LONG, HUGE = __SHORT__, __LONG__, __HUGE__
checks = []


def check(label, ok):
    checks.append({"label": label, "ok": bool(ok)})


def plain():
    return httpx.Client(base_url=BASE, verify=CA, timeout=20, trust_env=False)


admin = admin_client()
anonymous = plain()


def new_group(name, seconds):
    send(admin, "POST", "/v1/group", {"attrs": {"name": [name]}})
    send(admin, "POST", "/v1/group/%s/_attr/class" % name, ["account_policy"])
    status, _ = send(admin, "PUT", "/v1/group/%s/_attr/authsession_expiry" % name, [str(seconds)])
    assert status == 200, status


def new_person(name, groups):
    send(admin, "POST", "/v1/person", {"attrs": {"name": [name], "displayname": [name]}})
    for group in [GROUPS["atlas_role_child"], *groups]:
        send(admin, "POST", "/v1/group/%s/_attr/member" % group, [name])
    intent = send(admin, "GET", "/v1/person/%s/_credential/_update_intent/300" % name)[1]["token"]
    session = anonymous.post("/v1/credential/_exchange_intent", json=intent).json()[0]
    password = "Zq" + secrets.token_urlsafe(18) + "!9"
    done = anonymous.post("/v1/credential/_update", json=[{"password": password}, session])
    assert done.status_code == 200, done.status_code
    assert anonymous.post("/v1/credential/_commit", json=session).status_code == 200
    return password


def browser_login(name, password):
    \"\"\"A sign-in that issues the session cookie, as the sign-in page does.\"\"\"
    client = plain()
    first = {"step": {"init2": {"username": name, "issue": "cookie", "privileged": False}}}
    reply = client.post("/v1/auth", json=first)
    headers = {"X-KANIDM-AUTH-SESSION-ID": reply.headers["X-KANIDM-AUTH-SESSION-ID"]}
    client.post("/v1/auth", json={"step": {"begin": "password"}}, headers=headers)
    reply = client.post("/v1/auth", json={"step": {"cred": {"password": password}}}, headers=headers)
    assert "success" in reply.json()["state"], "the sign-in was refused"
    for line in reply.headers.get_list("set-cookie"):
        pair = line.split(";", 1)[0]
        if pair.startswith("bearer=") and len(pair) > 20 and "Max-Age=0" not in line:
            return pair
    raise AssertionError("no session cookie")


def expiry_in(cookie):
    \"\"\"Seconds left on the session, read from the session token's own expiry claim.\"\"\"
    payload = cookie.split("=", 1)[1].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return claims["expiry"] - time.time()


def get(path, cookie, params=None, bearer=None):
    headers = {"Accept": "text/html"}
    if cookie:
        headers["Cookie"] = cookie
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    return httpx.get(
        BASE + path, params=params, headers=headers, verify=CA, trust_env=False,
        follow_redirects=False,
    )


def authorise(cookie, challenge):
    query = {
        "response_type": "code", "client_id": "atlas", "state": "s1", "nonce": "n1",
        "redirect_uri": REDIRECT, "scope": "openid profile groups",
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return get("/ui/oauth2", cookie, query)


def kind(reply):
    \"\"\"What a browser would be shown: consent, login, or a redirect to the client with a code.\"\"\"
    where = reply.headers.get("location", "")
    if reply.status_code in (302, 303) and where.startswith(REDIRECT) and "code=" in where:
        return "code"
    match = re.search(r"<title>(.*?)</title>", reply.text, re.S)
    return (match.group(1).strip() if match else "?").lower() if reply.status_code == 200 else "other"


def challenge():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def token(client_secret, data):
    return httpx.post(
        BASE + "/oauth2/token", data=data, auth=("atlas", client_secret), verify=CA,
        trust_env=False,
    )


def usable(client_secret, refresh):
    \"\"\"Does a refresh token still give an access token that Kanidm accepts?\"\"\"
    reply = token(client_secret, {"grant_type": "refresh_token", "refresh_token": refresh})
    if reply.status_code != 200:
        return False, reply.status_code, None
    info = httpx.get(
        BASE + "/oauth2/openid/atlas/userinfo",
        headers={"Authorization": "Bearer " + reply.json()["access_token"]},
        verify=CA, trust_env=False,
    )
    return info.status_code == 200, reply.status_code, info.status_code


new_group("expiry_short", SHORT)
new_group("expiry_long", LONG)
new_group("expiry_huge", HUGE)
who = {
    "both": ("expiry-both", ["expiry_short", "expiry_long"]),
    "long": ("expiry-long", ["expiry_long"]),
    "huge": ("expiry-huge", ["expiry_huge"]),
    "none": ("expiry-none", []),
}
cookies, signed_in = {}, {}
for key, (name, groups) in who.items():
    password = new_person(name, groups)
    cookies[key] = browser_login(name, password)
    signed_in[key] = time.monotonic()

client_secret = send(admin, "GET", "/v1/oauth2/atlas/_basic_secret")[1]
finish_admin()

# --- which value applies ------------------------------------------------------------------
left = {key: expiry_in(cookie) for key, cookie in cookies.items()}
near = lambda seconds, want: want - 15 <= seconds <= want
check("a person in a 60 s group and a 300 s group gets 60 s", near(left["both"], SHORT))
check("a person in the 300 s group alone gets 300 s", near(left["long"], LONG))
check("a group value above 86400 s changes nothing", near(left["huge"], 86400) and near(left["none"], 86400))

# --- before the expiry: the browser flow, and a refresh token -----------------------------------
refresh = {}
for key, cookie in cookies.items():
    check("%s: the session is valid" % key, get("/v1/self", cookie).status_code == 200)
    verifier, challenge_value = challenge()
    page = authorise(cookie, challenge_value)
    check("%s: the authorisation request asks for consent" % key, kind(page) == "consent required")
    found = re.search('name="consent_token" value="([^"]+)"', page.text)
    consent = httpx.post(
        BASE + "/ui/oauth2/consent", data={"consent_token": found.group(1)},
        headers={"Cookie": cookie}, verify=CA, trust_env=False, follow_redirects=False,
    )
    check("%s: consent gives a code" % key, kind(consent) == "code")
    code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
    exchanged = token(client_secret, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    })
    refresh[key] = exchanged.json().get("refresh_token")
    check("%s: the code gives a refresh token" % key, bool(refresh[key]))
    check("%s: the next request gives a code at once" % key, kind(authorise(cookie, challenge()[1])) == "code")
    check("%s: still inside its time when checked" % key, time.monotonic() - signed_in[key] < 50)

# --- after the 60 s value, and before any other --------------------------------------------------
time.sleep(max(0, max(signed_in.values()) + SHORT + 5 - time.monotonic()))
check("both: /v1/self is refused after 60 s", get("/v1/self", cookies["both"]).status_code == 401)
check("both: the authorisation request goes back to the login page", kind(authorise(cookies["both"], challenge()[1])) == "login")
for key in ("long", "huge", "none"):
    check("%s: not affected by the shorter value" % key, get("/v1/self", cookies[key]).status_code == 200)
    check("%s: still gets a code" % key, kind(authorise(cookies[key], challenge()[1])) == "code")

# A refresh token from before the expiry: the control must work, the expired one must not.
works, _, _ = usable(client_secret, refresh["none"])
check("control: a refresh token of an unexpired session gives a usable access token", works)
works, status, info = usable(client_secret, refresh["both"])
check("a refresh token from before the expiry gives no usable access token", not works)
print(json.dumps({"checks": checks, "refresh_after_expiry": [status, info], "left": {k: round(v) for k, v in left.items()}}))
""".replace("__PUBLIC_HOST__", PUBLIC_HOST)  # noqa: E501
    .replace("__SHORT__", str(SHORT))
    .replace("__LONG__", str(LONG))
    .replace("__HUGE__", str(HUGE))
)


def test_a_groups_auth_expiry_bounds_the_kanidm_browser_session(make_stack: MakeStack) -> None:
    stack = rights_stack(make_stack)

    out = json.loads(run_script(stack, SCRIPT, timeout=300))

    failed = [c["label"] for c in out["checks"] if not c["ok"]]
    assert failed == [], (
        f"{failed}; refresh after expiry (grant, userinfo): {out['refresh_after_expiry']}"
    )
    assert len(out["checks"]) >= 25, out["checks"]
