const refreshButton = document.querySelector("#refresh-status");
const checkedLabel = document.querySelector("#last-checked");
const authNext = document.querySelector("#auth-next");

const stateNames = {
  ok: "Ready",
  waiting: "Waiting",
  not_configured: "Not configured",
  degraded: "Degraded",
  offline: "Offline",
};

function renderComponent(name, component) {
  const node = document.querySelector(`[data-component="${name}"]`);
  if (!node || !component) return;

  node.querySelector("h3").textContent = component.label;
  node.querySelector("p").textContent = component.detail;

  const badge = node.querySelector(".state");
  badge.dataset.state = component.state;
  badge.textContent = stateNames[component.state] ?? component.state;
}

async function refreshStatus() {
  refreshButton.setAttribute("aria-busy", "true");
  refreshButton.disabled = true;

  try {
    const response = await fetch("/api/status", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const status = await response.json();
    renderComponent("app", status.app);
    renderComponent("vpn", status.vpn);
    renderComponent("telegram_network", status.telegram_network);
    renderComponent("telegram_auth", status.telegram_auth);

    authNext.textContent = status.telegram_auth.next_action ?? status.telegram_auth.detail;
    checkedLabel.textContent = `Checked ${new Date(status.generated_at).toLocaleTimeString()}`;
  } catch (error) {
    renderComponent("app", {
      state: "offline",
      label: "Web application",
      detail: `Health request failed: ${error.message}`,
    });
    checkedLabel.textContent = "Check failed";
  } finally {
    refreshButton.removeAttribute("aria-busy");
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", refreshStatus);
refreshStatus();
window.setInterval(refreshStatus, 15_000);
