document.addEventListener("DOMContentLoaded", async () => {
  const user = await hgRequireAuth();
  if (!user) return;
  hgInitWorkspaceShell(user);

  try {
    const data = await HealthGuardAPI.metrics();
    document.getElementById("emptyState").style.display = "none";
    document.getElementById("insightsContent").style.display = "block";
    render(data);
  } catch (err) {
    // no upload yet — empty state stays visible
  }

  function render(data) {
    renderAlerts(data.insights);
    renderFindings(data.insights);
    renderRecommendations(data.recommendations);
    renderSummaries(data.summaries);
    renderKeywords(data.top_keywords);
  }

  function renderAlerts(insights) {
    const el = document.getElementById("alertList");
    const alerts = (insights && insights.alerts) || [];
    document.getElementById("alertCount").textContent = `${alerts.length} active`;
    if (!alerts.length) {
      el.innerHTML = `<p class="muted">No alerts — nothing above the alert threshold right now.</p>`;
      return;
    }
    el.innerHTML = alerts
      .map(
        (a) => `
        <div class="rec-item" style="padding:14px 18px; display:flex; gap:12px; align-items:flex-start;">
          <span class="pill ${a.level === "critical" ? "pill-terracotta" : "pill-gold"}">${hgEscape(a.level || "notice")}</span>
          <span style="font-size:0.85rem; color:var(--ink-soft);">${hgEscape(a.message || "")}</span>
        </div>`
      )
      .join("");
  }

  function renderFindings(insights) {
    const el = document.getElementById("findingList");
    const findings = (insights && insights.key_findings) || [];
    if (!findings.length) {
      el.innerHTML = `<p class="muted">No findings generated yet.</p>`;
      return;
    }
    el.innerHTML = findings
      .map(
        (f) => `
        <details class="rec-item">
          <summary>${hgEscape(f.icon || "")} ${hgEscape(f.title || "")}</summary>
          <div class="rec-body"><p style="margin:0;">${hgEscape(f.description || "")}</p></div>
        </details>`
      )
      .join("");
  }

  function renderRecommendations(recs) {
    const el = document.getElementById("recList");
    if (!recs || Object.keys(recs).length === 0) {
      el.innerHTML = `<p class="muted">No recommendations yet.</p>`;
      return;
    }
    el.innerHTML = Object.entries(recs)
      .map(
        ([dept, items]) => `
        <details class="rec-item">
          <summary>${hgEscape(dept)}</summary>
          <div class="rec-body"><ul>${items.map((i) => `<li>${hgEscape(i)}</li>`).join("")}</ul></div>
        </details>`
      )
      .join("");
  }

  function renderSummaries(summaries) {
    const el = document.getElementById("summaryList");
    if (!summaries || Object.keys(summaries).length === 0) {
      el.innerHTML = `<p class="muted">No summaries yet.</p>`;
      return;
    }
    el.innerHTML = Object.entries(summaries)
      .map(
        ([dept, text]) => `
        <details class="rec-item">
          <summary>${hgEscape(dept)}</summary>
          <div class="rec-body"><p style="margin:0;">${hgEscape(text)}</p></div>
        </details>`
      )
      .join("");
  }

  function renderKeywords(keywords) {
    const el = document.getElementById("keywordCloud");
    if (!keywords || keywords.length === 0) {
      el.innerHTML = `<p class="muted">No keyword data yet.</p>`;
      return;
    }
    el.innerHTML = keywords.map((k) => `<span class="keyword-chip">${hgEscape(k.word)} <b>${k.count}</b></span>`).join("");
  }
});
