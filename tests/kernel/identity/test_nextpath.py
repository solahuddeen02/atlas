"""`next` of a login is a path inside Atlas (ADR-003 §4 and Verification, H1)."""

import pytest

from atlas.kernel.identity.nextpath import MAX_NEXT_LENGTH, safe_next_path


@pytest.mark.parametrize(
    "value",
    [
        "/",
        "/admin",
        "/admin/users?page=2",
        "/files/a%20b",
        "/a//b",  # a double slash inside a path is a path
        "/search?q=https://example.net",
        "/p#section",
        "/%252F/not-decoded-twice",  # %25 decodes once to %2F: still only a path
    ],
)
def test_a_path_inside_atlas_is_kept_as_it_is(value: str) -> None:
    assert safe_next_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "//host.example.net",  # a scheme-relative URL
        "//host.example.net/path",
        "/\\host.example.net",  # browsers treat /\ like //
        "/\\\\host.example.net",
        "https://host.example.net/",  # a scheme
        "http://host.example.net",
        "javascript:alert(1)",
        "data:text/html,x",
        "host.example.net/path",  # no leading slash
        "path/without/slash",
        "",
        "?x=1",
        "#frag",
        "/a\\b",  # a backslash anywhere
        "/ok\\..\\evil",
        "/line\nbreak",  # control characters
        "/tab\there",
        "/nul\x00byte",
        "/\x7fdel",
        "/c1\x85control",
        "/\r\nSet-Cookie: x=1",
    ],
)
def test_anything_else_becomes_the_root(value: str) -> None:
    assert safe_next_path(value) == "/"


@pytest.mark.parametrize(
    "value",
    [
        "/%2F/host.example.net",  # decodes to //host
        "/%2f/host.example.net",
        "/%5Chost.example.net",  # decodes to /\host
        "/%5chost.example.net",
        "/ok%5Cevil",  # a backslash after decoding
        "/%0d%0aSet-Cookie:%20x=1",  # control characters after decoding
        "/%00",
        "/%09tab",
        "/%7f",
    ],
)
def test_it_is_decoded_once_and_judged_again(value: str) -> None:
    assert safe_next_path(value) == "/"


@pytest.mark.parametrize("value", [None, 5, b"/x", ["/x"], {"a": 1}])
def test_not_text_becomes_the_root(value: object) -> None:
    assert safe_next_path(value) == "/"


def test_the_length_is_bounded() -> None:
    longest = "/" + "a" * (MAX_NEXT_LENGTH - 1)
    assert safe_next_path(longest) == longest
    assert safe_next_path(longest + "a") == "/"


def test_the_default_is_the_callers_to_choose() -> None:
    assert safe_next_path("//evil", default="/home") == "/home"
    assert safe_next_path("/ok", default="/home") == "/ok"
