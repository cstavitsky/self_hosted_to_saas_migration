"""Per-run console logging -> timestamped file.

Call ``start_run_log("<tool-name>")`` once at the top of a script's entry point.
It creates ``<tool-name>_<YYYYmmdd_HHMMSS>.log`` in the current working directory
(next to the ``*_results.json`` files the tools already write) and captures the
run's console output there while still printing to the terminal:

  * a logging handler is attached so every ``logger.*`` line is written to the file;
  * ``sys.stdout`` / ``sys.stderr`` are tee'd so ``print()`` output and uncaught
    tracebacks are captured too.

Returns the log file path. Safe to call once per process (subsequent calls no-op).
The console handler installed by ``logging.basicConfig`` is left untouched, so
nothing is duplicated in the file.
"""
import atexit
import logging
import os
import sys
from datetime import datetime


class _Tee:
    """Write to the real stream and the log file; delegate everything else."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        self._stream.write(data)
        try:
            self._fh.write(data)
        except Exception:
            pass
        return len(data)

    def flush(self):
        self._stream.flush()
        try:
            self._fh.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


_started = False


def start_run_log(name, log_dir=None):
    global _started
    if _started:
        return None
    _started = True

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = log_dir or os.getcwd()
    path = os.path.join(log_dir, f"{name}_{ts}.log")
    fh = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered
    atexit.register(fh.close)

    # 1) logging output -> file (separate handler; console handler is left as-is)
    handler = logging.StreamHandler(fh)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)

    # 2) print() + uncaught tracebacks -> tee'd to the same file
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)

    logging.getLogger(name).info(f"Run log: {path}")
    return path
