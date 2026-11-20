/**
 * Frozen wire contracts mirrored 1:1 from work/NOVA-004..NOVA-013/contract.md.
 */

export type CallSessionState =
  | "standby"
  | "listening"
  | "thinking"
  | "executing"
  | "speaking"
  | "closed";

export type WorkspaceQueryMode = "exact_path" | "voice_fuzzy" | "ui_picker";
export type WorkspaceTopology = "multi-project" | "single-repo";
export type AgentEngineId = "pi" | "agy" | "claude" | "opencode";
export type AgentEngineChoice = "auto" | AgentEngineId;
export type AgentAuthMode = "native_cli" | "api_token" | "unconfigured";
export type CommandSource =
  | "tier1_pi"
  | "tier2_agy"
  | "tier1_claude"
  | "tier2_claude"
  | "tier1_opencode"
  | "tier2_opencode"
  | "tier1_agy"
  | "tier2_pi";
export type AuditRiskLevel = "medium" | "high" | "critical";

export type UiEventType =
  | "workspace_changed"
  | "workspace_cleared"
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
  | "tier1_agent"
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

export interface UiProjectView {
  readonly project_id: string;
  readonly name: string;
  readonly relative_path: string;
  readonly canonical_path: string;
}

export interface UiWorkspaceView {
  readonly display_name: string;
  readonly canonical_path: string;
  readonly selected_via: WorkspaceQueryMode;
  readonly topology?: WorkspaceTopology | undefined;
  readonly projects?: readonly UiProjectView[] | undefined;
}

export interface UiWorkspaceOption {
  readonly display_name: string;
  readonly canonical_path: string;
  readonly topology?: WorkspaceTopology | undefined;
  readonly project_count?: number | undefined;
  readonly projects?: readonly UiProjectView[] | undefined;
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
  readonly onboarding_required?: boolean | undefined;
  readonly process_cwd?: string | undefined;
  readonly allowed_roots?: readonly string[] | undefined;
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
    readonly first_run_completed?: boolean | undefined;
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
    readonly tier1_engine?: AgentEngineChoice | undefined;
    readonly tier1_provider?: string | undefined;
    readonly tier1_model?: string | undefined;
    readonly tier2_engine?: AgentEngineChoice | undefined;
    readonly tier2_provider?: string | undefined;
    readonly single_engine_mode?: boolean | undefined;
    readonly pi_provider: string;
    readonly pi_model: string;
    readonly tier2_model: string;
  };
  readonly shortcuts: {
    readonly global_shortcut: string;
    readonly ptt_mode: PttMode;
    readonly control_command?: string | undefined;
    readonly desktop_environment?: string | undefined;
    readonly auto_register_supported?: boolean | undefined;
  };
}

export interface SettingsGetResponse {
  readonly schema_version: number;
  readonly config_path: string;
  readonly settings: NovaSettingsDocument;
}

export interface SettingsPutResponse {
  readonly saved?: boolean | undefined;
  readonly status?: string | undefined;
  readonly requires_restart: boolean;
  readonly restart_fields: readonly string[];
  readonly settings: NovaSettingsDocument;
}

export interface ShortcutRegisterResponse {
  readonly status: "registered" | "manual_required";
  readonly desktop_environment: string;
  readonly shortcut: string;
  readonly command: string;
  readonly manual_command?: string | null | undefined;
}

export interface MicDiagnosticResponse {
  readonly ok: boolean;
  readonly status: string;
  readonly duration_ms: number;
  readonly peak_level: number;
  readonly rms_level: number;
  readonly speech_detected: boolean;
  readonly transcript_preview: string;
  readonly message: string;
}

export interface AudioOutputDiagnosticResponse {
  readonly ok: boolean;
  readonly status: string;
  readonly engine: string;
  readonly latency_ms: number;
  readonly message: string;
}

export interface PiDiagnosticResponse {
  readonly ok: boolean;
  readonly status: string;
  readonly provider: string;
  readonly model: string;
  readonly latency_ms: number;
  readonly message: string;
}

export interface AntigravityDiagnosticResponse {
  readonly ok: boolean;
  readonly status: string;
  readonly model: string;
  readonly plugin_verified: boolean;
  readonly latency_ms: number;
  readonly message: string;
}

export interface AgentDiagnosticResponse {
  readonly ok: boolean;
  readonly engine: AgentEngineId;
  readonly status: string;
  readonly provider: string;
  readonly model: string;
  readonly plugin_verified: boolean;
  readonly latency_ms: number;
  readonly message: string;
}

export interface AgentCatalogEntry {
  readonly engine: AgentEngineId;
  readonly display_name: string;
  readonly installed: boolean;
  readonly binary_path: string | null;
  readonly auth_mode: AgentAuthMode;
  readonly configured_env_vars: readonly string[];
  readonly supports_tier1: boolean;
  readonly supports_tier2: boolean;
  readonly default_tier1_model: string;
  readonly default_tier2_model: string;
}

export interface ProviderCredentialStatus {
  readonly provider: string;
  readonly env_var: string;
  readonly configured: boolean;
}

export interface ActiveTierSelection {
  readonly engine: AgentEngineId;
  readonly provider: string;
  readonly model: string;
}

export interface AgentCatalogSnapshot {
  readonly schema_version: string;
  readonly credential_backend: "libsecret" | "local_file_0600" | "none";
  readonly active_tier1: ActiveTierSelection | null;
  readonly active_tier2: ActiveTierSelection | null;
  readonly single_engine_mode: boolean;
  readonly engines: readonly AgentCatalogEntry[];
  readonly providers: readonly ProviderCredentialStatus[];
}

export type DiagnosticTarget = "microphone" | "audio-output" | "pi" | "antigravity";

export interface LiveDiagnosticsState {
  readonly runningTarget: DiagnosticTarget | "all" | null;
  readonly microphone: MicDiagnosticResponse | null;
  readonly audioOutput: AudioOutputDiagnosticResponse | null;
  readonly pi: PiDiagnosticResponse | null;
  readonly antigravity: AntigravityDiagnosticResponse | null;
}

export interface InitializeWorkspaceParams {
  readonly parentRoot: string;
  readonly workspaceSlug: string;
  readonly displayName?: string | undefined;
  readonly projects: readonly string[];
}

export type AgentTaskStatus =
  | "running"
  | "queued"
  | "completed"
  | "gatekeeper_blocked"
  | "failed";

export type AgentsFilterKey = "all" | "running" | "gatekeeper" | "queued" | "completed";

export interface UiAgentTask {
  readonly id: string;
  readonly title: string;
  readonly description?: string;
  readonly workspace: string;
  readonly tier: "tier1_pi" | "tier2_agy";
  readonly agentName: string;
  readonly status: AgentTaskStatus;
  readonly progress?: number;
  readonly duration?: string;
  readonly lastLog?: string;
  readonly branch?: string;
}

export interface UiAgentPoolSlot {
  readonly name: string;
  readonly tier: "tier1_pi" | "tier2_agy";
  readonly activeSlots: number;
  readonly maxSlots: number;
  readonly status: "active" | "idle";
}

export interface UiAgentsOverview {
  readonly daemonStatus: "connected" | "disconnected";
  readonly daemonPid?: number;
  readonly memoryMb?: number;
  readonly poolSlots: readonly UiAgentPoolSlot[];
  readonly tasks: readonly UiAgentTask[];
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
  readonly noticeBanner?: UiNoticeBanner | null | undefined;
  readonly pickerFallbackOpen: boolean;
  readonly lastMilestone: string | null;
  readonly uiMode: "panel" | "orb";
  readonly activeTab?: "chat" | "agents" | undefined;
  readonly agentsFilter?: AgentsFilterKey | undefined;
  readonly agentsOverview?: UiAgentsOverview | undefined;
  readonly isStandby?: boolean | undefined;
  readonly settingsDrawerOpen?: boolean | undefined;
  readonly settingsLoading?: boolean | undefined;
  readonly settingsSaving?: boolean | undefined;
  readonly settingsData?: NovaSettingsDocument | null | undefined;
  readonly audioDevices?: AudioDeviceCatalog | null | undefined;
  readonly settingsNotice?: string | null | undefined;
  readonly settingsError?: string | null | undefined;
  readonly shortcutStatus?: ShortcutRegisterResponse | null | undefined;
  readonly appShuttingDown?: boolean | undefined;
  readonly onboardingOpen?: boolean | undefined;
  readonly onboardingSaving?: boolean | undefined;
  readonly onboardingDraftAudio?:
    | {
        readonly captureTarget: string;
        readonly playbackTarget: string;
      }
    | undefined;
  readonly processCwd?: string | undefined;
  readonly allowedRoots?: readonly string[] | undefined;
  readonly initWorkspaceOpen?: boolean | undefined;
  readonly diagnostics?: LiveDiagnosticsState | null | undefined;
  readonly agentCatalog?: AgentCatalogSnapshot | null | undefined;
  readonly agentProbeResult?: AgentDiagnosticResponse | null | undefined;
}
