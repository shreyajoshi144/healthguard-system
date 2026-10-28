/* Hub views, part 2: vitals, timeline, graph, visit prep, follow-ups, family + bootstrap */
(() => {
const { api, esc, q, fmtDate, fmtTs, provPill, evChips, loading, error, empty, flash, can } = HG;
const iso = (d) => new Date(d).toISOString();
const NS = "http://www.w3.org/2000/svg";

/* ---------------- vitals ---------------- */
function lineChart(series) {
  const pts = series.points; if (!pts.length) return "";
  const W = 760, H = 280, L = 56, R = 16, T = 16, B = 40;
  const t = pts.map(p => new Date(p.t).getTime()); const t0 = Math.min(...t), t1 = Math.max(...t) || t0 + 1;
  const vals = pts.flatMap(p => [p.v, p.v2].filter(v => v != null)); let lo = Math.min(...vals), hi = Math.max(...vals); if (lo === hi) { lo -= 1; hi += 1; } const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const x = (ms) => L + (t1 === t0 ? (W - L - R) / 2 : (ms - t0) / (t1 - t0) * (W - L - R)); const y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
  const path = (key) => pts.filter(p => p[key] != null).map((p, i) => `${i ? "L" : "M"}${x(new Date(p.t).getTime()).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  const dots = (key, color) => pts.filter(p => p[key] != null).map(p => `<circle cx="${x(new Date(p.t).getTime()).toFixed(1)}" cy="${y(p[key]).toFixed(1)}" r="4" fill="${color}"><title>${esc(`${key === "v" ? series.label : series.secondary_label}: ${p[key]} ${series.unit} — ${fmtTs(p.t)} — ${p.provenance}`)}</title></circle>`).join("");
  const ticks = [0, 0.5, 1].map(f => { const v = lo + (hi - lo) * f; return `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#e3e0d4"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="#6b6b5e">${v.toFixed(1)}</text>`; }).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.label)} over time, ${pts.length} readings">${ticks}
    <text x="${L}" y="${H - 14}" font-size="11" fill="#6b6b5e">${esc(new Date(t0).toLocaleDateString())}</text><text x="${W - R}" y="${H - 14}" text-anchor="end" font-size="11" fill="#6b6b5e">${esc(new Date(t1).toLocaleDateString())}</text>
    <text x="14" y="14" font-size="11" fill="#6b6b5e">${esc(series.unit)}</text>
    <path d="${path("v")}" fill="none" stroke="#3f6b52" stroke-width="2"/>${dots("v", "#3f6b52")}
    ${series.secondary_label ? `<path d="${path("v2")}" fill="none" stroke="#b5704a" stroke-width="2"/>${dots("v2", "#b5704a")}` : ""}</svg>
    <div class="meta">${series.secondary_label ? `<span style="color:#3f6b52">●</span> ${series.kind === "BLOOD_PRESSURE" ? "Systolic" : esc(series.label)} <span style="color:#b5704a">●</span> ${esc(series.secondary_label)} · ` : ""}${series.downsampled ? `Showing ${pts.length} of ${series.total_points} readings (evenly thinned; every reading is in the table).` : ""}</div>`;
}
HG.register("vitals", async (el) => {
  const gate = HG.needScope("MEASUREMENTS"); if (gate) { el.innerHTML = `<h2>Vitals</h2>${gate}`; return; }
  const types = (await api("/api/measurement-types")).types; const writable = can("MEASUREMENTS", true);
  let kind = new URLSearchParams(location.hash.split("?")[1] || "").get("kind") || types[0].kind; let range = "all"; let unit = null; let page = 1;
  const draw = async () => {
    const spec = types.find(t => t.kind === kind) || types[0];
    el.innerHTML = `<h2>Vitals</h2><p class="sub">Readings you or your records reported. Values are shown as entered; HealthGuard does not label them normal or abnormal.</p>
      <div class="tabs" role="tablist">${types.map(t => `<a href="#/vitals?kind=${t.kind}" ${t.kind === spec.kind ? 'aria-current="page"' : ""}>${esc(t.label)}</a>`).join("")}</div>
      <div class="seg" role="group" aria-label="Range">${[["30", "30 days"], ["90", "90 days"], ["365", "1 year"], ["all", "All"]].map(([k, l]) => `<button data-r="${k}" aria-pressed="${range === k}">${l}</button>`).join("")}</div>
      ${writable ? `<details class="tile" style="margin-bottom:14px"><summary><strong>Add a ${esc(spec.label.toLowerCase())} reading</strong></summary><form class="form" id="mf"><label>${spec.secondary_label ? "Systolic" : "Value"}<input name="value" type="number" step="any" required></label>${spec.secondary_label ? `<label>${esc(spec.secondary_label)}<input name="value_secondary" type="number" step="any" required></label>` : ""}<label>Unit<select name="unit">${spec.units.map(u => `<option>${esc(u)}</option>`).join("")}</select></label>${spec.contexts.length ? `<label>Context<select name="context"><option value="">—</option>${spec.contexts.map(c => `<option value="${esc(c)}">${esc(c.toLowerCase().replace(/_/g, " "))}</option>`).join("")}</select></label>` : ""}<label>When<input name="measured_at" type="datetime-local" required value="${new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)}"></label><label>Note<input name="note" maxlength="500"></label><button class="btn btn-primary btn-sm">Save reading</button></form></details>` : ""}
      <div id="chart"></div><div id="hist" style="margin-top:16px"></div>`;
    el.querySelectorAll(".seg button").forEach(b => b.onclick = () => { range = b.dataset.r; draw(); });
    const f = el.querySelector("#mf");
    if (f) f.onsubmit = async (e) => { e.preventDefault(); const o = Object.fromEntries(new FormData(f)); const body = { kind: spec.kind, value: Number(o.value), unit: o.unit, measured_at: new Date(o.measured_at).toISOString() };
      if (o.value_secondary) body.value_secondary = Number(o.value_secondary); if (o.context) body.context = o.context; if (o.note) body.note = o.note;
      try { await api(q("/measurements"), { method: "POST", body }); await draw(); flash(el, "ok", "Reading saved."); } catch (err) { flash(el, "err", err.message); } };
    const ch = el.querySelector("#chart"), hist = el.querySelector("#hist"); loading(ch);
    const qs = new URLSearchParams({ kind: spec.kind }); if (unit) qs.set("unit", unit); if (range !== "all") qs.set("start", iso(Date.now() - Number(range) * 864e5));
    try {
      const s = await api(q(`/measurements/series?${qs}`));
      unit = s.unit;
      ch.innerHTML = !s.points.length ? empty(`No ${spec.label.toLowerCase()} readings${range === "all" ? " yet" : " in this range"}.`) :
        `${s.available_units.length > 1 ? `<label class="meta">Unit <select id="u">${s.available_units.map(u => `<option ${u === s.unit ? "selected" : ""}>${esc(u)}</option>`).join("")}</select> (units are never mixed)</label>` : ""}${lineChart(s)}
        <div class="grid" style="margin-top:12px"><div class="tile"><div class="meta">Readings</div><strong>${s.stats.count}</strong></div><div class="tile"><div class="meta">Lowest / highest</div><strong>${s.stats.min} – ${s.stats.max}</strong></div><div class="tile"><div class="meta">Average</div><strong>${s.stats.mean}</strong></div><div class="tile"><div class="meta">Latest</div><strong>${s.stats.latest}${s.stats.secondary ? "/" + s.stats.secondary.latest : ""} ${esc(s.unit)}</strong><div class="meta">${esc(fmtTs(s.stats.latest_at))}</div></div></div><div class="notice">${esc(s.notice)}</div>`;
      const u = ch.querySelector("#u"); if (u) u.onchange = () => { unit = u.value; draw(); };
      const l = await api(q(`/measurements?kind=${spec.kind}&page=${page}&page_size=15`));
      hist.innerHTML = l.total ? `<h3>History</h3><table class="t"><thead><tr><th>When</th><th>Value</th><th>Source</th><th></th></tr></thead><tbody>${l.items.map(m => `<tr><td>${esc(fmtTs(m.measured_at))}</td><td>${esc(m.value)}${m.value_secondary != null ? "/" + esc(m.value_secondary) : ""} ${esc(m.unit)}${m.context ? ` <span class="meta">(${esc(m.context.toLowerCase().replace(/_/g, " "))})</span>` : ""}${m.note ? `<div class="meta">${esc(m.note)}</div>` : ""}</td><td>${provPill(m.provenance)}${m.source_document_id ? evChips([{ source_type: "DOCUMENT", document_id: m.source_document_id }]) : ""}</td><td>${writable ? `<button class="btn btn-ghost btn-sm" data-del="${esc(m.id)}">Delete</button>` : ""}</td></tr>`).join("")}</tbody></table>
        <div style="display:flex;gap:8px;margin-top:8px"><button class="btn btn-ghost btn-sm" id="prev" ${page <= 1 ? "disabled" : ""}>Newer</button><span class="meta">Page ${l.page} of ${Math.max(1, Math.ceil(l.total / l.page_size))}</span><button class="btn btn-ghost btn-sm" id="next" ${page * l.page_size >= l.total ? "disabled" : ""}>Older</button></div>` : "";
      hist.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => { if (!confirm("Delete this reading?")) return; try { await api(`/api/measurements/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); await draw(); } catch (err) { flash(el, "err", err.message); } });
      const pv = hist.querySelector("#prev"), nx = hist.querySelector("#next"); if (pv) pv.onclick = () => { page--; draw(); }; if (nx) nx.onclick = () => { page++; draw(); };
    } catch (e) { error(ch, e, draw); }
  };
  window.addEventListener("hashchange", function h() { if (!location.hash.startsWith("#/vitals")) { window.removeEventListener("hashchange", h); return; } const k = new URLSearchParams(location.hash.split("?")[1] || "").get("kind"); if (k && k !== kind) { kind = k; unit = null; page = 1; draw(); } });
  await draw();
});

/* ---------------- unified timeline ---------------- */
HG.register("timeline", async (el) => {
  const TYPES = { HEALTH_EVENT: "Events", DOCUMENT: "Documents", MEDICATION: "Medications", MEDICATION_EVENT: "Medication changes", INVESTIGATION: "Investigations", INVESTIGATION_RESULT: "Results", HEALTH_ACTION: "Follow-ups", MEASUREMENT: "Vitals", HEALTH_FACT: "Extracted facts", HANDOFF: "Handoffs" };
  let selected = new Set(), offset = 0; const LIMIT = 50;
  const draw = async (append) => {
    if (!append) { offset = 0; el.innerHTML = `<h2>Timeline</h2><p class="sub">Everything in your record in one place, newest first. Dates show exactly how precise they are; undated items come last.</p><div class="tabs" id="flt" role="group" aria-label="Filter by type">${Object.entries(TYPES).map(([k, l]) => `<a href="javascript:void 0" data-t="${k}" ${selected.has(k) ? 'aria-current="page"' : ""}>${esc(l)}</a>`).join("")}</div><div id="tl"></div>`; el.querySelectorAll("#flt a").forEach(a => a.onclick = () => { selected.has(a.dataset.t) ? selected.delete(a.dataset.t) : selected.add(a.dataset.t); draw(); }); loading(el.querySelector("#tl")); }
    const box = el.querySelector("#tl");
    try {
      const r = await api(q(`/unified-timeline?limit=${LIMIT}&offset=${offset}${selected.size ? "&types=" + [...selected].join(",") : ""}`));
      const rows = r.items.map(i => `<div class="it ${i.date ? "" : "undated"}"><span class="pill">${esc(TYPES[i.item_type] || i.item_type)}</span> <strong>${esc(i.title || i.item_type)}</strong> ${i.relation === "CONTRADICTORY" ? '<span class="pill warn">conflicts with another record</span>' : ""} ${provPill(i.provenance, i.provenance === "SYSTEM_DERIVED")}<div class="meta">${esc(fmtDate(i.date, i.date_precision))}${i.details && i.details.date_meaning ? " · " + esc(i.details.date_meaning) : ""}${i.details && i.details.status ? " · " + esc(String(i.details.status).toLowerCase()) : ""}${i.page ? ` · page ${esc(i.page)}` : ""}</div>${i.source_text ? `<div class="meta">“${esc(String(i.source_text).slice(0, 200))}”</div>` : ""}${i.document_id ? `<div>${evChips([{ source_type: "DOCUMENT", document_id: i.document_id, page: i.page }])}</div>` : ""}</div>`).join("");
      if (!append) box.innerHTML = r.total ? `<div class="meta" style="margin-bottom:10px">${r.total} item(s)${r.unavailable.length ? ` · not shown (no access): ${esc(r.unavailable.join(", "))}` : ""}</div><div class="tl" id="items"></div><div id="more"></div>` : empty(selected.size ? "Nothing of this type in the record." : "Your timeline is empty. Upload a document or add an event, medication or reading to start it.");
      if (r.total) { el.querySelector("#items").insertAdjacentHTML("beforeend", rows); const more = el.querySelector("#more"); offset += r.items.length; more.innerHTML = offset < r.total ? `<button class="btn btn-ghost" id="lm" style="margin-top:12px">Load more (${r.total - offset} left)</button>` : ""; const lm = more.querySelector("#lm"); if (lm) lm.onclick = () => draw(true); }
    } catch (e) { error(box, e, () => draw(append)); }
  };
  await draw(false);
});

/* ---------------- personal health graph ---------------- */
HG.register("graph", async (el) => {
  const g = await api(q("/health-graph"));
  const COL = { PATIENT: 0, VITALS: 1, EVENT: 1, MEDICATION: 1, INVESTIGATION: 1, FOLLOW_UP: 1, HANDOFF: 1, MEDICATION_EVENT: 2, RESULT: 2, FACT: 2, DOCUMENT: 3 };
  const COLOR = { PATIENT: "#1f3a2e", DOCUMENT: "#6b6b5e", MEDICATION: "#3f6b52", MEDICATION_EVENT: "#7fa58c", INVESTIGATION: "#b5704a", RESULT: "#d39a7a", EVENT: "#c8a24a", FOLLOW_UP: "#5a7fa8", FACT: "#9a9a8a", VITALS: "#4a8f8f", HANDOFF: "#8a6fa8" };
  if (g.node_count <= 1) { el.innerHTML = `<h2>Personal health graph</h2>${empty("There is nothing to connect yet. Add documents, medications, investigations, events or follow-ups to see how they relate.")}`; return; }
  const cols = [[], [], [], []]; g.nodes.forEach(n => cols[COL[n.type] ?? 1].push(n));
  const rowH = 30, H = Math.max(520, Math.max(...cols.map(c => c.length)) * rowH + 40), W = 1100, xs = [60, 340, 660, 960]; const pos = {};
  cols.forEach((c, ci) => { c.sort((a, b) => a.type.localeCompare(b.type) || (a.label || "").localeCompare(b.label || "")); c.forEach((n, i) => { pos[n.id] = { x: xs[ci], y: (H / (c.length + 1)) * (i + 1) }; }); });
  const edge = (e) => { const a = pos[e.source], b = pos[e.target]; if (!a || !b) return ""; const c = e.relation === "CONFLICTS_WITH" ? "#b5704a" : e.relation === "EVIDENCE" ? "#9a9a8a" : "#3f6b52"; return `<path class="e" data-s="${esc(e.source)}" data-t="${esc(e.target)}" d="M${a.x},${a.y} C${(a.x + b.x) / 2},${a.y} ${(a.x + b.x) / 2},${b.y} ${b.x},${b.y}" fill="none" stroke="${c}" stroke-width="${e.relation === "CONFLICTS_WITH" ? 2.5 : 1.2}" ${e.relation === "EVIDENCE" ? 'stroke-dasharray="4 3"' : ""} opacity=".55"><title>${esc(e.relation.replace(/_/g, " ").toLowerCase())}</title></path>`; };
  const node = (n) => { const p = pos[n.id]; return `<g class="n" tabindex="0" role="button" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})" style="cursor:pointer"><circle r="8" fill="${COLOR[n.type] || "#999"}" stroke="${n.provenance === "SYSTEM_DERIVED" ? "#c8a24a" : "#fff"}" stroke-width="2"/><text x="12" y="4" font-size="11" fill="#1f2a24">${esc((n.label || n.type).slice(0, 34))}</text></g>`; };
  el.innerHTML = `<h2>Personal health graph</h2><p class="sub">${esc(g.notice)}</p>${g.truncated ? `<div class="notice">Showing ${g.node_count} of ${g.total_possible_nodes} items. The graph is capped for readability; use the timeline for the complete record.</div>` : ""}
    <div class="meta" style="margin-bottom:8px">${Object.entries(COLOR).map(([k, c]) => `<span style="color:${c}">●</span> ${esc(k.replace(/_/g, " ").toLowerCase())}`).join(" · ")} · dashed = evidence link · orange line = records conflict</div>
    <div style="overflow:auto"><svg class="graph" viewBox="0 0 ${W} ${H}" style="min-width:900px;height:${H}px" role="group" aria-label="Relationship graph of your health record">${g.edges.map(edge).join("")}${g.nodes.map(node).join("")}</svg></div><div id="detail" class="tile" style="margin-top:12px" aria-live="polite"><span class="meta">Select an item to see what it connects to.</span></div>`;
  const byId = Object.fromEntries(g.nodes.map(n => [n.id, n]));
  const pick = (id) => { const n = byId[id]; const rel = g.edges.filter(e => e.source === id || e.target === id);
    el.querySelectorAll(".e").forEach(p => { const on = p.dataset.s === id || p.dataset.t === id; p.setAttribute("opacity", on ? "1" : ".15"); });
    el.querySelector("#detail").innerHTML = `<strong>${esc(n.label)}</strong> <span class="pill">${esc(n.type.replace(/_/g, " ").toLowerCase())}</span> ${provPill(n.provenance)}<div class="meta">${n.date ? esc(fmtDate(n.date, n.date_precision || "EXACT")) : ""} ${n.status ? "· " + esc(n.status.toLowerCase()) : ""}${n.page ? ` · page ${esc(n.page)}` : ""}</div><div style="margin-top:8px">${rel.map(e => { const other = byId[e.source === id ? e.target : e.source]; return other ? `<div class="row">${esc(e.relation.replace(/_/g, " ").toLowerCase())} ${e.source === id ? "→" : "←"} ${esc(other.label)} <span class="meta">(${esc(other.type.toLowerCase())})</span></div>` : ""; }).join("") || '<span class="meta">No connections.</span>'}</div>${n.type === "DOCUMENT" ? evChips([{ source_type: "DOCUMENT", document_id: id.split(":")[1] }]) : ""}`; };
  el.querySelectorAll(".n").forEach(g_ => { g_.onclick = () => pick(g_.dataset.id); g_.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(g_.dataset.id); } }; });
});

/* ---------------- doctor visit / second opinion prep ---------------- */
HG.register("visit", async (el) => {
  const [sum, qn] = await Promise.all([api(q("/summary?mode=detailed")), api(q("/doctor-questions"))]);
  let handoffs = []; const canH = can("HANDOFFS"); if (canH) { try { handoffs = await api(q("/handoffs")); } catch { handoffs = []; } }
  const sec = (key) => sum.sections.find(s => s.key === key);
  const block = (title, s, none) => `<section class="tile" style="margin-bottom:12px"><h3>${esc(title)}</h3>${s && s.items.length ? `<ul>${s.items.map(it => `<li class="row">${esc(it.text)} ${provPill(it.provenance, it.unconfirmed)}<div class="meta">${esc(fmtDate(it.date, it.date_precision))}${it.detail ? " · " + esc(it.detail) : ""}</div><div>${evChips(it.refs)}</div></li>`).join("")}</ul>` : `<div class="meta">${esc(none)}</div>`}</section>`;
  el.innerHTML = `<h2>Doctor visit &amp; second-opinion preparation</h2><p class="sub">A single page you can review, print, or hand to a clinician. Everything is drawn from your records and linked to its source.</p>
    <div class="form" style="grid-template-columns:1fr"><label>Reason for this visit (printed with the page; not saved)<input id="reason" maxlength="300" placeholder="e.g. Annual review / second opinion on my medication plan"></label></div><div id="reasonout" class="meta" style="display:none"></div>
    ${block("Health context", sec("profile"), "No profile details.")}${block("Conditions documented", sec("conditions"), "None documented.")}${block("Current medications", sec("medications"), "None recorded.")}${block("Medication changes", sec("medication_changes"), "None recorded.")}${block("Investigations and results", sec("investigations"), "None recorded.")}${block("Recent events", sec("events"), "None recorded.")}${block("Vitals", sec("vitals"), "None recorded.")}${block("Open follow-ups", sec("follow_ups"), "None open.")}${block("Conflicts and uncertainty", sec("conflicts"), "No disagreements detected.")}
    <section class="tile" style="margin-bottom:12px"><h3>Questions to ask</h3>${qn.suggested_questions.length ? `<ol>${qn.suggested_questions.map(x => `<li>${esc(x.question)}</li>`).join("")}</ol>` : `<div class="meta">${esc(qn.empty_message || "")}</div>`}</section>
    ${sum.limitations.length ? `<div class="notice">${sum.limitations.map(esc).join("<br>")}</div>` : ""}<div class="notice">${esc(sum.notice)}</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn btn-primary btn-sm" onclick="window.print()">Print / save as PDF</button><a class="btn btn-ghost btn-sm" href="#/family">Share temporarily with a clinician</a></div>
    ${canH ? `<h3 style="margin-top:28px">Evidence-backed handoff documents</h3><p class="sub">Generate a draft from your authorized evidence, edit it, then finalize. Finalized handoffs are immutable.</p>${can("HANDOFFS", true) ? `<button class="btn btn-primary btn-sm" id="gen">Generate a new draft</button>` : ""}<div id="hl" style="display:grid;gap:12px;margin-top:12px"></div>` : ""}`;
  const reason = el.querySelector("#reason"), ro = el.querySelector("#reasonout"); reason.oninput = () => { ro.style.display = reason.value ? "block" : "none"; ro.textContent = "Reason for visit: " + reason.value; };
  if (!canH) return;
  const hl = el.querySelector("#hl");
  const drawH = () => { hl.innerHTML = handoffs.length ? handoffs.map(h => `<section class="tile"><h3>${esc(h.title)} <span class="pill ${h.status === "FINAL" ? "ok" : "warn"}">${esc(h.status.toLowerCase())}</span></h3><div class="meta">${esc(fmtTs(h.updated_at))} · ${(h.evidence_refs || []).length} evidence reference(s)</div>${h.status === "FINAL" ? `<pre style="white-space:pre-wrap;font:inherit">${esc(h.content)}</pre>` : `<textarea data-id="${esc(h.id)}" style="width:100%;min-height:140px">${esc(h.content)}</textarea><div style="display:flex;gap:8px;margin-top:6px"><button class="btn btn-ghost btn-sm" data-save="${esc(h.id)}">Save edits</button><button class="btn btn-primary btn-sm" data-fin="${esc(h.id)}">Finalize</button></div>`}</section>`).join("") : empty("No handoff drafts yet."); 
    hl.querySelectorAll("[data-save]").forEach(b => b.onclick = async () => { try { await api(`/api/handoffs/${b.dataset.save}`, { method: "PATCH", body: { content: hl.querySelector(`textarea[data-id="${b.dataset.save}"]`).value } }); flash(el, "ok", "Draft saved."); } catch (e) { flash(el, "err", e.message); } });
    hl.querySelectorAll("[data-fin]").forEach(b => b.onclick = async () => { if (!confirm("Finalize this handoff? It cannot be edited afterwards.")) return; try { const h = await api(`/api/handoffs/${b.dataset.fin}/finalize`, { method: "POST" }); handoffs = handoffs.map(x => x.id === h.id ? h : x); drawH(); flash(el, "ok", "Handoff finalized."); } catch (e) { flash(el, "err", e.message); } }); };
  drawH();
  const gen = el.querySelector("#gen"); if (gen) gen.onclick = async () => { gen.disabled = true; try { const h = await api(q("/handoffs"), { method: "POST", body: { title: "Doctor handoff", handoff_type: "DOCTOR_HANDOFF" } }); handoffs.unshift(h); drawH(); flash(el, "ok", "Draft generated from your records."); } catch (e) { flash(el, "err", e.message); } gen.disabled = false; };
});

/* ---------------- follow-ups / reminders ---------------- */
HG.register("followups", async (el) => {
  const gate = HG.needScope("HEALTH_ACTIONS"); if (gate) { el.innerHTML = `<h2>Follow-ups</h2>${gate}`; return; }
  const acts = await api(q("/health-actions")); const w = can("HEALTH_ACTIONS", true); const today = new Date().toISOString().slice(0, 10);
  const suggested = (a) => a.source === "DOCUMENT_DERIVED" && !a.confirmed_at && a.status === "PLANNED";
  const groups = [["Suggested from your documents — confirm or dismiss", a => suggested(a)], ["Scheduled", a => a.status === "SCHEDULED"], ["Planned (no date)", a => a.status === "PLANNED" && !suggested(a)], ["Completed", a => a.status === "COMPLETED"], ["Cancelled", a => a.status === "CANCELLED"]];
  const card = (a) => `<li class="row"><strong>${esc(a.title)}</strong> ${suggested(a) ? '<span class="pill warn">suggested · not confirmed</span>' : a.source === "DOCUMENT_DERIVED" ? '<span class="pill ok">confirmed by you</span>' : '<span class="pill">created by you</span>'} ${a.status === "SCHEDULED" && a.due_date < today ? '<span class="pill bad">past its date</span>' : ""}<div class="meta">${esc(fmtDate(a.due_date, a.temporal_precision))}${a.due_date ? "" : " — HealthGuard does not invent deadlines"}${a.description ? " · " + esc(a.description) : ""}</div><div>${a.source_document_id ? evChips([{ source_type: "DOCUMENT", document_id: a.source_document_id }]) : ""}</div>
    ${w && ["PLANNED", "SCHEDULED"].includes(a.status) ? `<div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">${suggested(a) ? `<button class="btn btn-primary btn-sm" data-act="confirm" data-id="${esc(a.id)}">Confirm</button>` : `<button class="btn btn-primary btn-sm" data-act="complete" data-id="${esc(a.id)}">Mark done</button>`}<button class="btn btn-ghost btn-sm" data-act="cancel" data-id="${esc(a.id)}">${suggested(a) ? "Dismiss" : "Cancel"}</button></div>` : ""}</li>`;
  el.innerHTML = `<h2>Follow-ups &amp; reminders</h2><p class="sub">Only dates you or your documents actually state are shown. Suggestions found in documents stay separate until you confirm them.</p>
    ${w ? `<details class="tile" style="margin-bottom:14px"><summary><strong>Add a follow-up</strong></summary><form class="form" id="af"><label>What<input name="title" required maxlength="200"></label><label>Date (optional)<input type="date" name="due_date"></label><label>Notes<input name="description" maxlength="500"></label><button class="btn btn-primary btn-sm">Add</button></form></details>` : ""}
    ${acts.length ? groups.map(([t, f]) => { const rows = acts.filter(f); return rows.length ? `<section class="tile" style="margin-bottom:12px"><h3>${esc(t)} <span class="meta">${rows.length}</span></h3><ul>${rows.map(card).join("")}</ul></section>` : ""; }).join("") : empty("No follow-ups yet.")}
    <section class="tile" id="rem" style="margin-top:16px"><h3>Email reminders</h3><div id="rembody"><span class="spin"></span>Loading…</div></section>`;
  const af = el.querySelector("#af"); if (af) af.onsubmit = async (e) => { e.preventDefault(); const o = Object.fromEntries(new FormData(af)); const body = { title: o.title }; if (o.description) body.description = o.description; if (o.due_date) { body.due_date = o.due_date; body.temporal_precision = "EXACT"; } try { await api(q("/health-actions"), { method: "POST", body }); HG.route(); } catch (err) { flash(el, "err", err.message); } };
  el.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => { try { await api(`/api/health-actions/${b.dataset.id}/${b.dataset.act}`, { method: "POST" }); HG.route(); } catch (err) { flash(el, "err", err.message); } });
  await remindersPanel(el.querySelector("#rembody"));
});

async function remindersPanel(box) {
  const STATUS = { PENDING: ["scheduled", ""], SENDING: ["sending", "warn"], SENT: ["sent", "ok"], FAILED: ["failed", "bad"], CANCELLED: ["cancelled", ""], EXPIRED: ["expired (too late to send)", ""], SKIPPED: ["skipped", ""] };
  const ERR = { smtp_not_configured: "email delivery is not configured on this server", recipient_refused: "your email address was refused", smtp_auth_failed: "the mail server rejected our login", smtp_unavailable: "the mail server was unreachable", lease_expired: "the sender stopped unexpectedly", unexpected_error: "an unexpected error occurred" };
  const LEAD = { 0: "On the day", 1: "1 day before", 2: "2 days before", 3: "3 days before", 7: "1 week before", 14: "2 weeks before" };
  const draw = async () => {
    try {
      const [p, d] = await Promise.all([api(q("/reminder-preferences")), api(q("/reminder-deliveries?page_size=10"))]);
      const tz = p.timezone === "UTC" ? (Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC") : p.timezone;
      box.innerHTML = `<p class="meta">Email only. Reminders are sent for follow-ups with an exact date that you created or confirmed — never for suggestions awaiting confirmation or for approximate dates. Emails contain no health details unless you choose to include the title.</p>
        ${!p.email_verified ? `<div class="err-msg">Verify your email address to turn on reminders.</div>` : ""}${!p.email_delivery_configured ? `<div class="notice">Email delivery is not configured on this server, so reminders cannot be sent yet.</div>` : ""}
        <form class="form" id="rp" style="grid-template-columns:1fr 1fr"><label style="grid-column:1/-1;flex-direction:row;display:flex;gap:8px;align-items:center"><input type="checkbox" name="enabled" ${p.enabled ? "checked" : ""}> Email me reminders for this profile's follow-ups</label>
          <fieldset style="grid-column:1/-1;border:0;padding:0"><legend class="meta">When (choose up to ${p.max_lead_days})</legend><div style="display:flex;flex-wrap:wrap;gap:10px">${p.allowed_lead_days.map(n => `<label style="display:flex;gap:4px;align-items:center;font-size:13px"><input type="checkbox" name="lead" value="${n}" ${p.lead_days.includes(n) ? "checked" : ""}> ${esc(LEAD[n])}</label>`).join("")}</div></fieldset>
          <label>Time of day<select name="send_hour">${Array.from({ length: 24 }, (_, h) => `<option value="${h}" ${h === p.send_hour ? "selected" : ""}>${String(h).padStart(2, "0")}:00</option>`).join("")}</select></label>
          <label>Time zone<input name="timezone" value="${esc(tz)}" maxlength="64" required></label>
          <label style="grid-column:1/-1;flex-direction:row;display:flex;gap:8px;align-items:center"><input type="checkbox" name="include_title" ${p.include_title ? "checked" : ""}> Include the follow-up title in the email (emails are not end-to-end private)</label>
          <button class="btn btn-primary btn-sm">Save reminder settings</button></form>
        <h4>Recent reminder emails</h4>${d.items.length ? `<table class="t"><thead><tr><th>Follow-up</th><th>Reminder</th><th>Status</th></tr></thead><tbody>${d.items.map(r => `<tr><td>${esc(r.action_title || "Follow-up")}<div class="meta">due ${esc(r.due_date)}</div></td><td class="meta">${esc(LEAD[r.lead_days] || r.lead_days + " days before")}<br>${esc(fmtTs(r.scheduled_for))}</td><td><span class="pill ${STATUS[r.status][1]}">${esc(STATUS[r.status][0])}</span>${r.attempts ? `<div class="meta">attempt${r.attempts > 1 ? "s" : ""}: ${r.attempts}</div>` : ""}${r.last_error ? `<div class="meta">${esc(ERR[r.last_error] || r.last_error)}</div>` : ""}${r.status === "PENDING" && r.next_attempt_at ? `<div class="meta">retrying ${esc(fmtTs(r.next_attempt_at))}</div>` : ""}${r.sent_at ? `<div class="meta">${esc(fmtTs(r.sent_at))}</div>` : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="meta">No reminder emails yet.</div>`}`;
      box.querySelector("#rp").onsubmit = async (e) => {
        e.preventDefault(); const f = e.target; const fd = new FormData(f);
        const body = { enabled: f.enabled.checked, lead_days: fd.getAll("lead").map(Number), send_hour: Number(fd.get("send_hour")), timezone: String(fd.get("timezone")).trim(), include_title: f.include_title.checked };
        try { await api(q("/reminder-preferences"), { method: "PUT", body }); await draw(); flash(box, "ok", body.enabled ? "Reminders are on." : "Reminders are off."); } catch (err) { flash(box, "err", err.message === "Invalid request." ? "Please check the options you selected." : err.message); }
      };
    } catch (e) { HG.error(box, e, draw); }
  };
  await draw();
}

/* ---------------- family & sharing ---------------- */
HG.register("family", async (el) => {
  const p = HG.st.profile;
  const profiles = HG.st.profiles;
  const scopesAll = ["PROFILE", "DOCUMENTS", "MEDICATIONS", "INVESTIGATIONS", "TIMELINE", "HEALTH_ACTIONS", "MEASUREMENTS", "HANDOFFS"];
  const label = (s) => s.toLowerCase().replace(/_/g, " ");
  let rel = [], shares = [];
  if (p.is_owner) { [rel, shares] = await Promise.all([api(q("/access")).catch(() => []), api(q("/shares")).catch(() => [])]); }
  const scopeBoxes = (name) => scopesAll.map(s => `<label style="display:flex;gap:4px;align-items:center;font-size:13px"><input type="checkbox" name="${name}" value="${s}" ${s === "PROFILE" ? "checked" : ""}> ${esc(label(s))}</label>`).join("");
  el.innerHTML = `<h2>Family health &amp; sharing</h2><p class="sub">Switch between the profiles you manage or that were shared with you. The profile you are viewing is always shown at the top of every page.</p>
    <section class="tile" style="margin-bottom:16px"><h3>Profiles</h3><table class="t"><thead><tr><th>Profile</th><th>Your role</th><th>Access</th><th></th></tr></thead><tbody>${profiles.map(x => `<tr><td><strong>${esc(x.name)}</strong>${x.patient_id === HG.st.patientId ? ' <span class="pill ok">viewing</span>' : ""}</td><td>${x.is_owner ? "Owner" : esc(x.relationship.toLowerCase())}</td><td class="meta">${x.is_owner ? `${x.active_relationships} ongoing, ${x.active_temporary_shares} temporary` : esc(x.scopes.map(label).join(", ")) + (x.expires_at ? ` · ends ${esc(fmtTs(x.expires_at))}` : "")}</td><td>${x.patient_id === HG.st.patientId ? "" : `<a class="btn btn-ghost btn-sm" href="/hub?patient_id=${encodeURIComponent(x.patient_id)}#/overview">Switch</a>`}</td></tr>`).join("")}</tbody></table><div class="meta" style="margin-top:8px">To add a new family member's profile, use <a href="/health">Manage patients</a>.</div></section>
    ${p.is_owner ? `<section class="tile" style="margin-bottom:16px"><h3>Caregiver &amp; family access to this profile</h3>
      ${rel.filter(r => r.status === "ACTIVE" && !r.revoked_at).length ? `<table class="t"><thead><tr><th>Person</th><th>Role</th><th>Can see</th><th>Can edit</th><th>Ends</th><th></th></tr></thead><tbody>${rel.filter(r => r.status === "ACTIVE" && !r.revoked_at).map(r => `<tr><td class="meta">${esc(r.email || r.user_id.slice(0, 8))}</td><td>${esc(r.role.toLowerCase())}</td><td class="meta">${esc(r.scopes.map(label).join(", "))}</td><td class="meta">${esc((r.write_scopes || []).map(label).join(", ") || "view only")}</td><td class="meta">${r.expires_at ? esc(fmtTs(r.expires_at)) : "no end"}</td><td><button class="btn btn-ghost btn-sm" data-rev="${esc(r.user_id)}">Revoke</button></td></tr>`).join("")}</tbody></table>` : `<div class="meta">Nobody else has ongoing access.</div>`}
      <details style="margin-top:10px"><summary><strong>Give someone access</strong></summary><form class="form" id="rf" style="grid-template-columns:1fr 1fr"><label>Their HealthGuard email<input type="email" name="user_email" required></label><label>Relationship<select name="role"><option value="FAMILY">Family</option><option value="CAREGIVER">Caregiver</option></select></label><label>Access ends (optional)<input type="datetime-local" name="expires_at"></label><fieldset style="grid-column:1/-1;border:0;padding:0"><legend class="meta">What they can see</legend><div style="display:flex;flex-wrap:wrap;gap:10px">${scopeBoxes("scopes")}</div></fieldset><fieldset style="grid-column:1/-1;border:0;padding:0"><legend class="meta">What they can also edit (optional)</legend><div style="display:flex;flex-wrap:wrap;gap:10px">${scopesAll.map(s => `<label style="display:flex;gap:4px;align-items:center;font-size:13px"><input type="checkbox" name="write_scopes" value="${s}"> ${esc(label(s))}</label>`).join("")}</div></fieldset><button class="btn btn-primary btn-sm">Grant access</button></form></details></section>
    <section class="tile"><h3>Temporary sharing (e.g. with a doctor)</h3>
      ${shares.length ? `<table class="t"><thead><tr><th>Status</th><th>Can see</th><th>Ends</th><th></th></tr></thead><tbody>${shares.map(s => `<tr><td><span class="pill ${s.status === "ACTIVE" ? "ok" : "warn"}">${esc(s.status.toLowerCase())}</span></td><td class="meta">${esc(s.scopes.map(label).join(", "))}</td><td class="meta">${esc(fmtTs(s.expires_at))}</td><td>${s.status === "ACTIVE" ? `<button class="btn btn-ghost btn-sm" data-revs="${esc(s.id)}">Revoke now</button>` : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="meta">No temporary shares.</div>`}
      <details style="margin-top:10px"><summary><strong>Create a temporary share link</strong></summary><form class="form" id="sf" style="grid-template-columns:1fr"><label>Ends at (required, max 30 days)<input type="datetime-local" name="expires_at" required></label><fieldset style="border:0;padding:0"><legend class="meta">What it shows</legend><div style="display:flex;flex-wrap:wrap;gap:10px">${scopeBoxes("sscopes")}</div></fieldset><button class="btn btn-primary btn-sm">Create link</button></form><div id="newshare"></div></details></section>` : `<div class="notice">Only the profile owner can change who has access. Your access is shown in the bar at the top.</div>`}`;
  const chk = (f, n) => [...f.querySelectorAll(`input[name="${n}"]:checked`)].map(i => i.value);
  const rf = el.querySelector("#rf"); if (rf) rf.onsubmit = async (e) => { e.preventDefault(); const o = Object.fromEntries(new FormData(rf)); const body = { user_email: o.user_email, role: o.role, scopes: chk(rf, "scopes"), write_scopes: chk(rf, "write_scopes") }; if (o.expires_at) body.expires_at = new Date(o.expires_at).toISOString(); try { await api(q("/access"), { method: "POST", body }); HG.route(); } catch (err) { flash(el, "err", err.message === "Invalid request." ? "Please check the email address and the options you selected." : err.message); } };
  el.querySelectorAll("[data-rev]").forEach(b => b.onclick = async () => { if (!confirm("Revoke this person's access immediately?")) return; try { await api(q(`/access/${b.dataset.rev}`), { method: "DELETE" }); HG.route(); } catch (err) { flash(el, "err", err.message); } });
  el.querySelectorAll("[data-revs]").forEach(b => b.onclick = async () => { if (!confirm("Revoke this temporary share now?")) return; try { await api(`/api/shares/${b.dataset.revs}/revoke`, { method: "POST" }); HG.route(); } catch (err) { flash(el, "err", err.message); } });
  const sf = el.querySelector("#sf"); if (sf) sf.onsubmit = async (e) => { e.preventDefault(); const o = Object.fromEntries(new FormData(sf)); try { const r = await api(q("/shares"), { method: "POST", body: { scopes: chk(sf, "sscopes"), resources: {}, expires_at: new Date(o.expires_at).toISOString() } }); el.querySelector("#newshare").innerHTML = `<div class="ok-msg">Share created. Copy this one-time access code now — it will not be shown again:<br><code style="word-break:break-all">${esc(r.token)}</code></div>`; } catch (err) { flash(el, "err", err.message); } };
});

hgRequireAuth().then(u => { if (u) { hgInitWorkspaceShell(u); HG.boot(); } });
})();
