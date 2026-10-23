"""Password check, CSRF comparison, and the login rate limit.

No HTTP types live here on purpose. The CLI (`python -m app.hash_password`)
and the tests can call these functions without starting the web server.
In ASP.NET terms this is the auth helper, not the middleware.
"""

from __future__ import annotations

import secrets
import time

import bcrypt


def hash_password(password: str) -> str:
    """Return a bcrypt hash string. bcrypt stores the salt inside the hash."""
    if password == "":
        raise ValueError("Password must not be empty.")
    # bcrypt only uses the first 72 bytes. Reject longer passwords so a
    # user is not given a false sense that the extra characters matter.
    if len(password.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 bytes (bcrypt's limit).")
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def is_bcrypt_hash(value: str) -> bool:
    """True for a normal bcrypt hash ($2a$, $2b$, or $2y$)."""
    return value.startswith(("$2a$", "$2b$", "$2y$")) and len(value) >= 50


def verify_password(password: str, password_hash: str) -> bool:
    """True when `password` matches the stored bcrypt hash.

    A malformed hash returns False instead of raising, so a bad
    configuration cannot crash the login request. checkpw is deliberately
    slow; that is what makes guessing expensive.
    """
    if password == "" or not is_bcrypt_hash(password_hash):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def new_csrf_token() -> str:
    """Random token stored in the session and echoed back by the form."""
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str | None, provided: str | None) -> bool:
    """Constant-time comparison. Missing tokens never match.

    compare_digest raises if the lengths differ on some Python versions
    only when the types differ; different lengths are fine for strings.
    We still reject non-strings so a bad caller fails closed.
    """
    if not expected or not provided:
        return False
    if not isinstance(expected, str) or not isinstance(provided, str):
        return False
    return secrets.compare_digest(expected, provided)


class LoginLimiter:
    """Count failed logins per client address, in memory.

    This resets when the process restarts, and it is not shared between
    processes. Run a single server process (the container command does).
    A successful login clears the counter for that address.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}

    def too_many(self, client: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        recent = self._recent(client, moment)
        return len(recent) >= self.max_attempts

    def record_failure(self, client: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        recent = self._recent(client, moment)
        recent.append(moment)
        self._failures[client] = recent

    def reset(self, client: str) -> None:
        self._failures.pop(client, None)

    def _recent(self, client: str, now: float) -> list[float]:
        window = self.window_seconds
        return [stamp for stamp in self._failures.get(client, []) if now - stamp < window]
