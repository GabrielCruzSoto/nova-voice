"""Local authenticated AF_UNIX control socket and fast CLI client (NOVA-008 OP-6)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

CONTROL_IPC_SCHEMA = "nova.control.ipc.v1"
VALID_CONTROL_ACTIONS: frozenset[str] = frozenset(
    {"ptt-toggle", "toggle-panel", "barge-in", "ping"}
)
MAX_CONTROL_LINE_BYTES = 1024

ControlHandler = Callable[[str], Awaitable[dict[str, Any]]]


def resolve_control_socket_path(
    *,
    runtime_base_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve `$XDG_RUNTIME_DIR/nova/control.sock` or `/tmp/nova-control-<uid>/control.sock`."""
    if runtime_base_dir is not None:
        base = Path(runtime_base_dir)
        return base / "nova" / "control.sock"
    effective_env = env if env is not None else os.environ
    xdg_runtime = effective_env.get("XDG_RUNTIME_DIR", "").strip()
    if xdg_runtime:
        return Path(xdg_runtime) / "nova" / "control.sock"
    return Path(f"/tmp/nova-control-{os.getuid()}") / "control.sock"


def _validate_control_action(action: str) -> str:
    cleaned = action.strip()
    if cleaned not in VALID_CONTROL_ACTIONS:
        allowed = ", ".join(sorted(VALID_CONTROL_ACTIONS))
        raise ValueError(f"Unsupported control action '{action}'. Expected one of: {allowed}")
    return cleaned


def _peer_uid_matches(sock: socket.socket) -> bool:
    if not hasattr(socket, "SO_PEERCRED"):
        return True
    try:
        cred_bytes = sock.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        _pid, peer_uid, _gid = struct.unpack("3i", cred_bytes)
        return int(peer_uid) == os.getuid()
    except OSError:
        return False


def send_control_command(
    action: str,
    *,
    socket_path: Path | str | None = None,
    timeout_seconds: float = 1.5,
) -> dict[str, Any]:
    """Synchronously send a single JSONL control action over AF_UNIX using stdlib only."""
    validated = _validate_control_action(action)
    target = Path(socket_path) if socket_path is not None else resolve_control_socket_path()
    payload = (
        json.dumps(
            {"schema": CONTROL_IPC_SCHEMA, "action": validated},
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout_seconds)
        client.connect(str(target))
        client.sendall(payload)
        chunks: list[bytes] = []
        total = 0
        while total < MAX_CONTROL_LINE_BYTES:
            data = client.recv(1024)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if b"\n" in data:
                break

    raw_line = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if not raw_line:
        raise OSError("Empty control response from Nova daemon.")
    parsed = json.loads(raw_line)
    if not isinstance(parsed, dict):
        raise OSError("Malformed control response from Nova daemon.")
    return parsed


class UnixControlIpcServer:
    """Confined 0700/0600 AF_UNIX JSONL server with SO_PEERCRED UID verification."""

    def __init__(
        self,
        *,
        handler: ControlHandler,
        runtime_base_dir: Path | str | None = None,
        socket_path: Path | None = None,
    ) -> None:
        self._handler = handler
        self._socket_path = (
            socket_path
            if socket_path is not None
            else resolve_control_socket_path(runtime_base_dir=runtime_base_dir)
        )
        self._server: asyncio.AbstractServer | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()

    @property
    def socket_path(self) -> str:
        return str(self._socket_path)

    async def start(self) -> None:
        """Create 0700 parent dir, verify no live daemon holds the socket, and listen (0600)."""
        if self._server is not None:
            return

        parent = self._socket_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)

        if self._socket_path.exists():
            try:
                probe = await asyncio.to_thread(
                    send_control_command,
                    "ping",
                    socket_path=self._socket_path,
                    timeout_seconds=0.25,
                )
                if probe.get("ok"):
                    raise RuntimeError(
                        f"Nova daemon is already running on {self._socket_path}."
                    )
            except OSError:
                with contextlib.suppress(OSError):
                    self._socket_path.unlink()

        server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self._socket_path),
            limit=MAX_CONTROL_LINE_BYTES,
        )
        os.chmod(self._socket_path, 0o600)
        self._server = server

    async def close(self) -> None:
        """Stop listening, cancel active client tasks, and unlink the socket."""
        srv = self._server
        self._server = None
        if srv is not None:
            srv.close()
        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()
        if srv is not None:
            with contextlib.suppress(Exception):
                await srv.wait_closed()
        with contextlib.suppress(OSError):
            self._socket_path.unlink()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            sock = writer.get_extra_info("socket")
            if isinstance(sock, socket.socket) and not _peer_uid_matches(sock):
                await self._write_response(
                    writer,
                    {
                        "schema": CONTROL_IPC_SCHEMA,
                        "ok": False,
                        "error_code": "CONTROL_UNAUTHORIZED",
                        "message": "Peer UID does not match Nova daemon UID.",
                    },
                )
                return

            try:
                raw_line = await asyncio.wait_for(reader.readline(), timeout=1.5)
            except TimeoutError:
                return
            if not raw_line or len(raw_line) > MAX_CONTROL_LINE_BYTES:
                await self._write_response(
                    writer,
                    {
                        "schema": CONTROL_IPC_SCHEMA,
                        "ok": False,
                        "error_code": "CONTROL_INVALID_REQUEST",
                        "message": "Invalid or oversized control frame.",
                    },
                )
                return

            try:
                req = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                await self._write_response(
                    writer,
                    {
                        "schema": CONTROL_IPC_SCHEMA,
                        "ok": False,
                        "error_code": "CONTROL_INVALID_JSON",
                        "message": "Malformed JSON control request.",
                    },
                )
                return

            if not isinstance(req, dict) or req.get("schema") != CONTROL_IPC_SCHEMA:
                await self._write_response(
                    writer,
                    {
                        "schema": CONTROL_IPC_SCHEMA,
                        "ok": False,
                        "error_code": "CONTROL_INVALID_SCHEMA",
                        "message": "Unexpected control IPC schema.",
                    },
                )
                return

            action = req.get("action")
            if not isinstance(action, str) or action not in VALID_CONTROL_ACTIONS:
                await self._write_response(
                    writer,
                    {
                        "schema": CONTROL_IPC_SCHEMA,
                        "ok": False,
                        "error_code": "CONTROL_INVALID_ACTION",
                        "message": "Unsupported control action.",
                    },
                )
                return

            if action == "ping":
                await self._write_response(
                    writer,
                    {"schema": CONTROL_IPC_SCHEMA, "ok": True, "action": "ping"},
                )
                return

            result = await self._handler(action)
            payload = {"schema": CONTROL_IPC_SCHEMA, "ok": True, **result}
            await self._write_response(writer, payload)
        finally:
            if task is not None:
                self._client_tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
    ) -> None:
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        writer.write(encoded)
        await writer.drain()
