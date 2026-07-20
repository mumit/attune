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
const signupButton = document.querySelector("#hosted-signup-create");
const workspace = document.querySelector("#workspace-connection");
const workspaceButton = document.querySelector("#google-workspace-connect");
const disconnectButton = document.querySelector("#google-workspace-disconnect");
const onboarding = document.querySelector("#onboarding-progress");
const onboardingStart = document.querySelector("#onboarding-start");
const channelPreferences = document.querySelector("#channel-preferences");
const channelsSave = document.querySelector("#channels-save");
const channelInstallations = document.querySelector("#channel-installations");
const googleChatInstallation = document.querySelector("#google-chat-installation");
const googleChatInstallationState = document.querySelector("#google-chat-installation-state");
const googleChatLinkStart = document.querySelector("#google-chat-link-start");
const googleChatDeliveryTest = document.querySelector("#google-chat-delivery-test");
const googleChatDisconnect = document.querySelector("#google-chat-disconnect");
const googleChatLinkInstructions = document.querySelector("#google-chat-link-instructions");
const googleChatLinkCommand = document.querySelector("#google-chat-link-command");
const googleChatLinkExpiry = document.querySelector("#google-chat-link-expiry");
const slackInstallation = document.querySelector("#slack-installation");
const slackInstallationState = document.querySelector("#slack-installation-state");
const slackInstallStart = document.querySelector("#slack-install-start");
const slackDeliveryTest = document.querySelector("#slack-delivery-test");
const slackDisconnect = document.querySelector("#slack-disconnect");
const policyReview = document.querySelector("#policy-review");
const policyAutomatic = document.querySelector("#policy-automatic");
const policyExcluded = document.querySelector("#policy-excluded");
const policyConfirm = document.querySelector("#policy-confirm");
const customerExports = document.querySelector("#customer-exports");
const customerExportCreate = document.querySelector("#customer-export-create");
const customerExportList = document.querySelector("#customer-export-list");
const modelProfile = document.querySelector("#model-profile");
const modelProfileSelect = document.querySelector("#model-profile-select");
const modelProfileSave = document.querySelector("#model-profile-save");
const modelProfileState = document.querySelector("#model-profile-state");
const modelUsage = document.querySelector("#model-usage");
const accountDeletion = document.querySelector("#account-deletion");
const accountDeletionState = document.querySelector("#account-deletion-state");
const accountDeletionRequest = document.querySelector("#account-deletion-request");
const accountDeletionCancel = document.querySelector("#account-deletion-cancel");
const conversationPanel = document.querySelector("#conversation-panel");
const conversationMessages = document.querySelector("#conversation-messages");
const conversationIndicator = document.querySelector("#conversation-indicator");
const conversationInput = document.querySelector("#conversation-input");
const conversationSend = document.querySelector("#conversation-send");
const conversationNotifyToggle = document.querySelector("#conversation-notify-toggle");
const conversationNotifyState = document.querySelector("#conversation-notify-state");
const conversationHints = document.querySelector("#conversation-hints");
const sessionSignOut = document.querySelector("#session-sign-out");
const status = document.querySelector("#status");
let hostedPolicyAvailable = false;
let hostedChannelsAvailable = false;
let hostedChannelSetupAvailable = false;
let hostedChannelLifecycleAvailable = false;
let hostedWebConversationAvailable = false;
let customerExportsAvailable = false;
let conversationHighestSequence = 0;
let conversationPollTimer = null;
let conversationPendingSince = null;
let conversationSending = false;
let conversationPollFailures = 0;
let conversationNotificationsEnabled = false;

// --- Recency-window UX (UX review hosted item #1) -------------------------
//
// The server independently re-validates session age on every recency-gated
// ceremony (policy confirmation, channel install/disconnect, deletion,
// export authorize/download -- see docs/hosted-policy.md and the
// `recent_authentication_required` 409 each of those routes can return).
// That ten-minute bar is the actual security boundary and is unchanged by
// anything below. Everything in this section is advisory client-side UX
// only: a best-effort estimate of how much of the window is left, used to
// warn the user before they get bounced and to offer a resumable re-auth
// instead of a dead end. It can never grant, extend, or shorten the
// server's own authority window -- if this estimate is ever wrong (clock
// skew, a session that predates this tab, a session created in another
// tab), the ceremony's own 409 is still authoritative and is handled the
// same way (see `forceLapsedNow` below).
const RECENCY_WINDOW_MS = 10 * 60 * 1000;
const RECENCY_WARNING_MS = 3 * 60 * 1000;
const SESSION_STARTED_KEY = "attune_session_started_at";
const RESUME_SECTION_KEY = "attune_resume_section";
const CONVERSATION_POLL_INTERVAL_MS = 2000;
const CONVERSATION_POLL_SLOW_INTERVAL_MS = 15000;
const CONVERSATION_STALL_MS = 60_000;
const CONVERSATION_TERMINAL_MS = 5 * 60_000;

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

async function attemptSignup(auth, bootstrap) {
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  const credential = await signInWithPopup(auth, provider);
  try {
    const idToken = await getIdToken(credential.user, true);
    return await json(
      await fetch("/v1/signup", {
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

function markSessionStarted() {
  // Recorded only at the moment this tab actually performs the sign-in
  // exchange (see the `exchange()` call site below) -- never inferred from
  // an already-existing session on page load, since this tab has no way to
  // know when that session truly began. sessionStorage (not a cookie) keeps
  // it out of anything the server reads, and it naturally survives a
  // same-tab reload but not a new tab or browser restart.
  try {
    window.sessionStorage.setItem(SESSION_STARTED_KEY, String(Date.now()));
  } catch {
    /* Storage unavailable (private browsing, disabled storage): the
       countdown and pre-flight simply stay in their "unknown" state below;
       the server's own recency check is unaffected either way. */
  }
}

function recencyRemainingMs() {
  let raw;
  try {
    raw = window.sessionStorage.getItem(SESSION_STARTED_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  const started = Number(raw);
  if (!Number.isFinite(started)) return null;
  return RECENCY_WINDOW_MS - (Date.now() - started);
}

function forceLapsedNow() {
  // A ceremony call still returned `recent_authentication_required` despite
  // our local estimate (clock skew, a race, or an unknown/never-set
  // estimate). The server is right; fold that into our local state
  // immediately so the in-place "sign in again" affordance appears at every
  // gated ceremony (recency is one session-wide window, not per-ceremony)
  // instead of leaving only a passive status message.
  try {
    window.sessionStorage.setItem(
      SESSION_STARTED_KEY,
      String(Date.now() - RECENCY_WINDOW_MS - 1),
    );
  } catch {
    /* Nothing to force locally; the textual error message still guides the
       user to sign out and back in. */
  }
  refreshRecencyGates();
}

function formatRecencyCountdown(remainingMs) {
  const totalSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

const RECENCY_GATE_KEYS = [
  "policy",
  "channels",
  "channel-installations",
  "deletion",
  "export",
];
const recencyGates = new Map();

function buildRecencyGate(key) {
  const anchor = document.querySelector(`[data-recency-gate="${key}"]`);
  if (!anchor) return null;
  const container = document.createElement("p");
  container.className = "recency-status";
  container.setAttribute("aria-live", "polite");
  container.hidden = true;
  const countdownText = document.createElement("span");
  countdownText.hidden = true;
  const lapsedText = document.createElement("span");
  lapsedText.hidden = true;
  lapsedText.textContent = "Sign in again to continue this step.";
  const reauthButton = document.createElement("button");
  reauthButton.type = "button";
  reauthButton.className = "secondary recency-reauth";
  reauthButton.hidden = true;
  reauthButton.textContent = "Sign in again";
  reauthButton.addEventListener("click", () => requestReauthForSection(key));
  container.append(countdownText, lapsedText, reauthButton);
  anchor.parentNode.insertBefore(container, anchor);
  return { key, container, countdownText, lapsedText, reauthButton };
}

for (const key of RECENCY_GATE_KEYS) {
  const gate = buildRecencyGate(key);
  if (gate) recencyGates.set(key, gate);
}

function renderRecencyGate(gate, remainingMs) {
  const lapsed = remainingMs !== null && remainingMs <= 0;
  const warning = !lapsed && remainingMs !== null && remainingMs <= RECENCY_WARNING_MS;
  gate.container.hidden = !(lapsed || warning);
  gate.countdownText.hidden = !warning;
  gate.lapsedText.hidden = !lapsed;
  gate.reauthButton.hidden = !lapsed;
  if (warning) {
    gate.countdownText.textContent =
      `Recent sign-in expires in ${formatRecencyCountdown(remainingMs)} — ` +
      "finish this step or you'll sign in again.";
  }
  if (lapsed) {
    // The pre-flight: hide the real confirm control(s) rather than merely
    // disabling them, so the "sign in again" affordance above is the only
    // thing offered. A fresh sign-in reruns the normal render pipeline,
    // which restores each control's correct hidden/shown state from real
    // server data -- nothing here needs to remember or restore it.
    for (const control of document.querySelectorAll(`[data-recency-gate="${gate.key}"]`)) {
      control.hidden = true;
    }
  }
}

function refreshRecencyGates() {
  const remaining = recencyRemainingMs();
  for (const gate of recencyGates.values()) renderRecencyGate(gate, remaining);
}

// Advisory-only ticker: reconciles every mounted gate against the estimated
// remaining window once a second. Harmless no-op while no local estimate
// exists (nothing lapses or warns), and each render function below also
// calls refreshRecencyGates() itself so a status change is reflected
// immediately rather than waiting up to a second for this tick.
window.setInterval(refreshRecencyGates, 1000);

async function performSignOut() {
  const csrf = cookie("__Host-attune_csrf");
  if (!csrf) throw new Error("missing session binding");
  await json(
    await fetch("/v1/session", {
      method: "DELETE",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-Attune-CSRF": csrf },
    }),
  );
}

async function requestReauthForSection(sectionKey) {
  // Reuses the exact same sign-out-then-sign-in-again ceremony the visible
  // "Sign out" button already performs -- no new re-auth endpoint, no
  // change to session semantics. The only addition is remembering, in this
  // tab's sessionStorage, which section to return the user to once they
  // finish signing back in (see resumePendingSection()).
  try {
    window.sessionStorage.setItem(RESUME_SECTION_KEY, sectionKey);
  } catch {
    /* The user can still sign back in; they just won't be scrolled back to
       this section automatically. */
  }
  show("Signing you out so you can sign in again…", "pending");
  try {
    await performSignOut();
  } catch {
    /* Best-effort: every route re-validates the session server-side
       regardless of what this tab believes, so navigating to "/" still
       forces a fresh sign-in even if this call itself failed. */
  }
  window.location.assign("/");
}

const RESUME_SECTION_ELEMENTS = {
  policy: policyReview,
  channels: channelPreferences,
  "channel-installations": channelInstallations,
  deletion: accountDeletion,
  export: customerExports,
};

function resumePendingSection() {
  let key;
  try {
    key = window.sessionStorage.getItem(RESUME_SECTION_KEY);
    if (key) window.sessionStorage.removeItem(RESUME_SECTION_KEY);
  } catch {
    return;
  }
  if (!key) return;
  const target = RESUME_SECTION_ELEMENTS[key];
  if (target && !target.hidden) {
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    show("Signed back in. Continuing where you left off.", "success");
  }
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
  hostedChannelsAvailable = session.hosted_channels === "available";
  hostedChannelSetupAvailable = session.hosted_channel_setup === "available";
  hostedChannelLifecycleAvailable = session.hosted_channel_lifecycle === "available";
  hostedWebConversationAvailable = session.hosted_web_conversation === "available";
  customerExportsAvailable = session.customer_exports === "available";
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
  if (hostedChannelsAvailable && state.status !== "not_started") {
    await showChannels();
  }
  if (hostedChannelSetupAvailable && state.status !== "not_started") {
    await showChannelInstallations();
  }
  if (
    hostedWebConversationAvailable &&
    session.google_workspace_oauth === "connected" &&
    state.steps?.policy === "validated"
  ) {
    await startConversation();
  }
  if (customerExportsAvailable) await showCustomerExports();
}

function exportStateLabel(item) {
  const labels = {
    requested: "Queued",
    running: "Creating encrypted archive…",
    ready: "Ready to download",
    consumed: "Downloaded and queued for secure cleanup",
    expired: "Expired and securely erased",
    failed: "Could not be created",
    cancelled: "Cancelled",
  };
  return labels[item.state] || "Unavailable";
}

function renderCustomerExports(payload) {
  customerExports.hidden = false;
  customerExportList.replaceChildren(
    ...(payload.exports || []).map((item) => {
      const container = document.createElement("div");
      container.className = "customer-export-item";
      const state = document.createElement("strong");
      state.textContent = exportStateLabel(item);
      const detail = document.createElement("div");
      detail.textContent = `Account export requested ${new Date(item.created_at).toLocaleString()}.`;
      container.append(state, detail);
      if (item.download_available) {
        const download = document.createElement("button");
        download.type = "button";
        download.textContent = "Download once";
        download.dataset.recencyGate = "export";
        download.addEventListener("click", () => downloadCustomerExport(item.id, download));
        container.append(download);
      }
      return container;
    }),
  );
  customerExportCreate.disabled = (payload.exports || []).some((item) =>
    ["requested", "running", "ready"].includes(item.state),
  );
  refreshRecencyGates();
}

function deletionStateLabel(item) {
  if (!item || item.status === "none") return null;
  const labels = {
    pending: `Deletion requested. It proceeds automatically on ${
      item.grace_expires_at ? new Date(item.grace_expires_at).toLocaleString() : "the grace date"
    } unless cancelled.`,
    claimed: "Deletion is in progress and can no longer be cancelled.",
    completed: "Your account has been deleted.",
    failed: "Deletion could not complete automatically. Contact your operator.",
  };
  return labels[item.status] || null;
}

async function renderModelUsage() {
  try {
    const usage = await json(
      await fetch("/v1/usage", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }),
    );
    if (!usage.items.length) {
      modelUsage.textContent = `No model usage recorded in the last ${usage.window_days} days.`;
      return;
    }
    const totals = usage.items.reduce(
      (acc, item) => ({
        requests: acc.requests + item.request_count,
        input: acc.input + item.input_tokens,
        output: acc.output + item.output_tokens,
      }),
      { requests: 0, input: 0, output: 0 },
    );
    modelUsage.textContent =
      `${totals.requests} requests, ${totals.input} input tokens, ` +
      `${totals.output} output tokens in the last ${usage.window_days} days.`;
  } catch {
    modelUsage.textContent = "";
  }
}

async function renderModelProfile() {
  // This section has no pre-session availability signal (mirrors the
  // account-deletion section immediately below): it is shown optimistically
  // after sign-in, and a 404 from its own route is the honest signal that
  // the ATTUNE_ENABLE_TENANT_MODEL_PROFILES gate is off.
  let current;
  try {
    current = await json(
      await fetch("/v1/model-profile", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }),
    );
  } catch (error) {
    if (error.status === 404) {
      modelProfile.hidden = true;
      return null;
    }
    return null;
  }
  modelProfile.hidden = false;
  modelProfileSelect.value = current.profile;
  await renderModelUsage();
  return current;
}

modelProfileSave.addEventListener("click", async () => {
  modelProfileSave.disabled = true;
  modelProfileState.textContent = "Saving…";
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    await json(
      await fetch("/v1/model-profile", {
        method: "PUT",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Attune-CSRF": csrf,
        },
        body: JSON.stringify({
          schema_version: 1,
          profile: modelProfileSelect.value,
        }),
      }),
    );
    modelProfileState.textContent = "Model profile saved.";
    await renderModelUsage();
  } catch {
    modelProfileState.textContent = "Model profile could not be saved. Please try again.";
  }
  modelProfileSave.disabled = false;
});

async function renderAccountDeletion() {
  // This section has no pre-session availability signal (mirrors the hosted
  // signup button, docs/hosted-signup.md section 9): it is shown
  // optimistically after sign-in, and a 404 from its own route is the
  // honest signal that the gate is off.
  let item;
  try {
    item = await json(
      await fetch("/v1/account/deletion-request", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }),
    );
  } catch (error) {
    if (error.status === 404) {
      accountDeletion.hidden = true;
      return null;
    }
    return null;
  }
  accountDeletion.hidden = false;
  const label = deletionStateLabel(item);
  accountDeletionState.textContent = label || "";
  const active = item.status === "pending" || item.status === "claimed";
  accountDeletionRequest.hidden = active;
  accountDeletionCancel.hidden = item.status !== "pending";
  refreshRecencyGates();
  return item;
}

accountDeletionRequest.addEventListener("click", async () => {
  if (
    !window.confirm(
      "Delete your Attune account? This begins a grace period before your content and account are permanently erased.",
    )
  ) return;
  accountDeletionRequest.disabled = true;
  show("Requesting account deletion…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    await json(
      await fetch("/v1/account/deletion-requests", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Attune-CSRF": csrf,
        },
        body: JSON.stringify({ confirmation: "delete my account" }),
      }),
    );
    await renderAccountDeletion();
    show("Account deletion requested.", "success");
  } catch (error) {
    accountDeletionRequest.disabled = false;
    if (error.code === "recent_authentication_required") forceLapsedNow();
    show(
      error.code === "recent_authentication_required"
        ? "Sign out and sign in again before deleting your account."
        : "Account deletion could not be requested. Please try again.",
      error.code === "recent_authentication_required" ? "pending" : "error",
    );
  }
});

accountDeletionCancel.addEventListener("click", async () => {
  accountDeletionCancel.disabled = true;
  show("Cancelling account deletion…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    await json(
      await fetch("/v1/account/deletion-requests", {
        method: "DELETE",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Attune-CSRF": csrf,
        },
        body: JSON.stringify({ confirmation: "cancel deletion" }),
      }),
    );
    await renderAccountDeletion();
    show("Account deletion cancelled.", "success");
  } catch (error) {
    if (error.code === "recent_authentication_required") forceLapsedNow();
    show(
      error.code === "recent_authentication_required"
        ? "Sign out and sign in again before cancelling deletion."
        : "Account deletion could not be cancelled. Please try again.",
      error.code === "recent_authentication_required" ? "pending" : "error",
    );
  } finally {
    accountDeletionCancel.disabled = false;
  }
});

async function showCustomerExports() {
  const payload = await json(
    await fetch("/v1/exports", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }),
  );
  renderCustomerExports(payload);
  return payload;
}

customerExportCreate.addEventListener("click", async () => {
  customerExportCreate.disabled = true;
  show("Creating a private encrypted export…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    await json(
      await fetch("/v1/exports", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Attune-CSRF": csrf,
        },
        body: JSON.stringify({ scope: "account", confirmation: "create export" }),
      }),
    );
    await showCustomerExports();
    show("Export requested. This page will show when it is ready.", "success");
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await wait(2000);
      const payload = await showCustomerExports();
      if (!(payload.exports || []).some((item) => ["requested", "running"].includes(item.state))) break;
    }
  } catch (error) {
    customerExportCreate.disabled = false;
    if (error.code === "recent_authentication_required") forceLapsedNow();
    show(
      error.code === "recent_authentication_required"
        ? "Sign out and sign in again before creating an export."
        : "The export could not be requested. Please try again.",
      error.code === "recent_authentication_required" ? "pending" : "error",
    );
  }
});

async function downloadCustomerExport(exportId, button) {
  button.disabled = true;
  show("Authorizing one-time download…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const grant = await json(
      await fetch(`/v1/exports/${encodeURIComponent(exportId)}/download-authorizations`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Attune-CSRF": csrf,
        },
        body: JSON.stringify({ confirmation: "download export" }),
      }),
    );
    const response = await fetch("/v1/export-download", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grant_id: grant.grant_id, secret: grant.secret }),
    });
    if (!response.ok) throw new Error("download refused");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "attune-account-export.zip";
    link.click();
    URL.revokeObjectURL(url);
    await showCustomerExports();
    show("Export downloaded once and queued for secure deletion.", "success");
  } catch (error) {
    button.disabled = false;
    if (error.code === "recent_authentication_required") forceLapsedNow();
    show(
      error.code === "recent_authentication_required"
        ? "Sign out and sign in again before downloading your export."
        : "The one-time download could not be completed. Please try again.",
      error.code === "recent_authentication_required" ? "pending" : "error",
    );
  }
}

function updateConversationHints() {
  // First-run hints (UX review hosted item #9): shown only for a genuinely
  // empty conversation, and hidden the moment any turn -- prior or just
  // sent -- exists. No separate "have I shown this before" flag is needed:
  // the message list itself is the source of truth.
  if (!conversationHints) return;
  conversationHints.hidden = conversationMessages.children.length > 0;
}

function conversationNotificationsSupported() {
  return typeof window.Notification === "function";
}

function renderConversationNotifyControl() {
  // Web-panel reply notifications: the control disappears once permission
  // is denied (browsers never re-prompt) or Notification isn't supported,
  // and explains why via the adjacent status text instead of leaving a
  // dead button (deliverable 2's "handle denied/unsupported gracefully").
  if (!conversationNotifyToggle) return;
  if (!conversationNotificationsSupported()) {
    conversationNotifyToggle.hidden = true;
    conversationNotifyState.hidden = false;
    conversationNotifyState.textContent =
      "Reply notifications aren't supported in this browser.";
    return;
  }
  if (Notification.permission === "denied") {
    conversationNotifyToggle.hidden = true;
    conversationNotifyState.hidden = false;
    conversationNotifyState.textContent =
      "Reply notifications are blocked. Allow notifications for this site " +
      "in your browser settings to enable them.";
    return;
  }
  conversationNotifyToggle.hidden = false;
  conversationNotifyState.hidden = true;
  const active = Notification.permission === "granted" && conversationNotificationsEnabled;
  conversationNotifyToggle.setAttribute("aria-pressed", String(active));
  conversationNotifyToggle.textContent = active
    ? "Reply notifications on"
    : "Notify me when Attune replies";
}

conversationNotifyToggle?.addEventListener("click", async () => {
  if (!conversationNotificationsSupported()) return;
  if (Notification.permission === "granted") {
    conversationNotificationsEnabled = !conversationNotificationsEnabled;
    renderConversationNotifyControl();
    return;
  }
  if (Notification.permission === "denied") {
    renderConversationNotifyControl();
    return;
  }
  // Permission is requested ONLY here, on this explicit click -- never
  // automatically on page load or poll.
  try {
    const permission = await Notification.requestPermission();
    conversationNotificationsEnabled = permission === "granted";
  } catch {
    conversationNotificationsEnabled = false;
  }
  renderConversationNotifyControl();
});

for (const hint of conversationHints?.querySelectorAll(".conversation-hint") ?? []) {
  hint.addEventListener("click", () => {
    conversationInput.value = hint.dataset.hintText || hint.textContent || "";
    conversationInput.focus();
  });
}

function notifyOfConversationReply() {
  if (!conversationNotificationsEnabled || !conversationNotificationsSupported()) return;
  if (Notification.permission !== "granted") return;
  if (!document.hidden) return;
  try {
    // Content-free by construction, matching every other OS-notification
    // and audit surface in this codebase: the reply text never leaves the
    // page, even into the notification center.
    const notification = new Notification("Attune replied");
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch {
    /* Notification construction can fail in some environments; it must
       never affect the poll that triggered it. */
  }
}

function appendConversationTurns(turns) {
  for (const turn of turns) {
    const element = document.createElement("p");
    element.className = "conversation-turn";
    element.dataset.actor = turn.actor;
    element.textContent = turn.text;
    conversationMessages.appendChild(element);
    if (turn.sequence > conversationHighestSequence) {
      conversationHighestSequence = turn.sequence;
    }
  }
  if (turns.length) {
    conversationMessages.scrollTop = conversationMessages.scrollHeight;
    updateConversationHints();
  }
}

function setConversationPending(pending) {
  if (pending) {
    if (!conversationPendingSince) conversationPendingSince = Date.now();
    const elapsed = Date.now() - conversationPendingSince;
    conversationIndicator.hidden = false;
    if (elapsed > CONVERSATION_TERMINAL_MS) {
      // A genuine terminal state (UX review hosted item #10), not an error:
      // the message was durably accepted (docs/hosted-conversation.md's
      // acceptance ceremony) and will still be answered. This is honest
      // about that rather than implying anything failed.
      conversationIndicator.textContent =
        "This is taking much longer than expected — your message was " +
        "accepted and will still be answered; check back or send a follow-up.";
      conversationIndicator.dataset.state = "terminal";
    } else if (elapsed > CONVERSATION_STALL_MS) {
      conversationIndicator.textContent = "Attune is still working on this…";
      conversationIndicator.dataset.state = "stalled";
    } else {
      conversationIndicator.textContent = "Attune is working…";
      conversationIndicator.dataset.state = "working";
    }
  } else {
    conversationPendingSince = null;
    conversationIndicator.hidden = true;
    conversationIndicator.textContent = "";
    conversationIndicator.dataset.state = "";
  }
}

function stopConversationPolling() {
  if (conversationPollTimer) {
    window.clearTimeout(conversationPollTimer);
    conversationPollTimer = null;
  }
}

async function pollConversationTurns() {
  stopConversationPolling();
  let pending = false;
  try {
    const payload = await json(
      await fetch(
        `/v1/conversation/turns?after=${encodeURIComponent(conversationHighestSequence)}`,
        {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        },
      ),
    );
    const turns = payload.turns || [];
    appendConversationTurns(turns);
    if (turns.some((turn) => turn.actor === "assistant")) notifyOfConversationReply();
    pending = Boolean(payload.pending);
    conversationPollFailures = 0;
  } catch (error) {
    if (error.status === 401) {
      show("Sign out and sign in again to keep conversing with Attune.", "pending");
      return;
    }
    conversationPollFailures += 1;
    if (conversationPollFailures >= 5) {
      setConversationPending(false);
      show("Attune's replies could not be checked. Please try again shortly.", "error");
      return;
    }
    pending = true;
  }
  setConversationPending(pending);
  if (pending) {
    // Past the terminal bound, drop to slow polling: the reply is still
    // coming (the acceptance is durable), so polling never stops outright,
    // but there is no reason to keep hitting the server every two seconds
    // for a reply that is already known to be taking unusually long.
    const elapsed = conversationPendingSince ? Date.now() - conversationPendingSince : 0;
    const interval =
      elapsed > CONVERSATION_TERMINAL_MS
        ? CONVERSATION_POLL_SLOW_INTERVAL_MS
        : CONVERSATION_POLL_INTERVAL_MS;
    conversationPollTimer = window.setTimeout(pollConversationTurns, interval);
  }
}

async function startConversation() {
  conversationPanel.hidden = false;
  conversationHighestSequence = 0;
  conversationMessages.replaceChildren();
  renderConversationNotifyControl();
  await pollConversationTurns();
  updateConversationHints();
}

async function sendConversationMessage() {
  const text = conversationInput.value;
  if (!text.trim() || text.length > 8_000) {
    show("Type a message between 1 and 8000 characters.", "pending");
    return;
  }
  if (conversationSending) return;
  conversationSending = true;
  conversationSend.disabled = true;
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const accepted = await json(
      await fetch("/v1/conversation/messages", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Attune-CSRF": csrf,
        },
        body: JSON.stringify({ schema_version: 1, text }),
      }),
    );
    appendConversationTurns([
      { sequence: accepted.user_sequence, actor: "user", text },
    ]);
    conversationInput.value = "";
    setConversationPending(true);
    stopConversationPolling();
    await pollConversationTurns();
  } catch (error) {
    if (error.status === 401) {
      show("Sign out and sign in again to keep conversing with Attune.", "pending");
    } else {
      show("The message could not be sent. Please try again.", "error");
    }
  } finally {
    conversationSending = false;
    conversationSend.disabled = false;
  }
}

conversationSend.addEventListener("click", sendConversationMessage);
conversationInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendConversationMessage();
  }
});

function selectedChannels(purpose) {
  return [...channelPreferences.querySelectorAll(`[data-purpose="${purpose}"]:checked`)]
    .map((input) => input.value)
    .sort();
}

function renderChannels(channels) {
  channelPreferences.hidden = false;
  const interaction = new Set(channels.interaction_channels || []);
  const briefs = new Set(channels.brief_channels || []);
  for (const input of channelPreferences.querySelectorAll("[data-purpose]")) {
    input.checked = (input.dataset.purpose === "interaction" ? interaction : briefs).has(
      input.value,
    );
  }
  channelsSave.textContent =
    channels.status === "not_started" ? "Save channel choices" : "Update channel choices";
  channelsSave.disabled = false;
  refreshRecencyGates();
}

async function showChannels() {
  const channels = await json(
    await fetch("/v1/onboarding/channels", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }),
  );
  renderChannels(channels);
}

channelsSave.addEventListener("click", async () => {
  const interactionChannels = selectedChannels("interaction");
  const briefChannels = selectedChannels("brief");
  if (!interactionChannels.length && !briefChannels.length) {
    show("Choose at least one conversation or brief channel.", "pending");
    return;
  }
  channelsSave.disabled = true;
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/onboarding/channels", {
        method: "PUT",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Attune-CSRF": csrf,
        },
        body: JSON.stringify({
          schema_version: 1,
          interaction_channels: interactionChannels,
          brief_channels: briefChannels,
        }),
      }),
    );
    renderChannels(result.channels);
    renderOnboarding(result.onboarding);
    if (hostedChannelSetupAvailable) await showChannelInstallations();
    show("Channel choices saved. App installation and destination verification are still required.", "success");
  } catch (error) {
    channelsSave.disabled = false;
    if (error.code === "recent_authentication_required") {
      forceLapsedNow();
      show("Sign out and sign in again before changing channel choices.", "pending");
    } else {
      show("Channel choices could not be saved. Please try again.", "error");
    }
  }
});

function providerState(payload, provider) {
  return (payload.providers || []).find((item) => item.provider === provider);
}

function renderChannelInstallations(payload) {
  const googleChat = providerState(payload, "google_chat");
  const slack = providerState(payload, "slack");
  const selected = Boolean(googleChat?.selected || slack?.selected);
  channelInstallations.hidden = !selected;
  googleChatInstallation.hidden = !googleChat?.selected;
  slackInstallation.hidden = !slack?.selected;
  if (googleChat?.selected) {
    const destination = googleChat.destination_state || "not_started";
    const setup = googleChat.setup_state || "not_started";
    googleChatInstallationState.textContent =
      destination === "active"
        ? "Owner-only Google Chat destination verified and active. No new link code is needed."
        : destination === "pending_test"
          ? "Google Chat owner and direct-message destination linked; delivery test remains."
          : destination === "needs_relink"
            ? "Google Chat is linked, but its encrypted delivery route must be adopted. Generate a new link code."
          : setup === "pending"
            ? "A previous link code is pending. Generate a new code to replace it."
            : "Google Chat is selected but not linked.";
    googleChatLinkStart.hidden = destination === "active" || destination === "pending_test";
    if (destination === "needs_relink") {
      googleChatLinkStart.textContent = "Generate route-adoption link code";
    }
    googleChatDeliveryTest.hidden = destination !== "pending_test";
    googleChatDeliveryTest.disabled = false;
    googleChatDisconnect.hidden =
      !hostedChannelLifecycleAvailable ||
      !["active", "pending_test", "needs_relink"].includes(destination);
    googleChatDisconnect.disabled = false;
  }
  if (slack?.selected) {
    const destination = slack.destination_state || "not_started";
    slackInstallationState.textContent =
      destination === "active"
        ? "Owner-only Slack destination verified and active. No new installation is needed."
        : destination === "pending_test"
          ? "Slack app installed and owner-only destination linked; delivery test remains."
          : "Slack is selected but not installed.";
    slackInstallStart.hidden = destination === "active" || destination === "pending_test";
    slackInstallStart.disabled = false;
    slackDeliveryTest.hidden = destination !== "pending_test";
    slackDeliveryTest.disabled = false;
    slackDisconnect.hidden =
      !hostedChannelLifecycleAvailable || !["active", "pending_test"].includes(destination);
    slackDisconnect.disabled = false;
  }
  refreshRecencyGates();
}

async function showChannelInstallations() {
  const payload = await json(
    await fetch("/v1/onboarding/channel-installations", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }),
  );
  renderChannelInstallations(payload);
}

googleChatLinkStart.addEventListener("click", async () => {
  googleChatLinkStart.disabled = true;
  googleChatLinkInstructions.hidden = true;
  googleChatLinkCommand.textContent = "";
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/onboarding/channel-installations/google-chat/link", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-Attune-CSRF": csrf },
      }),
    );
    googleChatLinkCommand.textContent = result.link_command;
    googleChatLinkExpiry.textContent = `Expires at ${new Date(result.expires_at).toLocaleTimeString()}.`;
    googleChatLinkInstructions.hidden = false;
    googleChatInstallationState.textContent =
      "Link code created. It is shown once and does not contain provider authority.";
    googleChatLinkStart.textContent = "Generate a new link code";
    googleChatLinkStart.disabled = false;
    show("Google Chat link code ready. Send it only in an owner direct message.", "success");
  } catch (error) {
    googleChatLinkStart.disabled = false;
    if (error.code === "recent_authentication_required") {
      forceLapsedNow();
      show("Sign out and sign in again before linking Google Chat.", "pending");
    } else {
      show("Google Chat linking could not be started. Please try again.", "error");
    }
  }
});

googleChatDeliveryTest.addEventListener("click", async () => {
  googleChatDeliveryTest.disabled = true;
  show("Sending the fixed, content-free Google Chat connection test…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/onboarding/channel-installations/google-chat/test", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-Attune-CSRF": csrf },
      }),
    );
    renderChannelInstallations(result);
    const state = await json(
      await fetch("/v1/onboarding", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }),
    );
    renderOnboarding(state);
    show("Google Chat delivery verified. Only the fixed connection-test text was sent.", "success");
  } catch (error) {
    googleChatDeliveryTest.disabled = false;
    if (error.code === "recent_authentication_required") {
      forceLapsedNow();
      show("Sign out and sign in again before testing Google Chat delivery.", "pending");
    } else {
      show("Google Chat delivery could not be verified. No workspace data was sent.", "error");
    }
  }
});

googleChatDisconnect.addEventListener("click", async () => {
  if (
    !window.confirm(
      "Disconnect Google Chat? Attune will immediately stop accepting messages and sending replies to this destination. You can relink later.",
    )
  ) return;
  googleChatDisconnect.disabled = true;
  show("Disconnecting Google Chat…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/onboarding/channel-installations/google-chat", {
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
    googleChatLinkInstructions.hidden = true;
    googleChatLinkCommand.textContent = "";
    googleChatLinkStart.textContent = "Generate Google Chat link code";
    renderChannelInstallations(result);
    if (result.onboarding) renderOnboarding(result.onboarding);
    show(
      "Google Chat is disconnected. Attune no longer accepts messages or sends replies to that destination.",
      "success",
    );
  } catch (error) {
    googleChatDisconnect.disabled = false;
    if (error.code === "recent_authentication_required") {
      forceLapsedNow();
      show("Sign out and sign in again before disconnecting Google Chat.", "pending");
    } else {
      show("Google Chat could not be disconnected. Please try again.", "error");
    }
  }
});

slackInstallStart.addEventListener("click", async () => {
  slackInstallStart.disabled = true;
  show("Preparing Slack installation…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/onboarding/channel-installations/slack/install", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-Attune-CSRF": csrf },
      }),
    );
    window.location.assign(result.authorize_url);
  } catch (error) {
    slackInstallStart.disabled = false;
    if (error.code === "recent_authentication_required") {
      forceLapsedNow();
      show("Sign out and sign in again before installing Slack.", "pending");
    } else {
      show("Slack installation could not be started. Please try again.", "error");
    }
  }
});

slackDeliveryTest.addEventListener("click", async () => {
  slackDeliveryTest.disabled = true;
  show("Sending the fixed, content-free Slack connection test…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/onboarding/channel-installations/slack/test", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-Attune-CSRF": csrf },
      }),
    );
    renderChannelInstallations(result);
    const state = await json(
      await fetch("/v1/onboarding", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }),
    );
    renderOnboarding(state);
    show("Slack delivery verified. Only the fixed connection-test text was sent.", "success");
  } catch (error) {
    slackDeliveryTest.disabled = false;
    if (error.code === "recent_authentication_required") {
      forceLapsedNow();
      show("Sign out and sign in again before testing Slack delivery.", "pending");
    } else {
      show("Slack delivery could not be verified. No workspace data was sent.", "error");
    }
  }
});

slackDisconnect.addEventListener("click", async () => {
  if (
    !window.confirm(
      "Disconnect Slack? Attune will immediately stop accepting messages and sending replies to this destination. You can reinstall later.",
    )
  ) return;
  slackDisconnect.disabled = true;
  show("Disconnecting Slack…");
  try {
    const csrf = cookie("__Host-attune_csrf");
    if (!csrf) throw new Error("missing session binding");
    const result = await json(
      await fetch("/v1/onboarding/channel-installations/slack", {
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
    renderChannelInstallations(result);
    if (result.onboarding) renderOnboarding(result.onboarding);
    show(
      "Slack is disconnected. Attune no longer accepts messages or sends replies to that destination.",
      "success",
    );
  } catch (error) {
    slackDisconnect.disabled = false;
    if (error.code === "recent_authentication_required") {
      forceLapsedNow();
      show("Sign out and sign in again before disconnecting Slack.", "pending");
    } else {
      show("Slack could not be disconnected. Please try again.", "error");
    }
  }
});

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
  refreshRecencyGates();
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
      forceLapsedNow();
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
    await performSignOut();
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
    if (hostedChannelsAvailable) await showChannels();
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

function slackInstallReturnMessage(outcome) {
  if (outcome === "connected") {
    return "Slack installation completed. Send the fixed connection test to verify the destination.";
  }
  if (outcome === "failed") {
    return "The Slack installation was not completed. Try installing again.";
  }
  return null;
}

async function main() {
  const params = new URLSearchParams(window.location.search);
  const outcome = params.get("workspace");
  const slackOutcome = params.get("slack_install");
  if (outcome || slackOutcome) window.history.replaceState({}, "", window.location.pathname);
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
    await renderModelProfile();
    await renderAccountDeletion();
    const slackMessage = slackInstallReturnMessage(slackOutcome);
    if (slackMessage) slackInstallationState.textContent = slackMessage;
    resumePendingSection();
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
      markSessionStarted();
      show("Signed in to Attune.", "success");
      button.hidden = true;
      sessionSignOut.hidden = false;
      const session = await existingSession();
      if (session) {
        await showWorkspace(session);
        await showOnboarding(session);
        await renderAccountDeletion();
        resumePendingSection();
      }
    } catch (error) {
      if (error.status === 409) {
        show(
          "Google identity verified. Your Attune membership is not provisioned yet.",
          "pending",
        );
        // Offered optimistically: this page has no pre-session signal for
        // whether hosted signup is enabled, so the signup route's own
        // 404/429/other response is the availability signal (see
        // docs/hosted-signup.md section 9).
        signupButton.hidden = false;
        signupButton.disabled = false;
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

  signupButton.addEventListener("click", async () => {
    signupButton.disabled = true;
    show("Waiting for Google sign-in to create your Attune account…");
    try {
      const result = await attemptSignup(auth, bootstrap);
      signupButton.hidden = true;
      show(
        result.status === "created"
          ? "Your Attune account was created. Continue with Google to finish signing in."
          : "You already have an Attune account. Continue with Google to sign in.",
        "success",
      );
    } catch (error) {
      if (error.status === 404) {
        signupButton.hidden = true;
        show(
          "Creating an account is not available yet. Contact your operator.",
          "pending",
        );
      } else if (error.status === 429) {
        show("Too many attempts. Please wait a minute and try again.", "pending");
        signupButton.disabled = false;
      } else {
        show(safeFailure(error), "error");
        signupButton.disabled = false;
      }
    }
    try {
      bootstrap = await prepareLoginBinding();
      button.disabled = false;
    } catch {
      show("Sign-in is temporarily unavailable.", "error");
    }
  });
}

main().catch(() => {
  show("Sign-in is temporarily unavailable.", "error");
});
