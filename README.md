# VOID·SPACE — 3D Digital Asset Management Platform

A full-stack, AI-enhanced 3D asset management and XR-publishing platform.
Frontend and backend are now fully integrated: every button in the UI
calls a real REST API, backed by a persistent database and a live Claude
API integration for AI-assisted asset tagging.

```
voidspace-project/
├── backend/          Flask REST API + AI Assistant Service + SQLite DB
│   ├── app.py         Main application (routes, request handling)
│   ├── auth.py         JWT auth, login, scopes
│   ├── db.py            SQLite schema + demo data seeding
│   ├── ai_assistant.py    Claude API integration (with offline fallback)
│   ├── requirements.txt
│   ├── .env.example
│   └── uploads/          Uploaded asset files land here
└── frontend/
    └── index.html      The full single-page app (UI + JS, talks to the API)
```

## 1. Run it (VS Code or terminal)

```bash
cd backend
pip install -r requirements.txt
python3 app.py
```

Open **http://localhost:5001** in your browser. That's it — the frontend
is served by the same Flask app, so there's no separate frontend server
or build step.

The database (`voidspace.db`) and demo data are created automatically on
first run. To reset everything, stop the server and delete `voidspace.db`
(plus any `-wal`/`-shm` files next to it).

### Demo logins
| Role | Email | Password |
|---|---|---|
| Creator | alex.rivera@gmail.com | create3d |
| Assessor | morgan.ellis@gmail.com | assess3d |

The "Sign in with Google" button opens a mocked account picker (Sam Chen /
Jordan Blake for Creator, Casey Park for Assessor) — selecting one calls
the real `/api/v1/auth/google` endpoint and provisions/logs in that account.

## 2. Enable live AI-assisted tagging (optional)

Every asset upload automatically asks an AI Assistant Service to suggest
tags, a description, and quality flags. By default (no API key) this runs
on a deterministic **offline fallback** so the app works out of the box.
To use the real Claude API instead:

```bash
cp backend/.env.example backend/.env
# then edit backend/.env and set:
ANTHROPIC_API_KEY=sk-ant-...your key...
```

Restart the server. The health check at `/api/v1/health` reports which
mode is active, and the sidebar startup log prints it too:

```
AI Assistant: Claude API (claude-sonnet-5)
# or
AI Assistant: offline fallback — set ANTHROPIC_API_KEY to enable live AI enrichment
```

Nothing else changes — the upload flow, review workflow, and UI behave
identically either way (this is a deliberate design choice; see the
companion SDD, §NFR-REL.1: the app must never be blocked by third-party
availability).

## 3. What's actually wired up

Every page and action in the UI now calls the real backend:

- **Login** (email/password and mocked Google SSO) → real JWT session tokens
- **Dashboard / My Assets / Review Queue / All Assets** → live data from SQLite, refetched after every action
- **Upload** → real multipart file upload (`.glb .gltf .obj .fbx .stl .blend`, up to 200MB), stored on disk, triggers async AI enrichment
- **AI suggestions** → shown inline on asset cards and in the review queue once the (async) Claude API call completes
- **Approve / Reject / Request Revision** → real status-changing API calls, restricted to the Assessor role and `publish` scope
- **Publish to EoN Reality** → validates the asset is `approved` first (409 otherwise), matching the documented business rule
- **3D previews** → genuine Three.js WebGL rendering, keyed off each asset's real `shape`/`color` fields from the database
- **API Docs / Test Connection** → the "Test Connection" button makes a real `GET /api/v1/health` call
- Tool integration cards (Sketchfab, Poly Pizza, Blender, Meshy AI) are documented and their endpoints are live and tested; the UI cards link to the docs tab rather than embedding full search widgets

## 4. Full API reference

See `backend/app.py` for the implementation, or open the app and go to
**API Docs** in the sidebar for the same reference rendered in-app.

| Method | Path | Scope |
|---|---|---|
| POST | /api/v1/auth/login | — |
| POST | /api/v1/auth/google | — |
| POST | /api/v1/auth/token | — |
| GET  | /api/v1/users/me | read |
| GET  | /api/v1/assets | read |
| GET  | /api/v1/assets/:id | read |
| POST | /api/v1/assets | write |
| POST | /api/v1/assets/:id/analyze | write |
| PUT  | /api/v1/assets/:id/status | publish |
| DELETE | /api/v1/assets/:id | write |
| GET  | /api/v1/stats/dashboard | read |
| GET  | /api/v1/tools | read |
| GET  | /api/v1/tools/sketchfab/search | read |
| GET  | /api/v1/tools/polypizza/models | read |
| POST | /api/v1/tools/blender/export | write |
| POST | /api/v1/tools/meshy/generate | write |
| GET  | /api/v1/jobs/:job_id | read |
| POST | /api/v1/publish/eon-reality | publish |
| GET  | /api/v1/health | — |

## 5. Notes

- This is a development setup (Flask's built-in server, SQLite, local file
  storage). For production, put it behind Gunicorn + Nginx and switch to
  PostgreSQL — see the companion SDD (`VOID-SPACE_SDD.docx`) §9 for the
  recommended deployment topology.
- CORS is wide open by default for easy local development; tighten
  `Access-Control-Allow-Origin` in `app.py` before deploying publicly.
- The JWT signing secret in `auth.py` is a development placeholder —
  change `JWT_SECRET` before deploying anywhere real.
