# VPN switcher

A small self-hosted web page for changing the exit location of a [Gluetun](https://github.com/qdm12/gluetun) container (AirVPN, WireGuard) from a phone browser. It runs in its own container, on a private network such as Tailscale, behind a single password.

Picking a city writes that country and city into the VPN stack's `.env` file and recreates the VPN container, plus any container that shares its network (`network_mode: service:<vpn>`). Gluetun's kill switch keeps traffic from leaking while the tunnel comes back.

## How a switch works

1. The browser sends the country and city, with a CSRF token.
2. The app checks the signed session cookie, the token, and that no other switch is already running.
3. It checks the city is in the current AirVPN list (the browser is not trusted to send an arbitrary string).
4. It rewrites two keys in the stack's `.env`, leaving every other line, comment, and the file mode alone. The write is a temp file in the same directory, then an atomic rename.
5. It runs `docker compose up -d --force-recreate <vpn service> <dependents>` in the stack directory, through the Docker socket.
6. It waits until Gluetun reports healthy.
7. It asks a public IP service through Gluetun's HTTP proxy and shows the result. If that lookup fails, the switch still stands; the page says why the lookup failed.

Only one switch runs at a time. Compose, the health wait, and the IP lookup each have a timeout, and the page shows which one ran out.

## Change this in the VPN stack

Gluetun reads `SERVER_COUNTRIES` and `SERVER_CITIES`. This app does not edit the compose file. It edits the stack's `.env`, so the compose file has to pass those values through:

```yaml
services:
  gluetun:
    environment:
      - SERVER_COUNTRIES=${VPN_SERVER_COUNTRIES:-}
      - SERVER_CITIES=${VPN_SERVER_CITIES:-}
      # Leave SERVER_REGIONS unset, or set a region that contains every
      # city you might pick. Gluetun ANDs every filter you set, so a fixed
      # region rejects cities outside it.
```

`VPN_SERVER_COUNTRIES` and `VPN_SERVER_CITIES` are the names this app writes. Both names are configurable (`COUNTRY_ENV`, `CITY_ENV`). The `:-` empty default lets the stack start before a city has been chosen.

Put the two keys in the stack's `.env` as well (any starting value is fine). Containers that use `network_mode: service:<your vpn service>` must be listed in `DEPENDENT_SERVICES`, because they lose their network when the VPN container is recreated and have to be recreated with it.

For the exit-IP line, turn on Gluetun's HTTP proxy and publish it:

```yaml
services:
  gluetun:
    environment:
      - HTTPPROXY=on
    ports:
      - "8888:8888/tcp"
```

Then set `GLUETUN_PROXY_URL` to an address this app's container can reach (see the example compose file). If the proxy is unset, switches still work and the page says the IP check was skipped.

AirVPN city names that contain a comma ("Phoenix, Arizona") are written the way Gluetun stores them ("Phoenix Arizona"). Gluetun removes commas because `SERVER_CITIES` is itself a comma-separated list.

## The mount path has to match

Compose resolves a relative bind mount (for example `./gluetun:/gluetun`) against the project directory **on the host**, the machine where the Docker daemon runs. This app runs `docker compose` from inside its own container, so the stack folder must be mounted into that container at the **same absolute path** it has on the host:

```yaml
volumes:
  - /path/to/vpn-stack:/path/to/vpn-stack
environment:
  STACK_DIR: /path/to/vpn-stack
```

`STACK_DIR` is that path. It is also where the `.env` file lives (`ENV_FILE`, default `.env` inside `STACK_DIR`).

## Setup

1. Install Docker on the server. This app's image includes the Docker **client** and the Compose plugin; it uses the host's daemon.

2. Generate a password hash and a session secret on any machine with Python:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python -m app.hash_password
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   The hash command reads the password from the terminal, not from the arguments, so it does not land in shell history. Paste the `PASSWORD_HASH=` line it prints. That line already doubles every `$`.

   Compose treats `$name` in an env file as interpolation. A bcrypt hash is full of dollar signs (`$2b$12$...`). Unescaped, Compose deletes pieces of the hash and login never works. `$$` is how you write a literal `$`.

3. Copy `.env.example` to `.env` next to the switcher's compose file (this is the switcher's config, not the VPN stack's `.env`) and fill in `PASSWORD_HASH`, `SESSION_SECRET`, and `STACK_DIR`.

4. Make the edits in [the VPN stack](#change-this-in-the-vpn-stack) above.

5. Build and start, from this repo:

   ```bash
   export DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
   export APP_UID="$(id -u)"
   export APP_GID="$(id -g)"
   docker compose -f docker-compose.example.yml up -d --build
   ```

   Edit the example first: replace `/path/to/vpn-stack`, set `LISTEN_ADDRESS` to a private address, and set `DEPENDENT_SERVICES` to the services that share the VPN network. `APP_UID` must be able to write the stack's `.env`. `DOCKER_GID` is the socket's group, which is a different number on every machine; `group_add` grants it at runtime so the image does not have to be rebuilt per host.

6. Open `http://<private-address>:8080` from a device on that network and sign in.

Run a single process. The command in the image does. A second worker would not share the in-memory switch lock or the login limiter.

## Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `PASSWORD_HASH` | required | bcrypt hash of the single password. Only the hash is stored. |
| `SESSION_SECRET` | required | Signs the session cookie. At least 16 characters. |
| `STACK_DIR` | required | Absolute path of the VPN compose project. Same path on the host and in this container. |
| `ENV_FILE` | `.env` | Env file to edit, relative to `STACK_DIR` unless absolute. |
| `COMPOSE_FILE` | compose's own default | Optional `-f` file, relative to `STACK_DIR` unless absolute. |
| `VPN_SERVICE` | `gluetun` | Compose service to recreate. |
| `VPN_CONTAINER` | empty | Container name to inspect. Empty asks `docker compose ps` for the service's container. |
| `DEPENDENT_SERVICES` | empty | Comma-separated services recreated with the VPN (the ones that use its network). |
| `COUNTRY_ENV` | `VPN_SERVER_COUNTRIES` | Key written in the stack `.env` for the country. |
| `CITY_ENV` | `VPN_SERVER_CITIES` | Key written in the stack `.env` for the city. |
| `CONTAINER_COUNTRY_ENV` | `SERVER_COUNTRIES` | Key read from the running container. |
| `CONTAINER_CITY_ENV` | `SERVER_CITIES` | Key read from the running container. |
| `LOCATION_REGIONS` | `Europe` | Comma-separated continent names as AirVPN reports them (`Europe`, `America`, `Asia`, …). Empty means every region. |
| `AIRVPN_STATUS_URL` | `https://airvpn.org/api/status/` | Server list. Cached in memory. |
| `LOCATION_CACHE_SECONDS` | `300` | How long a successful list is reused. |
| `LOCATION_FETCH_TIMEOUT` | `15` | Seconds for one status request. |
| `GLUETUN_PROXY_URL` | empty | HTTP proxy on Gluetun, for example `http://host.docker.internal:8888`. The exit-IP check is skipped when this is empty. Also the fallback for the auto-picked city when the control server is not set. |
| `GLUETUN_CONTROL_URL` | empty | Base URL of Gluetun's control server, for example `http://host.docker.internal:8000`. When no country or city is set, the page reads the connected city from `GET /v1/publicip/ip`. |
| `GLUETUN_CONTROL_API_KEY` | empty | Sent as `X-API-Key` when the control server requires auth. Leave empty only if that server allows unauthenticated reads on your private network. |
| `IP_INFO_URL` | `https://ipinfo.io/json` | Address lookup. `http://ip-api.com/json` works too. |
| `COMPOSE_TIMEOUT_SECONDS` | `180` | Ceiling for `docker compose up`. |
| `HEALTH_TIMEOUT_SECONDS` | `120` | How long to wait for Gluetun to become healthy. |
| `HEALTH_POLL_SECONDS` | `2` | Pause between health checks. |
| `IP_CHECK_TIMEOUT_SECONDS` | `20` | Ceiling for the exit-IP lookup, including retries. |
| `SECURE_COOKIE` | `false` | Set `true` when the app is served over HTTPS. |
| `SESSION_MAX_AGE_SECONDS` | `604800` | Session cookie lifetime (7 days). |
| `LOGIN_MAX_ATTEMPTS` | `5` | Failed logins allowed per client address. |
| `LOGIN_WINDOW_SECONDS` | `600` | Window for that limit. |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | Where the process listens inside the container. |

`COMPOSE_PROJECT_NAME` and other `COMPOSE_*` variables are passed through to the compose command if you set them on this container. The rest of this process's environment is not: Compose would otherwise let those variables override the stack's `.env`, including the location just written, and the password hash would be visible to the child process.

The heading is the location saved in the stack `.env`, read from the running container (`CONTAINER_COUNTRY_ENV` / `CONTAINER_CITY_ENV`, falling back to the `.env` key names). If the container cannot be read, the page shows the `.env` values and says so. If the container and the file disagree, the container wins and the page says the file is different.

When both the country and the city are empty, Gluetun has picked a server itself. The heading then shows where the tunnel actually is, with the note "Auto-picked", and that city's switch is on if the name matches the list. The preferred source is Gluetun's control server (`GLUETUN_CONTROL_URL`): it already knows the city and uses a full country name. If that URL is unset, or the control server has no city, the page uses the same exit lookup as the line under the heading (`GLUETUN_PROXY_URL` and `IP_INFO_URL`). A 2-letter country code from that lookup (ipinfo.io sends "NL") is matched to AirVPN's country code. If the city cannot be read, the heading says the location is unknown.

## Security

- **The Docker socket is full control of the host's containers.** Anyone who can act as this app can start, stop, and replace them. Keep the app on a private network. Publish its port on a tailnet address or on `127.0.0.1`, not on a public interface.
- One shared password. The plain password is never stored. Login is checked against the bcrypt hash, and failed attempts are rate-limited in memory (per process, reset on restart).
- The session cookie is signed, `HttpOnly`, and `SameSite=Lax`. Set `SECURE_COOKIE=true` behind HTTPS so the browser only sends it on HTTPS.
- Login, logout, and the switch POST all require a CSRF token from the session.
- Docker errors are shown in the page. Lines that look like a private key, password, or token are removed first, because Compose sometimes echoes a service's environment when a command fails.
- Do not commit `.env`. It is gitignored. `.env.example` has placeholders only.

## Where the code lives

The layout is the whole design. There is no framework beyond FastAPI, and no database.

| File | Role |
| --- | --- |
| `app/main.py` | Routes and HTML. The controller. |
| `app/config.py` | `Settings` loaded from environment variables. |
| `app/auth.py` | bcrypt check, CSRF comparison, login rate limit. No HTTP types, so the CLI and the tests call it directly. |
| `app/hash_password.py` | `python -m app.hash_password`. |
| `app/envfile.py` | Read and atomically update the stack `.env`. |
| `app/locations.py` | Fetch, filter, and cache AirVPN's status list. |
| `app/dockerctl.py` | The `docker compose` command, health wait, and container env. |
| `app/ipcheck.py` | Exit IP lookup, and the connected city from Gluetun's control server when no city is saved. |
| `app/switcher.py` | One switch, start to finish, and the one-at-a-time lock. |
| `app/templates/` | Jinja HTML. One login page, one location page. |
| `app/static/app.js` | Stays on the page and shows "Switching…". The forms work without it. |
| `app/static/style.css` | Mobile layout. |
| `tests/` | pytest. Docker and AirVPN are faked. |

A few Python shapes that map to C#:

- A `dataclass` with `frozen=True` is a record: fields are set once, then not assigned.
- `Path` is the path type. `os.replace` is an atomic rename, the same idea as replacing a file by renaming a finished temp file over it.
- `list[str] | None` is a nullable type. No `null` keyword; the value is `None`.
- The web server is async. `asyncio.to_thread(...)` runs the blocking Docker wait on a worker thread so other requests (the login page, a status check) can still be answered. Like `Task.Run`, with the lock still owned by the request.
- Dependencies are imported, not injected through a container. Tests replace a function with `monkeypatch`.

## Develop

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
docker build -t vpn-switcher .
```

`pytest` does not need Docker or a network. The image build does need network access, to download the Docker CLI and Compose plugin.
