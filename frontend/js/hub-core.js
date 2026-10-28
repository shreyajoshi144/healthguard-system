/* HealthGuard hub core: routing, patient context, shared render helpers. All dynamic text is escaped. */
const HG = (() => {
  const api = (p, o) => HealthGuardAPI.request(p, o);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const params = new URLSearchParams(location.search);
  const st = { patientId: params.get("patient_id"), profiles: [], profile: null, views: {}, order: [] };
  const NAV = [
    ["overview", "Overview"], ["summary", "Health summary"], ["ask", "Ask my records"], ["questions", "Questions for my doctor"],
    ["medications", "Medications"], ["vitals", "Vitals"], ["timeline", "Timeline"], ["graph", "Health graph"], ["visit", "Doctor visit prep"], ["followups", "Follow-ups"], ["family", "Family & sharing"],
  ];
  const fmtDate = (d, prec) => {
    if (!d) return "Date unknown";
    const s = String(d).slice(0, 10);
    if (prec === "YEAR") return s.slice(0, 4) + " (year only)";
    if (prec === "APPROXIMATE") return "≈ " + s + " (approximate)";
    if (prec === "UNKNOWN") return "Date unknown";
    return s;
  };
  const fmtTs = (t) => t ? new Date(t).toLocaleString() : "—";
  const provPill = (p, unconfirmed) => !p ? "" : `<span class="pill ${unconfirmed || p === "SYSTEM_DERIVED" ? "warn" : "ok"}" title="Where this came from">${esc(({ USER_REPORTED: "You reported", FAMILY_REPORTED: "Family reported", DOCUMENT_VERIFIED: "From a document", SYSTEM_DERIVED: "System-derived · unconfirmed" })[p] || p)}</span>`;
  const evChips = (refs) => (refs || []).map(r => {
    const label = ({ DOCUMENT: "Source document", HEALTH_FACT: "Extracted fact", MEDICATION: "Medication record", HEALTH_EVENT: "Event record", INVESTIGATION: "Investigation", INVESTIGATION_RESULT: "Result record", HEALTH_ACTION: "Follow-up record", MEASUREMENT: "Vital reading", PROFILE: "Profile" })[r.source_type] || r.source_type;
    const doc = r.document_id ? `/api/documents/${encodeURIComponent(r.document_id)}/content` : null;
    const pg = r.page ? ` · p.${esc(r.page)}` : "";
    return doc ? `<a class="chip" href="${doc}" target="_blank" rel="noopener" title="Opens the original uploaded document">📄 ${esc(label)}${pg}</a>` : `<span class="chip">${esc(label)}</span>`;
  }).join("");
  const loading = (el, msg = "Loading…") => { el.innerHTML = `<div class="state" role="status"><span class="spin"></span>${esc(msg)}</div>`; };
  const error = (el, e, retry) => { el.innerHTML = `<div class="state err" role="alert">${esc(e.message || e)}${retry ? ` <br><button class="btn btn-sm btn-ghost" id="hg-retry">Try again</button>` : ""}</div>`; if (retry) el.querySelector("#hg-retry").onclick = retry; };
  const empty = (msg, cta) => `<div class="state">${esc(msg)}${cta || ""}</div>`;
  const flash = (el, kind, msg) => { const d = document.createElement("div"); d.className = kind === "ok" ? "ok-msg" : "err-msg"; d.setAttribute("role", kind === "ok" ? "status" : "alert"); d.textContent = msg; el.prepend(d); setTimeout(() => d.remove(), 6000); };
  const can = (scope, write) => { const p = st.profile; if (!p) return false; if (p.is_owner) return true; return write ? (p.write_scopes || []).includes(scope) : (p.scopes || []).includes(scope); };
  const needScope = (scope) => can(scope) ? null : `<div class="state">This part of the record is outside the access granted to you for this patient.</div>`;
  const register = (key, fn) => { st.views[key] = fn; };
  const q = (patientPath) => `/api/patients/${encodeURIComponent(st.patientId)}${patientPath}`;

  function renderNav(view) {
    document.getElementById("nav").innerHTML = NAV.map(([k, label]) => `<a href="/hub?patient_id=${encodeURIComponent(st.patientId)}#/${k}" ${k === view ? 'aria-current="page"' : ""}>${esc(label)}</a>`).join("") +
      `<div class="sep">More</div><a href="/patient?id=${encodeURIComponent(st.patientId)}">Documents &amp; uploads</a>`;
  }
  function renderCtx() {
    const p = st.profile; const el = document.getElementById("ctx");
    const opts = st.profiles.map(x => `<option value="${esc(x.patient_id)}" ${x.patient_id === st.patientId ? "selected" : ""}>${esc(x.name)}${x.is_owner ? "" : " (shared with you)"}</option>`).join("");
    const role = p.is_owner ? "You own this profile" : `Shared with you as ${p.relationship.toLowerCase()}`;
    const exp = p.expires_at ? ` · access ends ${esc(fmtTs(p.expires_at))}` : "";
    el.innerHTML = `<label class="meta" for="switch">Viewing health record of</label><select id="switch" aria-label="Switch patient profile">${opts}</select><span class="ctx-badge ${p.is_owner ? "" : "shared"}">${esc(role)}${exp}</span>${p.is_owner ? "" : `<span class="meta">Access: ${esc((p.scopes || []).join(", ").toLowerCase().replace(/_/g, " "))}</span>`}`;
    document.getElementById("switch").onchange = (e) => { location.href = `/hub?patient_id=${encodeURIComponent(e.target.value)}${location.hash}`; };
  }
  async function route() {
    const view = (location.hash.replace(/^#\//, "") || "overview").split("?")[0];
    const fn = st.views[view] || st.views.overview;
    const el = document.getElementById("view"); renderNav(st.views[view] ? view : "overview");
    loading(el); el.focus({ preventScroll: true });
    try { await fn(el); } catch (e) { error(el, e, route); }
  }
  async function boot() {
    const main = document.getElementById("view");
    try {
      const ov = await api("/api/family/overview"); st.profiles = ov.profiles;
      if (!st.profiles.length) { document.getElementById("ctx").innerHTML = ""; main.innerHTML = empty("You have no patient profiles yet.", ` <a class="btn btn-primary btn-sm" href="/health">Create a profile</a>`); return; }
      if (!st.profiles.some(p => p.patient_id === st.patientId)) st.patientId = st.profiles[0].patient_id;
      st.profile = st.profiles.find(p => p.patient_id === st.patientId);
      renderCtx(); window.addEventListener("hashchange", route); await route();
    } catch (e) { error(main, e, boot); }
  }
  return { api, esc, st, fmtDate, fmtTs, provPill, evChips, loading, error, empty, flash, can, needScope, register, q, boot, route };
})();
