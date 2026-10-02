"""Single-flight background jobs (login/sync/index) with captured log output.

Playwright's sync API must stay off the asyncio event loop, so every job runs
in its own worker thread; only one job runs at a time (the browser profile is
single-user anyway).
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Callable

log = logging.getLogger("canvassync")


class Job:
    def __init__(self, kind: str):
        self.kind = kind
        self.state = "running"  # running | done | error
        self.started = time.time()
        self.finished: float | None = None
        self.lines: list[str] = []
        self.result: str | None = None
        self.error: str | None = None

    def to_dict(self, tail: int = 200) -> dict:
        return {
            "kind": self.kind,
            "state": self.state,
            "started": self.started,
            "finished": self.finished,
            "lines": self.lines[-tail:],
            "result": self.result,
            "error": self.error,
        }


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self._job = job
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._job.lines.append(self.format(record))
        except Exception:
            pass


class JobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self.current: Job | None = None

    def start(self, kind: str, fn: Callable[[Job], str]) -> tuple[Job | None, str | None]:
        """Start `fn` in a worker thread. Returns (job, None) or (None, busy-reason)."""
        with self._lock:
            if self.current and self.current.state == "running":
                return None, f"a '{self.current.kind}' job is already running"
            job = Job(kind)
            self.current = job

        def run() -> None:
            handler = _JobLogHandler(job)
            log.addHandler(handler)
            try:
                job.result = fn(job)
                job.state = "done"
            except Exception as exc:
                job.error = str(exc) or type(exc).__name__
                job.lines.append(f"error: {job.error}")
                job.state = "error"
                log.debug("job %s failed:\n%s", kind, traceback.format_exc())
            finally:
                log.removeHandler(handler)
                job.finished = time.time()

        threading.Thread(target=run, name=f"canvassync-job-{kind}", daemon=True).start()
        return job, None


runner = JobRunner()
