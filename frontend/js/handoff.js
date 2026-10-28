document.addEventListener("DOMContentLoaded", async () => {
  const user = await hgRequireAuth(); if (!user) return;
  const patientId = new URLSearchParams(location.search).get("patient_id");
  const box = document.getElementById("handoffs"), status = document.getElementById("status");
  if (!patientId) { status.textContent="A patient is required."; status.className="status-banner show error"; return; }
  document.getElementById("back").href=`/patient?id=${encodeURIComponent(patientId)}`;
  const err=e=>{status.textContent=e.message||"Something went wrong.";status.className="status-banner show error";};
  async function load(){try{const rows=await HealthGuardAPI.request(`/api/patients/${patientId}/handoffs`);box.innerHTML=rows.length?rows.map(h=>`<article class="card" style="padding:14px"><div class="inline" style="justify-content:space-between"><strong>${hgEscape(h.title)}</strong><span class="muted">${hgEscape(h.status)}</span></div><textarea data-id="${h.id}" style="min-height:180px">${hgEscape(h.content)}</textarea><div class="inline"><button class="btn btn-ghost btn-sm" data-save="${h.id}">Save draft</button>${h.status!=="FINAL"?`<button class="btn btn-primary btn-sm" data-final="${h.id}">Finalize</button>`:""}</div></article>`).join(""):'<p class="muted">No handoff drafts yet.</p>';}catch(e){err(e);}}
  document.getElementById("generate").onclick=async()=>{try{await HealthGuardAPI.request(`/api/patients/${patientId}/handoffs`,{method:"POST",body:{title:"Doctor handoff"}});await load();}catch(e){err(e);}};
  box.addEventListener("click",async e=>{const save=e.target.closest("[data-save]"),fin=e.target.closest("[data-final]");try{if(save){const ta=box.querySelector(`textarea[data-id="${save.dataset.save}"]`);await HealthGuardAPI.request(`/api/handoffs/${save.dataset.save}`,{method:"PATCH",body:{content:ta.value}});await load();}if(fin){await HealthGuardAPI.request(`/api/handoffs/${fin.dataset.final}/finalize`,{method:"POST"});await load();}}catch(x){err(x);}});
  load();
});
