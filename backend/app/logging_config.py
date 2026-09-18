"""Centralized logging configuration for the backend.

Previously the only logging in this app was a StreamHandler on the
"websockets" logger (routers/streams_ws.py) -- console-only, and scoped to
one router. Nothing was written to disk, so once a terminal scrolled away
or was closed there was no record of what the backend had actually seen
(including, critically, which Origin header a rejected CORS request
carried).

setup_logging() configures the ROOT logger with both a console handler and
a rotating file handler, and re-points uvicorn's own loggers ("uvicorn",
"uvicorn.error", "uvicorn.access") at the same handlers -- uvicorn
configures those with propagate=False by default, so without this they
would keep bypassing the root logger and never reach the file.

Call this once, as early as possible in app/main.py (before the FastAPI
app or any router that grabs a logger at import time).
"""
import logging
import logging.handlers
import os

# backend/app/logging_config.py -> backend/logs/backend.log
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(_BACKEND_DIR, "logs"))
LOG_FILE = os.path.join(LOG_DIR, "backend.log")

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        # Guards against double configuration -- e.g. uvicorn --reload
        # re-imports app.main in the reloader's parent process too.
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [file_handler, console_handler]

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [file_handler, console_handler]
        uv_logger.propagate = False

    _configured = True
    logging.getLogger(__name__).info("Logging configured. Writing to %s", LOG_FILE)
