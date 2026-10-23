"""Generate PASSWORD_HASH without putting the password in shell history.

Usage, from the repo directory or inside the image:

    python -m app.hash_password

The password is read from the terminal, not from arguments, so it does
not show up in `ps` or in `~/.bash_history`. To script it, pipe the
password on stdin (still not as an argument):

    python -m app.hash_password <<'EOF'
    your-password
    EOF
"""

from __future__ import annotations

import getpass
import sys

from app.auth import hash_password


def compose_env_assignment(password_hash: str) -> str:
    """A PASSWORD_HASH line safe to paste into a Compose env file.

    Compose interpolates $NAME in an env file. A bcrypt hash is full of
    dollar signs ($2b$12$...), so each one has to be written as $$ or
    Compose deletes pieces of the hash before the app ever sees it.
    """
    return "PASSWORD_HASH=" + password_hash.replace("$", "$$")


def main() -> None:
    password = _read_password()
    try:
        hashed = hash_password(password)
    except ValueError as exc:
        sys.exit(str(exc))
    print("Add this line to the switcher's Compose .env file:")
    print()
    print(compose_env_assignment(hashed))
    print()
    print("Raw bcrypt hash (only if something else injects the environment")
    print("and will not expand dollar signs):")
    print()
    print(hashed)


def _read_password() -> str:
    if sys.stdin.isatty():
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Again: ")
        if first != second:
            sys.exit("The two entries did not match.")
        password = first
    else:
        password = sys.stdin.readline()
        if password.endswith("\n"):
            password = password[:-1]
    if password == "":
        sys.exit("Password must not be empty.")
    return password


if __name__ == "__main__":
    main()
