"""Process-group isolation and clean SIGTERM/SIGKILL reaping helpers for CLI adapters (NOVA-020)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable
from typing import Literal


async def await_with_heartbeat[T](
    awaitable: Awaitable[T],
    *,
    deadline: float,
    heartbeat_seconds: float | None,
    pause_predicate: Callable[[], bool] | None = None,
) -> tuple[T | None, Literal["ok", "heartbeat", "total"]]:
    """Await `awaitable` with total deadline and pausable stdout heartbeat watchdog."""
    loop = asyncio.get_running_loop()
    task: asyncio.Task[T] = asyncio.ensure_future(awaitable)
    silent_elapsed = 0.0
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None, "total"

            hb = heartbeat_seconds
            if hb is None or hb <= 0:
                slice_timeout = remaining
                expecting: Literal["heartbeat", "total"] = "total"
            elif pause_predicate is None:
                if hb < remaining:
                    slice_timeout = hb
                    expecting = "heartbeat"
                else:
                    slice_timeout = remaining
                    expecting = "total"
            else:
                if pause_predicate():
                    silent_elapsed = 0.0
                hb_remaining = max(0.01, hb - silent_elapsed)
                poll_cap = min(1.0, max(0.05, hb / 4.0))
                slice_timeout = min(remaining, hb_remaining, poll_cap)
                expecting = "heartbeat" if hb_remaining < remaining else "total"

            t0 = loop.time()
            done, _ = await asyncio.wait({task}, timeout=slice_timeout)
            if task in done:
                return task.result(), "ok"
            waited = max(0.0, loop.time() - t0)
            if loop.time() >= deadline:
                return None, "total"
            if pause_predicate is None:
                return None, expecting
            if pause_predicate():
                silent_elapsed = 0.0
                continue
            silent_elapsed += waited
            if hb is not None and silent_elapsed >= hb:
                return None, "heartbeat"
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _is_pgrp_alive(pgid: int) -> bool:
    """Return True if at least one process in POSIX process group `pgid` still exists."""
    if not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


async def _wait_for_pgrp_exit(pgid: int, *, timeout_seconds: float) -> bool:
    """Wait up to `timeout_seconds` for all processes in `pgid` to exit; return True if gone."""
    poll_interval = 0.05
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.05, timeout_seconds)
    max_steps = max(1, int(max(0.05, timeout_seconds) / poll_interval) + 2)
    for _ in range(max_steps):
        if not _is_pgrp_alive(pgid):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))
    return not _is_pgrp_alive(pgid)


async def terminate_process_group(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 2.0,
) -> None:
    """Terminate a child CLI process and its entire POSIX process group with SIGKILL escalation."""
    pid = proc.pid
    my_pgrp: int | None = None
    with contextlib.suppress(OSError):
        my_pgrp = os.getpgrp()

    target_pgid: int | None = None
    if pid is not None and pid > 0 and hasattr(os, "killpg"):
        pgid: int | None = None
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            # Direct child may already have been reaped while grandchildren remain in pgrp == pid.
            pgid = pid if _is_pgrp_alive(pid) else None
        except OSError:
            pgid = None
        if pgid is not None and pgid == pid and (my_pgrp is None or pgid != my_pgrp):
            target_pgid = pgid

    def _signal_target(sig: signal.Signals) -> None:
        if target_pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(target_pgid, sig)
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                if sig == signal.SIGKILL:
                    proc.kill()
                else:
                    proc.terminate()

    loop = asyncio.get_running_loop()
    if proc.returncode is None:
        deadline = loop.time() + max(0.05, grace_seconds)
        _signal_target(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(0.05, grace_seconds))
        except TimeoutError:
            _signal_target(signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
        # Even if direct child exited quickly, wait briefly for stubborn grandchildren then SIGKILL
        if target_pgid is not None and _is_pgrp_alive(target_pgid):
            remaining = max(0.05, deadline - loop.time())
            if not await _wait_for_pgrp_exit(target_pgid, timeout_seconds=remaining):
                _signal_target(signal.SIGKILL)
    elif target_pgid is not None and _is_pgrp_alive(target_pgid):
        # Direct child already exited, but grandchildren remain in its session process group
        _signal_target(signal.SIGTERM)
        sweep_timeout = min(0.5, max(0.05, grace_seconds))
        if not await _wait_for_pgrp_exit(target_pgid, timeout_seconds=sweep_timeout):
            _signal_target(signal.SIGKILL)

    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()
    await asyncio.sleep(0)
