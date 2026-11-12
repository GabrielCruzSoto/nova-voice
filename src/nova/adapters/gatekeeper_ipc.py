"""Authenticated AF_UNIX JSONL server bridging CLI hooks to SecurityGatekeeper (OP-1)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import stat
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nova.domain.errors import GatekeeperError
from nova.domain.models import (
    CommandExecutionDecision,
    CommandInterceptRequest,
    CommandSource,
    ToolInterceptName,
)
from nova.domain.ports import SecurityGatekeeperPort

MAX_IPC_FRAME_BYTES = 65536
MAX_SUN_PATH_BYTES = 100


class GatekeeperIPCConfig(BaseModel):
    """Configuration boundary for UnixSocketGatekeeperServer."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runtime_base_dir: str | None = None
    read_timeout_seconds: float = Field(default=5.0, gt=0.0)


class _GatekeeperIPCRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    protocol_version: Literal["1.0"] = "1.0"
    token: str
    correlation_id: str = Field(min_length=1)
    source: CommandSource
    tool_name: ToolInterceptName
    command: str
    cwd: str = Field(min_length=1)


def _prepare_secure_socket_dir(custom_base: str | None) -> Path:
    if custom_base is not None:
        target = Path(custom_base)
    else:
        target = Path(f"/tmp/nova-{os.getuid()}")

    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(target)
    if stat.S_ISLNK(st.st_mode):
        raise GatekeeperError(
            code="GATEKEEPER_IPC_UNSAFE_DIR",
            message="Gatekeeper socket directory cannot be a symbolic link.",
        )
    if st.st_uid != os.getuid():
        raise GatekeeperError(
            code="GATEKEEPER_IPC_UNSAFE_DIR",
            message="Gatekeeper socket directory is not owned by the current user.",
        )
    os.chmod(target, 0o700)
    return target


class UnixSocketGatekeeperServer:
    """Listens on a 0600 AF_UNIX socket and authenticates pre-execution CLI hooks."""

    def __init__(
        self,
        gatekeeper: SecurityGatekeeperPort,
        config: GatekeeperIPCConfig | None = None,
    ) -> None:
        self._gatekeeper = gatekeeper
        self._config = config or GatekeeperIPCConfig()
        self._token = secrets.token_urlsafe(32)
        register_fn = getattr(gatekeeper, "register_ephemeral_secret", None)
        if callable(register_fn):
            register_fn(self._token)

        self._server: asyncio.AbstractServer | None = None
        self._socket_path: str = ""

    @property
    def socket_path(self) -> str:
        """Active AF_UNIX socket filesystem path."""
        return self._socket_path

    def child_env(self) -> dict[str, str]:
        """Environment variables injected into child CLI processes (Pi and agy)."""
        if not self._socket_path:
            raise GatekeeperError(
                code="GATEKEEPER_IPC_NOT_STARTED",
                message="UnixSocketGatekeeperServer has not been started yet.",
            )
        return {
            "NOVA_GATEKEEPER_SOCKET": self._socket_path,
            "NOVA_GATEKEEPER_TOKEN": self._token,
        }

    async def __aenter__(self) -> UnixSocketGatekeeperServer:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Create 0600 Unix domain socket and start accepting hook requests."""
        if self._server is not None:
            return

        sock_dir = _prepare_secure_socket_dir(self._config.runtime_base_dir)
        sock_file = sock_dir / f"gk-{uuid.uuid4().hex[:8]}.sock"
        sock_str = str(sock_file)
        if len(sock_str.encode()) > MAX_SUN_PATH_BYTES:
            raise GatekeeperError(
                code="GATEKEEPER_IPC_PATH_TOO_LONG",
                message=f"AF_UNIX path exceeds {MAX_SUN_PATH_BYTES} bytes: {sock_str}",
            )

        with contextlib.suppress(FileNotFoundError):
            sock_file.unlink()

        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=sock_str,
                limit=MAX_IPC_FRAME_BYTES,
            )
        finally:
            os.umask(old_umask)

        os.chmod(sock_str, 0o600)
        self._socket_path = sock_str

    async def close(self) -> None:
        """Stop server and remove socket file from disk."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._socket_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._socket_path)
            self._socket_path = ""

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            decision = await self._process_single_request(reader)
            wire_bytes = (decision.model_dump_json() + "\n").encode()
            writer.write(wire_bytes)
            await writer.drain()
        except Exception:
            fallback = CommandExecutionDecision(
                allowed=False,
                verdict="invalid_request",
                audit_id=None,
                reason="GATEKEEPER_INVALID_REQUEST: Internal IPC processing failure.",
            )
            with contextlib.suppress(Exception):
                writer.write((fallback.model_dump_json() + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _process_single_request(
        self,
        reader: asyncio.StreamReader,
    ) -> CommandExecutionDecision:
        try:
            raw_line = await asyncio.wait_for(
                reader.readline(),
                timeout=self._config.read_timeout_seconds,
            )
        except (TimeoutError, ValueError, asyncio.LimitOverrunError):
            return CommandExecutionDecision(
                allowed=False,
                verdict="invalid_request",
                audit_id=None,
                reason="GATEKEEPER_INVALID_REQUEST: IPC frame timed out or exceeded 64 KiB.",
            )

        if not raw_line or not raw_line.endswith(b"\n"):
            return CommandExecutionDecision(
                allowed=False,
                verdict="invalid_request",
                audit_id=None,
                reason="GATEKEEPER_INVALID_REQUEST: Incomplete or empty IPC frame.",
            )

        try:
            payload: Any = json.loads(raw_line.decode("utf-8"))
            ipc_req = _GatekeeperIPCRequest.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return CommandExecutionDecision(
                allowed=False,
                verdict="invalid_request",
                audit_id=None,
                reason="GATEKEEPER_INVALID_REQUEST: Malformed IPC JSON payload.",
            )

        if not hmac.compare_digest(ipc_req.token.encode(), self._token.encode()):
            return CommandExecutionDecision(
                allowed=False,
                verdict="unauthorized",
                audit_id=None,
                reason="GATEKEEPER_IPC_UNAUTHORIZED: Invalid gatekeeper IPC credential.",
            )

        try:
            return await self._await_authorization_or_disconnect(
                reader=reader,
                request=CommandInterceptRequest(
                    correlation_id=ipc_req.correlation_id,
                    source=ipc_req.source,
                    tool_name=ipc_req.tool_name,
                    command=ipc_req.command,
                    cwd=ipc_req.cwd,
                ),
            )
        except GatekeeperError as exc:
            return CommandExecutionDecision(
                allowed=False,
                verdict="invalid_request",
                audit_id=None,
                reason=f"{exc.code}: {exc.sanitized_message}",
            )

    async def _await_authorization_or_disconnect(
        self,
        *,
        reader: asyncio.StreamReader,
        request: CommandInterceptRequest,
    ) -> CommandExecutionDecision:
        auth_task = asyncio.create_task(self._gatekeeper.evaluate_and_authorize(request))
        eof_task = asyncio.create_task(reader.read(1))
        try:
            done, _ = await asyncio.wait(
                {auth_task, eof_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if auth_task in done:
                return auth_task.result()

            eof_bytes = eof_task.result()
            if eof_bytes == b"":
                auth_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await auth_task
                return CommandExecutionDecision(
                    allowed=False,
                    verdict="invalid_request",
                    audit_id=None,
                    reason="GATEKEEPER_CLIENT_DISCONNECTED: IPC client closed socket early.",
                )

            return await auth_task
        finally:
            if not eof_task.done():
                eof_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await eof_task
            if not auth_task.done():
                auth_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await auth_task
