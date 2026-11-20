/**
 * Pure Presentational Components for Nova Desktop UI (profiles/frontend-web.md#2,6,7,8).
 * Compliant with Nova Master Visual Identity & GNOME Desktop Specification.
 * Zero network calls, zero mutable state, strict HTML escaping against XSS.
 */

import type {
  AgentsFilterKey,
  AudioDeviceInfo,
  CallSessionState,
  LiveDiagnosticsState,
  ModelsProvisionState,
  PresentationalAppModel,
  SystemHealthReport,
  UiAgentPoolSlot,
  UiAgentTask,
  UiAgentsOverview,
  UiApprovalItem,
  UiChatMessage,
  UiNoticeBanner,
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

const FLOATING_ORB_SUBTITLES: Readonly<Record<CallSessionState, string>> = {
  standby: "(Di 'Oye Nova')",
  listening: "Escuchando...",
  thinking: "Pensando...",
  executing: "Ejecutando...",
  speaking: "Hablando...",
  closed: "(Di 'Oye Nova')",
};

export function renderNovaSquircleSvg(size: number, active: boolean): string {
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

const RADIAL_BAR_HEIGHTS: readonly number[] = [
  12, 7, 16, 9, 6, 14, 18, 8, 11, 15, 6, 13,
  19, 9, 7, 15, 11, 17, 8, 13, 16, 7, 10, 18,
  8, 14, 11, 6, 17, 10, 15, 8, 13, 18, 9, 11,
];

const RADIAL_BAR_COLORS: readonly string[] = [
  "#00d2ff",
  "#38bdf8",
  "#6366f1",
  "#a855f7",
  "#ec4899",
];

export function renderRadialEqualizerSvg(): string {
  const cx = 64;
  const cy = 64;
  const innerR = 41;
  const count = RADIAL_BAR_HEIGHTS.length;
  const bars: string[] = [];

  for (let i = 0; i < count; i += 1) {
    const angleRad = (i * 2 * Math.PI) / count;
    const barLen = RADIAL_BAR_HEIGHTS[i] ?? 10;
    const x1 = (cx + Math.cos(angleRad) * innerR).toFixed(2);
    const y1 = (cy + Math.sin(angleRad) * innerR).toFixed(2);
    const x2 = (cx + Math.cos(angleRad) * (innerR + barLen)).toFixed(2);
    const y2 = (cy + Math.sin(angleRad) * (innerR + barLen)).toFixed(2);
    const stroke = RADIAL_BAR_COLORS[i % RADIAL_BAR_COLORS.length] ?? "#00d2ff";
    const groupClass = i % 2 === 0 ? "nova-eq-bar nova-eq-bar--even" : "nova-eq-bar nova-eq-bar--odd";
    bars.push(
      `<line class="${groupClass}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${stroke}" stroke-width="2.2" stroke-linecap="round" />`,
    );
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

function isActiveCallState(callState: CallSessionState): boolean {
  return (
    callState === "listening" ||
    callState === "thinking" ||
    callState === "executing" ||
    callState === "speaking"
  );
}

function renderFloatingOrbTooltip(callState: CallSessionState): string {
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
  activeTab: "chat" | "agents" = "chat",
  activeTaskCount: number = 0,
): string {
  const safeState = escapeHtml(callState);
  const stateMeta = CALL_STATE_LABELS[callState] ?? CALL_STATE_LABELS.standby;

  const projectCount = active?.projects?.length ?? 0;
  const projectNamesTitle =
    active?.projects && active.projects.length > 0
      ? `Proyectos aislados: ${active.projects.map((p) => p.name).join(", ")}`
      : "";
  const projectsBadgeHtml =
    active && active.topology === "multi-project" && projectCount > 0
      ? `<span class="workspace-projects-badge" title="${escapeHtml(projectNamesTitle)}">${projectCount} ${projectCount === 1 ? "proyecto" : "proyectos"}</span>`
      : "";

  const chipHtml = active
    ? `<span class="workspace-chip" title="${escapeHtml(active.canonical_path)}">${escapeHtml(active.display_name)}</span>${projectsBadgeHtml}`
    : `<span class="workspace-chip workspace-chip--empty" title="Modo conversacional en ~ (sin acceso a archivos)">Sin workspace</span>`;

  const clearBtnHtml = active
    ? `<button type="button" class="btn btn--ghost btn-clear-ws" data-action="clear-workspace" title="Salir del workspace y volver a modo conversacional">Salir</button>`
    : "";

  const optionItems = options
    .map((opt) => {
      const selected = active && opt.canonical_path === active.canonical_path ? " selected" : "";
      return `<option value="${escapeHtml(opt.canonical_path)}" title="${escapeHtml(opt.canonical_path)}"${selected}>${escapeHtml(opt.display_name)}</option>`;
    })
    .join("");

  const fallbackNote = pickerFallbackOpen
    ? `<span class="workspace-fallback-hint" role="status">Selector nativo no disponible; elige un proyecto del catálogo confinado.</span>`
    : "";

  const activeBadgeHtml =
    activeTaskCount > 0
      ? `<span class="nova-tab-badge" aria-label="${activeTaskCount} tareas activas">${activeTaskCount}</span>`
      : "";

  return `
    <div class="workspace-bar">
      <div class="workspace-identity">
        <span class="workspace-brand">NOVA</span>
        <div data-slot="state-indicator" role="status" aria-live="polite" class="nova-state-indicator">
          <span class="nova-state-pill" data-state="${safeState}">${escapeHtml(stateMeta.title)}</span>
        </div>
      </div>
      <div class="nova-view-switcher" role="tablist" aria-label="Vistas principales">
        <button
          type="button"
          role="tab"
          class="nova-tab-btn ${activeTab === "chat" ? "nova-tab-btn--active" : ""}"
          data-action="switch-tab"
          data-tab="chat"
          aria-selected="${activeTab === "chat" ? "true" : "false"}"
          title="Vista de conversación y chat"
        >
          <span class="tab-icon" aria-hidden="true">💬</span> Chat
        </button>
        <button
          type="button"
          role="tab"
          class="nova-tab-btn ${activeTab === "agents" ? "nova-tab-btn--active" : ""}"
          data-action="switch-tab"
          data-tab="agents"
          aria-selected="${activeTab === "agents" ? "true" : "false"}"
          title="Vista de agentes y tareas en ejecución"
        >
          <span class="tab-icon" aria-hidden="true">🤖</span> Agentes${activeBadgeHtml ? ` ${activeBadgeHtml}` : ""}
        </button>
      </div>
      <div class="workspace-view-actions">
        <button
          type="button"
          class="btn-toggle-view btn-settings"
          data-action="open-settings"
          title="Ajustes"
          aria-label="Ajustes"
        >
          ⚙ Ajustes
        </button>
        <button
          type="button"
          class="btn-toggle-view"
          data-action="toggle-ui-mode"
          title="Modo widget"
          aria-label="Modo widget"
        >
          Modo widget
        </button>
        <button
          type="button"
          class="btn-toggle-view btn-quit-app"
          data-action="quit-app"
          title="Cerrar Nova"
          aria-label="Cerrar Nova"
        >
          ✕
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
        ${clearBtnHtml}
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

const ERROR_CATALOG: Readonly<Record<string, string>> = {
  WORKSPACE_NOT_FOUND: "No se encontró el proyecto indicado.",
  WORKSPACE_HOME_FORBIDDEN:
    "La carpeta personal del usuario no puede utilizarse como workspace; seleccioná un workspace aislado.",
  WORKSPACE_OUTSIDE_ALLOWED_ROOTS: "El proyecto se encuentra fuera de las carpetas permitidas.",
  WORKSPACE_ALREADY_EXISTS: "Ya existe un workspace configurado en esa carpeta.",
  WORKSPACE_INVALID_MANIFEST:
    "El manifiesto del workspace o los nombres de proyectos no son válidos.",
  WORKSPACE_AMBIGUOUS: "Se encontraron varios proyectos que coinciden con el nombre.",
  WORKSPACE_RESOLUTION_TIMEOUT: "Se agotó el tiempo al intentar resolver el proyecto.",
  DIAGNOSTIC_INVALID_PARAMS: "Los parámetros de la prueba de diagnóstico no son válidos.",
  UI_DIAGNOSTICS_UNAVAILABLE: "El servicio de diagnóstico en vivo no está disponible.",
  UI_VOICE_UNAVAILABLE: "El servicio de voz no está disponible en este momento.",
  UI_EMPTY_UTTERANCE: "El mensaje no puede estar vacío.",
  UI_PAYLOAD_TOO_LARGE: "El tamaño del contenido supera el límite permitido.",
  UI_FORBIDDEN_ORIGIN_OR_CSRF: "La solicitud fue rechazada por seguridad.",
  UI_SHUTDOWN_UNAVAILABLE: "El servicio de apagado no está disponible en este momento.",
  ORCHESTRATOR_UNEXPECTED_FAILURE: "Ocurrió un error inesperado al procesar la solicitud.",
  COMPONENTS_MISSING: "Faltan componentes necesarios para el funcionamiento de Nova.",
};

const GENERIC_ERROR_FALLBACK = "Ocurrió un error inesperado al procesar la solicitud.";

export function resolveHumanNotice(code: string, customMessage?: string): UiNoticeBanner {
  let message: string;
  if (code === "COMPONENTS_MISSING" && customMessage && customMessage.trim().length > 0) {
    message = customMessage.trim();
  } else {
    message =
      ERROR_CATALOG[code] ??
      (customMessage && customMessage.trim().length > 0
        ? customMessage.trim()
        : GENERIC_ERROR_FALLBACK);
  }
  return {
    code,
    message,
    dismissible: true,
  };
}

const ROLE_BADGE_MAP: Readonly<Record<string, string>> = {
  user: "Tú",
  assistant: "Nova",
  system: "Sistema",
  security: "Gatekeeper",
};

export function renderWorkspaceEmptyState(
  options: readonly UiWorkspaceOption[],
  initWorkspaceOpen = false,
  allowedRoots: readonly string[] = [],
): string {
  const optionsHtml =
    options.length > 0
      ? `
      <div class="workspace-catalog">
        ${options
          .map((opt) => {
            const countHint =
              opt.topology === "multi-project" && typeof opt.project_count === "number"
                ? ` (${opt.project_count} proy.)`
                : "";
            return `
          <button
            type="button"
            class="btn-workspace-option"
            data-action="select-workspace-option"
            data-canonical-path="${escapeHtml(opt.canonical_path)}"
          >
            ${escapeHtml(opt.display_name)}${escapeHtml(countHint)}
          </button>
        `;
          })
          .join("")}
      </div>
    `.trim()
      : "";

  const defaultParentRoot = allowedRoots[0] ?? "";
  const initFormHtml = initWorkspaceOpen
    ? `
      <form class="workspace-init-form" data-action="init-workspace-form">
        <input type="hidden" name="parent_root" value="${escapeHtml(defaultParentRoot)}" />
        <label class="nova-settings-field">
          <span>Identificador del workspace (carpeta)</span>
          <input type="text" name="workspace_slug" placeholder="cliente-acme-workspace" maxlength="80" required />
        </label>
        <label class="nova-settings-field">
          <span>Nombre visible</span>
          <input type="text" name="display_name" placeholder="Cliente Acme" maxlength="100" />
        </label>
        <label class="nova-settings-field">
          <span>Proyectos aislados (separados por coma)</span>
          <input type="text" name="projects" placeholder="api-backend, web-portal" maxlength="240" />
        </label>
        <div class="workspace-init-actions">
          <button type="submit" class="btn btn--primary">Crear workspace</button>
          <button type="button" class="btn btn--ghost" data-action="toggle-init-workspace">Cancelar</button>
        </div>
      </form>
    `.trim()
    : "";

  return `
    <div class="workspace-empty-state" data-slot="workspace-empty-state">
      <h2 class="workspace-empty-title">Elegí un proyecto</h2>
      <p class="workspace-empty-desc">Modo conversacional activo sin acceso a archivos. Podés conversar ahora mismo o seleccionar un workspace aislado.</p>
      ${optionsHtml}
      <div class="workspace-empty-actions">
        <button
          type="button"
          class="btn btn--secondary"
          data-action="pick-native"
        >
          Abrir carpeta…
        </button>
        ${
          defaultParentRoot
            ? `<button type="button" class="btn btn--ghost" data-action="toggle-init-workspace">Nuevo workspace…</button>`
            : ""
        }
      </div>
      ${initFormHtml}
    </div>
  `.trim();
}

export function renderChatTimeline(
  status: ViewStatus,
  messages: readonly UiChatMessage[],
  errorBanner: string | null,
  activeWorkspace?: UiWorkspaceView | null,
  availableWorkspaces: readonly UiWorkspaceOption[] = [],
  initWorkspaceOpen = false,
  allowedRoots: readonly string[] = [],
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

  const visibleMessages = messages.filter((msg) => msg.modality !== "error");

  if (activeWorkspace === null && visibleMessages.length === 0) {
    return `
      <div class="chat-timeline chat-timeline--state" data-view-status="${escapeHtml(status)}">
        ${safeError}
        ${renderWorkspaceEmptyState(availableWorkspaces, initWorkspaceOpen, allowedRoots)}
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
      const voiceIconHtml =
        msg.modality === "voice"
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
    const describedByAttr = !hasWorkspace ? ' aria-describedby="composer-conversational-hint"' : "";

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
              required${describedByAttr}
            ></textarea>
            ${!hasWorkspace ? `<span id="composer-conversational-hint" class="sr-only">Modo conversacional activo sin workspace (sin acceso a archivos)</span>` : ""}
            <div class="composer-actions">
              <button
                type="button"
                class="btn-icon nova-ptt-hold-btn"
                data-action="ptt-hold"
                title="Mantener para hablar"
                aria-label="Mantener para hablar"${!model.voiceEnabled ? ' disabled aria-disabled="true"' : ""}
              >
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
                  <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
                  <line x1="12" y1="19" x2="12" y2="23" />
                  <line x1="8" y1="23" x2="16" y2="23" />
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

function renderAudioDeviceOptions(
  devices: readonly AudioDeviceInfo[],
  selectedId: string | null,
): string {
  const defaultSelected = selectedId === null || selectedId === "" ? " selected" : "";
  const items = devices
    .map((dev) => {
      const sel = selectedId === dev.id ? " selected" : "";
      const label = dev.is_default ? `${dev.description} (Predeterminado)` : dev.description;
      return `<option value="${escapeHtml(dev.id)}"${sel}>${escapeHtml(label)}</option>`;
    })
    .join("");
  return `<option value=""${defaultSelected}>Predeterminado del sistema</option>${items}`;
}

function renderLiveDiagnosticsPanel(diag: LiveDiagnosticsState | null | undefined): string {
  const running = diag?.runningTarget ?? null;
  const busyAttr = running !== null ? " disabled" : "";
  const mic = diag?.microphone ?? null;
  const out = diag?.audioOutput ?? null;
  const pi = diag?.pi ?? null;
  const agy = diag?.antigravity ?? null;

  const micDetail = mic
    ? `<p class="diag-result ${mic.ok ? "diag-result--ok" : "diag-result--warn"}">${escapeHtml(mic.message)} (pico ${(mic.peak_level * 100).toFixed(0)}%)${mic.transcript_preview ? ` · «${escapeHtml(mic.transcript_preview)}»` : ""}</p>`
    : "";
  const outDetail = out
    ? `<p class="diag-result ${out.ok ? "diag-result--ok" : "diag-result--warn"}">${escapeHtml(out.message)} (${out.latency_ms.toFixed(0)} ms)</p>`
    : "";
  const piDetail = pi
    ? `<p class="diag-result ${pi.ok ? "diag-result--ok" : "diag-result--warn"}">${escapeHtml(pi.message)} (${pi.latency_ms.toFixed(0)} ms)</p>`
    : "";
  const agyDetail = agy
    ? `<p class="diag-result ${agy.ok ? "diag-result--ok" : "diag-result--warn"}">${escapeHtml(agy.message)} (${agy.latency_ms.toFixed(0)} ms)</p>`
    : "";

  return `
    <div class="nova-diagnostics-grid">
      <div class="diag-card">
        <div class="diag-card-row">
          <span>1. Micrófono</span>
          <button type="button" class="btn btn--secondary" data-action="diag-microphone"${busyAttr}>
            ${running === "microphone" ? "Probando…" : "Probar micrófono"}
          </button>
        </div>
        ${micDetail}
      </div>
      <div class="diag-card">
        <div class="diag-card-row">
          <span>2. Salida de audio</span>
          <button type="button" class="btn btn--secondary" data-action="diag-audio-output"${busyAttr}>
            ${running === "audio-output" ? "Probando…" : "Probar audio"}
          </button>
        </div>
        ${outDetail}
      </div>
      <div class="diag-card">
        <div class="diag-card-row">
          <span>3. Conexión Nova ↔ Pi</span>
          <button type="button" class="btn btn--secondary" data-action="diag-pi"${busyAttr}>
            ${running === "pi" ? "Verificando…" : "Probar Pi"}
          </button>
        </div>
        ${piDetail}
      </div>
      <div class="diag-card">
        <div class="diag-card-row">
          <span>4. Conexión Nova ↔ Antigravity</span>
          <button type="button" class="btn btn--secondary" data-action="diag-antigravity"${busyAttr}>
            ${running === "antigravity" ? "Verificando…" : "Probar Antigravity"}
          </button>
        </div>
        ${agyDetail}
      </div>
      <div class="diag-actions-bar">
        <button type="button" class="btn btn--ghost" data-action="diag-run-all"${busyAttr}>
          ${running === "all" ? "Ejecutando pruebas…" : "Ejecutar las 4 pruebas"}
        </button>
      </div>
    </div>
  `.trim();
}

export function renderOnboardingSystemReadiness(
  healthReport: SystemHealthReport | null | undefined,
  modelsStatus: ModelsProvisionState | null | undefined,
  rechecking: boolean,
): string {
  const models = modelsStatus?.models ?? [];
  const missingModels = models.filter((m) => !m.installed);
  const isProvisioning = modelsStatus?.status === "running";
  const allModelsOk = modelsStatus ? modelsStatus.all_installed : true;
  const installedModelsCount = models.length - missingModels.length;

  let modelsBlock = "";
  if (modelsStatus && models.length > 0) {
    if (isProvisioning) {
      const pct = Math.max(0, Math.min(100, modelsStatus.percent));
      const cur = modelsStatus.current_model ? escapeHtml(modelsStatus.current_model) : "modelos";
      modelsBlock = `
        <div class="nova-onboarding-readiness-row" data-slot="models-progress">
          <div class="nova-onboarding-readiness-head">
            <span>Descargando ${cur} (${modelsStatus.completed_models}/${modelsStatus.total_models})</span>
            <strong>${pct}%</strong>
          </div>
          <progress max="100" value="${pct}" aria-label="Progreso de descarga de modelos de voz"></progress>
        </div>
      `.trim();
    } else if (allModelsOk) {
      modelsBlock = `
        <div class="nova-onboarding-readiness-row nova-onboarding-readiness-row--ok" data-slot="models-ready">
          <span>Modelos de voz locales listos (${installedModelsCount}/${models.length})</span>
        </div>
      `.trim();
    } else {
      const missingNames = missingModels.map((m) => escapeHtml(m.filename)).join(", ");
      const errHtml = modelsStatus.error
        ? `<p class="nova-settings-error" role="alert">${escapeHtml(modelsStatus.error)}</p>`
        : "";
      const btnLabel =
        modelsStatus.status === "error"
          ? "Reintentar instalación de modelos"
          : "Instalar modelos de voz";
      modelsBlock = `
        <div class="nova-onboarding-readiness-row nova-onboarding-readiness-row--warn" data-slot="models-missing">
          <p>Faltan modelos de voz: <code>${missingNames}</code></p>
          ${errHtml}
          <button type="button" class="btn btn--secondary" data-action="provision-models">
            ${btnLabel}
          </button>
        </div>
      `.trim();
    }
  }

  const nonModelMissing = (healthReport?.components ?? []).filter(
    (c) => c.category !== "model" && !c.installed,
  );
  const pmLabel =
    healthReport?.host_environment?.package_manager &&
    healthReport.host_environment.package_manager !== "unknown"
      ? ` (${escapeHtml(healthReport.host_environment.package_manager)})`
      : "";

  let depsBlock = "";
  if (nonModelMissing.length > 0) {
    const itemsHtml = nonModelMissing
      .map((c) => {
        const badge = c.required ? "Requerido" : "Opcional";
        const remedyHtml = c.remedy
          ? `<code class="nova-onboarding-remedy">${escapeHtml(c.remedy)}</code>`
          : "";
        return `
          <li class="nova-onboarding-dep-item" data-component="${escapeHtml(c.name)}">
            <strong>${escapeHtml(c.name)}</strong> <span>[${badge}]</span>: ${escapeHtml(c.details)}
            ${remedyHtml}
          </li>
        `.trim();
      })
      .join("");
    depsBlock = `
      <div class="nova-onboarding-readiness-row nova-onboarding-readiness-row--warn" data-slot="system-deps-missing">
        <p>Dependencias del sistema faltantes${pmLabel}:</p>
        <ul class="nova-onboarding-dep-list">${itemsHtml}</ul>
      </div>
    `.trim();
  } else if (healthReport) {
    depsBlock = `
      <div class="nova-onboarding-readiness-row nova-onboarding-readiness-row--ok" data-slot="system-deps-ready">
        <span>Herramientas del sistema y audio verificadas${pmLabel}.</span>
      </div>
    `.trim();
  }

  if (!modelsBlock && !depsBlock) {
    return "";
  }

  const recheckDisabled = rechecking || isProvisioning ? " disabled" : "";
  return `
    <div class="nova-settings-section" data-slot="onboarding-system-readiness">
      <h3 class="nova-settings-section-title">Componentes y modelos de voz</h3>
      ${modelsBlock}
      ${depsBlock}
      <div class="nova-onboarding-readiness-actions">
        <button type="button" class="btn btn--secondary" data-action="recheck-health"${recheckDisabled}>
          ${rechecking ? "Verificando…" : "Volver a verificar"}
        </button>
      </div>
    </div>
  `.trim();
}

export function renderOnboardingModal(model: PresentationalAppModel): string {
  if (!model.onboardingOpen) {
    return "";
  }
  const inputs = model.audioDevices?.inputs ?? [];
  const outputs = model.audioDevices?.outputs ?? [];
  const capTarget =
    model.onboardingDraftAudio !== undefined
      ? model.onboardingDraftAudio.captureTarget || null
      : (model.settingsData?.audio.capture_target ?? null);
  const playTarget =
    model.onboardingDraftAudio !== undefined
      ? model.onboardingDraftAudio.playbackTarget || null
      : (model.settingsData?.audio.playback_target ?? null);
  const isProvisioning = model.modelsStatus?.status === "running";
  const savingAttr = model.onboardingSaving || isProvisioning ? " disabled" : "";
  const readinessHtml = renderOnboardingSystemReadiness(
    model.healthReport,
    model.modelsStatus,
    Boolean(model.healthRechecking),
  );
  const modelsStillMissing =
    model.modelsStatus !== undefined &&
    model.modelsStatus !== null &&
    model.modelsStatus.total_models > 0 &&
    !model.modelsStatus.all_installed;
  const finishLabel = model.onboardingSaving
    ? "Guardando…"
    : modelsStillMissing
      ? "Finalizar en modo solo texto"
      : "Finalizar configuración";

  return `
    <div class="nova-settings-backdrop" data-slot="onboarding-modal">
      <section
        class="nova-onboarding-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Configuración inicial y diagnóstico en vivo"
      >
        <div class="nova-settings-header">
          <h2 class="nova-settings-title">Configuración inicial y diagnóstico</h2>
        </div>
        <p class="nova-onboarding-desc">
          Verifica que el micrófono, la salida de audio, los modelos de voz y las conexiones con Pi y Antigravity respondan antes de comenzar. Al finalizar, Nova iniciará en modo conversacional sin workspace activo.
        </p>
        ${readinessHtml}
        <div class="nova-settings-section">
          <h3 class="nova-settings-section-title">Dispositivos de audio</h3>
          <label class="nova-settings-field">
            <span>Micrófono</span>
            <select name="onboarding.capture_target" data-action="onboarding-audio-select">
              ${renderAudioDeviceOptions(inputs, capTarget)}
            </select>
          </label>
          <label class="nova-settings-field">
            <span>Salida de audio</span>
            <select name="onboarding.playback_target" data-action="onboarding-audio-select">
              ${renderAudioDeviceOptions(outputs, playTarget)}
            </select>
          </label>
        </div>
        <div class="nova-settings-section">
          <h3 class="nova-settings-section-title">Pruebas interactivas en vivo</h3>
          ${renderLiveDiagnosticsPanel(model.diagnostics)}
        </div>
        <div class="nova-settings-footer">
          <button
            type="button"
            class="btn btn--primary"
            data-action="complete-onboarding"${savingAttr}
          >
            ${finishLabel}
          </button>
        </div>
      </section>
    </div>
  `.trim();
}

export function renderSettingsDrawer(model: PresentationalAppModel): string {
  if (!model.settingsDrawerOpen) {
    return "";
  }
  const s = model.settingsData;
  if (model.settingsLoading && !s) {
    return `
      <div class="nova-settings-backdrop" data-action="close-settings">
        <aside class="nova-settings-drawer" role="dialog" aria-modal="true" aria-label="Ajustes de Nova">
          <div class="nova-settings-header">
            <h2 class="nova-settings-title">Ajustes</h2>
            <button type="button" class="btn-icon" data-action="close-settings" aria-label="Cerrar ajustes">✕</button>
          </div>
          <p class="nova-settings-loading">Cargando configuración…</p>
          <div class="nova-settings-footer">
            <button type="button" class="btn btn--reject btn-quit-drawer" data-action="quit-app">Cerrar Nova</button>
          </div>
        </aside>
      </div>
    `.trim();
  }

  const inputs = model.audioDevices?.inputs ?? [];
  const outputs = model.audioDevices?.outputs ?? [];
  const capTarget = s?.audio.capture_target ?? null;
  const playTarget = s?.audio.playback_target ?? null;
  const sensitivity = s?.voice.sensitivity_preset ?? "normal";
  const ttsSpeed = s?.voice.tts_speed ?? 1.1;
  const ttsEngine = s?.voice.tts_engine ?? "kokoro";
  const whisperDevice = s?.voice.whisper_device === "cpu" ? "cpu" : "cuda";
  const whisperModel = s?.voice.whisper_model ?? "small";
  const knownModels = ["tiny", "base", "small", "medium"];
  const customModelOption = !knownModels.includes(whisperModel)
    ? `<option value="${escapeHtml(whisperModel)}" selected>${escapeHtml(whisperModel)}</option>`
    : "";
  const whisperLang = s?.voice.whisper_language ?? "";
  const pttMode = s?.shortcuts.ptt_mode ?? "single_turn";
  const pttShortcut = s?.shortcuts.global_shortcut ?? "Super+Shift+Space";
  const piProvider = s?.models.pi_provider ?? "github-copilot";
  const piModel = s?.models.pi_model ?? "gpt-4.1";
  const tier2Model = s?.models.tier2_model ?? "claude-opus-5-5-high";
  const rememberWs = s?.general.remember_workspace ?? true;
  const savingAttr = model.settingsSaving ? " disabled" : "";

  const errorBanner = model.settingsError
    ? `<div class="nova-settings-alert nova-settings-alert--error" role="alert">${escapeHtml(model.settingsError)}</div>`
    : "";
  const noticeBanner = model.settingsNotice
    ? `<div class="nova-settings-alert nova-settings-alert--notice" role="status">${escapeHtml(model.settingsNotice)}</div>`
    : "";
  const sc = model.shortcutStatus;
  const shortcutHint = sc
    ? sc.status === "registered"
      ? `<p class="nova-settings-hint nova-settings-hint--ok">Atajo registrado: <code>${escapeHtml(sc.shortcut)}</code> → <code>${escapeHtml(sc.command)}</code></p>`
      : `<p class="nova-settings-hint">Configura manualmente en tu escritorio: <code>${escapeHtml(sc.manual_command ?? sc.command)}</code></p>`
    : "";

  return `
    <div class="nova-settings-backdrop">
      <aside class="nova-settings-drawer" role="dialog" aria-modal="true" aria-label="Ajustes de Nova">
        <div class="nova-settings-header">
          <h2 class="nova-settings-title">Ajustes</h2>
          <button type="button" class="btn-icon" data-action="close-settings" aria-label="Cerrar ajustes">✕</button>
        </div>
        ${errorBanner}
        ${noticeBanner}
        <form class="nova-settings-form" data-action="settings-form">
          <section class="nova-settings-section">
            <h3 class="nova-settings-section-title">Audio</h3>
            <label class="nova-settings-field">
              <span>Micrófono</span>
              <select name="audio.capture_target">${renderAudioDeviceOptions(inputs, capTarget)}</select>
            </label>
            <label class="nova-settings-field">
              <span>Salida de audio</span>
              <select name="audio.playback_target">${renderAudioDeviceOptions(outputs, playTarget)}</select>
            </label>
          </section>

          <section class="nova-settings-section">
            <h3 class="nova-settings-section-title">Voz y sensibilidad</h3>
            <label class="nova-settings-field">
              <span>Sensibilidad de activación</span>
              <select name="voice.sensitivity_preset">
                <option value="low"${sensitivity === "low" ? " selected" : ""}>Baja (menos falsos positivos)</option>
                <option value="normal"${sensitivity === "normal" ? " selected" : ""}>Normal</option>
                <option value="high"${sensitivity === "high" ? " selected" : ""}>Alta (más sensible)</option>
              </select>
            </label>
            <label class="nova-settings-field">
              <span>Velocidad de voz (${ttsSpeed.toFixed(2)}×)</span>
              <input type="range" name="voice.tts_speed" min="0.5" max="2.0" step="0.05" value="${ttsSpeed}" />
            </label>
            <label class="nova-settings-field">
              <span>Motor TTS</span>
              <select name="voice.tts_engine">
                <option value="kokoro"${ttsEngine === "kokoro" ? " selected" : ""}>Kokoro ONNX</option>
                <option value="piper"${ttsEngine === "piper" ? " selected" : ""}>Piper ONNX</option>
              </select>
            </label>
            <label class="nova-settings-field">
              <span>Dispositivo STT (Whisper)</span>
              <select name="voice.whisper_device">
                <option value="cuda"${whisperDevice === "cuda" ? " selected" : ""}>GPU (CUDA)</option>
                <option value="cpu"${whisperDevice === "cpu" ? " selected" : ""}>CPU (Sin tarjeta gráfica)</option>
              </select>
            </label>
            <label class="nova-settings-field">
              <span>Modelo STT (Whisper)</span>
              <select name="voice.whisper_model">
                ${customModelOption}
                <option value="tiny"${whisperModel === "tiny" ? " selected" : ""}>tiny (mínimo consumo CPU)</option>
                <option value="base"${whisperModel === "base" ? " selected" : ""}>base (balanceado CPU)</option>
                <option value="small"${whisperModel === "small" ? " selected" : ""}>small (recomendado GPU / CPU veloz)</option>
                <option value="medium"${whisperModel === "medium" ? " selected" : ""}>medium (alta precisión)</option>
              </select>
            </label>
            <label class="nova-settings-field">
              <span>Idioma Whisper (vacío = auto)</span>
              <input type="text" name="voice.whisper_language" value="${escapeHtml(whisperLang)}" placeholder="es, en, ..." maxlength="16" />
            </label>
          </section>

          <section class="nova-settings-section">
            <h3 class="nova-settings-section-title">Push-to-Talk global</h3>
            <label class="nova-settings-field">
              <span>Modo del atajo</span>
              <select name="shortcuts.ptt_mode">
                <option value="single_turn"${pttMode === "single_turn" ? " selected" : ""}>Turno único (recomendado)</option>
                <option value="toggle_call"${pttMode === "toggle_call" ? " selected" : ""}>Alternar llamada continua</option>
              </select>
            </label>
            <label class="nova-settings-field">
              <span>Combinación de teclas</span>
              <div class="nova-settings-inline">
                <input type="text" name="shortcuts.global_shortcut" value="${escapeHtml(pttShortcut)}" maxlength="64" />
                <button type="button" class="btn btn--secondary" data-action="register-shortcut"${savingAttr}>Registrar</button>
              </div>
            </label>
            ${shortcutHint}
          </section>

          <section class="nova-settings-section">
            <h3 class="nova-settings-section-title">Modelos y general</h3>
            <label class="nova-settings-field">
              <span>Proveedor Tier 1</span>
              <input type="text" name="models.pi_provider" value="${escapeHtml(piProvider)}" maxlength="64" />
            </label>
            <label class="nova-settings-field">
              <span>Modelo Tier 1</span>
              <input type="text" name="models.pi_model" value="${escapeHtml(piModel)}" maxlength="128" />
            </label>
            <label class="nova-settings-field">
              <span>Modelo Tier 2 (Agente)</span>
              <input type="text" name="models.tier2_model" value="${escapeHtml(tier2Model)}" maxlength="128" />
            </label>
            <label class="nova-settings-checkbox">
              <input type="checkbox" name="general.remember_workspace"${rememberWs ? " checked" : ""} />
              <span>Recordar último workspace al iniciar</span>
            </label>
          </section>

          <section class="nova-settings-section">
            <h3 class="nova-settings-section-title">Diagnóstico y pruebas en vivo</h3>
            ${renderLiveDiagnosticsPanel(model.diagnostics)}
          </section>

          <div class="nova-settings-footer">
            <button type="button" class="btn btn--reject btn-quit-drawer" data-action="quit-app">Cerrar Nova</button>
            <button type="button" class="btn btn--ghost" data-action="close-settings">Cancelar</button>
            <button type="submit" class="btn btn--primary"${savingAttr}>${model.settingsSaving ? "Guardando…" : "Guardar"}</button>
          </div>
        </form>
      </aside>
    </div>
  `.trim();
}

export function getDefaultAgentsOverview(_activeWorkspacePath?: string | null): UiAgentsOverview {
  return {
    daemonStatus: "connected",
    poolSlots: [
      {
        name: "Tier 1: Pi Agent",
        tier: "tier1_pi",
        activeSlots: 0,
        maxSlots: 4,
        status: "idle",
      },
      {
        name: "Tier 2: Antigravity CLI",
        tier: "tier2_agy",
        activeSlots: 0,
        maxSlots: 3,
        status: "idle",
      },
    ],
    tasks: [],
  };
}

const TASK_STATUS_META: Readonly<Record<string, { label: string; icon: string; cssClass: string }>> = {
  running: {
    label: "En ejecución",
    icon: "⟳",
    cssClass: "status--running",
  },
  gatekeeper_blocked: {
    label: "Gatekeeper pendiente",
    icon: "⚠",
    cssClass: "status--gatekeeper",
  },
  queued: {
    label: "En cola",
    icon: "⏳",
    cssClass: "status--queued",
  },
  completed: {
    label: "Completada",
    icon: "✓",
    cssClass: "status--completed",
  },
  failed: {
    label: "Falló",
    icon: "✕",
    cssClass: "status--failed",
  },
};

export function renderAgentsMatrixView(model: PresentationalAppModel): string {
  const overview = model.agentsOverview ?? getDefaultAgentsOverview(model.activeWorkspace?.canonical_path);
  const activeFilter: AgentsFilterKey = model.agentsFilter ?? "all";

  const allTasks = overview.tasks;
  const runningTasks = allTasks.filter((t) => t.status === "running");
  const gatekeeperTasks = allTasks.filter((t) => t.status === "gatekeeper_blocked");
  const queuedTasks = allTasks.filter((t) => t.status === "queued");
  const completedTasks = allTasks.filter((t) => t.status === "completed");

  const filteredTasks = allTasks.filter((task) => {
    if (activeFilter === "running") return task.status === "running";
    if (activeFilter === "gatekeeper") return task.status === "gatekeeper_blocked";
    if (activeFilter === "queued") return task.status === "queued";
    if (activeFilter === "completed") return task.status === "completed";
    return true;
  });

  const slotsHtml = overview.poolSlots
    .map((slot) => {
      const isFull = slot.activeSlots >= slot.maxSlots;
      const tierLabel = slot.tier === "tier1_pi" ? "Tier 1: Tareas Rápidas" : "Tier 2: Refactor Profundo";
      const pct = slot.maxSlots > 0 ? Math.round((slot.activeSlots / slot.maxSlots) * 100) : 0;
      return `
        <div class="nova-agent-slot-card" data-tier="${escapeHtml(slot.tier)}">
          <div class="nova-agent-slot-header">
            <span class="nova-agent-slot-name">${escapeHtml(slot.name)}</span>
            <span class="nova-agent-slot-badge ${slot.activeSlots > 0 ? "badge--active" : "badge--idle"}">
              ${slot.activeSlots > 0 ? "Activo" : "En espera"}
            </span>
          </div>
          <div class="nova-agent-slot-meta">
            <span class="nova-agent-slot-desc">${escapeHtml(tierLabel)}</span>
            <span class="nova-agent-slot-count ${isFull ? "count--full" : ""}">${slot.activeSlots}/${slot.maxSlots} slots</span>
          </div>
          <div class="nova-agent-slot-bar-track">
            <div class="nova-agent-slot-bar-fill" style="width: ${pct}%"></div>
          </div>
        </div>
      `;
    })
    .join("");

  const tasksListHtml =
    filteredTasks.length === 0
      ? `
        <div class="nova-agents-empty" role="status">
          <div class="nova-empty-icon" aria-hidden="true">🤖</div>
          <p class="nova-empty-title">No hay tareas en esta categoría</p>
          <p class="nova-empty-subtitle">Puedes solicitar una tarea por voz o texto usando la barra inferior para que los agentes comiencen a ejecutarla en segundo plano.</p>
        </div>
      `
      : filteredTasks
          .map((task) => {
            const meta = TASK_STATUS_META[task.status] ?? TASK_STATUS_META["queued"]!;
            const isBlocked = task.status === "gatekeeper_blocked";
            const progress = typeof task.progress === "number" ? task.progress : 0;
            const branchHtml = task.branch
              ? `<span class="task-badge task-badge--branch" title="Rama git"><span class="badge-icon">🌿</span> ${escapeHtml(task.branch)}</span>`
              : "";
            const durationHtml = task.duration
              ? `<span class="task-badge task-badge--time"><span class="badge-icon">⏱</span> ${escapeHtml(task.duration)}${task.status === "running" ? ` • ${progress}%` : ""}</span>`
              : "";
            const logHtml = task.lastLog
              ? `
                <div class="nova-task-log-row" title="Registro de actividad en streaming">
                  <code>${escapeHtml(task.lastLog)}</code>
                </div>
              `
              : "";

            const actionsHtml = isBlocked
              ? `
                <div class="nova-task-actions">
                  <button type="button" class="btn btn--primary btn--compact" data-action="approve-task" data-task-id="${escapeHtml(task.id)}">Aprobar</button>
                  <button type="button" class="btn btn--ghost btn--compact" data-action="inspect-task" data-task-id="${escapeHtml(task.id)}">Ver detalles</button>
                  <button type="button" class="btn btn--reject btn--compact" data-action="cancel-task" data-task-id="${escapeHtml(task.id)}">Rechazar</button>
                </div>
              `
              : `
                <div class="nova-task-actions">
                  <button type="button" class="btn btn--ghost btn--compact" data-action="inspect-task" data-task-id="${escapeHtml(task.id)}">Ver detalles</button>
                  ${
                    task.status === "running"
                      ? `<button type="button" class="btn btn--ghost btn--compact" data-action="pause-task" data-task-id="${escapeHtml(task.id)}">Pausar</button>`
                      : ""
                  }
                  ${
                    task.status === "running" || task.status === "queued"
                      ? `<button type="button" class="btn btn--reject btn--compact" data-action="cancel-task" data-task-id="${escapeHtml(task.id)}">Cancelar</button>`
                      : ""
                  }
                </div>
              `;

            return `
              <article class="nova-task-card ${meta.cssClass}" data-task-id="${escapeHtml(task.id)}" data-task-status="${escapeHtml(task.status)}">
                <div class="nova-task-card-header">
                  <div class="nova-task-title-group">
                    <span class="nova-task-id">#tsk-${escapeHtml(task.id)}</span>
                    <h3 class="nova-task-title">${escapeHtml(task.title)}</h3>
                  </div>
                  <span class="nova-task-status-pill ${meta.cssClass}">
                    <span class="status-icon" aria-hidden="true">${meta.icon}</span>
                    <span>${escapeHtml(meta.label)}</span>
                  </span>
                </div>

                ${task.description ? `<p class="nova-task-desc">${escapeHtml(task.description)}</p>` : ""}

                <div class="nova-task-badges-row">
                  <span class="task-badge task-badge--workspace" title="Espacio de trabajo">
                    <span class="badge-icon">📁</span> ${escapeHtml(task.workspace)}
                  </span>
                  <span class="task-badge task-badge--agent" title="Agente asignado">
                    <span class="badge-icon">🤖</span> ${escapeHtml(task.agentName)}
                  </span>
                  ${branchHtml}
                  ${durationHtml}
                </div>

                ${
                  task.status === "running"
                    ? `
                      <div class="nova-task-progress-track">
                        <div class="nova-task-progress-fill" style="width: ${progress}%"></div>
                      </div>
                    `
                    : ""
                }

                ${logHtml}
                ${actionsHtml}
              </article>
            `;
          })
          .join("");

  const activeCount = runningTasks.length + gatekeeperTasks.length;
  const daemonSubText =
    overview.daemonPid != null && overview.memoryMb != null
      ? `PID ${overview.daemonPid} • ${overview.memoryMb} MB en uso`
      : overview.daemonPid != null
        ? `PID ${overview.daemonPid}`
        : activeCount > 0
          ? `${activeCount} en curso`
          : "Sin tareas en ejecución";

  return `
    <div class="nova-agents-matrix-container">
      <section class="nova-agents-system-bar" aria-label="Estado del servicio en segundo plano">
        <div class="nova-daemon-indicator">
          <span class="nova-daemon-pulse" aria-hidden="true"></span>
          <span class="nova-daemon-label">Daemon: <strong>${overview.daemonStatus === "disconnected" ? "Desconectado" : "Conectado"}</strong></span>
          <span class="nova-daemon-sub">${escapeHtml(daemonSubText)}</span>
        </div>
        <div class="nova-agent-slots-grid">
          ${slotsHtml}
        </div>
      </section>

      <section class="nova-agents-filter-bar" role="toolbar" aria-label="Filtro de tareas">
        <button
          type="button"
          class="nova-filter-pill ${activeFilter === "all" ? "nova-filter-pill--active" : ""}"
          data-action="filter-agents"
          data-filter="all"
          aria-pressed="${activeFilter === "all" ? "true" : "false"}"
        >
          Todas <span class="filter-count">${allTasks.length}</span>
        </button>
        <button
          type="button"
          class="nova-filter-pill ${activeFilter === "running" ? "nova-filter-pill--active" : ""}"
          data-action="filter-agents"
          data-filter="running"
          aria-pressed="${activeFilter === "running" ? "true" : "false"}"
        >
          En ejecución <span class="filter-count">${runningTasks.length}</span>
        </button>
        <button
          type="button"
          class="nova-filter-pill ${activeFilter === "gatekeeper" ? "nova-filter-pill--active" : ""}"
          data-action="filter-agents"
          data-filter="gatekeeper"
          aria-pressed="${activeFilter === "gatekeeper" ? "true" : "false"}"
        >
          Gatekeeper <span class="filter-count">${gatekeeperTasks.length}</span>
        </button>
        <button
          type="button"
          class="nova-filter-pill ${activeFilter === "queued" ? "nova-filter-pill--active" : ""}"
          data-action="filter-agents"
          data-filter="queued"
          aria-pressed="${activeFilter === "queued" ? "true" : "false"}"
        >
          En cola <span class="filter-count">${queuedTasks.length}</span>
        </button>
        <button
          type="button"
          class="nova-filter-pill ${activeFilter === "completed" ? "nova-filter-pill--active" : ""}"
          data-action="filter-agents"
          data-filter="completed"
          aria-pressed="${activeFilter === "completed" ? "true" : "false"}"
        >
          Completadas <span class="filter-count">${completedTasks.length}</span>
        </button>
      </section>

      <section class="nova-tasks-grid" aria-label="Listado de tareas concurrentes">
        ${tasksListHtml}
      </section>
    </div>
  `.trim();
}

export function renderAppShell(model: PresentationalAppModel): string {
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
            <button
              type="button"
              class="nova-widget-close-btn"
              data-action="quit-app"
              title="Cerrar Nova"
              aria-label="Cerrar Nova"
            >
              ×
            </button>
          </div>
        </div>
      </div>
    `.trim();
  }

  const activeTab = model.activeTab ?? "chat";
  const overview = model.agentsOverview ?? getDefaultAgentsOverview(model.activeWorkspace?.canonical_path);
  const activeTaskCount = overview.tasks.filter((t) => t.status === "running" || t.status === "gatekeeper_blocked").length;

  const milestoneHtml = model.lastMilestone
    ? `<p class="nova-milestone-toast" aria-live="polite">${escapeHtml(model.lastMilestone)}</p>`
    : "";

  return `
    <div class="nova-layout" data-ui-mode="panel" data-call-state="${escapeHtml(model.callSession.state)}" data-active-tab="${activeTab}">
      <header class="nova-header" data-slot="header">
        ${renderWorkspaceBar(model.activeWorkspace, model.availableWorkspaces, model.pickerFallbackOpen, model.callSession.state, activeTab, activeTaskCount)}
      </header>

      <main class="nova-main" aria-label="${activeTab === "agents" ? "Matriz de agentes y tareas concurrentes" : "Historial de conversación y aprobaciones"}">
        ${
          activeTab === "agents"
            ? `<div class="nova-agents-scroll" data-slot="agents-view">${renderAgentsMatrixView(model)}</div>`
            : `
              <div class="nova-timeline-scroll" data-slot="timeline">
                ${renderChatTimeline(
                  model.status,
                  model.messages,
                  model.errorBanner,
                  model.activeWorkspace,
                  model.availableWorkspaces,
                  Boolean(model.initWorkspaceOpen),
                  model.allowedRoots ?? [],
                )}
              </div>

              <div class="nova-approvals-slot" data-slot="approvals">
                ${renderApprovalBanner(model.pendingApprovals)}
              </div>
            `
        }

        <div data-slot="milestone" aria-live="polite">${milestoneHtml}</div>

        ${renderNoticeSlot(model)}

        <div class="nova-dock" data-slot="dock" data-dock-mode="${dockMode}">
          ${renderDock(model)}
        </div>
      </main>
      <div data-slot="settings-drawer">${renderSettingsDrawer(model)}</div>
      <div data-slot="onboarding-container">${renderOnboardingModal(model)}</div>
    </div>
  `.trim();
}
