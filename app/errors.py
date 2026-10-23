"""Errors whose text is safe to show in the browser.

Anything raised as AppError is a situation we expected (bad input, Docker
failed, the status API was down). Unexpected bugs are not AppError; the
web layer logs those and shows a generic message instead.
"""


class AppError(Exception):
    """Base class for failures we can explain to the person using the app."""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BadRequestError(AppError):
    """The browser sent a location we will not apply."""

    status_code = 400


class SwitchBusyError(AppError):
    """Another switch is still running. Only one is allowed at a time."""

    status_code = 409


class DockerError(AppError):
    """The Docker CLI failed, or Gluetun did not become healthy in time."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class EnvEditError(AppError):
    """The stack .env file could not be updated safely."""

    status_code = 500


class LocationError(AppError):
    """AirVPN's server list could not be loaded or parsed."""

    status_code = 502
