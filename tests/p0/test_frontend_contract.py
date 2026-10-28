"""Static frontend checks: every local asset referenced by an HTML page exists, every JS file parses, and every
/api path used by the P0 pages (hub + admin) matches a real backend route (catches frontend/backend mismatches)."""
import re, subprocess
from pathlib import Path
import pytest
from backend.main import app

FRONT = Path(__file__).resolve().parents[2] / "frontend"
P0_JS = ["hub-core.js", "hub-views.js", "hub-views2.js", "admin.js"]

def _route_regexes():
    out = []
    for r in app.routes:
        path = getattr(r, "path", None)
        if path and path.startswith("/api"):
            out.append(re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/?]+", path) + "$"))
    return out

def test_every_js_file_parses():
    for f in sorted((FRONT / "js").glob("*.js")):
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr[:300]}"

def test_html_references_resolve():
    for html in FRONT.glob("*.html"):
        for ref in re.findall(r'(?:src|href)="(/(?:js|css|assets)/[^"?#]+)', html.read_text()):
            assert (FRONT / ref.lstrip("/")).exists(), f"{html.name} references missing {ref}"

@pytest.mark.parametrize("name", P0_JS)
def test_api_paths_used_by_p0_pages_exist(name):
    src = (FRONT / "js" / name).read_text(); routes = _route_regexes(); bad = []
    helpers = ""                                                      # hub pages build patient paths via q("/x") -> /api/patients/{id}/x
    for m in re.finditer(r'''q\(\s*[`"'](/[^`"']*)''', src):
        path = "/api/patients/PID" + re.sub(r"\$\{[^}]*\}", "X", m.group(1))
        if not any(r.match(path.split("?")[0]) for r in routes): bad.append(path)
    for m in re.finditer(r'''[`"'](/api/[^`"']*)''', src):
        path = re.sub(r"\$\{[^}]*\}", "X", m.group(1)).split("?")[0]
        if path.endswith("/"): continue                               # prefix concatenations (e.g. "/api/admin/jobs/" + id) are covered by the template forms
        if path.endswith("/X/X"):                                     # action verb chosen at runtime from data-act / data-op / data-al attributes: check every verb the page can emit
            attr = {"jobs": "op", "alerts": "al", "health-actions": "act"}[path.split("/")[-3]]
            verbs = set(re.findall(rf'data-{attr}="([a-z]+)"', src)) - {""}
            assert verbs, f"{name}: dynamic action path with no data-{attr} verbs"
            candidates = [path[:-1] + v for v in verbs]
        else:
            candidates = [path]
        for cand in candidates:
            if not any(r.match(cand) for r in routes): bad.append(cand)
    assert not bad, f"{name}: no matching backend route for {sorted(set(bad))}"
