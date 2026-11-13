/**
 * Container Layer: DesktopAppController (profiles/frontend-web.md#1,2,3,6,9).
 * Holds CSRF token strictly in private JS memory, manages SSE + fetch lifecycle via
 * AbortController, and delegates rendering to pure components.ts functions.
 */
import { escapeHtml, renderAppShell, renderApprovalBanner, renderChatTimeline, renderDock, renderNoticeSlot, renderWorkspaceBar, resolveHumanNotice, } from "./components.js";
const MAX_CLIENT_MESSAGES = 200;
export class DesktopAppController {
    #csrfToken = "";
    #abortController = new AbortController();
    #disposed = false;
    #shellMounted = false;
    #sleepTimer = null;
    #fetch;
    #root;
    #onRender;
    #model = {
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
    };
    constructor(options = {}) {
        this.#root = options.rootElement ?? null;
        this.#fetch = options.fetchImpl ?? ((url, init) => globalThis.fetch(url, init));
        this.#onRender = options.onRender;
    }
    get model() {
        return this.#model;
    }
    async start() {
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
            const snapshot = (await resp.json());
            this.#applySnapshot(snapshot);
            void this.#consumeSseLoop();
        }
        catch (err) {
            if (this.#abortController.signal.aborted) {
                return;
            }
            const msg = err instanceof Error ? err.message : "Fallo de conexión con el puente local.";
            this.#updateModel({ status: "error", errorBanner: msg });
        }
    }
    dispose() {
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
    dismissNotice() {
        if (this.#disposed) {
            return;
        }
        this.#updateModel({ noticeBanner: null, errorBanner: null });
    }
    async submitChat(utterance) {
        const trimmed = utterance.trim();
        if (!trimmed || this.#disposed || !this.#model.activeWorkspace) {
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
        }
        else if (this.#model.noticeBanner !== null || this.#model.errorBanner !== null) {
            this.#updateModel({ noticeBanner: null, errorBanner: null });
        }
    }
    async triggerCallAction(action) {
        if (this.#disposed) {
            return;
        }
        const inCall = this.#model.callSession.state !== "standby" && this.#model.callSession.state !== "closed";
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
    async toggleUiMode() {
        if (this.#disposed) {
            return;
        }
        const nextMode = (this.#model.uiMode ?? "panel") === "orb" ? "panel" : "orb";
        this.#updateModel({ uiMode: nextMode });
    }
    async handleEscapeKey() {
        if (this.#disposed) {
            return;
        }
        const inCall = this.#model.callSession.state !== "standby" && this.#model.callSession.state !== "closed";
        if (inCall) {
            await this.triggerCallAction("barge-in");
            return;
        }
        if ((this.#model.uiMode ?? "panel") === "orb") {
            this.#updateModel({ uiMode: "panel" });
        }
    }
    async selectWorkspace(pathOrQuery) {
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
        const data = (await resp.json());
        if (!data.cancelled && data.workspace) {
            this.#updateModel({
                activeWorkspace: data.workspace,
                pickerFallbackOpen: false,
                errorBanner: null,
                noticeBanner: null,
            });
        }
    }
    async pickNativeWorkspace() {
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
                const selectEl = this.#root.querySelector("#nova-workspace-select");
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
        const data = (await resp.json());
        if (!data.cancelled && data.workspace) {
            this.#updateModel({
                activeWorkspace: data.workspace,
                pickerFallbackOpen: false,
                errorBanner: null,
                noticeBanner: null,
            });
        }
    }
    async resolveApproval(auditId, approved) {
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
    async #postJson(url, body) {
        return this.#fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-Nova-CSRF": this.#csrfToken,
            },
            body: JSON.stringify(body),
            signal: this.#abortController.signal,
        });
    }
    async #safeParseError(resp) {
        try {
            return (await resp.json());
        }
        catch {
            return null;
        }
    }
    async #consumeSseLoop() {
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
            }
            catch {
                if (this.#disposed || this.#abortController.signal.aborted) {
                    return;
                }
                await new Promise((resolve) => {
                    const timer = setTimeout(resolve, 1000);
                    this.#abortController.signal.addEventListener("abort", () => {
                        clearTimeout(timer);
                        resolve();
                    }, { once: true });
                });
            }
        }
    }
    #handleSseFrame(rawFrame) {
        let eventName = "message";
        const dataLines = [];
        for (const line of rawFrame.split(/\r?\n/)) {
            if (!line || line.startsWith(":")) {
                continue;
            }
            if (line.startsWith("event:")) {
                eventName = line.slice(6).trim();
            }
            else if (line.startsWith("data:")) {
                dataLines.push(line.slice(5).trimStart());
            }
        }
        if (dataLines.length === 0) {
            return;
        }
        const rawJson = dataLines.join("\n");
        try {
            if (eventName === "snapshot") {
                const snap = JSON.parse(rawJson);
                this.#applySnapshot(snap);
            }
            else if (eventName === "nova_event") {
                const env = JSON.parse(rawJson);
                this.#applyEnvelope(env);
            }
        }
        catch {
            // Ignore malformed frame without crashing UI stream loop
        }
    }
    #applySnapshot(snap) {
        if (snap.csrf_token) {
            this.#csrfToken = snap.csrf_token;
        }
        const capped = snap.messages.slice(-MAX_CLIENT_MESSAGES);
        const nextStatus = capped.length > 0 ? "success" : "empty";
        this.#updateModel({
            status: nextStatus,
            voiceEnabled: snap.voice_enabled,
            callSession: snap.call_session,
            activeWorkspace: snap.active_workspace,
            availableWorkspaces: snap.available_workspaces,
            pendingApprovals: snap.pending_approvals,
            messages: capped,
            errorBanner: null,
        });
    }
    #applyEnvelope(env) {
        const p = env.payload;
        if (env.event_type === "workspace_changed") {
            const ws = {
                display_name: String(p["display_name"] ?? ""),
                canonical_path: String(p["canonical_path"] ?? ""),
                selected_via: p["selected_via"] ?? "exact_path",
            };
            this.#updateModel({ activeWorkspace: ws, pickerFallbackOpen: false });
            return;
        }
        if (env.event_type === "call_state_changed") {
            const nextState = p["state"] ?? "standby";
            const turnCount = typeof p["turn_count"] === "number" ? p["turn_count"] : this.#model.callSession.turn_count;
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
            }
            else {
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
            const closedStreams = this.#model.messages.map((m) => m.correlation_id === env.correlation_id && m.streaming ? { ...m, streaming: false } : m);
            this.#updateModel({
                messages: closedStreams,
                lastMilestone: summary || this.#model.lastMilestone,
            });
            return;
        }
        if (env.event_type === "voice_transcription") {
            const msg = p;
            this.#appendMessage(msg);
            return;
        }
        if (env.event_type === "runner_failure") {
            const errorCode = String(p["error_code"] ?? "ORCHESTRATOR_UNEXPECTED_FAILURE");
            this.#updateModel({ noticeBanner: resolveHumanNotice(errorCode) });
            return;
        }
        if (env.event_type === "command_blocked") {
            const nested = p["message"];
            if (nested) {
                this.#appendMessage(nested);
            }
            return;
        }
        if (env.event_type === "command_approval_requested") {
            const item = p;
            const filtered = this.#model.pendingApprovals.filter((a) => a.audit_id !== item.audit_id);
            this.#updateModel({ pendingApprovals: [...filtered, item] });
            return;
        }
        if (env.event_type === "command_approval_resolved") {
            const auditId = String(p["audit_id"] ?? "");
            const remaining = this.#model.pendingApprovals.filter((a) => a.audit_id !== auditId);
            this.#updateModel({ pendingApprovals: remaining });
        }
    }
    #appendMessage(msg) {
        const next = [...this.#model.messages, msg].slice(-MAX_CLIENT_MESSAGES);
        this.#updateModel({ status: "success", messages: next });
        this.#resetSleepTimer();
    }
    #resetSleepTimer() {
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
    #updateModel(patch) {
        this.#model = { ...this.#model, ...patch };
        this.#render();
    }
    #render() {
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
        const headerSlot = this.#root.querySelector('[data-slot="header"]');
        if (headerSlot) {
            headerSlot.innerHTML = renderWorkspaceBar(this.#model.activeWorkspace, this.#model.availableWorkspaces, this.#model.pickerFallbackOpen, this.#model.callSession.state);
        }
        const timelineSlot = this.#root.querySelector('[data-slot="timeline"]');
        if (timelineSlot) {
            timelineSlot.innerHTML = renderChatTimeline(this.#model.status, this.#model.messages, this.#model.errorBanner, this.#model.activeWorkspace, this.#model.availableWorkspaces);
            timelineSlot.scrollTop = timelineSlot.scrollHeight;
        }
        const approvalsSlot = this.#root.querySelector('[data-slot="approvals"]');
        if (approvalsSlot) {
            approvalsSlot.innerHTML = renderApprovalBanner(this.#model.pendingApprovals);
        }
        const milestoneSlot = this.#root.querySelector('[data-slot="milestone"]');
        if (milestoneSlot) {
            milestoneSlot.innerHTML = this.#model.lastMilestone
                ? `<p class="nova-milestone-toast" aria-live="polite">${escapeHtml(this.#model.lastMilestone)}</p>`
                : "";
        }
        const noticeSlot = this.#root.querySelector('[data-slot="notice"]');
        if (noticeSlot) {
            noticeSlot.outerHTML = renderNoticeSlot(this.#model);
        }
        const inCall = this.#model.callSession.state !== "standby" && this.#model.callSession.state !== "closed";
        const dockSlot = this.#root.querySelector('[data-slot="dock"]');
        if (dockSlot) {
            const currentDockMode = dockSlot.dataset["dockMode"];
            const targetDockMode = inCall ? "call" : "standby";
            if (currentDockMode !== targetDockMode) {
                dockSlot.dataset["dockMode"] = targetDockMode;
                dockSlot.innerHTML = renderDock(this.#model);
            }
        }
        const layoutEl = this.#root.querySelector(".nova-layout");
        if (layoutEl) {
            layoutEl.dataset["uiMode"] = this.#model.uiMode ?? "panel";
            layoutEl.dataset["callState"] = this.#model.callSession.state;
        }
    }
    #bindDomEvents() {
        const root = this.#root;
        if (!root) {
            return;
        }
        const sig = this.#abortController.signal;
        root.addEventListener("click", (ev) => {
            const target = ev.target?.closest("[data-action]");
            if (!target) {
                return;
            }
            const action = target.dataset["action"];
            if (action === "toggle-ui-mode") {
                void this.toggleUiMode();
            }
            else if (action === "call-start") {
                void this.triggerCallAction("start");
            }
            else if (action === "call-end") {
                void this.triggerCallAction("end");
            }
            else if (action === "call-barge-in") {
                void this.triggerCallAction("barge-in");
            }
            else if (action === "pick-native") {
                void this.pickNativeWorkspace();
            }
            else if (action === "select-workspace-option") {
                const canonicalPath = target.dataset["canonicalPath"] ?? "";
                if (canonicalPath) {
                    void this.selectWorkspace(canonicalPath);
                }
            }
            else if (action === "dismiss-notice") {
                this.dismissNotice();
            }
            else if (action === "approve-command") {
                const auditId = target.dataset["auditId"] ?? "";
                void this.resolveApproval(auditId, true);
            }
            else if (action === "reject-command") {
                const auditId = target.dataset["auditId"] ?? "";
                void this.resolveApproval(auditId, false);
            }
        }, { signal: sig });
        root.addEventListener("change", (ev) => {
            const target = ev.target;
            if (target?.dataset["action"] === "select-workspace-dropdown" && target.value) {
                void this.selectWorkspace(target.value);
            }
        }, { signal: sig });
        root.addEventListener("submit", (ev) => {
            const form = ev.target?.closest('[data-action="chat-form"]');
            if (!form) {
                return;
            }
            ev.preventDefault();
            const input = form.querySelector("#nova-chat-input");
            if (input && input.value.trim()) {
                const text = input.value;
                input.value = "";
                void this.submitChat(text);
            }
        }, { signal: sig });
        root.addEventListener("keydown", (ev) => {
            if (ev.key === "Escape") {
                void this.handleEscapeKey();
                return;
            }
            const target = ev.target;
            if (target?.id === "nova-chat-input" &&
                ev.key === "Enter" &&
                !ev.shiftKey &&
                !ev.isComposing) {
                ev.preventDefault();
                const form = target.closest("form");
                form?.requestSubmit();
            }
        }, { signal: sig });
    }
}
