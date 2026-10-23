"""Tests for the password check, CSRF comparison, and login limiter."""

import pytest

from app.auth import (
    LoginLimiter,
    csrf_matches,
    hash_password,
    is_bcrypt_hash,
    new_csrf_token,
    verify_password,
)
from app.hash_password import compose_env_assignment

# Hashed once for the module. bcrypt is slow on purpose.
HASH = hash_password("correct horse")


def test_accepts_the_right_password_and_rejects_the_wrong_one() -> None:
    assert is_bcrypt_hash(HASH)
    assert verify_password("correct horse", HASH)
    assert not verify_password("wrong", HASH)


def test_rejects_empty_password_and_a_malformed_hash() -> None:
    assert not verify_password("", HASH)
    assert not verify_password("correct horse", "not-a-hash")
    assert not verify_password("correct horse", "")
    assert not is_bcrypt_hash("replace-with-output-of-hash-password")


def test_compose_env_line_escapes_dollar_signs() -> None:
    # Compose would otherwise treat $2b as a variable name and delete it.
    assert compose_env_assignment("$2b$12$abc") == "PASSWORD_HASH=$$2b$$12$$abc"


def test_hash_password_rejects_empty_and_overlong_passwords() -> None:
    with pytest.raises(ValueError):
        hash_password("")
    with pytest.raises(ValueError):
        hash_password("a" * 73)


def test_csrf_token_matches_only_itself() -> None:
    token = new_csrf_token()
    assert csrf_matches(token, token)
    assert not csrf_matches(token, token + "x")
    assert not csrf_matches(token, "")
    assert not csrf_matches(None, token)
    assert not csrf_matches(token, None)


def test_login_limiter_blocks_inside_the_window_and_then_resets() -> None:
    limiter = LoginLimiter(max_attempts=2, window_seconds=100)
    assert not limiter.too_many("10.0.0.8", now=0)
    limiter.record_failure("10.0.0.8", now=0)
    limiter.record_failure("10.0.0.8", now=1)
    assert limiter.too_many("10.0.0.8", now=2)
    # A different address is not affected.
    assert not limiter.too_many("10.0.0.9", now=2)
    # Old failures age out of the window.
    assert not limiter.too_many("10.0.0.8", now=200)
    limiter.record_failure("10.0.0.8", now=0)
    limiter.reset("10.0.0.8")
    assert not limiter.too_many("10.0.0.8", now=1)
