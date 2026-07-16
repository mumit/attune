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
const status = document.querySelector("#status");

function show(message, kind = "info") {
  status.textContent = message;
  status.dataset.kind = kind;
}

async function json(response) {
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error("request refused");
    error.status = response.status;
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
      show("Google Workspace is connected and verified.", "success");
      return;
    }
    if (result.state === "failed") throw new Error("connection test failed");
  }
  throw new Error("connection test timed out");
}

async function showWorkspace(session) {
  workspace.hidden = false;
  if (session.google_workspace_oauth === "connected") {
    try {
      await verifyWorkspaceConnection();
    } catch {
      workspaceButton.disabled = true;
      workspaceButton.textContent = "Workspace connected; verification unavailable";
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
  workspaceButton.addEventListener("click", async () => {
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
  });
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
    await showWorkspace(session);
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
      const session = await existingSession();
      if (session) await showWorkspace(session);
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
