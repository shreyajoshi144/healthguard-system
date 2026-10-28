document.addEventListener("DOMContentLoaded", async () => {
  const user = await hgRequireAuth();
  if (!user) return;
  hgInitWorkspaceShell(user);

  try {
    const health = await HealthGuardAPI.health();
    document.getElementById("statusIssue").textContent = health.issue_classifier_available ? "ready" : "not trained";
    document.getElementById("statusSat").textContent = health.satisfaction_model_available ? "ready" : "not trained";
    document.getElementById("statusThreshold").textContent = health.issue_classifier_confidence_threshold ?? "—";
    document.getElementById("dotIssue").classList.toggle("off", !health.issue_classifier_available);
    document.getElementById("dotSat").classList.toggle("off", !health.satisfaction_model_available);
  } catch (err) {
    /* health check is best-effort */
  }

  let modelMetrics = null;
  try {
    const data = await HealthGuardAPI.metrics();
    modelMetrics = data.model_metrics;
  } catch (err) {
    // No upload yet — model metrics.json is still readable independently of
    // any uploaded dataset, since it comes from the training run, not the
    // current session's data. Retry via a dataset-less call isn't available
    // through /api/metrics (it requires an upload), so show a clear note.
  }

  if (!modelMetrics) {
    document.getElementById("modelMetricsGrid").innerHTML = `
      <div class="card panel" style="grid-column: 1 / -1;">
        <p class="muted" style="margin:0;">Upload a dataset from the Overview page to load model performance details (the metrics themselves come from the last training run, not the dataset — but this page currently reuses the same endpoint as Overview).</p>
      </div>`;
    return;
  }

  renderModelCards(modelMetrics);
  renderConfusionMatrices(modelMetrics);

  function renderModelCards(mm) {
    const grid = document.getElementById("modelMetricsGrid");
    const cards = [];

    const im = mm.issue_classifier;
    if (im) {
      const t = im.test || {};
      cards.push(`
        <div class="card model-metric-card panel">
          <div class="model-name">Issue Classifier</div>
          <h4>TF-IDF + Logistic Regression</h4>
          <div class="metric-row"><span>Test accuracy</span><b>${hgFmtPct((t.accuracy || 0) * 100)}</b></div>
          <div class="metric-row"><span>Macro precision</span><b>${hgFmtNum(t.macro_precision, 3)}</b></div>
          <div class="metric-row"><span>Macro recall</span><b>${hgFmtNum(t.macro_recall, 3)}</b></div>
          <div class="metric-row"><span>Macro F1</span><b>${hgFmtNum(t.macro_f1, 3)}</b></div>
          <div class="metric-row"><span>Train / Val / Test</span><b>${im.n_train} / ${im.n_val} / ${im.n_test}</b></div>
          <div class="metric-row"><span>Classes</span><b>${(im.classes || []).length}</b></div>
          <div class="model-note">${hgEscape(im.dataset_notice || "")}</div>
        </div>`);
    }

    const sm = mm.satisfaction_model;
    if (sm) {
      const t = sm.test || {};
      cards.push(`
        <div class="card model-metric-card panel">
          <div class="model-name">Satisfaction Model — Experimental</div>
          <h4>RandomForest · rating_band</h4>
          <div class="metric-row"><span>Test accuracy</span><b>${hgFmtPct((t.accuracy || 0) * 100)}</b></div>
          <div class="metric-row"><span>Macro precision</span><b>${hgFmtNum(t.macro_precision, 3)}</b></div>
          <div class="metric-row"><span>Macro recall</span><b>${hgFmtNum(t.macro_recall, 3)}</b></div>
          <div class="metric-row"><span>Macro F1</span><b>${hgFmtNum(t.macro_f1, 3)}</b></div>
          <div class="metric-row"><span>Train / Val / Test</span><b>${sm.n_train} / ${sm.n_val} / ${sm.n_test}</b></div>
          <div class="metric-row"><span>Features</span><b style="font-weight:500; font-size:0.76rem;">${(sm.features || []).join(", ")}</b></div>
          <div class="model-note experimental">${hgEscape(sm.status_note || "")}</div>
        </div>`);
    }

    grid.innerHTML = cards.join("");
  }

  function renderConfusionMatrices(mm) {
    const section = document.getElementById("confusionSection");
    let html = "";

    [
      { key: "issue_classifier", title: "Issue Classifier — Confusion Matrix (test set)" },
      { key: "satisfaction_model", title: "Satisfaction Model — Confusion Matrix (test set)" },
    ].forEach(({ key, title }) => {
      const m = mm[key];
      const cm = m && m.test && m.test.confusion_matrix;
      const labels = m && m.test && m.test.confusion_matrix_labels;
      if (!cm || !labels) return;

      let table = `<table class="cm-table"><thead><tr><th>Actual \\ Predicted</th>${labels
        .map((l) => `<th>${hgEscape(l)}</th>`)
        .join("")}</tr></thead><tbody>`;
      cm.forEach((row, i) => {
        table += `<tr><th>${hgEscape(labels[i])}</th>${row
          .map((v, j) => `<td class="${i === j ? "diag" : ""}">${v}</td>`)
          .join("")}</tr>`;
      });
      table += "</tbody></table>";

      html += `
        <div class="card panel reveal" style="margin-top:20px;">
          <div class="panel-head"><h3>${hgEscape(title)}</h3></div>
          <div class="cm-wrap">${table}</div>
        </div>`;
    });

    section.innerHTML = html;
  }
});
