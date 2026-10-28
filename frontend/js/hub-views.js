/* Hub views, part 1: overview, summary, ask, doctor questions, medications */
(() => {
const { api, esc, q, fmtDate, fmtTs, provPill, evChips, loading, error, empty, flash, can } = HG;

HG.register("overview", async (el) => {
  const d = await api(q("/dashboard")); const s = d.sections;
  const tile = (title, link, body) => `<section class="tile"><h3>${esc(title)}${link ? `<a class="meta" href="#/${link}">Open →</a>` : ""}</h3>${body}</section>`;
  const list = (rows, none) => rows.length ? `<ul>${rows.join("")}</ul>` : `<div class="meta">${esc(none)}</div>`;
  const tiles = [];
  if (s.documents) tiles.push(tile("Documents", null, `<div><strong>${s.documents.total}</strong> uploaded ${Object.entries(s.documents.by_status).map(([k, v]) => `<span class="pill ${k === "FAILED" ? "bad" : k === "COMPLETED" ? "ok" : "warn"}">${v} ${esc(k.toLowerCase())}</span>`).join(" ")}</div>` + list(s.documents.recent.map(x => `<li class="row">${esc(x.filename)}<div class="meta">${esc(x.document_type)} · ${esc(fmtTs(x.created_at))}</div></li>`), "No documents yet.") + `<a class="btn btn-ghost btn-sm" href="/patient?id=${encodeURIComponent(HG.st.patientId)}">Upload or manage</a>`));
  if (s.medications) tiles.push(tile("Medications", "medications", `<div><strong>${s.medications.active_count}</strong> active (as recorded)</div>` + list(s.medications.active.map(m => `<li class="row">${esc([m.name, m.dose, m.frequency].filter(Boolean).join(" · "))} ${provPill(m.provenance, m.unconfirmed)}</li>`), "No active medications recorded.") + (s.medications.recent_changes.length ? `<div class="meta" style="margin-top:8px">Recent changes</div>` + list(s.medications.recent_changes.map(c => `<li class="row">${esc(c.medicine)} — ${esc(c.type.toLowerCase())}<div class="meta">${esc(fmtDate(c.date, c.date ? "EXACT" : "UNKNOWN"))}</div></li>`), "") : "")));
  if (s.investigations) tiles.push(tile("Investigations & results", "timeline", list(s.investigations.recent.map(i => `<li class="row">${esc(i.name)} <span class="pill">${esc(i.status.toLowerCase())}</span><div class="meta">${esc(fmtDate(i.date, i.date ? "EXACT" : "UNKNOWN"))}${i.result_as_reported ? ` · as reported: ${esc(i.result_as_reported)}` : i.has_result ? "" : " · no result recorded"}</div></li>`), "No investigations recorded.")));
  if (s.events) tiles.push(tile("Recent events", "timeline", list(s.events.recent.map(e => `<li class="row">${esc(e.title)}<div class="meta">${esc(fmtDate(e.date, e.date_precision))} ${provPill(e.provenance)}</div></li>`), "No health events recorded.")));
  if (s.vitals) tiles.push(tile("Vitals", "vitals", list(s.vitals.latest.map(v => `<li class="row">${esc(v.label)}: <strong>${esc(v.value)}${v.value_secondary != null ? "/" + esc(v.value_secondary) : ""} ${esc(v.unit)}</strong><div class="meta">${esc(fmtTs(v.measured_at))} · ${v.count} reading(s) ${provPill(v.provenance)}</div></li>`), "No vitals recorded yet.")));
  if (s.follow_ups) tiles.push(tile("Follow-ups", "followups", `<div><strong>${s.follow_ups.open_count}</strong> open${s.follow_ups.suggested_unconfirmed_count ? ` · <span class="pill warn">${s.follow_ups.suggested_unconfirmed_count} suggested, awaiting your confirmation</span>` : ""}</div>` + list(s.follow_ups.upcoming.map(a => `<li class="row">${esc(a.title)} ${a.confirmed ? "" : '<span class="pill warn">suggested</span>'} ${a.overdue ? '<span class="pill bad">past its date</span>' : ""}<div class="meta">${esc(fmtDate(a.due_date, a.date_precision))}</div></li>`), "No open follow-ups.")));
  tiles.push(tile("Conflicts & uncertainty", "summary", s.conflicts.count ? `<div><span class="pill warn">${s.conflicts.count} disagreement(s) between records</span></div>` + list(s.conflicts.items.map(c => `<li class="row">${esc(c.title)}<div>${evChips(c.refs)}</div></li>`), "") : `<div class="meta">No disagreements between your records were detected.</div>`));
  if (s.family) tiles.push(tile("Family & sharing", "family", `<div><strong>${s.family.active_relationships}</strong> people with ongoing access · <strong>${s.family.active_temporary_shares}</strong> temporary share(s)</div>`));
  const quick = [["summary", "Health summary"], ["ask", "Ask my records"], ["questions", "Questions for my doctor"], ["visit", "Prepare for a visit"]].map(([k, l]) => `<a class="btn btn-ghost btn-sm" href="#/${k}">${l}</a>`).join(" ");
  const nothing = !tiles.some(t => true) ? "" : "";
  el.innerHTML = `<h2>${esc(d.patient.name || "Shared profile")}</h2><p class="sub">Overview built from the records you are allowed to see.</p><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">${quick}</div>` +
    (d.unavailable.length ? `<div class="notice">Not shown because it is outside your access: ${esc(d.unavailable.join(", "))}.</div>` : "") + `<div class="grid">${tiles.join("")}</div>`;
});

HG.register("summary", async (el) => {
  let mode = "short";
  const draw = async () => {
    el.innerHTML = `<h2>Health summary</h2><p class="sub">Assembled directly from your records. Every line shows where it came from.</p><div class="seg" role="group" aria-label="Summary length"><button data-m="short" aria-pressed="${mode === "short"}">Short</button><button data-m="detailed" aria-pressed="${mode === "detailed"}">Detailed</button></div><div id="sum"></div>`;
    el.querySelectorAll(".seg button").forEach(b => b.onclick = () => { mode = b.dataset.m; draw(); });
    const box = el.querySelector("#sum"); loading(box, "Building your summary…");
    try {
      const r = await api(q(`/summary?mode=${mode}`));
      box.innerHTML = r.sections.map(sec => `<section class="tile" style="margin-bottom:12px"><h3>${esc(sec.title)}${sec.total > sec.items.length ? `<span class="meta">showing ${sec.items.length} of ${sec.total}</span>` : ""}</h3>` +
        (sec.items.length ? `<ul>${sec.items.map(it => `<li class="row">${esc(it.text)} ${provPill(it.provenance, it.unconfirmed)}${it.detail ? `<div class="meta">${esc(it.detail)}</div>` : ""}<div class="meta">${esc(fmtDate(it.date, it.date_precision))}</div>${it.quote ? `<blockquote class="meta" style="margin:4px 0">“${esc(it.quote)}”</blockquote>` : ""}<div>${evChips(it.refs)}</div></li>`).join("")}</ul>` : `<div class="meta">${esc(sec.empty_message || "Nothing recorded.")}</div>`) + `</section>`).join("") +
        (r.limitations.length ? `<div class="notice"><strong>Limitations</strong><br>${r.limitations.map(esc).join("<br>")}</div>` : "") + `<div class="notice">${esc(r.notice)}</div><button class="btn btn-ghost btn-sm" onclick="window.print()">Print or save as PDF</button>`;
    } catch (e) { error(box, e, draw); }
  };
  await draw();
});

HG.register("ask", async (el) => {
  const gate = HG.needScope("DOCUMENTS"); if (gate) { el.innerHTML = `<h2>Ask my records</h2>${gate}`; return; }
  el.innerHTML = `<h2>Ask my health records</h2><p class="sub">Answers use only your uploaded and recorded information, and every statement cites its source. It will not diagnose, interpret results or advise on treatment.</p>
  <form class="form" id="askf"><textarea id="qn" maxlength="2000" required placeholder="e.g. When was my last HbA1c test and what did the report say?" aria-label="Your question"></textarea>
  <label>Search mode<select id="mode"><option value="hybrid">Hybrid (recommended)</option><option value="deterministic">Exact matching</option><option value="semantic">Meaning-based</option></select></label>
  <label>From date<input type="date" id="df"></label><label>To date<input type="date" id="dt"></label><button class="btn btn-primary" id="go">Ask</button></form><div id="out" aria-live="polite"></div>`;
  const out = el.querySelector("#out");
  el.querySelector("#askf").onsubmit = async (ev) => {
    ev.preventDefault(); const go = el.querySelector("#go"); go.disabled = true; loading(out, "Searching your records…");
    const body = { question: el.querySelector("#qn").value.trim(), retrieval_mode: el.querySelector("#mode").value };
    if (el.querySelector("#df").value) body.date_from = el.querySelector("#df").value; if (el.querySelector("#dt").value) body.date_to = el.querySelector("#dt").value;
    try {
      const r = await api(q("/ask"), { method: "POST", body });
      const by = Object.fromEntries((r.evidence || []).map(e => [e.evidence_id, e]));
      const cited = (r.citations || []).map(c => by[c]).filter(Boolean);
      const evRow = (e, n) => `<li class="row"><strong>[${n}]</strong> ${esc(e.value || e.source_text || e.source_type)} ${provPill(e.provenance)}<div class="meta">${esc(String(e.source_type).replace(/_/g, " ").toLowerCase())} · ${esc(fmtDate(e.date, e.date_precision))}${e.page ? ` · page ${esc(e.page)}` : ""}${e.confidence != null ? ` · extraction confidence ${esc(e.confidence)}` : ""}</div>${e.source_text ? `<blockquote class="meta" style="margin:4px 0">“${esc(String(e.source_text).slice(0, 300))}”</blockquote>` : ""}<div>${evChips([{ source_type: e.source_type, document_id: e.document_id, page: e.page }])}</div></li>`;
      out.innerHTML = `<section class="tile"><h3>Answer ${r.grounded ? '<span class="pill ok">grounded in your records</span>' : '<span class="pill warn">not enough evidence</span>'}</h3><p style="white-space:pre-wrap">${esc(r.answer)}</p></section>` +
        (r.uncertainty ? `<div class="notice"><strong>Uncertainty:</strong> ${esc(r.uncertainty)}</div>` : "") +
        ((r.conflicts || []).length ? `<div class="notice"><strong>Your records disagree.</strong> The retrieved evidence contains conflicting statements (${r.conflicts.map(g => g.map(id => esc(id)).join(" vs ")).join("; ")}); both are listed below — please confirm with your clinician.</div>` : "") +
        (cited.length ? `<section class="tile" style="margin-top:12px"><h3>Evidence cited</h3><ul>${cited.map((e, i) => evRow(e, i + 1)).join("")}</ul></section>` : "") +
        ((r.evidence || []).length > cited.length ? `<details style="margin-top:12px"><summary>All ${r.evidence.length} records retrieved for this question</summary><ul class="tile" style="list-style:none">${r.evidence.map((e, i) => evRow(e, i + 1)).join("")}</ul></details>` : "") +
        `<div class="notice">This is not medical advice. HealthGuard only reports what your records state.</div>`;
    } catch (e) { error(out, new Error(/503|unavailable|not configured/i.test(e.message) ? "The answer service is not available right now. Your records are unchanged; please try again later." : e.message)); }
    go.disabled = false;
  };
});

HG.register("questions", async (el) => {
  const r = await api(q("/doctor-questions"));
  const list = (items, fn, none) => items.length ? `<ul>${items.map(fn).join("")}</ul>` : `<div class="meta">${esc(none)}</div>`;
  el.innerHTML = `<h2>Questions to ask my doctor</h2><p class="sub">Documented facts, open uncertainties, and suggested questions — kept separate so nothing is mixed up.</p>` +
    (r.empty_message ? `<div class="state">${esc(r.empty_message)}</div>` : "") +
    `<div class="grid"><section class="tile"><h3>1 · What your records document</h3>${list(r.documented_facts, f => `<li class="row">${esc(f.text)} ${provPill(f.provenance, f.unconfirmed)}<div class="meta">${esc(fmtDate(f.date, f.date_precision))}</div><div>${evChips(f.refs)}</div></li>`, "Nothing documented yet.")}</section>
    <section class="tile"><h3>2 · Uncertain or conflicting</h3>${list(r.uncertainty, u => `<li class="row"><span class="pill warn">${esc(u.kind.replace(/_/g, " ").toLowerCase())}</span> ${esc(u.text)}${u.detail ? `<div class="meta">${esc(u.detail)}</div>` : ""}<div>${evChips(u.refs)}</div></li>`, "No conflicts or gaps detected.")}</section></div>
    <section class="tile" style="margin-top:16px"><h3>3 · Suggested questions <button class="btn btn-ghost btn-sm" id="copy">Copy list</button></h3>${list(r.suggested_questions, x => `<li class="row"><span class="pill">${esc(x.category.toLowerCase().replace("_", " "))}</span> ${esc(x.question)}<div>${evChips(x.basis)}</div></li>`, "No specific questions yet.")}</section>
    ${r.limitations.length ? `<div class="notice">${r.limitations.map(esc).join("<br>")}</div>` : ""}<div class="notice">${esc(r.notice)}</div><button class="btn btn-ghost btn-sm" onclick="window.print()">Print for my appointment</button>`;
  const copy = el.querySelector("#copy");
  if (copy) copy.onclick = async () => { try { await navigator.clipboard.writeText(r.suggested_questions.map((x, i) => `${i + 1}. ${x.question}`).join("\n")); flash(el, "ok", "Copied."); } catch { flash(el, "err", "Copy is not available in this browser."); } };
});

HG.register("medications", async (el) => {
  const gate = HG.needScope("MEDICATIONS"); if (gate) { el.innerHTML = `<h2>Medications</h2>${gate}`; return; }
  const t = await api(q("/medication-tracker"));
  const writable = can("MEDICATIONS", true);
  const card = (m) => `<section class="tile" style="margin-bottom:12px"><h3>${esc(m.name)} <span>${provPill(m.provenance, m.confirmation === "UNCONFIRMED_INFERENCE")}<span class="pill ${m.status === "ACTIVE" ? "ok" : ""}">${esc(m.status.toLowerCase())}</span></span></h3>
    <div class="meta">${esc([m.dose, m.frequency, m.route].filter(Boolean).join(" · ") || "No dose details recorded")} · started ${esc(fmtDate(m.start_date, m.start_date ? "EXACT" : "UNKNOWN"))}${m.end_date ? ` · ended ${esc(fmtDate(m.end_date, "EXACT"))}` : ""}</div>
    ${m.confirmation === "UNCONFIRMED_INFERENCE" ? `<div class="notice">This entry was inferred by the system and is <strong>not</strong> confirmed medication history.</div>` : ""}
    ${m.conflicts.map(c => `<div class="notice"><span class="pill warn">conflict</span> ${esc(c.title)}</div>`).join("")}
    <div class="meta" style="margin-top:8px">History of changes</div>${m.events.length ? `<div class="tl">${m.events.map(e => `<div class="it ${e.date ? "" : "undated"}"><strong>${esc(e.type.toLowerCase())}</strong>${e.dose ? ` (${esc(e.dose)})` : ""} <span class="meta">${esc(fmtDate(e.date, e.date_precision))}</span> ${provPill(e.provenance, e.unconfirmed)}${e.notes ? `<div class="meta">${esc(e.notes)}</div>` : ""}</div>`).join("")}</div>` : `<div class="meta">No starts, changes or stops recorded.</div>`}
    <div>${evChips(m.evidence)}</div>
    ${writable ? `<details style="margin-top:8px"><summary>Record a change</summary><form class="form" data-med="${esc(m.id)}"><label>What happened<select name="event_type"><option>STARTED</option><option>CHANGED</option><option>STOPPED</option></select></label><label>Date<input type="date" name="event_date"></label><label>Dose<input name="dose" maxlength="100"></label><label>Frequency<input name="frequency" maxlength="100"></label><button class="btn btn-primary btn-sm">Save</button></form></details>` : ""}</section>`;
  el.innerHTML = `<h2>Medications</h2><p class="sub">A longitudinal view of what is recorded. HealthGuard never recommends starting, changing or stopping a medicine.</p>
    ${t.conflict_count ? `<div class="notice"><span class="pill warn">${t.conflict_count}</span> disagreement(s) between your records are flagged below.</div>` : ""}
    ${writable ? `<details class="tile" style="margin-bottom:16px"><summary><strong>Add a medication</strong></summary><form class="form" id="addmed"><label>Medicine name<input name="medicine_name" required maxlength="200"></label><label>Dose<input name="dose" maxlength="100"></label><label>Frequency<input name="frequency" maxlength="100"></label><label>Status<select name="status"><option value="ACTIVE">Currently taking</option><option value="STOPPED">Stopped</option><option value="HISTORICAL">Past</option></select></label><label>Start date<input type="date" name="start_date"></label><label>End date<input type="date" name="end_date"></label><button class="btn btn-primary btn-sm">Add</button></form></details>` : ""}
    <h3>Current</h3>${t.current.length ? t.current.map(card).join("") : empty("No current medications recorded.")}
    <h3 style="margin-top:20px">History</h3>${t.history.length ? t.history.map(card).join("") : empty("No past medications recorded.")}<div class="notice">${esc(t.notice)}</div>`;
  const clean = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== ""));
  const add = el.querySelector("#addmed");
  if (add) add.onsubmit = async (e) => { e.preventDefault(); try { await api(q("/medications"), { method: "POST", body: clean(Object.fromEntries(new FormData(add))) }); HG.route(); } catch (err) { flash(el, "err", err.message); } };
  el.querySelectorAll("form[data-med]").forEach(f => f.onsubmit = async (e) => { e.preventDefault(); try { await api(`/api/medications/${encodeURIComponent(f.dataset.med)}/events`, { method: "POST", body: clean(Object.fromEntries(new FormData(f))) }); HG.route(); } catch (err) { flash(el, "err", err.message); } });
});
})();
