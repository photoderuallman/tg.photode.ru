const refreshButton = document.querySelector("#refresh-status");
const checkedLabel = document.querySelector("#last-checked");
const authNext = document.querySelector("#auth-next");
const authPanel = document.querySelector("#authorization");
const authMode = document.querySelector("#auth-mode");
const authForm = document.querySelector("#auth-form");
const authLabel = document.querySelector("#auth-label");
const authInput = document.querySelector("#auth-input");
const authSubmit = document.querySelector("#auth-submit");
const authHelper = document.querySelector("#auth-helper");
const authLocked = document.querySelector("#auth-locked");
const authLockedText = document.querySelector("#auth-locked-text");
const authFeedback = document.querySelector("#auth-feedback");

let renderedAuthorizationState = null;

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

const authorizationStages = {
  not_configured: { complete: 0, active: "api" },
  wait_phone_number: { complete: 1, active: "number" },
  wait_code: { complete: 2, active: "proof" },
  wait_password: { complete: 2, active: "proof" },
  ready: { complete: 4, active: null },
  error: { complete: 0, active: "api" },
};

const stageNames = ["api", "number", "proof", "ready"];

function renderAuthorizationRail(state) {
  const progress = authorizationStages[state] ?? authorizationStages.error;

  document.querySelectorAll("[data-auth-step]").forEach((step) => {
    const index = stageNames.indexOf(step.dataset.authStep);
    const isComplete = index < progress.complete;
    const isActive = step.dataset.authStep === progress.active;

    step.classList.toggle("is-complete", isComplete);
    step.classList.toggle("is-active", isActive);
    if (isActive) {
      step.setAttribute("aria-current", "step");
    } else {
      step.removeAttribute("aria-current");
    }
  });
}

function configureAuthorizationForm(status) {
  const stateChanged = renderedAuthorizationState !== status.state;
  const mockWord = status.is_mock ? "test " : "";
  const configuration = {
    wait_phone_number: {
      label: "Phone number",
      type: "tel",
      inputMode: "tel",
      autocomplete: "tel",
      placeholder: "+12223334455",
      endpoint: "/api/telegram/auth/phone",
      field: "phone_number",
      button: `Continue with ${mockWord}number`,
      maxLength: 16,
    },
    wait_code: {
      label: "Authorization code",
      type: "text",
      inputMode: "text",
      autocomplete: "one-time-code",
      placeholder: "Enter the code Telegram sent",
      endpoint: "/api/telegram/auth/code",
      field: "code",
      button: `Verify ${mockWord}code`,
      maxLength: 64,
    },
    wait_password: {
      label: "Two-step verification password",
      type: "password",
      inputMode: "text",
      autocomplete: "current-password",
      placeholder: "Enter your Telegram password",
      endpoint: "/api/telegram/auth/password",
      field: "password",
      button: `Check ${mockWord}password`,
      maxLength: 256,
    },
  }[status.state];

  if (!configuration) {
    authForm.hidden = true;
    authInput.disabled = true;
    return;
  }

  authForm.hidden = false;
  authInput.disabled = false;
  authLabel.textContent = configuration.label;
  authInput.type = configuration.type;
  authInput.inputMode = configuration.inputMode;
  authInput.autocomplete = configuration.autocomplete;
  authInput.placeholder = configuration.placeholder;
  authInput.maxLength = configuration.maxLength;
  authForm.dataset.endpoint = configuration.endpoint;
  authForm.dataset.field = configuration.field;
  authSubmit.textContent = configuration.button;
  authHelper.textContent = status.password_hint ?? status.next_action ?? status.detail;

  if (stateChanged) {
    authInput.value = "";
    window.requestAnimationFrame(() => authInput.focus());
  }
}

function renderAuthorization(status) {
  authPanel.dataset.authState = status.state;
  authMode.dataset.mock = String(status.is_mock);
  authMode.textContent = status.is_mock
    ? "Simulation / no network"
    : status.state === "not_configured"
      ? "Live path starting"
      : "Live TDLib connection";
  authNext.textContent = status.next_action ?? status.detail;
  renderAuthorizationRail(status.state);
  configureAuthorizationForm(status);

  const showLockedMessage = ["not_configured", "ready", "error"].includes(status.state);
  authLocked.hidden = !showLockedMessage;
  if (status.state === "ready") {
    authLockedText.textContent = status.detail;
  } else if (status.state === "error") {
    authLockedText.textContent = "The adapter needs attention before authorization can continue.";
  } else {
    authLockedText.textContent = "Waiting for the TDLib adapter and private API credentials.";
  }

  renderedAuthorizationState = status.state;
}

function setAuthorizationFeedback(message = "", tone = "") {
  authFeedback.textContent = message;
  if (tone) {
    authFeedback.dataset.tone = tone;
  } else {
    delete authFeedback.dataset.tone;
  }
}

async function parseApiError(response) {
  try {
    const payload = await response.json();
    const message = payload.detail?.message ?? payload.detail;
    return typeof message === "string" ? message : `Request rejected (HTTP ${response.status}).`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

async function refreshStatus() {
  refreshButton.setAttribute("aria-busy", "true");
  refreshButton.disabled = true;

  try {
    const requestOptions = {
      headers: { Accept: "application/json" },
      cache: "no-store",
    };
    const [response, authorizationResponse] = await Promise.all([
      fetch("/api/status", requestOptions),
      fetch("/api/telegram/auth", requestOptions),
    ]);

    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (!authorizationResponse.ok) {
      throw new Error(`Authorization HTTP ${authorizationResponse.status}`);
    }

    const status = await response.json();
    const authorization = await authorizationResponse.json();
    renderComponent("app", status.app);
    renderComponent("vpn", status.vpn);
    renderComponent("telegram_network", status.telegram_network);
    renderComponent("telegram_auth", status.telegram_auth);
    renderAuthorization(authorization);

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

authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const value = authInput.value;
  const field = authForm.dataset.field;
  const endpoint = authForm.dataset.endpoint;

  if (!value.trim() || !field || !endpoint) {
    setAuthorizationFeedback("Enter a value before continuing.", "error");
    return;
  }

  authInput.disabled = true;
  authSubmit.disabled = true;
  authSubmit.setAttribute("aria-busy", "true");
  setAuthorizationFeedback("Checking this step…");

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      cache: "no-store",
      body: JSON.stringify({ [field]: value }),
    });

    if (!response.ok) throw new Error(await parseApiError(response));

    const authorization = await response.json();
    authInput.value = "";
    renderAuthorization(authorization);
    setAuthorizationFeedback("Step accepted; the input was cleared.", "success");
    await refreshStatus();
  } catch (error) {
    if (["code", "password"].includes(field)) authInput.value = "";
    setAuthorizationFeedback(error.message, "error");
  } finally {
    authInput.disabled = authForm.hidden;
    authSubmit.disabled = authForm.hidden;
    authSubmit.removeAttribute("aria-busy");
    if (!authForm.hidden) authInput.focus();
  }
});

refreshButton.addEventListener("click", refreshStatus);
refreshStatus();
window.setInterval(refreshStatus, 15_000);
