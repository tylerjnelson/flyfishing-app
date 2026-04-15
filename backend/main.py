import logging
import logging.config
import logging.handlers
import os
import sys
import uuid
from contextvars import ContextVar

import pillow_heif
from fastapi import FastAPI, Request, Response

# Add the repo root (/opt/flyfish) to sys.path so that `prompts/` is importable
# as a top-level package from any module under backend/.
# WorkingDirectory in the systemd unit is /opt/flyfish/backend — the parent
# directory is /opt/flyfish which contains prompts/, scripts/, etc.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import settings  # noqa: F401 — validates env vars at startup

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

# Per-coroutine request ID storage — safe for concurrent async requests
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


_LOG_FILE = "/var/log/flyfish/app.log"

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "logging.Formatter",
            "fmt": '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","request_id":"%(request_id)s","message":"%(message)s"}',
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        }
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "class": "logging.handlers.WatchedFileHandler",
            "formatter": "json",
            "filename": _LOG_FILE,
            # WatchedFileHandler reopens the file when it detects an inode change,
            # which is what logrotate's copytruncate produces — no SIGHUP needed.
        },
    },
    "root": {"level": "INFO", "handlers": ["stdout", "file"]},
}

logging.config.dictConfig(LOGGING_CONFIG)

# Apply request ID filter to the handler directly (avoids __main__ class ref issues)
_request_id_filter = _RequestIdFilter()
for handler in logging.root.handlers:
    handler.addFilter(_request_id_filter)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Fly Fish WA", docs_url=None, redoc_url=None)

# Holds a persistent DB connection in the worker that won the scheduler lock.
# None in all other workers. Closed on shutdown to release the advisory lock.
_scheduler_lock_conn = None

# Advisory lock key — arbitrary stable integer unique to this app.
_SCHEDULER_LOCK_KEY = 1_234_567_890


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    global _scheduler_lock_conn

    # Register pillow-heif so Pillow can open HEIC/HEIF images natively
    pillow_heif.register_heif_opener()
    log.info("pillow_heif_registered")

    # Elect exactly one gunicorn worker to run the scheduler.
    # pg_try_advisory_lock is non-blocking: the first worker that calls it wins
    # the lock and starts the scheduler; subsequent workers get False and skip.
    # The lock is session-scoped — releasing the connection releases the lock,
    # so if the elected worker dies another worker can take over on restart.
    from sqlalchemy import text
    from db.connection import engine

    _scheduler_lock_conn = await engine.connect()
    result = await _scheduler_lock_conn.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": _SCHEDULER_LOCK_KEY}
    )
    got_lock = result.scalar()

    if got_lock:
        from conditions.scheduler import start_scheduler
        start_scheduler()
        log.info("scheduler_worker_elected")
    else:
        log.info("scheduler_worker_skipped")


@app.on_event("shutdown")
async def shutdown() -> None:
    global _scheduler_lock_conn

    from conditions.scheduler import stop_scheduler
    stop_scheduler()

    if _scheduler_lock_conn is not None:
        await _scheduler_lock_conn.close()
        _scheduler_lock_conn = None


# ---------------------------------------------------------------------------
# Correlation request ID middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next) -> Response:
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    token = _request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        _request_id_var.reset(token)

    response.headers["X-Request-ID"] = request_id
    return response


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

from auth.router import router as auth_router
from chat.router import router as chat_router
from notes.router import router as notes_router
from spots.router import router as spots_router
from trips.router import router as trips_router
from users.router import router as users_router

app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(users_router, prefix="/api/users", tags=["users"])
app.include_router(spots_router, prefix="/api/spots", tags=["spots"])
app.include_router(notes_router, prefix="/api/notes", tags=["notes"])
app.include_router(trips_router, prefix="/api/trips", tags=["trips"])
app.include_router(chat_router, prefix="/api", tags=["chat"])
