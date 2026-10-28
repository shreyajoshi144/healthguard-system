/* ============================================================
   HealthGuard — shared frontend utilities
   API base is same-origin (FastAPI serves this file), so requests
   are plain relative paths like /api/health.
   ============================================================ */

const HG_USER_KEY = "hg_user";
let hgUserState = null;

function hgGetCookie(name) {
  const prefix = `${name}=`;
  const item = document.cookie.split(";").map(v => v.trim()).find(v => v.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : null;
}
function hgSetSession(_token, user) {
  // The server sets the HttpOnly session cookie. Keep authenticated UI state
  // in memory only; browser authentication never uses Web Storage.
  hgUserState = user || null;
}
function hgClearSession() {
  hgUserState = null;
}

function hgGetCachedUser() {
  return hgUserState;
}

const HealthGuardAPI = (() => {
  async function request(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    const method = (options.method || "GET").toUpperCase();
    if (["POST","PUT","PATCH","DELETE"].includes(method)) {
      const csrf = hgGetCookie("hg_csrf");
      if (csrf) headers["X-CSRF-Token"] = csrf;
    }

    if (options.body && !options.rawBody && typeof options.body !== "string" && !(options.body instanceof FormData)) { headers["Content-Type"]="application/json"; options.body=JSON.stringify(options.body); }
    const res = await fetch(path, Object.assign({}, options, { headers }));
    let body = null;
    try {
      body = await res.json();
    } catch (_) {
      /* no JSON body */
    }
    if (!res.ok) {
      if (res.status === 401) {
        hgClearSession();
        if (!location.pathname.startsWith("/login") && !location.pathname.startsWith("/register")) {
          location.href = "/login?next=" + encodeURIComponent(location.pathname);
        }
      }
      const detail = (body && body.detail) ? body.detail : `Request failed (${res.status})`;
      throw new Error(detail);
    }
    return body;
  }

  return {
    request,
    health: () => request("/api/health"),

    register: (username, email, password) =>
      request("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, email, password }),
      }),

    login: (username, password) =>
      request("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      }),

    me: () => request("/api/auth/me"),

    analyze: (payload) =>
      request("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),

    upload: (file) => {
      const form = new FormData();
      form.append("file", file);
      return request("/api/upload", { method: "POST", body: form });
    },

    metrics: (uploadId) =>
      request(uploadId ? `/api/metrics?upload_id=${encodeURIComponent(uploadId)}` : "/api/metrics"),

    resendVerification: (email) =>
      request("/api/auth/resend-verification", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      }),
  };
})();

/* ── Auth guard for workspace pages ────────────────────────────
   Call at the top of every protected page. Redirects to /login
   by validating the server-managed HttpOnly session cookie via /api/auth/me.
   The JWT is intentionally never exposed to JavaScript. */
async function hgRequireAuth() {
  try {
    const user = await HealthGuardAPI.me();
    hgSetSession(null, user);
    return user;
  } catch (err) {
    // HealthGuardAPI.request already redirects to /login on 401
    return null;
  }
}

async function hgLogout() {
  try { await HealthGuardAPI.request("/api/auth/logout", { method: "POST" }); } catch (_) {}
  hgClearSession();
  location.href = "/login";
}

/* ── Sidebar / topbar shell (shared across workspace pages) ───── */
function hgInitWorkspaceShell(user) {
  const nameEl = document.getElementById("hgUserName");
  if (nameEl && user) nameEl.textContent = user.username;

  const initialEl = document.getElementById("hgUserInitial");
  if (initialEl && user && user.username) initialEl.textContent = user.username[0].toUpperCase();

  const current = location.pathname.replace(/\/$/, "") || "/workspace";
  document.querySelectorAll(".sidebar-link").forEach((a) => {
    const href = a.getAttribute("href");
    a.classList.toggle("active", href === current);
  });

  document.querySelectorAll("[data-logout]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      hgLogout();
    });
  });

  const sidebarToggle = document.getElementById("sidebarToggle");
  const shell = document.querySelector(".workspace-shell");
  if (sidebarToggle && shell) {
    sidebarToggle.addEventListener("click", () => shell.classList.toggle("sidebar-open"));
  }
}

/* ── Nav: mobile toggle + active link (public pages) ───────────── */
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const links = document.querySelector(".nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", () => {
      links.classList.toggle("open");
    });
  }

  const current = window.location.pathname.replace(/\/$/, "") || "/";
  document.querySelectorAll(".nav-links a").forEach((a) => {
    const href = a.getAttribute("href");
    if (href === current) a.classList.add("active");
  });

  // scroll-reveal
  const revealEls = document.querySelectorAll(".reveal");
  if (revealEls.length && "IntersectionObserver" in window) {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12 }
    );
    revealEls.forEach((el) => io.observe(el));
  } else {
    revealEls.forEach((el) => el.classList.add("is-visible"));
  }
});

/* ── small formatting helpers reused across pages ──────────────── */
function hgFmtPct(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return `${x.toFixed(1)}%`;
}
function hgFmtNum(x, digits = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return Number(x).toFixed(digits);
}
function hgEscape(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

/* ── Shared chart renderers (hand-rolled SVG/CSS — no chart lib) ─
   Used by overview.js, insights.js, departments.js so the bar/donut
   rendering logic isn't duplicated per page. */

function hgRenderBars(containerId, distribution, colorClass) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!distribution || Object.keys(distribution).length === 0) {
    el.innerHTML = `<p class="muted">No data yet.</p>`;
    return;
  }
  const entries = Object.entries(distribution).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(([, v]) => v));
  el.innerHTML = entries
    .map(
      ([label, value]) => `
      <div class="bar-row">
        <div class="bar-label" title="${hgEscape(label)}">${hgEscape(label)}</div>
        <div class="bar-track"><div class="bar-fill ${colorClass || ""}" style="width:${(value / max) * 100}%"></div></div>
        <div class="bar-value mono-num">${value}</div>
      </div>`
    )
    .join("");
}

function hgRenderDonut(containerId, distribution, colorMap) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!distribution || Object.keys(distribution).length === 0) {
    el.innerHTML = `<p class="muted">No data yet.</p>`;
    return;
  }
  const total = Object.values(distribution).reduce((a, b) => a + b, 0);
  const r = 54, cx = 70, cy = 70, circumference = 2 * Math.PI * r;
  let offset = 0;
  const segments = Object.entries(distribution)
    .map(([label, value]) => {
      const frac = value / total;
      const dash = frac * circumference;
      const seg = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${colorMap[label] || "#8faa8b"}"
        stroke-width="18" stroke-dasharray="${dash} ${circumference - dash}"
        stroke-dashoffset="${-offset}" transform="rotate(-90 ${cx} ${cy})" />`;
      offset += dash;
      return seg;
    })
    .join("");

  const legend = Object.entries(distribution)
    .map(
      ([label, value]) => `
      <div style="display:flex; justify-content:space-between; font-size:0.82rem; padding:5px 0;">
        <span><span class="legend-dot" style="background:${colorMap[label] || "#8faa8b"}"></span>${hgEscape(label)}</span>
        <span class="mono-num muted">${value} (${((value / total) * 100).toFixed(1)}%)</span>
      </div>`
    )
    .join("");

  el.innerHTML = `
    <div style="display:flex; align-items:center; gap:24px; flex-wrap:wrap;">
      <svg width="140" height="140" viewBox="0 0 140 140">${segments}</svg>
      <div style="flex:1; min-width:140px;">${legend}</div>
    </div>`;
}

// Sidebar "Log out" must work on every page, including pages whose scripts never call hgInitWorkspaceShell
// (health, patient, actions, handoff, settings). Pages that bind it themselves call preventDefault first, so this never double-fires.
document.addEventListener("click", (e) => {
  const btn = e.target && e.target.closest ? e.target.closest("[data-logout]") : null;
  if (!btn || e.defaultPrevented) return;
  e.preventDefault();
  hgLogout();
});
