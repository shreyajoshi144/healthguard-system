document.addEventListener("DOMContentLoaded", async () => {
  const user = await hgRequireAuth();
  if (!user) return;
  hgInitWorkspaceShell(user);

  try {
    const data = await HealthGuardAPI.metrics();
    document.getElementById("emptyState").style.display = "none";
    document.getElementById("deptContent").style.display = "block";
    render(data.department_breakdown || []);
  } catch (err) {
    // no upload yet — empty state stays visible
  }

  function render(rows) {
    const avgRating = {};
    const critical = {};
    rows.forEach((r) => {
      if (r.avg_rating !== null && r.avg_rating !== undefined) avgRating[r.department] = r.avg_rating;
      critical[r.department] = r.critical_issues || 0;
    });
    hgRenderBars("avgRatingBars", avgRating, "gold");
    hgRenderBars("criticalBars", critical, "terracotta");
    renderTable(rows);
  }

  function renderTable(rows) {
    const tbody = document.querySelector("#deptTable tbody");
    if (!rows || rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">No department data yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows
      .map(
        (r) => `
        <tr>
          <td class="name">${hgEscape(r.department)}</td>
          <td class="num">${r.total}</td>
          <td class="num">${r.avg_rating !== null ? r.avg_rating.toFixed(2) : "—"}</td>
          <td class="num">${r.positive_pct !== null ? hgFmtPct(r.positive_pct) : "—"}</td>
          <td class="num">${r.negative_pct !== null ? hgFmtPct(r.negative_pct) : "—"}</td>
          <td>${hgEscape(r.top_issue || "—")}</td>
          <td class="num">${r.critical_issues}</td>
        </tr>`
      )
      .join("");
  }
});
