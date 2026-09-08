"""
Gunicorn settings for the OCR service.

The shape of this workload is unusual: every request occupies a CPU for
several seconds and each worker holds its own copy of the models in RAM.
So: few workers, long timeouts, no thread pool.
"""

import multiprocessing
import os

# ---- socket ----------------------------------------------------------------
# Unix socket is faster than TCP and cannot be reached from outside the box.
bind = os.environ.get("BIND", "unix:/run/occular/gunicorn.sock")
umask = 0o007                       # socket group-readable by nginx

# ---- workers ---------------------------------------------------------------
# Occular already uses several threads per inference (num_threads defaults to
# min(cores, 4)), so more workers than cores/4 just makes every request slower.
# Each worker also loads its own ~1 GB of weights — check RAM before raising.
workers = int(os.environ.get("WEB_CONCURRENCY", max(1, multiprocessing.cpu_count() // 4)))
worker_class = "sync"               # the models are not thread-safe
threads = 1

# Do NOT preload: ONNX runtime sessions and forked processes get along badly.
# Each worker builds its own pipeline in post_worker_init below.
preload_app = False

# ---- timeouts --------------------------------------------------------------
# A 300 dpi A4 page takes a few seconds on CPU; a dense spread can take much
# longer, and the very first request in a worker also loads the weights.
timeout = int(os.environ.get("TIMEOUT", 300))
graceful_timeout = 60
keepalive = 5

# Restarting a worker costs a full model reload, so recycling is off by default.
max_requests = int(os.environ.get("MAX_REQUESTS", 0))
max_requests_jitter = 50

# ---- logging ---------------------------------------------------------------
accesslog = os.environ.get("ACCESS_LOG", "-")
errorlog = os.environ.get("ERROR_LOG", "-")
loglevel = os.environ.get("LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'


def post_worker_init(worker):
    """
    Load the models as the worker starts rather than on the first user request,
    so nobody waits 60 seconds for a cold page.
    """
    try:
        import numpy as np
        import server

        with server._lock:
            pipe = server.get_pipeline(None)
            pipe.process_image(np.full((320, 320, 3), 255, dtype=np.uint8))
        worker.log.info("models warm")
    except Exception as exc:  # noqa: BLE001
        worker.log.warning("warm-up skipped: %s", exc)
