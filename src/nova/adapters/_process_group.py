"""Process-group isolation and clean SIGTERM/SIGKILL reaping helpers for CLI adapters (NOVA-020)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal


async def terminate_process_group(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 2.0,
) -> None:
    """Terminate a child CLI process and its entire POSIX process group cleanly."""
    pid = proc.pid
    my_pgrp: int | None = None
    with contextlib.suppress(OSError):
        my_pgrp = os.getpgrp()

    def _signal_group_or_proc(sig: signal.Signals) -> None:
        if pid is not None and pid > 0 and hasattr(os, "killpg"):
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                pgid = pid
            except OSError:
                pgid = None
            if pgid is not None and pgid == pid and (my_pgrp is None or pgid != my_pgrp):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(pgid, sig)
                return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                if sig == signal.SIGKILL:
                    proc.kill()
                else:
                    proc.terminate()

    if proc.returncode is None:
        _signal_group_or_proc(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except TimeoutError:
            _signal_group_or_proc(signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
    else:
        # Even if the direct child already exited, sweep any lingering grandchildren in its pgrp.
        _signal_group_or_proc(signal.SIGTERM)

    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()
    await asyncio.sleep(0)
