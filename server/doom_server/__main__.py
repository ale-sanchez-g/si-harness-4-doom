"""Run the Doom API server: ``python -m doom_server``."""

import logging

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info",
                access_log=False)


if __name__ == "__main__":
    main()
