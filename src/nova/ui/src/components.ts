/**
 * Pure Presentational Components for Nova Desktop UI (profiles/frontend-web.md#2,6,7,8).
 * Compliant with Nova Master Visual Identity & GNOME Desktop Specification.
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
    title: "En reposo",
    subtitle: "Di «Oye Nova» o haz clic para activar",
  },
  listening: {
    title: "¡Oye Nova! Escuchando…",
    subtitle: "Micrófono activo — habla con naturalidad",
  },
  thinking: {
    title: "Procesando",
    subtitle: "Analizando intención y planificando",
  },
  executing: {
    title: "Ejecutando",
    subtitle: "Trabajando en el proyecto activo",
  },
  speaking: {
    title: "Respondiendo",
    subtitle: "Puedes interrumpir en cualquier momento",
  },
  closed: {
    title: "Sesión cerrada",
    subtitle: "Subsistema de llamada detenido",
  },
};

export function renderNovaSquircleSvg(size: number, active: boolean): string {
  if (!active) {
    return `
      <svg class="nova-squircle-svg nova-squircle-svg--muted" viewBox="0 0 216 216" width="${size}" height="${size}" aria-hidden="true">
        <rect x="6" y="6" width="204" height="204" rx="54" ry="54" fill="rgba(34, 40, 52, 0.68)" stroke="#526071" stroke-width="2" stroke-opacity="0.5" />
        <g opacity="0.35">
          <ellipse cx="108" cy="108" rx="88" ry="34" fill="none" stroke="#526071" stroke-width="1.5" transform="rotate(-30 108 108)" />
          <ellipse cx="108" cy="108" rx="88" ry="34" fill="none" stroke="#526071" stroke-width="1.5" transform="rotate(35 108 108)" />
        </g>
        <path d="M 68 156 L 68 62 L 86 62 L 132 128 L 132 62 L 150 62 L 150 156 L 132 156 L 86 90 L 86 156 Z"
              fill="#526071" stroke="#64748b" stroke-width="1" stroke-linejoin="round" />
        <g transform="translate(142, 68)">
          <circle cx="0" cy="0" r="10" fill="#64748b" opacity="0.25" />
          <path d="M 0 -14 Q 1 -3 14 0 Q 1 3 0 14 Q -1 3 -14 0 Q -1 -3 0 -14 Z" fill="#94a3b8" opacity="0.6" />
          <circle cx="0" cy="0" r="2" fill="#cbd5e1" />
        </g>
      </svg>
    `.trim();
  }

  return `
    <svg class="nova-squircle-svg nova-squircle-svg--active" viewBox="0 0 216 216" width="${size}" height="${size}" aria-hidden="true">
      <defs>
        <linearGradient id="squircleBgLive" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#1e2638" />
          <stop offset="100%" stop-color="#0d1322" />
        </linearGradient>
        <linearGradient id="squircleBorderLive" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0.8" />
          <stop offset="40%" stop-color="#38bdf8" stop-opacity="0.5" />
          <stop offset="100%" stop-color="#a855f7" stop-opacity="0.7" />
        </linearGradient>
        <linearGradient id="monogramGradLive" x1="0%" y1="100%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="#00d2ff" />
          <stop offset="45%" stop-color="#6366f1" />
          <stop offset="100%" stop-color="#a855f7" />
        </linearGradient>
        <radialGradient id="starRadialLive" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#fffbeb" />
          <stop offset="35%" stop-color="#fde68a" />
          <stop offset="70%" stop-color="#f59e0b" stop-opacity="0.8" />
          <stop offset="100%" stop-color="#f59e0b" stop-opacity="0.8" />
        </radialGradient>
      </defs>
      <rect x="6" y="6" width="204" height="204" rx="54" ry="54" fill="url(#squircleBgLive)" stroke="url(#squircleBorderLive)" stroke-width="3" />
      <g opacity="0.85">
        <ellipse cx="108" cy="108" rx="88" ry="34" fill="none" stroke="#00d2ff" stroke-width="2" transform="rotate(-30 108 108)" />
        <circle cx="44" cy="74" r="4.5" fill="#38bdf8" />
        <circle cx="172" cy="142" r="3.5" fill="#00d2ff" />
        <ellipse cx="108" cy="108" rx="88" ry="34" fill="none" stroke="#a855f7" stroke-width="2" transform="rotate(35 108 108)" />
        <circle cx="168" cy="70" r="5" fill="#ec4899" />
        <circle cx="48" cy="146" r="3.5" fill="#a855f7" />
      </g>
      <path d="M 68 156 L 68 62 L 86 62 L 132 128 L 132 62 L 150 62 L 150 156 L 132 156 L 86 90 L 86 156 Z"
            fill="url(#monogramGradLive)" stroke="#38bdf8" stroke-width="1.5" stroke-linejoin="round" />
      <path d="M 72 152 L 72 68 L 84 68 L 130 134 L 130 68 L 142 68 L 142 152 L 130 152 L 84 86 L 84 152 Z"
            fill="none" stroke="#ffffff" stroke-width="1" opacity="0.35" />
      <g transform="translate(142, 68)">
        <circle cx="0" cy="0" r="28" fill="url(#starRadialLive)" opacity="0.6" />
        <path d="M 0 -36 Q 2 -8 36 0 Q 2 8 0 36 Q -2 8 -36 0 Q -2 -8 0 -36 Z" fill="#fffbeb" />
        <path d="M 0 -20 Q 1.5 -5 20 0 Q 1.5 5 0 20 Q -1.5 5 -20 0 Q -1.5 -5 0 -20 Z" fill="#fde68a" transform="rotate(45)" />
        <circle cx="0" cy="0" r="4.5" fill="#ffffff" />
      </g>
    </svg>
  `.trim();
}

export function renderNovaReactiveOrb(
  callState: CallSessionState,
  voiceEnabled: boolean,
): string {
  const safeState = escapeHtml(callState);
  const meta = CALL_STATE_LABELS[callState];
  const isStandby = callState === "standby" || callState === "closed";
  const voiceBadge = voiceEnabled ? "Voz GPU local activa" : "Modo solo texto (voz deshabilitada)";
  const ariaLabel = `Estado de Nova: ${meta.title}. ${meta.subtitle}. ${voiceBadge}`;

  const bubbleText = callState === "listening"
    ? "¡Oye Nova! Escuchando…"
    : meta.title;

  return `
    <div
      class="nova-orb-stage"
      data-call-state="${safeState}"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      aria-label="${escapeHtml(ariaLabel)}"
      data-action="toggle-ui-mode"
      tabindex="0"
    >
      <div class="nova-orb-visual" aria-hidden="true">
        <div class="nova-orb-halo"></div>
        <div class="nova-orb-radial-wave"></div>
        <div class="nova-orb-medallion">
          ${renderNovaSquircleSvg(isStandby ? 56 : 64, !isStandby)}
        </div>
      </div>
      <div class="nova-orb-caption">
        <span class="nova-state-pill" data-state="${safeState}">${escapeHtml(bubbleText)}</span>
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
  const canBargeIn = voiceEnabled && inCall;
  const bargeInDisabledAttr = canBargeIn ? "" : ' disabled aria-disabled="true"';
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
        data-action="call-barge-in"${bargeInDisabledAttr}
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
  callState: CallSessionState = "standby",
): string {
  const safeState = escapeHtml(callState);
  const stateMeta = CALL_STATE_LABELS[callState] ?? CALL_STATE_LABELS.standby;

  const chipHtml = active
    ? `<span class="workspace-chip" title="${escapeHtml(active.canonical_path)}">${escapeHtml(active.display_name)}</span>`
    : `<span class="workspace-chip workspace-chip--empty">Sin proyecto</span>`;

  const optionItems = options
    .map((opt) => {
      const selected = active && opt.canonical_path === active.canonical_path ? " selected" : "";
      return `<option value="${escapeHtml(opt.canonical_path)}" title="${escapeHtml(opt.canonical_path)}"${selected}>${escapeHtml(opt.display_name)}</option>`;
    })
    .join("");

  const fallbackNote = pickerFallbackOpen
    ? `<span class="workspace-fallback-hint" role="status">Selector nativo no disponible; elige un proyecto del catálogo confinado.</span>`
    : "";

  return `
    <div class="workspace-bar">
      <div class="workspace-identity">
        <span class="workspace-brand">NOVA</span>
        <div data-slot="state-indicator" role="status" aria-live="polite" class="nova-state-indicator">
          <span class="nova-state-pill" data-state="${safeState}">${escapeHtml(stateMeta.title)}</span>
        </div>
      </div>
      <div class="workspace-selection">
        ${chipHtml}
        <label class="sr-only" for="nova-workspace-select">Cambiar proyecto activo</label>
        <select
          id="nova-workspace-select"
          class="workspace-select"
          data-action="select-workspace-dropdown"
          aria-label="Catálogo de proyectos permitidos"
        >
          <option value="">Seleccionar proyecto…</option>
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
      <div class="workspace-view-actions">
        <button
          type="button"
          class="btn-toggle-view"
          data-action="toggle-ui-mode"
          title="Modo widget"
          aria-label="Modo widget"
        >
          Modo widget
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

export function renderWorkspaceEmptyState(options: readonly UiWorkspaceOption[]): string {
  const optionsHtml =
    options.length > 0
      ? `
      <div class="workspace-catalog">
        ${options
          .map(
            (opt) => `
          <button
            type="button"
            class="btn-workspace-option"
            data-action="select-workspace-option"
            data-canonical-path="${escapeHtml(opt.canonical_path)}"
          >
            ${escapeHtml(opt.display_name)}
          </button>
        `,
          )
          .join("")}
      </div>
    `.trim()
      : "";

  return `
    <div class="workspace-empty-state" data-slot="workspace-empty-state">
      <h2 class="workspace-empty-title">Elegí un proyecto</h2>
      <p class="workspace-empty-desc">Para comenzar a trabajar, seleccioná un proyecto del catálogo o abrí una carpeta.</p>
      ${optionsHtml}
      <button
        type="button"
        class="btn btn--secondary"
        data-action="pick-native"
      >
        Abrir carpeta…
      </button>
    </div>
  `.trim();
}

export function renderChatTimeline(
  status: ViewStatus,
  messages: readonly UiChatMessage[],
  errorBanner: string | null,
  activeWorkspace?: UiWorkspaceView | null,
  availableWorkspaces: readonly UiWorkspaceOption[] = [],
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

  if (activeWorkspace === null) {
    return `
      <div class="chat-timeline chat-timeline--state" data-view-status="${escapeHtml(status)}">
        ${safeError}
        ${renderWorkspaceEmptyState(availableWorkspaces)}
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
        <div class="nova-empty-greeting">
          <h3 class="nova-greeting-title">Hola, ¿en qué te ayudo?</h3>
          <p class="nova-greeting-subtitle">Di «Oye Nova» o escribe una consulta para comenzar.</p>
        </div>
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

export function renderSinusoidalWave(callState: CallSessionState): string {
  const isStandby = callState === "standby" || callState === "closed";

  if (isStandby) {
    return `
      <div class="nova-wave-box" data-wave-state="standby" aria-hidden="true">
        <svg class="nova-wave-svg nova-wave-svg--flat" viewBox="0 0 300 24" preserveAspectRatio="none">
          <line x1="0" y1="12" x2="300" y2="12" stroke="rgba(100, 116, 139, 0.40)" stroke-width="2" stroke-linecap="round" />
        </svg>
      </div>
    `.trim();
  }

  return `
    <div class="nova-wave-box" data-wave-state="active" aria-hidden="true">
      <svg class="nova-wave-svg nova-wave-svg--dynamic" viewBox="0 0 300 48" preserveAspectRatio="none">
        <defs>
          <linearGradient id="sinusoidalGrad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stop-color="#00d2ff" />
            <stop offset="45%" stop-color="#38bdf8" />
            <stop offset="100%" stop-color="#a855f7" />
          </linearGradient>
        </defs>
        <path class="nova-sinusoidal-path" d="M 0 24 Q 37.5 4, 75 24 T 150 24 T 225 24 T 300 24" fill="none" stroke="url(#sinusoidalGrad)" stroke-width="3" stroke-linecap="round" />
      </svg>
    </div>
  `.trim();
}

export function renderComposerForm(): string {
  return `
    <form class="chat-composer" data-action="chat-form">
      <label class="sr-only" for="nova-chat-input">Pregúntale a Nova…</label>
      <div class="composer-pill-wrapper">
        <textarea
          id="nova-chat-input"
          name="utterance"
          class="chat-input"
          rows="1"
          maxlength="16000"
          placeholder="Pregúntale a Nova…"
          required
        ></textarea>
        <div class="composer-actions">
          <button
            type="button"
            class="btn-icon btn-mic"
            data-action="call-start"
            title="Activar micrófono"
            aria-label="Activar micrófono"
          >
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="22" />
            </svg>
          </button>
          <button type="submit" class="btn-icon btn-send" title="Enviar mensaje" aria-label="Enviar">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        </div>
      </div>
    </form>
  `.trim();
}

export function renderNoticeSlot(model: PresentationalAppModel): string {
  if (model.noticeBanner) {
    const dismissBtn = model.noticeBanner.dismissible
      ? `<button type="button" class="btn-dismiss" data-action="dismiss-notice" aria-label="Cerrar aviso">Cerrar</button>`
      : "";
    return `
      <div class="nova-notice-container" data-slot="notice" role="alert" data-error-code="${escapeHtml(model.noticeBanner.code)}">
        <span class="notice-message">${escapeHtml(model.noticeBanner.message)}</span>
        ${dismissBtn}
      </div>
    `.trim();
  }
  if (model.errorBanner) {
    return `
      <div class="nova-notice-container" data-slot="notice" role="alert">
        <span class="notice-message">${escapeHtml(model.errorBanner)}</span>
      </div>
    `.trim();
  }
  return `<div class="nova-notice-container" data-slot="notice"></div>`;
}

export function renderDock(model: PresentationalAppModel): string {
  const inCall = model.callSession.state !== "standby" && model.callSession.state !== "closed";

  const orbDisabled = !model.voiceEnabled || inCall;
  const orbDisabledAttrs = orbDisabled ? ' disabled aria-disabled="true"' : "";
  const orbHtml = `
    <button
      type="button"
      class="nova-dock-orb"
      data-action="call-start"
      aria-label="Iniciar llamada"
      ${orbDisabledAttrs}
    >
      ${renderNovaSquircleSvg(40, inCall)}
    </button>
  `.trim();

  if (!inCall) {
    const hasWorkspace = model.activeWorkspace !== null;
    const inputDisabledAttr = !hasWorkspace ? ' disabled' : "";
    const describedByAttr = !hasWorkspace ? ' aria-describedby="composer-no-workspace-hint"' : "";
    const submitDisabledAttr = !hasWorkspace ? ' disabled' : "";

    return `
      <div class="nova-dock-content nova-dock-content--standby">
        ${orbHtml}
        <form class="chat-composer" data-action="chat-form">
          <label class="sr-only" for="nova-chat-input">Pregúntale a Nova…</label>
          <div class="composer-pill-wrapper">
            <textarea
              id="nova-chat-input"
              name="utterance"
              class="chat-input"
              rows="1"
              maxlength="16000"
              placeholder="Pregúntale a Nova…"
              required${inputDisabledAttr}${describedByAttr}
            ></textarea>
            ${!hasWorkspace ? `<span id="composer-no-workspace-hint" class="sr-only">Selecciona un proyecto para chatear</span>` : ""}
            <div class="composer-actions">
              <button type="submit" class="btn-icon btn-send" title="Enviar mensaje" aria-label="Enviar"${submitDisabledAttr}>
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <line x1="22" y1="2" x2="11" y2="13" />
                  <polygon points="22 2 15 22 11 13 2 9 22 2" />
                </svg>
              </button>
            </div>
          </div>
        </form>
      </div>
    `.trim();
  }

  const bargeInDisabled = !model.voiceEnabled || !inCall;
  const bargeInAttrs = bargeInDisabled ? ' disabled aria-disabled="true"' : "";
  const endDisabled = !model.voiceEnabled || !inCall;
  const endAttrs = endDisabled ? ' disabled aria-disabled="true"' : "";

  return `
    <div class="nova-dock-content nova-dock-content--call">
      ${orbHtml}
      <div class="nova-dock-wave">
        ${renderSinusoidalWave(model.callSession.state)}
      </div>
      <div class="nova-dock-call-actions">
        <button
          type="button"
          class="btn btn--warning btn-barge-in"
          data-action="call-barge-in"${bargeInAttrs}
        >
          Interrumpir (Esc)
        </button>
        <button
          type="button"
          class="btn btn--ghost btn-call-end"
          data-action="call-end"${endAttrs}
        >
          Colgar
        </button>
      </div>
    </div>
  `.trim();
}

export function renderAppShell(model: PresentationalAppModel): string {
  const uiMode = model.uiMode ?? "panel";
  const inCall = model.callSession.state !== "standby" && model.callSession.state !== "closed";
  const dockMode = inCall ? "call" : "standby";

  if (uiMode === "orb") {
    const meta = CALL_STATE_LABELS[model.callSession.state] ?? CALL_STATE_LABELS.standby;
    return `
      <div class="nova-layout" data-ui-mode="orb" data-call-state="${escapeHtml(model.callSession.state)}">
        <div class="nova-widget-container" data-slot="widget-orb">
          <button
            type="button"
            class="nova-widget-orb-btn"
            data-action="toggle-ui-mode"
            data-call-state="${escapeHtml(model.callSession.state)}"
            aria-label="Estado de Nova: ${escapeHtml(meta.title)}"
          >
            ${renderNovaSquircleSvg(48, inCall)}
          </button>
        </div>
      </div>
    `.trim();
  }

  const milestoneHtml = model.lastMilestone
    ? `<p class="nova-milestone-toast" aria-live="polite">${escapeHtml(model.lastMilestone)}</p>`
    : "";

  return `
    <div class="nova-layout" data-ui-mode="panel" data-call-state="${escapeHtml(model.callSession.state)}">
      <header class="nova-header" data-slot="header">
        ${renderWorkspaceBar(model.activeWorkspace, model.availableWorkspaces, model.pickerFallbackOpen, model.callSession.state)}
      </header>

      <main class="nova-main" aria-label="Historial de conversación y aprobaciones">
        <div class="nova-timeline-scroll" data-slot="timeline">
          ${renderChatTimeline(
            model.status,
            model.messages,
            model.errorBanner,
            model.activeWorkspace,
            model.availableWorkspaces,
          )}
        </div>

        <div class="nova-approvals-slot" data-slot="approvals">
          ${renderApprovalBanner(model.pendingApprovals)}
        </div>

        <div data-slot="milestone">${milestoneHtml}</div>

        ${renderNoticeSlot(model)}

        <div class="nova-dock" data-slot="dock" data-dock-mode="${dockMode}">
          ${renderDock(model)}
        </div>
      </main>
    </div>
  `.trim();
}
