/**
 * Nova SecurityGatekeeper Extension for Pi Agent (Tier 1).
 *
 * 1. Confines approved workspace file tools (`read`, `write`, `edit`, `ls`, `find`, `grep`)
 *    strictly within `realpath(ctx.cwd)` by validating BOTH Pi's exact `resolveToCwd` /
 *    `resolveReadPathAsync` resolution (preserving ASCII spaces, single `@` strip, `~`,
 *    `file://`, `\u202F` AM/PM, NFD, and `\u2019` variants) AND defensive trimmed/multi-`@`
 *    forms.
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
const NARROW_NO_BREAK_SPACE = "\u202F";

const SHELL_TOOLS = new Set(["bash", "powershell", "sh", "zsh", "cmd"]);
const SAFE_FILE_TOOLS = new Set(["read", "write", "edit", "ls", "find", "grep"]);
const MANDATORY_PATH_TOOLS = new Set(["read", "write", "edit"]);
const MUTATING_FILE_TOOLS = new Set(["write", "edit"]);

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
  if (rawPath.startsWith("~/") || (process.platform === "win32" && rawPath.startsWith("~\\"))) {
    return path.join(os.homedir(), rawPath.slice(2));
  }
  return rawPath;
}

function normalizeExactPiPath(rawPath: string): string | null {
  let normalized = rawPath.replace(UNICODE_SPACES_RE, " ");
  if (normalized.startsWith("@")) {
    normalized = normalized.slice(1);
  }
  normalized = expandHome(normalized);
  if (/^file:\/\//.test(normalized)) {
    try {
      return fileURLToPath(normalized);
    } catch {
      return null;
    }
  }
  if (/^file:/i.test(normalized)) {
    return null;
  }
  return normalized;
}

function normalizeDefensivePath(rawPath: string): string | null {
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
  if (normalized.startsWith("~/") || normalized.startsWith("~\\") || normalized === "~") {
    return normalized === "~"
      ? os.homedir()
      : path.join(os.homedir(), normalized.slice(2));
  }
  return normalized;
}

function expandReadVariants(resolved: string): string[] {
  const amPm = resolved.replace(/ (AM|PM)\./gi, `${NARROW_NO_BREAK_SPACE}$1.`);
  const nfd = resolved.normalize("NFD");
  const curly = resolved.replace(/'/g, "\u2019");
  const nfdCurly = nfd.replace(/'/g, "\u2019");
  return Array.from(new Set([resolved, amPm, nfd, curly, nfdCurly]));
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

function touchesGitMetadata(candidateReal: string, workspaceReal: string): boolean {
  const rel = path.relative(workspaceReal, candidateReal);
  if (!rel || rel.startsWith("..")) {
    return false;
  }
  const segments = rel.split(/[\\/]+/);
  return segments.some((seg) => seg.toLowerCase() === ".git");
}

function isPathConfinedToWorkspace(
  rawPath: string,
  workspaceReal: string,
  forbidGitMetadataWrite = false,
): boolean {
  const exactNorm = normalizeExactPiPath(rawPath);
  const defensiveNorm = normalizeDefensivePath(rawPath);
  if (exactNorm === null || defensiveNorm === null) {
    return false;
  }

  const candidateForms = new Set<string>();
  for (const norm of [exactNorm, defensiveNorm]) {
    if (!norm) {
      continue;
    }
    const abs = path.isAbsolute(norm) ? path.resolve(norm) : path.resolve(workspaceReal, norm);
    for (const variant of expandReadVariants(abs)) {
      candidateForms.add(variant);
    }
  }

  for (const candidateAbs of candidateForms) {
    const resolved = resolveAbsoluteRealpath(candidateAbs);
    if (!resolved || !isInsideWorkspace(resolved, workspaceReal)) {
      return false;
    }
    if (forbidGitMetadataWrite && touchesGitMetadata(resolved, workspaceReal)) {
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
    const forbidGitWrite = MUTATING_FILE_TOOLS.has(toolName);
    if (!isPathConfinedToWorkspace(val, workspaceReal, forbidGitWrite)) {
      return {
        block: true,
        reason: `GATEKEEPER_OUTSIDE_WORKSPACE_FILE: File path '${val}' escapes workspace or mutates protected .git metadata.`,
      };
    }
  }

  return undefined;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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
          const parsed: unknown = JSON.parse(line);
          if (!isPlainObject(parsed)) {
            finish({
              allowed: false,
              reason: "GATEKEEPER_INVALID_RESPONSE: Expected JSON object from Nova gatekeeper.",
            });
            return;
          }
          finish({
            allowed: typeof parsed.allowed === "boolean" ? parsed.allowed : false,
            verdict: typeof parsed.verdict === "string" ? parsed.verdict : undefined,
            audit_id:
              typeof parsed.audit_id === "string" || parsed.audit_id === null
                ? parsed.audit_id
                : undefined,
            reason: typeof parsed.reason === "string" ? parsed.reason : undefined,
          });
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

    if (event.input !== undefined && !isPlainObject(event.input)) {
      return {
        block: true,
        reason: "GATEKEEPER_INVALID_TOOL_INPUT: Tool input must be a JSON object.",
      };
    }
    const inputObj = event.input;
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
