/**
 * Nova SecurityGatekeeper Extension for Pi Agent (Tier 1).
 *
 * 1. Confines approved workspace file tools (`read`, `write`, `edit`, `ls`, `find`, `grep`)
 *    strictly within `realpath(ctx.cwd)` after applying Pi's exact path normalizations
 *    (Unicode spaces, leading `@` prefixes, `~` expansion, `file://` URIs, and NFD/quote variants).
 * 2. Intercepts pre-execution shell tools (`bash`, `powershell`, etc.) and authorizes them over
 *    Nova's authenticated AF_UNIX JSONL gatekeeper socket (`NOVA_GATEKEEPER_SOCKET`).
 * 3. Blocks unknown or untrusted non-file tools fail-closed when gatekeeper mode is active.
 */

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface GatekeeperIPCResponse {
  allowed?: boolean;
  verdict?: string;
  audit_id?: string | null;
  reason?: string;
}

const IPC_TIMEOUT_MS = 280_000;
const MAX_IPC_FRAME_BYTES = 65_536;
const UNICODE_SPACES_RE = /[\u00A0\u2000-\u200A\u202F\u205F\u3000]/g;

const SHELL_TOOLS = new Set(["bash", "powershell", "sh", "zsh", "cmd"]);
const SAFE_FILE_TOOLS = new Set(["read", "write", "edit", "ls", "find", "grep"]);
const MANDATORY_PATH_TOOLS = new Set(["read", "write", "edit"]);

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

function normalizePiPath(rawPath: string): string | null {
  let normalized = rawPath.replace(UNICODE_SPACES_RE, " ").trim();
  while (normalized.startsWith("@")) {
    normalized = normalized.slice(1).replace(UNICODE_SPACES_RE, " ").trim();
  }
  if (/^file:/i.test(normalized)) {
    try {
      normalized = fileURLToPath(normalized);
    } catch {
      return null;
    }
  }
  return expandHome(normalized);
}

function resolveAbsoluteRealpath(absolute: string): string | null {
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

function isPathConfinedToWorkspace(rawPath: string, workspaceReal: string): boolean {
  const normalized = normalizePiPath(rawPath);
  if (normalized === null) {
    return false;
  }
  if (!normalized) {
    return true;
  }

  const baseAbsolute = path.resolve(workspaceReal, normalized);
  const variants = new Set<string>([
    baseAbsolute,
    baseAbsolute.normalize("NFD"),
    baseAbsolute.replace(/'/g, "\u2019"),
    baseAbsolute.normalize("NFD").replace(/'/g, "\u2019"),
  ]);

  for (const variant of variants) {
    const resolved = resolveAbsoluteRealpath(variant);
    if (!resolved || !isInsideWorkspace(resolved, workspaceReal)) {
      return false;
    }
  }
  return true;
}

function isUnsafeGlobPattern(pattern: string): boolean {
  const trimmed = pattern.replace(UNICODE_SPACES_RE, " ").trim();
  return (
    trimmed.includes("..") ||
    trimmed.startsWith("/") ||
    trimmed.startsWith("~") ||
    trimmed.startsWith("@") ||
    /^file:/i.test(trimmed)
  );
}

function checkFileToolConfinement(
  toolName: string,
  input: Record<string, unknown> | undefined,
  cwd: string,
): { block: true; reason: string } | undefined {
  let workspaceReal: string;
  try {
    workspaceReal = fs.realpathSync(cwd);
  } catch {
    return {
      block: true,
      reason: "GATEKEEPER_INVALID_WORKSPACE: Active workspace directory cannot be resolved.",
    };
  }

  if (MANDATORY_PATH_TOOLS.has(toolName)) {
    if (!input || typeof input.path !== "string" || !input.path.trim()) {
      return {
        block: true,
        reason: `GATEKEEPER_MISSING_FILE_PATH: Tool '${toolName}' requires a non-empty string path.`,
      };
    }
  }

  if (!input) {
    return undefined;
  }

  if (toolName === "find" && typeof input.pattern === "string" && isUnsafeGlobPattern(input.pattern)) {
    return {
      block: true,
      reason: `GATEKEEPER_OUTSIDE_WORKSPACE_GLOB: Glob '${input.pattern}' may escape workspace.`,
    };
  }
  if (toolName === "grep" && typeof input.glob === "string" && isUnsafeGlobPattern(input.glob)) {
    return {
      block: true,
      reason: `GATEKEEPER_OUTSIDE_WORKSPACE_GLOB: Glob '${input.glob}' may escape workspace.`,
    };
  }

  for (const key of PATH_ARGUMENT_KEYS) {
    const val = input[key];
    if (val === undefined) {
      continue;
    }
    if (typeof val !== "string") {
      return {
        block: true,
        reason: `GATEKEEPER_INVALID_FILE_PATH: Argument '${key}' must be a string.`,
      };
    }
    if (!isPathConfinedToWorkspace(val, workspaceReal)) {
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

    const inputObj = event.input as Record<string, unknown> | undefined;
    const hasShellCommandField =
      inputObj !== undefined &&
      (inputObj.command !== undefined || inputObj.CommandLine !== undefined);

    if (SHELL_TOOLS.has(event.toolName) || hasShellCommandField) {
      const rawCommand = inputObj?.command ?? inputObj?.CommandLine;
      if (typeof rawCommand !== "string") {
        return {
          block: true,
          reason: "GATEKEEPER_INVALID_COMMAND: Shell execution tool requires string command.",
        };
      }
      const authSecret = process.env.NOVA_GATEKEEPER_TOKEN ?? "";
      const decision = await queryGatekeeperSocket(
        socketPath,
        authSecret,
        rawCommand,
        ctx.cwd,
      );
      if (decision.allowed === true) {
        if (event.toolName !== "bash" && decision.verdict !== "approved_by_human") {
          return {
            block: true,
            reason: `GATEKEEPER_NON_BASH_SHELL: Non-bash shell '${event.toolName}' cannot bypass POSIX Layer 1 without explicit human approval.`,
          };
        }
        return undefined;
      }
      return {
        block: true,
        reason: decision.reason ?? "Blocked by Nova SecurityGatekeeper.",
      };
    }

    if (SAFE_FILE_TOOLS.has(event.toolName)) {
      return checkFileToolConfinement(event.toolName, inputObj, ctx.cwd);
    }

    return {
      block: true,
      reason: `GATEKEEPER_UNTRUSTED_TOOL: Tool '${event.toolName}' is not in approved workspace file toolset.`,
    };
  });
}
