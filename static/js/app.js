/* ══════════════════════════════════════════════════════════════════
   COA Reviewer Web App – Frontend
   ══════════════════════════════════════════════════════════════════ */

const STATUS_ICONS = {
    pending: "\u25CB",   // ○
    loading: "\u23F3",   // ⏳
    ready:   "\u25CF",   // ●
    good:    "\u2713",   // ✓
    bad:     "\u2717",   // ✗
    error:   "\u26A0",   // ⚠
};

// ── State ────────────────────────────────────────────────────────────
const state = {
    currentTab: "Yesterday",
    samples: {},          // { tab: [sample_dict, ...] }
    currentSample: null,
    selectedAttId: null,
    eventSource: null,
    viewMode: "split",    // "split" | "sif" | "coa"
};

// ── Session / auth state ─────────────────────────────────────────────
let currentUserName = "";
let portalLoginConfirmed = false; // true only after an actual login/reauth succeeds
let lastActivity = Date.now();
let inactivityTimer = null;
let heartbeatTimer = null;
const INACTIVITY_MS = 10 * 60 * 1000;  // 10 minutes
const SLOW_LOGIN_MS = 90 * 1000;        // 90 seconds before showing manual login option

// ── DOM refs ─────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ── Browser detection ────────────────────────────────────────────────
// Safari (proper, not Chrome/Edge/Firefox on iOS) ignores color-scheme on
// the PDF iframe — its QuickLook viewer still paints a white frame around
// the document. `html.is-safari` marks the runtime; the actual filter is
// applied only when the user-controlled `force-dark-pdf` toggle is on.
const IS_SAFARI = (function () {
    const ua = navigator.userAgent;
    const safari = /Safari/.test(ua) && !/Chrome|Chromium|CriOS|FxiOS|Edg/i.test(ua);
    if (safari) document.documentElement.classList.add("is-safari");
    return safari;
})();

// ══════════════════════════════════════════════════════════════════════
// Initialization
// ══════════════════════════════════════════════════════════════════════

document.addEventListener("DOMContentLoaded", async () => {
    // ── Pre-portal setup — must run on BOTH the logged-in and logged-out
    //    paths, because the early `return` for the login screen below
    //    skips setupAppHandlers() entirely. Anything the login screen
    //    needs (theme pips, antigravity canvas) wires up here. ────────
    initTheme();
    $$(".theme-pip").forEach(b => b.addEventListener("click", () => applyTheme(b.dataset.theme)));
    initAntigravity();
    initReviewModeModal();

    // Step 1: Check portal session (POST so Cloudflare never caches the result).
    // The try/catch here is strictly for NETWORK errors — its reload-in-3s
    // recovery exists for "server not ready yet". UI/logic failures inside
    // the try block must not fall into it, or any JS bug becomes a refresh
    // loop that looks like a crash.
    let portalData;
    try {
        const portalResp = await fetch("/api/portal-session", { method: "POST" });
        portalData = await portalResp.json();
    } catch (e) {
        showModal("boot-splash");
        $("#boot-msg").textContent = "Waiting for server to start...";
        setTimeout(() => location.reload(), 3000);
        return;
    }

    if (!portalData.logged_in) {
        showModal("portal-login-modal");
        setupPortalLoginHandlers();
        return;
    }

    // Portal logged in — proceed.
    portalLoginConfirmed = true;
    currentUserName = portalData.name;
    updateUserDisplay(portalData.name);
    startInactivityTimer();
    startHeartbeat();

    // Mode picker: blocks until the reviewer chooses Tests. Info is a soft
    // dead end (the promise never resolves until they click Back → Tests).
    // If the picker itself fails (e.g. cached old HTML), proceed anyway.
    try {
        await chooseReviewMode();
    } catch (e) {
        console.warn("[reviewMode] picker failed, proceeding without it:", e);
    }
    // Default to Tests if no mode was chosen (picker skipped, cached HTML,
    // etc.) — otherwise both #test-editor and #info-editor would be visible
    // since the data-show-mode CSS only kicks in when a mode class is set.
    if (!currentReviewMode) {
        currentReviewMode = "tests";
        applyReviewMode("tests");
    }

    await initQBenchApp();

    // Portal logout button
    $("#portal-logout-btn").addEventListener("click", handlePortalLogout);

    // Reauth (timeout overlay)
    $("#reauth-btn").addEventListener("click", handleReauth);
    $("#reauth-password").addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleReauth();
    });
});


// ══════════════════════════════════════════════════════════════════════
// Portal Login
// ══════════════════════════════════════════════════════════════════════

function setupPortalLoginHandlers() {
    $("#portal-login-btn").addEventListener("click", handlePortalLogin);
    ["portal-username", "portal-password"].forEach(id => {
        document.getElementById(id).addEventListener("keydown", (e) => {
            if (e.key === "Enter") handlePortalLogin();
        });
    });
    // Keycard is the default way in; the password form is the fallback, so
    // it only takes focus once the reviewer switches to it.
    setupCardLogin();
}

async function handlePortalLogin() {
    const btn = $("#portal-login-btn");
    btn.disabled = true;
    btn.textContent = "Signing in...";
    $("#portal-login-error").textContent = "";

    const username = $("#portal-username").value.trim();
    const password = $("#portal-password").value;

    if (!username || !password) {
        $("#portal-login-error").textContent = "Username and password are required.";
        btn.disabled = false;
        btn.textContent = "Sign In";
        return;
    }

    try {
        const resp = await fetch("/api/portal-login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
        });
        const data = await resp.json();

        if (data.ok) {
            // Identity is the LabLink account LabCore resolved, not what was
            // typed — same as the card path.
            $("#portal-password").value = "";
            await completePortalLogin(data.name);
        } else {
            $("#portal-login-error").textContent = data.labcore_down
                ? "Can't reach LabCore to check your sign-in. Try again shortly."
                : (data.error || "Sign in failed.");
        }
    } catch(e) {
        $("#portal-login-error").textContent = "Connection error: " + e.message;
    }

    btn.disabled = false;
    btn.textContent = "Sign In";
}

/**
 * Everything that happens once a session exists, regardless of how the
 * reviewer proved who they are. Shared by the password and keycard paths so
 * the two can never drift.
 */
async function completePortalLogin(name) {
    portalLoginConfirmed = true;
    hideModal("portal-login-modal");
    currentUserName = name;
    updateUserDisplay(name);
    startInactivityTimer();
    startHeartbeat();
    await chooseReviewMode();
    await initQBenchApp();
    // Set up portal logout now that app is ready
    $("#portal-logout-btn").addEventListener("click", handlePortalLogout);
    // Reauth
    $("#reauth-btn").addEventListener("click", handleReauth);
    $("#reauth-password").addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleReauth();
    });
}


// ══════════════════════════════════════════════════════════════════════
// Keycard login
// ══════════════════════════════════════════════════════════════════════

/**
 * LabLink keycard readers are keyboard wedges: they type the card's code and
 * press Enter into whatever holds focus. So the capture field keeps focus
 * while the scan view is up, and consumes Enter itself.
 */
function setupCardLogin() {
    const input = $("#portal-card-input");
    if (!input) return;

    const refocus = () => {
        if (!$("#portal-card-view").classList.contains("hidden")) input.focus();
    };

    input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();          // don't let the wedge's Enter escape
        const code = input.value.trim();
        input.value = "";            // never leave a credential sitting in the DOM
        if (code) submitCardLogin(code);
    });

    // A wedge types into whatever is focused; if the reviewer clicks away the
    // next scan would land nowhere.
    input.addEventListener("blur", () => setTimeout(refocus, 50));
    $("#portal-card-view").addEventListener("click", refocus);

    $("#portal-use-password").addEventListener("click", () => showLoginView("password"));
    $("#portal-use-card").addEventListener("click", () => showLoginView("card"));
    refocus();
}

function showLoginView(which) {
    $("#portal-card-view").classList.toggle("hidden", which !== "card");
    $("#portal-password-view").classList.toggle("hidden", which === "card");
    $("#portal-login-error").textContent = "";
    if (which === "card") {
        $("#portal-card-input").focus();
    } else {
        $("#portal-username").focus();
    }
}

async function submitCardLogin(code) {
    const status = $("#portal-card-status");
    status.textContent = "Checking card…";
    $("#portal-login-error").textContent = "";
    try {
        const resp = await fetch("/api/portal-card-login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code }),
        });
        const data = await resp.json();
        if (data.ok) {
            // No initials prompt: identity is the LabLink account.
            status.textContent = `Welcome, ${data.name}`;
            await completePortalLogin(data.name);
            return;
        }
        status.textContent = "";
        // "Card not recognised" and "couldn't reach LabCore" are different
        // problems for whoever is standing at the terminal.
        $("#portal-login-error").textContent = data.labcore_down
            ? "Can't reach LabCore to check the card. Use a password to sign in."
            : (data.error || "Card not recognised.");
    } catch (e) {
        status.textContent = "";
        $("#portal-login-error").textContent = "Connection error: " + e.message;
    }
}


async function handlePortalLogout() {
    if (!confirm(`Sign out ${currentUserName}? Your current session will end.`)) return;
    try {
        await fetch("/api/portal-logout", { method: "POST" });
    } catch(e) { /* ignore */ }
    location.reload();
}

function updateUserDisplay(name) {
    const el = $("#user-display");
    if (el) el.textContent = name ? `\u{1F464} ${name}` : "";
}


// ══════════════════════════════════════════════════════════════════════
// Review Mode Picker — runs after portal login, before the main app
// ══════════════════════════════════════════════════════════════════════
//
// Two pills: "Info" (placeholder / dead end for now) and "Tests" (the
// existing COA review screen). The picker is modal-blocking: the bootstrap
// awaits `chooseReviewMode()` and only proceeds to `initQBenchApp()` when
// the user picks Tests. Clicking Info swaps the modal contents to a
// "coming soon" state with a Back button — the promise stays unresolved,
// so the main app never initializes until the user lands on Tests.

let currentReviewMode = null;
let _reviewModeResolver = null;

function initReviewModeModal() {
    const modal = $("#review-mode-modal");
    if (!modal) return;

    modal.querySelectorAll(".review-pill").forEach(btn => {
        btn.addEventListener("click", () => {
            const mode = btn.dataset.mode;
            if (mode !== "info" && mode !== "tests") return;
            currentReviewMode = mode;
            try { localStorage.setItem("reviewMode", mode); } catch (e) { /* ignore */ }
            applyReviewMode(mode);
            hideModal("review-mode-modal");
            if (_reviewModeResolver) {
                _reviewModeResolver(mode);
                _reviewModeResolver = null;
            }
        });
    });
}

// Apply the chosen mode to <body> so CSS can branch — Info hides
// Re-review from the sidebar and surfaces the Intaked tab + sample-info
// editor, Tests shows the existing test editor + Re-review. Also flips
// to the mode's default tab if the main app is already running (so
// mid-session mode changes don't strand the user on a hidden tab).
function applyReviewMode(mode) {
    document.body.classList.toggle("mode-info",  mode === "info");
    document.body.classList.toggle("mode-tests", mode === "tests");
    if (typeof switchTab === "function" && state && state.currentTab !== undefined) {
        const next = defaultTabForMode(mode);
        if (state.currentTab !== next) switchTab(next);
    }
    // The Lab Vision pane shows different things per mode, so a mid-session
    // switch has to re-render it or it strands the other mode's contents.
    if (typeof renderLabVisionData === "function" && LV.labId) {
        if (LV.labId === state?.currentSample?.lab_id) renderLabVisionData();
        else if (state?.currentSample) loadLabVisionData(state.currentSample.lab_id);
    }
}

// Per-mode default tab. Tests opens on Due Out (the most-urgent column);
// Info opens on Intaked (yesterday's intakes — the freshest sample data).
function defaultTabForMode(mode) {
    return mode === "info" ? "Intaked" : "Due Out";
}

function chooseReviewMode() {
    // Bulletproof: if the modal element is missing — Cloudflare or the
    // browser may have cached an old index.html while serving fresh JS —
    // resolve immediately and let the app continue without the picker.
    const modal = $("#review-mode-modal");
    if (!modal) {
        console.warn("[reviewMode] modal element missing — skipping picker");
        return Promise.resolve(null);
    }
    return new Promise((resolve) => {
        _reviewModeResolver = resolve;
        const nameEl = $("#review-mode-name");
        if (nameEl) nameEl.textContent = currentUserName || "there";
        // Reset to picker state in case it was previously left on "coming soon".
        const picker = modal.querySelector(".review-mode-picker");
        const soon   = modal.querySelector(".review-mode-soon");
        if (picker) picker.classList.remove("hidden");
        if (soon)   soon.classList.add("hidden");
        showModal("review-mode-modal");
    });
}


// ══════════════════════════════════════════════════════════════════════
// QBench App Initialization (runs after portal login)
// ══════════════════════════════════════════════════════════════════════

async function initQBenchApp() {
    showModal("boot-splash");
    $("#boot-msg").textContent = "Checking QBench connection...";

    // Set up main app handlers. A wiring failure (e.g. HTML/JS skew after
    // a partial deploy) must not brick the boot — log it and keep going;
    // the affected control just stays inert. Without this, a single
    // missing element froze the app at "Checking QBench connection..."
    // (2026-07-10).
    // Command Center config (LabVision URL + availability). Fire-and-forget:
    // a LabCore that is down must not hold up the COA boot — the banner and
    // the individual calls report it.
    initCommandCenter();

    try {
        setupAppHandlers();
    } catch (e) {
        console.error("setupAppHandlers failed (continuing boot):", e);
        appendBootStatus("Warning: some controls failed to initialize — try a hard refresh (Ctrl+F5).");
    }

    try {
        const resp = await fetch("/api/config");
        if (resp.status === 401) {
            // Session not found — server may have restarted. Ask user to sign in again.
            hideModal("boot-splash");
            showModal("portal-login-modal");
            setupPortalLoginHandlers();
            $("#portal-login-error").textContent = "Session lost (application may have restarted). Please sign in again.";
            return;
        }
        const cfg = await resp.json();
        if (cfg.username) $("#login-username").value = cfg.username;
        if (cfg.has_password) $("#login-password").placeholder = "(saved)";

        if (cfg.logged_in) {
            hideModal("boot-splash");
            $("#app").classList.remove("hidden");
            connectSSE();
            if (cfg.has_data) {
                await restoreAllTabs();
            }
        } else if (cfg.has_password) {
            // Saved creds — auto-login in progress
            $("#boot-msg").textContent = "Logging in to QBench...";
            appendBootStatus("Connecting to QBench...");
            connectSSE();
            // Poll /api/config every 3 s in case the SSE auto_login_done event
            // was broadcast before this connection was established (race condition).
            const pollTimer = setInterval(async () => {
                if ($("#boot-splash").classList.contains("hidden")) {
                    clearInterval(pollTimer);
                    return;
                }
                try {
                    const pr = await fetch("/api/config");
                    if (pr.status === 401) { clearInterval(pollTimer); triggerTimeout(); return; }
                    const pd = await pr.json();
                    if (pd.logged_in) {
                        clearInterval(pollTimer);
                        hideModal("boot-splash");
                        $("#app").classList.remove("hidden");
                        if (pd.has_data) await restoreAllTabs();
                    }
                } catch(e) { /* ignore — will retry */ }
            }, 3000);
            // If still waiting after SLOW_LOGIN_MS, show manual option
            setTimeout(() => {
                if ($("#boot-splash") && !$("#boot-splash").classList.contains("hidden")) {
                    $("#boot-slow-notice").classList.remove("hidden");
                }
            }, SLOW_LOGIN_MS);
            $("#boot-show-login").addEventListener("click", () => {
                hideModal("boot-splash");
                showModal("login-modal");
            });
        } else {
            hideModal("boot-splash");
            showModal("login-modal");
        }
    } catch(e) {
        hideModal("boot-splash");
        showModal("login-modal");
        $("#login-error").textContent = "Could not connect to server: " + e.message;
    }
}

function appendBootStatus(msg) {
    const log = $("#boot-status-log");
    if (!log) return;
    const line = document.createElement("div");
    line.className = "boot-status-line";
    line.textContent = msg;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
    // Keep only last 8 lines
    while (log.children.length > 8) {
        log.removeChild(log.firstChild);
    }
}

function setupAppHandlers() {
    // Login (QBench)
    $("#login-btn").addEventListener("click", handleLogin);
    $("#login-password").addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleLogin();
    });

    // Start pulling
    $("#start-btn").addEventListener("click", handleStart);

    // Export
    $("#export-btn").addEventListener("click", showExportModal);
    $("#export-cancel").addEventListener("click", () => hideModal("export-modal"));
    $("#export-ok").addEventListener("click", handleExport);

    // Good Samples
    $("#good-links-btn").addEventListener("click", showGoodModal);
    $("#good-modal-cancel").addEventListener("click", () => hideModal("good-modal"));
    $("#good-modal-open").addEventListener("click", handleOpenGoodLinks);

    // Theme: pips + initTheme run in the DOMContentLoaded preamble (the
    // login screen needs them before this function is reached). Only the
    // top-bar #dark-toggle, which exists only in the logged-in UI, lives
    // here.
    $("#dark-toggle").addEventListener("click", cycleTheme);

    // Force-dark-PDF: always on. The CSS rule is gated by `body.dark` so it
    // only takes effect in dark mode anyway. Safari's QuickLook PDF viewer
    // ignores `color-scheme: dark`, so this filter is the only way to stop
    // the white frame around the PDF page in Safari + dark mode.
    document.documentElement.classList.add("force-dark-pdf");

    // Server restart
    $("#restart-btn").addEventListener("click", handleRestartServer);
    initRestartConfirm();
    initFieldSettings();

    // Change-mode button — re-opens the picker mid-session. The picker's
    // own click handler calls applyReviewMode, which (now that state is
    // initialized) will also swap to the new mode's default tab.
    // Null-safe: if the element is missing (cached old HTML, partial
    // deploy) the rest of setupAppHandlers must still run, otherwise
    // initQBenchApp catches the error and looks like a QBench failure.
    $("#change-mode-btn")?.addEventListener("click", () => {
        chooseReviewMode();
    });

    // View toggle
    setupPaneToggles();

    // Tab buttons
    $$(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    // Search
    $("#search-btn").addEventListener("click", handleSearch);
    $("#search-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleSearch();
    });

    // Custom day
    $("#custom-date").value = new Date().toISOString().split("T")[0];
    $("#custom-load-btn").addEventListener("click", handleCustomLoad);

    // Good / Bad
    $("#good-btn").addEventListener("click", markGood);
    $("#bad-btn").addEventListener("click", markBad);

    // Un-mark
    $("#uncheck-btn")?.addEventListener("click", handleUncheck);

    // Regenerate every unmarked sample on the current tab
    $("#regen-pending-btn")?.addEventListener("click", handleRegeneratePending);

    // Command Center — flag modal
    $("#cc-cancel")?.addEventListener("click", () => hideModal("cc-modal"));
    $("#cc-submit")?.addEventListener("click", handleCcSubmit);
    $("#cc-sample-input")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); addCcSample(); }
    });
    $("#cc-problem")?.addEventListener("keydown", (e) => {
        // Ctrl/Cmd+Enter submits; plain Enter is a newline in a description.
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            handleCcSubmit();
        }
    });

    // Command Center — conflict
    $("#cc-conflict-cancel")?.addEventListener("click", handleCcConflictCancel);
    $("#cc-conflict-create")?.addEventListener("click", handleCcConflictCreate);

    // Command Center — resolve an open listing (complete / continue / back out)
    $("#cc-resolve-back")?.addEventListener("click", () => finishResolve(false));
    $("#cc-resolve-continue")?.addEventListener("click", () => finishResolve(true));
    $("#cc-resolve-complete")?.addEventListener("click", handleResolveComplete);

    // Bottom bar buttons
    $("#download-btn").addEventListener("click", () => {
        if (state.currentSample && state.currentSample.has_preview) {
            const a = document.createElement("a");
            a.href = `/api/pdf/${encodeURIComponent(state.currentSample.lab_id)}/download`;
            a.download = `COA_${state.currentSample.lab_id}.pdf`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        }
    });
    $("#open-browser-btn").addEventListener("click", () => {
        if (state.currentSample && state.currentSample.has_preview) {
            window.open(`/api/pdf/${state.currentSample.lab_id}#navpanes=0`, "_blank");
        }
    });
    // QBench's sample detail page uses a query param — /sample/<id> 404s
    // (URL format verified against the live UI, 2026-07-10). Null-safe
    // wiring: a cached older index.html may not have this button yet.
    $("#open-qbench-btn")?.addEventListener("click", () => {
        if (state.currentSample && state.currentSample.sample_id) {
            window.open(
                `https://asaplabs.qbench.net/sample?id=${encodeURIComponent(state.currentSample.sample_id)}`,
                "_blank",
            );
        }
    });
    $("#regen-btn").addEventListener("click", handleRegenerate);
    $("#open-labvision-btn")?.addEventListener("click", openInLabVision);
    $("#sync-data-btn")?.addEventListener("click", openSyncData);
    $("#sync-cancel")?.addEventListener("click", () => hideModal("sync-modal"));
    $("#sync-apply")?.addEventListener("click", applySyncData);

    // Drag multi-select + right-click bulk actions on the sample list.
    setupSampleSelection();

    // Attachment
    $("#att-refresh-btn").addEventListener("click", () => {
        if (state.currentSample) loadAttachments(state.currentSample.lab_id);
    });
    $("#att-delete-btn").addEventListener("click", handleDeleteAttachment);

    // Comments
    $("#comment-edit-btn").addEventListener("click", enterCommentEditMode);
    $("#comment-save-btn").addEventListener("click", saveComments);
    $("#comment-cancel-btn").addEventListener("click", exitCommentEditMode);

    // Keyboard shortcuts
    document.addEventListener("keydown", handleKeyboard);
}


// ══════════════════════════════════════════════════════════════════════
// Inactivity Timer & Heartbeat
// ══════════════════════════════════════════════════════════════════════

function resetActivity() {
    lastActivity = Date.now();
}

function startInactivityTimer() {
    lastActivity = Date.now(); // reset so stale page-load time doesn't fire immediately
    if (inactivityTimer) clearInterval(inactivityTimer);
    document.addEventListener("mousemove", resetActivity, { passive: true });
    document.addEventListener("click", resetActivity, { passive: true });
    document.addEventListener("keydown", resetActivity, { passive: true });
    document.addEventListener("scroll", resetActivity, { passive: true, capture: true });

    inactivityTimer = setInterval(() => {
        if (Date.now() - lastActivity > INACTIVITY_MS) {
            triggerTimeout();
        }
    }, 30000);  // check every 30 seconds
}

function triggerTimeout() {
    clearInterval(inactivityTimer);
    clearInterval(heartbeatTimer);
    inactivityTimer = null;
    heartbeatTimer = null;

    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }

    if (!portalLoginConfirmed) {
        // User was never truly authenticated (e.g. proxy served a cached session
        // check). Send them to the portal login modal instead of the timeout overlay.
        portalLoginConfirmed = false;
        currentUserName = "";
        hideModal("boot-splash");
        hideModal("login-modal");
        showModal("portal-login-modal");
        setupPortalLoginHandlers();
        $("#portal-login-error").textContent = "Please sign in to continue.";
        return;
    }

    // User was logged in — show the inactivity timeout overlay
    $("#timeout-user-display").textContent = currentUserName;
    showModal("timeout-overlay");
    setupReauthCard();
    refocusReauthCard();
}

async function handleReauth() {
    const password = $("#reauth-password").value;
    if (!password) {
        $("#reauth-error").textContent = "Password is required.";
        return;
    }
    await submitReauth({ username: currentUserName, password });
}

/**
 * The timeout overlay accepts the same two proofs as the login screen. Both
 * land here so restoring a session behaves identically whichever was used.
 * The server checks the resolved LabLink account against the one that owns
 * the session — a colleague tapping in gets 403, not the review in progress.
 */
async function submitReauth(body) {
    const btn = $("#reauth-btn");
    btn.disabled = true;
    btn.textContent = "Logging in...";
    $("#reauth-error").textContent = "";

    try {
        const resp = await fetch("/api/portal-reauth", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await resp.json();

        if (data.ok) {
            portalLoginConfirmed = true;
            currentUserName = data.name;
            updateUserDisplay(data.name);
            hideModal("timeout-overlay");
            $("#reauth-password").value = "";
            $("#reauth-error").textContent = "";

            // Restart timers and reconnect SSE
            lastActivity = Date.now();
            startInactivityTimer();
            startHeartbeat();
            connectSSE();

            setStatus(data.restored
                ? "Welcome back, " + data.name + "!"
                : "Logged back in. You may need to restart pulling.");
        } else {
            $("#reauth-error").textContent = data.labcore_down
                ? "Can't reach LabCore to check your sign-in. Try again shortly."
                : (data.error || "Authentication failed.");
        }
    } catch(e) {
        $("#reauth-error").textContent = "Connection error: " + e.message;
    }

    btn.disabled = false;
    btn.textContent = "Log Back In";
    refocusReauthCard();
}

/**
 * A wedge reader types into whatever holds focus, so the overlay's capture
 * field takes it back whenever the reviewer isn't typing a password.
 */
function refocusReauthCard() {
    const input = $("#reauth-card-input");
    if (!input) return;
    if (!$("#timeout-overlay").classList.contains("hidden") &&
        document.activeElement !== $("#reauth-password")) {
        input.focus();
    }
}

function setupReauthCard() {
    const input = $("#reauth-card-input");
    if (!input || input.dataset.wired) return;
    input.dataset.wired = "1";
    input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();          // don't let the wedge's Enter escape
        const code = input.value.trim();
        input.value = "";            // never leave a credential in the DOM
        if (code) submitReauth({ code });
    });
    input.addEventListener("blur", () => setTimeout(refocusReauthCard, 50));
}

function startHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(async () => {
        try {
            const resp = await fetch("/api/heartbeat", { method: "POST" });
            if (resp.status === 401) {
                triggerTimeout();
            }
        } catch(e) { /* network error — ignore, will retry */ }
    }, 60000);  // every 60 seconds
}


// ══════════════════════════════════════════════════════════════════════
// QBench Login (manual)
// ══════════════════════════════════════════════════════════════════════

async function handleLogin() {
    const btn = $("#login-btn");
    btn.disabled = true;
    btn.textContent = "Logging in...";
    $("#login-error").textContent = "";

    const username = $("#login-username").value.trim();
    const password = $("#login-password").value.trim();
    const save = $("#login-save").checked;

    if (!username || !password) {
        $("#login-error").textContent = "Username and password are required.";
        btn.disabled = false;
        btn.textContent = "Login & Start";
        return;
    }

    try {
        const resp = await fetch("/api/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password, save }),
        });
        const data = await resp.json();
        if (resp.status === 401 && data.portal_auth === false) {
            triggerTimeout();
            return;
        }
        if (data.ok) {
            hideModal("login-modal");
            $("#app").classList.remove("hidden");
            connectSSE();
            handleStart();
        } else {
            $("#login-error").textContent = data.error || "Login failed.";
        }
    } catch(e) {
        $("#login-error").textContent = "Connection error: " + e.message;
    }

    btn.disabled = false;
    btn.textContent = "Login & Start";
}


// ══════════════════════════════════════════════════════════════════════
// SSE – Real-time updates
// ══════════════════════════════════════════════════════════════════════

function connectSSE() {
    if (state.eventSource) state.eventSource.close();
    state.eventSource = new EventSource("/api/events");

    state.eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleSSE(data);
        } catch(e) { /* ignore parse errors */ }
    };

    state.eventSource.onerror = () => {
        // Auto-reconnect is built into EventSource
    };
}

function handleSSE(data) {
    switch(data.type) {
        case "status":
            setStatus(data.message);
            // Also show in boot splash if it's visible
            if ($("#boot-splash") && !$("#boot-splash").classList.contains("hidden")) {
                appendBootStatus(data.message);
            }
            break;
        case "tab_loaded":
            loadTab(data.tab);
            break;
        case "sample_status":
            updateSampleStatus(data.tab, data.lab_id, data.status);
            break;
        case "sif_status":
            updateSifStatus(data.tab, data.lab_id, data.status, data.sif_page, data.sif_total_pages);
            break;
        case "comment_saved":
            delete _pendingComments[data.lab_id];
            setStatus(`Comments saved for ${data.lab_id}`);
            break;
        case "comment_failed":
            setStatus(`Failed to save comments for ${data.lab_id}: ${data.error || "QBench error"} — not saved.`);
            // Revert the optimistic edit so the reviewer doesn't believe a
            // lost comment was written.
            if (data.lab_id in _pendingComments) {
                const prior = _pendingComments[data.lab_id];
                delete _pendingComments[data.lab_id];
                if (state.currentSample && state.currentSample.lab_id === data.lab_id) {
                    _commentsRaw = prior;
                    $("#comments-display").textContent = prior || "(no comments)";
                }
            }
            break;
        case "auto_login_done":
            hideModal("boot-splash");
            if (data.ok) {
                $("#app").classList.remove("hidden");
            } else {
                showModal("login-modal");
                $("#login-error").textContent = data.error || "Auto-login failed. Please enter credentials.";
            }
            break;
    }
}


// ══════════════════════════════════════════════════════════════════════
// Start / Tabs
// ══════════════════════════════════════════════════════════════════════

async function handleStart() {
    $("#start-btn").disabled = true;
    setStatus("Pulling from QBench...");

    try {
        // Mode controls which third tab the server fetches: Re-review
        // (tests mode) vs Intaked (info mode). Server defaults to tests
        // if mode is missing/invalid.
        const resp = await fetch("/api/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: currentReviewMode || "tests" }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        if (!data.ok) {
            setStatus("Start failed: " + (data.error || "unknown error"));
        }
    } catch(e) {
        setStatus("Start failed: " + e.message);
    }

    setTimeout(() => { $("#start-btn").disabled = false; }, 3000);
}

// ══════════════════════════════════════════════════════════════════════
// Sample multi-select (drag / shift / ctrl) + right-click actions
// ══════════════════════════════════════════════════════════════════════

const SEL = {
    ids: new Set(),   // lab_ids selected on the CURRENT tab
    anchor: null,     // index the drag or shift-range started from
    dragging: false,
};

function currentTabSamples() {
    return state.samples[state.currentTab] || [];
}

function clearSampleSelection() {
    SEL.ids.clear();
    SEL.anchor = null;
    paintSelection();
    hideContextMenu();
}

function paintSelection() {
    $$("#sample-list .sample-item").forEach(el => {
        el.classList.toggle("selected", SEL.ids.has(el.dataset.lab));
    });
    const n = SEL.ids.size;
    const label = $("#sample-count");
    if (label) {
        const total = currentTabSamples().length;
        label.textContent = n
            ? `${n} of ${total} selected`
            : `${total} sample${total !== 1 ? "s" : ""}`;
    }
}

function selectRange(fromIdx, toIdx) {
    const samples = currentTabSamples();
    if (fromIdx == null) return;
    const [lo, hi] = fromIdx <= toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
    SEL.ids.clear();
    for (let i = lo; i <= hi && i < samples.length; i++) SEL.ids.add(samples[i].lab_id);
    paintSelection();
}

function extendSelection(idx, e) {
    const samples = currentTabSamples();
    const s = samples[idx];
    if (!s) return;
    if (e.shiftKey && SEL.anchor != null) {
        selectRange(SEL.anchor, idx);
        return;
    }
    // Ctrl/Cmd-click toggles one row and becomes the new anchor.
    if (SEL.ids.has(s.lab_id)) SEL.ids.delete(s.lab_id);
    else SEL.ids.add(s.lab_id);
    SEL.anchor = idx;
    paintSelection();
}

function setupSampleSelection() {
    const list = $("#sample-list");
    if (!list) return;

    // Finish a drag anywhere — releasing outside the list must not leave the
    // selection stuck in dragging mode.
    document.addEventListener("mouseup", () => { SEL.dragging = false; });

    list.addEventListener("contextmenu", (e) => {
        const item = e.target.closest(".sample-item");
        if (!item) return;
        e.preventDefault();          // our menu, not the browser's
        // Right-clicking outside the selection acts on that row instead —
        // otherwise the menu would silently target something else.
        if (!SEL.ids.has(item.dataset.lab)) {
            SEL.ids.clear();
            SEL.ids.add(item.dataset.lab);
            SEL.anchor = parseInt(item.dataset.idx, 10);
            paintSelection();
        }
        showContextMenu(e.clientX, e.clientY);
    });

    document.addEventListener("click", (e) => {
        if (!e.target.closest("#sample-context-menu")) hideContextMenu();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { hideContextMenu(); clearSampleSelection(); }
    });

    $("#ctx-regenerate")?.addEventListener("click", regenerateSelected);
    $("#ctx-group-cc")?.addEventListener("click", groupSelectedIntoListing);
    $("#ctx-clear")?.addEventListener("click", clearSampleSelection);
}

function showContextMenu(x, y) {
    const menu = $("#sample-context-menu");
    if (!menu) return;
    const n = SEL.ids.size;
    $("#ctx-regenerate").textContent =
        `Regenerate ${n} selected sample${n !== 1 ? "s" : ""}`;
    $("#ctx-group-cc").textContent =
        `Group ${n} into one Command Center listing…`;
    menu.classList.remove("hidden");
    // Keep it on screen when right-clicking near an edge.
    const r = menu.getBoundingClientRect();
    menu.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
    menu.style.top = Math.min(y, window.innerHeight - r.height - 8) + "px";
}

function hideContextMenu() {
    $("#sample-context-menu")?.classList.add("hidden");
}

async function regenerateSelected() {
    const labIds = [...SEL.ids];
    hideContextMenu();
    if (!labIds.length) return;

    labIds.forEach(id => { _pdfVersion[id] = (_pdfVersion[id] || 0) + 1; });
    try {
        const resp = await fetch("/api/regenerate-selected", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tab: state.currentTab, lab_ids: labIds }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        setStatus(`Regenerating ${data.count} selected sample(s)…`);
    } catch (e) {
        setStatus("Regenerate selected failed: " + e.message);
    }
}

/**
 * File every selected sample under ONE Command Center listing.
 *
 * Reuses the existing flag modal rather than adding a second listing form:
 * it already takes a list of sample chips, so a group is just that list
 * pre-filled. One form to keep in step with LabCore's enums, not two.
 */
async function groupSelectedIntoListing() {
    const labIds = [...SEL.ids];
    hideContextMenu();
    if (!labIds.length) return;

    $("#cc-title").textContent =
        `Flag ${labIds.length} Samples — Command Center`;
    $("#cc-problem").value = "";
    $("#cc-context").value = "";
    $("#cc-customer").value = "";
    $("#cc-type").value = "double_check";
    $("#cc-status").value = "open";
    $("#cc-department").value = "";
    $("#cc-error").textContent = "";
    CC.samples = labIds.map(id => ({ lab_id: id }));
    CC.pendingDraft = null;
    CC.groupMode = true;      // marking applies to every sample, not just one
    renderCcSamples();
    showModal("cc-modal");
    $("#cc-problem").focus();
    loadCcCustomers();

    // Autofill the customer from the first sample — a grouped listing is
    // usually one customer's batch, and it stays editable.
    try {
        const resp = await fetch(`/api/cc/lookup/${encodeURIComponent(labIds[0])}`);
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.customer_name && !$("#cc-customer").value) {
            $("#cc-customer").value = data.customer_name;
        }
    } catch (e) { /* autofill is optional */ }
}


function switchTab(tabName) {
    // lab_ids only mean something together with their tab, so a selection
    // must never survive a tab change.
    clearSampleSelection();
    state.currentTab = tabName;
    $$(".tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === tabName);
    });

    $("#search-bar").classList.toggle("hidden", tabName !== "Search");
    $("#custom-day-bar").classList.toggle("hidden", tabName !== "Custom Day");

    if (tabName === "Search") {
        $("#search-input").focus();
    }

    renderSampleList();
}

async function loadTab(tabName) {
    try {
        const resp = await fetch(`/api/tabs/${encodeURIComponent(tabName)}`);
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        state.samples[tabName] = data.samples || [];

        // Tab labels stay clean — the count for the active tab is shown in
        // the .sample-count line below the tab row.

        if (state.currentTab === tabName) {
            renderSampleList();
        }
    } catch(e) { /* ignore */ }
}

function updateSampleStatus(tab, labId, status) {
    const samples = state.samples[tab];
    if (!samples) return;
    const sample = samples.find(s => s.lab_id === labId);
    if (sample) {
        sample.status = status;
        sample.has_preview = (status === "ready" || status === "good" || status === "bad");
    }

    const item = document.querySelector(`.sample-item[data-lab="${labId}"][data-tab="${tab}"]`);
    if (item) {
        item.dataset.status = status;
        item.querySelector(".status-icon").textContent = STATUS_ICONS[status] || "\u25CB";
    }

    // Tab-level buttons track every sample on the tab, selected or not.
    updateTabActionButtons();

    if (state.currentSample && state.currentSample.lab_id === labId && state.currentSample.tab === tab) {
        state.currentSample.status = status;
        state.currentSample.has_preview = (status === "ready" || status === "good" || status === "bad");
        updateActionButtons();

        // Only (re)load when this lab's PDF isn't already on screen.
        // Un-marking returns a sample to `ready`, which re-enters this path —
        // without the guard it would reload the very iframe the reviewer is
        // looking at. showPDFPlaceholder() clears _currentPdfLab, so a real
        // regenerate still reloads.
        if (status === "ready" && _currentPdfLab !== labId) {
            loadPDF(labId);
            loadTests(labId);
        }
    }

    const allDone = Object.values(state.samples).flat()
        .every(s => s.status !== "pending" && s.status !== "loading");
    if (allDone) {
        $("#start-btn").disabled = false;
        $("#export-btn").disabled = false;
        $("#good-links-btn").disabled = false;
    }
}


// ══════════════════════════════════════════════════════════════════════
// Sample List
// ══════════════════════════════════════════════════════════════════════

function renderSampleList() {
    const list = $("#sample-list");
    const samples = state.samples[state.currentTab] || [];

    // Tab-level actions (Regenerate Pending) must be live as soon as the tab
    // renders — not only once a sample has been clicked.
    updateTabActionButtons();

    list.innerHTML = "";
    // Stagger each row's entrance animation. Cap at 24 rows so a 200-sample
    // list doesn't take 4 seconds to settle \u2014 beyond that, batch reveal.
    const STAGGER_MS = 14;
    const STAGGER_CAP = 24;
    samples.forEach((s, idx) => {
        const div = document.createElement("div");
        div.className = "sample-item";
        div.dataset.lab = s.lab_id;
        div.dataset.tab = s.tab;
        div.dataset.status = s.status;
        div.dataset.idx = idx;
        if (idx < STAGGER_CAP) {
            div.style.animationDelay = (idx * STAGGER_MS) + "ms";
        }
        div.innerHTML = `<span class="status-icon">${STATUS_ICONS[s.status] || "\u25CB"}</span><span>${s.lab_id}</span>`;
        // The list is rebuilt on every status event while previews render, so
        // the selection has to be re-applied rather than living in the DOM.
        if (SEL.ids.has(s.lab_id)) div.classList.add("selected");
        div.addEventListener("click", (e) => {
            // A plain click is still "open this sample"; modifiers extend the
            // selection instead, matching every file list people already use.
            if (e.shiftKey || e.ctrlKey || e.metaKey) {
                e.preventDefault();
                extendSelection(idx, e);
                return;
            }
            clearSampleSelection();
            selectSample(s);
        });
        div.addEventListener("mousedown", (e) => {
            if (e.button !== 0 || e.shiftKey || e.ctrlKey || e.metaKey) return;
            SEL.dragging = true;
            SEL.anchor = idx;
        });
        div.addEventListener("mouseenter", () => {
            // Only a real drag paints a range; a stray hover must not select.
            if (SEL.dragging) selectRange(SEL.anchor, idx);
        });
        list.appendChild(div);
    });

    const count = samples.length;
    $("#sample-count").textContent = `${count} sample${count !== 1 ? "s" : ""}`;
    // Re-render wipes the count text; restore the "N of M selected" form.
    if (SEL.ids.size) paintSelection();

    if (samples.length > 0) {
        if (!state.currentSample || state.currentSample.tab !== state.currentTab) {
            selectSample(samples[0]);
        } else {
            highlightSample(state.currentSample.lab_id);
        }
    }
}

function highlightSample(labId) {
    $$(".sample-item").forEach(el => {
        el.classList.toggle("selected", el.dataset.lab === labId);
    });
}

function selectSample(sample) {
    state.currentSample = sample;
    highlightSample(sample.lab_id);
    updateActionButtons();

    $("#sample-info").textContent = `Lab ID: ${sample.lab_id}  |  Tab: ${sample.tab}  |  Status: ${sample.status}`;

    const rrInfo = $("#rr-info");
    if (sample.tab === "Re-review" && sample.cc_task) {
        const t = sample.cc_task;
        const lines = [`Command Center CC #${t.id} — ${t.status || "open"}`];
        if (t.initial_problem) lines.push(`Problem: ${t.initial_problem}`);
        if (t.customer) lines.push(`Customer: ${t.customer}`);
        if (t.department) lines.push(`Department: ${t.department}`);
        if (t.created_by) lines.push(`Raised by: ${t.created_by}`);
        if (t.source_program) lines.push(`Via: ${t.source_program}`);
        if (t.latest_update) {
            lines.push(`Latest: ${t.latest_update}` +
                       (t.latest_update_by ? ` (${t.latest_update_by})` : ""));
        }
        if (t.date_created) lines.push(`Opened: ${t.date_created}`);
        rrInfo.textContent = lines.join("\n");
        rrInfo.classList.remove("hidden");
    } else {
        rrInfo.classList.add("hidden");
    }

    if (sample.has_preview) {
        loadPDF(sample.lab_id);
    } else if (sample.status === "loading") {
        showPDFPlaceholder(`Generating COA preview for ${sample.lab_id}...\nThis may take 10-30 seconds.`);
    } else {
        showPDFPlaceholder("Preview not available. Use Regenerate to retry.");
    }

    loadTests(sample.lab_id);
    if (panesVisible.labvision) loadLabVisionData(sample.lab_id);
    loadAttachments(sample.lab_id);
    loadComments(sample.lab_id);

    // Info mode: the right panel is the sample-info editor instead of the
    // test editor. Load whenever a sample is selected — server is the
    // source of truth, no client-side caching here yet.
    if (currentReviewMode === "info") {
        loadSampleInfo(sample.lab_id);
    }
}

function updateActionButtons() {
    const s = state.currentSample;
    const reviewable = s && ["ready", "good", "bad", "error"].includes(s.status);
    $("#good-btn").disabled = !reviewable;
    $("#bad-btn").disabled = !reviewable;
    $("#regen-btn").disabled = !s || !["ready", "error", "good", "bad"].includes(s.status);
    $("#open-browser-btn").disabled = !s || !s.has_preview;
    $("#download-btn").disabled = !s || !s.has_preview;
    // QBench link needs only the sample id — works even while the
    // preview is still rendering or failed. Guarded: the button may be
    // absent when a cached older index.html is being served.
    const qbBtn = $("#open-qbench-btn");
    if (qbBtn) qbBtn.disabled = !s || !s.sample_id;

    // Lab Vision keys off lab_id alone, so it works even while the preview
    // is rendering or has failed. Needs the base URL from /api/cc/config.
    const lvBtn = $("#open-labvision-btn");
    if (lvBtn) lvBtn.disabled = !s || !CC.labVisionUrl;

    // Sync needs a lab_id and a QBench sample; CSS hides it outside Info mode.
    const syncBtn = $("#sync-data-btn");
    if (syncBtn) syncBtn.disabled = !s || !s.sample_id;

    // Un-mark only means something for a sample that carries a mark.
    const unBtn = $("#uncheck-btn");
    if (unBtn) unBtn.disabled = !s || !["good", "bad"].includes(s.status);

    updateTabActionButtons();
}


/**
 * Button state that depends on the TAB, not on the selected sample.
 *
 * Kept separate because it must run when no sample is selected. Folding this
 * into updateActionButtons() alone meant it only ever recomputed from
 * selectSample(), so on a freshly loaded tab — before the reviewer clicks
 * anything — Regenerate Pending sat greyed out despite a tab full of pending
 * samples.
 */
function updateTabActionButtons() {
    const rpBtn = $("#regen-pending-btn");
    if (!rpBtn) return;
    const pending = (state.samples[state.currentTab] || [])
        .filter(x => !["good", "bad"].includes(x.status));
    rpBtn.disabled = pending.length === 0;
}


// ══════════════════════════════════════════════════════════════════════
// PDF Viewer
// ══════════════════════════════════════════════════════════════════════

let _currentPdfLab = null;
let _pdfVersion = {};

// ── Pane visibility ───────────────────────────────────────────────────
//
// Any combination of COA / SIF / Lab Vision. Width rule: COA takes half
// whenever anything else is shown, and the remaining half is split evenly
// between the others — so all three gives 1/2, 1/4, 1/4, and COA + Lab
// Vision gives 1/2, 1/2.

const PANES = [
    { key: "coa",        pane: "coa-pane",        toggle: "view-coa" },
    { key: "sif",        pane: "sif-pane",        toggle: "view-sif" },
    { key: "labvision",  pane: "labvision-pane",  toggle: "view-labvision" },
];

function loadPaneVisibility() {
    try {
        const saved = JSON.parse(localStorage.getItem("panesVisible") || "null");
        if (saved && typeof saved === "object") return saved;
    } catch (e) { /* fall through to the default */ }
    return { coa: true, sif: true, labvision: false };
}

let panesVisible = loadPaneVisibility();

function savePaneVisibility() {
    try { localStorage.setItem("panesVisible", JSON.stringify(panesVisible)); }
    catch (e) { /* a full or blocked localStorage must not break the view */ }
}

function setPaneVisible(key, on) {
    // Never let the reviewer end up with an empty viewer and no obvious way
    // back — the last visible pane stays put.
    if (!on && PANES.filter(p => panesVisible[p.key]).length <= 1) {
        const cb = document.getElementById(PANES.find(p => p.key === key).toggle);
        if (cb) cb.checked = true;
        return;
    }
    panesVisible[key] = !!on;
    savePaneVisibility();
    applyPaneLayout();
    if (key === "labvision" && on && state.currentSample) {
        loadLabVisionData(state.currentSample.lab_id);
    }
}

function applyPaneLayout() {
    const coaShown = !!panesVisible.coa;
    const sideKeys = ["sif", "labvision"].filter(k => panesVisible[k]);
    const sideShown = sideKeys.length > 0;

    // Left half: COA. Right half: the stacked SIF / Lab Vision column.
    const coaEl = document.getElementById("coa-pane");
    const sideEl = document.getElementById("side-panes");
    const coaPct = (coaShown && sideShown) ? 50 : 100;

    if (coaEl) {
        coaEl.style.display = coaShown ? "flex" : "none";
        coaEl.style.flex = `1 1 ${coaPct}%`;
        coaEl.style.maxWidth = `${coaPct}%`;
    }
    if (sideEl) {
        sideEl.style.display = sideShown ? "flex" : "none";
        sideEl.style.flex = `1 1 ${coaShown ? 50 : 100}%`;
        sideEl.style.maxWidth = `${coaShown ? 50 : 100}%`;
    }

    // Inside the column the visible panes split the height evenly, so SIF and
    // Lab Vision together are a quarter of the area each next to a half-width
    // COA — top half and bottom half of the right-hand side.
    ["sif", "labvision"].forEach(key => {
        const el = document.getElementById(key === "sif" ? "sif-pane" : "labvision-pane");
        if (!el) return;
        if (!panesVisible[key]) { el.style.display = "none"; return; }
        el.style.display = "flex";
        const pct = 100 / sideKeys.length;
        el.style.flex = `1 1 ${pct}%`;
        el.style.maxWidth = "100%";      // full width of the column
        el.style.maxHeight = `${pct}%`;
    });

    PANES.forEach(p => {
        const cb = document.getElementById(p.toggle);
        if (cb) cb.checked = !!panesVisible[p.key];
    });

    // Legacy callers still read state.viewMode to decide whether to show a
    // pane at all; keep it meaningful.
    const shown = PANES.filter(p => panesVisible[p.key]);
    state.viewMode = shown.length === 1 ? shown[0].key : "split";
}

function setupPaneToggles() {
    PANES.forEach(p => {
        const cb = document.getElementById(p.toggle);
        if (cb) cb.addEventListener("change", () => setPaneVisible(p.key, cb.checked));
    });
    applyPaneLayout();
}


// ── Lab Vision data pane ──────────────────────────────────────────────

// Last payload for the pane, kept so a mode switch can re-render without
// re-fetching. Both halves of it come from the one /api/sync-preview call.
const LV = { labId: null, pairs: [], tests: [] };

async function loadLabVisionData(labId) {
    const box = $("#labvision-content");
    if (!box || !panesVisible.labvision) return;
    box.innerHTML = `<div class="labvision-empty">Loading ${escapeHtml(labId)}…</div>`;
    try {
        const resp = await fetch(`/api/sync-preview/${encodeURIComponent(labId)}`);
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        if (data.labcore_down) {
            box.innerHTML = `<div class="labvision-empty">Lab Vision unreachable.</div>`;
            return;
        }
        if (data.error) {
            box.innerHTML = `<div class="labvision-empty">${escapeHtml(data.error)}</div>`;
            return;
        }
        LV.labId = labId;
        LV.pairs = data.pairs || [];
        LV.tests = data.tests || [];
        renderLabVisionData();
    } catch (e) {
        box.innerHTML = `<div class="labvision-empty">Failed to load: ${escapeHtml(e.message)}</div>`;
    }
}

// Tests mode is a results review, so the pane lists Lab Vision's tests;
// Info mode is the one that syncs sample information, so it keeps the field
// list. The label always names which, so the pane is never ambiguous.
function renderLabVisionData() {
    const box = $("#labvision-content");
    if (!box) return;
    const testsMode = document.body.classList.contains("mode-tests");
    const label = $("#labvision-label");
    if (label) label.textContent = testsMode ? "Lab Vision — Tests"
                                             : "Lab Vision — Sample Info";
    if (testsMode) renderLabVisionTests(box, LV.tests);
    else           renderLabVisionInfo(box, LV.pairs);
}

function renderLabVisionTests(box, tests) {
    if (!tests.length) {
        box.innerHTML = `<div class="labvision-empty">No Lab Vision tests for this sample.</div>`;
        return;
    }
    // A test with no result renders an em dash, never an empty cell: a blank
    // reads as a rendering fault, and a missing result is exactly the thing
    // the reviewer is here to catch.
    box.innerHTML = tests.map(t => `
        <div class="lv-test-row${t.result ? "" : " lv-test-row--noresult"}">
            <div class="lv-test-name">${escapeHtml(t.test)}</div>
            <div class="lv-test-result">${t.result ? escapeHtml(t.result) : "—"}</div>
            <div class="lv-test-op">${escapeHtml(t.operator || "")}</div>
        </div>`).join("");
}

function renderLabVisionInfo(box, pairs) {
    if (!pairs.length) {
        box.innerHTML = `<div class="labvision-empty">No Lab Vision data for this sample.</div>`;
        return;
    }
    box.innerHTML = pairs.map(p => `
        <div class="lv-row${p.clash ? " lv-row--clash" : ""}">
            <div class="lv-key">${escapeHtml(p.source)}</div>
            <div class="lv-val">${escapeHtml(p.value)}</div>
            ${p.clash ? `<div class="lv-note">QBench: ${escapeHtml(p.current)}</div>` : ""}
        </div>`).join("");
}

// Safari's iframe PDF viewer (QuickLook) latches onto whatever PDF is currently
// rendered. Setting `.src` to a new URL doesn't always trigger a fresh load —
// the previous COA can stay visible while the new one loads, or render blank.
// Round-tripping through about:blank forces Safari to discard its viewer state
// before committing the new resource. Two animation frames is the safest delay:
// one for Safari to commit the blank navigation, one for paint.
// Per-iframe generation token. Each _setPdfSrc call increments the iframe's
// token; only the most recent load handler is allowed to bring opacity back
// up. This prevents rapid sample-switching from leaving a fade-in stuck on
// a stale navigation.
const _pdfTokens = new Map();

function _setPdfSrc(id, url) {
    const el = document.getElementById(id);
    if (!el) return;
    const token = (_pdfTokens.get(id) || 0) + 1;
    _pdfTokens.set(id, token);

    // Crossfade: fade out → swap → fade in on load. Pairs with the
    // `transition: opacity` rule on `.pdf-pane iframe`. The load listener
    // is armed only after the *real* URL is assigned — otherwise it would
    // fire on the about:blank step and snap opacity back too early.
    const armFadeIn = () => {
        const onLoad = () => {
            el.removeEventListener("load", onLoad);
            if (_pdfTokens.get(id) === token) el.style.opacity = "1";
        };
        el.addEventListener("load", onLoad);
    };

    const current = el.getAttribute("src");
    el.style.opacity = "0";
    if (!current || current === "about:blank" || current === "") {
        armFadeIn();
        el.src = url;
        return;
    }
    el.src = "about:blank";
    requestAnimationFrame(() => requestAnimationFrame(() => {
        if (document.getElementById(id) !== el || _pdfTokens.get(id) !== token) return;
        el.style.opacity = "0"; // re-assert in case a stale handler bumped it
        armFadeIn();
        el.src = url;
    }));
}

function loadPDF(labId) {
    const placeholder = $("#pdf-placeholder");
    const coaPane = $("#coa-pane");
    const sifPane = $("#sif-pane");
    const sifViewer = $("#sif-viewer");
    const sifPlaceholder = $("#sif-placeholder");

    const ver = _pdfVersion[labId] || 0;
    _currentPdfLab = labId;

    placeholder.style.display = "none";
    // Which panes are up is the checkboxes' business, not the PDF loader's.
    applyPaneLayout();

    _setPdfSrc("pdf-viewer", `/api/pdf/${encodeURIComponent(labId)}?v=${ver}#view=FitH`);

    const sample = state.currentSample;
    if (sample && sample.has_sif) {
        sifViewer.style.display = "block";
        sifPlaceholder.style.display = "none";
        _setPdfSrc("sif-viewer", `/api/sif/${encodeURIComponent(labId)}#view=FitH`);
        const badge = $("#sif-page-badge");
        if (badge && sample.sif_page !== null && sample.sif_page !== undefined) {
            badge.textContent = `Page ${sample.sif_page + 1} of ${sample.sif_total_pages}`;
        } else if (badge) {
            badge.textContent = "";
        }
    } else {
        sifViewer.style.display = "none";
        sifViewer.src = "";
        sifPlaceholder.style.display = "flex";
        const badge = $("#sif-page-badge");
        applySifPlaceholder(sifPlaceholder, sample && sample.sif_status);
        if (badge) badge.textContent = "";
    }
}

function showPDFPlaceholder(text) {
    _currentPdfLab = null;
    $("#pdf-viewer").src = "";
    $("#sif-viewer").src = "";
    $("#coa-pane").style.display = "none";
    $("#sif-pane").style.display = "none";
    const placeholder = $("#pdf-placeholder");
    placeholder.textContent = text;
    placeholder.style.display = "flex";
}


/**
 * What the SIF pane should say for a given status.
 *
 * "No SIF" is three different situations and they are not interchangeable:
 * a customer-portal order legitimately has no SIF, while a paper order
 * missing its document is a problem someone needs to chase. QBench's
 * order_request_status tells them apart server-side (see classify_missing_sif).
 */
function sifPlaceholderText(status) {
    switch (status) {
        case "loading":      return "Loading SIF...";
        case "online_entry": return "No SIF — Online Entry";
        case "missing":      return "SIF missing for this order";
        case "error":        return "Couldn't load the SIF";
        default:             return "SIF pending...";
    }
}

/** Style hook so an expected absence doesn't look like an alarm. */
function sifPlaceholderClass(status) {
    if (status === "online_entry") return "sif-online";
    if (status === "missing") return "sif-missing";
    return "";
}

function applySifPlaceholder(el, status) {
    if (!el) return;
    el.textContent = sifPlaceholderText(status);
    el.classList.remove("sif-online", "sif-missing");
    const cls = sifPlaceholderClass(status);
    if (cls) el.classList.add(cls);
}

function updateSifStatus(tab, labId, sifStatus, sifPage, sifTotalPages) {
    const samples = state.samples[tab];
    if (samples) {
        const sample = samples.find(s => s.lab_id === labId);
        if (sample) {
            sample.has_sif = (sifStatus === "found");
            sample.sif_status = sifStatus;
            sample.sif_page = sifPage;
            sample.sif_total_pages = sifTotalPages;
        }
    }

    if (state.currentSample && state.currentSample.lab_id === labId) {
        state.currentSample.has_sif = (sifStatus === "found");
        state.currentSample.sif_status = sifStatus;
        state.currentSample.sif_page = sifPage;
        state.currentSample.sif_total_pages = sifTotalPages;

        if (sifStatus === "found" && _currentPdfLab === labId) {
            const sifViewer = $("#sif-viewer");
            const sifPlaceholder = $("#sif-placeholder");
            sifViewer.style.display = "block";
            sifPlaceholder.style.display = "none";
            _setPdfSrc("sif-viewer", `/api/sif/${encodeURIComponent(labId)}#view=FitH`);
            const badge = $("#sif-page-badge");
            if (badge && sifPage !== null && sifPage !== undefined) {
                badge.textContent = `Page ${sifPage + 1} of ${sifTotalPages}`;
            }
        } else if (_currentPdfLab === labId) {
            const sifPlaceholder = $("#sif-placeholder");
            if (sifPlaceholder) {
                sifPlaceholder.style.display = "flex";
                applySifPlaceholder(sifPlaceholder, sifStatus);
            }
        }
    }
}


// ══════════════════════════════════════════════════════════════════════
// Test Editor
// ══════════════════════════════════════════════════════════════════════

// ──────────────────────────────────────────────────────────────────────
// Sample-Info editor (Info mode right panel)
// ──────────────────────────────────────────────────────────────────────
//
// Renders **every** key in the QBench sample dict — editable fields
// (per the server's `editable_fields` whitelist) as inputs that save on
// Enter, the rest as read-only displays. INFO_EDITOR_FIELDS is now an
// ordering / labeling / multiline hint table, not a render filter; any
// QBench field that isn't in the table still renders, just at the end
// with an auto-prettified label.
//
// Save-on-Enter: PATCH /api/sample-info/<labId> with { [field]: value }.

// Exact field list from the QBench sample-info screen (2026-05-19). Order
// matters — this is the order reviewers expect to see them in. Custom
// fields (most of these) live under sample.custom_fields server-side; the
// /api/sample-info GET handler flattens them onto the top level so the
// renderer can iterate one shape. The /api/sample-info PATCH handler
// re-nests custom fields before calling QBench's update_sample.
// Enumerated field domains. Sample types and rush options are the lab's
// official lists (provided 2026-07-10); package sizes are the three
// bottle sizes in use. Fields with an `options` list render as dropdowns
// so reviewers can't type unsupported values.
const SAMPLE_TYPE_OPTIONS = [
    "Diesel #1", "Diesel #2", "ULSD", "DULSD", "LSD", "High Sulfur Diesel",
    "Type A", "Type B", "Off-road Diesel", "Marine Diesel", "Gasoline",
    "E87 Regular", "E89 Mid", "E91-93 Premium", "Jet A", "Jet A-1", "Jet B",
    "Kerosene", "Avgas", "Aviation Turbine", "Other", "Biodiesel", "B100",
    "B20", "B5", "R100", "R99", "R20", "R5", "Coolant",
    "Extended Life Coolant", "Oil", "B6-B20", "RD80/20",
];
const PACKAGE_SIZE_OPTIONS = ["5 oz/150 mL", "16 oz/500 mL", "32 oz/2000 mL"];
const RUSH_OPTIONS = [
    "Standard (72 hours)", "2 Day (+ 50% additional charge)",
    "24 hours (2x rate)", "Same Day (3x rate)",
];

const INFO_EDITOR_FIELDS = [
    { key: "lab_id",              label: "Lab ID" },
    { key: "fw",                  label: "Field Works Number" },
    { key: "work_order",          label: "Work Order" },
    { key: "accession_number",    label: "Accession Number" },
    { key: "fuel_type",           label: "Sample Type",  options: SAMPLE_TYPE_OPTIONS },
    { key: "package_size",        label: "Package Size", options: PACKAGE_SIZE_OPTIONS },
    { key: "po_number",           label: "PO" },
    { key: "time_of_collection",  label: "Time of Collection", type: "datetime" },
    { key: "customer_sample_id",  label: "Customer Sample ID" },
    { key: "sample_taken_from",   label: "Sample Taken From" },
    { key: "tank",                label: "Tank Serial Number" },
    { key: "generator",           label: "Generator" },
    { key: "component_model",     label: "Component Model" },
    { key: "tank_capacity",       label: "Tank Capacity" },
    { key: "point_of_collection", label: "Point of Collection" },
    { key: "quantity_tank",       label: "Quantity in Tank" },
    { key: "site_location",       label: "Site Location" },
    { key: "source",              label: "Tank" },
    { key: "tags",                label: "Tags" },
    { key: "attachments",         label: "Attachments" },
    { key: "comments",            label: "Comments",     multiline: true },
    { key: "Rush",                label: "Rush sample",  options: RUSH_OPTIONS },
];

// ── Shared field-visibility settings ──────────────────────────────────
// One config for ALL reviewers, persisted server-side in
// field_settings.json (survives restarts and logins). Cached here after
// the first fetch; the settings modal refreshes the cache on save.
let _fieldSettings = null;
let _lastInfoLabId = null;   // last sample rendered, for re-render on save

async function getFieldSettings() {
    if (_fieldSettings) return _fieldSettings;
    try {
        const resp = await fetch("/api/field-settings");
        if (resp.ok) _fieldSettings = await resp.json();
    } catch (e) { /* fall through to defaults */ }
    if (!_fieldSettings) _fieldSettings = { sample_info_hidden: [], show_extra_fields: true };
    return _fieldSettings;
}

function initFieldSettings() {
    const btn      = $("#field-settings-btn");
    const modal    = $("#field-settings-modal");
    const listEl   = $("#field-settings-list");
    const extrasEl = $("#field-settings-extras");
    const errorEl  = $("#field-settings-error");
    if (!btn || !modal || !listEl) return;

    btn.addEventListener("click", async () => {
        const settings = await getFieldSettings();
        const hidden = new Set(settings.sample_info_hidden || []);
        listEl.innerHTML = INFO_EDITOR_FIELDS.map(f => `
            <label class="checkbox-label field-settings-item">
                <input type="checkbox" data-key="${escapeAttr(f.key)}"${hidden.has(f.key) ? "" : " checked"}>
                ${escapeHtml(f.label)}
            </label>`).join("");
        if (extrasEl) extrasEl.checked = settings.show_extra_fields !== false;
        if (errorEl) errorEl.textContent = "";
        showModal("field-settings-modal");
    });

    $("#field-settings-cancel")?.addEventListener("click", () => hideModal("field-settings-modal"));
    modal.addEventListener("click", (e) => {
        if (e.target === modal) hideModal("field-settings-modal");
    });

    $("#field-settings-save")?.addEventListener("click", async () => {
        const hiddenKeys = [...listEl.querySelectorAll("input[data-key]")]
            .filter(cb => !cb.checked)
            .map(cb => cb.dataset.key);
        try {
            const resp = await fetch("/api/field-settings", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    sample_info_hidden: hiddenKeys,
                    show_extra_fields: !extrasEl || extrasEl.checked,
                }),
            });
            if (resp.status === 401) { triggerTimeout(); return; }
            const data = await resp.json();
            if (!resp.ok || !data.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            _fieldSettings = data.settings;
        } catch (e) {
            if (errorEl) errorEl.textContent = `Failed to save: ${e.message}`;
            return;
        }
        hideModal("field-settings-modal");
        // Re-render the open sample so the filter applies immediately.
        if (_lastInfoLabId) loadSampleInfo(_lastInfoLabId);
    });
}

function _prettifyFieldName(key) {
    return String(key)
        .replace(/_/g, " ")
        .replace(/\b\w/g, c => c.toUpperCase());
}

function _formatFieldValue(value) {
    if (value == null || value === "") return "";
    if (typeof value === "object") {
        try { return JSON.stringify(value); }
        catch (e) { return String(value); }
    }
    return String(value);
}

async function loadSampleInfo(labId) {
    const fieldsEl = $("#info-editor-fields");
    const statusEl = $("#info-editor-status");
    if (!fieldsEl) return;
    _lastInfoLabId = labId;
    if (statusEl) statusEl.textContent = "Loading…";
    fieldsEl.innerHTML = '<p class="info-editor-placeholder">Loading…</p>';

    const fieldSettings = await getFieldSettings();
    const hiddenFields = new Set(fieldSettings.sample_info_hidden || []);

    let data;
    try {
        const resp = await fetch(`/api/sample-info/${encodeURIComponent(labId)}`);
        if (resp.status === 401) { triggerTimeout(); return; }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        data = await resp.json();
    } catch (e) {
        if (statusEl) statusEl.textContent = "";
        fieldsEl.innerHTML = `<p class="info-editor-placeholder">Failed to load: ${e.message}</p>`;
        return;
    }

    const sample = data.sample || {};
    const panels = Array.isArray(data.panels) ? data.panels : [];
    // Server-driven whitelist; falls back to an empty set if missing.
    const editable = new Set(Array.isArray(data.editable_fields) ? data.editable_fields : []);

    // Order: preferred fields first (in their declared order), then any
    // remaining sample keys alphabetically. That way newly-exposed QBench
    // fields automatically show up at the bottom without code changes.
    // Both lists respect the shared visibility settings: hidden known
    // fields are dropped (not demoted to "extras"), and the extras block
    // can be switched off entirely.
    const knownKeys = new Set(INFO_EDITOR_FIELDS.map(s => s.key));
    const preferred = INFO_EDITOR_FIELDS.map(s => s.key).filter(k => !hiddenFields.has(k));
    const extraKeys = fieldSettings.show_extra_fields === false
        ? []
        : Object.keys(sample).filter(k => !knownKeys.has(k) && !hiddenFields.has(k)).sort();
    const orderedKeys = [...preferred, ...extraKeys];
    const hintByKey = Object.fromEntries(INFO_EDITOR_FIELDS.map(s => [s.key, s]));

    const rows = orderedKeys.map(key => {
        const hint = hintByKey[key] || {};
        const label = hint.label || _prettifyFieldName(key);
        const isEditable = editable.has(key);
        const display = _formatFieldValue(sample[key]);
        let inputEl;
        if (isEditable && hint.options) {
            // Enumerated field → dropdown. A stored value that's not in
            // the official list is kept as an extra option, so rendering
            // the editor never silently rewrites existing data.
            const opts = (display === "" || hint.options.includes(display))
                ? hint.options
                : [display, ...hint.options];
            inputEl = `<select class="info-input" data-field="${key}">` +
                `<option value=""${display === "" ? " selected" : ""}>—</option>` +
                opts.map(o =>
                    `<option value="${escapeAttr(o)}"${o === display ? " selected" : ""}>${escapeHtml(o)}</option>`
                ).join("") +
                `</select>`;
        } else if (isEditable && hint.type === "datetime" && (display === "" || _toDatetimeLocal(display))) {
            // Datetime field → native picker. QBench stores
            // "MM/DD/YYYY hh:mm AM/PM"; an unparseable stored value falls
            // through to the plain text input below instead of being
            // blanked by a picker that can't represent it.
            inputEl = `<input type="datetime-local" class="info-input" data-field="${key}"` +
                ` data-type="datetime" value="${escapeAttr(_toDatetimeLocal(display))}">`;
        } else if (isEditable) {
            inputEl = hint.multiline
                ? `<textarea class="info-input" data-field="${key}" rows="3">${escapeHtml(display)}</textarea>`
                : `<input type="text" class="info-input" data-field="${key}" value="${escapeAttr(display)}">`;
        } else {
            inputEl = `<div class="info-readonly">${escapeHtml(display) || '<span class="info-empty">—</span>'}</div>`;
        }
        return `
            <div class="info-row${isEditable ? '' : ' info-row--readonly'}">
                <label class="info-label">${escapeHtml(label)}</label>
                ${inputEl}
            </div>`;
    }).join("");

    const panelsDisplay = panels.length
        ? panels.map(p => `<span class="info-panel-chip">${escapeHtml(p)}</span>`).join("")
        : '<span class="info-empty">—</span>';

    fieldsEl.innerHTML = rows + `
        <div class="info-row info-row--readonly">
            <label class="info-label">Panels</label>
            <div class="info-readonly info-panels">${panelsDisplay}</div>
        </div>`;

    if (statusEl) statusEl.textContent = "";

    // Save triggers: Enter on text inputs, Cmd/Ctrl+Enter on textareas
    // (plain Enter in a textarea inserts a newline), AND blur (click-off
    // anywhere outside the input). Blur-saves are deduped against the
    // value at focus time so click-off without changes doesn't fire a
    // pointless PATCH.
    fieldsEl.querySelectorAll(".info-input").forEach(el => {
        // Cache the value at focus so blur-save can skip unchanged fields.
        let focusValue = el.value;
        el.addEventListener("focus", () => { focusValue = el.value; });
        // Dropdowns save the moment a choice is made; syncing focusValue
        // makes the subsequent blur a no-op instead of a duplicate PATCH.
        if (el.tagName === "SELECT") {
            el.addEventListener("change", () => {
                focusValue = el.value;
                saveSampleInfoField(labId, el);
            });
        }
        el.addEventListener("keydown", (e) => {
            const isTextarea = el.tagName === "TEXTAREA";
            if (e.key !== "Enter") return;
            if (isTextarea && !(e.metaKey || e.ctrlKey)) return;
            e.preventDefault();
            focusValue = el.value;  // mark "synced" so blur is a no-op
            saveSampleInfoField(labId, el);
        });
        el.addEventListener("blur", () => {
            if (el.value === focusValue) return;     // no change → no save
            focusValue = el.value;
            saveSampleInfoField(labId, el);
        });
    });
}

// QBench stores collection times as "MM/DD/YYYY hh:mm AM/PM"; the native
// datetime-local input speaks "YYYY-MM-DDTHH:mm". Convert both ways.
// _toDatetimeLocal returns "" for values it can't parse — the renderer
// uses that to fall back to a text input rather than blank the value.
function _toDatetimeLocal(s) {
    const m = String(s).trim().match(
        /^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)$/i);
    if (!m) return "";
    let h = parseInt(m[4], 10) % 12;
    if (/pm/i.test(m[6])) h += 12;
    const pad = n => String(n).padStart(2, "0");
    return `${m[3]}-${pad(m[1])}-${pad(m[2])}T${pad(h)}:${m[5]}`;
}
function _fromDatetimeLocal(v) {
    const m = String(v).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    if (!m) return v;
    let h = parseInt(m[4], 10);
    const ap = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    return `${m[2]}/${m[3]}/${m[1]} ${String(h).padStart(2, "0")}:${m[5]} ${ap}`;
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function escapeAttr(s) {
    return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

async function saveSampleInfoField(labId, inputEl) {
    const field = inputEl.dataset.field;
    if (!field) return;
    const statusEl = $("#info-editor-status");
    let value = inputEl.value;
    // datetime-local inputs hold "YYYY-MM-DDTHH:mm" — convert back to the
    // "MM/DD/YYYY hh:mm AM/PM" format QBench stores before saving.
    if (inputEl.dataset.type === "datetime" && value) value = _fromDatetimeLocal(value);
    inputEl.classList.add("info-input--saving");
    if (statusEl) statusEl.textContent = `Saving ${field}…`;
    try {
        const resp = await fetch(`/api/sample-info/${encodeURIComponent(labId)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ [field]: value }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        inputEl.classList.remove("info-input--saving", "info-input--error");
        inputEl.classList.add("info-input--saved");
        // Green persists until a different sample is selected. loadSampleInfo
        // re-renders the editor on selection, which clears all per-field
        // state via innerHTML replacement. Status text fades after a moment
        // since "Saved fw" is just a transient confirmation.
        if (statusEl) {
            statusEl.textContent = `Saved ${field}`;
            setTimeout(() => {
                if (statusEl.textContent === `Saved ${field}`) statusEl.textContent = "";
            }, 1400);
        }
    } catch (e) {
        inputEl.classList.remove("info-input--saving");
        inputEl.classList.add("info-input--error");
        if (statusEl) statusEl.textContent = `Failed: ${e.message}`;
    }
}


async function loadTests(labId) {
    const tbody = $("#test-table-body");
    const statusEl = $("#test-editor-status");
    statusEl.textContent = "Loading...";

    try {
        const resp = await fetch(`/api/tests/${encodeURIComponent(labId)}`);
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        const tests = data.tests || [];

        if (tests.length === 0) {
            tbody.innerHTML = '<tr><td colspan="2" class="placeholder-cell">No tests found</td></tr>';
            statusEl.textContent = "";
            return;
        }

        tbody.innerHTML = "";
        tests.forEach(t => {
            const tr = document.createElement("tr");

            const tdName = document.createElement("td");
            tdName.className = "test-name-cell";
            tdName.textContent = t.test_name;
            tdName.title = t.test_name;
            tr.appendChild(tdName);

            const tdResult = document.createElement("td");
            tdResult.className = "test-result-cell";

            const input = document.createElement("input");
            input.type = "text";
            input.className = "test-result-input";
            input.value = t.results || "";
            input.dataset.testId = t.test_id;
            input.dataset.original = t.results || "";

            if (!t.results || t.results === "N/A") {
                input.classList.add("na-value");
                if (t.results === "N/A") input.value = "";
            }

            input.addEventListener("change", async (e) => {
                const newVal = e.target.value.trim();
                const testId = parseInt(e.target.dataset.testId);
                input.classList.remove("na-value");
                input.classList.add("changed");

                try {
                    const r = await fetch(`/api/tests/${testId}`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ value: newVal }),
                    });
                    if (r.status === 401) { triggerTimeout(); return; }
                    const body = await r.json().catch(() => ({}));
                    if (!r.ok || !body.ok) {
                        statusEl.textContent = `Error updating ${t.test_name}: ${body.error || `HTTP ${r.status}`}`;
                        input.classList.remove("changed");
                        return;
                    }
                    statusEl.textContent = `Updated: ${t.test_name}`;
                    setTimeout(() => { statusEl.textContent = ""; }, 3000);
                } catch(err) {
                    statusEl.textContent = `Error updating ${t.test_name}`;
                    input.classList.remove("changed");
                }
            });

            input.addEventListener("focus", () => {
                if (input.classList.contains("na-value")) {
                    input.value = "";
                    input.classList.remove("na-value");
                }
            });

            tdResult.appendChild(input);
            tr.appendChild(tdResult);
            tbody.appendChild(tr);
        });

        statusEl.textContent = `${tests.length} test(s)`;
    } catch(e) {
        tbody.innerHTML = `<tr><td colspan="2" class="placeholder-cell">Error loading tests</td></tr>`;
        statusEl.textContent = "Error";
    }
}


// ══════════════════════════════════════════════════════════════════════
// Attachments & Comments
// ══════════════════════════════════════════════════════════════════════

async function loadAttachments(labId) {
    const list = $("#att-list");
    list.innerHTML = '<div class="placeholder-item">Loading...</div>';
    state.selectedAttId = null;
    $("#att-delete-btn").disabled = true;

    try {
        const resp = await fetch(`/api/attachments/${encodeURIComponent(labId)}`);
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        const atts = data.attachments || [];

        if (atts.length === 0) {
            list.innerHTML = '<div class="placeholder-item">(no attachments)</div>';
            return;
        }

        list.innerHTML = "";
        atts.forEach(a => {
            const div = document.createElement("div");
            div.className = "att-item";
            div.dataset.attId = a.id;
            const dot = a.is_report ? "\u25CF " : "\u25CB ";
            div.textContent = dot + a.filename;
            div.title = `ID: ${a.id} | ${a.is_report ? "Attached to report" : "Not attached"}`;
            div.addEventListener("click", () => {
                $$(".att-item").forEach(el => el.classList.remove("selected"));
                div.classList.add("selected");
                state.selectedAttId = a.id;
                $("#att-delete-btn").disabled = false;
            });
            list.appendChild(div);
        });
    } catch(e) {
        list.innerHTML = '<div class="placeholder-item">Error loading</div>';
    }
}

let _commentsRaw = "";
// lab_id -> last server-confirmed comment text, kept while a save is in
// flight so a comment_failed SSE event can revert the optimistic display.
const _pendingComments = {};

async function loadComments(labId) {
    const display = $("#comments-display");
    const editor = $("#comments-editor");
    const editBtn = $("#comment-edit-btn");
    const saveBtn = $("#comment-save-btn");
    const cancelBtn = $("#comment-cancel-btn");

    display.classList.remove("hidden");
    editor.classList.add("hidden");
    saveBtn.classList.add("hidden");
    cancelBtn.classList.add("hidden");
    editBtn.disabled = true;
    display.innerHTML = '<span class="placeholder-item">Loading...</span>';

    try {
        const resp = await fetch(`/api/comments/${encodeURIComponent(labId)}`);
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        _commentsRaw = data.comments || "";
        display.textContent = _commentsRaw || "(no comments)";
    } catch(e) {
        display.textContent = "(error loading comments)";
        _commentsRaw = "";
    } finally {
        // Always re-enable Edit — otherwise a 401 or a failed comment fetch
        // leaves the button stuck disabled and the reviewer "can't edit".
        editBtn.disabled = false;
    }
}

function enterCommentEditMode() {
    $("#comments-display").classList.add("hidden");
    $("#comments-editor").classList.remove("hidden");
    $("#comments-editor").value = _commentsRaw;
    $("#comments-editor").focus();
    $("#comment-edit-btn").classList.add("hidden");
    $("#comment-save-btn").classList.remove("hidden");
    $("#comment-cancel-btn").classList.remove("hidden");
}

function exitCommentEditMode() {
    $("#comments-editor").classList.add("hidden");
    $("#comments-display").classList.remove("hidden");
    $("#comment-edit-btn").classList.remove("hidden");
    $("#comment-save-btn").classList.add("hidden");
    $("#comment-cancel-btn").classList.add("hidden");
}

async function saveComments() {
    const s = state.currentSample;
    if (!s) return;

    const newText = $("#comments-editor").value.trim();
    const saveBtn = $("#comment-save-btn");
    saveBtn.disabled = true;

    try {
        const resp = await fetch(`/api/comments/${encodeURIComponent(s.lab_id)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ comments: newText }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        if (data.ok) {
            // Optimistic: the write is queued server-side and survives a
            // window close. The comment_saved/comment_failed SSE event
            // reconciles the final outcome. Remember the prior value so a
            // failed save can be reverted.
            _pendingComments[s.lab_id] = _commentsRaw;
            _commentsRaw = newText;
            $("#comments-display").textContent = newText || "(no comments)";
            setStatus(`Saving comments for ${s.lab_id}…`);
            exitCommentEditMode();
        } else {
            // Stay in edit mode so the reviewer can retry without retyping.
            setStatus(`Failed to save comments: ${data.error || "unknown error"}`);
        }
    } catch(e) {
        setStatus(`Failed to save comments: ${e.message}`);
    } finally {
        // Always re-enable Save — a 401/early-return must never leave it stuck.
        saveBtn.disabled = false;
    }
}

async function handleDeleteAttachment() {
    if (!state.selectedAttId) return;
    if (!confirm("Permanently delete this attachment from QBench?\n\nThis cannot be undone.")) return;

    try {
        const resp = await fetch(`/api/attachments/${state.selectedAttId}`, { method: "DELETE" });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        if (data.ok && state.currentSample) {
            loadAttachments(state.currentSample.lab_id);
        }
    } catch(e) {
        alert("Failed to delete attachment.");
    }
}


// ══════════════════════════════════════════════════════════════════════
// Good / Bad Marking
// ══════════════════════════════════════════════════════════════════════

// Applies a mark locally. The Command Center side (creating or completing a
// listing) is handled by the callers before they get here.
async function applyMark(sample, outcome, extra = {}) {
    const resp = await fetch("/api/mark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            tab: sample.tab, lab_id: sample.lab_id, outcome, ...extra,
        }),
    });
    if (resp.status === 401) { triggerTimeout(); return null; }
    return await resp.json();
}

async function markGood() {
    const s = state.currentSample;
    if (!s) return;

    // A sample with an open listing must not be quietly closed out — ask
    // first. resolveListing returns false when the reviewer backs out.
    if (!await resolveListing(s, "marking this sample Good")) return;

    try {
        const data = await applyMark(s, "good");
        if (data && data.ok) {
            setStatus(`${s.lab_id} marked Good`);
            advanceToNext();
        }
    } catch(e) {
        setStatus(`Error marking good: ${e.message}`);
    }
}

async function handleUncheck() {
    const s = state.currentSample;
    if (!s) return;
    if (!["good", "bad"].includes(s.status)) return;

    if (!await resolveListing(s, "un-marking this sample")) return;

    try {
        const data = await applyMark(s, "uncheck");
        if (data && data.ok) {
            setStatus(`${s.lab_id} un-marked`);
            // Deliberately does not advance: un-marking means the reviewer
            // wants another look at this sample.
        }
    } catch(e) {
        setStatus(`Error un-marking: ${e.message}`);
    }
}


// ══════════════════════════════════════════════════════════════════════
// Command Center
// ══════════════════════════════════════════════════════════════════════

const CC = {
    labVisionUrl: "",
    available: false,
    samples: [],        // lab_id chips on the open flag form
    pendingDraft: null, // listing body held back by a conflict
    resolve: null,      // resolver for the in-flight resolveListing() promise
    groupMode: false,   // one listing covering a whole selection
};

async function initCommandCenter() {
    try {
        const resp = await fetch("/api/cc/config");
        if (!resp.ok) return;
        const cfg = await resp.json();
        CC.labVisionUrl = cfg.lab_vision_url || "";
        CC.available = !!cfg.available;
        updateCommandCenterBanner();
        // This resolves after boot, so a sample may already be selected with
        // its buttons computed against an empty labVisionUrl. Without this
        // refresh "Open in Lab Vision" stays dead until the reviewer happens
        // to select a different sample.
        updateActionButtons();
    } catch(e) { /* banner stays hidden; calls will surface their own errors */ }
}

function updateCommandCenterBanner() {
    const el = $("#cc-banner");
    if (!el) return;
    el.classList.toggle("hidden", CC.available);
}

function ccListingHtml(tasks) {
    if (!tasks || !tasks.length) return "<p class='cc-modal-msg'>No listing found.</p>";
    return tasks.map(t => `
        <div class="cc-listing" data-task-id="${t.id}">
            <div class="cc-listing-head">
                <span class="cc-listing-id">CC #${t.id}</span>
                <span class="cc-listing-status cc-status-${escapeHtml(t.status || "")}">${escapeHtml(t.status || "")}</span>
            </div>
            <div class="cc-listing-problem">${escapeHtml(t.initial_problem || "(no description)")}</div>
            ${t.customer ? `<div class="cc-listing-meta">Customer: ${escapeHtml(t.customer)}</div>` : ""}
            ${t.latest_update ? `<div class="cc-listing-meta">Latest: ${escapeHtml(t.latest_update)}</div>` : ""}
        </div>`).join("");
}

// ── Flagging Bad ──────────────────────────────────────────────────────

async function markBad() {
    const s = state.currentSample;
    if (!s) return;

    $("#cc-title").textContent = `Flag Sample — ${s.lab_id}`;
    $("#cc-problem").value = "";
    $("#cc-context").value = "";
    $("#cc-customer").value = "";
    $("#cc-type").value = "double_check";
    $("#cc-status").value = "open";
    $("#cc-department").value = "";
    $("#cc-error").textContent = "";
    CC.samples = [{ lab_id: s.lab_id }];
    CC.pendingDraft = null;
    CC.groupMode = false;
    renderCcSamples();
    showModal("cc-modal");
    $("#cc-problem").focus();

    // Autofill from LabCore. Best-effort: an unreachable LabCore must not
    // stop the reviewer typing — the submit is where it fails loudly.
    try {
        const resp = await fetch(`/api/cc/lookup/${encodeURIComponent(s.lab_id)}`);
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.customer_name) $("#cc-customer").value = data.customer_name;
        CC.samples[0].customer_name = data.customer_name || "";
        CC.samples[0].fuel_type = data.fuel_type || "";
        if (data.conflict && data.existing_tasks && data.existing_tasks.length) {
            // Pre-warn rather than letting them fill the whole form first.
            $("#cc-error").textContent =
                `Note: already on the board as CC #${data.existing_tasks[0].id}.`;
        }
    } catch(e) { /* autofill is optional */ }

    loadCcCustomers();
}

async function loadCcCustomers() {
    try {
        const resp = await fetch("/api/cc/customers");
        if (!resp.ok) return;
        const names = await resp.json();
        $("#cc-customer-options").innerHTML =
            (names || []).map(n => `<option value="${escapeHtml(n)}"></option>`).join("");
    } catch(e) { /* datalist is optional */ }
}

function renderCcSamples() {
    $("#cc-samples").innerHTML = CC.samples.map((s, i) => `
        <span class="cc-chip">${escapeHtml(s.lab_id)}
            <button type="button" class="cc-chip-x" data-idx="${i}" aria-label="Remove">&times;</button>
        </span>`).join("");
    $$("#cc-samples .cc-chip-x").forEach(btn => {
        btn.addEventListener("click", () => {
            CC.samples.splice(parseInt(btn.dataset.idx, 10), 1);
            renderCcSamples();
        });
    });
}

function addCcSample() {
    const input = $("#cc-sample-input");
    const raw = input.value.trim();
    if (!raw) return;
    raw.split(",").map(x => x.trim()).filter(Boolean).forEach(labId => {
        if (!CC.samples.some(s => s.lab_id === labId)) CC.samples.push({ lab_id: labId });
    });
    input.value = "";
    renderCcSamples();
}

function ccFormBody() {
    return {
        initial_problem: $("#cc-problem").value.trim(),
        type: $("#cc-type").value,
        context: $("#cc-context").value.trim(),
        customer: $("#cc-customer").value.trim(),
        status: $("#cc-status").value,
        department: $("#cc-department").value,
        sample_ids: CC.samples,
    };
}

async function submitCcListing(body) {
    const s = state.currentSample;
    const btn = $("#cc-submit");
    btn.disabled = true;
    try {
        const resp = await fetch("/api/cc/tasks", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();

        if (data.labcore_down) {
            // Loud on purpose: a flag that silently fails to file would let
            // the reviewer move on believing the sample was recorded.
            $("#cc-error").textContent =
                "Command Center is unreachable — the sample was NOT flagged. Try again.";
            return;
        }
        if (data.error) { $("#cc-error").textContent = data.error; return; }

        if (data.conflict) {
            CC.pendingDraft = body;
            hideModal("cc-modal");
            $("#cc-conflict-list").innerHTML = ccListingHtml(data.existing_tasks);
            showModal("cc-conflict-modal");
            return;
        }

        hideModal("cc-modal");

        if (CC.groupMode) {
            // One listing, many samples: every sample in the group is marked
            // Bad against it, not just whichever was last clicked.
            const labIds = body.sample_ids.map(x => x.lab_id);
            for (const labId of labIds) {
                const sample = (state.samples[state.currentTab] || [])
                    .find(x => x.lab_id === labId);
                if (!sample) continue;   // e.g. a lab_id typed in by hand
                await applyMark(sample, "bad", {
                    reason: body.initial_problem,
                    cc_task_id: data.task_id,
                });
            }
            CC.groupMode = false;
            clearSampleSelection();
            setStatus(`${labIds.length} sample(s) flagged — Command Center CC #${data.task_id}`);
            return;
        }

        const marked = await applyMark(s, "bad", {
            reason: body.initial_problem,
            cc_task_id: data.task_id,
        });
        if (marked && marked.ok) {
            setStatus(`${s.lab_id} flagged — Command Center CC #${data.task_id}`);
            advanceToNext();
        }
    } catch(e) {
        $("#cc-error").textContent = `Failed to file listing: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

function handleCcSubmit() {
    const body = ccFormBody();
    if (!body.initial_problem) {
        $("#cc-error").textContent = "Enter a description of the problem.";
        return;
    }
    $("#cc-error").textContent = "";
    submitCcListing(body);
}

// ── Conflict resolution ───────────────────────────────────────────────

function handleCcConflictCreate() {
    if (!CC.pendingDraft) return;
    hideModal("cc-conflict-modal");
    // LabCore returns the same conflict forever without this flag.
    submitCcListing({ ...CC.pendingDraft, force_create: true });
    CC.pendingDraft = null;
}

function handleCcConflictCancel() {
    CC.pendingDraft = null;
    hideModal("cc-conflict-modal");
}

// ── Resolving an open listing before Good / Uncheck ───────────────────

/**
 * If the sample has an active listing, ask what to do with it.
 * Resolves true to proceed with the mark, false to abandon it.
 */
async function resolveListing(sample, actionLabel) {
    let tasks = [];
    try {
        // /api/cc/check, not /api/cc/lookup: this runs on every Good mark and
        // every un-mark, and resolving a listing does not need the
        // customer/fuel autofill the flag form uses. Measured against live
        // LabCore, that is ~150ms here instead of ~272ms.
        const resp = await fetch(`/api/cc/check/${encodeURIComponent(sample.lab_id)}`);
        if (resp.status === 401) { triggerTimeout(); return false; }
        const data = await resp.json();
        if (data.labcore_down) {
            // Can't tell whether a listing exists. Let the reviewer decide
            // rather than blocking them or silently assuming there is none.
            return confirm(
                "Command Center is unreachable, so any open listing for this " +
                `sample can't be checked. Continue ${actionLabel} anyway?`
            );
        }
        tasks = (data.conflict && data.existing_tasks) ? data.existing_tasks : [];
    } catch(e) {
        return confirm(`Couldn't reach Command Center. Continue ${actionLabel} anyway?`);
    }

    if (!tasks.length) return true;   // nothing to resolve

    $("#cc-resolve-title").textContent = `Open listing for ${sample.lab_id}`;
    $("#cc-resolve-list").innerHTML = ccListingHtml(tasks);
    $("#cc-resolve-notes").value = "";
    $("#cc-resolve-error").textContent = "";
    showModal("cc-resolve-modal");

    return await new Promise(resolve => {
        CC.resolve = { resolve, tasks, sample };
    });
}

function finishResolve(proceed) {
    const pending = CC.resolve;
    CC.resolve = null;
    hideModal("cc-resolve-modal");
    if (pending) pending.resolve(proceed);
}

async function handleResolveComplete() {
    const pending = CC.resolve;
    if (!pending) return;
    const notes = $("#cc-resolve-notes").value.trim();
    if (!notes) {
        // LabCore rejects an empty completion, so stop here rather than
        // round-tripping for a guaranteed error.
        $("#cc-resolve-error").textContent = "Enter the final result to complete the listing.";
        return;
    }

    const btn = $("#cc-resolve-complete");
    btn.disabled = true;
    try {
        for (const task of pending.tasks) {
            const resp = await fetch(`/api/cc/tasks/${task.id}/complete`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ notes }),
            });
            if (resp.status === 401) { triggerTimeout(); return; }
            const data = await resp.json();
            if (data.labcore_down || data.error) {
                $("#cc-resolve-error").textContent =
                    data.error || "Command Center unreachable — listing not completed.";
                return;
            }
        }
        setStatus(`Completed ${pending.tasks.length} Command Center listing(s)`);
        finishResolve(true);
    } catch(e) {
        $("#cc-resolve-error").textContent = `Failed to complete: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

// ══════════════════════════════════════════════════════════════════════
// Sync Data — Lab Vision sample information → QBench
//
// Sample information only; test results are deliberately out of scope.
// LabVision and QBench name the same field differently often enough that the
// reviewer re-points rows by dragging, so auto-pairing stays conservative and
// anything it can't match confidently is left unpaired.
// ══════════════════════════════════════════════════════════════════════

// The board's state. `links` maps a QBench field name to the Lab Vision field
// paired with it — one QBench field holds at most one source, and a source
// sits in at most one QBench field. `send` is the tick per QBench field.
const SYNC = {
    labId: null, lvFields: [], qbFields: [], links: {}, send: {},
    qbenchRead: true,
};

async function openSyncData() {
    const s = state.currentSample;
    if (!s) return;

    SYNC.labId = s.lab_id;
    SYNC.lvFields = [];
    SYNC.qbFields = [];
    SYNC.links = {};
    SYNC.send = {};
    $("#sync-title").textContent = `Sync Lab Vision Data — ${s.lab_id}`;
    $("#sync-error").textContent = "";
    $("#sync-warning").classList.add("hidden");
    $("#sync-lv-col").innerHTML = `<div class="sync-empty">Loading…</div>`;
    $("#sync-qb-col").innerHTML = "";
    showModal("sync-modal");

    try {
        const resp = await fetch(`/api/sync-preview/${encodeURIComponent(s.lab_id)}`);
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        if (data.labcore_down) {
            $("#sync-lv-col").innerHTML = "";
            $("#sync-error").textContent = "Lab Vision is unreachable — nothing was synced.";
            return;
        }
        if (data.error) {
            $("#sync-lv-col").innerHTML = "";
            $("#sync-error").textContent = data.error;
            return;
        }
        SYNC.lvFields = data.lv_fields || [];
        SYNC.qbFields = data.qb_fields || [];
        SYNC.qbenchRead = data.qbench_read !== false;

        // Seed the board with what the server auto-paired, then default the
        // ticks the same way a manual pair is defaulted.
        (data.pairs || []).forEach(p => {
            if (p.target) SYNC.links[p.target] = p.source;
        });
        Object.entries(SYNC.links).forEach(([qb, lv]) => {
            SYNC.send[qb] = syncDefaultSend(lv, qb);
        });

        // Without QBench's values nothing can be called a clash, and every
        // "current" would read as an empty field. Say so rather than imply it.
        const warn = $("#sync-warning");
        warn.classList.toggle("hidden", SYNC.qbenchRead);
        if (!SYNC.qbenchRead) {
            warn.textContent = "QBench's current values could not be read — " +
                "existing values are not shown and clashes are not detected.";
        }
        renderSyncBoard();
    } catch (e) {
        $("#sync-lv-col").innerHTML = "";
        $("#sync-error").textContent = "Failed to load Lab Vision data: " + e.message;
    }
}

/** What a pairing would do to QBench, recomputed from the board's own data.
 *  The old code cleared the clash flag on a manual re-point, so dragging onto
 *  a populated QBench field could overwrite a released value in silence. */
function syncPairState(lvName, qbName) {
    const lv = SYNC.lvFields.find(f => f.name === lvName);
    const qb = SYNC.qbFields.find(f => f.name === qbName);
    const value = lv ? lv.value : "";
    const current = qb ? qb.current : "";
    return {
        value, current,
        unchanged: Boolean(current && current === value),
        clash: Boolean(current && current !== value),
    };
}

/** A pair that only fills a blank QBench field is ticked; one that would
 *  replace or repeat an existing value is not. */
function syncDefaultSend(lvName, qbName) {
    const st = syncPairState(lvName, qbName);
    if (st.clash || st.unchanged) return false;
    return true;
}

/** Which QBench field a Lab Vision field is currently paired with, if any. */
function syncTargetOf(lvName) {
    return Object.keys(SYNC.links).find(qb => SYNC.links[qb] === lvName) || null;
}

/**
 * Pair `lvName` into the QBench field `qbName`.
 *
 * A source lives in one QBench field and a QBench field holds one source, so
 * both old homes are vacated first. Whatever `qbName` held is left UNPAIRED
 * rather than reassigned somewhere else — guessing again is how a value ends
 * up in the wrong field.
 */
function pairSyncField(lvName, qbName) {
    const lv = SYNC.lvFields.find(f => f.name === lvName);
    const qb = SYNC.qbFields.find(f => f.name === qbName);
    if (!lv || !lv.syncable || !qb || !qb.editable) return;

    const previousHome = syncTargetOf(lvName);
    if (previousHome) { delete SYNC.links[previousHome]; delete SYNC.send[previousHome]; }
    // The displaced source simply stops being paired; it returns to the left
    // column with no target.
    delete SYNC.links[qbName];

    SYNC.links[qbName] = lvName;
    SYNC.send[qbName] = syncDefaultSend(lvName, qbName);
    renderSyncBoard();
}

function unpairSyncField(qbName) {
    delete SYNC.links[qbName];
    delete SYNC.send[qbName];
    renderSyncBoard();
}

function renderSyncBoard() {
    const lvBox = $("#sync-lv-col");
    const qbBox = $("#sync-qb-col");
    if (!lvBox || !qbBox) return;

    // ── left: every Lab Vision field ──────────────────────────────────
    if (!SYNC.lvFields.length) {
        lvBox.innerHTML = `<div class="sync-empty">No Lab Vision data for this sample.</div>`;
    } else {
        lvBox.innerHTML = SYNC.lvFields.map(f => {
            const target = syncTargetOf(f.name);
            // A field QBench will not accept is shown but not draggable —
            // offering the drag would promise a write the server refuses.
            const cls = [
                "sync-card",
                f.syncable ? "" : "sync-card--locked",
                target ? "sync-card--paired" : "",
            ].filter(Boolean).join(" ");
            return `
            <div class="${cls}" data-lv="${escapeHtml(f.name)}"
                 ${f.syncable ? 'draggable="true" title="Drag onto a QBench field"' : 'title="QBench cannot accept this field"'}>
                <span class="sync-card-name">${escapeHtml(f.name)}</span>
                <span class="sync-card-val">${escapeHtml(f.value) || "—"}</span>
                ${target ? `<span class="sync-card-link">→ ${escapeHtml(target)}</span>` : ""}
            </div>`;
        }).join("");
    }

    // ── right: every QBench field, editable ones first ────────────────
    let sawReadOnly = false;
    qbBox.innerHTML = SYNC.qbFields.map(f => {
        let head = "";
        if (!f.editable && !sawReadOnly) {
            sawReadOnly = true;
            head = `<div class="sync-qb-sep">Read-only in QBench</div>`;
        }
        const lvName = SYNC.links[f.name];
        const st = lvName ? syncPairState(lvName, f.name) : null;
        const cls = [
            "sync-slot",
            f.editable ? "" : "sync-slot--locked",
            lvName ? "sync-slot--paired" : "",
            st && st.clash ? "sync-slot--clash" : "",
            st && st.unchanged ? "sync-slot--same" : "",
        ].filter(Boolean).join(" ");
        const checkbox = lvName
            ? `<input type="checkbox" class="sync-check" data-qb="${escapeHtml(f.name)}"
                      ${SYNC.send[f.name] ? "checked" : ""}${st.unchanged ? " disabled" : ""}
                      aria-label="Send ${escapeHtml(lvName)} to ${escapeHtml(f.name)}">`
            : "";
        return `${head}
        <div class="${cls}" data-qb="${escapeHtml(f.name)}">
            <div class="sync-slot-main">
                <span class="sync-slot-name">${escapeHtml(f.name)}</span>
                <span class="sync-slot-cur">${escapeHtml(f.current) || (SYNC.qbenchRead ? "(empty)" : "—")}</span>
            </div>
            ${lvName ? `
            <div class="sync-slot-pair">
                <span class="sync-slot-from">← ${escapeHtml(lvName)}: ${escapeHtml(st.value)}</span>
                ${st.clash ? `<span class="sync-slot-warn">replaces "${escapeHtml(st.current)}"</span>` : ""}
                ${st.unchanged ? `<span class="sync-slot-warn">already matches</span>` : ""}
                <button class="sync-unpair" data-qb="${escapeHtml(f.name)}"
                        title="Unpair" aria-label="Unpair ${escapeHtml(f.name)}">×</button>
            </div>` : ""}
            <div class="sync-slot-send">${checkbox}</div>
        </div>`;
    }).join("");

    wireSyncBoard(lvBox, qbBox);
}

function wireSyncBoard(lvBox, qbBox) {
    lvBox.querySelectorAll('.sync-card[draggable="true"]').forEach(el => {
        el.addEventListener("dragstart", (e) => {
            e.dataTransfer.setData("text/plain", el.dataset.lv);
            e.dataTransfer.effectAllowed = "move";
        });
    });

    // Dropping back on the left column unpairs — the other way out besides
    // the × on the pairing.
    lvBox.addEventListener("dragover", (e) => e.preventDefault());
    lvBox.addEventListener("drop", (e) => {
        e.preventDefault();
        const lvName = e.dataTransfer.getData("text/plain");
        const home = syncTargetOf(lvName);
        if (home) unpairSyncField(home);
    });

    qbBox.querySelectorAll(".sync-slot:not(.sync-slot--locked)").forEach(el => {
        el.addEventListener("dragover", (e) => {
            e.preventDefault();
            el.classList.add("sync-slot--over");
        });
        el.addEventListener("dragleave", () => el.classList.remove("sync-slot--over"));
        el.addEventListener("drop", (e) => {
            e.preventDefault();
            el.classList.remove("sync-slot--over");
            pairSyncField(e.dataTransfer.getData("text/plain"), el.dataset.qb);
        });
    });

    qbBox.querySelectorAll(".sync-unpair").forEach(btn => {
        btn.addEventListener("click", () => unpairSyncField(btn.dataset.qb));
    });

    // Ticks are board state, not DOM state: a re-render must not forget them.
    qbBox.querySelectorAll(".sync-check").forEach(cb => {
        cb.addEventListener("change", () => { SYNC.send[cb.dataset.qb] = cb.checked; });
    });
}

async function applySyncData() {
    const mappings = Object.entries(SYNC.links)
        .filter(([qb, lv]) => SYNC.send[qb] && !syncPairState(lv, qb).unchanged)
        .map(([qb, lv]) => ({ source: lv, target: qb }));

    if (!mappings.length) {
        $("#sync-error").textContent = "Nothing ticked to sync.";
        return;
    }

    const btn = $("#sync-apply");
    btn.disabled = true;
    btn.textContent = "Syncing…";
    try {
        const resp = await fetch(`/api/sync-sample-info/${encodeURIComponent(SYNC.labId)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mappings }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        if (!data.ok) {
            $("#sync-error").textContent = data.error || "Sync failed.";
            return;
        }
        hideModal("sync-modal");
        // The server re-renders the COA because sample info feeds it; bump the
        // cache-buster so the iframe picks up the new PDF rather than the old.
        _pdfVersion[SYNC.labId] = (_pdfVersion[SYNC.labId] || 0) + 1;
        showPDFPlaceholder(`Regenerating COA for ${SYNC.labId} after sync…`);
        setStatus(`Synced ${Object.keys(data.updated).length} field(s) to QBench — regenerating COA`);
        if (panesVisible.labvision) loadLabVisionData(SYNC.labId);
    } catch (e) {
        $("#sync-error").textContent = "Sync failed: " + e.message;
    } finally {
        btn.disabled = false;
        btn.textContent = "Sync & Regenerate";
    }
}


// ── Open in Lab Vision ────────────────────────────────────────────────

function openInLabVision() {
    const s = state.currentSample;
    if (!s || !CC.labVisionUrl) return;
    // LabVision's hash router: #/sample/{labId} opens sample detail.
    window.open(`${CC.labVisionUrl}/#/sample/${encodeURIComponent(s.lab_id)}`, "_blank");
}

function advanceToNext() {
    const samples = state.samples[state.currentTab] || [];
    if (!state.currentSample) return;

    const currentIdx = samples.findIndex(s => s.lab_id === state.currentSample.lab_id);
    for (let i = currentIdx + 1; i < samples.length; i++) {
        if (!["good", "bad"].includes(samples[i].status)) {
            selectSample(samples[i]);
            const item = document.querySelector(`.sample-item[data-lab="${samples[i].lab_id}"]`);
            if (item) item.scrollIntoView({ block: "nearest" });
            return;
        }
    }

    const remaining = samples.filter(s => !["good", "bad", "error"].includes(s.status));
    if (remaining.length === 0) {
        setStatus(`All ${state.currentTab} samples reviewed!`);
        $("#export-btn").disabled = false;
    }
}


// ══════════════════════════════════════════════════════════════════════
// Search & Custom Day
// ══════════════════════════════════════════════════════════════════════

async function handleSearch() {
    const query = $("#search-input").value.trim();
    if (!query) return;

    $("#search-btn").disabled = true;
    setStatus(`Searching for '${query}'...`);

    try {
        const resp = await fetch("/api/search", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            setStatus("Search failed: " + (data.error || `HTTP ${resp.status}`));
            return;
        }
        state.samples["Search"] = data.samples || [];
        switchTab("Search");
    } catch(e) {
        setStatus("Search failed: " + e.message);
    }

    $("#search-btn").disabled = false;
}

async function handleCustomLoad() {
    const dateStr = $("#custom-date").value;
    if (!dateStr) return;

    $("#custom-load-btn").disabled = true;
    setStatus(`Loading samples for ${dateStr}...`);

    try {
        const resp = await fetch("/api/custom-day", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ date: dateStr }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        switchTab("Custom Day");
    } catch(e) {
        setStatus("Custom day load failed: " + e.message);
    }

    setTimeout(() => { $("#custom-load-btn").disabled = false; }, 2000);
}


// ══════════════════════════════════════════════════════════════════════
// Regenerate
// ══════════════════════════════════════════════════════════════════════

async function handleRegenerate() {
    const s = state.currentSample;
    if (!s) return;

    _pdfVersion[s.lab_id] = (_pdfVersion[s.lab_id] || 0) + 1;
    showPDFPlaceholder(`Regenerating COA for ${s.lab_id}...`);
    $("#regen-btn").disabled = true;

    try {
        const resp = await fetch("/api/regenerate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tab: s.tab, lab_id: s.lab_id }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
    } catch(e) {
        setStatus("Regenerate failed: " + e.message);
    }

    setTimeout(() => { $("#regen-btn").disabled = false; }, 3000);
}


async function handleRegeneratePending() {
    const tab = state.currentTab;
    const btn = $("#regen-pending-btn");
    btn.disabled = true;

    // Bump every pending sample's cache-buster up front so any in-flight
    // iframe load for them is discarded rather than racing the new render.
    (state.samples[tab] || [])
        .filter(s => !["good", "bad"].includes(s.status))
        .forEach(s => { _pdfVersion[s.lab_id] = (_pdfVersion[s.lab_id] || 0) + 1; });

    try {
        const resp = await fetch("/api/regenerate-pending", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tab }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        if (data.ok) {
            setStatus(`Regenerating ${data.count} pending sample(s) on ${tab}…`);
            // Results arrive as sample_status SSE events — no polling, and the
            // reviewer can keep working while they come back.
            if (state.currentSample &&
                !["good", "bad"].includes(state.currentSample.status)) {
                showPDFPlaceholder(`Regenerating COA for ${state.currentSample.lab_id}...`);
            }
        } else {
            setStatus(data.error || "Regenerate pending failed.");
        }
    } catch(e) {
        setStatus("Regenerate pending failed: " + e.message);
    }

    setTimeout(() => { updateActionButtons(); }, 3000);
}


// ══════════════════════════════════════════════════════════════════════
// Export
// ══════════════════════════════════════════════════════════════════════

function showExportModal() {
    const allTabs = ["Yesterday", "Due Out", "Re-review", "Search", "Custom Day"];
    const container = $("#export-tabs-list");
    container.innerHTML = "";

    allTabs.forEach(tab => {
        const label = document.createElement("label");
        label.className = "checkbox-label";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = tab;
        cb.checked = true;
        label.appendChild(cb);
        label.appendChild(document.createTextNode(` ${tab}`));
        container.appendChild(label);
    });

    $("#export-result").classList.add("hidden");
    showModal("export-modal");
}

async function handleExport() {
    const tabs = [];
    $$("#export-tabs-list input[type=checkbox]:checked").forEach(cb => tabs.push(cb.value));
    const includeLinks = $("#export-links").checked;

    if (tabs.length === 0) { alert("Select at least one tab."); return; }

    try {
        const resp = await fetch("/api/export", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tabs, include_links: includeLinks }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();

        if (data.ok) {
            $("#export-filename").textContent = `Saved: ${data.filename}`;
            const linksDiv = $("#export-links-list");
            linksDiv.innerHTML = "";
            if (data.links && data.links.length > 0) {
                data.links.forEach(url => {
                    const a = document.createElement("a");
                    a.href = url;
                    a.target = "_blank";
                    a.textContent = "QBench Link";
                    a.style.display = "block";
                    a.style.marginTop = "4px";
                    linksDiv.appendChild(a);
                });
                navigator.clipboard.writeText(data.links.join("\n")).catch(() => {});
            }
            $("#export-result").classList.remove("hidden");
            setStatus(`Exported to ${data.filename}`);
        } else {
            alert(data.error || "Export failed");
        }
    } catch(e) {
        alert("Export failed: " + e.message);
    }
}


// ══════════════════════════════════════════════════════════════════════
// Keyboard Shortcuts
// ══════════════════════════════════════════════════════════════════════

function handleKeyboard(e) {
    const tag = e.target.tagName.toLowerCase();
    if (tag === "input" || tag === "textarea") return;

    const samples = state.samples[state.currentTab] || [];
    const currentIdx = state.currentSample
        ? samples.findIndex(s => s.lab_id === state.currentSample.lab_id)
        : -1;

    switch(e.key) {
        case "Tab":
            e.preventDefault();
            const allTabs = ["Yesterday", "Due Out", "Re-review", "Search", "Custom Day"];
            const idx = allTabs.indexOf(state.currentTab);
            switchTab(allTabs[(idx + 1) % allTabs.length]);
            break;

        case "ArrowUp":
            e.preventDefault();
            if (currentIdx > 0) {
                selectSample(samples[currentIdx - 1]);
                scrollSampleIntoView(samples[currentIdx - 1].lab_id);
            }
            break;

        case "ArrowDown":
            e.preventDefault();
            if (currentIdx < samples.length - 1) {
                selectSample(samples[currentIdx + 1]);
                scrollSampleIntoView(samples[currentIdx + 1].lab_id);
            }
            break;

        case "ArrowRight":
            e.preventDefault();
            if (!$("#good-btn").disabled) markGood();
            break;

        case "ArrowLeft":
            e.preventDefault();
            if (!$("#bad-btn").disabled) markBad();
            break;

        case "u":
        case "U":
            e.preventDefault();
            if (!$("#uncheck-btn")?.disabled) handleUncheck();
            break;
    }
}

function scrollSampleIntoView(labId) {
    const item = document.querySelector(`.sample-item[data-lab="${labId}"]`);
    if (item) item.scrollIntoView({ block: "nearest" });
}


// ══════════════════════════════════════════════════════════════════════
// Restore state on page refresh
// ══════════════════════════════════════════════════════════════════════

async function restoreAllTabs() {
    const allTabs = ["Yesterday", "Due Out", "Re-review", "Search", "Custom Day"];
    await Promise.all(allTabs.map(tab => loadTab(tab)));
    renderSampleList();
    $("#export-btn").disabled = false;
    $("#good-links-btn").disabled = false;
}


// ══════════════════════════════════════════════════════════════════════
// Good Samples (open QBench links)
// ══════════════════════════════════════════════════════════════════════

function showGoodModal() {
    const allTabs = ["Yesterday", "Due Out", "Re-review", "Search", "Custom Day"];
    const container = $("#good-tabs-list");
    container.innerHTML = "";

    allTabs.forEach(tab => {
        const label = document.createElement("label");
        label.className = "checkbox-label";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = tab;
        cb.checked = ["Yesterday", "Due Out", "Custom Day"].includes(tab);
        label.appendChild(cb);
        label.appendChild(document.createTextNode(` ${tab}`));
        container.appendChild(label);
    });

    $("#good-links-result").classList.add("hidden");
    showModal("good-modal");
}

async function handleOpenGoodLinks() {
    const tabs = [];
    $$("#good-tabs-list input[type=checkbox]:checked").forEach(cb => tabs.push(cb.value));
    if (tabs.length === 0) { alert("Select at least one tab."); return; }

    try {
        const resp = await fetch("/api/good-links", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tabs }),
        });
        if (resp.status === 401) { triggerTimeout(); return; }
        const data = await resp.json();
        const links = data.links || [];

        if (links.length === 0) {
            const result = $("#good-links-result");
            result.innerHTML = '<p style="color:#999;">No Good samples found in the selected tabs.</p>';
            result.classList.remove("hidden");
            return;
        }

        links.forEach(l => window.open(l.url, "_blank"));

        const result = $("#good-links-result");
        result.innerHTML = links.map(l =>
            `<p>${l.tab}: ${l.count} good sample(s) — <a href="${l.url}" target="_blank">link</a></p>`
        ).join("");
        result.classList.remove("hidden");

        const allUrls = links.map(l => l.url).join("\n");
        navigator.clipboard.writeText(allUrls).catch(() => {});
        setStatus(`Opened ${links.length} QBench link(s)`);
    } catch(e) {
        alert("Failed: " + e.message);
    }
}


// ══════════════════════════════════════════════════════════════════════
// Dark Mode
// ══════════════════════════════════════════════════════════════════════

// Theme: "system" (follow OS), "dark" (force), "light" (force).
// JS computes the *effective* mode (resolving "system" against
// prefers-color-scheme) and toggles a single `body.dark` class — that way the
// CSS only needs one dark palette block and existing `body.dark .x` selectors
// keep working. `force-dark`/`force-light` are additional hints if any rule
// ever needs to distinguish user-pinned from system-derived.
const THEME_ORDER = ["system", "dark", "light"];
const THEME_LABEL = { system: "Auto", dark: "Dark", light: "Light" };

function initTheme() {
    // Migrate the old `dark`=true|false key the first time.
    let stored = localStorage.getItem("theme");
    if (!stored) {
        const legacy = localStorage.getItem("dark");
        stored = legacy === "true" ? "dark" : "system";
        localStorage.setItem("theme", stored);
    }
    applyTheme(stored);

    // React to live system theme changes when the user is on "system".
    if (window.matchMedia) {
        const mql = window.matchMedia("(prefers-color-scheme: dark)");
        const onChange = () => {
            if ((localStorage.getItem("theme") || "system") === "system") {
                // Re-apply so the button label refreshes its hint.
                applyTheme("system");
            }
        };
        if (mql.addEventListener) mql.addEventListener("change", onChange);
        else if (mql.addListener) mql.addListener(onChange); // Safari <14
    }
}

function applyTheme(mode) {
    const body = document.body;
    localStorage.setItem("theme", mode);

    const sysDark = !!(window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    const effectiveDark = (mode === "dark") || (mode === "system" && sysDark);

    // `body.dark` drives the entire dark palette in CSS — keeping it as the
    // single source of truth means the existing `body.dark .x` rules don't
    // need to grow new variants. `force-dark` / `force-light` exist only so
    // CSS can render slightly different button affordances if it wants to.
    body.classList.toggle("dark", effectiveDark);
    body.classList.toggle("force-dark", mode === "dark");
    body.classList.toggle("force-light", mode === "light");

    const btn = $("#dark-toggle");
    if (btn) {
        if (mode === "system") {
            btn.textContent = "Auto " + (sysDark ? "(Dark)" : "(Light)");
        } else {
            btn.textContent = THEME_LABEL[mode];
        }
    }

    // Sync the login-screen pip strip (Auto / Light / Dark).
    document.querySelectorAll(".theme-pip").forEach(pip => {
        pip.classList.toggle("active", pip.dataset.theme === mode);
    });
}

function cycleTheme() {
    const current = localStorage.getItem("theme") || "system";
    const next = THEME_ORDER[(THEME_ORDER.indexOf(current) + 1) % THEME_ORDER.length];
    applyTheme(next);
}


// ══════════════════════════════════════════════════════════════════════
// Antigravity — interactive dot grid for the portal login screen
// ══════════════════════════════════════════════════════════════════════
//
// A canvas of dots arranged on a coarse grid. The cursor (or finger) acts
// as a gravity well: each dot is repulsed from the pointer with a falloff,
// and springs back to its rest position when the pointer leaves the field.
// Idle dots also breathe in opacity so the field never feels dead.
//
// The system is gated:
//   · Runs only while #portal-login-modal is visible — RAF stops when the
//     modal is hidden (login complete, timeout, etc).
//   · Pauses when the tab loses focus.
//   · Becomes a static dot field under prefers-reduced-motion.
//
// Colors come from CSS vars so the field follows the active theme
// automatically; no JS palette swap needed when dark/light flips.

function initAntigravity() {
    const canvas = document.getElementById("antigravity-canvas");
    // The canvas is on screen while the main UI is hidden — i.e. through
    // portal-login → boot-splash → review-mode-modal — and stops once
    // #app is rendered. Observing #app's `hidden` class is the single
    // signal that flips the field on/off.
    const app    = document.getElementById("app");
    if (!canvas || !app) return;

    const ctx = canvas.getContext("2d", { alpha: true });
    const reducedMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // Field parameters — tuned for a 1440×900-ish viewport but scale-aware.
    //
    // The visual model is a "flashlight" over a dot field: at rest every
    // cell is a faint small dot, and the dot grows + brightens as the
    // cursor approaches. Size and alpha both ramp with proximity, so
    // distance from the mouse reads as dimming.
    const SPACING         = 22;   // px between rest positions
    const DOT_MIN_RADIUS  = 1.4;  // px — resting dot far from the cursor
    const DOT_MAX_RADIUS  = 5;    // px — dot right under the cursor
    const MIN_ALPHA       = 0.20; // resting opacity — softly present, readable
    const MAX_ALPHA       = 1.00; // full opacity right under the cursor
    // Visual and physics radii are decoupled: the cursor "lights up" a
    // huge area of the field (REVEAL_RADIUS) but only physically pushes
    // dots near it (PUSH_RADIUS). That separation lets the reveal feel
    // expansive without the field feeling rubbery.
    const PUSH_RADIUS     = 600;  // px — physics push reach
    const REVEAL_RADIUS   = 1200; // px — visual reveal reach
    const MAX_PUSH        = 50;   // peak displacement at the cursor
    const SPRING          = 0.075;// pull-back to rest position
    const DAMPING         = 0.84; // velocity decay
    const BREATH_PERIOD   = 4200; // ms for the idle breathing cycle (subtle
                                  // size-only modulation so ghost dots
                                  // aren't completely dead)

    // ── Wave pulses (spawned on click) ──────────────────────────────
    // A click spawns an expanding ring centered at the click point. Each
    // dot the ring sweeps over gets a temporary proximity boost — the
    // bloom and wave share the same proximity → alpha+radius+halo path,
    // so a swept dot looks identical to a cursor-adjacent dot while the
    // wave is passing through it.
    const PULSE_LIFETIME    = 1400; // ms — pulse fully decays after this
    const PULSE_SPEED       = 1.4;  // px/ms — how fast the ring expands
    const PULSE_THICKNESS   = 260;  // px — width of the bright band (extra
                                    // wide so the traveling emoji ring
                                    // reads as a band, not a line)
    const PULSE_AMPLITUDE   = 0.95; // peak proximity boost at the ring center
    // While the ring sweeps a dot, the dot crossfades into one of these
    // emojis and back out as the band moves on. Each cell's emoji is
    // assigned deterministically at build time so a dot never flickers
    // between different emojis from frame to frame.
    const EMOJIS            = ["✨", "🎉", "⭐", "💥", "🌈", "🔥"];
    const EMOJI_MIN_SIZE    = 12;   // px — glyph size at the band's edge
    const EMOJI_MAX_SIZE    = 26;   // px — glyph size at the band's center

    // Pre-rendered emoji sprites, one per EMOJIS entry. Drawing emoji with
    // fillText per dot per frame re-parses the font and re-rasterizes the
    // color glyph at every unique fractional size, which made waves lag
    // badly. A drawImage blit of a pre-rendered sprite is cheap. Sprites
    // render at 2× the max display size so downscales stay crisp on HiDPI.
    const SPRITE_SIZE       = 64;   // px — sprite canvas edge
    const SPRITE_FONT_RATIO = 0.8;  // glyph em size within the sprite, the
                                    // rest is padding so nothing clips
    const emojiSprites = EMOJIS.map(e => {
        const c = document.createElement("canvas");
        c.width = SPRITE_SIZE;
        c.height = SPRITE_SIZE;
        const sctx = c.getContext("2d");
        sctx.textAlign = "center";
        sctx.textBaseline = "middle";
        // Default opaque fillStyle: Chromium multiplies fillStyle's alpha
        // into color-emoji glyphs, so it must stay opaque here.
        sctx.font = `${SPRITE_SIZE * SPRITE_FONT_RATIO}px sans-serif`;
        sctx.fillText(e, SPRITE_SIZE / 2, SPRITE_SIZE / 2);
        return c;
    });
    const WAVE_MAX_PUSH     = 60;   // peak outward velocity kick from the
                                    // wave (slightly stronger than the
                                    // cursor's MAX_PUSH so clicks feel
                                    // punchy — same spring-back behavior)

    // ── Ball-pit physics (the login screen's beaker button) ─────────
    // Three modes. "grid": normal spring-to-rest field. "drop": spring
    // off, gravity on — balls bounce off all four screen edges and off
    // each other. "return": gravity off — every ball is sucked back to
    // its rest slot black-hole style: lazy attraction at range, fierce
    // pull up close, so it overshoots and loses energy over tightening
    // orbits until it seats.
    const GRAVITY           = 0.32; // px/frame² downward while dropped
    const BALL_RADIUS       = 4;    // px — fixed render + collision radius
    const BALL_ALPHA_FLOOR  = 0.5;  // physics balls stay clearly visible
    const RESTITUTION       = 0.75; // energy kept on wall + ball bounces
    const FLOOR_FRICTION    = 0.97; // x-velocity decay while on the floor
    const RETURN_PULL_FAR   = 0.12; // baseline slot attraction at range —
                                    // low enough to read as a lazy drift,
                                    // high enough that a cross-screen trip
                                    // takes ~3-4s, not ten
    const RETURN_PULL_NEAR  = 14;   // ~1/r term — the black-hole whip-in
    const RETURN_DAMPING    = 0.96; // light — lets balls overshoot + orbit
    const RETURN_NEAR_ZONE  = 40;   // px — inside this "event horizon" the
                                    // pull hands over to the grid spring.
                                    // A 1/r pull never weakens at the slot,
                                    // so balls would orbit forever instead
                                    // of seating; the spring converges.
    const SEAT_DIST         = 0.7;  // px — close enough to snap home…
    const SEAT_SPEED        = 0.5;  // px/frame — …while moving this slowly
    const CELL              = BALL_RADIUS * 2;  // spatial-hash cell edge

    let dpr = Math.max(1, window.devicePixelRatio || 1);
    let dots = [];
    let pulses = [];
    let mode = "grid";      // "grid" | "drop" | "return"
    let seatedCount = 0;    // return-mode progress; all seated → "grid"
    let pointer = { x: -9999, y: -9999, active: false };
    let rafId = null;
    let running = false;
    let dotColorRgb = "15, 23, 42";   // overridden from CSS var on resize

    // Read the accent color out of the CSS var and convert "#0e7490" to "14,116,144"
    // so we can vary alpha per-dot. Falls back to slate if anything fails.
    function refreshDotColor() {
        const styles = getComputedStyle(document.body);
        const raw = (styles.getPropertyValue("--antigravity-dot") || "").trim();
        if (!raw) return;
        const m = raw.match(/^#([0-9a-f]{6})$/i);
        if (m) {
            const n = parseInt(m[1], 16);
            dotColorRgb = `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
            return;
        }
        const rgb = raw.match(/(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
        if (rgb) dotColorRgb = `${rgb[1]}, ${rgb[2]}, ${rgb[3]}`;
    }

    function rebuildField() {
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        canvas.width  = Math.floor(w * dpr);
        canvas.height = Math.floor(h * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        dots = [];
        // Inset so the outermost dots aren't clipped at the edge.
        const offsetX = (w % SPACING) / 2;
        const offsetY = (h % SPACING) / 2;
        for (let y = offsetY; y < h; y += SPACING) {
            for (let x = offsetX; x < w; x += SPACING) {
                // A tiny jitter (deterministic per-cell) keeps the grid from
                // looking like graph paper without losing structure.
                const jx = ((x * 13 + y * 7) % 5) - 2;
                const jy = ((x * 11 + y * 17) % 5) - 2;
                // Well-mixed deterministic hash so neighboring cells don't
                // form visible stripes of the same emoji.
                const hash = Math.abs(Math.sin(x * 12.9898 + y * 78.233) * 43758.5453);
                dots.push({
                    rx: x + jx, ry: y + jy,  // rest position
                    x: x + jx,  y: y + jy,   // current position
                    vx: 0, vy: 0,
                    // Phase offset for the breathing cycle so dots aren't synced.
                    phase: (x * 0.013 + y * 0.017) % (Math.PI * 2),
                    emojiIdx: Math.floor(hash) % EMOJIS.length,
                    seated: false,      // return-mode: locked back into slot
                    prox: 0, waveT: 0,  // per-frame stash for the render pass
                });
            }
        }
        // A rebuild (resize) re-lays the grid, so any ball-pit in progress
        // is abandoned and the field starts back in normal grid mode.
        seatedCount = 0;
        syncPhysicsMode("grid");
        refreshDotColor();
    }

    function tick(now) {
        if (!running) return;
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        ctx.clearRect(0, 0, w, h);

        const breathT = (now % BREATH_PERIOD) / BREATH_PERIOD * Math.PI * 2;

        // Expire pulses whose ring has fully decayed. Done in-place so we
        // don't reallocate the array every frame when nothing's clicking.
        if (pulses.length) {
            pulses = pulses.filter(p => now - p.startTime < PULSE_LIFETIME);
        }
        // Pre-compute each active pulse's current ring radius + life
        // fraction so the per-dot inner loop stays tight.
        const livePulses = pulses.map(p => {
            const age = now - p.startTime;
            return {
                x: p.x, y: p.y,
                ringRadius: age * PULSE_SPEED,
                lifeT: 1 - age / PULSE_LIFETIME,  // 1 at birth → 0 at death
            };
        });

        for (let i = 0; i < dots.length; i++) {
            const d = dots[i];

            // cursorProximity ∈ [0, 1] — how much this dot is inside the
            // cursor's sphere. waveBoost ∈ [0, 1] — how much the dot is
            // currently being lit by an active wave ring. The combined
            // proximity drives alpha, radius, AND bloom — so a swept dot
            // looks identical to a cursor-adjacent dot during the sweep.
            let cursorProximity = 0;
            let waveBoost = 0;

            if (pointer.active) {
                const dx = d.x - pointer.x;
                const dy = d.y - pointer.y;
                const dist = Math.hypot(dx, dy);
                // Physics push — narrow-ish radius, quadratic falloff.
                // During the black-hole return it only applies to balls
                // that have already seated: the push can hold a homing
                // ball in equilibrium ~35px off its slot indefinitely, so
                // a parked cursor would keep the field from ever finishing
                // its reseat. The reveal glow below still follows the
                // cursor for every ball.
                if ((mode !== "return" || d.seated) && dist < PUSH_RADIUS && dist > 0.001) {
                    const t = 1 - dist / PUSH_RADIUS;
                    const force = MAX_PUSH * t * t;
                    const nx = dx / dist;
                    const ny = dy / dist;
                    d.vx += (nx * force) * 0.06;
                    d.vy += (ny * force) * 0.06;
                }
                // Visual reveal — wide radius, quadratic falloff.
                if (dist < REVEAL_RADIUS) {
                    const tv = 1 - dist / REVEAL_RADIUS;
                    cursorProximity = tv * tv;
                }
            }

            // Wave ring sweep — dot is "lit" AND shoved outward when
            // it's near the current ring radius. Visual boost and the
            // physics kick share the same falloff shape (quadratic with
            // distance from ring center × linear with pulse age) so the
            // dot's bounce visually matches the brightness pulse.
            for (let pi = 0; pi < livePulses.length; pi++) {
                const p = livePulses[pi];
                const dx = d.x - p.x;
                const dy = d.y - p.y;
                const dist = Math.hypot(dx, dy);
                const distFromRing = Math.abs(dist - p.ringRadius);
                if (distFromRing < PULSE_THICKNESS) {
                    const ringT = 1 - distFromRing / PULSE_THICKNESS;
                    const boost = ringT * ringT * p.lifeT * PULSE_AMPLITUDE;
                    if (boost > waveBoost) waveBoost = boost;
                    // Outward kick — same shape as the cursor's push, but
                    // sourced from the pulse center. The existing spring
                    // + damping integration below pulls the dot back to
                    // rest after the ring passes, producing the "bounce".
                    if (dist > 0.001) {
                        const force = WAVE_MAX_PUSH * ringT * ringT * p.lifeT;
                        const nx = dx / dist;
                        const ny = dy / dist;
                        d.vx += nx * force * 0.06;
                        d.vy += ny * force * 0.06;
                    }
                }
            }

            // Combined proximity: cursor field OR wave pulse, whichever
            // is brighter. Bloom follows this combined value. Stashed on
            // the dot because rendering happens in a second pass, after
            // ball-ball collisions have settled final positions.
            d.prox = Math.max(cursorProximity, waveBoost);
            d.waveT = waveBoost;

            if (mode === "grid") {
                // Spring toward rest.
                d.vx += (d.rx - d.x) * SPRING;
                d.vy += (d.ry - d.y) * SPRING;
                d.vx *= DAMPING;
                d.vy *= DAMPING;
            } else if (mode === "drop") {
                d.vy += GRAVITY;
            } else if (d.seated) {
                // Seated balls act like normal grid dots while the rest
                // of the field is still flying home: cursor pushes and
                // wave kicks work on them, and the spring pulls them back
                // to the slot. They stay counted as seated, so a wobble
                // here can't stall the return's completion.
                d.vx += (d.rx - d.x) * SPRING;
                d.vy += (d.ry - d.y) * SPRING;
                d.vx *= DAMPING;
                d.vy *= DAMPING;
            } else {
                // Black-hole return: pull strength ramps ~1/r as the ball
                // nears its slot, so it whips in fast and overshoots.
                const hx = d.rx - d.x;
                const hy = d.ry - d.y;
                const hd = Math.hypot(hx, hy);
                if (hd < SEAT_DIST && Math.abs(d.vx) + Math.abs(d.vy) < SEAT_SPEED) {
                    d.x = d.rx; d.y = d.ry;
                    d.vx = 0; d.vy = 0;
                    d.seated = true;
                    seatedCount++;
                    if (seatedCount === dots.length) syncPhysicsMode("grid");
                } else if (hd < RETURN_NEAR_ZONE) {
                    // Inside the event horizon the grid spring takes over:
                    // the arrival speed still carries the ball past its
                    // slot, but spring + heavy damping decay the
                    // oscillation until it's slow enough to seat.
                    d.vx = (d.vx + hx * SPRING) * DAMPING;
                    d.vy = (d.vy + hy * SPRING) * DAMPING;
                } else {
                    const pull = RETURN_PULL_FAR + RETURN_PULL_NEAR / hd;
                    d.vx = (d.vx + (hx / hd) * pull) * RETURN_DAMPING;
                    d.vy = (d.vy + (hy / hd) * pull) * RETURN_DAMPING;
                }
            }
            d.x += d.vx;
            d.y += d.vy;

            // All four screen edges are walls — drop mode only. The wall
            // margin is BALL_RADIUS, but grid offset + jitter can put a
            // rest slot within ~2px of an edge; during the return the
            // clamp would hold those balls just outside seating distance
            // forever, so homing balls ignore the walls.
            if (mode === "drop") {
                if (d.x < BALL_RADIUS) {
                    d.x = BALL_RADIUS;
                    if (d.vx < 0) d.vx = -d.vx * RESTITUTION;
                } else if (d.x > w - BALL_RADIUS) {
                    d.x = w - BALL_RADIUS;
                    if (d.vx > 0) d.vx = -d.vx * RESTITUTION;
                }
                if (d.y < BALL_RADIUS) {
                    d.y = BALL_RADIUS;
                    if (d.vy < 0) d.vy = -d.vy * RESTITUTION;
                } else if (d.y > h - BALL_RADIUS) {
                    d.y = h - BALL_RADIUS;
                    if (d.vy > 0) d.vy = -d.vy * RESTITUTION;
                    // Rolling friction so floor piles settle instead of
                    // sliding forever.
                    d.vx *= FLOOR_FRICTION;
                }
            }
        }

        // Ball-ball collisions once all positions are integrated — drop
        // mode only. During the return, thousands of balls funnel through
        // the same corridors; colliding there thermalizes the flock into
        // a mosh pit that takes forever to seat. Black-hole returns
        // stream through each other instead.
        if (mode === "drop") resolveCollisions(w, h);

        // ── Render pass ──────────────────────────────────────────────
        for (let i = 0; i < dots.length; i++) {
            const d = dots[i];

            // Alpha interpolates from "ghost" to "full" via proximity, then
            // gets a subtle breath modulation so idle cells aren't fully
            // dead. Size ramps with the same proximity, so dots both dim
            // and shrink with distance from the cursor. Physics balls use
            // a fixed radius and an alpha floor — a bouncing ball that
            // fades out mid-air reads as a glitch.
            // Seated balls render grid-style mid-return, so "landed" dots
            // visibly rejoin the field while the rest are still in flight.
            const ballStyle = mode !== "grid" && !d.seated;
            const breath = 1 + 0.08 * Math.sin(breathT + d.phase);
            let alpha = (MIN_ALPHA + (MAX_ALPHA - MIN_ALPHA) * d.prox) * breath;
            if (ballStyle && alpha < BALL_ALPHA_FLOOR) alpha = BALL_ALPHA_FLOOR;

            // Crossfade dot ↔ emoji on the wave band: waveT rises toward
            // 1 as the ring center crosses the dot, so the dot melts into
            // its emoji and back out as the band moves on. Steep
            // smoothstep rather than a linear fade: color emoji at
            // partial alpha over a dark background read muddy-brown, so
            // the band is mostly "fully on" with a narrow fade edge. The
            // same curve fades the whole ring out as the pulse dies
            // (peak waveT sinks below the 0.35 ceiling).
            let emojiT = (d.waveT - 0.08) / 0.27;
            emojiT = emojiT < 0 ? 0 : emojiT > 1 ? 1 : emojiT;
            emojiT = emojiT * emojiT * (3 - 2 * emojiT);

            if (emojiT < 0.999) {
                const r = ballStyle
                    ? BALL_RADIUS
                    : DOT_MIN_RADIUS + (DOT_MAX_RADIUS - DOT_MIN_RADIUS) * d.prox;
                ctx.fillStyle = `rgba(${dotColorRgb}, ${(alpha * (1 - emojiT)).toFixed(3)})`;
                ctx.beginPath();
                ctx.arc(d.x, d.y, r, 0, Math.PI * 2);
                ctx.fill();
            }
            if (emojiT > 0.02) {
                // The wave is its own light source: opacity follows emojiT
                // directly rather than the field's distance-dimmed alpha,
                // so emojis stay vivid even far from the cursor.
                // The sprite's glyph fills SPRITE_FONT_RATIO of its box, so
                // scale the destination box up to keep the visible glyph at
                // the intended size.
                const size = EMOJI_MIN_SIZE + (EMOJI_MAX_SIZE - EMOJI_MIN_SIZE) * emojiT;
                const box  = size / SPRITE_FONT_RATIO;
                ctx.globalAlpha = emojiT;
                ctx.drawImage(emojiSprites[d.emojiIdx],
                              d.x - box / 2, d.y - box / 2, box, box);
                ctx.globalAlpha = 1;
            }
        }

        // When reduced motion is on, render once and stop.
        if (reducedMotion) { running = false; return; }
        rafId = requestAnimationFrame(tick);
    }

    function start() {
        if (running) return;
        running = true;
        rebuildField();
        // Draw a single frame even under reduced-motion so it's not blank.
        rafId = requestAnimationFrame(tick);
    }

    function stop() {
        running = false;
        if (rafId) cancelAnimationFrame(rafId);
        rafId = null;
        ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    }

    // ── Ball-ball collisions via a uniform spatial hash ──────────────
    // Flat Int32Array linked-list buckets, rebuilt each physics frame:
    // head[cell] → first dot index in that cell, next[i] → next dot in
    // the same cell. Each ball only tests its 3×3 cell neighborhood
    // (cells are one ball-diameter wide), so the pass stays O(n) instead
    // of O(n²) across the ~3–4k balls on screen.
    let hashHead = null;
    let hashNext = null;
    let hashCols = 0;
    let hashRows = 0;

    function resolveCollisions(w, h) {
        const minD = BALL_RADIUS * 2;
        hashCols = Math.max(1, Math.ceil(w / CELL));
        hashRows = Math.max(1, Math.ceil(h / CELL));
        if (!hashHead || hashHead.length < hashCols * hashRows) {
            hashHead = new Int32Array(hashCols * hashRows);
        }
        if (!hashNext || hashNext.length < dots.length) {
            hashNext = new Int32Array(dots.length);
        }
        hashHead.fill(-1, 0, hashCols * hashRows);
        for (let i = 0; i < dots.length; i++) {
            const d = dots[i];
            let cx = (d.x / CELL) | 0;
            let cy = (d.y / CELL) | 0;
            if (cx < 0) cx = 0; else if (cx >= hashCols) cx = hashCols - 1;
            if (cy < 0) cy = 0; else if (cy >= hashRows) cy = hashRows - 1;
            const cell = cy * hashCols + cx;
            hashNext[i] = hashHead[cell];
            hashHead[cell] = i;
        }

        for (let i = 0; i < dots.length; i++) {
            const a = dots[i];
            let cx = (a.x / CELL) | 0;
            let cy = (a.y / CELL) | 0;
            if (cx < 0) cx = 0; else if (cx >= hashCols) cx = hashCols - 1;
            if (cy < 0) cy = 0; else if (cy >= hashRows) cy = hashRows - 1;
            const y0 = cy > 0 ? cy - 1 : 0;
            const y1 = cy < hashRows - 1 ? cy + 1 : cy;
            const x0 = cx > 0 ? cx - 1 : 0;
            const x1 = cx < hashCols - 1 ? cx + 1 : cx;
            for (let ny = y0; ny <= y1; ny++) {
                for (let nx = x0; nx <= x1; nx++) {
                    for (let j = hashHead[ny * hashCols + nx]; j !== -1; j = hashNext[j]) {
                        if (j <= i) continue;  // handle each pair once
                        const b = dots[j];
                        const dx = b.x - a.x;
                        const dy = b.y - a.y;
                        const d2 = dx * dx + dy * dy;
                        if (d2 === 0 || d2 >= minD * minD) continue;
                        const dist = Math.sqrt(d2);
                        const nxu = dx / dist;
                        const nyu = dy / dist;
                        // Push overlapping balls apart.
                        const half = (minD - dist) / 2;
                        a.x -= nxu * half; a.y -= nyu * half;
                        b.x += nxu * half; b.y += nyu * half;
                        // Equal-mass impulse along the normal, only when
                        // the pair is approaching.
                        const rvn = (b.vx - a.vx) * nxu + (b.vy - a.vy) * nyu;
                        if (rvn < 0) {
                            const imp = -(1 + RESTITUTION) * rvn / 2;
                            a.vx -= imp * nxu; a.vy -= imp * nyu;
                            b.vx += imp * nxu; b.vy += imp * nyu;
                        }
                    }
                }
            }
        }
    }

    // ── Physics (ball-pit) toggle button ─────────────────────────────
    const physicsBtn = document.getElementById("physics-toggle");

    function syncPhysicsMode(next) {
        mode = next;
        if (!physicsBtn) return;
        // Button stays lit through the return flight; it goes quiet only
        // once every ball has seated and the field is a grid again.
        physicsBtn.classList.toggle("active", mode !== "grid");
        physicsBtn.setAttribute("aria-pressed", mode !== "grid" ? "true" : "false");
    }

    if (physicsBtn) {
        if (reducedMotion) {
            // The field is a static single frame under reduced motion —
            // a physics toggle would do nothing.
            physicsBtn.hidden = true;
        } else {
            physicsBtn.addEventListener("click", () => {
                if (mode === "drop") {
                    seatedCount = 0;
                    for (let i = 0; i < dots.length; i++) dots[i].seated = false;
                    syncPhysicsMode("return");
                } else {
                    // Clear flags left over from a completed return —
                    // seated balls render grid-style, and a fresh drop
                    // should look like falling balls, not falling dots.
                    for (let i = 0; i < dots.length; i++) dots[i].seated = false;
                    syncPhysicsMode("drop");
                }
            });
        }
    }

    // Pointer tracking — listen on window so the canvas catches mouse
    // moves regardless of which modal is on top. The canvas itself has
    // pointer-events: none so it never steals clicks.
    window.addEventListener("pointermove", (e) => {
        if (!running) return;
        pointer.x = e.clientX;
        pointer.y = e.clientY;
        pointer.active = true;
    });
    window.addEventListener("pointerout", (e) => {
        // pointerout with no relatedTarget = left the viewport
        if (!e.relatedTarget) pointer.active = false;
    });
    // Click anywhere → spawn a wave. The canvas has pointer-events: none,
    // so this listener doesn't steal clicks from the login form, the
    // theme pips, or the picker pills — it just adds a visual ripple at
    // the click coordinate.
    window.addEventListener("pointerdown", (e) => {
        if (!running) return;
        // No shockwave from the physics button itself — it would blast
        // the balls it's about to drop or call home.
        if (e.target.closest && e.target.closest("#physics-toggle")) return;
        pulses.push({ x: e.clientX, y: e.clientY, startTime: performance.now() });
    });

    // Resize + DPR change handling.
    const onResize = () => {
        dpr = Math.max(1, window.devicePixelRatio || 1);
        if (running) rebuildField();
    };
    window.addEventListener("resize", onResize);

    // Theme flips change --antigravity-dot. Re-read after the dark class toggles.
    const themeObserver = new MutationObserver(refreshDotColor);
    themeObserver.observe(document.body, { attributes: true, attributeFilter: ["class"] });

    // Tab visibility — don't burn CPU when the user is elsewhere.
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) stop();
        else if (app.classList.contains("hidden")) start();
    });

    // Run while the main #app is hidden (i.e. through portal-login →
    // boot-splash → review-mode-modal); stop the moment #app shows up.
    const appObserver = new MutationObserver(() => {
        if (app.classList.contains("hidden")) start();
        else stop();
    });
    appObserver.observe(app, { attributes: true, attributeFilter: ["class"] });

    // Kick off if #app is hidden at boot (the common case — login flow ahead).
    if (app.classList.contains("hidden")) start();
}


// ══════════════════════════════════════════════════════════════════════
// Server Restart
// ══════════════════════════════════════════════════════════════════════

let _restartOldPid = null;
let _restartPollCount = 0;

async function handleRestartServer() {
    // Populate the confirm modal with live uptime + active-user counts, then
    // show it. The actual restart only fires when the user clicks the
    // confirm button inside the modal — see initRestartConfirm() for wiring.
    const uptimeEl  = $("#restart-uptime");
    const activeEl  = $("#restart-active-users");
    if (uptimeEl) uptimeEl.textContent = "…";
    if (activeEl) activeEl.textContent = "…";
    try {
        const resp = await fetch("/api/server-info");
        const data = await resp.json();
        if (uptimeEl) uptimeEl.textContent = data.uptime;
        if (activeEl) activeEl.textContent = String(data.active_users);
    } catch (e) {
        if (uptimeEl) uptimeEl.textContent = "unknown";
        if (activeEl) activeEl.textContent = "unknown";
    }
    showModal("restart-confirm-modal");
}

function initRestartConfirm() {
    const modal  = $("#restart-confirm-modal");
    const cancel = $("#restart-cancel");
    const ok     = $("#restart-confirm-ok");
    if (!modal || !ok) return;

    cancel?.addEventListener("click", () => hideModal("restart-confirm-modal"));
    modal.addEventListener("click", (e) => {
        if (e.target === modal) hideModal("restart-confirm-modal");
    });
    ok.addEventListener("click", () => {
        hideModal("restart-confirm-modal");
        triggerServerRestart();
    });
}

async function triggerServerRestart() {
    const btn = $("#restart-btn");
    if (btn) {
        btn.disabled = true;
        btn.textContent = "Restarting...";
    }
    setStatus("Application is restarting...");

    _restartOldPid = null;
    _restartPollCount = 0;

    try {
        const resp = await fetch("/api/restart", { method: "POST" });
        const data = await resp.json();
        _restartOldPid = data.old_pid || null;
    } catch(e) { /* expected — server is shutting down */ }

    // Close SSE so it doesn't interfere with reconnection
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }

    // Wait for server to die + launcher restart + Cloudflare tunnel reconnect
    setStatus("Application shutting down... waiting for restart...");
    setTimeout(() => pollForRestart(), 6000);
}

function pollForRestart() {
    _restartPollCount++;
    const elapsed = _restartPollCount * 2;
    setStatus(`Waiting for application... (${elapsed}s)`);

    // Use /api/health (no auth, lightweight) with cache-busting param
    fetch("/api/health?_t=" + Date.now(), { method: "GET", cache: "no-store" })
        .then(resp => {
            if (!resp.ok) {
                setTimeout(pollForRestart, 2000);
                return;
            }
            return resp.json();
        })
        .then(data => {
            if (!data) return; // already scheduled retry above
            // If we know the old PID, wait until we see a DIFFERENT PID (new server)
            if (_restartOldPid && data.pid === _restartOldPid) {
                // Still the old server (hasn't died yet) — keep waiting
                setTimeout(pollForRestart, 2000);
                return;
            }
            // New server is up — reload
            setStatus("Application is back! Reloading...");
            setTimeout(() => location.reload(), 500);
        })
        .catch(() => {
            // Server not reachable (or Cloudflare tunnel reconnecting) — keep polling
            if (_restartPollCount > 30) {
                // 60+ seconds — something is wrong
                setStatus("Application hasn't come back after 60s. Try refreshing manually.");
                const btn = $("#restart-btn");
                btn.disabled = false;
                btn.textContent = "\u21bb Restart";
                return;
            }
            setTimeout(pollForRestart, 2000);
        });
}


// ══════════════════════════════════════════════════════════════════════
// Utilities
// ══════════════════════════════════════════════════════════════════════

function setStatus(msg) {
    const el = $("#status-label");
    if (!el) return;
    el.textContent = msg;
    // Replay the brief accent flash to mark fresh activity. Reflow trick
    // restarts the CSS animation without needing to bounce a class.
    el.style.animation = "none";
    void el.offsetWidth;
    el.style.animation = "";
}

function showModal(id) {
    document.getElementById(id).classList.remove("hidden");
}

function hideModal(id) {
    document.getElementById(id).classList.add("hidden");
}
