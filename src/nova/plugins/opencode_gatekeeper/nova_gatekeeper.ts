/**
 * OpenCode tool.execute.before plugin bridging bash/shell commands to Nova SecurityGatekeeper.
 * Fail-closed whenever NOVA_GATEKEEPER_SOCKET is set in the child process environment.
 */
import { randomUUID } from "node:crypto";
import net from "node:net";

interface GatekeeperDecision {
  allowed?: boolean;
  reason?: string;
}

function queryGatekeeper(
  socketPath: string,
  payload: Record<string, unknown>,
  timeoutMs = 280_000,
): Promise<GatekeeperDecision> {
  return new Promise((resolve, reject) => {
    const client = net.createConnection({ path: socketPath });
    let buffer = "";
    let settled = false;

    const finish = (err: Error | null, decision?: GatekeeperDecision): void => {
      if (settled) return;
      settled = true;
      client.destroy();
      if (err) reject(err);
      else resolve(decision ?? { allowed: false, reason: "Empty Gatekeeper response." });
    };

    client.setTimeout(timeoutMs, () => {
      finish(new Error("GATEKEEPER_IPC_TIMEOUT: Timed out waiting for Nova Gatekeeper."));
    });

    client.on("error", (err) => {
      finish(new Error(`GATEKEEPER_IPC_UNREACHABLE: ${err.message} (fail-closed).`));
    });

    client.on("connect", () => {
      client.write(`${JSON.stringify(payload)}\n`, "utf8");
    });

    client.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      const newlineIdx = buffer.indexOf("\n");
      if (newlineIdx >= 0) {
        const line = buffer.slice(0, newlineIdx).trim();
        try {
          const parsed = JSON.parse(line) as GatekeeperDecision;
          finish(null, parsed);
        } catch {
          finish(new Error("GATEKEEPER_INVALID_IPC_RESPONSE: Malformed JSON (fail-closed)."));
        }
      }
    });

    client.on("end", () => {
      if (!settled) {
        finish(new Error("GATEKEEPER_INCOMPLETE_IPC_RESPONSE: EOF before newline (fail-closed)."));
      }
    });
  });
}

export const NovaGatekeeperPlugin = async (ctx: { directory?: string }) => {
  return {
    "tool.execute.before": async (
      input: { tool?: string },
      output: { args?: Record<string, unknown> },
    ) => {
      const socketPath = (process.env.NOVA_GATEKEEPER_SOCKET ?? "").trim();
      if (!socketPath) {
        return;
      }
      const toolName = (input.tool ?? "").toLowerCase();
      if (toolName !== "bash" && toolName !== "shell") {
        return;
      }

      const args = output.args ?? {};
      const command = String(args.command ?? args.cmd ?? "");
      const cwd = String(args.workdir ?? args.cwd ?? ctx.directory ?? process.cwd());
      const source = (process.env.NOVA_GATEKEEPER_SOURCE ?? "tier2_opencode").trim();

      const decision = await queryGatekeeper(socketPath, {
        protocol_version: "1.0",
        token: process.env.NOVA_GATEKEEPER_TOKEN ?? "",
        correlation_id: `corr-${randomUUID()}`,
        source,
        tool_name: toolName === "shell" ? "shell" : "bash",
        command,
        cwd,
      });

      if (decision.allowed !== true) {
        const reason = decision.reason ?? "Blocked by Nova SecurityGatekeeper.";
        throw new Error(`Nova SecurityGatekeeper denied command: ${reason}`);
      }
    },
  };
};

export default NovaGatekeeperPlugin;
