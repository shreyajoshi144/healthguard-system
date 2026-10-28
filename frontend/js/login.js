document.addEventListener("DOMContentLoaded", () => {
  // Already signed in? skip straight to workspace.
  HealthGuardAPI.me().then(() => { location.href = "/workspace"; }).catch(() => {});

  const form = document.getElementById("loginForm");
  const errorEl = document.getElementById("authError");
  const submitBtn = document.getElementById("submitBtn");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.classList.remove("show");
    submitBtn.disabled = true;
    submitBtn.textContent = "Signing in…";

    const username = document.getElementById("username").value.trim();
    const password = document.getElementById("password").value;

    try {
      const result = await HealthGuardAPI.login(username, password);
      hgSetSession(null, result.user);
      const params = new URLSearchParams(location.search);
      const next = params.get("next");
      location.href = next && next.startsWith("/") ? next : "/workspace";
    } catch (err) {
      errorEl.textContent = err.message || "Sign in failed.";
      errorEl.classList.add("show");
      submitBtn.disabled = false;
      submitBtn.textContent = "Sign In";
    }
  });
});
