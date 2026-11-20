/**
 * Container Layer: DesktopAppController (profiles/frontend-web.md#1,2,3,6,9).
 * Holds CSRF token strictly in private JS memory, manages SSE + fetch lifecycle via
 * AbortController, and delegates rendering to pure components.ts functions.
 */

import {
  escapeHtml,
  getDefaultAgentsOverview,
  renderAgentsMatrixView,
  renderAppShell,
  renderApprovalBanner,
  renderCallControls,
  renderChatTimeline,
  renderDock,
  renderNoticeSlot,
  renderNovaReactiveOrb,
  renderNovaSquircleSvg,
  renderOnboardingModal,
  renderSettingsDrawer,
  renderSinusoidalWave,
  renderWorkspaceBar,
  resolveHumanNotice,
} from "./components.js";
import type {
  AgentCatalogSnapshot,
  AgentDiagnosticResponse,
  AgentEngineId,
  AgentTaskStatus,
  AgentsFilterKey,
  AntigravityDiagnosticResponse,
  AudioDeviceCatalog,
  AudioOutputDiagnosticResponse,
  DiagnosticTarget,
  ErrorResponse,
  HealthRecheckResponse,
  InitializeWorkspaceParams,
  LiveDiagnosticsState,
  MicDiagnosticResponse,
  ModelsProvisionState,
  SystemHealthReport,
  UiCallSessionView,
  NovaSettingsDocument,
  PiDiagnosticResponse,
  PresentationalAppModel,
  SettingsGetResponse,
  SettingsPutResponse,
  ShortcutRegisterResponse,
  UiAgentTask,
  UiApprovalItem,
  UiChatMessage,
  UiEventEnvelope,
  UiProjectView,
  UiStateSnapshot,
  UiWorkspaceView,
  ViewStatus,
} from "./contracts.js";

const MAX_CLIENT_MESSAGES = 200;

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface DesktopControllerOptions {
  readonly rootElement?: HTMLElement | null;
  readonly fetchImpl?: FetchLike;
  readonly onRender?: (html: string, model: PresentationalAppModel) => void;
  readonly onMinimizeToHost?: (() => void) | undefined;
  readonly onQuitHost?: (() => void) | undefined;
}

const SETTINGS_DRAFT_FIELDS = [
  "audio.capture_target",
  "audio.playback_target",
  "voice.sensitivity_preset",
  "voice.tts_speed",
  "voice.tts_engine",
  "voice.whisper_language",
  "shortcuts.ptt_mode",
  "shortcuts.global_shortcut",
  "models.tier1_engine",
  "models.tier2_engine",
  "models.single_engine_mode",
  "models.pi_provider",
  "models.pi_model",
  "models.tier2_model",
  "general.remember_workspace",
] as const;

const ONBOARDING_DRAFT_FIELDS = [
  "onboarding.capture_target",
  "onboarding.playback_target",
] as const;

interface FormFieldDraft {
  readonly value: string;
  readonly checked: boolean;
}

function snapshotFormFields(
  container: HTMLElement,
  fieldNames: readonly string[],
): Map<string, FormFieldDraft> {
  const out = new Map<string, FormFieldDraft>();
  for (const name of fieldNames) {
    const el = container.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`);
    if (el) {
      out.set(name, {
        value: el.value,
        checked: "checked" in el ? Boolean(el.checked) : false,
      });
    }
  }
  return out;
}

function restoreFormFields(
  container: HTMLElement,
  snapshot: ReadonlyMap<string, FormFieldDraft>,
): void {
  for (const [name, draft] of snapshot.entries()) {
    const el = container.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`);
    if (!el) {
      continue;
    }
    el.value = draft.value;
    if ("checked" in el) {
      el.checked = draft.checked;
    }
  }
}

export class DesktopAppController {
  #csrfToken = "";
  #abortController = new AbortController();
  #disposed = false;
  #shellMounted = false;
  #pttHolding = false;
  #lastRenderedSettingsRef: NovaSettingsDocument | null | undefined = undefined;
  #sleepTimer: ReturnType<typeof setTimeout> | null = null;
  readonly #fetch: FetchLike;
  readonly #root: HTMLElement | null;
  readonly #onRender: ((html: string, model: PresentationalAppModel) => void) | undefined;
  readonly #onMinimizeToHost: (() => void) | undefined;
  readonly #onQuitHost: (() => void) | undefined;

  #model: PresentationalAppModel = {
    status: "loading",
    voiceEnabled: false,
    callSession: {
      call_id: "call-loading",
      state: "standby",
      turn_count: 0,
      active_correlation_id: null,
    },
    activeWorkspace: null,
    availableWorkspaces: [],
    pendingApprovals: [],
    messages: [],
    errorBanner: null,
    noticeBanner: null,
    pickerFallbackOpen: false,
    lastMilestone: null,
    uiMode: "panel",
    activeTab: "chat",
    agentsFilter: "all",
    agentsOverview: getDefaultAgentsOverview(),
    onboardingOpen: false,
    onboardingSaving: false,
    processCwd: "",
    allowedRoots: [],
    initWorkspaceOpen: false,
    diagnostics: {
      runningTarget: null,
      microphone: null,
      audioOutput: null,
      pi: null,
      antigravity: null,
    },
  };

  constructor(options: DesktopControllerOptions = {}) {
    this.#root = options.rootElement ?? null;
    this.#fetch = options.fetchImpl ?? ((url, init) => globalThis.fetch(url, init));
    this.#onRender = options.onRender;
    this.#onMinimizeToHost = options.onMinimizeToHost;
    this.#onQuitHost = options.onQuitHost;
  }

  get model(): PresentationalAppModel {
    return this.#model;
  }

  async start(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    this.#bindDomEvents();
    this.#render();

    try {
      const resp = await this.#fetch("/api/state", {
        method: "GET",
        credentials: "same-origin",
        signal: this.#abortController.signal,
      });
      if (!resp.ok) {
        const errBody = await this.#safeParseError(resp);
        this.#updateModel({
          status: "error",
          errorBanner: errBody?.message ?? `Error HTTP ${resp.status} al cargar estado.`,
        });
        return;
      }
      const snapshot = (await resp.json()) as UiStateSnapshot;
      this.#applySnapshot(snapshot);

      if (snapshot.onboarding_required) {
        await this.#preloadSettingsAndDevices();
      }

      try {
        const healthResp = await this.#fetch("/api/health", {
          method: "GET",
          credentials: "same-origin",
          signal: this.#abortController.signal,
        });
        if (healthResp.ok) {
          const healthData = (await healthResp.json()) as SystemHealthReport;
          if (!healthData.all_ok && healthData.alert_message) {
            this.#updateModel({
              healthReport: healthData,
              noticeBanner: resolveHumanNotice("COMPONENTS_MISSING", healthData.alert_message),
            });
          } else {
            this.#updateModel({ healthReport: healthData });
          }
        }
      } catch {
        // Non-blocking health probe
      }

      await this.#fetchModelsStatus();

      void this.#consumeSseLoop();
    } catch (err) {
      if (this.#abortController.signal.aborted) {
        return;
      }
      const msg = err instanceof Error ? err.message : "Fallo de conexión con el puente local.";
      this.#updateModel({ status: "error", errorBanner: msg });
    }
  }

  dispose(): void {
    if (this.#disposed) {
      return;
    }
    this.#disposed = true;
    if (this.#sleepTimer) {
      clearTimeout(this.#sleepTimer);
      this.#sleepTimer = null;
    }
    this.#csrfToken = "";
    this.#abortController.abort();
  }

  dismissNotice(): void {
    if (this.#disposed) {
      return;
    }
    this.#updateModel({ noticeBanner: null, errorBanner: null });
  }

  async submitChat(utterance: string): Promise<void> {
    const trimmed = utterance.trim();
    if (!trimmed || this.#disposed) {
      return;
    }
    const resp = await this.#postJson("/api/chat", { utterance: trimmed });
    if (!resp.ok) {
      const errBody = await this.#safeParseError(resp);
      const code = errBody?.error_code ?? "ORCHESTRATOR_UNEXPECTED_FAILURE";
      this.#updateModel({
        noticeBanner: resolveHumanNotice(code),
        errorBanner: null,
      });
    } else if (this.#model.noticeBanner !== null || this.#model.errorBanner !== null) {
      this.#updateModel({ noticeBanner: null, errorBanner: null });
    }
  }

  async triggerCallAction(action: "start" | "end" | "barge-in"): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const inCall =
      this.#model.callSession.state !== "standby" && this.#model.callSession.state !== "closed";
    if (action === "barge-in" && (!inCall || !this.#model.voiceEnabled)) {
      return;
    }
    const resp = await this.#postJson(`/api/call/${action}`, {});
    if (!resp.ok) {
      const errBody = await this.#safeParseError(resp);
      const code = errBody?.error_code ?? "ORCHESTRATOR_UNEXPECTED_FAILURE";
      this.#updateModel({
        noticeBanner: resolveHumanNotice(code),
        errorBanner: null,
      });
    }
  }

  async toggleUiMode(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    if ((this.#model.uiMode ?? "panel") === "panel" && this.#onMinimizeToHost) {
      this.#onMinimizeToHost();
      return;
    }
    const nextMode = (this.#model.uiMode ?? "panel") === "orb" ? "panel" : "orb";
    this.#updateModel({ uiMode: nextMode });
  }

  async quitApplication(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    try {
      const resp = await this.#postJson("/api/shutdown", {});
      if (!resp.ok) {
        const errBody = await this.#safeParseError(resp);
        const code = errBody?.error_code ?? "ORCHESTRATOR_UNEXPECTED_FAILURE";
        this.#updateModel({
          noticeBanner: resolveHumanNotice(code),
          errorBanner: null,
        });
        return;
      }
    } catch {
      if (this.#disposed) {
        return;
      }
    }
    this.#updateModel({
      appShuttingDown: true,
      settingsDrawerOpen: false,
      callSession: {
        ...this.#model.callSession,
        state: "closed",
        active_correlation_id: null,
      },
      errorBanner: null,
      noticeBanner: {
        code: "APP_SHUTDOWN",
        message: "Nova se cerró correctamente.",
        dismissible: false,
      },
    });
    this.dispose();
    if (this.#onQuitHost) {
      this.#onQuitHost();
    }
  }

  async handleEscapeKey(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    if (this.#model.settingsDrawerOpen) {
      this.closeSettingsDrawer();
      return;
    }
    const inCall =
      this.#model.callSession.state !== "standby" && this.#model.callSession.state !== "closed";
    if (inCall) {
      await this.triggerCallAction("barge-in");
      return;
    }
    if ((this.#model.uiMode ?? "panel") === "orb") {
      this.#updateModel({ uiMode: "panel" });
    }
  }

  switchTab(tab: "chat" | "agents"): void {
    if (this.#disposed) {
      return;
    }
    this.#updateModel({ activeTab: tab });
  }

  setAgentsFilter(filter: AgentsFilterKey): void {
    if (this.#disposed) {
      return;
    }
    this.#updateModel({ agentsFilter: filter });
  }

  async handleAgentTaskAction(
    taskId: string,
    action: "approve" | "cancel" | "pause" | "inspect",
  ): Promise<void> {
    if (this.#disposed || !taskId) {
      return;
    }
    const overview = this.#model.agentsOverview ?? getDefaultAgentsOverview(this.#model.activeWorkspace?.canonical_path);
    const targetTask = overview.tasks.find((t) => t.id === taskId);
    if (!targetTask) {
      return;
    }

    if (action === "inspect") {
      const detail = targetTask.lastLog ? ` — ${targetTask.lastLog}` : "";
      this.#updateModel({
        lastMilestone: `#tsk-${targetTask.id}: ${targetTask.title}${detail}`,
      });
      return;
    }

    if (action === "approve" || action === "cancel") {
      const matchingApproval = this.#model.pendingApprovals.find((a) => a.audit_id === taskId);
      if (matchingApproval) {
        await this.resolveApproval(taskId, action === "approve");
      }
    }

    let nextStatus: AgentTaskStatus = targetTask.status;
    let nextLog = targetTask.lastLog;
    let milestone = "";

    if (action === "approve" && targetTask.status === "gatekeeper_blocked") {
      nextStatus = "running";
      nextLog = "> Aprobado por el usuario; reanudando ejecución...";
      milestone = `Tarea #tsk-${taskId} aprobada.`;
    } else if (action === "pause" && targetTask.status === "running") {
      nextStatus = "queued";
      nextLog = "Pausada por el usuario.";
      milestone = `Tarea #tsk-${taskId} pausada.`;
    } else if (
      action === "cancel" &&
      (targetTask.status === "running" ||
        targetTask.status === "queued" ||
        targetTask.status === "gatekeeper_blocked")
    ) {
      nextStatus = "failed";
      nextLog = "Cancelada por el usuario.";
      milestone = `Tarea #tsk-${taskId} cancelada.`;
    } else {
      return;
    }

    const updatedTasks: readonly UiAgentTask[] = overview.tasks.map((task) =>
      task.id === taskId
        ? {
            ...task,
            status: nextStatus,
            ...(nextLog !== undefined ? { lastLog: nextLog } : {}),
          }
        : task,
    );

    const updatedSlots = overview.poolSlots.map((slot) => {
      const activeForTier = updatedTasks.filter(
        (t) => t.tier === slot.tier && (t.status === "running" || t.status === "gatekeeper_blocked"),
      ).length;
      return {
        ...slot,
        activeSlots: activeForTier,
        status: activeForTier > 0 ? ("active" as const) : ("idle" as const),
      };
    });

    this.#updateModel({
      agentsOverview: {
        ...overview,
        poolSlots: updatedSlots,
        tasks: updatedTasks,
      },
      lastMilestone: milestone,
    });
  }
  toggleInitWorkspaceForm(): void {
    if (this.#disposed) {
      return;
    }
    this.#updateModel({ initWorkspaceOpen: !this.#model.initWorkspaceOpen });
  }

  async clearWorkspace(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const resp = await this.#postJson("/api/workspace/clear", {});
    if (!resp.ok) {
      const errBody = await this.#safeParseError(resp);
      const code = errBody?.error_code ?? "ORCHESTRATOR_UNEXPECTED_FAILURE";
      this.#updateModel({ noticeBanner: resolveHumanNotice(code), errorBanner: null });
      return;
    }
    this.#updateModel({
      activeWorkspace: null,
      initWorkspaceOpen: false,
      pickerFallbackOpen: false,
      errorBanner: null,
      noticeBanner: null,
    });
  }

  async initializeWorkspace(params: InitializeWorkspaceParams): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const slug = params.workspaceSlug.trim();
    const parent = params.parentRoot.trim();
    if (!slug || !parent) {
      return;
    }
    const resp = await this.#postJson("/api/workspace/init", {
      parent_root: parent,
      workspace_slug: slug,
      display_name: params.displayName?.trim() || null,
      projects: params.projects.map((p) => p.trim()).filter((p) => p.length > 0),
      activate: true,
    });
    if (!resp.ok) {
      const errBody = await this.#safeParseError(resp);
      const code = errBody?.error_code ?? "WORKSPACE_INVALID_MANIFEST";
      this.#updateModel({ noticeBanner: resolveHumanNotice(code), errorBanner: null });
      return;
    }
    const data = (await resp.json()) as {
      created: boolean;
      workspace: UiWorkspaceView;
    };
    if (data.created && data.workspace) {
      const existsInList = this.#model.availableWorkspaces.some(
        (w) => w.canonical_path === data.workspace.canonical_path,
      );
      const nextAvailable = existsInList
        ? this.#model.availableWorkspaces
        : [
            ...this.#model.availableWorkspaces,
            {
              display_name: data.workspace.display_name,
              canonical_path: data.workspace.canonical_path,
              topology: data.workspace.topology,
              project_count: data.workspace.projects?.length ?? 0,
              projects: data.workspace.projects,
            },
          ];
      this.#updateModel({
        activeWorkspace: data.workspace,
        availableWorkspaces: nextAvailable,
        initWorkspaceOpen: false,
        pickerFallbackOpen: false,
        errorBanner: null,
        noticeBanner: null,
      });
    }
  }

  #currentDiagState(): LiveDiagnosticsState {
    return (
      this.#model.diagnostics ?? {
        runningTarget: null,
        microphone: null,
        audioOutput: null,
        pi: null,
        antigravity: null,
      }
    );
  }

  #readActiveProbeParams(): {
    captureTarget: string | null;
    playbackTarget: string | null;
    piProvider: string | null;
    piModel: string | null;
    tier2Model: string | null;
  } {
    const s = this.#model.settingsData;
    const root = this.#root;
    let captureTarget: string | null = s?.audio.capture_target ?? null;
    let playbackTarget: string | null = s?.audio.playback_target ?? null;
    let piProvider: string | null = s?.models.pi_provider ?? null;
    let piModel: string | null = s?.models.pi_model ?? null;
    let tier2Model: string | null = s?.models.tier2_model ?? null;

    if (this.#model.onboardingOpen) {
      const capDom = root?.querySelector<HTMLSelectElement>('[name="onboarding.capture_target"]');
      const playDom = root?.querySelector<HTMLSelectElement>('[name="onboarding.playback_target"]');
      const capVal =
        capDom !== null && capDom !== undefined
          ? capDom.value
          : this.#model.onboardingDraftAudio?.captureTarget;
      const playVal =
        playDom !== null && playDom !== undefined
          ? playDom.value
          : this.#model.onboardingDraftAudio?.playbackTarget;
      if (capVal !== undefined) {
        captureTarget = capVal.trim() || null;
      }
      if (playVal !== undefined) {
        playbackTarget = playVal.trim() || null;
      }
    } else if (this.#model.settingsDrawerOpen && root) {
      const capDom = root.querySelector<HTMLSelectElement>('[name="audio.capture_target"]');
      const playDom = root.querySelector<HTMLSelectElement>('[name="audio.playback_target"]');
      const provDom = root.querySelector<HTMLInputElement>('[name="models.pi_provider"]');
      const modDom = root.querySelector<HTMLInputElement>('[name="models.pi_model"]');
      const t2Dom = root.querySelector<HTMLInputElement>('[name="models.tier2_model"]');
      if (capDom) {
        captureTarget = capDom.value.trim() || null;
      }
      if (playDom) {
        playbackTarget = playDom.value.trim() || null;
      }
      if (provDom) {
        piProvider = provDom.value.trim() || piProvider;
      }
      if (modDom) {
        piModel = modDom.value.trim() || piModel;
      }
      if (t2Dom) {
        tier2Model = t2Dom.value.trim() || tier2Model;
      }
    }

    return { captureTarget, playbackTarget, piProvider, piModel, tier2Model };
  }

  async runDiagnostic(
    target: DiagnosticTarget,
    options?: { keepRunningAll?: boolean },
  ): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const probeParams = this.#readActiveProbeParams();
    const baseDiag = this.#currentDiagState();
    this.#updateModel({
      diagnostics: {
        ...baseDiag,
        runningTarget: options?.keepRunningAll ? "all" : target,
      },
    });

    try {
      if (target === "microphone") {
        const resp = await this.#postJson("/api/diagnostics/microphone", {
          capture_target: probeParams.captureTarget,
          duration_seconds: 2.0,
        });
        if (resp.ok) {
          const payload = (await resp.json()) as MicDiagnosticResponse;
          this.#updateModel({
            diagnostics: {
              ...this.#currentDiagState(),
              runningTarget: options?.keepRunningAll ? "all" : null,
              microphone: payload,
            },
          });
          return;
        }
      } else if (target === "audio-output") {
        const resp = await this.#postJson("/api/diagnostics/audio-output", {
          playback_target: probeParams.playbackTarget,
        });
        if (resp.ok) {
          const payload = (await resp.json()) as AudioOutputDiagnosticResponse;
          this.#updateModel({
            diagnostics: {
              ...this.#currentDiagState(),
              runningTarget: options?.keepRunningAll ? "all" : null,
              audioOutput: payload,
            },
          });
          return;
        }
      } else if (target === "pi") {
        const resp = await this.#postJson("/api/diagnostics/pi", {
          provider: probeParams.piProvider,
          model: probeParams.piModel,
        });
        if (resp.ok) {
          const payload = (await resp.json()) as PiDiagnosticResponse;
          this.#updateModel({
            diagnostics: {
              ...this.#currentDiagState(),
              runningTarget: options?.keepRunningAll ? "all" : null,
              pi: payload,
            },
          });
          return;
        }
      } else if (target === "antigravity") {
        const resp = await this.#postJson("/api/diagnostics/antigravity", {
          model: probeParams.tier2Model,
        });
        if (resp.ok) {
          const payload = (await resp.json()) as AntigravityDiagnosticResponse;
          this.#updateModel({
            diagnostics: {
              ...this.#currentDiagState(),
              runningTarget: options?.keepRunningAll ? "all" : null,
              antigravity: payload,
            },
          });
          return;
        }
      }
    } catch {
      // Handled by resetting runningTarget below
    }

    if (!options?.keepRunningAll && !this.#disposed) {
      this.#updateModel({
        diagnostics: {
          ...this.#currentDiagState(),
          runningTarget: null,
        },
      });
    }
  }

  async loadAgentCatalog(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    try {
      const resp = await this.#fetch("/api/agents/catalog", {
        method: "GET",
        credentials: "same-origin",
      });
      if (!resp.ok || this.#disposed) {
        return;
      }
      const catalog = (await resp.json()) as AgentCatalogSnapshot;
      this.#updateModel({ agentCatalog: catalog });
    } catch {
      // Non-blocking catalog fetch
    }
  }

  async runAgentDiagnostic(engine: AgentEngineId, tier: "tier1" | "tier2" = "tier1"): Promise<void> {
    if (this.#disposed) {
      return;
    }
    try {
      const resp = await this.#postJson("/api/diagnostics/agent", { engine, tier });
      if (resp.ok && !this.#disposed) {
        const result = (await resp.json()) as AgentDiagnosticResponse;
        this.#updateModel({ agentProbeResult: result });
      }
    } catch {
      // Ignore transient network errors
    }
  }

  async saveProviderCredential(provider: string, envVar: string, value: string): Promise<void> {
    if (this.#disposed || !value.trim()) {
      return;
    }
    try {
      const resp = await this.#fetch("/api/credentials", {
        method: "PUT",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Nova-CSRF": this.#csrfToken,
        },
        body: JSON.stringify({ provider, env_var: envVar, value: value.trim() }),
      });
      if (resp.ok) {
        await this.loadAgentCatalog();
      }
    } catch {
      // Ignore transient network errors
    }
  }

  async deleteProviderCredential(provider: string, envVar: string): Promise<void> {
    if (this.#disposed) {
      return;
    }
    try {
      const resp = await this.#fetch("/api/credentials", {
        method: "DELETE",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Nova-CSRF": this.#csrfToken,
        },
        body: JSON.stringify({ provider, env_var: envVar }),
      });
      if (resp.ok) {
        await this.loadAgentCatalog();
      }
    } catch {
      // Ignore transient network errors
    }
  }

  async runAllDiagnostics(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const order: readonly DiagnosticTarget[] = [
      "microphone",
      "audio-output",
      "pi",
      "antigravity",
    ];
    for (const target of order) {
      if (this.#disposed) {
        return;
      }
      await this.runDiagnostic(target, { keepRunningAll: true });
    }
    if (!this.#disposed) {
      this.#updateModel({
        diagnostics: {
          ...this.#currentDiagState(),
          runningTarget: null,
        },
      });
    }
  }

  async completeOnboarding(audioOverride?: {
    captureTarget?: string;
    playbackTarget?: string;
  }): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const draft = this.#model.onboardingDraftAudio;
    const effectiveAudio =
      audioOverride ??
      (draft !== undefined
        ? {
            captureTarget: draft.captureTarget,
            playbackTarget: draft.playbackTarget,
          }
        : undefined);
    this.#updateModel({ onboardingSaving: true });
    const patch: Record<string, unknown> = {
      general: { first_run_completed: true },
    };
    if (effectiveAudio) {
      patch["audio"] = {
        capture_target: effectiveAudio.captureTarget ?? "",
        playback_target: effectiveAudio.playbackTarget ?? "",
      };
    }
    const resp = await this.#sendJson("PUT", "/api/settings", patch);
    if (!resp.ok) {
      const err = await this.#safeParseError(resp);
      this.#updateModel({
        onboardingSaving: false,
        noticeBanner: resolveHumanNotice(
          err?.error_code ?? "ORCHESTRATOR_UNEXPECTED_FAILURE",
          err?.message,
        ),
      });
      return;
    }
    const payload = (await resp.json()) as SettingsPutResponse;
    this.#updateModel({
      onboardingSaving: false,
      onboardingOpen: false,
      onboardingDraftAudio: undefined,
      settingsData: payload.settings,
      lastMilestone: "Configuración inicial completada.",
    });
    await this.recheckHealth();
  }

  async #fetchModelsStatus(): Promise<ModelsProvisionState | null> {
    if (this.#disposed) {
      return null;
    }
    try {
      const resp = await this.#fetch("/api/models/status", {
        method: "GET",
        credentials: "same-origin",
        signal: this.#abortController.signal,
      });
      if (!resp.ok) {
        return null;
      }
      const data = (await resp.json()) as ModelsProvisionState;
      this.#updateModel({ modelsStatus: data });
      return data;
    } catch {
      return null;
    }
  }

  async recheckHealth(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    this.#updateModel({ healthRechecking: true });
    try {
      const resp = await this.#sendJson("POST", "/api/health/recheck", {});
      if (resp.ok) {
        const data = (await resp.json()) as HealthRecheckResponse;
        const nextMessages = data.health.all_ok
          ? this.#model.messages.filter((m) => m.step_type !== "COMPONENTS_MISSING")
          : this.#model.messages;
        const nextNotice =
          data.health.all_ok && this.#model.noticeBanner?.code === "COMPONENTS_MISSING"
            ? null
            : !data.health.all_ok && data.health.alert_message
              ? resolveHumanNotice("COMPONENTS_MISSING", data.health.alert_message)
              : this.#model.noticeBanner;
        this.#updateModel({
          healthRechecking: false,
          healthReport: data.health,
          voiceEnabled: data.voice_enabled,
          messages: nextMessages,
          noticeBanner: nextNotice,
        });
      } else {
        this.#updateModel({ healthRechecking: false });
      }
      await this.#fetchModelsStatus();
    } catch {
      this.#updateModel({ healthRechecking: false });
    }
  }

  async provisionVoiceModels(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const resp = await this.#sendJson("POST", "/api/models/provision", {});
    if (!resp.ok) {
      const err = await this.#safeParseError(resp);
      this.#updateModel({
        modelsStatus: {
          status: "error",
          all_installed: false,
          total_models: this.#model.modelsStatus?.total_models ?? 0,
          completed_models: this.#model.modelsStatus?.completed_models ?? 0,
          bytes_downloaded: 0,
          total_bytes: this.#model.modelsStatus?.total_bytes ?? 0,
          percent: 0,
          error: err?.message ?? "No se pudo iniciar la descarga de modelos.",
          models: this.#model.modelsStatus?.models ?? [],
        },
      });
      return;
    }
    let state = (await resp.json()) as ModelsProvisionState;
    this.#updateModel({ modelsStatus: state });

    let polls = 0;
    while (!this.#disposed && state.status === "running" && polls < 600) {
      polls += 1;
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, 250);
        this.#abortController.signal.addEventListener(
          "abort",
          () => {
            clearTimeout(timer);
            resolve();
          },
          { once: true },
        );
      });
      if (this.#disposed) {
        return;
      }
      const latest = await this.#fetchModelsStatus();
      if (!latest) {
        break;
      }
      state = latest;
    }

    if (!this.#disposed && state.status === "completed") {
      await this.recheckHealth();
    }
  }

  async #preloadSettingsAndDevices(): Promise<void> {
    try {
      const [settingsResp, audioResp] = await Promise.all([
        this.#fetch("/api/settings", {
          method: "GET",
          credentials: "same-origin",
          signal: this.#abortController.signal,
        }),
        this.#fetch("/api/audio/devices", {
          method: "GET",
          credentials: "same-origin",
          signal: this.#abortController.signal,
        }),
      ]);
      const nextSettings = settingsResp.ok
        ? ((await settingsResp.json()) as SettingsGetResponse).settings
        : (this.#model.settingsData ?? null);
      const nextAudio = audioResp.ok
        ? ((await audioResp.json()) as AudioDeviceCatalog)
        : (this.#model.audioDevices ?? null);
      if (!this.#disposed) {
        this.#updateModel({ settingsData: nextSettings, audioDevices: nextAudio });
      }
    } catch {
      // Non-blocking onboarding preload
    }
  }

  async selectWorkspace(pathOrQuery: string): Promise<void> {
    const cleaned = pathOrQuery.trim();
    if (!cleaned || this.#disposed) {
      return;
    }
    const resp = await this.#postJson("/api/workspace/select", { path_or_query: cleaned });
    if (!resp.ok) {
      const errBody = await this.#safeParseError(resp);
      const code = errBody?.error_code ?? "ORCHESTRATOR_UNEXPECTED_FAILURE";
      this.#updateModel({
        noticeBanner: resolveHumanNotice(code),
        errorBanner: null,
      });
      return;
    }
    const data = (await resp.json()) as {
      cancelled: boolean;
      workspace: UiWorkspaceView | null;
    };
    if (!data.cancelled && data.workspace) {
      this.#updateModel({
        activeWorkspace: data.workspace,
        pickerFallbackOpen: false,
        errorBanner: null,
        noticeBanner: null,
      });
    }
  }

  async pickNativeWorkspace(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const resp = await this.#postJson("/api/workspace/pick-native", {});
    if (resp.status === 503) {
      this.#updateModel({
        pickerFallbackOpen: true,
        errorBanner: null,
      });
      if (this.#root) {
        const selectEl = this.#root.querySelector<HTMLSelectElement>("#nova-workspace-select");
        selectEl?.focus();
      }
      return;
    }
    if (!resp.ok) {
      const errBody = await this.#safeParseError(resp);
      const code = errBody?.error_code ?? "ORCHESTRATOR_UNEXPECTED_FAILURE";
      this.#updateModel({
        noticeBanner: resolveHumanNotice(code),
        errorBanner: null,
      });
      return;
    }
    const data = (await resp.json()) as {
      cancelled: boolean;
      workspace: UiWorkspaceView | null;
    };
    if (!data.cancelled && data.workspace) {
      this.#updateModel({
        activeWorkspace: data.workspace,
        pickerFallbackOpen: false,
        errorBanner: null,
        noticeBanner: null,
      });
    }
  }

  async resolveApproval(auditId: string, approved: boolean): Promise<void> {
    if (this.#disposed || !auditId) {
      return;
    }
    const encoded = encodeURIComponent(auditId);
    const resp = await this.#postJson(`/api/approvals/${encoded}/resolve`, { approved });
    if (resp.ok || resp.status === 404) {
      const remaining = this.#model.pendingApprovals.filter((item) => item.audit_id !== auditId);
      this.#updateModel({ pendingApprovals: remaining, errorBanner: null });
      return;
    }
    const errBody = await this.#safeParseError(resp);
    this.#updateModel({
      errorBanner: errBody?.message ?? "No se pudo resolver la solicitud de aprobación.",
    });
  }

  async openSettingsDrawer(): Promise<void> {
    if (this.#disposed) {
      return;
    }
    void this.loadAgentCatalog();
    this.#updateModel({
      settingsDrawerOpen: true,
      settingsLoading: true,
      settingsError: null,
      settingsNotice: null,
      shortcutStatus: null,
    });
    try {
      const [settingsResp, audioResp] = await Promise.all([
        this.#fetch("/api/settings", {
          method: "GET",
          credentials: "same-origin",
          signal: this.#abortController.signal,
        }),
        this.#fetch("/api/audio/devices", {
          method: "GET",
          credentials: "same-origin",
          signal: this.#abortController.signal,
        }),
      ]);
      if (!settingsResp.ok) {
        const err = await this.#safeParseError(settingsResp);
        this.#updateModel({
          settingsLoading: false,
          settingsError: err?.message ?? "No se pudo cargar la configuración.",
        });
        return;
      }
      const settingsPayload = (await settingsResp.json()) as SettingsGetResponse;
      const audioCatalog = audioResp.ok
        ? ((await audioResp.json()) as AudioDeviceCatalog)
        : (this.#model.audioDevices ?? null);
      this.#updateModel({
        settingsLoading: false,
        settingsData: settingsPayload.settings,
        audioDevices: audioCatalog,
      });
    } catch {
      if (!this.#disposed) {
        this.#updateModel({
          settingsLoading: false,
          settingsError: "Error de conexión al cargar los ajustes.",
        });
      }
    }
  }

  closeSettingsDrawer(): void {
    if (this.#disposed) {
      return;
    }
    this.#updateModel({
      settingsDrawerOpen: false,
      settingsError: null,
      settingsNotice: null,
    });
  }

  async saveSettings(patch: Record<string, unknown>): Promise<void> {
    if (this.#disposed) {
      return;
    }
    this.#updateModel({ settingsSaving: true, settingsError: null, settingsNotice: null });
    const resp = await this.#sendJson("PUT", "/api/settings", patch);
    if (!resp.ok) {
      const err = await this.#safeParseError(resp);
      this.#updateModel({
        settingsSaving: false,
        settingsError: err?.message ?? "No se pudieron guardar los ajustes.",
      });
      return;
    }
    const payload = (await resp.json()) as SettingsPutResponse;
    this.#updateModel({
      settingsSaving: false,
      settingsData: payload.settings,
      settingsDrawerOpen: payload.requires_restart ? true : false,
      settingsNotice: payload.requires_restart
        ? `Guardado. Algunos cambios aplican al reiniciar (${payload.restart_fields.join(", ")}).`
        : null,
      lastMilestone: payload.requires_restart ? null : "Ajustes guardados y aplicados.",
    });
  }

  async registerGlobalShortcut(shortcut: string): Promise<void> {
    if (this.#disposed) {
      return;
    }
    const resp = await this.#postJson("/api/settings/shortcut", { shortcut });
    if (!resp.ok) {
      const err = await this.#safeParseError(resp);
      this.#updateModel({
        settingsError: err?.message ?? "No se pudo registrar el atajo global.",
      });
      return;
    }
    const sc = (await resp.json()) as ShortcutRegisterResponse;
    this.#updateModel({ settingsError: null, shortcutStatus: sc });
  }

  async triggerPtt(action: "start" | "stop" | "toggle"): Promise<void> {
    if (this.#disposed || !this.#model.voiceEnabled) {
      return;
    }
    const resp = await this.#postJson("/api/call/ptt", { action });
    if (!resp.ok) {
      const err = await this.#safeParseError(resp);
      this.#updateModel({
        errorBanner: err?.message ?? "No se pudo ejecutar Push-to-Talk.",
      });
    }
  }

  async #postJson(url: string, body: Record<string, unknown>): Promise<Response> {
    return this.#sendJson("POST", url, body);
  }

  async #sendJson(
    method: "POST" | "PUT",
    url: string,
    body: Record<string, unknown>,
  ): Promise<Response> {
    return this.#fetch(url, {
      method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-Nova-CSRF": this.#csrfToken,
      },
      body: JSON.stringify(body),
      signal: this.#abortController.signal,
    });
  }

  async #safeParseError(resp: Response): Promise<ErrorResponse | null> {
    try {
      return (await resp.json()) as ErrorResponse;
    } catch {
      return null;
    }
  }

  async #consumeSseLoop(): Promise<void> {
    while (!this.#disposed && !this.#abortController.signal.aborted) {
      try {
        const resp = await this.#fetch("/api/events", {
          method: "GET",
          credentials: "same-origin",
          headers: { Accept: "text/event-stream" },
          signal: this.#abortController.signal,
        });
        if (!resp.ok || !resp.body) {
          break;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (!this.#disposed) {
          const { value, done } = await reader.read();
          if (done) {
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          let boundaryIdx = buffer.indexOf("\n\n");
          while (boundaryIdx !== -1) {
            const rawFrame = buffer.slice(0, boundaryIdx);
            buffer = buffer.slice(boundaryIdx + 2);
            this.#handleSseFrame(rawFrame);
            boundaryIdx = buffer.indexOf("\n\n");
          }
        }
      } catch {
        if (this.#disposed || this.#abortController.signal.aborted) {
          return;
        }
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, 1000);
          this.#abortController.signal.addEventListener(
            "abort",
            () => {
              clearTimeout(timer);
              resolve();
            },
            { once: true },
          );
        });
      }
    }
  }

  #handleSseFrame(rawFrame: string): void {
    let eventName = "message";
    const dataLines: string[] = [];

    for (const line of rawFrame.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) {
        continue;
      }
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }

    if (dataLines.length === 0) {
      return;
    }
    const rawJson = dataLines.join("\n");
    try {
      if (eventName === "snapshot") {
        const snap = JSON.parse(rawJson) as UiStateSnapshot;
        this.#applySnapshot(snap);
      } else if (eventName === "nova_event") {
        const env = JSON.parse(rawJson) as UiEventEnvelope;
        this.#applyEnvelope(env);
      }
    } catch {
      // Ignore malformed frame without crashing UI stream loop
    }
  }

  #applySnapshot(snap: UiStateSnapshot): void {
    if (snap.csrf_token) {
      this.#csrfToken = snap.csrf_token;
    }
    const capped = snap.messages.slice(-MAX_CLIENT_MESSAGES);
    const nextStatus: ViewStatus = capped.length > 0 ? "success" : "empty";
    const nextOnboarding =
      snap.onboarding_required === true
        ? true
        : snap.onboarding_required === false
          ? false
          : Boolean(this.#model.onboardingOpen);
    this.#updateModel({
      status: nextStatus,
      voiceEnabled: snap.voice_enabled,
      onboardingOpen: nextOnboarding,
      processCwd: snap.process_cwd ?? this.#model.processCwd ?? "",
      allowedRoots: snap.allowed_roots ?? this.#model.allowedRoots ?? [],
      callSession: snap.call_session,
      activeWorkspace: snap.active_workspace,
      availableWorkspaces: snap.available_workspaces,
      pendingApprovals: snap.pending_approvals,
      messages: capped,
      errorBanner: null,
    });
  }

  #applyEnvelope(env: UiEventEnvelope): void {
    const p = env.payload;

    if (env.event_type === "workspace_changed") {
      const rawProjects = Array.isArray(p["projects"]) ? (p["projects"] as UiProjectView[]) : [];
      const rawTopo = p["topology"] === "multi-project" ? "multi-project" : "single-repo";
      const ws: UiWorkspaceView = {
        display_name: String(p["display_name"] ?? ""),
        canonical_path: String(p["canonical_path"] ?? ""),
        selected_via: (p["selected_via"] as UiWorkspaceView["selected_via"]) ?? "exact_path",
        topology: rawTopo,
        projects: rawProjects,
      };
      this.#updateModel({
        activeWorkspace: ws,
        pickerFallbackOpen: false,
        initWorkspaceOpen: false,
      });
      return;
    }

    if (env.event_type === "workspace_cleared") {
      this.#updateModel({
        activeWorkspace: null,
        pickerFallbackOpen: false,
        initWorkspaceOpen: false,
        processCwd: String(p["process_cwd"] ?? this.#model.processCwd ?? ""),
      });
      return;
    }

    if (env.event_type === "health_updated") {
      const allOk = Boolean(p["all_ok"]);
      const voiceEnabled =
        typeof p["voice_enabled"] === "boolean"
          ? p["voice_enabled"]
          : this.#model.voiceEnabled;
      const nextMessages = allOk
        ? this.#model.messages.filter((m) => m.step_type !== "COMPONENTS_MISSING")
        : this.#model.messages;
      const nextNotice =
        allOk && this.#model.noticeBanner?.code === "COMPONENTS_MISSING"
          ? null
          : !allOk && typeof p["alert_message"] === "string" && p["alert_message"]
            ? resolveHumanNotice("COMPONENTS_MISSING", p["alert_message"])
            : this.#model.noticeBanner;
      const rawCall = p["call_session"] as UiCallSessionView | undefined;
      if (rawCall && typeof rawCall === "object" && typeof rawCall.state === "string") {
        this.#updateModel({
          voiceEnabled,
          messages: nextMessages,
          noticeBanner: nextNotice,
          callSession: rawCall,
        });
      } else {
        this.#updateModel({
          voiceEnabled,
          messages: nextMessages,
          noticeBanner: nextNotice,
        });
      }
      return;
    }

    if (env.event_type === "call_state_changed") {
      const nextState = (p["state"] as PresentationalAppModel["callSession"]["state"]) ?? "standby";
      const turnCount =
        typeof p["turn_count"] === "number" ? p["turn_count"] : this.#model.callSession.turn_count;
      const clearMilestone = nextState === "standby" || nextState === "closed";
      this.#updateModel({
        callSession: {
          call_id: String(p["call_id"] ?? this.#model.callSession.call_id),
          state: nextState,
          turn_count: turnCount,
          active_correlation_id: env.correlation_id,
        },
        ...(clearMilestone ? { lastMilestone: null } : {}),
      });
      this.#resetSleepTimer();
      return;
    }

    if (env.event_type === "chat_stream") {
      const messageId = String(p["message_id"] ?? `msg-${env.seq}`);
      const delta = String(p["delta"] ?? "");
      const source = p["source"] === "tier2_runner" ? "tier2_runner" : "tier1_pi";
      const stepType = String(p["step_type"] ?? "agent_response");

      const nextMessages = [...this.#model.messages];
      const existingIdx = nextMessages.findIndex((m) => m.message_id === messageId);
      if (existingIdx >= 0) {
        const prev = nextMessages[existingIdx];
        if (prev) {
          nextMessages[existingIdx] = {
            ...prev,
            content: prev.content + delta,
            streaming: true,
          };
        }
      } else {
        nextMessages.push({
          message_id: messageId,
          correlation_id: env.correlation_id,
          role: "assistant",
          modality: source,
          step_type: stepType,
          content: delta,
          timestamp: env.timestamp,
          streaming: true,
        });
      }
      const bounded = nextMessages.slice(-MAX_CLIENT_MESSAGES);
      this.#updateModel({ status: "success", messages: bounded });
      return;
    }

    if (env.event_type === "voice_milestone") {
      const summary = String(p["summary_text"] ?? "");
      const closedStreams = this.#model.messages.map((m) =>
        m.correlation_id === env.correlation_id && m.streaming ? { ...m, streaming: false } : m,
      );
      this.#updateModel({
        messages: closedStreams,
        lastMilestone: summary || this.#model.lastMilestone,
      });
      return;
    }

    if (env.event_type === "voice_transcription") {
      const msg = p as unknown as UiChatMessage;
      this.#appendMessage(msg);
      return;
    }

    if (env.event_type === "runner_failure") {
      const errorCode = String(p["error_code"] ?? "ORCHESTRATOR_UNEXPECTED_FAILURE");
      const customMsg = typeof p["sanitized_message"] === "string" ? p["sanitized_message"] : "";
      this.#updateModel({ noticeBanner: resolveHumanNotice(errorCode, customMsg) });
      return;
    }

    if (env.event_type === "command_blocked") {
      const nested = p["message"] as UiChatMessage | undefined;
      if (nested) {
        this.#appendMessage(nested);
      }
      return;
    }

    if (env.event_type === "command_approval_requested") {
      const item = p as unknown as UiApprovalItem;
      const filtered = this.#model.pendingApprovals.filter((a) => a.audit_id !== item.audit_id);
      this.#updateModel({ pendingApprovals: [...filtered, item] });
      return;
    }

    if (env.event_type === "command_approval_resolved") {
      const auditId = String(p["audit_id"] ?? "");
      const remaining = this.#model.pendingApprovals.filter((a) => a.audit_id !== auditId);
      this.#updateModel({ pendingApprovals: remaining });
      return;
    }

    if (env.event_type === "settings_updated") {
      if (this.#model.settingsDrawerOpen) {
        void this.openSettingsDrawer();
      }
      return;
    }

    if (env.event_type === "panel_toggle_requested") {
      if (this.#model.uiMode === "orb") {
        void this.toggleUiMode();
      }
    }
  }

  #appendMessage(msg: UiChatMessage): void {
    const next = [...this.#model.messages, msg].slice(-MAX_CLIENT_MESSAGES);
    this.#updateModel({ status: "success", messages: next });
    this.#resetSleepTimer();
  }

  #resetSleepTimer(): void {
    if (this.#sleepTimer) {
      clearTimeout(this.#sleepTimer);
      this.#sleepTimer = null;
    }
    const state = this.#model.callSession.state;
    if (state !== "standby" && state !== "closed") {
      this.#sleepTimer = setTimeout(() => {
        if (this.#disposed) {
          return;
        }
        if (this.#model.callSession.state !== "standby" && this.#model.callSession.state !== "closed") {
          void this.triggerCallAction("end");
        }
      }, 12000);
    }
  }

  #updateModel(patch: Partial<PresentationalAppModel>): void {
    this.#model = { ...this.#model, ...patch };
    this.#render();
  }

  #render(): void {
    const fullHtml = renderAppShell(this.#model);
    if (this.#onRender) {
      this.#onRender(fullHtml, this.#model);
    }
    if (!this.#root) {
      return;
    }
    if (!this.#shellMounted || this.#model.uiMode === "orb") {
      this.#root.innerHTML = fullHtml;
      this.#shellMounted = this.#model.uiMode !== "orb";
      return;
    }
    const activeTab = this.#model.activeTab ?? "chat";
    const currentLayoutEl = this.#root.querySelector<HTMLElement>(".nova-layout");
    if (currentLayoutEl) {
      const mountedTab = currentLayoutEl.dataset["activeTab"] ?? "chat";
      if (mountedTab !== activeTab) {
        this.#root.innerHTML = fullHtml;
        return;
      }
    }
    const overview = this.#model.agentsOverview ?? getDefaultAgentsOverview(this.#model.activeWorkspace?.canonical_path);
    const activeTaskCount = overview.tasks.filter((t) => t.status === "running" || t.status === "gatekeeper_blocked").length;

    const headerSlot = this.#root.querySelector<HTMLElement>('[data-slot="header"]');
    if (headerSlot) {
      headerSlot.innerHTML = renderWorkspaceBar(
        this.#model.activeWorkspace,
        this.#model.availableWorkspaces,
        this.#model.pickerFallbackOpen,
        this.#model.callSession.state,
        activeTab,
        activeTaskCount,
      );
    }
    if (activeTab === "agents") {
      const agentsSlot = this.#root.querySelector<HTMLElement>('[data-slot="agents-view"]');
      if (agentsSlot) {
        agentsSlot.innerHTML = renderAgentsMatrixView(this.#model);
      }
    } else {
      const timelineSlot = this.#root.querySelector<HTMLElement>('[data-slot="timeline"]');
      if (timelineSlot) {
        timelineSlot.innerHTML = renderChatTimeline(
          this.#model.status,
          this.#model.messages,
          this.#model.errorBanner,
          this.#model.activeWorkspace,
          this.#model.availableWorkspaces,
          Boolean(this.#model.initWorkspaceOpen),
          this.#model.allowedRoots ?? [],
        );
        timelineSlot.scrollTop = timelineSlot.scrollHeight;
      }
      const approvalsSlot = this.#root.querySelector<HTMLElement>('[data-slot="approvals"]');
      if (approvalsSlot) {
        approvalsSlot.innerHTML = renderApprovalBanner(this.#model.pendingApprovals);
      }
    }
    const milestoneSlot = this.#root.querySelector<HTMLElement>('[data-slot="milestone"]');
    if (milestoneSlot) {
      milestoneSlot.innerHTML = this.#model.lastMilestone
        ? `<p class="nova-milestone-toast" aria-live="polite">${escapeHtml(this.#model.lastMilestone)}</p>`
        : "";
    }
    const noticeSlot = this.#root.querySelector<HTMLElement>('[data-slot="notice"]');
    if (noticeSlot) {
      noticeSlot.outerHTML = renderNoticeSlot(this.#model);
    }
    const inCall =
      this.#model.callSession.state !== "standby" && this.#model.callSession.state !== "closed";
    const dockSlot = this.#root.querySelector<HTMLElement>('[data-slot="dock"]');
    if (dockSlot) {
      const currentDockMode = dockSlot.dataset["dockMode"];
      const targetDockMode = inCall ? "call" : "standby";
      if (currentDockMode !== targetDockMode) {
        dockSlot.dataset["dockMode"] = targetDockMode;
        dockSlot.innerHTML = renderDock(this.#model);
      } else if (targetDockMode === "call") {
        dockSlot.innerHTML = renderDock(this.#model);
      } else {
        const inputEl = dockSlot.querySelector<HTMLTextAreaElement>("#nova-chat-input");
        const preservedValue = inputEl ? inputEl.value : undefined;
        const preservedSelectionStart = inputEl ? inputEl.selectionStart : undefined;
        const preservedSelectionEnd = inputEl ? inputEl.selectionEnd : undefined;

        dockSlot.innerHTML = renderDock(this.#model);

        if (preservedValue !== undefined) {
          const restoredInput = dockSlot.querySelector<HTMLTextAreaElement>("#nova-chat-input");
          if (restoredInput) {
            restoredInput.value = preservedValue;
            if (
              typeof preservedSelectionStart === "number" &&
              typeof preservedSelectionEnd === "number" &&
              typeof restoredInput.setSelectionRange === "function"
            ) {
              try {
                restoredInput.setSelectionRange(preservedSelectionStart, preservedSelectionEnd);
              } catch {
                // Ignore in headless / test environments
              }
            }
          }
        }
      }
    }
    const layoutEl = this.#root.querySelector<HTMLElement>(".nova-layout");
    if (layoutEl) {
      layoutEl.dataset["uiMode"] = this.#model.uiMode ?? "panel";
      layoutEl.dataset["callState"] = this.#model.callSession.state;
      layoutEl.dataset["activeTab"] = this.#model.activeTab ?? "chat";
    }
    const settingsSlot = this.#root.querySelector<HTMLElement>('[data-slot="settings-drawer"]');
    if (settingsSlot) {
      const preserveSettingsDraft =
        Boolean(this.#model.settingsDrawerOpen) &&
        !this.#model.settingsLoading &&
        this.#lastRenderedSettingsRef === this.#model.settingsData;
      const settingsDraft = preserveSettingsDraft
        ? snapshotFormFields(settingsSlot, SETTINGS_DRAFT_FIELDS)
        : null;
      settingsSlot.innerHTML = renderSettingsDrawer(this.#model);
      this.#lastRenderedSettingsRef = this.#model.settingsData;
      if (settingsDraft && settingsDraft.size > 0) {
        restoreFormFields(settingsSlot, settingsDraft);
      }
    }
    const onboardingSlot = this.#root.querySelector<HTMLElement>(
      '[data-slot="onboarding-container"]',
    );
    if (onboardingSlot) {
      const onboardingDraft =
        this.#model.onboardingOpen && this.#model.onboardingDraftAudio === undefined
          ? snapshotFormFields(onboardingSlot, ONBOARDING_DRAFT_FIELDS)
          : null;
      onboardingSlot.innerHTML = renderOnboardingModal(this.#model);
      if (onboardingDraft && onboardingDraft.size > 0) {
        restoreFormFields(onboardingSlot, onboardingDraft);
      }
    }
  }

  #bindDomEvents(): void {
    const root = this.#root;
    if (!root) {
      return;
    }
    const sig = this.#abortController.signal;

    root.addEventListener(
      "click",
      (ev) => {
        const target = (ev.target as HTMLElement | null)?.closest<HTMLElement>("[data-action]");
        if (!target) {
          return;
        }
        const action = target.dataset["action"];
        if (action === "switch-tab") {
          const tab = (target.dataset["tab"] as "chat" | "agents") || "chat";
          this.switchTab(tab);
        } else if (action === "filter-agents") {
          const filter = (target.dataset["filter"] as AgentsFilterKey) || "all";
          this.setAgentsFilter(filter);
        } else if (action === "approve-task") {
          const taskId = target.dataset["taskId"] ?? "";
          void this.handleAgentTaskAction(taskId, "approve");
        } else if (action === "cancel-task") {
          const taskId = target.dataset["taskId"] ?? "";
          void this.handleAgentTaskAction(taskId, "cancel");
        } else if (action === "pause-task") {
          const taskId = target.dataset["taskId"] ?? "";
          void this.handleAgentTaskAction(taskId, "pause");
        } else if (action === "inspect-task") {
          const taskId = target.dataset["taskId"] ?? "";
          void this.handleAgentTaskAction(taskId, "inspect");
        } else if (action === "toggle-ui-mode") {
          void this.toggleUiMode();
        } else if (action === "quit-app") {
          void this.quitApplication();
        } else if (action === "call-start") {
          void this.triggerCallAction("start");
        } else if (action === "call-end") {
          void this.triggerCallAction("end");
        } else if (action === "call-barge-in") {
          void this.triggerCallAction("barge-in");
        } else if (action === "pick-native") {
          void this.pickNativeWorkspace();
        } else if (action === "select-workspace-option") {
          const canonicalPath = target.dataset["canonicalPath"] ?? "";
          if (canonicalPath) {
            void this.selectWorkspace(canonicalPath);
          }
        } else if (action === "dismiss-notice") {
          this.dismissNotice();
        } else if (action === "approve-command") {
          const auditId = target.dataset["auditId"] ?? "";
          void this.resolveApproval(auditId, true);
        } else if (action === "reject-command") {
          const auditId = target.dataset["auditId"] ?? "";
          void this.resolveApproval(auditId, false);
        } else if (action === "open-settings") {
          void this.openSettingsDrawer();
        } else if (action === "close-settings") {
          this.closeSettingsDrawer();
        } else if (action === "register-shortcut") {
          const shortcutInput = root.querySelector<HTMLInputElement>(
            '[name="shortcuts.global_shortcut"]',
          );
          const shortcut = shortcutInput?.value.trim() || "Super+Shift+Space";
          void this.registerGlobalShortcut(shortcut);
        } else if (action === "clear-workspace") {
          void this.clearWorkspace();
        } else if (action === "toggle-init-workspace") {
          this.toggleInitWorkspaceForm();
        } else if (action === "diag-microphone") {
          void this.runDiagnostic("microphone");
        } else if (action === "diag-audio-output") {
          void this.runDiagnostic("audio-output");
        } else if (action === "diag-pi") {
          void this.runDiagnostic("pi");
        } else if (action === "diag-antigravity") {
          void this.runDiagnostic("antigravity");
        } else if (action === "diag-run-all") {
          void this.runAllDiagnostics();
        } else if (action === "probe-agent") {
          const eng = target.dataset["engine"] as AgentEngineId | undefined;
          const tier = target.dataset["tier"] === "tier2" ? "tier2" : "tier1";
          if (eng) {
            void this.runAgentDiagnostic(eng, tier);
          }
        } else if (action === "save-credential") {
          const prov = target.dataset["provider"] ?? "";
          const envVar = target.dataset["envVar"] ?? "";
          const inp = root.querySelector<HTMLInputElement>(`[data-cred-input="${envVar}"]`);
          if (prov && envVar && inp && inp.value.trim()) {
            const val = inp.value;
            inp.value = "";
            void this.saveProviderCredential(prov, envVar, val);
          }
        } else if (action === "delete-credential") {
          const prov = target.dataset["provider"] ?? "";
          const envVar = target.dataset["envVar"] ?? "";
          if (prov && envVar) {
            void this.deleteProviderCredential(prov, envVar);
          }
        } else if (action === "provision-models") {
          void this.provisionVoiceModels();
        } else if (action === "recheck-health") {
          void this.recheckHealth();
        } else if (action === "complete-onboarding") {
          const capEl = root.querySelector<HTMLSelectElement>(
            '[name="onboarding.capture_target"]',
          );
          const playEl = root.querySelector<HTMLSelectElement>(
            '[name="onboarding.playback_target"]',
          );
          const audioOverride =
            capEl || playEl
              ? {
                  captureTarget: capEl?.value ?? "",
                  playbackTarget: playEl?.value ?? "",
                }
              : undefined;
          void this.completeOnboarding(audioOverride);
        }
      },
      { signal: sig },
    );

    root.addEventListener(
      "pointerdown",
      (ev) => {
        const pttBtn = (ev.target as HTMLElement | null)?.closest<HTMLButtonElement>(
          '[data-action="ptt-hold"]',
        );
        if (!pttBtn || pttBtn.disabled) {
          return;
        }
        this.#pttHolding = true;
        void this.triggerPtt("start");
      },
      { signal: sig },
    );

    const stopPttHold = (): void => {
      if (!this.#pttHolding) {
        return;
      }
      this.#pttHolding = false;
      void this.triggerPtt("stop");
    };
    root.addEventListener("pointerup", stopPttHold, { signal: sig });
    root.addEventListener("pointercancel", stopPttHold, { signal: sig });

    root.addEventListener(
      "change",
      (ev) => {
        const target = ev.target as HTMLSelectElement | null;
        if (target?.dataset["action"] === "select-workspace-dropdown" && target.value) {
          void this.selectWorkspace(target.value);
        } else if (target?.dataset["action"] === "onboarding-audio-select") {
          const changedName = target.name || target.getAttribute?.("name") || "";
          const capEl = root.querySelector<HTMLSelectElement>(
            '[name="onboarding.capture_target"]',
          );
          const playEl = root.querySelector<HTMLSelectElement>(
            '[name="onboarding.playback_target"]',
          );
          const nextCapture =
            changedName === "onboarding.capture_target"
              ? target.value
              : (capEl?.value ??
                this.#model.onboardingDraftAudio?.captureTarget ??
                this.#model.settingsData?.audio.capture_target ??
                "");
          const nextPlayback =
            changedName === "onboarding.playback_target"
              ? target.value
              : (playEl?.value ??
                this.#model.onboardingDraftAudio?.playbackTarget ??
                this.#model.settingsData?.audio.playback_target ??
                "");
          this.#updateModel({
            onboardingDraftAudio: {
              captureTarget: nextCapture,
              playbackTarget: nextPlayback,
            },
          });
        }
      },
      { signal: sig },
    );

    root.addEventListener(
      "submit",
      (ev) => {
        const initWsForm = (ev.target as HTMLElement | null)?.closest<HTMLFormElement>(
          '[data-action="init-workspace-form"]',
        );
        if (initWsForm) {
          ev.preventDefault();
          const fd = new FormData(initWsForm);
          const parentRoot = String(fd.get("parent_root") ?? "").trim();
          const workspaceSlug = String(fd.get("workspace_slug") ?? "").trim();
          const displayName = String(fd.get("display_name") ?? "").trim();
          const rawProjects = String(fd.get("projects") ?? "");
          const projects = rawProjects
            .split(",")
            .map((x) => x.trim())
            .filter((x) => x.length > 0);
          void this.initializeWorkspace({
            parentRoot,
            workspaceSlug,
            displayName: displayName || undefined,
            projects,
          });
          return;
        }

        const settingsForm = (ev.target as HTMLElement | null)?.closest<HTMLFormElement>(
          '[data-action="settings-form"]',
        );
        if (settingsForm) {
          ev.preventDefault();
          const fd = new FormData(settingsForm);
          const patch: Record<string, unknown> = {
            general: {
              remember_workspace: fd.get("general.remember_workspace") === "on",
            },
            audio: {
              capture_target: String(fd.get("audio.capture_target") ?? ""),
              playback_target: String(fd.get("audio.playback_target") ?? ""),
            },
            voice: {
              sensitivity_preset: String(fd.get("voice.sensitivity_preset") ?? "normal"),
              tts_speed: Number(fd.get("voice.tts_speed") ?? 1.1),
              tts_engine: String(fd.get("voice.tts_engine") ?? "kokoro"),
              whisper_language: String(fd.get("voice.whisper_language") ?? ""),
              whisper_device: String(fd.get("voice.whisper_device") ?? "cuda"),
              whisper_model: String(fd.get("voice.whisper_model") ?? "small"),
            },
            models: {
              tier1_engine: String(fd.get("models.tier1_engine") ?? "auto").trim(),
              tier2_engine: String(fd.get("models.tier2_engine") ?? "auto").trim(),
              single_engine_mode: fd.get("models.single_engine_mode") === "on",
              tier1_provider: String(fd.get("models.pi_provider") ?? "").trim(),
              tier1_model: String(fd.get("models.pi_model") ?? "").trim(),
              pi_provider: String(fd.get("models.pi_provider") ?? "").trim(),
              pi_model: String(fd.get("models.pi_model") ?? "").trim(),
              tier2_model: String(fd.get("models.tier2_model") ?? "").trim(),
            },
            shortcuts: {
              ptt_mode: String(fd.get("shortcuts.ptt_mode") ?? "single_turn"),
              global_shortcut: String(
                fd.get("shortcuts.global_shortcut") ?? "Super+Shift+Space",
              ).trim(),
            },
          };
          void this.saveSettings(patch);
          return;
        }
        const form = (ev.target as HTMLElement | null)?.closest<HTMLFormElement>(
          '[data-action="chat-form"]',
        );
        if (!form) {
          return;
        }
        ev.preventDefault();
        const input = form.querySelector<HTMLTextAreaElement>("#nova-chat-input");
        if (input && input.value.trim()) {
          const text = input.value;
          input.value = "";
          void this.submitChat(text);
        }
      },
      { signal: sig },
    );

    root.addEventListener(
      "keydown",
      (ev) => {
        if (ev.key === "Escape") {
          void this.handleEscapeKey();
          return;
        }
        const target = ev.target as HTMLElement | null;
        if (
          target?.id === "nova-chat-input" &&
          ev.key === "Enter" &&
          !ev.shiftKey &&
          !ev.isComposing
        ) {
          ev.preventDefault();
          const form = target.closest("form");
          form?.requestSubmit();
        }
      },
      { signal: sig },
    );
  }
}
