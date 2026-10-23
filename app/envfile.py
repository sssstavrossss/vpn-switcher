"""Read and update a Docker Compose .env file.

Compose treats the file as KEY=value lines. Lines starting with # are
comments. We only rewrite the location keys we were asked to change and
copy every other line through unchanged, including comments and blank
lines. The write is atomic (temp file in the same directory, then
os.replace) and the original permission bits are copied onto the new file.

os.replace is the Python equivalent of File.Replace / an atomic rename:
readers see either the old file or the new file, never a half-written one.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from app.errors import EnvEditError

# indent + optional "export " + KEY + "=" + the rest of the line.
# Commented lines do not match, because "#" is not a valid key character
# and it is not the optional export prefix.
_ASSIGNMENT = re.compile(
    r"^(?P<indent>\s*)(?P<export>export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$"
)
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Values Compose can take unquoted. Spaces and "#" need quotes.
_PLAIN = re.compile(r"[A-Za-z0-9_./:+@-]*")


def read_env(path: Path) -> dict[str, str]:
    """Return the last uncommented assignment for each key.

    Compose uses the last value when a key is repeated, so this reader
    does the same.
    """
    try:
        text = _read_text(path)
    except FileNotFoundError as exc:
        raise EnvEditError(f"Env file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise EnvEditError(f"Env file is not UTF-8: {path}") from exc

    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        found[match.group("key")] = unquote_value(match.group("value"))
    return found


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set each key in `updates`, leaving every other line untouched.

    Existing uncommented assignments are replaced in place (a repeated key
    is updated everywhere, so the file cannot disagree with itself). Keys
    that are not in the file yet are appended. A commented-out copy of the
    same key is left alone.
    """
    if not updates:
        return
    for key, value in updates.items():
        _reject_unsafe(key, value)

    if not path.is_file():
        raise EnvEditError(f"Env file not found: {path}")

    info = path.stat()
    try:
        original = _read_text(path)
    except UnicodeDecodeError as exc:
        raise EnvEditError(f"Env file is not UTF-8: {path}") from exc

    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    seen: set[str] = set()
    rewritten: list[str] = []

    for line in lines:
        body, ending = _split_ending(line)
        match = _ASSIGNMENT.match(body)
        if match is not None and match.group("key") in updates:
            key = match.group("key")
            seen.add(key)
            prefix = f"{match.group('indent')}{match.group('export') or ''}"
            rendered = prefix + key + "=" + format_env_value(updates[key])
            rewritten.append(rendered + (ending or newline))
        else:
            rewritten.append(line)

    missing = [key for key in updates if key not in seen]
    if missing:
        if rewritten and not rewritten[-1].endswith(("\n", "\r\n")):
            rewritten[-1] = rewritten[-1] + newline
        for key in missing:
            rewritten.append(key + "=" + format_env_value(updates[key]) + newline)

    _atomic_write(
        path,
        "".join(rewritten),
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
    )


def format_env_value(value: str) -> str:
    """Quote values that contain spaces or comment markers.

    Single quotes are literal in a Compose env file (no interpolation,
    no escapes), which is what we want for a city name.
    """
    if _PLAIN.fullmatch(value):
        return value
    return "'" + value + "'"


def unquote_value(raw: str) -> str:
    """Undo the quoting format_env_value writes, plus Compose's usual forms."""
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        close = value.find(quote, 1)
        if close != -1:
            inner = value[1:close]
            if quote == '"':
                inner = inner.replace(r"\"", '"').replace("\\\\", "\\")
            return inner
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _reject_unsafe(key: str, value: str) -> None:
    if not _KEY.fullmatch(key):
        raise EnvEditError(f"Refusing to write invalid env name {key!r}.")
    if any(char in value for char in ("\n", "\r", "\x00", "'")):
        # A newline would start a new env line. A single quote would break
        # the quoting we use for values that contain spaces.
        raise EnvEditError(f"Refusing to write an unsafe character in {key}.")


def _read_text(path: Path) -> str:
    """Read the file without translating line endings.

    Opening a text file normally turns CRLF into LF on the way in, so a
    rewrite would quietly change Windows line endings. newline="" leaves
    them alone. Path.read_text only grew a newline argument in Python 3.13,
    so this uses open() directly.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _split_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def _atomic_write(path: Path, text: str, *, mode: int, uid: int, gid: int) -> None:
    """Write `text` and then rename it over `path`.

    The temp file sits in the same directory so the rename does not cross
    filesystems (a cross-filesystem move is a copy, and not atomic).
    Permission bits are applied before the rename, so the file the stack
    sees already has the mode of the file it replaced.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        try:
            os.chown(temporary, uid, gid)
        except PermissionError:
            # A non-root container cannot give the file back to another
            # user. The mode bits are already set; the owner is the user
            # the container runs as, which is the user who can write it.
            pass
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
