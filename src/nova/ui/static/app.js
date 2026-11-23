/**
 * Browser entrypoint mounting DesktopAppController onto #nova-app-root.
 */
import { DesktopAppController } from "./controller.js";
function bootstrapNovaDesktop() {
    if (typeof document === "undefined") {
        return;
    }
    const root = document.getElementById("nova-app-root");
    if (!root) {
        return;
    }
    const isFramelessShell = window.location.hash === "#desktop-shell";
    if (isFramelessShell) {
        document.documentElement.setAttribute("data-shell-frameless", "true");
    }
    const params = new URLSearchParams(window.location.search);
    const rawTab = params.get("tab");
    const initialTab = rawTab === "whiteboard" || rawTab === "agents" || rawTab === "chat" ? rawTab : undefined;
    const rawDeck = params.get("deck")?.trim();
    const initialDeckId = rawDeck && /^[A-Za-z0-9._-]{1,80}$/.test(rawDeck) ? rawDeck : undefined;
    const controller = new DesktopAppController({
        rootElement: root,
        initialTab,
        initialDeckId,
        onMinimizeToHost: isFramelessShell
            ? () => {
                document.title = "Nova::HIDE_PANEL";
                window.setTimeout(() => {
                    document.title = "Nova";
                }, 60);
            }
            : undefined,
        onQuitHost: isFramelessShell
            ? () => {
                document.title = "Nova::QUIT_APP";
            }
            : undefined,
    });
    void controller.start();
    window.addEventListener("pagehide", () => {
        controller.dispose();
    }, { once: true });
}
if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrapNovaDesktop, { once: true });
    }
    else {
        bootstrapNovaDesktop();
    }
}
export { DesktopAppController };
