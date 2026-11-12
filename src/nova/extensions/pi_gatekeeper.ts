/**
 * Nova SecurityGatekeeper Extension for Pi Agent (Tier 1).
 *
 * 1. Confines non-bash file tools (`read`, `write`, `edit`, `ls`, `find`, `grep`, etc.)
 *    strictly within `realpath(ctx.cwd)` (BR-3, F3).
 * 2. Intercepts pre-execution `bash` tool calls and authorizes them over Nova's
 *    authenticated AF_UNIX JSONL gatekeeper socket (`NOVA_GATEKEEPER_SOCKET`).
 */

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface GatekeeperIPCResponse {
  allowed?: boolean;
  verdict?: string;
  audit_id?: string | null;
  reason?: string;
}

const IPC_TIMEOUT_MS = 280_000;
const MAX_IPC_FRAME_BYTES = 65_536;
const PATH_ARGUMENT_KEYS = [
  "path",
  "filePath",
  "file_path",
  "directory",
  "cwd",
  "target",
  "source",
  "destination",
] as const;

function expandHome(rawPath: string): string {
  if (rawPath === "~") {
    return os.homedir();
  }
  if (rawPath.startsWith("~/") || rawPath.startsWith("~\\")) {
    return path.join(os.homedir(), rawPath.slice(2));
  }
  return rawPath;
}

function resolveCandidateRealpath(rawPath: string, baseDir: string): string | null {
  const expanded = expandHome(rawPath.trim());
  if (!expanded) {
    return baseDir;
  }
  const absolute = path.resolve(baseDir, expanded);
  let current = absolute;
  const trailingSegments: string[] = [];

  while (true) {
    try {
      const lstat = fs.lstatSync(current);
      if (lstat.isSymbolicLink()) {
        try {
          const realTarget = fs.realpathSync(current);
          return trailingSegments.length > 0
            ? path.join(realTarget, ...trailingSegments.reverse())
            : realTarget;
        } catch {
          // Broken symbolic link: fail-closed rather than walking up to parent
          return null;
        }
      }
      const realCurrent = fs.realpathSync(current);
      return trailingSegments.length > 0
        ? path.join(realCurrent, ...trailingSegments.reverse())
        : realCurrent;
    } catch {
      const parent = path.dirname(current);
      if (parent === current) {
        return null;
      }
      trailingSegments.push(path.basename(current));
      current = parent;
    }
  }
}

function isInsideWorkspace(candidateReal: string, workspaceReal: string): boolean {
  if (candidateReal === workspaceReal) {
    return true;
  }
  const prefix = workspaceReal.endsWith(path.sep)
    ? workspaceReal
    : `${workspaceReal}${path.sep}`;
  return candidateReal.startsWith(prefix);
}

function checkFileToolConfinement(
  input: Record<string, unknown> | undefined,
  cwd: string,
): { block: true; reason: string } | undefined {
  if (!input) {
    return undefined;
  }

  let workspaceReal: string;
  try {
    workspaceReal = fs.realpathSync(cwd);
  } catch {
    return {
      block: true,
      reason: "GATEKEEPER_INVALID_WORKSPACE: Active workspace directory cannot be resolved.",
    };
  }

  for (const key of PATH_ARGUMENT_KEYS) {
    const val = input[key];
    if (typeof val !== "string" || !val.trim()) {
      continue;
    }
    const candidateReal = resolveCandidateRealpath(val, workspaceReal);
    if (!candidateReal || !isInsideWorkspace(candidateReal, workspaceReal)) {
      return {
        block: true,
        reason: `GATEKEEPER_OUTSIDE_WORKSPACE_FILE: File path '${val}' escapes active workspace.`,
      };
    }
  }

  return undefined;
}

function queryGatekeeperSocket(
  socketPath: string,
  authSecret: string,
  command: string,
  cwd: string,
): Promise<GatekeeperIPCResponse> {
  return new Promise((resolve) => {
    const client = net.createConnection({ path: socketPath });
    client.setEncoding("utf8");
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

    client.on("data", (chunk: string) => {
      buffer += chunk;
      if (Buffer.byteLength(buffer, "utf8") > MAX_IPC_FRAME_BYTES) {
        finish({
          allowed: false,
          reason: "GATEKEEPER_FRAME_TOO_LARGE: IPC response exceeded 64 KiB (fail-closed).",
        });
        return;
      }
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
    const socketPath = process.env.NOVA_GATEKEEPER_SOCKET;
    if (!socketPath) {
      return undefined;
    }

    if (event.toolName !== "bash") {
      return checkFileToolConfinement(
        event.input as Record<string, unknown> | undefined,
        ctx.cwd,
      );
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
