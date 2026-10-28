/* ============================================================
   Analyze page logic
   ============================================================ */

const SAMPLES = {
  wait: "I waited almost three hours in the emergency room before anyone even looked at me. The waiting room was packed and no one gave us an update.",
  positive: "The nurse was incredibly kind and explained every step of my treatment. Overall a great experience, the whole team was wonderful.",
  billing: "My final bill had charges I was never told about and the estimate I received before treatment was completely wrong. Very frustrating experience.",
};

document.addEventListener("DOMContentLoaded", async () => {
  const user = await hgRequireAuth();
  if (!user) return;
  hgInitWorkspaceShell(user);

  const textEl = document.getElementById("feedbackText");
  const charCount = document.getElementById("charCount");
  const deptEl = document.getElementById("department");
  const ratingPicker = document.getElementById("ratingPicker");
  const form = document.getElementById("analyzeForm");
  const submitBtn = document.getElementById("submitBtn");
  const submitSpinner = document.getElementById("submitSpinner");
  const submitLabel = document.getElementById("submitLabel");
  const formError = document.getElementById("formError");

  let selectedRating = null;

  textEl.addEventListener("input", () => {
    charCount.textContent = textEl.value.length;
  });

  document.querySelectorAll(".sample-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      textEl.value = SAMPLES[chip.dataset.sample] || "";
      charCount.textContent = textEl.value.length;
      textEl.focus();
    });
  });

  ratingPicker.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const isSame = btn.classList.contains("selected");
      ratingPicker.querySelectorAll("button").forEach((b) => b.classList.remove("selected"));
      if (!isSame) {
        btn.classList.add("selected");
        selectedRating = Number(btn.dataset.rating);
      } else {
        selectedRating = null;
      }
    });
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    formError.classList.remove("show", "error");

    const feedback_text = textEl.value.trim();
    if (!feedback_text) return;

    setLoading(true);
    try {
      const payload = {
        feedback_text,
        department: deptEl.value,
      };
      if (selectedRating) payload.overall_rating = selectedRating;

      const result = await HealthGuardAPI.analyze(payload);
      renderResult(result);
    } catch (err) {
      formError.textContent = err.message || "Something went wrong analyzing this feedback.";
      formError.classList.add("show", "error");
    } finally {
      setLoading(false);
    }
  });

  function setLoading(isLoading) {
    submitBtn.disabled = isLoading;
    submitSpinner.classList.toggle("show", isLoading);
    submitLabel.textContent = isLoading ? "Analyzing…" : "Analyze Feedback";
  }

  function renderResult(result) {
    document.getElementById("resultPlaceholder").style.display = "none";
    const panel = document.getElementById("resultPanel");
    panel.classList.add("show");

    const sourcePill = document.getElementById("sourcePill");
    const isML = result.issue_category_source === "ml";
    sourcePill.textContent = isML ? "ML classified" : "Rule-based fallback";
    sourcePill.className = "pill " + (isML ? "pill-forest" : "pill-gold");

    document.getElementById("categoryTag").textContent = result.issue_category || "Uncategorized";
    document.getElementById("sourceNote").innerHTML = isML
      ? `Classified by the trained <b>TF-IDF + Logistic Regression</b> model.`
      : `ML confidence was below threshold — classified by the <b>rule-based keyword</b> fallback.`;

    const conf = result.issue_category_confidence;
    const confPct = conf !== null && conf !== undefined ? Math.round(conf * 100) : null;
    const circle = document.getElementById("confCircle");
    const circumference = 251.2;
    const offset = confPct !== null ? circumference - (confPct / 100) * circumference : circumference;
    circle.style.strokeDashoffset = offset;
    document.getElementById("confValue").textContent = confPct !== null ? `${confPct}%` : "—";

    const sentiment = result.sentiment || {};
    document.getElementById("sentimentValue").textContent = sentiment.label || "—";
    document.getElementById("sentimentSub").textContent =
      sentiment.compound !== null && sentiment.compound !== undefined
        ? `compound score ${hgFmtNum(sentiment.compound, 3)}`
        : "—";

    document.getElementById("priorityValue").textContent =
      result.priority_label ? result.priority_label.replace(/^[^\s]+\s/, "") : "—";
    document.getElementById("prioritySub").textContent =
      result.priority_score !== null && result.priority_score !== undefined
        ? `score ${hgFmtNum(result.priority_score, 1)} / 10`
        : "no rating supplied";

    document.getElementById("actionText").textContent =
      result.recommended_action || "No action mapped for this category.";

    const satPanel = document.getElementById("satPanel");
    const satValue = document.getElementById("satValue");
    if (result.satisfaction_prediction) {
      satPanel.style.display = "block";
      const band = result.satisfaction_prediction.predicted_rating_band;
      document.getElementById("satTier").textContent = band;
      document.getElementById("satNote").textContent = result.satisfaction_prediction.note;
      satValue.textContent = band;
    } else {
      satPanel.style.display = "none";
      satValue.textContent = "unavailable";
    }
  }
});
