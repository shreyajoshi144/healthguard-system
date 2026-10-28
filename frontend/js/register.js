document.addEventListener("DOMContentLoaded", () => {
  HealthGuardAPI.me().then(() => { location.href = "/workspace"; }).catch(() => {});

  const form = document.getElementById("registerForm");
  const errorEl = document.getElementById("authError");
  const submitBtn = document.getElementById("submitBtn");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.classList.remove("show");
    submitBtn.disabled = true;
    submitBtn.textContent = "Creating account…";

    const username = document.getElementById("username").value.trim();
    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("password").value;

    try {
      await HealthGuardAPI.register(username, email, password);
      location.href = "/verify?email=" + encodeURIComponent(email);
    } catch (err) {
      errorEl.textContent = err.message || "Registration failed.";
      errorEl.classList.add("show");
      submitBtn.disabled = false;
      submitBtn.textContent = "Create Account";
    }
  });
});
