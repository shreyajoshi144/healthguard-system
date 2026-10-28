# HealthGuard — Phase 2 Report (Corrected): Auth + Workspace Structure

This corrects the Phase 2 product structure: public marketing/auth pages,
plus a JWT-authenticated workspace with a persistent sidebar and four
distinct operational pages, replacing the earlier flat
`index/dashboard/analyze` layout. All Phase 1 ML logic and the working
FastAPI pipeline endpoints are unchanged — this pass only adds auth and
reorganizes routes/pages around it.

## 1. Final route map

**Public**
| Route | Page |
|---|---|
| `/` | Landing — product storytelling |
| `/login` | Sign in |
| `/register` | Create account |

**Authenticated workspace** (persistent sidebar shell)
| Route | Page |
|---|---|
| `/workspace` | **Overview** — default page, real KPI/chart dashboard |
| `/analyze` | Single-feedback ML analysis (the main interactive feature) |
| `/insights` | Alerts, key findings, department recommendations, summaries, keywords |
| `/departments` | Department comparison charts + full performance table |
| `/models` | Model performance cards + confusion matrices, read from `metrics.json` |

**API** — unchanged endpoints, now split public/protected:
| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/auth/register` | public | Create account, returns JWT |
| `POST /api/auth/login` | public | Authenticate, returns JWT |
| `GET /api/auth/me` | **protected** | Confirms token validity, returns user |
| `GET /api/health` | public | Service/model status |
| `POST /api/analyze` | **protected** | Single-feedback ML inference |
| `POST /api/upload` | **protected** | CSV upload → full pipeline |
| `GET /api/metrics` | **protected** | Dashboard payload + model metrics |
| `GET /api/uploads` | **protected** | Session upload list |

Old `/dashboard.html` and `/analyze.html` URLs 307-redirect to `/workspace`
and `/analyze` respectively, so nothing that already linked to them breaks.

## 2. Authentication — lightweight, as scoped

`backend/auth.py`, entirely new, ~150 lines, zero ML/business logic:

- **Passwords**: PBKDF2-HMAC-SHA256 (stdlib `hashlib`, 200,000 iterations)
  + a random 16-byte salt per user. No bcrypt dependency needed.
- **Tokens**: stateless JWT (HS256, PyJWT), 8-hour expiry. Signing secret
  is read from `HEALTHGUARD_JWT_SECRET` if set, otherwise generated once
  at process startup.
- **User store**: in-memory dict, guarded by a lock — the same pattern
  already used for the upload store in `backend/pipeline.py`. **No
  database, no OAuth**, per your instruction. This means the user list
  (and thus all sessions) resets on process restart — an explicit,
  documented trade-off for "lightweight," not an oversight.
- **Logout is client-side only**: the frontend deletes the token from
  `localStorage`. There's no server session to invalidate with a
  stateless JWT — this is the standard, correct behavior for this
  approach, not a missing feature.
- Protected routes use a single FastAPI dependency,
  `Depends(auth.get_current_user)`, added to `/api/analyze`, `/api/upload`,
  `/api/metrics`, `/api/uploads`, and `/api/auth/me`.

**How page-level protection works**: the workspace HTML files are static
and technically reachable without a token (FastAPI has no server-side
session to gate a static file on). Real protection is at the data layer —
every API call the workspace pages make requires a valid JWT, verified
above. On top of that, `frontend/js/api.js`'s `hgRequireAuth()` runs first
on every workspace page, calls `/api/auth/me`, and immediately redirects
to `/login` if there's no valid token — so in practice no workspace page
ever renders real content without a valid session, and no data is
reachable either way without one.

## 3. Workspace structure

`frontend/css/workspace.css` defines a shared shell: a fixed 250px
sidebar (Overview / Analyze / Insights / Departments / Models, plus a
user badge and logout button) and a topbar+content column. Each of the
five workspace pages includes the same sidebar markup (no templating
engine available in vanilla HTML, so it's duplicated by design) and loads
`hgInitWorkspaceShell(user)` from `api.js` to highlight the active link,
show the signed-in username, and wire the logout button — so the sidebar
behaves identically everywhere without duplicating that *logic*, only the
markup.

**Overview (`/workspace`) is now a real dashboard**, not the CSV upload
screen it effectively was before: KPI grid, issue-category bar chart,
sentiment donut, priority bars, ML-vs-rule classification-source bars, a
top-5-departments chart, and quick-link cards into Insights/Models. The
CSV upload control still lives here (top-right of the topbar) because
Overview is the natural place to load data in a single-tenant demo — but
it's one action among many on a working dashboard, not the whole page.

**Insights, Departments, and Models** are now separate pages instead of
one long scroll, each pulling only the slice of the same `/api/metrics`
payload it needs:
- Insights: alerts, key findings, recommendations by department, feedback
  summaries, keyword cloud.
- Departments: avg-rating and critical-issue bar charts + the full
  department performance table.
- Models: the two model-metric cards (now including precision/recall, not
  just accuracy/F1) plus a rendered confusion matrix table per model,
  built from the same `confusion_matrix`/`confusion_matrix_labels` arrays
  already present in `metrics.json` — this is new *rendering*, not new
  computation; the numbers were already being persisted by Phase 1's
  `training/train_models.py`.

**Analyze** is unchanged in substance (same form, same result panel,
same `/api/analyze` call) — it moved inside the sidebar shell and now
requires auth via the same `hgRequireAuth()` guard as every other
workspace page.

No chart library, no template engine, no React — same hand-rolled
SVG/CSS bar and donut charts as before, now shared via `hgRenderBars()`/
`hgRenderDonut()` in `api.js` instead of being duplicated per page.

## 4. What was NOT touched

- `modules/*.py`, `backend/pipeline.py`, `backend/metrics.py` — **zero
  changes**. Every workspace page still reads from the exact same
  Phase 1 pipeline sequence and the exact same persisted ML models.
- `training/*.py`, `models/*.joblib`, `models/*/metrics.json` — untouched.
- `app.py` (Streamlit) — untouched, still runs independently and
  concurrently with the FastAPI process (verified again, §5).
- No new ML models were added. No database. No OAuth provider. No
  Docker/deployment changes. No React/Node — everything is still plain
  HTML/CSS/JS served as static files by FastAPI.

## 5. Testing performed

**Static verification**
- All 8 HTML files parse cleanly (`html.parser`, no unclosed tags)
- All 8 JS files pass `node --check`
- Every `getElementById` in every page-specific JS file cross-checked
  against its HTML — 0 mismatches across all 7 JS/HTML pairs (66 total id
  references)
- Every `fetch("/api/...")` call in JS matched 1:1 against the FastAPI
  route table (including the new `/api/auth/*` routes)
- SVG tag balance checked across all 5 workspace pages (sidebar icons) —
  all balanced

**Live server tests (curl + a Python `requests` end-to-end script)**
- Public pages (`/`, `/login`, `/register`) → `200`
- Protected API called with no token → `401 {"detail":"Not authenticated."}`
- `POST /api/auth/register` → `200`, returns JWT + user
- `POST /api/auth/login` with correct password → `200`, new JWT
- `POST /api/auth/login` with wrong password → `401`
- Duplicate `POST /api/auth/register` (same username) → `400`
- `GET /api/auth/me` with valid token → `200`, correct user
- `GET /api/auth/me` with garbage token → `401 {"detail":"Invalid authentication token."}`
- `POST /api/analyze` with valid token → `200`, correct ML classification (`Billing`, source `ml`), full result shape unchanged from before
- `POST /api/upload` with valid token → `200`, 900 rows processed
- `POST /api/upload` / `GET /api/metrics` with **no** token → both `401`
- All five workspace routes (`/workspace`, `/analyze`, `/insights`,
  `/departments`, `/models`) → `200`
- Legacy `/dashboard.html` → `307` → `/workspace`; `/analyze.html` → `307`
  → `/analyze`
- **Full click-through simulation** (one script, one session, mirroring
  exactly what the browser JS does): landing loads → register → all five
  workspace pages load → `/api/auth/me` confirms session → Analyze
  returns a correct ML result → Overview's upload flow processes the
  sample CSV → Insights/Departments/Models' shared `/api/metrics` call
  returns department + model data → an invalid token is rejected
  (logout's effect). **All steps passed.**
- Server log reviewed after the full test session — clean, no unhandled
  exceptions
- `streamlit run app.py` and `uvicorn backend.main:app` re-verified
  running concurrently without conflict, both `200`

## 6. Run instructions

Unchanged from before, plus the auth dependency:

```bash
pip install -r requirements.txt   # now includes PyJWT
python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab'); nltk.download('stopwords'); nltk.download('wordnet')"

# Models must already be trained — if models/ is empty:
python training/generate_sample_data.py
python training/train_models.py

uvicorn backend.main:app --reload --port 8000
# → http://localhost:8000/            landing
# → http://localhost:8000/register    create an account
# → http://localhost:8000/workspace   Overview (after login)

streamlit run app.py   # unchanged, runs independently
```

Optional: set `HEALTHGUARD_JWT_SECRET` in the environment for a stable
signing key across restarts (otherwise a new one is generated each
process start, invalidating existing sessions — acceptable for this
lightweight, no-database setup, same as the user store itself resetting).

## 7. Files created / modified in this pass

**Created**
- `backend/auth.py`
- `frontend/login.html`, `frontend/register.html`, `frontend/workspace.html`, `frontend/insights.html`, `frontend/departments.html`, `frontend/models.html`
- `frontend/css/auth.css`, `frontend/css/workspace.css`
- `frontend/js/login.js`, `frontend/js/register.js`, `frontend/js/overview.js`, `frontend/js/insights.js`, `frontend/js/departments.js`, `frontend/js/models.js`

**Modified**
- `backend/main.py` — added auth routes, `Depends(auth.get_current_user)` on the four protected endpoints, new page routes, legacy redirects
- `frontend/index.html` — nav/CTAs point to `/login` and `/register` instead of the old dashboard/analyze links
- `frontend/analyze.html` — rebuilt inside the sidebar shell (same form/result markup, new chrome)
- `frontend/js/api.js` — rewritten: adds JWT-aware `request()` (attaches `Authorization` header, redirects to `/login` on `401`), `hgRequireAuth()`, `hgInitWorkspaceShell()`, and the shared `hgRenderBars`/`hgRenderDonut` chart helpers (moved here from the old `dashboard.js` so Overview/Insights/Departments can share them)
- `frontend/js/analyze.js` — added the `hgRequireAuth()` guard at the top
- `requirements.txt` — added `PyJWT`

**Removed** (fully superseded, not business logic)
- `frontend/dashboard.html`, `frontend/css/dashboard.css`, `frontend/js/dashboard.js` — split into `workspace.html` (Overview) + `insights.html` + `departments.html` + `models.html`, styled by the new `workspace.css`

**Untouched:** everything under `modules/`, `training/`, `models/`,
`data/`, `utils/` (except no changes at all this pass), `app.py`,
`backend/pipeline.py`, `backend/metrics.py`.

## 8. Limitations, carried over or new

- **In-memory user store + in-memory upload store**: both reset on
  process restart. This is the explicit "no database" trade-off — not a
  bug. A future phase adding persistence (SQLite, Postgres) would replace
  `backend/auth.py`'s `_USERS` dict and `backend/pipeline.py`'s
  `_UPLOADS` dict without touching any ML logic.
- **Single shared dataset across users**: uploads aren't scoped per-user
  yet — any authenticated user sees the most recent upload from any
  session. Acceptable for this phase's single-tenant-demo scope; flagged
  for Phase 3 if multi-tenant isolation is needed.
- **JWT secret regenerates on restart** unless `HEALTHGUARD_JWT_SECRET` is
  set, invalidating all sessions. Set the env var for anything longer-lived
  than a demo.
- Everything carried over from the Phase 1 and earlier Phase 2 reports
  still applies unchanged: synthetic training dataset, experimental
  satisfaction model, PDF-upload stub, etc.
