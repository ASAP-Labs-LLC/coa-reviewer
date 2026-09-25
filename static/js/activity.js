/* ══════════════════════════════════════════════════════════════════════
   Time online — /activity

   One day at a time: morning at the top, night at the bottom, one column
   per person. A soft bar is a stretch of being signed in; the solid sliver
   drawn over it is when that person was actually making changes. Green is
   reserved for "online right now".

   Data: GET /api/activity?date=YYYY-MM-DD (activity.build_day + online,
   is_today) and GET /api/activity/range. Times are epoch seconds and are
   shown in this browser's local time.

   Polling: every 120 s, and only while the tab is visible AND showing
   today. Nothing polls for a past day, and nothing polls in a hidden tab.
   ══════════════════════════════════════════════════════════════════════ */
"use strict";

const POLL_MS = 120000;
const RETRY_MS = [10000, 30000, 60000, POLL_MS];

// ── Guarded storage (private windows / blocked site data throw) ─────────
function storeGet(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
}
function storeSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (_) { /* ignore */ }
}

const DEBUG = storeGet("activityDebug") === "1";
function debug(...args) { if (DEBUG) console.debug("[activity]", ...args); }

// ── Kinds → friendly labels [singular, plural] ──────────────────────────
const KIND_LABELS = {
    mark:               ["mark", "marks"],
    unmark:             ["cleared mark", "cleared marks"],
    test_result:        ["test edit", "test edits"],
    sample_info:        ["sample-info edit", "sample-info edits"],
    sample_sync:        ["LabVision sync", "LabVision syncs"],
    comments:           ["comment edit", "comment edits"],
    attachment_deleted: ["attachment deleted", "attachments deleted"],
    listing_created:    ["listing filed", "listings filed"],
    listing_completed:  ["listing completed", "listings completed"],
    external_change:    ["outside change", "outside changes"],
};
const KIND_ORDER = Object.keys(KIND_LABELS);

// ── State ────────────────────────────────────────────────────────────────
const state = {
    date: null,          // "YYYY-MM-DD" being shown
    today: null,         // server's today (falls back to the browser's)
    first: null,         // first recorded day, or null before any history
    data: null,          // last payload for state.date
    loadedAt: 0,         // ms epoch of the last successful load
    pollTimer: null,
    retryTimer: null,
    retryStep: 0,
    seq: 0,              // ignores responses for a day we've stepped away from
    controller: null,
    signedOut: false,
    error: false,
};

// ── Helpers ─────────────────────────────────────────────────────────────
function $(sel) { return document.querySelector(sel); }

function escapeHtml(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function pad(n) { return String(n).padStart(2, "0"); }
function isoOf(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function parseIso(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
function addDays(iso, n) {
    const d = parseIso(iso);
    d.setDate(d.getDate() + n);
    return isoOf(d);
}
function localToday() { return isoOf(new Date()); }
function isToday() { return !!state.date && state.date === (state.today || localToday()); }

const fmtClock = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const fmtLong = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
const fmtShort = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });

function clock(ts) { return fmtClock.format(new Date(ts * 1000)); }

// "9:01 – 9:18 AM" when both ends share AM/PM, else "11:40 AM – 1:05 PM".
function clockRange(a, b, openEnd) {
    const A = clock(a);
    if (openEnd) return `${A} – now`;
    const B = clock(b);
    const ma = /\s?([AP]M)$/i.exec(A), mb = /\s?([AP]M)$/i.exec(B);
    if (ma && mb && ma[1] === mb[1]) return `${A.slice(0, ma.index)} – ${B}`;
    return `${A} – ${B}`;
}

function duration(seconds) {
    const m = Math.round(seconds / 60);
    if (m < 1) return "under a minute";
    const h = Math.floor(m / 60), r = m % 60;
    if (!h) return `${r}m`;
    return r ? `${h}h ${r}m` : `${h}h`;
}

function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

function breakdown(byKind) {
    const parts = [];
    for (const k of KIND_ORDER) {
        const n = byKind && byKind[k];
        if (n) parts.push(plural(n, KIND_LABELS[k][0], KIND_LABELS[k][1]));
    }
    for (const k of Object.keys(byKind || {})) {   // kinds added after this page
        if (!KIND_LABELS[k] && byKind[k]) parts.push(`${byKind[k]} ${k.replace(/_/g, " ")}`);
    }
    return parts.join(", ");
}

function initials(name) {
    const words = String(name).trim().split(/[\s._-]+/).filter(Boolean);
    if (!words.length) return "?";
    const a = words[0][0], b = words.length > 1 ? words[words.length - 1][0] : (words[0][1] || "");
    return (a + b).toUpperCase();
}

function firstName(name) { return String(name).trim().split(/\s+/)[0] || String(name); }

function onlineSet(data) {
    if (!data || !data.is_today) return new Set();
    return new Set((data.online || []).map(n => String(n).trim().toLowerCase()));
}
function isOnlineNow(data, user) { return onlineSet(data).has(String(user).trim().toLowerCase()); }

// Wall-clock hour of a timestamp on the day being shown (0..24). Anything
// at or past the next local midnight is the end of this day, not hour 0.
function hourOf(ts) {
    const day = parseIso(state.date);
    const next = new Date(day); next.setDate(next.getDate() + 1);
    if (ts * 1000 >= next.getTime()) return 24;
    if (ts * 1000 < day.getTime()) return 0;
    const d = new Date(ts * 1000);
    return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
}

function hourLabel(h) {
    if (h === 0 || h === 24) return "12 AM";
    if (h === 12) return "Noon";
    return h < 12 ? `${h} AM` : `${h - 12} PM`;
}

// ── Theme (mirrors app.js: same "theme" key, same body.dark switch) ─────
function initTheme() {
    let stored = storeGet("theme");
    if (!stored) {
        stored = storeGet("dark") === "true" ? "dark" : "system";
        storeSet("theme", stored);
    }
    applyTheme(stored);
    if (window.matchMedia) {
        const mql = window.matchMedia("(prefers-color-scheme: dark)");
        const onChange = () => { if ((storeGet("theme") || "system") === "system") applyTheme("system"); };
        if (mql.addEventListener) mql.addEventListener("change", onChange);
        else if (mql.addListener) mql.addListener(onChange);
    }
    // The review screen may change the theme in another tab.
    window.addEventListener("storage", (e) => { if (e.key === "theme" && e.newValue) applyTheme(e.newValue); });
}

function applyTheme(mode) {
    if (!["system", "light", "dark"].includes(mode)) mode = "system";
    storeSet("theme", mode);
    const sysDark = !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    const effectiveDark = mode === "dark" || (mode === "system" && sysDark);
    document.body.classList.toggle("dark", effectiveDark);
    document.body.classList.toggle("force-dark", mode === "dark");
    document.body.classList.toggle("force-light", mode === "light");
    document.querySelectorAll(".theme-pip").forEach(pip => {
        const on = pip.dataset.theme === mode;
        pip.classList.toggle("active", on);
        pip.setAttribute("aria-pressed", on ? "true" : "false");
    });
}

// ── Fetching ────────────────────────────────────────────────────────────
async function getJson(url, signal) {
    const t0 = performance.now();
    const resp = await fetch(url, { credentials: "same-origin", cache: "no-store", signal });
    let body = null;
    try { body = await resp.json(); } catch (_) { body = null; }
    debug(url, resp.status, `${Math.round(performance.now() - t0)} ms`);
    return { status: resp.status, ok: resp.ok, body };
}

async function loadRange() {
    try {
        const r = await getJson("/api/activity/range");
        if (r.status === 401) { showSignedOut(); return false; }
        if (r.ok && r.body) {
            state.first = r.body.first || null;
            state.today = r.body.today || state.today;
        }
    } catch (e) {
        debug("range failed", e);
    }
    return true;
}

async function load(opts = {}) {
    if (state.signedOut || !state.date) return;
    const seq = ++state.seq;
    if (state.controller) state.controller.abort();
    const controller = ("AbortController" in window) ? new AbortController() : null;
    state.controller = controller;
    const root = $("#activity-root");
    if (!opts.quiet) root.setAttribute("aria-busy", "true");
    try {
        const r = await getJson(`/api/activity?date=${encodeURIComponent(state.date)}`, controller && controller.signal);
        if (seq !== state.seq) return;
        if (r.status === 401) { showSignedOut(); return; }
        if (!r.ok || !r.body || !Array.isArray(r.body.users)) {
            throw new Error((r.body && r.body.error) || `HTTP ${r.status}`);
        }
        state.data = r.body;
        if (r.body.is_today && r.body.date) state.today = r.body.date;
        state.loadedAt = Date.now();
        state.error = false;
        state.retryStep = 0;
        clearTimeout(state.retryTimer);
        render();
        stampUpdated();
    } catch (e) {
        if (e && e.name === "AbortError") return;
        if (seq !== state.seq) return;
        debug("load failed", e);
        state.error = true;
        render();
        scheduleRetry();
    } finally {
        if (seq === state.seq) root.setAttribute("aria-busy", "false");
    }
}

function scheduleRetry() {
    clearTimeout(state.retryTimer);
    if (document.visibilityState !== "visible") return;   // visibilitychange retries
    const wait = RETRY_MS[Math.min(state.retryStep, RETRY_MS.length - 1)];
    state.retryStep += 1;
    debug("retry in", wait, "ms");
    state.retryTimer = setTimeout(() => load({ quiet: true }), wait);
}

// ── Polling (visible + today only) ──────────────────────────────────────
function shouldPoll() {
    return document.visibilityState === "visible" && isToday() && !state.signedOut;
}

function syncPolling() {
    if (shouldPoll()) {
        if (!state.pollTimer) {
            state.pollTimer = setInterval(() => { if (shouldPoll()) load({ quiet: true }); }, POLL_MS);
            debug("polling on");
        }
    } else if (state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        debug("polling off");
    }
}

document.addEventListener("visibilitychange", () => {
    syncPolling();
    if (document.visibilityState !== "visible") { clearTimeout(state.retryTimer); return; }
    const stale = Date.now() - state.loadedAt > POLL_MS;
    if ((isToday() && stale) || state.error) load({ quiet: true });
});

// ── Day navigation ──────────────────────────────────────────────────────
function clampDate(iso) {
    const today = state.today || localToday();
    if (!parseIso(iso) || iso > today) return today;
    if (state.first && iso < state.first) return state.first;
    return iso;
}

function setDate(iso, { push = false } = {}) {
    const next = clampDate(iso);
    if (next === state.date && state.data) return;
    state.date = next;
    state.data = null;
    state.error = false;
    state.retryStep = 0;
    clearTimeout(state.retryTimer);
    hideCard();
    try {
        const url = new URL(window.location.href);
        if (next === (state.today || localToday())) url.searchParams.delete("date");
        else url.searchParams.set("date", next);
        window.history[push ? "pushState" : "replaceState"]({ date: next }, "", url);
    } catch (_) { /* file:// or sandboxed — not important */ }
    renderHeader();
    syncPolling();
    load();
}

function renderHeader() {
    const d = parseIso(state.date);
    const today = state.today || localToday();
    const yesterday = addDays(today, -1);
    const rel = state.date === today ? "Today" : state.date === yesterday ? "Yesterday" : "";
    $("#act-date-label").innerHTML = rel
        ? `<span class="act-rel">${rel}</span> ${escapeHtml(fmtLong.format(d))}`
        : escapeHtml(fmtLong.format(d));
    $("#act-pick-text").textContent = fmtShort.format(d);
    document.title = `Time online · ${fmtShort.format(d)}`;

    const input = $("#act-date-input");
    input.value = state.date;
    input.max = today;
    if (state.first) input.min = state.first; else input.removeAttribute("min");

    $("#act-prev").disabled = !!state.first ? state.date <= state.first : true;
    $("#act-next").disabled = state.date >= today;
    $("#act-today").hidden = state.date === today;
}

function stampUpdated() {
    const el = $("#act-updated");
    el.textContent = `Updated ${fmtClock.format(new Date(state.loadedAt))}`;
    el.title = isToday() ? "Refreshes every 2 minutes while this tab is open" : "";
}

// ── Rendering ───────────────────────────────────────────────────────────
function showSignedOut() {
    state.signedOut = true;
    syncPolling();
    clearTimeout(state.retryTimer);
    document.body.classList.add("act-signed-out");
    $("#act-updated").textContent = "";
    $("#activity-root").innerHTML = `
        <div class="act-gate">
            <div class="act-gate-mark" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>
            </div>
            <h2>Sign in on the review screen first</h2>
            <p>Time online is visible to signed-in reviewers. Sign in there, then come back to this tab.</p>
            <a class="act-btn" href="/">Open the review screen</a>
        </div>`;
    $("#activity-root").setAttribute("aria-busy", "false");
}

function render() {
    const root = $("#activity-root");
    const data = state.data;

    if (!data) {
        root.innerHTML = state.error
            ? errorBanner() + `<div class="act-panel act-empty"><p>This day couldn't be loaded yet.</p></div>`
            : `<p class="act-loading">Loading…</p>`;
        wireRetry();
        return;
    }

    const users = data.users || [];
    let body;
    if (!users.length) {
        const firstRun = !state.first || state.date < state.first;
        body = `<div class="act-panel act-empty">
            <p class="act-empty-title">${firstRun ? "Time online is recorded from v4.0.0 on."
                : data.is_today ? "Nobody has signed in yet today." : "Nobody was signed in on this day."}</p>
            <p class="act-empty-sub">${firstRun
                ? "Sign-ins and changes will show up here as people use COA Reviewer."
                : data.is_today ? "This page refreshes every 2 minutes while it's open." : "Use ‹ and › to look at another day."}</p>
        </div>`;
    } else {
        body = chart(data);
    }

    root.innerHTML = (state.error ? errorBanner() : "")
        + stats(data)
        + body
        + (data.truncated ? `<p class="act-note">Some activity on this day isn't shown (too many records).</p>` : "");
    wireRetry();
}

function errorBanner() {
    return `<div class="act-banner" role="status">
        <span>Couldn't load this day — retrying</span>
        <button type="button" class="act-link-btn" data-retry>Retry now</button>
    </div>`;
}

function wireRetry() {
    const b = document.querySelector("[data-retry]");
    if (b) b.addEventListener("click", () => { state.retryStep = 0; load(); });
}

function stats(data) {
    const s = data.summary || {};
    const today = !!data.is_today;
    const onlineUsers = today
        ? (data.users || []).filter(u => isOnlineNow(data, u.user)).map(u => firstName(u.user))
        : [];
    const onlineNow = today ? (s.online_now || 0) : null;
    let onlineCaption;
    if (!today) onlineCaption = "Shown for today only";
    else if (!onlineUsers.length) onlineCaption = "Nobody right now";
    else if (onlineUsers.length <= 3) onlineCaption = onlineUsers.map(escapeHtml).join(", ");
    else onlineCaption = `${onlineUsers.slice(0, 2).map(escapeHtml).join(", ")} and ${onlineUsers.length - 2} more`;

    const hours = (s.online_seconds || 0) / 3600;
    const byKind = {};
    for (const u of data.users || []) {
        for (const [k, n] of Object.entries((u.totals && u.totals.by_kind) || {})) byKind[k] = (byKind[k] || 0) + n;
    }
    const topKinds = breakdown(byKind).split(", ").filter(Boolean).slice(0, 2).join(", ");

    return `<section class="act-stats" aria-label="Summary">
        <div class="act-stat">
            <p class="act-stat-label">${onlineNow ? `<span class="act-live-dot" aria-hidden="true"></span>` : ""}Online now</p>
            <p class="act-stat-value ${onlineNow === null ? "is-na" : ""}">${onlineNow === null ? "—" : onlineNow}</p>
            <p class="act-stat-sub">${onlineCaption}</p>
        </div>
        <div class="act-stat">
            <p class="act-stat-label">People</p>
            <p class="act-stat-value">${s.people || 0}</p>
            <p class="act-stat-sub">signed in this day</p>
        </div>
        <div class="act-stat">
            <p class="act-stat-label">Hours online</p>
            <p class="act-stat-value">${hours.toFixed(1)}</p>
            <p class="act-stat-sub">${s.people ? "across everyone" : "nobody signed in"}</p>
        </div>
        <div class="act-stat">
            <p class="act-stat-label">Changes</p>
            <p class="act-stat-value">${(s.changes || 0).toLocaleString()}</p>
            <p class="act-stat-sub">${topKinds ? escapeHtml(topKinds) : "no edits recorded"}</p>
        </div>
    </section>`;
}

function chart(data) {
    const b = data.bounds || { start_hour: 6, end_hour: 22 };
    const start = Math.max(0, Math.min(23, b.start_hour | 0));
    const end = Math.max(start + 1, Math.min(24, b.end_hour | 0));
    const span = end - start;
    const pct = (ts) => {
        const h = Math.max(start, Math.min(end, hourOf(ts)));
        return ((h - start) / span) * 100;
    };
    const users = data.users || [];

    // Hour gridlines + gutter labels.
    let lines = "", labels = "";
    for (let h = start; h <= end; h++) {
        const top = ((h - start) / span) * 100;
        lines += `<div class="act-hline${h === 12 ? " is-noon" : ""}" style="top:${top}%"></div>`;
        labels += `<span class="act-hlabel" style="top:${top}%">${hourLabel(h)}</span>`;
    }

    // "Now" line on today.
    // "Now" line on today: the line crosses every column, its time sits in
    // the (sticky) gutter so it stays readable while columns scroll, and
    // the rest of the day below it is shaded as not-yet-happened.
    let now = "", nowTag = "";
    if (data.is_today) {
        const nowTs = Date.now() / 1000;
        const h = hourOf(nowTs);
        if (h >= start && h <= end) {
            const top = ((h - start) / span) * 100;
            now = `<div class="act-future" style="top:${top}%"></div><div class="act-now" style="top:${top}%"></div>`;
            nowTag = `<span class="act-now-tag" style="top:${top}%">${escapeHtml(clock(nowTs))}</span>`;
            labels = labels.replace(/<span class="act-hlabel" style="top:([\d.]+)%">/g, (m, t) =>
                Math.abs(+t - top) < (45 / span) ? `<span class="act-hlabel is-hidden" style="top:${t}%">` : m);
        }
    }

    const heads = users.map((u, ui) => {
        const nameHtml = escapeHtml(u.user);
        const live = isOnlineNow(data, u.user);
        const t = u.totals || {};
        return `<div class="act-colhead${live ? " is-live" : ""}" data-u="${ui}">
            <span class="act-avatar" aria-hidden="true">${escapeHtml(initials(u.user))}${live ? `<i class="act-avatar-dot"></i>` : ""}</span>
            <span class="act-name" title="${nameHtml}">${nameHtml}</span>
            <span class="act-total">${escapeHtml(duration(t.online_seconds || 0))}</span>
            <span class="act-total act-total-sub">${t.changes ? escapeHtml(plural(t.changes, "change", "changes")) : "no changes"}</span>
        </div>`;
    }).join("");

    const cols = users.map((u, ui) => {
        const nameHtml = escapeHtml(u.user);
        const live = isOnlineNow(data, u.user);
        const spans = (u.spans || []).map((s, si) => {
            const top = pct(s.start), bottom = pct(s.end);
            const openLive = s.open && live;
            const label = u.user + `, signed in ${clockRange(s.start, s.end, openLive)}, ${duration(s.end - s.start)}, ${plural(s.changes || 0, "change", "changes")}`;
            return `<div class="act-span${openLive ? " is-open" : ""}" tabindex="0" role="img"
                data-u="${ui}" data-s="${si}" aria-label="${escapeHtml(label)}"
                style="top:${top}%;height:${Math.max(0, bottom - top)}%">${openLive ? `<i class="act-open-dot" aria-hidden="true"></i>` : ""}</div>`;
        }).join("");
        const blocks = (u.blocks || []).map((bk, bi) => {
            const top = pct(bk.start), bottom = pct(bk.end);
            const label = u.user + `, making changes ${clockRange(bk.start, bk.end)}, ${plural(bk.count, "change", "changes")}`;
            return `<div class="act-block" tabindex="0" role="img"
                data-u="${ui}" data-b="${bi}" aria-label="${escapeHtml(label)}"
                style="top:${top}%;height:${Math.max(0, bottom - top)}%"></div>`;
        }).join("");
        return `<div class="act-col" data-u="${ui}" aria-label="${nameHtml}">${spans}${blocks}</div>`;
    }).join("");

    const style = `--cols:${users.length};--hours:${span}`;
    return `<section class="act-panel act-chart-panel" aria-label="Day chart">
        <div class="act-legend" aria-hidden="true">
            <span><i class="lg lg-span"></i>Signed in</span>
            <span><i class="lg lg-block"></i>Making changes</span>
            ${data.is_today ? `<span><i class="lg lg-live"></i>Online now</span>` : ""}
        </div>
        <div class="act-scroll">
            <div class="act-chart" style="${style}">
                <div class="act-heads">
                    <div class="act-gutter-head" aria-hidden="true"></div>
                    ${heads}
                </div>
                <div class="act-body">
                    <div class="act-gutter" aria-hidden="true">${labels}${nowTag}</div>
                    <div class="act-lines" aria-hidden="true">${lines}${now}</div>
                    ${cols}
                </div>
            </div>
        </div>
    </section>`;
}

// ── Hover / focus card ──────────────────────────────────────────────────
function cardHtml(el) {
    const data = state.data;
    if (!data) return "";
    const u = (data.users || [])[+el.dataset.u];
    if (!u) return "";
    const nameHtml = escapeHtml(u.user);
    const live = isOnlineNow(data, u.user);
    const t = u.totals || {};
    const head = `<p class="c-name">${nameHtml}${live ? `<span class="c-live"><i></i>Online now</span>` : ""}</p>`;

    if (el.dataset.b != null) {
        const bk = (u.blocks || [])[+el.dataset.b];
        if (!bk) return "";
        return `${head}
            <p class="c-main">${escapeHtml(clockRange(bk.start, bk.end))} · ${escapeHtml(plural(bk.count, "change", "changes"))}</p>
            <p class="c-sub">Making changes</p>`;
    }

    const s = (u.spans || [])[+el.dataset.s];
    if (!s) return "";
    const openLive = s.open && live;
    const sessions = (u.spans || []).length;
    const all = sessions > 1
        ? `<p class="c-sub">Signed in ${sessions} times: ${u.spans.map(x => escapeHtml(clockRange(x.start, x.end, x.open && live))).join(", ")}</p>`
        : "";
    const dayChanges = t.changes
        ? `${escapeHtml(plural(t.changes, "change", "changes"))}: ${escapeHtml(breakdown(t.by_kind))}`
        : "No changes this day";
    return `${head}
        <p class="c-main">${escapeHtml(clockRange(s.start, s.end, openLive))} · ${escapeHtml(duration(s.end - s.start))}</p>
        <p class="c-sub">${escapeHtml(plural(s.changes || 0, "change", "changes"))} in this stretch</p>
        <div class="c-rule"></div>
        <p class="c-day">${escapeHtml(duration(t.online_seconds || 0))} online this day</p>
        <p class="c-sub">${dayChanges}</p>
        ${all}`;
}

let cardFor = null;
function showCard(el, x, y) {
    const card = $("#act-card");
    if (cardFor !== el) {
        const html = cardHtml(el);
        if (!html) return hideCard();
        card.innerHTML = html;
        cardFor = el;
        document.querySelectorAll(".is-hot").forEach(n => n.classList.remove("is-hot"));
        const col = el.closest(".act-col");
        if (col) col.classList.add("is-hot");
        const head = document.querySelector(`.act-colhead[data-u="${el.dataset.u}"]`);
        if (head) head.classList.add("is-hot");
    }
    card.hidden = false;
    const r = card.getBoundingClientRect();
    const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    let left = x + 16, top = y + 14;
    if (left + r.width > vw - 12) left = x - r.width - 16;
    if (top + r.height > vh - 12) top = vh - r.height - 12;
    card.style.left = `${Math.max(12, left)}px`;
    card.style.top = `${Math.max(12, top)}px`;
}

function hideCard() {
    cardFor = null;
    const card = $("#act-card");
    if (card) card.hidden = true;
    document.querySelectorAll(".is-hot").forEach(n => n.classList.remove("is-hot"));
}

function barFrom(target) {
    return target && target.closest ? target.closest(".act-span, .act-block") : null;
}

function wireCard() {
    const root = $("#activity-root");
    root.addEventListener("pointermove", (e) => {
        const bar = barFrom(e.target);
        if (bar) showCard(bar, e.clientX, e.clientY);
        else if (cardFor && document.activeElement !== cardFor) hideCard();
    });
    root.addEventListener("pointerleave", () => { if (document.activeElement !== cardFor) hideCard(); });
    root.addEventListener("focusin", (e) => {
        const bar = barFrom(e.target);
        if (!bar) return;
        const r = bar.getBoundingClientRect();
        cardFor = null;
        showCard(bar, r.right, r.top);
    });
    root.addEventListener("focusout", (e) => { if (barFrom(e.target)) hideCard(); });
    document.addEventListener("scroll", () => { if (cardFor && document.activeElement !== cardFor) hideCard(); }, true);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideCard(); });
}

// ── Keyboard: ←/→ step days unless typing ───────────────────────────────
function wireKeys() {
    document.addEventListener("keydown", (e) => {
        if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
        const t = e.target;
        const tag = t && t.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
        if (state.signedOut || !state.date) return;
        if (e.key === "ArrowLeft" && !$("#act-prev").disabled) { e.preventDefault(); setDate(addDays(state.date, -1), { push: true }); }
        else if (e.key === "ArrowRight" && !$("#act-next").disabled) { e.preventDefault(); setDate(addDays(state.date, 1), { push: true }); }
    });
}

// ── Boot ────────────────────────────────────────────────────────────────
async function boot() {
    initTheme();
    document.querySelectorAll(".theme-pip").forEach(b => b.addEventListener("click", () => applyTheme(b.dataset.theme)));

    $("#act-prev").addEventListener("click", () => setDate(addDays(state.date, -1), { push: true }));
    $("#act-next").addEventListener("click", () => setDate(addDays(state.date, 1), { push: true }));
    $("#act-today").addEventListener("click", () => setDate(state.today || localToday(), { push: true }));
    const input = $("#act-date-input");
    input.addEventListener("click", () => { try { input.showPicker && input.showPicker(); } catch (_) { /* older browsers open on their own */ } });
    input.addEventListener("change", () => { if (input.value) setDate(input.value, { push: true }); });
    window.addEventListener("popstate", () => {
        let d = null;
        try { d = new URL(window.location.href).searchParams.get("date"); } catch (_) { /* ignore */ }
        setDate(d || state.today || localToday());
    });

    wireCard();
    wireKeys();

    state.today = localToday();
    if (!(await loadRange())) return;

    let wanted = null;
    try { wanted = new URL(window.location.href).searchParams.get("date"); } catch (_) { /* ignore */ }
    setDate(wanted || state.today);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
