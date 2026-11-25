"""Unit tests for iam.oidc's pure helpers."""

from iam.oidc import (
    decode_login_cookie,
    encode_login_cookie,
    new_login_request,
    pkce_challenge,
)


def test_pkce_challenge_matches_rfc7636_appendix_b_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_login_cookie_round_trips() -> None:
    login = new_login_request()
    assert decode_login_cookie(encode_login_cookie(login)) == login


def test_each_login_request_is_unique() -> None:
    assert new_login_request() != new_login_request()


def test_malformed_login_cookie_decodes_to_none() -> None:
    for value in (None, "", "a.b", "a.b.c.d", "a..c"):
        assert decode_login_cookie(value) is None
