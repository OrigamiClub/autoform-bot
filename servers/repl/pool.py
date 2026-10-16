"""Cold Lean REPL slots that each admit one disposable call."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .core import LeanRepl, LeanReplConfig, ReplCleanupError

logger = getLogger(__name__)

_SHUTDOWN = object()
_SHUTDOWN_REFUSAL = "Lean REPL pool is shut down; this call did not start and can be retried"


def _slot_timeout(timeout: float) -> TimeoutError:
    """Return the error for a call whose budget ran out before it started."""
    return TimeoutError(f"timed out after {timeout:g} seconds waiting for an idle Lean REPL slot")


@dataclass
class LeanReplPoolConfig(LeanReplConfig):
    """Configuration for a pool of cold Lean REPL slots."""

    num_repls: int = 1


class LeanReplPool:
    """Pool of cold wrappers that each own at most one disposable call.

    A dirty wrapper poisons the whole generation.  The project cache retains
    that generation and retries ``shutdown`` on a background reaper; request
    threads never spin on unbounded cleanup and no replacement can overlap it.
    """

    def __init__(self, config: LeanReplPoolConfig) -> None:
        self.config = config
        self.capacity = config.num_repls
        self._shutdown = False
        self._requires_retirement = False
        self._shutdown_wakeup = False

        self._workers: list[LeanRepl] = []
        self._idle: queue.Queue[LeanRepl | object] = queue.Queue()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active_calls = 0

        for _ in range(self.capacity):
            # Slots retain only Python wrappers. Each public call starts
            # and retires its own subprocess generation.
            repl = LeanRepl(config)
            self._workers.append(repl)
            self._idle.put(repl)

    @property
    def requires_retirement(self) -> bool:
        """Whether this generation must leave the resident project cache."""
        with self._condition:
            return self._requires_retirement

    def _stop_admission_locked(self, *, require_retirement: bool = False) -> None:
        """Stop admission and wake every queue waiter; condition is held."""
        if require_retirement:
            self._requires_retirement = True
        self._shutdown = True
        if not self._shutdown_wakeup:
            self._idle.put(_SHUTDOWN)
            self._shutdown_wakeup = True
        self._condition.notify_all()

    def _close_workers(self) -> None:
        """Close every constructed worker, preserving cleanup after one failure."""
        survivors: list[LeanRepl] = []
        first_error: BaseException | None = None
        for worker in reversed(self._workers):
            try:
                worker.close()
            except BaseException as error:
                logger.exception("failed to close REPL worker")
                survivors.append(worker)
                if first_error is None:
                    first_error = error
        self._workers = list(reversed(survivors))
        while True:
            try:
                self._idle.get_nowait()
            except queue.Empty:
                break
        self._shutdown_wakeup = False
        if first_error is not None:
            raise RuntimeError("one or more Lean REPL workers remain owned") from first_error

    def run(self, code: str, timeout: float | None = None) -> dict[str, Any]:
        """Run one cold call within a total queue-and-execution timeout."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            if self._shutdown:
                raise RuntimeError(_SHUTDOWN_REFUSAL)
            self._active_calls += 1

        repl: LeanRepl | None = None
        try:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise _slot_timeout(timeout)
            try:
                candidate = self._idle.get(timeout=remaining)
            except queue.Empty as error:
                raise _slot_timeout(timeout) from error
            if candidate is _SHUTDOWN:
                # Hand the wakeup to the next waiter.
                self._idle.put(_SHUTDOWN)
                raise RuntimeError(_SHUTDOWN_REFUSAL)
            repl = candidate

            with self._condition:
                if self._shutdown:
                    raise RuntimeError(_SHUTDOWN_REFUSAL)

            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise _slot_timeout(timeout)
            try:
                return repl.run_disposable(code, timeout=remaining, budget=timeout)
            except ReplCleanupError as error:
                # The response is process-free and remains valid. The dirty
                # generation transfers to retained project-cache retirement.
                return error.result
        finally:
            with self._condition:
                try:
                    if repl is not None:
                        if repl.is_clean():
                            if not self._shutdown:
                                self._idle.put(repl)
                        else:
                            self._stop_admission_locked(require_retirement=True)
                finally:
                    self._active_calls -= 1
                    self._condition.notify_all()

    def get_memory_usage(self) -> float:
        """Total memory usage in GB of the live child processes of calls in progress."""
        with self._lock:
            workers = tuple(self._workers)
        return sum(worker.get_memory_usage() for worker in workers)

    def shutdown(self) -> None:
        """Stop admission, wait for active calls, then retire every wrapper."""
        with self._condition:
            self._stop_admission_locked()
            while self._active_calls:
                self._condition.wait()
        self._close_workers()
