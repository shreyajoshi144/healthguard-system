document.addEventListener("DOMContentLoaded", async () => {
  const user = await hgRequireAuth();
  if (!user) return;
  const box = document.getElementById("patients");
  const formCard = document.getElementById("formCard");
  const statusEl = document.getElementById("status");
  const form = document.getElementById("patientForm");
  const showError = (e) => { statusEl.textContent = e.message || "Something went wrong."; statusEl.className = "status-banner show error"; };
  async function load() {
    try {
      const patients = await HealthGuardAPI.request("/api/patients?page=1&page_size=100");
      box.innerHTML = patients.length ? patients.map(p => `<div class="hg-item"><div class="inline" style="justify-content:space-between"><div><strong>${hgEscape(p.first_name)} ${hgEscape(p.last_name)}</strong><div class="muted">${p.date_of_birth || "DOB not provided"}${p.blood_group ? " · " + hgEscape(p.blood_group) : ""}</div></div><div class="inline"><a class="btn btn-primary btn-sm" href="/patient?id=${encodeURIComponent(p.id)}">Open record</a><a class="btn btn-ghost btn-sm" href="/hub?patient_id=${encodeURIComponent(p.id)}">Health overview</a></div></div></div>`).join("") : '<p class="muted">No patient profiles yet.</p>';
    } catch (e) { showError(e); }
  }
  document.getElementById("addPatient").onclick = () => { formCard.style.display = "block"; };
  document.getElementById("cancelPatient").onclick = () => { formCard.style.display = "none"; form.reset(); };
  form.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await HealthGuardAPI.request("/api/patients", {method:"POST", body:{first_name:first.value.trim(),last_name:last.value.trim(),date_of_birth:dob.value||null,gender:gender.value.trim()||null,blood_group:blood.value.trim()||null}});
      form.reset(); formCard.style.display="none"; statusEl.className="status-banner"; await load();
    } catch (err) { showError(err); }
  };
  load();
});
