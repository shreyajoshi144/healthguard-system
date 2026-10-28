document.addEventListener("DOMContentLoaded", async () => {
  const user = await hgRequireAuth();
  if (!user) return;
  hgInitWorkspaceShell(user);

  const fileInput = document.getElementById("fileInput");
  const filenameEl = document.getElementById("uploadFilename");
  const statusBanner = document.getElementById("statusBanner");
  const loadSampleBtn = document.getElementById("loadSampleBtn");

  refreshHealth();

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    filenameEl.textContent = file.name;
    await handleUpload(file);
  });

  loadSampleBtn.addEventListener("click", async () => {
    setStatus("Fetching sample dataset…", "info");
    try {
      const res = await fetch("/sample-data/sample_patient_survey.csv");
      if (!res.ok) throw new Error("Could not load the bundled sample dataset.");
      const blob = await res.blob();
      const file = new File([blob], "sample_patient_survey.csv", { type: "text/csv" });
      filenameEl.textContent = file.name;
      await handleUpload(file);
    } catch (err) {
      setStatus(err.message, "error");
    }
  });

  async function handleUpload(file) {
    setStatus("Uploading and running the pipeline (clean → ML classify → sentiment → priority)…", "info");
    try {
      const uploadResult = await HealthGuardAPI.upload(file);
      const warnings = uploadResult.validation_warnings || [];
      setStatus(
        `Processed ${uploadResult.n_rows} responses across ${uploadResult.n_columns} columns.` +
          (warnings.length ? ` ${warnings.length} validation note(s) — see console.` : ""),
        "success"
      );
      if (warnings.length) console.info("Validation notes:", warnings);
      await loadMetrics(uploadResult.upload_id);
    } catch (err) {
      setStatus(err.message || "Upload failed.", "error");
    }
  }

  function setStatus(msg, kind) {
    statusBanner.textContent = msg;
    statusBanner.className = `status-banner show ${kind}`;
  }

  async function refreshHealth() {
    try {
      const health = await HealthGuardAPI.health();
      document.getElementById("statusIssue").textContent = health.issue_classifier_available ? "ready" : "not trained";
      document.getElementById("statusSat").textContent = health.satisfaction_model_available ? "ready" : "not trained";
      document.getElementById("statusThreshold").textContent = health.issue_classifier_confidence_threshold ?? "—";
      document.getElementById("dotIssue").classList.toggle("off", !health.issue_classifier_available);
      document.getElementById("dotSat").classList.toggle("off", !health.satisfaction_model_available);
    } catch (err) {
      document.getElementById("statusIssue").textContent = "unreachable";
      document.getElementById("statusSat").textContent = "unreachable";
    }
  }

  async function loadMetrics(uploadId) {
    try {
      const data = await HealthGuardAPI.metrics(uploadId);
      document.getElementById("emptyState").style.display = "none";
      document.getElementById("overviewContent").style.display = "block";
      renderOverview(data);
    } catch (err) {
      setStatus(err.message || "Could not load metrics.", "error");
    }
  }

  // Populate immediately if data already exists in this session (e.g. the
  // user uploaded from another workspace page already).
  HealthGuardAPI.metrics()
    .then((data) => {
      document.getElementById("emptyState").style.display = "none";
      document.getElementById("overviewContent").style.display = "block";
      renderOverview(data);
    })
    .catch(() => { /* no upload yet — keep empty state visible */ });

  function renderOverview(data) {
    renderKPIs(data.kpis);
    hgRenderBars("issueCatBars", data.issue_category_distribution, "");
    hgRenderDonut("sentimentDonut", data.sentiment_distribution, { Positive: "#6f8f74", Neutral: "#d9b876", Negative: "#b8593f" });
    hgRenderBars("priorityBars", data.priority_distribution, "terracotta");
    hgRenderBars("sourceBars", data.issue_category_source_counts, "gold");

    const deptTotals = {};
    (data.department_breakdown || []).forEach((d) => { deptTotals[d.department] = d.total; });
    const top5 = Object.fromEntries(Object.entries(deptTotals).sort((a, b) => b[1] - a[1]).slice(0, 5));
    hgRenderBars("topDeptBars", top5, "");

    document.getElementById("issueCatCount").textContent = `${Object.keys(data.issue_category_distribution || {}).length} categories`;
  }

  function renderKPIs(kpis) {
    if (!kpis) return;
    const cards = [
      { label: "Total Responses", value: kpis.total_responses ?? "—", accent: "" },
      { label: "Avg Rating", value: kpis.avg_rating !== null ? `${kpis.avg_rating.toFixed(2)} / 5` : "—", accent: "accent-gold" },
      { label: "Positive %", value: kpis.positive_pct !== null ? hgFmtPct(kpis.positive_pct) : "—", accent: "accent-sage" },
      { label: "Departments", value: kpis.departments ?? "—", accent: "" },
      { label: "Critical Issues", value: kpis.critical_issues ?? "—", accent: "accent-terracotta" },
    ];
    document.getElementById("kpiGrid").innerHTML = cards
      .map(
        (c) => `
        <div class="card kpi-card ${c.accent}">
          <div class="kpi-label">${hgEscape(c.label)}</div>
          <div class="kpi-value">${hgEscape(String(c.value))}</div>
        </div>`
      )
      .join("");
  }
});
