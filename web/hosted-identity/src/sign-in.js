import { initializeApp } from "firebase/app";
import {
  GoogleAuthProvider,
  browserPopupRedirectResolver,
  getIdToken,
  initializeAuth,
  inMemoryPersistence,
  signInWithPopup,
  signOut,
} from "firebase/auth";

const button = document.querySelector("#google-sign-in");
const workspace = document.querySelector("#workspace-connection");
const workspaceButton = document.querySelector("#google-workspace-connect");
const disconnectButton = document.querySelector("#google-workspace-disconnect");
const onboarding = document.querySelector("#onboarding-progress");
const onboardingStart = document.querySelector("#onboarding-start");
const policyReview = document.querySelector("#policy-review");
const policyAutomatic = document.querySelector("#policy-automatic");
const policyExcluded = document.querySelector("#policy-excluded");
const policyConfirm = document.querySelector("#policy-confirm");
const sessionSignOut = document.querySelector("#session-sign-out");
const status = document.querySelector("#status");
let hostedPolicyAvailable = false;

function show(message, kind = "info") {
  status.textContent = message;
  status.dataset.kind = kind;
}

async function json(response) {
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error("request refused");
    error.status = response.status;
    error.code = payload.error;
    throw error;
  }
  return payload;
}

async function configure() {
  const configuration = await json(
    await fetch("/v1/identity/config", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }),
  );
  const app = initializeApp({
    apiKey: configuration.api_key,
    authDomain: configuration.auth_domain,
    projectId: configuration.project_id,
  });
  return initializeAuth(app, {
    persistence: inMemoryPersistence,
    popupRedirectResolver: browserPopupRedirectResolver,
  });
}

async function prepareLoginBinding() {
  return await json(
    await fetch("/v1/session/bootstrap", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }),
  );
}

async function exchange(auth, bootstrap) {
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  const credential = await signInWithPopup(auth, provider);
  try {
    const idToken = await getIdToken(credential.user, true);
    return await json(
      await fetch("/v1/session", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          id_token: idToken,
          login_challenge: bootstrap.login_challenge,
        }),
      }),
    );
  } finally {
    await signOut(auth).catch(() => {});
  }
}

function safeFailure(error) {
  const messages = {
    "auth/app-not-authorized": "This Attune application is not authorized for Google sign-in.",
    "auth/argument-error": "The Google sign-in request was rejected.",
    "auth/cancelled-popup-request": "A newer Google sign-in attempt replaced this one.",
    "auth/internal-error": "Google sign-in encountered an internal error.",
    "auth/invalid-api-key": "The Google sign-in configuration is invalid.",
    "auth/invalid-app-credential": "The Google sign-in application credential is invalid.",
    "auth/popup-blocked": "Your browser blocked the Google sign-in window.",
    "auth/popup-closed-by-user": "The Google sign-in window was closed.",
    "auth/unauthorized-domain": "This Attune domain is not authorized for sign-in.",
    "auth/operation-not-allowed": "Google sign-in is not enabled.",
    "auth/network-request-failed": "Google sign-in could not reach the identity service.",
    "auth/web-storage-unsupported": "This browser does not permit the storage required for Google sign-in.",
  };
  const code =
    typeof error?.code === "string" && /^auth\/[a-z0-9-]{1,64}$/.test(error.code)
      ? error.code
      : null;
  const name =
    typeof error?.name === "string" &&
    /^(DOMException|Error|FirebaseError|TypeError)$/.test(error.name)
      ? error.name
      : null;
  const message = messages[code] || "Sign-in was not completed. Please try again.";
  const diagnostic = code || name;
  return diagnostic ? `${message} (${diagnostic})` : message;
}

async function existingSession() {
  const response = await fetch("/v1/session", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  return response.ok ? await response.json() : null;
}

function cookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : null;
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function verifyWorkspaceConnection() {
  const csrf = cookie("__Host-attune_csrf");
  if (!csrf) throw new Error("missing session binding");
  workspaceButton.disabled = true;
  workspaceButton.textContent = "Verifying Gmail access…";
  show("Checking the read-only Google Workspace connection…");
  const started = await json(
    await fetch("/v1/connectors/google/test", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "X-Attune-CSRF": csrf,
      },
    }),
  );
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await wait(1000);
    const result = await json(
      await fetch(`/v1/connectors/google/tests/${encodeURIComponent(started.job_id)}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }),
    );
    if (result.state === "succeeded") {
      workspaceButton.textContent = "Gmail and Calendar connected";
      disconnectButton.hidden = false;
      disconnectButton.disabled = false;
      show("Google Workspace is connected and verified.", "success");
      return;
    }
    if (result.state === "failed") throw new Error("connection test failed");
  }
  throw new Error("connection test timed out");
}

async function startWorkspaceConnection() {
  workspaceButton.disabled = true;
  show("Preparing Google Workspace consent…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/connectors/google/start", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "X-Attune-CSRF": csrf,
        },
      }),
    );
    window.location.assign(result.authorization_url);
  } catch (error) {
    show(
      error.status === 409
        ? "Google Workspace is already connected."
        : "Workspace connection could not be started. Please try again.",
      error.status === 409 ? "success" : "error",
    );
    workspaceButton.disabled = false;
  }
}

async function disconnectWorkspace() {
  if (
    !window.confirm(
      "Disconnect Gmail and Calendar? Attune will immediately revoke its stored credential. You can reconnect later.",
    )
  ) return;
  disconnectButton.disabled = true;
  show("Disconnecting Google Workspace…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    await json(
      await fetch("/v1/connectors/google", {
        method: "DELETE",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Attune-CSRF": csrf,
        },
        body: JSON.stringify({ confirmation: "disconnect" }),
      }),
    );
    disconnectButton.hidden = true;
    workspaceButton.textContent = "Connect Gmail and Calendar";
    workspaceButton.disabled = false;
    show("Google Workspace is disconnected. Attune can no longer use the stored credential.", "success");
  } catch {
    disconnectButton.disabled = false;
    show("Google Workspace could not be disconnected. Please try again.", "error");
  }
}

workspaceButton.addEventListener("click", startWorkspaceConnection);
disconnectButton.addEventListener("click", disconnectWorkspace);

function renderOnboarding(state) {
  onboarding.hidden = false;
  for (const item of onboarding.querySelectorAll("[data-step]")) {
    const step = item.dataset.step;
    item.dataset.status = state.steps?.[step] || "not_started";
  }
  onboardingStart.hidden = state.status !== "not_started";
}

async function showOnboarding(session) {
  if (session.hosted_onboarding !== "available") return;
  hostedPolicyAvailable = session.hosted_policy === "available";
  const state = await json(
    await fetch("/v1/onboarding", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }),
  );
  renderOnboarding(state);
  if (hostedPolicyAvailable && state.status !== "not_started") {
    await showPolicy();
  }
}

function renderItems(target, items) {
  target.replaceChildren(
    ...items.map((item) => {
      const element = document.createElement("li");
      element.textContent = item;
      return element;
    }),
  );
}

function renderPolicy(policy) {
  policyReview.hidden = false;
  renderItems(policyAutomatic, policy.automatic || []);
  renderItems(policyExcluded, policy.excluded || []);
  policyConfirm.hidden = policy.status === "validated";
  policyConfirm.disabled = false;
}

async function showPolicy() {
  const policy = await json(
    await fetch("/v1/onboarding/policy", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }),
  );
  renderPolicy(policy);
}

policyConfirm.addEventListener("click", async () => {
  policyConfirm.disabled = true;
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/onboarding/policy/confirm", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-Attune-CSRF": csrf },
      }),
    );
    renderPolicy(result.policy);
    renderOnboarding(result.onboarding);
    show("Read-only policy enabled. Write capabilities remain unavailable.", "success");
  } catch (error) {
    policyConfirm.disabled = false;
    if (error.code === "recent_authentication_required") {
      show("Sign out and sign in again before changing assistant policy.", "pending");
    } else if (error.code === "policy_requires_repair") {
      show("Policy state changed outside Attune and requires operator repair.", "error");
    } else {
      show("Assistant policy could not be enabled. Please try again.", "error");
    }
  }
});

sessionSignOut.addEventListener("click", async () => {
  sessionSignOut.disabled = true;
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    await json(
      await fetch("/v1/session", {
        method: "DELETE",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-Attune-CSRF": csrf },
      }),
    );
    window.location.assign("/");
  } catch {
    sessionSignOut.disabled = false;
    show("Sign out could not be completed. Please try again.", "error");
  }
});

onboardingStart.addEventListener("click", async () => {
  onboardingStart.disabled = true;
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const state = await json(
      await fetch("/v1/onboarding/start", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-Attune-CSRF": csrf },
      }),
    );
    renderOnboarding(state);
    if (hostedPolicyAvailable) await showPolicy();
    show("Guided setup started. Your progress will be saved.", "success");
  } catch {
    onboardingStart.disabled = false;
    show("Guided setup could not be started. Please try again.", "error");
  }
});

async function showWorkspace(session) {
  workspace.hidden = false;
  if (session.google_workspace_oauth === "connected") {
    try {
      await verifyWorkspaceConnection();
    } catch {
      workspaceButton.disabled = true;
      workspaceButton.textContent = "Workspace connected; verification unavailable";
      disconnectButton.hidden = false;
      disconnectButton.disabled = false;
      show(
        "Google Workspace is connected, but Attune could not verify Gmail access. Try again later.",
        "error",
      );
    }
    return;
  }
  if (session.google_workspace_oauth !== "available") {
    workspaceButton.disabled = true;
    workspaceButton.textContent = "Workspace connection is being prepared";
    return;
  }
  workspaceButton.disabled = false;
}

async function main() {
  const outcome = new URLSearchParams(window.location.search).get("workspace");
  if (outcome) window.history.replaceState({}, "", window.location.pathname);
  const session = await existingSession();
  if (session) {
    const messages = {
      connected: ["Google Workspace is connected.", "success"],
      denied: ["Google Workspace access was not granted.", "pending"],
      failed: ["Workspace connection could not be completed. Please try again.", "error"],
    };
    const result = messages[outcome] || ["Signed in to Attune.", "success"];
    show(result[0], result[1]);
    button.hidden = true;
    sessionSignOut.hidden = false;
    await showWorkspace(session);
    await showOnboarding(session);
    return;
  }
  const auth = await configure();
  let bootstrap = await prepareLoginBinding();
  button.disabled = false;
  button.addEventListener("click", async () => {
    button.disabled = true;
    show("Waiting for Google sign-in…");
    try {
      // Calling exchange before the first await preserves the click's user
      // activation for browsers that otherwise block the provider popup.
      const attempt = exchange(auth, bootstrap);
      await attempt;
      show("Signed in to Attune.", "success");
      button.hidden = true;
      sessionSignOut.hidden = false;
      const session = await existingSession();
      if (session) {
        await showWorkspace(session);
        await showOnboarding(session);
      }
    } catch (error) {
      if (error.status === 409) {
        show(
          "Google identity verified. Your Attune development membership is not provisioned yet.",
          "pending",
        );
      } else {
        show(safeFailure(error), "error");
      }
      try {
        bootstrap = await prepareLoginBinding();
        button.disabled = false;
      } catch {
        show("Sign-in is temporarily unavailable.", "error");
      }
    }
  });
}

main().catch(() => {
  show("Sign-in is temporarily unavailable.", "error");
});
