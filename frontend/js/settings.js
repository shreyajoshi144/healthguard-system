document.addEventListener("DOMContentLoaded", async () => {
  const user = await hgRequireAuth(); if (!user) return;
  username.textContent=user.username; email.textContent=user.email; verified.textContent=user.email_verified?"Verified":"Not verified";
  logoutBtn.onclick=()=>hgLogout();
  deactivate.onclick=async()=>{if(!confirm("Deactivate this account? You will be signed out."))return;try{await HealthGuardAPI.request("/api/account/deactivate",{method:"POST"});hgClearSession();location.href="/login";}catch(e){status.textContent=e.message;status.className="status-banner show error";}};
  deleteAccount.onclick=async()=>{if(!confirm("Permanently delete this account and its patient records? This cannot be undone after storage deletion completes."))return;try{await HealthGuardAPI.request("/api/account",{method:"DELETE"});hgClearSession();location.href="/";}catch(e){status.textContent=e.message;status.className="status-banner show error";}};

  // ---- Privacy & data: explicit consent + patient data export -------------------------------------------------------------
  const h = (tag, attrs = {}, text) => { const n = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); if (text != null) n.textContent = text; return n; };
  (async () => {
    const box = document.getElementById("consentBox"), say = document.getElementById("consentStatus");
    try {
      const c = (await HealthGuardAPI.request("/api/privacy/consents")).find(r => r.purpose === "AI_PROCESSING");
      box.replaceChildren();
      const label = h("label", { class: "consent-row", for: "aiConsent" }), input = h("input", { type: "checkbox", id: "aiConsent", "aria-describedby": "aiConsentDesc aiConsentNote" });
      label.append(input, h("strong", {}, c.title)); input.checked = !!c.granted;
      box.append(label, h("p", { id: "aiConsentDesc", class: "muted" }, c.description),
        h("p", { id: "aiConsentNote", class: "muted" }, `Policy version ${c.policy_version}.` + (c.needs_reconsent ? " The wording has changed: please review it and confirm again." : "") +
          (c.required_now ? "" : " AI processing currently runs inside this service; your choice is saved and applies if an external AI provider is enabled.")));
      input.onchange = async () => {
        const wanted = input.checked; input.disabled = true;
        try {
          const r = await HealthGuardAPI.request("/api/privacy/consents/AI_PROCESSING", { method: "PUT", body: { granted: wanted } });
          say.textContent = wanted ? "AI processing is allowed." + (r.requeued_documents ? ` ${r.requeued_documents} waiting document(s) are now being processed.` : "") : "AI processing is turned off.";
        } catch (e) { input.checked = !wanted; say.textContent = "Your choice could not be saved: " + e.message; }
        input.disabled = false; input.focus();
      };
    } catch (e) { box.replaceChildren(h("p", { class: "muted", role: "alert" }, "Privacy choices could not be loaded: " + e.message)); }

    const list = document.getElementById("exportList"), note = document.getElementById("exportStatus");
    async function download(patient, format) {
      const who = `${patient.first_name} ${patient.last_name}`; note.textContent = `Preparing ${format.toUpperCase()} export for ${who}…`;
      try {
        const res = await fetch(`/api/patients/${patient.id}/export?format=${format}`, { credentials: "same-origin" });
        if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(res.status === 429 ? "Too many exports. Please wait a while and try again." : (typeof d.detail === "string" ? d.detail : "The export failed.")); }
        const url = URL.createObjectURL(await res.blob()), a = h("a", { href: url, download: `healthguard-${patient.first_name}-${patient.last_name}.${format}`.toLowerCase().replace(/[^a-z0-9.-]/g, "-") });
        document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
        note.textContent = `Export for ${who} downloaded.`;
      } catch (e) { note.textContent = `Export for ${who} failed: ${e.message}`; }
    }
    try {
      const mine = (await HealthGuardAPI.request("/api/patients")).filter(p => p.owner_user_id === user.id);
      if (!mine.length) list.append(h("li", { class: "muted" }, "You do not own any patient records yet."));
      for (const p of mine) {
        const li = h("li", { class: "export-row" }), name = `${p.first_name} ${p.last_name}`;
        li.append(h("span", {}, name));
        for (const f of ["json", "zip"]) { const b = h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": `Download ${name} as ${f.toUpperCase()}` }, f === "json" ? "Download JSON" : "Download ZIP with files"); b.onclick = () => download(p, f); li.append(b); }
        list.append(li);
      }
    } catch (e) { list.append(h("li", { class: "muted", role: "alert" }, "Your records could not be loaded: " + e.message)); }
  })();
});
