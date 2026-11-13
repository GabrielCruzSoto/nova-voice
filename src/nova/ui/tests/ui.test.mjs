import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  escapeHtml,
  renderAppShell,
  renderApprovalBanner,
  renderCallControls,
  renderChatTimeline,
  renderNoticeSlot,
  renderNovaReactiveOrb,
  renderNovaSquircleSvg,
  renderSinusoidalWave,
  resolveHumanNotice,
} from "../static/components.js";
import { DesktopAppController } from "../static/controller.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.resolve(__dirname, "../static");
const SRC_DIR = path.resolve(__dirname, "../src");

test("AC-2 & NFR-2: NovaReactiveOrb reflects all CallSession states and CSS uses GPU-only properties with reduced-motion", () => {
  const states = ["standby", "listening", "thinking", "executing", "speaking", "closed"];
  for (const state of states) {
    const html = renderNovaReactiveOrb(state, true);
    assert.match(html, new RegExp(`data-call-state="${state}"`));
    assert.match(html, /role="status"/);
    assert.match(html, /aria-live="polite"/);
    assert.match(html, /aria-label="[^"]+"/);
  }

  const css = fs.readFileSync(path.join(STATIC_DIR, "styles.css"), "utf8");
  assert.match(css, /@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)/);
  assert.match(css, /:focus-visible/);
  // Verify keyframes never animate layout-thrashing properties (width, height, top, left, margin)
  const keyframeBlocks = css.split("@keyframes").slice(1);
  assert.ok(keyframeBlocks.length >= 2, "Expected GPU keyframe animations for active orb states");
  for (const block of keyframeBlocks) {
    const body = block.slice(0, block.indexOf("\n}\n") > 0 ? block.indexOf("\n}\n") : block.length);
    assert.doesNotMatch(body, /\b(?:width|height|top|left|margin|padding)\s*:/);
  }
});

test("AC-3: ChatTimeline models loading/empty/success/error and escapes untrusted XSS payloads", () => {
  assert.match(renderChatTimeline("loading", [], null), /data-view-status="loading"/);
  assert.match(renderChatTimeline("empty", [], null), /data-view-status="empty"/);
  assert.match(renderChatTimeline("error", [], "Bridge error"), /data-view-status="error"/);

  const xssPayload = `<script>alert("xss")</script><img src=x onerror="evil()">`;
  const escaped = escapeHtml(xssPayload);
  assert.ok(!escaped.includes("<script>"));
  assert.ok(escaped.includes("&lt;script&gt;"));

  const timelineHtml = renderChatTimeline(
    "success",
    [
      {
        message_id: `msg-"onclick="evil()`,
        correlation_id: "corr-1",
        role: "user",
        modality: "text",
        step_type: "user_prompt",
        content: xssPayload,
        timestamp: "2026-11-12T15:30:00Z",
        streaming: false,
      },
    ],
    null,
  );
  assert.match(timelineHtml, /data-view-status="success"/);
  assert.ok(!timelineHtml.includes("<script>"));
  assert.ok(!timelineHtml.includes(`onerror="evil()"`));
  assert.ok(timelineHtml.includes("&lt;script&gt;"));
});

test("AC-5 & NFR-4: Semantic WCAG 2.1 AA landmarks, labels, and alertdialog SecurityGatekeeper cards", () => {
  const approvalCard = renderApprovalBanner([
    {
      audit_id: "aud-99",
      correlation_id: "corr-99",
      source: "tier2_agy",
      sanitized_command: "docker compose up -d",
      cwd: "/home/dev/Proyectos/nova-workspace",
      risk_level: "high",
      spoken_summary: "Confirma levantar contenedores.",
      technical_explanation: "Expone puertos locales",
      expires_in_seconds: 60,
      timestamp: "2026-11-12T15:30:00Z",
    },
  ]);
  assert.match(approvalCard, /role="alertdialog"/);
  assert.match(approvalCard, /data-action="approve-command"/);
  assert.match(approvalCard, /data-action="reject-command"/);

  const fullShell = renderAppShell({
    status: "empty",
    voiceEnabled: true,
    callSession: {
      call_id: "call-1",
      state: "standby",
      turn_count: 0,
      active_correlation_id: null,
    },
    activeWorkspace: {
      display_name: "nova-workspace",
      canonical_path: "/home/dev/Proyectos/nova-workspace",
      selected_via: "ui_picker",
    },
    availableWorkspaces: [
      { display_name: "nova-workspace", canonical_path: "/home/dev/Proyectos/nova-workspace" },
    ],
    pendingApprovals: [],
    messages: [],
    errorBanner: null,
    pickerFallbackOpen: false,
    lastMilestone: null,
  });

  assert.match(fullShell, /<header\b/);
  assert.doesNotMatch(fullShell, /<aside\b/);
  assert.match(fullShell, /<main\b/);
  assert.match(fullShell, /<form\b/);
  assert.match(fullShell, /<label[^>]+for="nova-chat-input"/);
  assert.match(fullShell, /<label[^>]+for="nova-workspace-select"/);
});

test("AC-1 & AC-2: Single-column layout without sidebar and unified header with project chip & state indicator", () => {
  const model = {
    status: "success",
    voiceEnabled: true,
    uiMode: "panel",
    callSession: {
      call_id: "call-1",
      state: "standby",
      turn_count: 0,
      active_correlation_id: null,
    },
    activeWorkspace: {
      display_name: "alpha",
      canonical_path: "/home/dev/Proyectos/alpha",
      selected_via: "exact_path",
    },
    availableWorkspaces: [
      { display_name: "alpha", canonical_path: "/home/dev/Proyectos/alpha" },
      { display_name: "beta", canonical_path: "/home/dev/Proyectos/beta" },
    ],
    pendingApprovals: [
      {
        audit_id: "aud-1",
        correlation_id: "corr-1",
        source: "tier2_agy",
        sanitized_command: "ls",
        cwd: "/home/dev/Proyectos/alpha",
        risk_level: "high",
        spoken_summary: "Listar archivos",
        technical_explanation: "Comando de lectura",
        expires_in_seconds: 60,
        timestamp: "2026-11-13T00:00:00Z",
      },
    ],
    messages: [
      {
        message_id: "msg-1",
        correlation_id: "corr-1",
        role: "user",
        modality: "text",
        step_type: "user_prompt",
        content: "Hola",
        timestamp: "2026-11-13T00:00:00Z",
        streaming: false,
      },
    ],
    errorBanner: "Aviso de prueba",
    noticeBanner: {
      code: "TEST_ERROR",
      message: "Aviso de prueba",
      dismissible: true,
    },
    pickerFallbackOpen: false,
    lastMilestone: null,
  };

  const html = renderAppShell(model);

  assert.ok(!html.includes("nova-sidebar"), "Must not contain nova-sidebar");
  assert.ok(!html.includes("<aside"), "Must not contain <aside");
  assert.ok(!html.includes("nova-workspace-grid"), "Must not contain nova-workspace-grid");
  assert.ok(!html.includes("panel-header-minimal"), "Must not contain panel-header-minimal");

  const headerIdx = html.indexOf('data-slot="header"');
  const timelineIdx = html.indexOf('data-slot="timeline"');
  const approvalsIdx = html.indexOf('data-slot="approvals"');
  const noticeIdx = html.indexOf('data-slot="notice"');
  const dockIdx = html.indexOf('data-slot="dock"');

  assert.ok(headerIdx !== -1, "header slot present");
  assert.ok(timelineIdx !== -1, "timeline slot present");
  assert.ok(approvalsIdx !== -1, "approvals slot present");
  assert.ok(noticeIdx !== -1, "notice slot present");
  assert.ok(dockIdx !== -1, "dock slot present");

  assert.ok(headerIdx < timelineIdx, "header < timeline");
  assert.ok(timelineIdx < approvalsIdx, "timeline < approvals");
  assert.ok(approvalsIdx < noticeIdx, "approvals < notice");
  assert.ok(noticeIdx < dockIdx, "notice < dock");

  const css = fs.readFileSync(path.join(STATIC_DIR, "styles.css"), "utf8");
  assert.doesNotMatch(css, /\.nova-sidebar\b/);
  assert.doesNotMatch(css, /grid-template-columns:\s*300px\s+1fr/);
  assert.match(css, /--conversation-max-width:\s*720px/);
  assert.match(css, /--header-height:\s*56px/);
  assert.match(css, /max-width:\s*var\(--conversation-max-width\)/);
  assert.match(css, /margin-inline:\s*auto/);

  const states = ["standby", "listening", "thinking", "executing", "speaking", "closed"];
  const CALL_TITLES = {
    standby: "En reposo",
    listening: "¡Oye Nova! Escuchando…",
    thinking: "Procesando",
    executing: "Ejecutando",
    speaking: "Respondiendo",
    closed: "Sesión cerrada",
  };

  for (const st of states) {
    const stModel = { ...model, callSession: { ...model.callSession, state: st } };
    const stHtml = renderAppShell(stModel);

    const novaMatches = stHtml.match(/>\s*NOVA\s*</g) || [];
    assert.equal(novaMatches.length, 1, `Expected exactly 1 visible NOVA text, found ${novaMatches.length} in state ${st}`);

    const indicatorMatches = stHtml.match(/data-slot="state-indicator"/g) || [];
    assert.equal(indicatorMatches.length, 1);
    assert.match(stHtml, /data-slot="state-indicator"[^>]*role="status"[^>]*aria-live="polite"/);
    assert.ok(stHtml.includes(CALL_TITLES[st]), `Missing state title ${CALL_TITLES[st]}`);

    assert.ok(!stHtml.includes("panel-standby-badge"));
    const pillMatches = stHtml.match(/class="[^"]*nova-state-pill[^"]*"/g) || [];
    assert.equal(pillMatches.length, 1, `Expected at most 1 nova-state-pill, got ${pillMatches.length}`);

    const headerHtml = stHtml.slice(stHtml.indexOf('data-slot="header"'), stHtml.indexOf("</header>"));
    assert.ok(headerHtml.includes("alpha"), "Header shows chip alpha");
    const headerTextOnly = headerHtml.replace(/<[^>]+>/g, " ");
    assert.ok(!headerTextOnly.includes("/home/dev/Proyectos/"), "Header text nodes must not contain canonical path");

    assert.match(headerHtml, /<option\s+value="\/home\/dev\/Proyectos\/alpha"\s+title="\/home\/dev\/Proyectos\/alpha"[^>]*>alpha<\/option>/);
    assert.match(headerHtml, /id="nova-workspace-select"/);
    assert.match(headerHtml, /data-action="select-workspace-dropdown"/);
    assert.match(headerHtml, /<label[^>]+for="nova-workspace-select"/);
  }
});

test("AC-1, AC-4, AC-5 & frontend-web#3,9: DesktopAppController keeps CSRF in memory only, handles SSE events, 503 picker fallback, 404 approval race, and AbortController cleanup", async () => {
  for (const file of fs.readdirSync(SRC_DIR)) {
    const srcText = fs.readFileSync(path.join(SRC_DIR, file), "utf8");
    assert.doesNotMatch(srcText, /\blocalStorage\b/);
    assert.doesNotMatch(srcText, /\bsessionStorage\b/);
  }

  const calls = [];
  let sseControllerRef = null;
  const encoder = new TextEncoder();

  const initialSnapshot = {
    schema_version: "1.3.0",
    csrf_token: "csrf-in-memory-secret",
    voice_enabled: true,
    call_session: {
      call_id: "call-1",
      state: "standby",
      turn_count: 0,
      active_correlation_id: null,
    },
    active_workspace: {
      display_name: "alpha",
      canonical_path: "/home/dev/Proyectos/alpha",
      selected_via: "exact_path",
    },
    available_workspaces: [
      { display_name: "alpha", canonical_path: "/home/dev/Proyectos/alpha" },
      { display_name: "beta", canonical_path: "/home/dev/Proyectos/beta" },
    ],
    pending_approvals: [
      {
        audit_id: "aud-race",
        correlation_id: "corr-r",
        source: "tier1_pi",
        sanitized_command: "npm test",
        cwd: "/home/dev/Proyectos/alpha",
        risk_level: "medium",
        spoken_summary: "Confirma npm test.",
        technical_explanation: "Ejecuta scripts",
        expires_in_seconds: 30,
        timestamp: "2026-11-12T15:31:00Z",
      },
    ],
    messages: [],
  };

  let streamSignal = null;
  const fakeFetch = async (url, init = {}) => {
    calls.push({ url, method: init.method ?? "GET", headers: init.headers ?? {}, body: init.body });

    if (url === "/api/state") {
      return new Response(JSON.stringify(initialSnapshot), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url === "/api/events") {
      streamSignal = init.signal;
      const stream = new ReadableStream({
        start(controller) {
          sseControllerRef = controller;
          controller.enqueue(
            encoder.encode(`event: snapshot\ndata: ${JSON.stringify(initialSnapshot)}\n\n`),
          );
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    if (url === "/api/workspace/pick-native") {
      return new Response(
        JSON.stringify({
          error_code: "UI_NATIVE_PICKER_UNAVAILABLE",
          message: "No DISPLAY",
          correlation_id: "corr-503",
        }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url === "/api/approvals/aud-race/resolve") {
      return new Response(
        JSON.stringify({
          error_code: "GATEKEEPER_AUDIT_NOT_FOUND",
          message: "Already resolved by voice",
          correlation_id: "corr-404",
        }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url === "/api/chat") {
      return new Response(JSON.stringify({ accepted: true, correlation_id: "corr-chat" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ ok: true, call_state: "listening" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const renderedSnapshots = [];
  const app = new DesktopAppController({
    fetchImpl: fakeFetch,
    onRender: (html, model) => {
      renderedSnapshots.push({ html, model });
    },
  });

  await app.start();
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(app.model.status, "empty");
  assert.equal(app.model.pendingApprovals.length, 1);

  // Push live SSE call_state_changed + chat_stream chunks
  sseControllerRef.enqueue(
    encoder.encode(
      `event: nova_event\ndata: ${JSON.stringify({
        seq: 1,
        event_type: "call_state_changed",
        correlation_id: "corr-c",
        timestamp: "2026-11-12T15:31:01Z",
        payload: {
          call_id: "call-1",
          previous_state: "standby",
          state: "speaking",
          reason: "tts_milestone_playback",
          turn_count: 1,
        },
      })}\n\n`,
    ),
  );
  sseControllerRef.enqueue(
    encoder.encode(
      `event: nova_event\ndata: ${JSON.stringify({
        seq: 2,
        event_type: "chat_stream",
        correlation_id: "corr-c",
        timestamp: "2026-11-12T15:31:02Z",
        payload: {
          message_id: "msg-stream-1",
          delta: "Hola <script>x</script>",
          source: "tier1_pi",
          step_type: "agent_response",
        },
      })}\n\n`,
    ),
  );
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(app.model.callSession.state, "speaking");
  assert.equal(app.model.status, "success");
  assert.equal(app.model.messages[0].content, "Hola <script>x</script>");

  // Trigger headless native picker -> 503 opens integrated fallback selector
  await app.pickNativeWorkspace();
  assert.equal(app.model.pickerFallbackOpen, true);

  // Trigger 404 approval race -> removes stale approval card cleanly and sends X-Nova-CSRF
  await app.resolveApproval("aud-race", true);
  assert.equal(app.model.pendingApprovals.length, 0);
  const resolveReq = calls.find((c) => c.url === "/api/approvals/aud-race/resolve");
  assert.equal(resolveReq.headers["X-Nova-CSRF"], "csrf-in-memory-secret");

  // Dispose aborts active SSE stream via AbortController
  assert.equal(streamSignal.aborted, false);
  app.dispose();
  assert.equal(streamSignal.aborted, true);
});

test("Visual Identity & GNOME Specification: design tokens, squircle SVGs, sinusoidal waves and 0% GPU standby", () => {
  const css = fs.readFileSync(path.join(STATIC_DIR, "styles.css"), "utf8");
  assert.match(css, /--brand-electric-blue:\s*#00d2ff/i);
  assert.match(css, /--brand-nova-violet:\s*#a855f7/i);
  assert.match(css, /--glass-orb-bg-inactive:\s*rgba\(\s*34,\s*40,\s*52,\s*0\.68\s*\)/);
  assert.match(css, /--conversation-max-width:\s*720px/);
  assert.match(css, /--header-height:\s*56px/);
  assert.match(css, /--wake-transition:\s*280ms cubic-bezier/);

  // Standby squircle is muted
  const mutedSquircle = renderNovaSquircleSvg(56, false);
  assert.match(mutedSquircle, /nova-squircle-svg--muted/);
  assert.match(mutedSquircle, /#526071/);

  // Active squircle has cosmic gradient and star
  const activeSquircle = renderNovaSquircleSvg(64, true);
  assert.match(activeSquircle, /nova-squircle-svg--active/);
  assert.match(activeSquircle, /#00d2ff/);
  assert.match(activeSquircle, /#fffbeb/);

  // Sinusoidal wave: dynamic when active, flat line when standby
  const activeWave = renderSinusoidalWave("listening");
  assert.match(activeWave, /nova-wave-svg--dynamic/);
  assert.match(activeWave, /nova-sinusoidal-path/);

  const standbyWave = renderSinusoidalWave("standby");
  assert.match(standbyWave, /nova-wave-svg--flat/);
  assert.match(standbyWave, /stroke="rgba\(100, 116, 139, 0\.40\)"/);

  // Tray symbolic icon exists
  const traySvgPath = path.join(STATIC_DIR, "nova-gnome-tray-symbolic.svg");
  assert.ok(fs.existsSync(traySvgPath));
});

test("AC-3 & AC-4: Dock standby and call modes, textarea lifecycle, and mic button removal", async () => {
  const baseModel = {
    status: "success",
    voiceEnabled: true,
    uiMode: "panel",
    callSession: {
      call_id: "call-1",
      state: "standby",
      turn_count: 0,
      active_correlation_id: null,
    },
    activeWorkspace: {
      display_name: "alpha",
      canonical_path: "/home/dev/Proyectos/alpha",
      selected_via: "exact_path",
    },
    availableWorkspaces: [
      { display_name: "alpha", canonical_path: "/home/dev/Proyectos/alpha" },
    ],
    pendingApprovals: [],
    messages: [],
    errorBanner: null,
    pickerFallbackOpen: false,
    lastMilestone: null,
  };

  // AC-3: Standby and closed modes
  for (const st of ["standby", "closed"]) {
    const html = renderAppShell({ ...baseModel, callSession: { ...baseModel.callSession, state: st } });
    assert.match(html, /data-slot="dock"[^>]*data-dock-mode="standby"/);

    // Orb in dock is the only call-start occurrence
    const startMatches = html.match(/data-action="call-start"/g) || [];
    assert.equal(startMatches.length, 1, `Expected exactly 1 call-start in state ${st}`);
    assert.match(html, /<button[^>]+class="nova-dock-orb"[^>]+data-action="call-start"[^>]+aria-label="Iniciar llamada"/);

    // Contains textarea with maxlength="16000" and submit button
    assert.match(html, /<textarea[^>]+id="nova-chat-input"[^>]+maxlength="16000"/);
    assert.match(html, /<button[^>]+type="submit"/);

    // Does NOT contain wave box, barge-in, call-end, nor btn-mic
    assert.ok(!html.includes("nova-wave-box"));
    assert.ok(!html.includes('data-action="call-barge-in"'));
    assert.ok(!html.includes('data-action="call-end"'));
    assert.ok(!html.includes("btn-mic"));
  }

  // Without voice available: orb is disabled
  const noVoiceHtml = renderAppShell({ ...baseModel, voiceEnabled: false });
  assert.match(noVoiceHtml, /data-action="call-start"[^>]*disabled/);
  assert.match(noVoiceHtml, /data-action="call-start"[^>]*aria-disabled="true"/);

  // AC-4: In call states {listening, thinking, executing, speaking}
  const callStates = ["listening", "thinking", "executing", "speaking"];
  for (const st of callStates) {
    const html = renderAppShell({ ...baseModel, callSession: { ...baseModel.callSession, state: st } });
    assert.match(html, /data-slot="dock"[^>]*data-dock-mode="call"/);
    assert.ok(html.includes("nova-wave-svg--dynamic"));

    // Barge-in and end buttons enabled without disabled attribute
    assert.match(html, /<button[^>]+data-action="call-barge-in"[^>]*>\s*Interrumpir \(Esc\)\s*<\/button>/);
    const bargeInMatch = html.match(/<button[^>]+data-action="call-barge-in"[^>]*>/);
    assert.ok(bargeInMatch && !bargeInMatch[0].includes("disabled"), `Barge-in should not be disabled in ${st}`);

    assert.match(html, /<button[^>]+data-action="call-end"[^>]*>\s*Colgar\s*<\/button>/);
    const endMatch = html.match(/<button[^>]+data-action="call-end"[^>]*>/);
    assert.ok(endMatch && !endMatch[0].includes("disabled"), `Call-end should not be disabled in ${st}`);

    // Orb has disabled and aria-disabled="true"
    assert.match(html, /data-action="call-start"[^>]*disabled/);
    assert.match(html, /data-action="call-start"[^>]*aria-disabled="true"/);

    // Shell does not contain textarea nor submit button
    assert.ok(!html.includes('id="nova-chat-input"'));
    assert.ok(!html.includes('type="submit"'));
  }
});

test("AC-5: Strict 12-combination barge-in enablement in renderCallControls and controller guard", async () => {
  const allStates = ["standby", "listening", "thinking", "executing", "speaking", "closed"];
  for (const st of allStates) {
    for (const voiceEnabled of [true, false]) {
      const inCall = st !== "standby" && st !== "closed";
      const expectedEnabled = voiceEnabled && inCall;
      const html = renderCallControls(st, voiceEnabled);

      const btnMatch = html.match(/<button[^>]+data-action="call-barge-in"[^>]*>/);
      assert.ok(btnMatch, `Button must exist for ${st}, voice=${voiceEnabled}`);

      if (expectedEnabled) {
        assert.ok(!btnMatch[0].includes("disabled"), `barge-in should be enabled for ${st}, voice=${voiceEnabled}`);
      } else {
        assert.ok(btnMatch[0].includes("disabled"), `barge-in must have disabled for ${st}, voice=${voiceEnabled}`);
        assert.ok(btnMatch[0].includes('aria-disabled="true"'), `barge-in must have aria-disabled for ${st}, voice=${voiceEnabled}`);
      }
    }
  }

  // Controller defense in depth
  const calls = [];
  const fakeFetch = async (url, init) => {
    calls.push({ url, method: init?.method });
    if (url === "/api/state") {
      return new Response(
        JSON.stringify({
          schema_version: "1.3.0",
          csrf_token: "csrf-token",
          voice_enabled: true,
          call_session: { call_id: "c1", state: "standby", turn_count: 0, active_correlation_id: null },
          active_workspace: { display_name: "w", canonical_path: "/w", selected_via: "exact_path" },
          available_workspaces: [],
          pending_approvals: [],
          messages: [],
        }),
        { status: 200 },
      );
    }
    if (url === "/api/events") {
      return new Response(new ReadableStream(), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  const app = new DesktopAppController({ fetchImpl: fakeFetch });
  await app.start();
  await new Promise((r) => setTimeout(r, 20));

  // Standby -> trigger barge-in must result in 0 calls to /api/call/barge-in
  await app.triggerCallAction("barge-in");
  const bargeInCalls = calls.filter((c) => c.url === "/api/call/barge-in");
  assert.equal(bargeInCalls.length, 0, "Standby barge-in must be blocked by controller guard");
  app.dispose();
});

test("AC-6: Scoped Escape key handling via public controller method handleEscapeKey", async () => {
  const calls = [];
  let sseControllerRef = null;
  const encoder = new TextEncoder();

  const fakeFetch = async (url, init) => {
    calls.push({ url, method: init?.method, body: init?.body });
    if (url === "/api/state") {
      return new Response(
        JSON.stringify({
          schema_version: "1.3.0",
          csrf_token: "csrf-token",
          voice_enabled: true,
          call_session: { call_id: "c1", state: "standby", turn_count: 0, active_correlation_id: null },
          active_workspace: { display_name: "w", canonical_path: "/w", selected_via: "exact_path" },
          available_workspaces: [],
          pending_approvals: [],
          messages: [],
        }),
        { status: 200 },
      );
    }
    if (url === "/api/events") {
      const stream = new ReadableStream({
        start(controller) {
          sseControllerRef = controller;
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  const app = new DesktopAppController({ fetchImpl: fakeFetch });
  await app.start();
  await new Promise((r) => setTimeout(r, 20));

  // Helper to emit call_state_changed
  const setCallState = async (state) => {
    const frame = `event: nova_event\ndata: ${JSON.stringify({
      seq: Date.now(),
      event_type: "call_state_changed",
      correlation_id: "corr-esc",
      timestamp: new Date().toISOString(),
      payload: { call_id: "c1", state, turn_count: 1 },
    })}\n\n`;
    sseControllerRef.enqueue(encoder.encode(frame));
    await new Promise((r) => setTimeout(r, 20));
  };

  // 1. speaking + panel -> exactly 1 POST to /api/call/barge-in
  await setCallState("speaking");
  assert.equal(app.model.callSession.state, "speaking");
  assert.equal(app.model.uiMode, "panel");
  const countBefore1 = calls.filter((c) => c.url === "/api/call/barge-in").length;
  await app.handleEscapeKey();
  const countAfter1 = calls.filter((c) => c.url === "/api/call/barge-in").length;
  assert.equal(countAfter1 - countBefore1, 1, "speaking + panel emits 1 barge-in request");

  // 2. listening + orb -> exactly 1 POST to /api/call/barge-in, uiMode remains orb
  await setCallState("listening");
  await app.toggleUiMode(); // switches to orb
  assert.equal(app.model.uiMode, "orb");
  const countBefore2 = calls.filter((c) => c.url === "/api/call/barge-in").length;
  await app.handleEscapeKey();
  const countAfter2 = calls.filter((c) => c.url === "/api/call/barge-in").length;
  assert.equal(countAfter2 - countBefore2, 1, "listening + orb emits 1 barge-in request");
  assert.equal(app.model.uiMode, "orb", "uiMode remains orb after barge-in during call");

  // 3. standby + orb -> 0 new requests, uiMode transitions to panel
  await setCallState("standby");
  assert.equal(app.model.uiMode, "orb");
  const countBefore3 = calls.length;
  await app.handleEscapeKey();
  const countAfter3 = calls.length;
  assert.equal(countAfter3, countBefore3, "standby + orb generates 0 network requests");
  assert.equal(app.model.uiMode, "panel", "standby + orb transitions uiMode to panel");

  // 4. standby + panel -> 0 new requests, model unchanged (structural equality)
  assert.equal(app.model.uiMode, "panel");
  assert.equal(app.model.callSession.state, "standby");
  const modelBefore4 = JSON.stringify(app.model);
  const countBefore4 = calls.length;
  await app.handleEscapeKey();
  const countAfter4 = calls.length;
  const modelAfter4 = JSON.stringify(app.model);
  assert.equal(countAfter4, countBefore4, "standby + panel generates 0 network requests");
  assert.equal(modelAfter4, modelBefore4, "standby + panel leaves model structurally identical");

  app.dispose();
});

test("AC-7: Empty workspace state with escaped catalog and pick-native", async () => {
  const modelWithMaliciousCatalog = {
    status: "empty",
    voiceEnabled: true,
    uiMode: "panel",
    callSession: {
      call_id: "c-empty",
      state: "standby",
      turn_count: 0,
      active_correlation_id: null,
    },
    activeWorkspace: null,
    availableWorkspaces: [
      { display_name: "alpha", canonical_path: "/home/dev/Proyectos/alpha" },
      { display_name: "<img src=x onerror=evil()>", canonical_path: "/home/dev/Proyectos/<evil>" },
    ],
    pendingApprovals: [],
    messages: [],
    errorBanner: null,
    noticeBanner: null,
    pickerFallbackOpen: false,
    lastMilestone: null,
  };

  const html = renderAppShell(modelWithMaliciousCatalog);

  // Exists data-slot="workspace-empty-state" with heading "Elegí un proyecto"
  assert.match(html, /data-slot="workspace-empty-state"/);
  assert.match(html, /<h2[^>]*>\s*Elegí un proyecto\s*<\/h2>/);

  // Contains button for each option with data-action="select-workspace-option" and escaped data-canonical-path
  assert.match(html, /data-action="select-workspace-option"[^>]+data-canonical-path="\/home\/dev\/Proyectos\/alpha"/);
  assert.match(html, /data-action="select-workspace-option"[^>]+data-canonical-path="\/home\/dev\/Proyectos\/&lt;evil&gt;"/);

  // Malicious display_name escaped
  assert.ok(html.includes("&lt;img src=x onerror=evil()&gt;"));
  assert.ok(!html.includes("<img src=x"));

  // Contains pick-native button
  assert.match(html, /<button[^>]+data-action="pick-native"[^>]*>\s*Abrir carpeta…\s*<\/button>/);

  // Does NOT contain greeting
  assert.ok(!html.includes("nova-empty-greeting"));

  // Empty catalog scenario
  const htmlEmptyCat = renderAppShell({ ...modelWithMaliciousCatalog, availableWorkspaces: [] });
  assert.ok(!htmlEmptyCat.includes('data-action="select-workspace-option"'));
  assert.match(htmlEmptyCat, /<button[^>]+data-action="pick-native"[^>]*>\s*Abrir carpeta…\s*<\/button>/);

  // Loading scenario: no empty state during loading
  const htmlLoading = renderAppShell({ ...modelWithMaliciousCatalog, status: "loading" });
  assert.match(htmlLoading, /data-view-status="loading"/);
  assert.ok(!htmlLoading.includes('data-slot="workspace-empty-state"'));

  // Controller interaction: select option via catalog uses CSRF
  const calls = [];
  const fakeFetch = async (url, init) => {
    calls.push({ url, method: init?.method, headers: init?.headers, body: init?.body });
    if (url === "/api/state") {
      return new Response(
        JSON.stringify({
          schema_version: "1.3.0",
          csrf_token: "csrf-empty-test",
          voice_enabled: true,
          call_session: { call_id: "c1", state: "standby", turn_count: 0, active_correlation_id: null },
          active_workspace: null,
          available_workspaces: [
            { display_name: "beta", canonical_path: "/home/dev/Proyectos/beta" },
          ],
          pending_approvals: [],
          messages: [],
        }),
        { status: 200 },
      );
    }
    if (url === "/api/events") {
      return new Response(new ReadableStream(), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    if (url === "/api/workspace/select") {
      return new Response(
        JSON.stringify({
          cancelled: false,
          workspace: {
            display_name: "beta",
            canonical_path: "/home/dev/Proyectos/beta",
            selected_via: "exact_path",
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  const renders = [];
  const app = new DesktopAppController({
    fetchImpl: fakeFetch,
    onRender: (renderedHtml, m) => {
      renders.push({ renderedHtml, m });
    },
  });
  await app.start();
  await new Promise((r) => setTimeout(r, 20));

  // Initial render has empty state
  assert.ok(renders[renders.length - 1].renderedHtml.includes('data-slot="workspace-empty-state"'));

  // User activates "beta"
  await app.selectWorkspace("/home/dev/Proyectos/beta");

  const selectReq = calls.find((c) => c.url === "/api/workspace/select");
  assert.ok(selectReq, "POST to /api/workspace/select registered");
  assert.equal(selectReq.headers["X-Nova-CSRF"], "csrf-empty-test");
  assert.deepEqual(JSON.parse(selectReq.body), { path_or_query: "/home/dev/Proyectos/beta" });

  // Empty state disappears from next render
  const latestHtml = renders[renders.length - 1].renderedHtml;
  assert.ok(!latestHtml.includes('data-slot="workspace-empty-state"'));

  app.dispose();
});

test("AC-8: Composer disabled guard with accessible description and backend race", async () => {
  const modelNoWorkspace = {
    status: "empty",
    voiceEnabled: true,
    uiMode: "panel",
    callSession: {
      call_id: "c-empty",
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
  };

  const html = renderAppShell(modelNoWorkspace);

  // Textarea and submit have disabled
  assert.match(html, /<textarea[^>]+id="nova-chat-input"[^>]*\sdisabled\b/);
  assert.match(html, /<button[^>]+type="submit"[^>]*\sdisabled\b/);

  // Textarea has aria-describedby matching element whose text contains "proyecto"
  const describedByMatch = html.match(/id="nova-chat-input"[^>]+aria-describedby="([^"]+)"/);
  assert.ok(describedByMatch, "textarea must have aria-describedby");
  const descId = describedByMatch[1];
  const descElMatch = html.match(new RegExp(`<[^>]+id="${descId}"[^>]*>([^<]+)<`));
  assert.ok(descElMatch, `Element with id ${descId} must exist`);
  assert.match(descElMatch[1], /proyecto/i, "Description element must mention 'proyecto'");

  // Controller guard prevents chat sending when activeWorkspace is null
  const calls = [];
  let sseControllerRef = null;
  const encoder = new TextEncoder();

  const fakeFetch = async (url, init) => {
    calls.push({ url, method: init?.method, body: init?.body });
    if (url === "/api/state") {
      return new Response(
        JSON.stringify({
          schema_version: "1.3.0",
          csrf_token: "csrf-ac8",
          voice_enabled: true,
          call_session: { call_id: "c1", state: "standby", turn_count: 0, active_correlation_id: null },
          active_workspace: null,
          available_workspaces: [],
          pending_approvals: [],
          messages: [],
        }),
        { status: 200 },
      );
    }
    if (url === "/api/events") {
      const stream = new ReadableStream({
        start(controller) {
          sseControllerRef = controller;
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    if (url === "/api/chat") {
      return new Response(
        JSON.stringify({
          error_code: "WORKSPACE_NOT_FOUND",
          message: "Workspace path does not exist",
          correlation_id: "corr-race",
        }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  const renders = [];
  const app = new DesktopAppController({
    fetchImpl: fakeFetch,
    onRender: (renderedHtml, m) => {
      renders.push({ renderedHtml, m });
    },
  });
  await app.start();
  await new Promise((r) => setTimeout(r, 20));

  // Guard prevents sending without workspace
  await app.submitChat("hola");
  const chatCalls = calls.filter((c) => c.url === "/api/chat");
  assert.equal(chatCalls.length, 0, "0 calls to /api/chat when activeWorkspace is null");

  // SSE emits workspace_changed -> composer enabled
  const frame = `event: nova_event\ndata: ${JSON.stringify({
    seq: 1,
    event_type: "workspace_changed",
    correlation_id: "corr-ws",
    timestamp: new Date().toISOString(),
    payload: {
      display_name: "valid-ws",
      canonical_path: "/home/dev/valid-ws",
      selected_via: "exact_path",
    },
  })}\n\n`;
  sseControllerRef.enqueue(encoder.encode(frame));
  await new Promise((r) => setTimeout(r, 20));

  const afterWsHtml = renders[renders.length - 1].renderedHtml;
  assert.match(afterWsHtml, /<textarea[^>]+id="nova-chat-input"/);
  const inputTagMatch = afterWsHtml.match(/<textarea[^>]+id="nova-chat-input"[^>]*>/);
  assert.ok(inputTagMatch && !inputTagMatch[0].includes("disabled"), "Textarea must not be disabled once workspace arrives");

  // Backend race: 404 WORKSPACE_NOT_FOUND on chat submit displays notice without adding to messages
  const msgCountBefore = app.model.messages.length;
  await app.submitChat("hola");
  assert.equal(app.model.messages.length, msgCountBefore, "Messages array must not grow on 404 failure");
  assert.ok(app.model.noticeBanner !== null || app.model.errorBanner !== null, "Notice banner must be set on 404");

  app.dispose();
});

test("AC-9: runner_failure and HTTP errors as human notice banner, error catalog, dismiss without network, XSS safety", async () => {
  // 1. Error catalog check with renderNoticeSlot
  const knownCodes = [
    "WORKSPACE_NOT_FOUND",
    "WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
    "WORKSPACE_AMBIGUOUS",
    "WORKSPACE_RESOLUTION_TIMEOUT",
    "UI_VOICE_UNAVAILABLE",
    "UI_EMPTY_UTTERANCE",
    "UI_PAYLOAD_TOO_LARGE",
    "UI_FORBIDDEN_ORIGIN_OR_CSRF",
    "ORCHESTRATOR_UNEXPECTED_FAILURE",
  ];
  for (const code of [...knownCodes, "X_UNKNOWN_CODE"]) {
    const banner = resolveHumanNotice(code);
    assert.ok(banner.message.length > 0, `Message for ${code} must not be empty`);
    assert.ok(!banner.message.includes(code), `Message for ${code} must not contain raw code`);
    assert.ok(!/\b[A-Z]{2,}(?:_[A-Z]+)+\b/.test(banner.message), `Message for ${code} must not match raw enum`);
    if (code === "WORKSPACE_NOT_FOUND") {
      assert.match(banner.message, /proyecto/i, "WORKSPACE_NOT_FOUND must mention proyecto");
      assert.ok(!banner.message.includes("Workspace not found"), "Must not contain English backend message");
    }
  }

  // 2. Controller SSE runner_failure handling
  let sseControllerRef = null;
  const calls = [];
  const fakeFetch = async (url, init) => {
    calls.push({ url, method: init?.method });
    if (url === "/api/state") {
      return new Response(
        JSON.stringify({
          schema_version: "1.3.0",
          csrf_token: "csrf-ac9",
          voice_enabled: true,
          call_session: { call_id: "c1", state: "standby", turn_count: 0, active_correlation_id: null },
          active_workspace: { display_name: "p1", canonical_path: "/p1", selected_via: "exact_path" },
          available_workspaces: [],
          pending_approvals: [],
          messages: [
            {
              message_id: "m-existing",
              correlation_id: "c-1",
              role: "user",
              modality: "text",
              step_type: "user_prompt",
              content: "Hola",
              timestamp: new Date().toISOString(),
              streaming: false,
            },
            {
              message_id: "m-rehydrated-err",
              correlation_id: "c-err",
              role: "assistant",
              modality: "error",
              step_type: "error",
              content: "Should be filtered",
              timestamp: new Date().toISOString(),
              streaming: false,
            },
          ],
        }),
        { status: 200 },
      );
    }
    if (url === "/api/events") {
      const stream = new ReadableStream({
        start(controller) {
          sseControllerRef = controller;
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  const renders = [];
  const app = new DesktopAppController({
    fetchImpl: fakeFetch,
    onRender: (renderedHtml, m) => {
      renders.push({ renderedHtml, m });
    },
  });
  await app.start();
  await new Promise((r) => setTimeout(r, 20));

  // Initial render: rehydrated modality: "error" message is filtered from timeline and does not create notice
  const initialHtml = renders[renders.length - 1].renderedHtml;
  assert.ok(!initialHtml.includes("Should be filtered"), "Rehydrated error message must be filtered from timeline");
  assert.ok(!initialHtml.includes('role="alert"'), "Rehydrated error must not create notice");

  const initialMsgCount = app.model.messages.length;

  // SSE emits runner_failure
  const encoder = new TextEncoder();
  const failureEvent = `event: nova_event\ndata: ${JSON.stringify({
    seq: 1,
    event_type: "runner_failure",
    correlation_id: "corr-fail-1",
    timestamp: new Date().toISOString(),
    payload: {
      error_code: "WORKSPACE_NOT_FOUND",
      sanitized_message: "Workspace not found",
      message: {
        message_id: "msg-err",
        correlation_id: "corr-fail-1",
        role: "assistant",
        modality: "error",
        step_type: "error",
        content: "Error in runner",
        timestamp: new Date().toISOString(),
        streaming: false,
      },
    },
  })}\n\n`;

  sseControllerRef.enqueue(encoder.encode(failureEvent));
  await new Promise((r) => setTimeout(r, 20));

  // Messages array does not grow
  assert.equal(app.model.messages.length, initialMsgCount, "Messages array must not grow on runner_failure");

  // Render contains data-slot="notice" with role="alert" and data-error-code="WORKSPACE_NOT_FOUND"
  const failureHtml = renders[renders.length - 1].renderedHtml;
  assert.match(failureHtml, /data-slot="notice"[^>]*\srole="alert"/);
  assert.match(failureHtml, /data-error-code="WORKSPACE_NOT_FOUND"/);
  assert.match(failureHtml, /data-action="dismiss-notice"[^>]*aria-label="Cerrar aviso"/);
  assert.match(failureHtml, /proyecto/i);
  assert.ok(!failureHtml.includes("Workspace not found"));
  assert.ok(!failureHtml.includes("Error in runner"));

  // Consecutive error replaces the first
  const secondFailure = `event: nova_event\ndata: ${JSON.stringify({
    seq: 2,
    event_type: "runner_failure",
    correlation_id: "corr-fail-2",
    timestamp: new Date().toISOString(),
    payload: {
      error_code: "UI_VOICE_UNAVAILABLE",
      sanitized_message: "Voice unavailable",
    },
  })}\n\n`;
  sseControllerRef.enqueue(encoder.encode(secondFailure));
  await new Promise((r) => setTimeout(r, 20));

  const secondFailureHtml = renders[renders.length - 1].renderedHtml;
  assert.match(secondFailureHtml, /data-error-code="UI_VOICE_UNAVAILABLE"/);
  assert.ok(!secondFailureHtml.includes("WORKSPACE_NOT_FOUND"));

  // Dismiss notice without network calls
  const fetchCountBeforeDismiss = calls.length;
  app.dismissNotice();
  await new Promise((r) => setTimeout(r, 10));

  assert.equal(calls.length, fetchCountBeforeDismiss, "0 network calls when dismissing notice");
  const dismissedHtml = renders[renders.length - 1].renderedHtml;
  assert.ok(!dismissedHtml.includes('role="alert"'));
  assert.ok(!dismissedHtml.includes("UI_VOICE_UNAVAILABLE"));

  // Hostile error code escaping
  const maliciousNoticeHtml = renderNoticeSlot({
    ...app.model,
    noticeBanner: {
      code: '<script>evil()</script>',
      message: 'Mensaje seguro',
      dismissible: true,
    },
  });
  assert.ok(!maliciousNoticeHtml.includes("<script>evil()"));
  assert.ok(maliciousNoticeHtml.includes("&lt;script&gt;evil()&lt;/script&gt;"));

  app.dispose();
});

test("AC-10: Timeline without modality meta and discreet voice icon", () => {
  const messages = [
    {
      message_id: "m-txt",
      correlation_id: "c-1",
      role: "user",
      modality: "text",
      step_type: "user_prompt",
      content: "Mensaje escrito",
      timestamp: "2026-11-12T10:00:00Z",
      streaming: false,
    },
    {
      message_id: "m-voc",
      correlation_id: "c-2",
      role: "user",
      modality: "voice",
      step_type: "transcription",
      content: "Mensaje hablado",
      timestamp: "2026-11-12T10:01:00Z",
      streaming: false,
    },
  ];

  const html = renderChatTimeline("success", messages, null);

  // Must not contain technical meta
  assert.ok(!html.includes("chat-msg-meta"));
  assert.ok(!html.includes("text · user_prompt"));
  assert.ok(!html.includes("voice · transcription"));

  // Voice message has data-modality-icon="voice" with accessible name "Mensaje de voz"
  assert.match(html, /data-modality-icon="voice"[^>]*aria-label="Mensaje de voz"/);

  // Text message does NOT have data-modality-icon
  const textMsgMatch = html.match(/<article[^>]+data-modality="text"[\s\S]*?<\/article>/);
  assert.ok(textMsgMatch, "Text message article found");
  assert.ok(!textMsgMatch[0].includes("data-modality-icon"), "Text message must not contain data-modality-icon");

  // Semantic landmarks preserved
  assert.match(html, /role="log"/);
  assert.match(html, /aria-live="polite"/);
});

test("AC-11: Voice milestone as ephemeral toast above dock", async () => {
  let sseControllerRef = null;
  const fakeFetch = async (url) => {
    if (url === "/api/state") {
      return new Response(
        JSON.stringify({
          schema_version: "1.3.0",
          csrf_token: "csrf-ac11",
          voice_enabled: true,
          call_session: { call_id: "c1", state: "speaking", turn_count: 1, active_correlation_id: null },
          active_workspace: { display_name: "p1", canonical_path: "/p1", selected_via: "exact_path" },
          available_workspaces: [],
          pending_approvals: [],
          messages: [],
        }),
        { status: 200 },
      );
    }
    if (url === "/api/events") {
      const stream = new ReadableStream({
        start(controller) {
          sseControllerRef = controller;
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  const renders = [];
  const app = new DesktopAppController({
    fetchImpl: fakeFetch,
    onRender: (renderedHtml, m) => {
      renders.push({ renderedHtml, m });
    },
  });
  await app.start();
  await new Promise((r) => setTimeout(r, 20));

  const encoder = new TextEncoder();
  // SSE emits voice_milestone
  const milestoneEvent = `event: nova_event\ndata: ${JSON.stringify({
    seq: 1,
    event_type: "voice_milestone",
    correlation_id: "corr-mile-1",
    timestamp: new Date().toISOString(),
    payload: {
      summary_text: "Listo <b>x</b>",
    },
  })}\n\n`;
  sseControllerRef.enqueue(encoder.encode(milestoneEvent));
  await new Promise((r) => setTimeout(r, 20));

  const milestoneHtml = renders[renders.length - 1].renderedHtml;
  assert.match(milestoneHtml, /data-slot="milestone"/);
  assert.ok(milestoneHtml.includes("Listo &lt;b&gt;x&lt;/b&gt;"));
  assert.ok(!milestoneHtml.includes("Listo <b>x</b>"));

  // milestone slot is located BEFORE dock slot
  const milestonePos = milestoneHtml.indexOf('data-slot="milestone"');
  const dockPos = milestoneHtml.indexOf('data-slot="dock"');
  assert.ok(milestonePos < dockPos, "Milestone slot must be before dock slot");

  // Call state changes to standby -> milestone toast cleared
  const standbyEvent = `event: nova_event\ndata: ${JSON.stringify({
    seq: 2,
    event_type: "call_state_changed",
    correlation_id: "corr-mile-2",
    timestamp: new Date().toISOString(),
    payload: {
      state: "standby",
    },
  })}\n\n`;
  sseControllerRef.enqueue(encoder.encode(standbyEvent));
  await new Promise((r) => setTimeout(r, 20));

  assert.equal(app.model.lastMilestone, null, "lastMilestone must be null in standby");
  const clearedHtml = renders[renders.length - 1].renderedHtml;
  assert.ok(!clearedHtml.includes("nova-milestone-toast"));

  app.dispose();
});
