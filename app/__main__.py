"""Start the web server. The container runs `python -m app`.

One worker process: the switch lock and the login limiter live in memory,
so a second worker would not know about them.
"""

import uvicorn

from app.config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
