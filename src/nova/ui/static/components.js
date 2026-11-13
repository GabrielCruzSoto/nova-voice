/**
 * Pure Presentational Components for Nova Desktop UI (profiles/frontend-web.md#2,6,7,8).
 * Compliant with Nova Master Visual Identity & GNOME Desktop Specification.
 * Zero network calls, zero mutable state, strict HTML escaping against XSS.
 */
const HTML_ESCAPE_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
};
export function escapeHtml(raw) {
    return raw.replace(/[&<>"']/g, (ch) => HTML_ESCAPE_MAP[ch] ?? ch);
}
const CALL_STATE_LABELS = {
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
const FLOATING_ORB_SUBTITLES = {
    standby: "(Di 'Oye Nova')",
    listening: "Escuchando...",
    thinking: "Pensando...",
    executing: "Ejecutando...",
    speaking: "Hablando...",
    closed: "(Di 'Oye Nova')",
};
export function renderNovaSquircleSvg(size, active) {
    if (!active) {
        return `
      <svg class="nova-squircle-svg nova-squircle-svg--muted" viewBox="0 0 216 216" width="${size}" height="${size}" aria-hidden="true">
        <defs>
          <linearGradient id="squircleMutedBg" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#222938" stop-opacity="0.92" />
            <stop offset="100%" stop-color="#121722" stop-opacity="0.96" />
          </linearGradient>
          <linearGradient id="monogramMutedBevel" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#7c8b9e" />
            <stop offset="55%" stop-color="#526071" />
            <stop offset="100%" stop-color="#3e4a59" />
          </linearGradient>
        </defs>
        <rect x="8" y="8" width="200" height="200" rx="52" ry="52" fill="url(#squircleMutedBg)" stroke="#526071" stroke-width="2.2" stroke-opacity="0.6" />
        <rect x="14" y="14" width="188" height="188" rx="46" ry="46" fill="none" stroke="#cbd5e1" stroke-opacity="0.08" stroke-width="1" />
        <path d="M 64 156 L 64 60 L 86 60 L 132 126 L 132 60 L 152 60 L 152 156 L 130 156 L 84 90 L 84 156 Z"
              fill="url(#monogramMutedBevel)" stroke="#94a3b8" stroke-opacity="0.45" stroke-width="1.2" stroke-linejoin="round" />
        <g transform="translate(146, 64)">
          <circle cx="0" cy="0" r="9" fill="#94a3b8" opacity="0.22" />
          <path d="M 0 -13 Q 1 -2.5 13 0 Q 1 2.5 0 13 Q -1 2.5 -13 0 Q -1 -2.5 0 -13 Z" fill="#cbd5e1" opacity="0.72" />
          <circle cx="0" cy="0" r="2" fill="#f8fafc" opacity="0.85" />
        </g>
      </svg>
    `.trim();
    }
    return `
    <svg class="nova-squircle-svg nova-squircle-svg--active" viewBox="0 0 216 216" width="${size}" height="${size}" aria-hidden="true">
      <defs>
        <linearGradient id="squircleBgLive" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#1b2438" />
          <stop offset="55%" stop-color="#101729" />
          <stop offset="100%" stop-color="#090d1a" />
        </linearGradient>
        <linearGradient id="squircleBorderLive" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0.88" />
          <stop offset="45%" stop-color="#38bdf8" stop-opacity="0.55" />
          <stop offset="100%" stop-color="#a855f7" stop-opacity="0.85" />
        </linearGradient>
        <linearGradient id="monogramGradLive" x1="0%" y1="100%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="#00d2ff" />
          <stop offset="48%" stop-color="#6366f1" />
          <stop offset="100%" stop-color="#a855f7" />
        </linearGradient>
        <radialGradient id="starRadialLive" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#fffbeb" />
          <stop offset="38%" stop-color="#fde68a" />
          <stop offset="75%" stop-color="#f59e0b" stop-opacity="0.7" />
          <stop offset="100%" stop-color="#f59e0b" stop-opacity="0" />
        </radialGradient>
      </defs>
      <rect x="8" y="8" width="200" height="200" rx="52" ry="52" fill="url(#squircleBgLive)" stroke="url(#squircleBorderLive)" stroke-width="3" />
      <rect x="14" y="14" width="188" height="188" rx="46" ry="46" fill="none" stroke="#ffffff" stroke-opacity="0.1" stroke-width="1.2" />
      <path d="M 64 156 L 64 60 L 86 60 L 132 126 L 132 60 L 152 60 L 152 156 L 130 156 L 84 90 L 84 156 Z"
            fill="url(#monogramGradLive)" stroke="#7dd3fc" stroke-width="1.5" stroke-linejoin="round" />
      <path d="M 69 151 L 69 65 L 84 65 L 131 132 L 131 65 L 147 65"
            fill="none" stroke="#ffffff" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" opacity="0.45" />
      <g transform="translate(146, 64)">
        <circle cx="0" cy="0" r="24" fill="url(#starRadialLive)" opacity="0.7" />
        <path d="M 0 -30 Q 2 -6 30 0 Q 2 6 0 30 Q -2 6 -30 0 Q -2 -6 0 -30 Z" fill="#fffbeb" />
        <path d="M 0 -16 Q 1.2 -4 16 0 Q 1.2 4 0 16 Q -1.2 4 -16 0 Q -1.2 -4 0 -16 Z" fill="#fde68a" transform="rotate(45)" />
        <circle cx="0" cy="0" r="4" fill="#ffffff" />
      </g>
    </svg>
  `.trim();
}
const RADIAL_BAR_HEIGHTS = [
    12, 7, 16, 9, 6, 14, 18, 8, 11, 15, 6, 13,
    19, 9, 7, 15, 11, 17, 8, 13, 16, 7, 10, 18,
    8, 14, 11, 6, 17, 10, 15, 8, 13, 18, 9, 11,
];
const RADIAL_BAR_COLORS = [
    "#00d2ff",
    "#38bdf8",
    "#6366f1",
    "#a855f7",
    "#ec4899",
];
export function renderRadialEqualizerSvg() {
    const cx = 64;
    const cy = 64;
    const innerR = 41;
    const count = RADIAL_BAR_HEIGHTS.length;
    const bars = [];
    for (let i = 0; i < count; i += 1) {
        const angleRad = (i * 2 * Math.PI) / count;
        const barLen = RADIAL_BAR_HEIGHTS[i] ?? 10;
        const x1 = (cx + Math.cos(angleRad) * innerR).toFixed(2);
        const y1 = (cy + Math.sin(angleRad) * innerR).toFixed(2);
        const x2 = (cx + Math.cos(angleRad) * (innerR + barLen)).toFixed(2);
        const y2 = (cy + Math.sin(angleRad) * (innerR + barLen)).toFixed(2);
        const stroke = RADIAL_BAR_COLORS[i % RADIAL_BAR_COLORS.length] ?? "#00d2ff";
        const groupClass = i % 2 === 0 ? "nova-eq-bar nova-eq-bar--even" : "nova-eq-bar nova-eq-bar--odd";
        bars.push(`<line class="${groupClass}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${stroke}" stroke-width="2.2" stroke-linecap="round" />`);
    }
    return `
    <svg class="nova-radial-equalizer-svg" viewBox="0 0 128 128" width="128" height="128" aria-hidden="true">
      <circle cx="64" cy="64" r="39" fill="none" stroke="#00d2ff" stroke-opacity="0.55" stroke-width="1.6" />
      ${bars.join("")}
      <g transform="translate(27, 46)">
        <path d="M 0 -7 Q 0.7 -1.5 7 0 Q 0.7 1.5 0 7 Q -0.7 1.5 -7 0 Q -0.7 -1.5 0 -7 Z" fill="#fffbeb" opacity="0.9" />
      </g>
      <g transform="translate(99, 82)">
        <path d="M 0 -6 Q 0.6 -1.2 6 0 Q 0.6 1.2 0 6 Q -0.6 1.2 -6 0 Q -0.6 -1.2 0 -6 Z" fill="#fde68a" opacity="0.85" />
      </g>
    </svg>
  `.trim();
}
function isActiveCallState(callState) {
    return (callState === "listening" ||
        callState === "thinking" ||
        callState === "executing" ||
        callState === "speaking");
}
function renderFloatingOrbTooltip(callState) {
    const isStandby = !isActiveCallState(callState);
    const subtitle = FLOATING_ORB_SUBTITLES[callState] ?? FLOATING_ORB_SUBTITLES.standby;
    if (isStandby) {
        return `
      <div class="nova-orb-tooltip nova-orb-tooltip--standby" aria-hidden="true">
        <span class="nova-orb-tooltip-title">En reposo</span>
        <span class="nova-orb-tooltip-sub">${escapeHtml(subtitle)}</span>
      </div>
    `.trim();
    }
    return `
    <div class="nova-orb-tooltip nova-orb-tooltip--active" aria-hidden="true">
      <span class="nova-orb-tooltip-title">¡Oye Nova!</span>
      <span class="nova-orb-tooltip-sub">${escapeHtml(subtitle)}</span>
    </div>
  `.trim();
}
export function renderNovaReactiveOrb(callState, voiceEnabled) {
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
        <div class="nova-orb-radial-wave">${!isStandby ? renderRadialEqualizerSvg() : ""}</div>
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
export function renderCallControls(callState, voiceEnabled) {
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
export function renderWorkspaceBar(active, options, pickerFallbackOpen, callState = "standby") {
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
      ${fallbackNote}
    </div>
  `.trim();
}
export function renderApprovalBanner(approvals) {
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
const ERROR_CATALOG = {
    WORKSPACE_NOT_FOUND: "No se encontró el proyecto indicado.",
    WORKSPACE_OUTSIDE_ALLOWED_ROOTS: "El proyecto se encuentra fuera de las carpetas permitidas.",
    WORKSPACE_AMBIGUOUS: "Se encontraron varios proyectos que coinciden con el nombre.",
    WORKSPACE_RESOLUTION_TIMEOUT: "Se agotó el tiempo al intentar resolver el proyecto.",
    UI_VOICE_UNAVAILABLE: "El servicio de voz no está disponible en este momento.",
    UI_EMPTY_UTTERANCE: "El mensaje no puede estar vacío.",
    UI_PAYLOAD_TOO_LARGE: "El tamaño del contenido supera el límite permitido.",
    UI_FORBIDDEN_ORIGIN_OR_CSRF: "La solicitud fue rechazada por seguridad.",
    ORCHESTRATOR_UNEXPECTED_FAILURE: "Ocurrió un error inesperado al procesar la solicitud.",
};
const GENERIC_ERROR_FALLBACK = "Ocurrió un error inesperado al procesar la solicitud.";
export function resolveHumanNotice(code) {
    const message = ERROR_CATALOG[code] ?? GENERIC_ERROR_FALLBACK;
    return {
        code,
        message,
        dismissible: true,
    };
}
const ROLE_BADGE_MAP = {
    user: "Tú",
    assistant: "Nova",
    system: "Sistema",
    security: "Gatekeeper",
};
export function renderWorkspaceEmptyState(options) {
    const optionsHtml = options.length > 0
        ? `
      <div class="workspace-catalog">
        ${options
            .map((opt) => `
          <button
            type="button"
            class="btn-workspace-option"
            data-action="select-workspace-option"
            data-canonical-path="${escapeHtml(opt.canonical_path)}"
          >
            ${escapeHtml(opt.display_name)}
          </button>
        `)
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
export function renderChatTimeline(status, messages, errorBanner, activeWorkspace, availableWorkspaces = []) {
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
    const visibleMessages = messages.filter((msg) => msg.modality !== "error");
    if (status === "empty" || visibleMessages.length === 0) {
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
    const itemsHtml = visibleMessages
        .map((msg) => {
        const badge = ROLE_BADGE_MAP[msg.role] ?? msg.role;
        const streamingAttr = msg.streaming ? ' data-streaming="true"' : "";
        const voiceIconHtml = msg.modality === "voice"
            ? `<span class="chat-msg-voice-icon" data-modality-icon="voice" role="img" aria-label="Mensaje de voz"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></span>`
            : "";
        return `
        <article
          class="chat-msg chat-msg--${escapeHtml(msg.role)}"
          data-modality="${escapeHtml(msg.modality)}"
          data-message-id="${escapeHtml(msg.message_id)}"${streamingAttr}
        >
          <header class="chat-msg-header">
            <span class="chat-msg-author">${escapeHtml(badge)}</span>
            ${voiceIconHtml}
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
export function renderSinusoidalWave(callState) {
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
export function renderComposerForm() {
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
export function renderNoticeSlot(model) {
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
export function renderDock(model) {
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
export function renderAppShell(model) {
    const uiMode = model.uiMode ?? "panel";
    const inCall = isActiveCallState(model.callSession.state);
    const dockMode = inCall ? "call" : "standby";
    if (uiMode === "orb") {
        const meta = CALL_STATE_LABELS[model.callSession.state] ?? CALL_STATE_LABELS.standby;
        const radialSvg = inCall ? renderRadialEqualizerSvg() : "";
        return `
      <div class="nova-layout" data-ui-mode="orb" data-call-state="${escapeHtml(model.callSession.state)}">
        <div class="nova-widget-container" data-slot="widget-orb">
          ${renderFloatingOrbTooltip(model.callSession.state)}
          <div class="nova-widget-orb-shell" data-call-state="${escapeHtml(model.callSession.state)}">
            <div class="nova-orb-halo" aria-hidden="true"></div>
            <div class="nova-orb-radial-wave" aria-hidden="true">${radialSvg}</div>
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
          ${renderChatTimeline(model.status, model.messages, model.errorBanner, model.activeWorkspace, model.availableWorkspaces)}
        </div>

        <div class="nova-approvals-slot" data-slot="approvals">
          ${renderApprovalBanner(model.pendingApprovals)}
        </div>

        <div data-slot="milestone" aria-live="polite">${milestoneHtml}</div>

        ${renderNoticeSlot(model)}

        <div class="nova-dock" data-slot="dock" data-dock-mode="${dockMode}">
          ${renderDock(model)}
        </div>
      </main>
    </div>
  `.trim();
}
