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
    const controller = new DesktopAppController({ rootElement: root });
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
