/**
 * Browser entrypoint mounting DesktopAppController onto #nova-app-root.
 */

import { DesktopAppController } from "./controller.js";

function bootstrapNovaDesktop(): void {
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

  const controller = new DesktopAppController({
    rootElement: root,
    onMinimizeToHost: isFramelessShell
      ? () => {
          document.title = "Nova::HIDE_PANEL";
          window.setTimeout(() => {
            document.title = "Nova";
          }, 60);
        }
      : undefined,
  });
  void controller.start();

  window.addEventListener(
    "pagehide",
    () => {
      controller.dispose();
    },
    { once: true },
  );
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrapNovaDesktop, { once: true });
  } else {
    bootstrapNovaDesktop();
  }
}

export { DesktopAppController };
