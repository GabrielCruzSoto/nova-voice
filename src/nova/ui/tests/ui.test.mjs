import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  escapeHtml,
  renderAppShell,
  renderApprovalBanner,
  renderChatTimeline,
  renderNovaReactiveOrb,
  renderNovaSquircleSvg,
  renderSinusoidalWave,
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
