/* HealthGuard operations console. Shows operational data only; no patient records exist behind these endpoints. */
(() => {
const api = (p, o) => HealthGuardAPI.request(p, o);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ts = (t) => t ? new Date(t).toLocaleString() : "—";
const SECTIONS = [["overview", "Overview"], ["jobs", "Jobs & processing"], ["ai", "AI operations"], ["security", "Security"], ["users", "Users"], ["alerts", "Alerts"], ["deployment", "Deployment & release"]];
const view = document.getElementById("view"); let me = null;
const pill = (s) => { const bad = ["DOWN", "FAILED", "CRITICAL", "POISONED", "BEHIND_OR_DIVERGED", "NO_HEARTBEAT", "STALE", "QUARANTINED"], warn = ["WARNING", "RETRYING", "NOT_CONFIGURED", "UNKNOWN", "NOT_PERFORMED", "ACKNOWLEDGED", "OPEN", "INFO"]; return `<span class="pill ${bad.includes(s) ? "bad" : warn.includes(s) ? "warn" : "ok"}">${esc(String(s).replace(/_/g, " ").toLowerCase())}</span>`; };
const state = (msg, err) => { view.innerHTML = `<div class="state ${err ? "err" : ""}" role="${err ? "alert" : "status"}">${err ? "" : '<span class="spin"></span>'}${esc(msg)}</div>`; };
const flash = (kind, msg) => { const d = document.createElement("div"); d.className = kind === "ok" ? "ok-msg" : "err-msg"; d.textContent = msg; view.prepend(d); setTimeout(() => d.remove(), 6000); };
const nav = (cur) => { document.getElementById("nav").innerHTML = SECTIONS.map(([k, l]) => `<a href="#/${k}" ${k === cur ? 'aria-current="page"' : ""}>${esc(l)}</a>`).join(""); };

async function stepUp() {
  document.getElementById("nav").innerHTML = "";
  view.innerHTML = `<h2>Operations console</h2><p class="sub">Administrator verification required. Enter the 6-digit code from your authenticator app.</p><form class="form" id="tf" style="max-width:360px"><label>Authenticator code<input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="\\d{6}" maxlength="6" required autofocus></label><button class="btn btn-primary">Verify</button></form><div id="msg"></div>`;
  view.querySelector("#tf").onsubmit = async (e) => { e.preventDefault(); try { await api("/api/admin/session", { method: "POST", body: { code: new FormData(e.target).get("code") } }); await boot(); } catch (err) { view.querySelector("#msg").innerHTML = `<div class="err-msg" role="alert">${esc(err.message)}</div>`; } };
}
const needsStepUp = (e) => /ADMIN_STEP_UP_REQUIRED/.test(e.message);

const views = {
  async overview() {
    const o = await api("/api/admin/overview"); const c = o.components;
    const comp = (name, x, extra) => `<section class="tile"><h3>${esc(name)} ${pill(x.status)}</h3><div class="meta">${extra || ""}</div></section>`;
    view.innerHTML = `<h2>Overview</h2><p class="sub">Generated ${esc(ts(o.generated_at))} · ${esc(o.version.environment)} · version ${esc(o.version.version)}${o.version.git_sha ? " (" + esc(o.version.git_sha) + ")" : ""}</p>
      <div class="grid">${comp("API", c.api, "Serving this request")}${comp("Database", c.database, c.database.latency_ms != null ? c.database.latency_ms + " ms" : esc(c.database.error_type || ""))}${comp("Redis", c.redis, c.redis.latency_ms != null ? c.redis.latency_ms + " ms" : "")}${comp("Object storage", c.storage, esc(c.storage.backend || ""))}${comp("AI provider", c.ai, esc((c.ai.provider || "") + (c.ai.model ? " · " + c.ai.model : "")) + " · live probe " + esc(c.ai.live_probe.toLowerCase().replace(/_/g, " ")))}
      ${comp("Workers", c.workers, `${c.workers.count} heartbeat(s)` + (c.workers.workers.length ? "<br>" + c.workers.workers.map(w => `${esc(w.worker)}: ${w.heartbeat_age_seconds}s ago`).join("<br>") : ""))}
      ${comp("Queue", c.queue, c.queue.waiting != null ? `${c.queue.waiting} waiting · ${c.queue.in_flight} in flight · ${c.queue.quarantined_messages} quarantined` : "")}
      <section class="tile"><h3>Migrations ${pill(o.migration.status)}</h3><div class="meta">current ${esc(o.migration.current || "—")}<br>head ${esc(o.migration.head || "—")}</div></section>
      <section class="tile"><h3>Jobs</h3><div class="meta">${Object.entries(o.jobs.by_status).map(([k, v]) => `${esc(k.toLowerCase())}: <strong>${v}</strong>`).join(" · ")}<br>stale ${o.jobs.stale} · retrying ${o.jobs.retrying} · poisoned ${o.jobs.poisoned} · quarantined ${o.jobs.quarantined}</div></section>
      <section class="tile"><h3>Email reminders ${pill(o.reminders.email_delivery_configured ? "OK" : "NOT_CONFIGURED")}</h3><div class="meta">${Object.keys(o.reminders.by_status).length ? Object.entries(o.reminders.by_status).map(([k, v]) => `${esc(k.toLowerCase())}: <strong>${v}</strong>`).join(" · ") : "none scheduled"}<br>overdue pending ${o.reminders.overdue_pending} · failed (24h) ${o.reminders.failed_24h}</div></section>
      <section class="tile"><h3>Open alerts</h3><div class="meta">${Object.keys(o.open_alerts).length ? Object.entries(o.open_alerts).map(([k, v]) => `${pill(k)} ${v}`).join(" ") : "None"}</div></section></div>
      <div class="notice"><strong>Not verified by this build:</strong> ${o.unverified.map(esc).join("; ")}.</div>`;
  },
  async jobs() {
    let st = "", page = 1;
    const draw = async () => {
      const r = await api(`/api/admin/jobs?page=${page}${st ? "&state=" + st : ""}`);
      const counts = r.counts;
      view.innerHTML = `<h2>Jobs &amp; processing</h2><p class="sub">Document-processing jobs. No filenames or document contents are shown; documents are referenced by an opaque id.</p>
        <div class="tabs">${["", "QUEUED", "PROCESSING", "RETRYING", "COMPLETED", "FAILED", "STALE", "POISONED", "QUARANTINED"].map(s => `<a href="javascript:void 0" data-s="${s}" ${s === st ? 'aria-current="page"' : ""}>${s ? esc(s.toLowerCase()) : "all"}</a>`).join("")}</div>
        <div class="meta" style="margin-bottom:8px">stale ${counts.stale} · retrying ${counts.retrying} · poisoned ${counts.poisoned} · quarantined ${counts.quarantined}</div>
        ${r.items.length ? `<div style="overflow:auto"><table class="t"><thead><tr><th>Job</th><th>State</th><th>Attempts</th><th>Last error</th><th>Age</th><th>Latency / heartbeat</th><th>Doc ref</th><th></th></tr></thead><tbody>${r.items.map(j => `<tr><td class="meta">${esc(j.id.slice(0, 8))}</td><td>${pill(j.state)}${j.quarantine_reason ? `<div class="meta">${esc(j.quarantine_reason.toLowerCase().replace(/_/g, " "))}</div>` : ""}</td><td>${j.attempts}/${j.max_attempts}</td><td class="meta">${esc(j.last_error_code || "—")}</td><td class="meta">${j.age_seconds != null ? j.age_seconds + "s" : "—"}</td><td class="meta">${j.processing_seconds != null ? "took " + j.processing_seconds + "s" : j.heartbeat_age_seconds != null ? "heartbeat " + j.heartbeat_age_seconds + "s ago" : "—"}</td><td class="meta">${esc(j.document_ref || "")}</td><td>${r.can_operate ? actions(j) : ""}</td></tr>`).join("")}</tbody></table></div><div style="display:flex;gap:8px;margin-top:8px"><button class="btn btn-ghost btn-sm" id="pv" ${page <= 1 ? "disabled" : ""}>Previous</button><span class="meta">Page ${r.page} · ${r.total} job(s)</span><button class="btn btn-ghost btn-sm" id="nx" ${page * r.page_size >= r.total ? "disabled" : ""}>Next</button></div>` : `<div class="state">No jobs in this state.</div>`}`;
      view.querySelectorAll("[data-s]").forEach(a => a.onclick = () => { st = a.dataset.s; page = 1; draw().catch(fail); });
      const pv = view.querySelector("#pv"), nx = view.querySelector("#nx"); if (pv) pv.onclick = () => { page--; draw().catch(fail); }; if (nx) nx.onclick = () => { page++; draw().catch(fail); };
      view.querySelectorAll("[data-op]").forEach(b => b.onclick = async () => {
        const op = b.dataset.op; if (op === "quarantine" && !confirm("Quarantine this job? It will not be retried until released.")) return; if (op === "retry" && !confirm("Re-queue this job?")) return;
        try { await api(`/api/admin/jobs/${b.dataset.id}/${op}`, { method: "POST", body: op === "quarantine" ? { reason: "MANUAL_REVIEW" } : undefined }); await draw(); flash("ok", `Job ${op} done.`); } catch (e) { flash("err", e.message); } });
    };
    const actions = (j) => j.state === "QUARANTINED" ? `<button class="btn btn-ghost btn-sm" data-op="release" data-id="${esc(j.id)}">Release</button>` : ["FAILED", "POISONED", "STALE"].includes(j.state) ? `<button class="btn btn-primary btn-sm" data-op="retry" data-id="${esc(j.id)}">Retry</button> <button class="btn btn-ghost btn-sm" data-op="quarantine" data-id="${esc(j.id)}">Quarantine</button>` : j.state === "QUEUED" || j.state === "RETRYING" ? `<button class="btn btn-ghost btn-sm" data-op="quarantine" data-id="${esc(j.id)}">Quarantine</button>` : "";
    await draw();
  },
  async ai() {
    const r = await api("/api/admin/ai"); const obj = (o) => Object.keys(o).length ? Object.entries(o).map(([k, v]) => `${esc(k)}: <strong>${v}</strong>`).join("<br>") : "none recorded";
    view.innerHTML = `<h2>AI operations</h2><p class="sub">${esc(r.scope)}</p><div class="grid">
      <section class="tile"><h3>Provider ${pill(r.provider.status)}</h3><div class="meta">${esc(r.provider.provider)}${r.provider.model ? " · " + esc(r.provider.model) : ""}<br>${esc(r.provider.note)}</div></section>
      <section class="tile"><h3>Requests</h3><div class="meta">succeeded <strong>${r.requests.succeeded}</strong> · failed <strong>${r.requests.failed}</strong><br>availability ${r.availability_ratio != null ? (r.availability_ratio * 100).toFixed(1) + "%" : "no data"}</div></section>
      <section class="tile"><h3>Latency</h3><div class="meta">${r.latency_ms.average != null ? "average <strong>" + r.latency_ms.average + " ms</strong> over " + r.latency_ms.count + " call(s)" : "no calls recorded"}</div></section>
      <section class="tile"><h3>Failed attempts, timeouts &amp; retries</h3><div class="meta">${obj(r.failed_attempts_by_type)}<br>timeouts/retriable: <strong>${r.timeouts_or_retries}</strong></div></section>
      <section class="tile"><h3>Safety rejections</h3><div class="meta">${obj(r.safety_rejections_by_surface)}</div></section>
      <section class="tile"><h3>Grounding rejections</h3><div class="meta">${obj(r.grounding_rejections)}</div></section>
      <section class="tile"><h3>Insufficient evidence</h3><div class="meta">${obj(r.insufficient_evidence_by_surface)}</div></section>
      <section class="tile"><h3>Tokens &amp; cost</h3><div class="meta">${esc((t => (t.available ? `Prompt ${t.tokens.prompt.toLocaleString()} · completion ${t.tokens.completion.toLocaleString()} tokens, as reported by the provider` : 'No provider-reported token usage yet.') + (t.calls_without_usage ? ` (${t.calls_without_usage} call(s) reported no usage)` : '') + ' — ' + (t.cost.available ? `cost ${(t.cost.micro_units / 1e6).toFixed(4)} at operator-configured prices` : t.cost.reason))(r.tokens_and_cost))}</div></section></div><div class="notice">Prompts, responses and patient content are never recorded or shown here.</div>`;
  },
  async security() {
    const r = await api("/api/admin/security");
    const t = (rows, cols) => rows.length ? `<div style="overflow:auto"><table class="t"><thead><tr>${cols.map(c => `<th>${esc(c[0])}</th>`).join("")}</tr></thead><tbody>${rows.map(x => `<tr>${cols.map(c => `<td class="meta">${c[1](x)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : `<div class="meta">None in this window.</div>`;
    view.innerHTML = `<h2>Security operations</h2><p class="sub">Last ${r.window_hours} hours. ${esc(r.note)}</p><div class="grid"><section class="tile"><h3>Events by type</h3><div class="meta">${Object.keys(r.counts_by_type).length ? Object.entries(r.counts_by_type).map(([k, v]) => `${pill(k)} ${v}`).join("<br>") : "No security events."}</div></section>
      <section class="tile"><h3>Sharing &amp; access audit (counts)</h3><div class="meta">${Object.keys(r.audit_counts).length ? Object.entries(r.audit_counts).map(([k, v]) => `${esc(k.toLowerCase().replace(/_/g, " "))}: <strong>${v}</strong>`).join("<br>") : "None."}</div></section></div>
      <h3>Most frequent routes</h3>${t(r.top_routes, [["Route", x => esc(x.route)], ["Type", x => esc(x.event_type)], ["Count", x => x.count]])}
      <h3 style="margin-top:18px">Recent events</h3>${t(r.recent_events, [["Time", x => esc(ts(x.occurred_at))], ["Type", x => esc(x.event_type)], ["Route", x => esc(x.method + " " + x.route)], ["Status", x => x.status_code], ["Client", x => esc(x.client_ref || "")], ["Account", x => esc(x.actor_ref || "—")]])}
      <h3 style="margin-top:18px">Recent sharing &amp; access changes</h3>${t(r.recent_sharing_audit, [["Time", x => esc(ts(x.occurred_at))], ["Action", x => esc(x.action)], ["Actor", x => esc(x.actor_ref || "")]])}
      <h3 style="margin-top:18px">Privileged admin actions</h3>${t(r.recent_admin_audit, [["Time", x => esc(ts(x.occurred_at))], ["Action", x => esc(x.action)], ["Admin", x => esc(x.actor_ref || "")]])}`;
  },
  async users() {
    let page = 1, qv = "";
    const draw = async () => {
      const r = await api(`/api/admin/users?page=${page}${qv ? "&q=" + encodeURIComponent(qv) : ""}`);
      view.innerHTML = `<h2>Users</h2><p class="sub">${esc(r.note)}</p><form class="form" id="uq" style="max-width:360px"><label>Username starts with<input name="q" value="${esc(qv)}" maxlength="40" pattern="[A-Za-z0-9_.\\-]*"></label><button class="btn btn-ghost btn-sm">Search</button></form>
        ${r.items.length ? `<div style="overflow:auto"><table class="t"><thead><tr><th>Username</th><th>Email</th><th>Status</th><th>Verified</th><th>Profiles</th><th>Created</th><th>Sessions v.</th><th></th></tr></thead><tbody>${r.items.map(u => `<tr><td>${esc(u.username)} ${u.is_admin ? '<span class="pill">admin</span>' : ""}</td><td class="meta">${esc(u.email_masked)}</td><td>${pill(u.status)}${u.deletion_pending ? ' <span class="pill warn">deletion pending</span>' : ""}</td><td>${u.email_verified ? "yes" : '<span class="pill warn">no</span>'}</td><td>${u.patient_profile_count}</td><td class="meta">${esc(ts(u.created_at))}</td><td class="meta">${u.session_version}</td><td>${r.can_operate ? `<button class="btn btn-ghost btn-sm" data-rv="${esc(u.id)}">Sign out everywhere</button>` : ""}</td></tr>`).join("")}</tbody></table></div><div style="display:flex;gap:8px;margin-top:8px"><button class="btn btn-ghost btn-sm" id="pv" ${page <= 1 ? "disabled" : ""}>Previous</button><span class="meta">Page ${r.page} · ${r.total} account(s)</span><button class="btn btn-ghost btn-sm" id="nx" ${page * r.page_size >= r.total ? "disabled" : ""}>Next</button></div>` : `<div class="state">No accounts match.</div>`}`;
      view.querySelector("#uq").onsubmit = (e) => { e.preventDefault(); qv = new FormData(e.target).get("q"); page = 1; draw().catch(fail); };
      const pv = view.querySelector("#pv"), nx = view.querySelector("#nx"); if (pv) pv.onclick = () => { page--; draw().catch(fail); }; if (nx) nx.onclick = () => { page++; draw().catch(fail); };
      view.querySelectorAll("[data-rv]").forEach(b => b.onclick = async () => { if (!confirm("Invalidate every session for this account?")) return; try { await api(`/api/admin/users/${b.dataset.rv}/revoke-sessions`, { method: "POST" }); await draw(); flash("ok", "Sessions revoked."); } catch (e) { flash("err", e.message); } });
    };
    await draw();
  },
  async alerts() {
    const draw = async (evaluate) => {
      if (evaluate && me.can_operate) { try { await api("/api/admin/alerts/evaluate", { method: "POST" }); } catch (e) { flash("err", e.message); } }
      const r = await api("/api/admin/alerts");
      view.innerHTML = `<h2>Alerts</h2><p class="sub">${esc(r.delivery_note)}</p>${me.can_operate ? `<button class="btn btn-primary btn-sm" id="ev">Run checks now</button>` : ""}
        ${r.items.length ? `<div style="overflow:auto;margin-top:12px"><table class="t"><thead><tr><th>Severity</th><th>Alert</th><th>Source</th><th>Status</th><th>Seen</th><th>Delivery</th><th></th></tr></thead><tbody>${r.items.map(a => `<tr><td>${pill(a.severity)}</td><td>${esc(a.title)}${a.detail ? `<div class="meta">${esc(a.detail)}</div>` : ""}</td><td class="meta">${esc(a.source)}</td><td>${pill(a.status)}</td><td class="meta">first ${esc(ts(a.first_seen_at))}<br>last ${esc(ts(a.last_seen_at))} · ×${a.occurrences}</td><td class="meta">${pill(a.delivery.state)}</td><td>${r.can_operate && a.status !== "RESOLVED" ? `${a.status === "OPEN" ? `<button class="btn btn-ghost btn-sm" data-al="acknowledge" data-id="${esc(a.id)}">Acknowledge</button> ` : ""}<button class="btn btn-ghost btn-sm" data-al="resolve" data-id="${esc(a.id)}">Resolve</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : `<div class="state" style="margin-top:12px">No alerts. Run the checks to evaluate current conditions.</div>`}`;
      const ev = view.querySelector("#ev"); if (ev) ev.onclick = () => draw(true).catch(fail);
      view.querySelectorAll("[data-al]").forEach(b => b.onclick = async () => { try { await api(`/api/admin/alerts/${b.dataset.id}/${b.dataset.al}`, { method: "POST" }); await draw(false); } catch (e) { flash("err", e.message); } });
    };
    await draw(true);
  },
  async deployment() {
    const d = await api("/api/admin/deployment"); const cfg = d.configuration;
    view.innerHTML = `<h2>Deployment &amp; release</h2><p class="sub">${esc(d.secrets)}</p><div class="grid">
      <section class="tile"><h3>Release ${pill(d.healthy ? "OK" : "DOWN")}</h3><div class="meta">version ${esc(d.version.version)}${d.version.git_sha ? " · " + esc(d.version.git_sha) : ""}<br>environment ${esc(d.version.environment)}<br>python ${esc(d.version.python)}</div></section>
      <section class="tile"><h3>Migrations ${pill(d.migration.status)}</h3><div class="meta">current ${esc(d.migration.current || "—")}<br>head ${esc(d.migration.head || "—")}</div></section>
      <section class="tile"><h3>Workers ${pill(d.workers.status)}</h3><div class="meta">${d.workers.count} heartbeat(s)</div></section>
      <section class="tile"><h3>Queue ${pill(d.queue.status)}</h3><div class="meta">${d.queue.waiting != null ? d.queue.waiting + " waiting" : ""}</div></section></div>
      ${d.configuration_problems.length ? `<div class="err-msg" role="alert"><strong>Configuration problems</strong><br>${d.configuration_problems.map(esc).join("<br>")}</div>` : `<div class="ok-msg">No configuration problems detected for this environment.</div>`}
      <section class="tile"><h3>Configuration checks (booleans only)</h3><table class="t"><tbody>${Object.entries(cfg).map(([k, v]) => `<tr><td>${esc(k.replace(/_/g, " "))}</td><td>${typeof v === "boolean" ? pill(v ? "OK" : "NOT_CONFIGURED") : esc(v)}</td></tr>`).join("")}</tbody></table></section>`;
  },
};
const fail = (e) => { if (needsStepUp(e)) return stepUp(); state(e.message || "Request failed", true); };
async function route() { const k = (location.hash.replace(/^#\//, "") || "overview"); const fn = views[k] || views.overview; nav(views[k] ? k : "overview"); state("Loading…"); try { await fn(); } catch (e) { fail(e); } }
async function boot() {
  try { await hgRequireAuth(); } catch {}
  try { const probe = await api("/api/admin/probe"); if (probe.step_up_required) return stepUp(); }
  catch (e) { return state("This page is not available.", true); }
  try { me = await api("/api/admin/me"); } catch (e) { return fail(e); }
  document.getElementById("who").innerHTML = `<span class="ctx-badge">${esc(me.username)} · ${esc(me.role.toLowerCase())}</span><span class="meta">Operational data only — no patient records are accessible here.</span>`;
  const end = document.getElementById("endsess"); end.hidden = false; end.onclick = async () => { try { await api("/api/admin/session", { method: "DELETE" }); } catch {} location.reload(); };
  window.removeEventListener("hashchange", route); window.addEventListener("hashchange", route); await route();
}
boot();
})();
