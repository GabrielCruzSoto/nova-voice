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
  | "voice_transcription";

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

export interface PresentationalAppModel {
  readonly status: ViewStatus;
  readonly voiceEnabled: boolean;
  readonly callSession: UiCallSessionView;
  readonly activeWorkspace: UiWorkspaceView | null;
  readonly availableWorkspaces: readonly UiWorkspaceOption[];
  readonly pendingApprovals: readonly UiApprovalItem[];
  readonly messages: readonly UiChatMessage[];
  readonly errorBanner: string | null;
  readonly pickerFallbackOpen: boolean;
  readonly lastMilestone: string | null;
}
