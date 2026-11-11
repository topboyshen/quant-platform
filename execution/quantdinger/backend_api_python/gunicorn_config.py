"""Gunicorn configuration for QuantDinger backend.

Background workers (strategy restore, portfolio monitor, etc.) are started
inside ``create_app()`` which is called once per worker.  We use gthread
(threads in a single worker) by default to keep a familiar single-process
model while still allowing concurrent I/O.  Increase ``workers`` for
higher throughput — background tasks are idempotent and use DB locks to
coordinate, so duplicate work is minimal.
"""
import os

bind = f"{os.getenv('PYTHON_API_HOST', '0.0.0.0')}:{os.getenv('PYTHON_API_PORT', '5000')}"

# Default: 1 worker + 4 threads — same concurrency model as Flask dev server
# but with better stability and connection handling.
# Increase GUNICORN_WORKERS for multi-core throughput.
workers = int(os.getenv("GUNICORN_WORKERS", 1))
threads = int(os.getenv("GUNICORN_THREADS", 4))

# Background workers and strategy restoration use process-local registries.
# Running them in multiple web workers can execute the same trading strategy
# more than once. Multi-worker mode is valid only for an API-only process.
skip_startup_hooks = os.getenv("SKIP_STARTUP_HOOKS", "").strip().lower() in (
    "1", "true", "yes", "on",
)
if workers > 1 and not skip_startup_hooks:
    raise RuntimeError(
        "GUNICORN_WORKERS>1 requires SKIP_STARTUP_HOOKS=1; run trading and "
        "background jobs in a separate single-instance worker"
    )

worker_class = "gthread"
timeout = 120
graceful_timeout = 30
keepalive = 5

# Do NOT preload — background threads in create_app() rely on being in
# the actual worker process.  preload would start them in master then
# lose them after fork.
preload_app = False

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")

limit_request_line = 8190
limit_request_fields = 100
