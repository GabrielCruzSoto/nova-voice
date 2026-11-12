/**
 * Pure Presentational Components for Nova Desktop UI (profiles/frontend-web.md#2,6,7,8).
 * Zero network calls, zero mutable state, strict HTML escaping against XSS.
 */

import type {
  CallSessionState,
  PresentationalAppModel,
  UiApprovalItem,
  UiChatMessage,
  UiWorkspaceOption,
  UiWorkspaceView,
  ViewStatus,
} from "./contracts.js";

const HTML_ESCAPE_MAP: Readonly<Record<string, string>> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(raw: string): string {
  return raw.replace(/[&<>"']/g, (ch) => HTML_ESCAPE_MAP[ch] ?? ch);
}

const CALL_STATE_LABELS: Readonly<Record<CallSessionState, { title: string; subtitle: string }>> = {
  standby: {
    title: "En espera",
    subtitle: "Di «Oye Nova» o inicia llamada continua",
  },
  listening: {
    title: "Escuchando",
    subtitle: "Micrófono activo — habla con naturalidad",
  },
  thinking: {
    title: "Analizando",
    subtitle: "Evaluando intención y complejidad",
  },
  executing: {
    title: "Ejecutando",
    subtitle: "Trabajando en el proyecto activo",
  },
  speaking: {
    title: "Hablando",
    subtitle: "Puedes interrumpir en cualquier momento",
  },
  closed: {
    title: "Sesión cerrada",
    subtitle: "Subsistema de llamada detenido",
  },
};

export function renderNovaReactiveOrb(
  callState: CallSessionState,
  voiceEnabled: boolean,
): string {
  const safeState = escapeHtml(callState);
  const meta = CALL_STATE_LABELS[callState];
  const voiceBadge = voiceEnabled ? "Voz GPU local activa" : "Modo solo texto (voz deshabilitada)";
  const ariaLabel = `Estado de Nova: ${meta.title}. ${meta.subtitle}. ${voiceBadge}`;

  return `
    <div
      class="nova-orb-stage"
      data-call-state="${safeState}"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      aria-label="${escapeHtml(ariaLabel)}"
    >
      <div class="nova-orb-visual" aria-hidden="true">
        <svg class="nova-orb-svg" viewBox="0 0 200 200" width="168" height="168">
          <defs>
            <radialGradient id="novaCoreGrad" cx="50%" cy="42%" r="58%">
              <stop offset="0%" stop-color="#e0f2fe" />
              <stop offset="48%" stop-color="#38bdf8" />
              <stop offset="100%" stop-color="#1e1b4b" />
            </radialGradient>
          </defs>
          <circle class="nova-orb-ring nova-orb-ring--outer" cx="100" cy="100" r="86" />
          <circle class="nova-orb-ring nova-orb-ring--inner" cx="100" cy="100" r="68" />
          <circle class="nova-orb-core" cx="100" cy="100" r="50" fill="url(#novaCoreGrad)" />
        </svg>
      </div>
      <div class="nova-orb-caption">
        <span class="nova-state-pill" data-state="${safeState}">${escapeHtml(meta.title)}</span>
        <p class="nova-state-subtitle">${escapeHtml(meta.subtitle)}</p>
        <small class="nova-voice-badge">${escapeHtml(voiceBadge)}</small>
      </div>
    </div>
  `.trim();
}

export function renderCallControls(
  callState: CallSessionState,
  voiceEnabled: boolean,
): string {
  const inCall = callState !== "standby" && callState !== "closed";
  const disabledAttr = voiceEnabled ? "" : ' disabled aria-disabled="true"';
  const startDisabled = !voiceEnabled || inCall ? ' disabled aria-disabled="true"' : "";
  const endDisabled = !voiceEnabled || !inCall ? ' disabled aria-disabled="true"' : "";

  return `
    <nav class="nova-call-controls" aria-label="Controles de llamada continua">
      <button
        type="button"
        class="btn btn--primary"
        data-action="call-start"${startDisabled}
      >
        Iniciar llamada
      </button>
      <button
        type="button"
        class="btn btn--warning"
        data-action="call-barge-in"${disabledAttr}
      >
        Interrumpir (Esc)
      </button>
      <button
        type="button"
        class="btn btn--ghost"
        data-action="call-end"${endDisabled}
      >
        Colgar
      </button>
    </nav>
  `.trim();
}

export function renderWorkspaceBar(
  active: UiWorkspaceView | null,
  options: readonly UiWorkspaceOption[],
  pickerFallbackOpen: boolean,
): string {
  const activeName = active ? escapeHtml(active.display_name) : "Sin proyecto seleccionado";
  const activePath = active
    ? escapeHtml(active.canonical_path)
    : "Selecciona una carpeta dentro de tus raíces permitidas";
  const currentCanonical = active?.canonical_path ?? "";

  const optionItems = options
    .map((opt) => {
      const selected = opt.canonical_path === currentCanonical ? " selected" : "";
      return `<option value="${escapeHtml(opt.canonical_path)}"${selected}>${escapeHtml(opt.display_name)} — ${escapeHtml(opt.canonical_path)}</option>`;
    })
    .join("");

  const fallbackNote = pickerFallbackOpen
    ? `<span class="workspace-fallback-hint" role="status">Selector nativo no disponible; elige un proyecto del catálogo confinado.</span>`
    : "";

  return `
    <div class="workspace-bar">
      <div class="workspace-identity">
        <span class="workspace-brand">NOVA</span>
        <div class="workspace-meta">
          <strong class="workspace-name">${activeName}</strong>
          <code class="workspace-path">${activePath}</code>
        </div>
      </div>
      <div class="workspace-actions">
        <label class="sr-only" for="nova-workspace-select">Cambiar proyecto activo</label>
        <select
          id="nova-workspace-select"
          class="workspace-select"
          data-action="select-workspace-dropdown"
          aria-label="Catálogo de proyectos permitidos"
        >
          <option value="">Seleccionar proyecto del catálogo…</option>
          ${optionItems}
        </select>
        <button
          type="button"
          class="btn btn--secondary"
          data-action="pick-native"
        >
          Abrir carpeta…
        </button>
      </div>
      ${fallbackNote}
    </div>
  `.trim();
}

export function renderApprovalBanner(approvals: readonly UiApprovalItem[]): string {
  if (approvals.length === 0) {
    return "";
  }

  return approvals
    .map((item) => {
      const safeId = escapeHtml(item.audit_id);
      const titleId = `approval-title-${safeId}`;
      const descId = `approval-desc-${safeId}`;
      return `
        <section
          class="approval-card"
          data-risk="${escapeHtml(item.risk_level)}"
          role="alertdialog"
          aria-modal="false"
          aria-labelledby="${titleId}"
          aria-describedby="${descId}"
        >
          <div class="approval-header">
            <h2 id="${titleId}" class="approval-title">
              SecurityGatekeeper — Aprobación requerida (${escapeHtml(item.risk_level.toUpperCase())})
            </h2>
            <span class="approval-source">${escapeHtml(item.source)}</span>
          </div>
          <p id="${descId}" class="approval-summary">${escapeHtml(item.spoken_summary)}</p>
          <pre class="approval-command"><code>${escapeHtml(item.sanitized_command)}</code></pre>
          <p class="approval-detail">
            <strong>Directorio:</strong> <code>${escapeHtml(item.cwd)}</code> ·
            <strong>Detalle:</strong> ${escapeHtml(item.technical_explanation)}
          </p>
          <div class="approval-actions">
            <button
              type="button"
              class="btn btn--approve"
              data-action="approve-command"
              data-audit-id="${safeId}"
            >
              Aprobar comando
            </button>
            <button
              type="button"
              class="btn btn--reject"
              data-action="reject-command"
              data-audit-id="${safeId}"
            >
              Rechazar
            </button>
          </div>
        </section>
      `.trim();
    })
    .join("\n");
}

const ROLE_BADGE_MAP: Readonly<Record<string, string>> = {
  user: "Tú",
  assistant: "Nova",
  system: "Sistema",
  security: "Gatekeeper",
};

export function renderChatTimeline(
  status: ViewStatus,
  messages: readonly UiChatMessage[],
  errorBanner: string | null,
): string {
  const safeError = errorBanner
    ? `<div class="timeline-error-banner" role="alert">${escapeHtml(errorBanner)}</div>`
    : "";

  if (status === "loading") {
    return `
      <div class="chat-timeline chat-timeline--state" data-view-status="loading" role="status" aria-live="polite">
        <p>Conectando con el puente local de Nova (127.0.0.1)…</p>
      </div>
    `.trim();
  }

  if (status === "error" && messages.length === 0) {
    return `
      <div class="chat-timeline chat-timeline--state" data-view-status="error" role="alert">
        ${safeError || "<p>No fue posible sincronizar el estado con Nova.</p>"}
      </div>
    `.trim();
  }

  if (status === "empty" || messages.length === 0) {
    return `
      <div class="chat-timeline chat-timeline--state" data-view-status="empty">
        ${safeError}
        <p>Conversación lista. Escribe una instrucción o di «Oye Nova» para comenzar.</p>
      </div>
    `.trim();
  }

  const itemsHtml = messages
    .map((msg) => {
      const badge = ROLE_BADGE_MAP[msg.role] ?? msg.role;
      const streamingAttr = msg.streaming ? ' data-streaming="true"' : "";
      return `
        <article
          class="chat-msg chat-msg--${escapeHtml(msg.role)}"
          data-modality="${escapeHtml(msg.modality)}"
          data-message-id="${escapeHtml(msg.message_id)}"${streamingAttr}
        >
          <header class="chat-msg-header">
            <span class="chat-msg-author">${escapeHtml(badge)}</span>
            <span class="chat-msg-meta">${escapeHtml(msg.modality)} · ${escapeHtml(msg.step_type)}</span>
          </header>
          <div class="chat-msg-body">${escapeHtml(msg.content)}</div>
        </article>
      `.trim();
    })
    .join("\n");

  return `
    <div class="chat-timeline" data-view-status="${escapeHtml(status)}" role="log" aria-live="polite" aria-relevant="additions text">
      ${safeError}
      ${itemsHtml}
    </div>
  `.trim();
}

export function renderComposerForm(): string {
  return `
    <form class="chat-composer" data-action="chat-form">
      <label class="sr-only" for="nova-chat-input">Escribe un mensaje o tarea para Nova</label>
      <textarea
        id="nova-chat-input"
        name="utterance"
        class="chat-input"
        rows="2"
        maxlength="16000"
        placeholder="Escribe una consulta rápida o delega una tarea compleja…"
        required
      ></textarea>
      <button type="submit" class="btn btn--primary">Enviar</button>
    </form>
  `.trim();
}

export function renderAppShell(model: PresentationalAppModel): string {
  const milestoneHtml = model.lastMilestone
    ? `<p class="nova-milestone-toast" aria-live="polite">${escapeHtml(model.lastMilestone)}</p>`
    : "";

  return `
    <div class="nova-layout">
      <header class="nova-header" data-slot="workspace-bar">
        ${renderWorkspaceBar(model.activeWorkspace, model.availableWorkspaces, model.pickerFallbackOpen)}
      </header>
      <div class="nova-workspace-grid">
        <aside class="nova-sidebar" aria-label="Panel de llamada y estado de Nova">
          <div data-slot="orb">
            ${renderNovaReactiveOrb(model.callSession.state, model.voiceEnabled)}
          </div>
          <div data-slot="milestone">${milestoneHtml}</div>
          <div data-slot="call-controls">
            ${renderCallControls(model.callSession.state, model.voiceEnabled)}
          </div>
        </aside>
        <main class="nova-main" aria-label="Historial de conversación y aprobaciones">
          <div data-slot="approvals">
            ${renderApprovalBanner(model.pendingApprovals)}
          </div>
          <div class="nova-timeline-scroll" data-slot="timeline">
            ${renderChatTimeline(model.status, model.messages, model.errorBanner)}
          </div>
          ${renderComposerForm()}
        </main>
      </div>
    </div>
  `.trim();
}
