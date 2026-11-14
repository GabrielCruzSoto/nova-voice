"""TDD tests for NOVA-008 T4: DesktopUiServer settings, audio devices, PTT, and hot reload."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.config_store import TomlConfigStore
from nova.adapters.desktop_ui_server import DesktopUiServer, DesktopUiServerSettings
from nova.adapters.shortcut_registrar import LinuxShortcutRegistrar
from nova.application.settings_service import SettingsService
from nova.application.ui_state_hub import UiStateHub
from nova.domain.events import CommandApprovalResolvedEvent, OrchestratorEventBus
from nova.domain.models import (
    AudioDeviceCatalog,
    AudioDeviceInfo,
    CallSession,
    CallSessionState,
    NovaPersistedSettings,
    PttMode,
    WorkspaceContext,
    WorkspaceQueryMode,
)


class _FakeOrchestrator:
    def __init__(self, tmp_path: Path) -> None:
        self._ws = WorkspaceContext(
            workspace_id="ws-1",
            name="nova",
            canonical_path=str(tmp_path),
            selected_via="exact_path",
        )
        self.updated_models: list[tuple[str, str, str]] = []

    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "ui_picker",
        correlation_id: str | None = None,
    ) -> WorkspaceContext:
        del query, mode, correlation_id
        return self._ws

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
    ) -> None:
        del utterance, correlation_id

    async def barge_in(self) -> None:
        return

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_ui",
    ) -> CommandApprovalResolvedEvent:
        del audit_id, approved, resolved_by
        raise NotImplementedError

    def update_models(self, *, pi_provider: str, pi_model: str, tier2_model: str) -> None:
        self.updated_models.append((pi_provider, pi_model, tier2_model))


class _FakeVoiceControl:
    def __init__(self) -> None:
        self._session = CallSession(
            call_id="call-1",
            state="standby",
            last_activity_monotonic=0.0,
            turn_count=0,
        )
        self.ptt_actions: list[str] = []
        self.updated_ptt_modes: list[PttMode] = []

    @property
    def session(self) -> CallSession:
        return self._session

    async def start_call(self, reason: str = "ui_call_start") -> None:
        del reason
        self._session = self._session.model_copy(update={"state": "listening"})

    async def end_call(self, reason: str = "ui_call_end") -> None:
        del reason
        self._session = self._session.model_copy(update={"state": "standby"})

    async def barge_in(self) -> None:
        self._session = self._session.model_copy(update={"state": "listening"})

    async def trigger_ptt_toggle(self) -> CallSessionState:
        self.ptt_actions.append("toggle")
        next_st: CallSessionState = "listening" if self._session.state == "standby" else "standby"
        self._session = self._session.model_copy(update={"state": next_st})
        return next_st

    async def start_ptt_hold(self) -> CallSessionState:
        self.ptt_actions.append("start")
        self._session = self._session.model_copy(update={"state": "listening"})
        return "listening"

    async def stop_ptt_hold(self) -> CallSessionState:
        self.ptt_actions.append("stop")
        self._session = self._session.model_copy(update={"state": "standby"})
        return "standby"

    def update_settings(
        self,
        *,
        ptt_mode: PttMode | None = None,
        speech_threshold: float | None = None,
        barge_in_threshold: float | None = None,
    ) -> None:
        del speech_threshold, barge_in_threshold
        if ptt_mode is not None:
            self.updated_ptt_modes.append(ptt_mode)


class _FakeAudioDiscovery:
    async def list_devices(self) -> AudioDeviceCatalog:
        return AudioDeviceCatalog(
            available=True,
            backend="pipewire",
            inputs=(AudioDeviceInfo(id="mic-1", description="USB Mic", is_default=True),),
            outputs=(AudioDeviceInfo(id="spk-1", description="Speakers", is_default=True),),
        )


async def _http_req(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        req_headers = {"Host": f"127.0.0.1:{port}", "Connection": "close"}
        if headers:
            req_headers.update(headers)
        if body:
            req_headers["Content-Length"] = str(len(body))
        raw = (
            f"{method} {path} HTTP/1.1\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in req_headers.items())
            + "\r\n"
        ).encode("latin-1") + body
        writer.write(raw)
        await writer.drain()
        resp_raw = await asyncio.wait_for(reader.read(), timeout=5.0)
    finally:
        writer.close()
        await writer.wait_closed()
    head, _, payload = resp_raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    hdrs: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, payload


@pytest.mark.asyncio
async def test_settings_endpoints_audio_devices_and_ptt(tmp_path: Path) -> None:
    cfg_path = tmp_path / "nova" / "config.toml"
    store = TomlConfigStore(config_path=cfg_path)
    bus = OrchestratorEventBus()
    orch = _FakeOrchestrator(tmp_path)
    voice = _FakeVoiceControl()
    hot_applied: list[NovaPersistedSettings] = []

    settings_svc = SettingsService(
        config_store=store,
        initial_settings=store.load(),
        audio_discovery=_FakeAudioDiscovery(),
        shortcut_registrar=LinuxShortcutRegistrar(
            env={"XDG_CURRENT_DESKTOP": "COSMIC", "XDG_CONFIG_HOME": str(tmp_path)}
        ),
        event_bus=bus,
        on_hot_apply=lambda updated: hot_applied.append(updated),
    )
    hub = UiStateHub(event_bus=bus, voice_enabled=True)
    srv = DesktopUiServer(
        orchestrator=orch,
        state_hub=hub,
        event_bus=bus,
        voice_coordinator=voice,
        settings_service=settings_svc,
        settings=DesktopUiServerSettings(host="127.0.0.1", port=0),
    )
    await srv.start()
    try:
        # 1. Bootstrap session + CSRF
        st_idx, hdr_idx, _ = await _http_req(srv.port, "GET", "/")
        assert st_idx == 200
        cookie = hdr_idx["set-cookie"].split(";", 1)[0]
        st_state, _, body_state = await _http_req(
            srv.port, "GET", "/api/state", headers={"Cookie": cookie}
        )
        assert st_state == 200
        csrf = json.loads(body_state.decode("utf-8"))["csrf_token"]

        # 2. GET /api/settings & GET /api/audio/devices
        st_get, _, body_get = await _http_req(
            srv.port, "GET", "/api/settings", headers={"Cookie": cookie}
        )
        assert st_get == 200
        assert json.loads(body_get.decode("utf-8"))["settings"]["shortcuts"]["ptt_mode"] == (
            "single_turn"
        )

        st_dev, _, body_dev = await _http_req(
            srv.port, "GET", "/api/audio/devices", headers={"Cookie": cookie}
        )
        assert st_dev == 200
        dev_json = json.loads(body_dev.decode("utf-8"))
        assert dev_json["inputs"][0]["id"] == "mic-1"

        # 3. PUT /api/settings requires CSRF (403 without) and rejects secrets (422)
        st_403, _, _ = await _http_req(
            srv.port,
            "PUT",
            "/api/settings",
            headers={"Cookie": cookie, "Origin": srv.base_url},
            body=b"{}",
        )
        assert st_403 == 403

        st_422, _, _ = await _http_req(
            srv.port,
            "PUT",
            "/api/settings",
            headers={
                "Cookie": cookie,
                "Origin": srv.base_url,
                "X-Nova-CSRF": csrf,
            },
            body=json.dumps({"models": {"api_key": "sk-secret-1234"}}).encode("utf-8"),
        )
        assert st_422 == 422

        # 4. Valid PUT /api/settings persists 0600 TOML and hot-applies (incl. empty lang -> auto)
        put_payload: dict[str, Any] = {
            "audio": {"capture_target": "mic-1"},
            "voice": {
                "tts_speed": 1.4,
                "sensitivity_preset": "high",
                "whisper_language": "",
            },
            "shortcuts": {
                "ptt_mode": "toggle_call",
                "global_shortcut": "Super+Alt+Space",
            },
        }
        st_put, _, body_put = await _http_req(
            srv.port,
            "PUT",
            "/api/settings",
            headers={
                "Cookie": cookie,
                "Origin": srv.base_url,
                "X-Nova-CSRF": csrf,
            },
            body=json.dumps(put_payload).encode("utf-8"),
        )
        assert st_put == 200
        put_json = json.loads(body_put.decode("utf-8"))
        assert put_json["requires_restart"] is False
        assert put_json["settings"]["voice"]["tts_speed"] == pytest.approx(1.4)
        assert put_json["settings"]["voice"]["wakeword_threshold"] == pytest.approx(0.45)
        assert put_json["settings"]["voice"]["whisper_language"] is None
        assert put_json["settings"]["shortcuts"]["global_shortcut"] == "Super+Alt+Space"
        assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
        assert len(hot_applied) == 1

        # 5. POST /api/call/ptt
        st_ptt, _, body_ptt = await _http_req(
            srv.port,
            "POST",
            "/api/call/ptt",
            headers={"Cookie": cookie, "Origin": srv.base_url, "X-Nova-CSRF": csrf},
            body=b'{"action":"start"}',
        )
        assert st_ptt == 200
        ptt_json = json.loads(body_ptt.decode("utf-8"))
        assert ptt_json["status"] == "accepted"
        assert ptt_json["call_state"] == "listening"
        assert voice.ptt_actions == ["start"]

        st_ptt_bad, _, body_ptt_bad = await _http_req(
            srv.port,
            "POST",
            "/api/call/ptt",
            headers={"Cookie": cookie, "Origin": srv.base_url, "X-Nova-CSRF": csrf},
            body=b'{"action":"invalid"}',
        )
        assert st_ptt_bad == 422
        assert json.loads(body_ptt_bad.decode("utf-8"))["error_code"] == "INVALID_PTT_ACTION"

        # 6. POST /api/settings/shortcut
        st_sc, _, body_sc = await _http_req(
            srv.port,
            "POST",
            "/api/settings/shortcut",
            headers={"Cookie": cookie, "Origin": srv.base_url, "X-Nova-CSRF": csrf},
            body=b'{"shortcut":"Super+Shift+Space"}',
        )
        assert st_sc == 200
        assert json.loads(body_sc.decode("utf-8"))["status"] == "registered"

        # 7. Workspace selection persists last_active_workspace
        st_ws, _, _ = await _http_req(
            srv.port,
            "POST",
            "/api/workspace/select",
            headers={"Cookie": cookie, "Origin": srv.base_url, "X-Nova-CSRF": csrf},
            body=json.dumps({"path_or_query": str(tmp_path)}).encode("utf-8"),
        )
        assert st_ws == 200
        assert store.load().general.last_active_workspace == str(tmp_path)
    finally:
        await srv.close()
