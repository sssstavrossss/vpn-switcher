"""HTTP routes. This is the controller layer.

Pages are server-rendered HTML. A short script in the browser calls the
same POST the form would send, so the page can show "Switching…" without
a full reload. With JavaScript disabled the form still works.

Routes:
- GET  /login     sign-in form
- POST /login     check the password, set a signed session cookie
- POST /logout    clear the session
- GET  /          the location list
- POST /switch    move the VPN
- GET  /status    current exit IP and, when no city is saved, the auto-picked place
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import LoginLimiter, csrf_matches, new_csrf_token, verify_password
from app.config import Settings
from app.errors import AppError
from app.ipcheck import ExitStatus, fetch_exit_info, resolve_exit
from app.locations import LocationCache, same_place
from app.switcher import DisplayLocation, SwitchGate, SwitchOutcome, choose_display, perform_switch, read_current

# Long enough for one control-server call or a couple of proxy tries.
# Short enough that a dead proxy does not hold the location list hostage.
# /status keeps trying for the full IP_CHECK_TIMEOUT_SECONDS and then
# corrects the heading.
_PAGE_EXIT_WAIT = 4.0

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. uvicorn calls this with no arguments (`--factory`)."""
    settings = settings if settings is not None else Settings.from_env()
    app = FastAPI(title="VPN switcher")
    app.state.settings = settings
    app.state.cache = LocationCache(settings)
    app.state.limiter = LoginLimiter(settings.login_max_attempts, settings.login_window_seconds)
    app.state.gate = SwitchGate()

    # The session cookie is signed with SESSION_SECRET. The server does not
    # store sessions; it can tell if the cookie was tampered with because
    # the signature would no longer match (same idea as ASP.NET's data
    # protection cookie, in a simpler form).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="vpn_switcher_session",
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.secure_cookie,
        path="/",
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/login")
    def login_form(request: Request) -> Response:
        if _authenticated(request):
            return _redirect("/")
        return _login_page(request, error=None, status_code=200)

    @app.post("/login")
    def login(request: Request, password: str = Form(""), csrf_token: str = Form("")) -> Response:
        if _authenticated(request):
            return _redirect("/")
        if not csrf_matches(_csrf(request), csrf_token):
            return _login_page(request, error="Reload the page and try again.", status_code=403)

        client = _client_address(request)
        limiter: LoginLimiter = request.app.state.limiter
        if limiter.too_many(client):
            return _login_page(
                request,
                error="Too many attempts. Wait a few minutes and try again.",
                status_code=429,
            )
        if not verify_password(password, settings.password_hash):
            limiter.record_failure(client)
            log.info("Rejected a login from %s", client)
            return _login_page(request, error="Wrong password.", status_code=401)

        limiter.reset(client)
        # Replace the session so a cookie from before login cannot be reused.
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf"] = new_csrf_token()
        return _redirect("/")

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")) -> Response:
        if _authenticated(request) and not csrf_matches(_csrf(request), csrf_token):
            return _redirect("/")
        request.session.clear()
        return _redirect("/login")

    @app.get("/")
    def index(request: Request) -> Response:
        if not _authenticated(request):
            return _redirect("/login")
        return _index_page(request)

    @app.get("/status")
    async def status(request: Request) -> JSONResponse:
        if not _authenticated(request):
            return JSONResponse({"ok": False, "detail": "Login required."}, status_code=401)
        snapshot = await asyncio.to_thread(_status_snapshot, request.app)
        snapshot["switching"] = request.app.state.gate.busy
        return JSONResponse(snapshot)

    @app.post("/switch")
    async def switch(
        request: Request,
        csrf_token: str = Form(""),
        country: str = Form(""),
        city: str = Form(""),
    ) -> Response:
        if not _authenticated(request):
            if _wants_json(request):
                return JSONResponse({"ok": False, "detail": "Login required."}, status_code=401)
            return _redirect("/login")
        if not csrf_matches(_csrf(request), csrf_token):
            outcome = SwitchOutcome(
                ok=False,
                detail="The form expired. Reload the page and try again.",
                status_code=403,
            )
            return _switch_response(request, outcome)

        gate: SwitchGate = request.app.state.gate
        await gate.acquire()
        try:
            try:
                outcome = await asyncio.to_thread(
                    perform_switch,
                    settings,
                    request.app.state.cache,
                    country,
                    city,
                )
            except AppError as exc:
                outcome = SwitchOutcome(ok=False, detail=exc.message, status_code=exc.status_code)
            except Exception:
                log.exception("Unexpected failure during a switch")
                outcome = SwitchOutcome(
                    ok=False,
                    detail="Something went wrong. The server log has the details.",
                    status_code=500,
                )
        finally:
            await gate.release()
        return _switch_response(request, outcome)

    return app


def _switch_response(request: Request, outcome: SwitchOutcome) -> Response:
    if _wants_json(request):
        return JSONResponse(outcome.as_dict(), status_code=outcome.status_code)
    kind = "error" if not outcome.ok else "warn" if outcome.detail and not outcome.unchanged else "info"
    banner = outcome.detail
    if outcome.ok and outcome.exit_ip:
        place = outcome.exit_label or outcome.label
        extra = f"Exit {outcome.exit_ip}" + (f" · {place}" if place else "")
        banner = f"{banner} {extra}".strip() if banner else extra
        kind = "warn" if outcome.detail else "info"
    return _index_page(request, banner=banner, banner_kind=kind, status_code=outcome.status_code if not outcome.ok else 200)


def _index_page(
    request: Request,
    banner: str | None = None,
    banner_kind: str = "info",
    status_code: int = 200,
) -> Response:
    settings: Settings = request.app.state.settings
    try:
        locations = request.app.state.cache.get()
        load_error = None
    except AppError as exc:
        locations = []
        load_error = exc.message

    current = read_current(settings)
    # Saved country/city: the heading is that choice, and the exit line
    # stays "Checking…" until /status (no extra lookup on this request).
    # Nothing saved: ask where the tunnel actually is, so the first paint
    # does not say "no location selected" above a real exit.
    if current.country or current.city:
        observed = None
    else:
        observed = resolve_exit(settings, timeout=_PAGE_EXIT_WAIT)
    display = choose_display(current, observed, locations)

    if banner is None and load_error:
        banner, banner_kind = load_error, "error"
    elif banner is None and current.warning:
        banner, banner_kind = current.warning, "warn"

    groups: list[dict] = []
    by_country: dict[str, dict] = {}
    for location in locations:
        key = location.country.casefold()
        if key not in by_country:
            by_country[key] = {"country": location.country, "rows": []}
            groups.append(by_country[key])
        selected = display.matched and same_place(
            location.country, location.city, display.country, display.city
        )
        by_country[key]["rows"].append(
            {
                "country": location.country,
                "city": location.city,
                "label": location.label,
                "selected": selected,
            }
        )

    if settings.regions:
        region_label = "Locations in " + ", ".join(sorted(name.title() for name in settings.regions))
    else:
        region_label = "Locations in every region"

    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "csrf_token": _ensure_csrf(request),
            "groups": groups,
            "place": display.heading,
            "place_note": display.note,
            "exit_line": _exit_line(observed),
            "banner": banner,
            "banner_kind": banner_kind,
            "region_label": region_label,
        },
        status_code=status_code,
    )
    return _no_store(response)


def _status_snapshot(app: FastAPI) -> dict:
    """Location heading plus the exit line. Runs in a worker thread."""
    settings: Settings = app.state.settings
    current = read_current(settings)
    try:
        locations = app.state.cache.get()
    except AppError:
        locations = []
    if current.country or current.city:
        # The heading is the saved choice. The exit lookup is only the
        # address line under it.
        exit_status = fetch_exit_info(settings)
        display = choose_display(current, None, locations)
    else:
        exit_status = resolve_exit(settings)
        display = choose_display(current, exit_status, locations)
    return _status_body(current.warning, display, exit_status)


def _status_body(warning: str | None, display: DisplayLocation, exit_status: ExitStatus) -> dict:
    return {
        "ok": True,
        "country": display.country,
        "city": display.city,
        "label": display.label,
        "place": display.heading,
        "note": display.note,
        "auto_picked": display.auto_picked,
        "matched": display.matched,
        "warning": warning,
        "exit_ip": exit_status.ip,
        "exit_label": exit_status.label,
        "exit_detail": exit_status.error,
    }


def _exit_line(observed: ExitStatus | None) -> str | None:
    """Text for the line under the heading, when we already looked it up."""
    if observed is None:
        return None
    if observed.ip:
        return f"Exit {observed.ip}" + (f" · {observed.label}" if observed.label else "")
    return observed.error


def _login_page(request: Request, *, error: str | None, status_code: int) -> Response:
    response = templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": _ensure_csrf(request), "error": error},
        status_code=status_code,
    )
    return _no_store(response)


def _ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str) or not token:
        token = new_csrf_token()
        request.session["csrf"] = token
    return token


def _csrf(request: Request) -> str | None:
    token = request.session.get("csrf")
    return token if isinstance(token, str) else None


def _authenticated(request: Request) -> bool:
    return request.session.get("authenticated") is True


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _client_address(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


def _redirect(url: str) -> RedirectResponse:
    return _no_store(RedirectResponse(url, status_code=303))


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response
