/**
 * Frozen wire contracts mirrored 1:1 from work/NOVA-004/contract.md (nova.desktop.ui.v1 v1.3.0).
 */

export type CallSessionState =
  | "standby"
  | "listening"
  | "thinking"
  | "executing"
  | "speaking"
  | "closed";

export type WorkspaceQueryMode = "exact_path" | "voice_fuzzy" | "ui_picker";
export type CommandSource = "tier1_pi" | "tier2_agy";
export type AuditRiskLevel = "medium" | "high" | "critical";

export type UiEventType =
  | "workspace_changed"
  | "chat_stream"
  | "voice_milestone"
  | "runner_failure"
  | "command_blocked"
  | "command_approval_requested"
  | "command_approval_resolved"
  | "call_state_changed"
  | "voice_transcription"
  | "settings_updated"
  | "panel_toggle_requested";

export type UiMessageRole = "user" | "assistant" | "system" | "security";

export type UiMessageModality =
  | "text"
  | "voice"
  | "tier1_pi"
  | "tier2_runner"
  | "milestone"
  | "error"
  | "blocked";

export type ViewStatus = "loading" | "empty" | "success" | "error";

export interface UiCallSessionView {
  readonly call_id: string;
  readonly state: CallSessionState;
  readonly turn_count: number;
  readonly active_correlation_id: string | null;
}

export interface UiWorkspaceView {
  readonly display_name: string;
  readonly canonical_path: string;
  readonly selected_via: WorkspaceQueryMode;
}

export interface UiWorkspaceOption {
  readonly display_name: string;
  readonly canonical_path: string;
}

export interface UiApprovalItem {
  readonly audit_id: string;
  readonly correlation_id: string;
  readonly source: CommandSource;
  readonly sanitized_command: string;
  readonly cwd: string;
  readonly risk_level: AuditRiskLevel;
  readonly spoken_summary: string;
  readonly technical_explanation: string;
  readonly expires_in_seconds: number;
  readonly timestamp: string;
}

export interface UiChatMessage {
  readonly message_id: string;
  readonly correlation_id: string;
  readonly role: UiMessageRole;
  readonly modality: UiMessageModality;
  readonly step_type: string;
  readonly content: string;
  readonly timestamp: string;
  readonly streaming: boolean;
}

export interface UiStateSnapshot {
  readonly schema_version: "1.3.0";
  readonly csrf_token: string;
  readonly voice_enabled: boolean;
  readonly call_session: UiCallSessionView;
  readonly active_workspace: UiWorkspaceView | null;
  readonly available_workspaces: readonly UiWorkspaceOption[];
  readonly pending_approvals: readonly UiApprovalItem[];
  readonly messages: readonly UiChatMessage[];
}

export interface UiEventEnvelope {
  readonly seq: number;
  readonly event_type: UiEventType;
  readonly correlation_id: string;
  readonly timestamp: string;
  readonly payload: Readonly<Record<string, unknown>>;
}

export interface ErrorResponse {
  readonly error_code: string;
  readonly message: string;
  readonly correlation_id: string;
}

export interface UiNoticeBanner {
  readonly code: string;
  readonly message: string;
  readonly dismissible: boolean;
}

export type SensitivityPreset = "low" | "normal" | "high";
export type PttMode = "single_turn" | "toggle_call";
export type TtsEngineChoice = "kokoro" | "piper";

export interface AudioDeviceInfo {
  readonly id: string;
  readonly description: string;
  readonly is_default: boolean;
}

export interface AudioDeviceCatalog {
  readonly available: boolean;
  readonly backend: string;
  readonly inputs: readonly AudioDeviceInfo[];
  readonly outputs: readonly AudioDeviceInfo[];
}

export interface NovaSettingsDocument {
  readonly schema_version: number;
  readonly general: {
    readonly remember_workspace: boolean;
    readonly last_active_workspace: string | null;
  };
  readonly audio: {
    readonly capture_target: string | null;
    readonly playback_target: string | null;
  };
  readonly voice: {
    readonly enable_voice: boolean;
    readonly sensitivity_preset: SensitivityPreset;
    readonly wakeword_threshold: number;
    readonly speech_threshold: number;
    readonly barge_in_threshold: number;
    readonly whisper_model: string;
    readonly whisper_language: string | null;
    readonly whisper_device: "cuda" | "cpu" | "auto";
    readonly tts_engine: TtsEngineChoice;
    readonly tts_speed: number;
    readonly kokoro_voice: string;
  };
  readonly models: {
    readonly pi_provider: string;
    readonly pi_model: string;
    readonly tier2_model: string;
  };
  readonly shortcuts: {
    readonly global_shortcut: string;
    readonly ptt_mode: PttMode;
    readonly control_command?: string;
    readonly desktop_environment?: string;
    readonly auto_register_supported?: boolean;
  };
}

export interface SettingsGetResponse {
  readonly schema_version: number;
  readonly config_path: string;
  readonly settings: NovaSettingsDocument;
}

export interface SettingsPutResponse {
  readonly saved: boolean;
  readonly requires_restart: boolean;
  readonly restart_fields: readonly string[];
  readonly settings: NovaSettingsDocument;
}

export interface ShortcutRegisterResponse {
  readonly status: "registered" | "manual_required";
  readonly desktop_environment: string;
  readonly shortcut: string;
  readonly command: string;
  readonly manual_command?: string | null;
}

export interface PresentationalAppModel {
  readonly status: ViewStatus;
  readonly voiceEnabled: boolean;
  readonly callSession: UiCallSessionView;
  readonly activeWorkspace: UiWorkspaceView | null;
  readonly availableWorkspaces: readonly UiWorkspaceOption[];
  readonly pendingApprovals: readonly UiApprovalItem[];
  readonly messages: readonly UiChatMessage[];
  readonly errorBanner: string | null;
  readonly noticeBanner?: UiNoticeBanner | null;
  readonly pickerFallbackOpen: boolean;
  readonly lastMilestone: string | null;
  readonly uiMode: "panel" | "orb";
  readonly isStandby?: boolean;
  readonly settingsDrawerOpen?: boolean;
  readonly settingsLoading?: boolean;
  readonly settingsSaving?: boolean;
  readonly settingsData?: NovaSettingsDocument | null;
  readonly audioDevices?: AudioDeviceCatalog | null;
  readonly settingsNotice?: string | null;
  readonly settingsError?: string | null;
  readonly shortcutStatus?: ShortcutRegisterResponse | null;
}
