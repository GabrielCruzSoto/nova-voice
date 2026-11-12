/**
 * Nova SecurityGatekeeper Extension for Pi Agent (Tier 1).
 *
 * Intercepts pre-execution `bash` tool calls and authorizes them over Nova's
 * authenticated AF_UNIX JSONL gatekeeper socket (`NOVA_GATEKEEPER_SOCKET`).
 * File tools (`read`, `write`, `edit`) inside the workspace remain unblocked (BR-3).
 */

import * as crypto from "node:crypto";
import * as net from "node:net";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface GatekeeperIPCResponse {
  allowed?: boolean;
  verdict?: string;
  audit_id?: string | null;
  reason?: string;
}

const IPC_TIMEOUT_MS = 280_000;

function queryGatekeeperSocket(
  socketPath: string,
  authSecret: string,
  command: string,
  cwd: string,
): Promise<GatekeeperIPCResponse> {
  return new Promise((resolve) => {
    const client = net.createConnection({ path: socketPath });
    let buffer = "";
    let settled = false;

    const finish = (resp: GatekeeperIPCResponse): void => {
      if (settled) return;
      settled = true;
      client.destroy();
      resolve(resp);
    };

    client.setTimeout(IPC_TIMEOUT_MS, () => {
      finish({
        allowed: false,
        reason: "GATEKEEPER_APPROVAL_TIMEOUT: Nova IPC request timed out (fail-closed).",
      });
    });

    client.on("connect", () => {
      const payload = JSON.stringify({
        protocol_version: "1.0",
        token: authSecret,
        correlation_id: `corr-${crypto.randomUUID()}`,
        source: "tier1_pi",
        tool_name: "bash",
        command,
        cwd,
      });
      client.write(`${payload}\n`, "utf8");
    });

    client.on("data", (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      const newlineIdx = buffer.indexOf("\n");
      if (newlineIdx !== -1) {
        const line = buffer.slice(0, newlineIdx).trim();
        try {
          finish(JSON.parse(line) as GatekeeperIPCResponse);
        } catch {
          finish({
            allowed: false,
            reason: "GATEKEEPER_INVALID_RESPONSE: Malformed JSON from Nova gatekeeper.",
          });
        }
      }
    });

    client.on("error", (err: Error) => {
      finish({
        allowed: false,
        reason: `GATEKEEPER_IPC_ERROR: ${err.message} (fail-closed).`,
      });
    });

    client.on("end", () => {
      finish({
        allowed: false,
        reason: "GATEKEEPER_IPC_CLOSED: Connection closed without decision (fail-closed).",
      });
    });
  });
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName !== "bash") {
      return undefined;
    }

    const socketPath = process.env.NOVA_GATEKEEPER_SOCKET;
    if (!socketPath) {
      return undefined;
    }

    const authSecret = process.env.NOVA_GATEKEEPER_TOKEN ?? "";
    const command = typeof event.input?.command === "string" ? event.input.command : "";
    const decision = await queryGatekeeperSocket(socketPath, authSecret, command, ctx.cwd);

    if (decision.allowed === true) {
      return undefined;
    }

    return {
      block: true,
      reason: decision.reason ?? "Blocked by Nova SecurityGatekeeper.",
    };
  });
}
