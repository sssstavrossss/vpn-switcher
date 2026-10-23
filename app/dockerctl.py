"""Talk to the Docker daemon through the docker CLI.

The app does not link against Docker's API. It runs the same commands a
person would run in the VPN stack's directory:

    docker compose up -d --force-recreate <vpn-service> <dependents>

Compose resolves relative bind mounts against the project directory on
the machine where the daemon runs (the host), not against a path inside
this container. The stack folder therefore has to be mounted into this
container at the same absolute path it has on the host. STACK_DIR is
that path, and it is also the current directory of the compose command.

The Docker socket is mounted in so the CLI can reach the host daemon.
Access to that socket is full control of every container on the host.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from app.config import Settings
from app.errors import DockerError

# Go templates passed to `docker inspect --format`.
_ENV_FORMAT = "{{range .Config.Env}}{{println .}}{{end}}"
_HEALTH_FORMAT = (
    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.State.Status}}"
)
_INSPECT_TIMEOUT = 30.0


def compose_base(settings: Settings) -> list[str]:
    """`docker compose` with the project directory (and optional -f) pinned."""
    command = ["docker", "compose", "--project-directory", str(settings.stack_dir)]
    if settings.compose_file is not None:
        command.extend(["-f", str(settings.compose_file)])
    return command


def compose_up_command(settings: Settings) -> list[str]:
    """The recreate command. Services come from configuration, not from the browser."""
    services = settings.recreate_services
    if not services:
        raise DockerError("No compose services are configured.")
    return compose_base(settings) + ["up", "-d", "--force-recreate", *services]


def recreate(settings: Settings) -> None:
    """Recreate the VPN service and every service that shares its network."""
    command = compose_up_command(settings)
    run_command(command, cwd=settings.stack_dir, timeout=settings.compose_timeout)


def resolve_container(settings: Settings) -> str:
    """Container name from VPN_CONTAINER, or the id compose reports for the service."""
    if settings.vpn_container:
        return settings.vpn_container
    command = compose_base(settings) + ["ps", "-q", settings.vpn_service]
    completed = run_command(command, cwd=settings.stack_dir, timeout=_INSPECT_TIMEOUT)
    ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not ids:
        raise DockerError(f"No container found for service {settings.vpn_service!r}.")
    return ids[0]


def inspect_env(container: str) -> dict[str, str]:
    """Environment variables of a running container. Values may contain '='."""
    completed = run_command(
        ["docker", "inspect", "--format", _ENV_FORMAT, container],
        cwd=None,
        timeout=_INSPECT_TIMEOUT,
    )
    env: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def inspect_health(container: str) -> tuple[str, str]:
    """Return (health, state). health is "none" when the image has no healthcheck."""
    completed = run_command(
        ["docker", "inspect", "--format", _HEALTH_FORMAT, container],
        cwd=None,
        timeout=_INSPECT_TIMEOUT,
    )
    text = completed.stdout.strip() or "none unknown"
    health, _, state = text.partition(" ")
    return health or "none", state or "unknown"


def wait_until_healthy(settings: Settings) -> None:
    """Poll until Gluetun is healthy, or until HEALTH_TIMEOUT_SECONDS elapses.

    Gluetun's image includes a healthcheck that passes once the tunnel is
    up. If that healthcheck has been removed, "running" is the best signal
    available and the exit-IP check afterwards is the real confirmation.
    """
    deadline = time.monotonic() + settings.health_timeout
    last = "no status yet"
    while True:
        try:
            container = resolve_container(settings)
            health, state = inspect_health(container)
            last = f"health={health}, state={state}"
            if health == "healthy" or (health == "none" and state == "running"):
                return
        except DockerError as exc:
            last = exc.message
        if time.monotonic() >= deadline:
            raise DockerError(
                f"Gluetun did not become healthy within {settings.health_timeout:.0f}s ({last}).",
                status_code=504,
            )
        time.sleep(settings.health_poll_seconds)


def run_command(
    command: list[str],
    *,
    cwd: Path | None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell. Arguments are a list, so they are not parsed."""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=docker_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerError(
            f"Timed out after {timeout:.0f}s running: {' '.join(command)}",
            status_code=504,
        ) from exc
    except FileNotFoundError as exc:
        raise DockerError(
            "The docker command was not found. This image needs the Docker CLI and Compose plugin."
        ) from exc
    if completed.returncode != 0:
        detail = _tail(completed.stderr or completed.stdout or "docker returned a non-zero exit code")
        raise DockerError(_redact(detail))
    return completed


def docker_subprocess_env() -> dict[str, str]:
    """Environment handed to docker compose.

    Compose lets the process environment override the project's .env file.
    This process holds PASSWORD_HASH and SESSION_SECRET, and it must not
    leak those into the VPN stack or override the location lines just
    written. Only the variables Docker itself needs are kept. COMPOSE_*
    is passed through so a custom COMPOSE_PROJECT_NAME still works.
    """
    kept: dict[str, str] = {}
    for key in ("PATH", "HOME", "USER", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH"):
        value = os.environ.get(key)
        if value:
            kept[key] = value
    for key, value in os.environ.items():
        if key.startswith("COMPOSE_"):
            kept[key] = value
    return kept


def _redact(text: str) -> str:
    """Drop lines that look like credentials before the text reaches the browser.

    Compose sometimes echoes a service's environment when a command fails.
    Those lines can contain a WireGuard private key. The person using the
    app does not need that in the page, or in a screenshot of it.
    """
    secret = re.compile(r"(?i)(wireguard|private[_ ]key|password|secret|token|credential|api[_ ]key)")
    lines = ["[line redacted]" if secret.search(line) else line for line in text.splitlines()]
    return "\n".join(lines)


def _tail(text: str, limit: int = 800) -> str:
    """Docker prints the useful error at the end of a long log."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]
