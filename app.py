"""
VOID·SPACE — integrated backend + frontend.

Serves the single-page frontend at "/" and the full REST API documented in
the frontend's own "API Docs" tab under /api/v1/, including the AI Content
Intelligence module (Claude API integration, see ai_assistant.py).

Run:
    pip install -r requirements.txt
    python3 app.py
Then open http://localhost:5001 in a browser.

See README.md for full setup, environment variables, and API reference.
"""
import os
import json
import time
import uuid
import random
import threading

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass  # python-dotenv is optional; real environment variables still work

from flask import Flask, request, jsonify, g, send_from_directory, send_file
from werkzeug.utils import secure_filename

from db import get_db, init_db, DB_PATH
from auth import (
    make_token, login_with_password, login_with_google,
    scopes_for_role, require_auth,
)
import ai_assistant

BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
ALLOWED_EXT = {"glb", "gltf", "obj", "fbx", "stl", "blend"}
MAX_UPLOAD_MB = 200

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# ─────────────────────────── CORS (hand-rolled, no deps) ───────────────────────────
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return "", 204


# ─────────────────────────── frontend serving ───────────────────────────
@app.get("/")
def serve_index():
    return send_file(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/<path:filename>")
def serve_frontend_asset(filename):
    """Serves any other static frontend file (css/js/images) if present;
    falls through to 404 otherwise. The current build is a single-file
    frontend, so this mainly future-proofs for a split-out asset."""
    full_path = os.path.join(FRONTEND_DIR, filename)
    if os.path.isfile(full_path):
        return send_from_directory(FRONTEND_DIR, filename)
    return jsonify({"error": "not_found", "message": "No such file"}), 404


# ─────────────────────────── helpers ───────────────────────────
def row_to_user_public(u):
    return {
        "id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
        "provider": u["provider"], "color": u["color"], "initials": u["initials"],
    }


def row_to_asset(a, conn):
    creator = conn.execute("SELECT * FROM users WHERE id=?", (a["creator_id"],)).fetchone()
    insight = conn.execute("SELECT * FROM ai_insights WHERE asset_id=?", (a["id"],)).fetchone()
    ai_insights = None
    if insight:
        ai_insights = {
            "suggested_tags": json.loads(insight["suggested_tags"] or "[]"),
            "suggested_description": insight["suggested_description"],
            "quality_flags": json.loads(insight["quality_flags"] or "[]"),
            "confidence": insight["confidence"],
            "model_version": insight["model_version"],
            "generated_at": insight["generated_at"],
        }
    return {
        "id": a["id"],
        "name": a["name"],
        "status": a["status"],
        "icon": a["icon"],
        "shape": a["shape"],
        "tags": json.loads(a["tags"] or "[]"),
        "category": a["category"],
        "color": a["color"],
        "comment": a["comment"],
        "format": a["format"],
        "polycount": a["polycount"],
        "source_tool": a["source_tool"],
        "xr_ready": bool(a["xr_ready"]),
        "download_url": f"/uploads/{a['file_path']}" if a["file_path"] else None,
        "preview_url": f"/uploads/{a['file_path']}" if a["file_path"] else None,
        "creator": creator["name"] if creator else "Unknown",
        "creator_id": a["creator_id"],
        "created_at": a["created_at"],
        "updated_at": a["updated_at"],
        "ai_insights": ai_insights,
    }


def error(msg, code=400, err="bad_request"):
    return jsonify({"error": err, "message": msg}), code


def _enrich_asset_async(asset_id, name, category, filename, tags, fmt, polycount):
    """Runs in a background thread (see create_asset). Calls the AI Assistant
    Service and persists the result into AI_INSIGHTS. SQLite connections are
    not shared across threads, so a fresh connection is opened here."""
    try:
        result = ai_assistant.analyze_asset(name, category, filename, tags, fmt, polycount)
        conn = get_db()
        now = int(time.time())
        conn.execute(
            "INSERT INTO ai_insights (asset_id,suggested_tags,suggested_description,quality_flags,"
            "confidence,model_version,generated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(asset_id) DO UPDATE SET suggested_tags=excluded.suggested_tags, "
            "suggested_description=excluded.suggested_description, quality_flags=excluded.quality_flags, "
            "confidence=excluded.confidence, model_version=excluded.model_version, generated_at=excluded.generated_at",
            (asset_id, json.dumps(result["suggested_tags"]), result["suggested_description"],
             json.dumps(result["quality_flags"]), result["confidence"], result["model_version"], now),
        )
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001 — background thread must never crash the process
        print(f"[ai_assistant] enrichment failed for asset {asset_id}: {exc}")


# ═══════════════════════════ AUTH ═══════════════════════════

@app.post("/api/v1/auth/login")
def auth_login():
    body = request.get_json(silent=True) or {}
    email, password = body.get("email"), body.get("password")
    if not email or not password:
        return error("email and password are required")
    user = login_with_password(email, password)
    if not user:
        return error("Invalid credentials.", 401, "invalid_credentials")
    token, ttl = make_token(sub=user["id"], kind="user", role=user["role"],
                             scopes=scopes_for_role(user["role"]), name=user["name"])
    return jsonify({"token": token, "expires_in": ttl, "user": row_to_user_public(user)})


@app.post("/api/v1/auth/google")
def auth_google():
    body = request.get_json(silent=True) or {}
    email, name, role = body.get("email"), body.get("name"), body.get("role")
    if not email or not name or role not in ("creator", "assessor"):
        return error("email, name and role ('creator'|'assessor') are required")
    user = login_with_google(email, name, role)
    token, ttl = make_token(sub=user["id"], kind="user", role=user["role"],
                             scopes=scopes_for_role(user["role"]), name=user["name"])
    return jsonify({"token": token, "expires_in": ttl, "user": row_to_user_public(user)})


@app.post("/api/v1/auth/token")
def auth_token():
    body = request.get_json(silent=True) or {}
    api_key, scope = body.get("api_key"), body.get("scope", "read")
    if not api_key:
        return error("api_key is required")
    conn = get_db()
    row = conn.execute("SELECT * FROM api_keys WHERE api_key=?", (api_key,)).fetchone()
    conn.close()
    if not row:
        return error("Invalid API key", 401, "invalid_api_key")
    granted = set(row["scopes"].split())
    requested = set(scope.split())
    if not requested.issubset(granted):
        return error(f"Key does not have scopes: {requested - granted}", 403, "insufficient_scope")
    token, ttl = make_token(sub=api_key, kind="api_key", role=None, scopes=" ".join(requested))
    return jsonify({"token": token, "expires_in": ttl})


@app.get("/api/v1/users/me")
@require_auth()
def users_me():
    if g.auth["kind"] != "user":
        return error("This endpoint requires a user session token", 403, "wrong_token_kind")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (g.auth["sub"],)).fetchone()
    conn.close()
    if not user:
        return error("User not found", 404, "not_found")
    return jsonify(row_to_user_public(user))


# ═══════════════════════════ ASSETS ═══════════════════════════

@app.get("/api/v1/assets")
@require_auth("read")
def list_assets():
    status = request.args.get("status")
    fmt = request.args.get("format")
    category = request.args.get("category")
    creator_id = request.args.get("creator_id")
    limit = min(int(request.args.get("limit", 20)), 200)

    q = "SELECT * FROM assets WHERE 1=1"
    params = []
    if status:
        q += " AND status=?"; params.append(status)
    if fmt:
        q += " AND format=?"; params.append(fmt)
    if category:
        q += " AND lower(category)=lower(?)"; params.append(category)
    if creator_id:
        q += " AND creator_id=?"; params.append(creator_id)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    conn = get_db()
    rows = conn.execute(q, params).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
    data = [row_to_asset(r, conn) for r in rows]
    conn.close()
    return jsonify({"data": data, "meta": {"total": total, "page": 1, "returned": len(data)}})


@app.get("/api/v1/assets/<int:asset_id>")
@require_auth("read")
def get_asset(asset_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        conn.close()
        return error("Asset not found", 404, "not_found")
    result = row_to_asset(row, conn)
    conn.close()
    return jsonify(result)


CATEGORY_ICON = {
    "Architecture": "🏗️", "Character": "🧍", "Prop / Object": "📦",
    "Environment": "🌲", "Vehicle": "🚗", "Texture Pack": "🪨", "XR Scene": "🥽",
}
CATEGORY_SHAPE = {
    "Architecture": "box", "Character": "capsule", "Prop / Object": "box",
    "Environment": "pyramid", "Vehicle": "cylinder", "Texture Pack": "sphere", "XR Scene": "torus",
}


@app.post("/api/v1/assets")
@require_auth("write")
def create_asset():
    if g.auth["kind"] != "user":
        return error("Uploading requires a user session token", 403, "wrong_token_kind")

    name = request.form.get("name")
    category = request.form.get("category", "Prop / Object")
    tags = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    source_tool = request.form.get("source_tool", "other")
    submit_for_review = request.form.get("submit_for_review", "true").lower() == "true"

    if not name:
        return error("name is required")

    file_path_name = None
    fmt = "glb"
    original_filename = None
    if "file" in request.files and request.files["file"].filename:
        f = request.files["file"]
        original_filename = f.filename
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_EXT:
            return error(f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXT))}")
        fmt = ext
        file_path_name = f"{uuid.uuid4().hex}_{secure_filename(f.filename)}"
        f.save(os.path.join(UPLOAD_DIR, file_path_name))

    now = int(time.time())
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO assets (name,status,icon,shape,tags,category,color,comment,format,"
        "polycount,source_tool,xr_ready,file_path,creator_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, "pending" if submit_for_review else "draft",
         CATEGORY_ICON.get(category, "📦"), CATEGORY_SHAPE.get(category, "box"),
         json.dumps(tags), category, random.choice(["#00d4ff", "#7c3aed", "#39ff14", "#ff6b35"]),
         "", fmt, 0, source_tool, 0, file_path_name, g.auth["sub"], now, now),
    )
    conn.commit()
    asset_id = cur.lastrowid
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    result = row_to_asset(row, conn)
    conn.close()

    # Fire-and-forget AI enrichment (FR-6.1) — never blocks the upload response.
    threading.Thread(
        target=_enrich_asset_async,
        args=(asset_id, name, category, original_filename, tags, fmt, 0),
        daemon=True,
    ).start()

    return jsonify({
        "id": f"ast_{asset_id}",
        "status": result["status"],
        "preview_job_id": f"job_render_{uuid.uuid4().hex[:8]}",
        "review_url": f"/review/{asset_id}",
        "asset": result,
    }), 201


@app.post("/api/v1/assets/<int:asset_id>/analyze")
@require_auth("write")
def reanalyze_asset(asset_id):
    """On-demand re-run of the AI Assistant Service for a given asset
    (e.g. after a Creator edits tags/category following a revision request)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        conn.close()
        return error("Asset not found", 404, "not_found")
    conn.close()
    tags = json.loads(row["tags"] or "[]")
    result = ai_assistant.analyze_asset(row["name"], row["category"], row["file_path"], tags, row["format"], row["polycount"])
    conn = get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO ai_insights (asset_id,suggested_tags,suggested_description,quality_flags,"
        "confidence,model_version,generated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(asset_id) DO UPDATE SET suggested_tags=excluded.suggested_tags, "
        "suggested_description=excluded.suggested_description, quality_flags=excluded.quality_flags, "
        "confidence=excluded.confidence, model_version=excluded.model_version, generated_at=excluded.generated_at",
        (asset_id, json.dumps(result["suggested_tags"]), result["suggested_description"],
         json.dumps(result["quality_flags"]), result["confidence"], result["model_version"], now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    out = row_to_asset(row, conn)
    conn.close()
    return jsonify(out)


@app.put("/api/v1/assets/<int:asset_id>/status")
@require_auth("publish")
def update_asset_status(asset_id):
    if g.auth["kind"] == "user" and g.auth.get("role") != "assessor":
        return error("Only assessors can review assets", 403, "forbidden")

    body = request.get_json(silent=True) or {}
    status = body.get("status")
    comment = body.get("comment", "")
    if status not in ("approved", "rejected", "revision_needed", "revision"):
        return error("status must be one of approved | rejected | revision_needed")
    if status == "revision_needed":
        status = "revision"

    conn = get_db()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        conn.close()
        return error("Asset not found", 404, "not_found")

    reviewer_id = g.auth["sub"] if g.auth["kind"] == "user" else None
    now = int(time.time())
    conn.execute(
        "UPDATE assets SET status=?, comment=?, reviewer_id=?, updated_at=? WHERE id=?",
        (status, comment, reviewer_id, now, asset_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    result = row_to_asset(row, conn)
    conn.close()
    return jsonify(result)


@app.delete("/api/v1/assets/<int:asset_id>")
@require_auth("write")
def delete_asset(asset_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        conn.close()
        return error("Asset not found", 404, "not_found")
    if g.auth["kind"] == "user" and row["creator_id"] != g.auth["sub"] and g.auth.get("role") != "assessor":
        conn.close()
        return error("You can only delete your own assets", 403, "forbidden")
    conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": True, "id": asset_id})


@app.get("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ═══════════════════════════ DASHBOARD / STATS ═══════════════════════════

@app.get("/api/v1/stats/dashboard")
@require_auth("read")
def dashboard_stats():
    conn = get_db()
    if g.auth["kind"] == "user" and g.auth.get("role") == "creator":
        uid = g.auth["sub"]
        mine = conn.execute("SELECT * FROM assets WHERE creator_id=?", (uid,)).fetchall()
        stats = {
            "my_assets": len(mine),
            "approved": len([a for a in mine if a["status"] == "approved"]),
            "pending_review": len([a for a in mine if a["status"] == "pending"]),
            "needs_revision": len([a for a in mine if a["status"] == "revision"]),
        }
    else:
        all_assets = conn.execute("SELECT * FROM assets").fetchall()
        stats = {
            "awaiting_review": len([a for a in all_assets if a["status"] == "pending"]),
            "approved_total": len([a for a in all_assets if a["status"] == "approved"]),
            "all_assets": len(all_assets),
            "avg_review_time_days": 2.1,
        }
    conn.close()
    return jsonify(stats)


# ═══════════════════════════ TOOL INTEGRATIONS ═══════════════════════════

TOOLS = [
    {"name": "Blender", "type": "Open Source 3D Suite", "endpoint": "POST /api/v1/tools/blender/export"},
    {"name": "Sketchfab", "type": "3D Model Marketplace", "endpoint": "GET /api/v1/tools/sketchfab/search"},
    {"name": "Poly Pizza", "type": "Free Low-Poly Asset Library", "endpoint": "GET /api/v1/tools/polypizza/models"},
    {"name": "Three.js", "type": "WebGL 3D Library", "endpoint": "POST /api/v1/tools/threejs/render"},
    {"name": "Meshy AI", "type": "AI 3D Model Generator", "endpoint": "POST /api/v1/tools/meshy/generate"},
    {"name": "EoN Reality", "type": "XR Training Platform", "endpoint": "POST /api/v1/publish/eon-reality"},
]


@app.get("/api/v1/tools")
@require_auth("read")
def list_tools():
    return jsonify({"tools": TOOLS})


_SKETCHFAB_MOCK = [
    {"name": "Robot Drone", "tags": ["robot", "sci-fi"]},
    {"name": "Cyberpunk Alley Lamp", "tags": ["urban", "prop"]},
    {"name": "Stylized Rock Cluster", "tags": ["nature", "rock"]},
    {"name": "Low Poly Fox", "tags": ["animal", "character"]},
]


@app.get("/api/v1/tools/sketchfab/search")
@require_auth("read")
def sketchfab_search():
    q = request.args.get("q", "")
    license_ = request.args.get("license", "cc0")
    max_faces = int(request.args.get("max_face_count", 10_000_000))
    results = []
    for m in _SKETCHFAB_MOCK:
        if q and q.lower() not in m["name"].lower() and not any(q.lower() in t for t in m["tags"]):
            continue
        face_count = random.randint(2000, 60000)
        if face_count > max_faces:
            continue
        sid = uuid.uuid4().hex[:8]
        results.append({
            "sketchfab_id": sid,
            "name": m["name"],
            "face_count": face_count,
            "license": license_,
            "download_url": f"https://sketchfab.com/3d-models/{sid}",
            "xr_suitable": face_count <= 50000,
        })
    return jsonify({"results": results})


_POLYPIZZA_MOCK = [
    {"name": "Low Poly Pine Tree", "category": "nature"},
    {"name": "Low Poly Sedan", "category": "urban"},
    {"name": "Fantasy Sword", "category": "fantasy"},
    {"name": "Sci-Fi Crate", "category": "sci-fi"},
]


@app.get("/api/v1/tools/polypizza/models")
@require_auth("read")
def polypizza_models():
    keyword = request.args.get("keyword", "")
    category = request.args.get("category")
    max_tris = int(request.args.get("max_tris", 10_000_000))
    models = []
    for m in _POLYPIZZA_MOCK:
        if keyword and keyword.lower() not in m["name"].lower():
            continue
        if category and category.lower() != m["category"]:
            continue
        tris = random.randint(200, 4000)
        if tris > max_tris:
            continue
        mid = f"pp_{uuid.uuid4().hex[:6]}"
        models.append({
            "id": mid, "name": m["name"], "tris": tris,
            "glb_url": f"https://poly.pizza/m/{mid}", "license": "CC0",
        })
    return jsonify({"models": models})


def _create_job(job_type, payload):
    job_id = f"job_{job_type}_{uuid.uuid4().hex[:8]}"
    now = int(time.time())
    conn = get_db()
    conn.execute(
        "INSERT INTO jobs (id,type,status,payload,result,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (job_id, job_type, "processing", json.dumps(payload), "{}", now, now),
    )
    conn.commit()
    conn.close()
    return job_id


@app.post("/api/v1/tools/blender/export")
@require_auth("write")
def blender_export():
    body = request.get_json(silent=True) or {}
    if not body.get("blend_url"):
        return error("blend_url is required")
    job_id = _create_job("blender_export", body)
    return jsonify({"job_id": job_id, "status": "processing"}), 202


@app.post("/api/v1/tools/meshy/generate")
@require_auth("write")
def meshy_generate():
    body = request.get_json(silent=True) or {}
    if not body.get("prompt"):
        return error("prompt is required")
    job_id = _create_job("meshy_generate", body)
    return jsonify({"job_id": job_id, "status": "processing"}), 202


@app.post("/api/v1/tools/threejs/render")
@require_auth("write")
def threejs_render():
    body = request.get_json(silent=True) or {}
    if not body.get("glb_url"):
        return error("glb_url is required")
    job_id = _create_job("threejs_render", body)
    return jsonify({"job_id": job_id, "status": "processing"}), 202


@app.get("/api/v1/jobs/<job_id>")
@require_auth("read")
def job_status(job_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return error("Job not found", 404, "not_found")

    if row["status"] == "processing" and time.time() - row["created_at"] > 4:
        payload = json.loads(row["payload"])
        if row["type"] == "blender_export":
            result = {"asset_url": f"https://cdn.voidspace.io/assets/{uuid.uuid4().hex[:8]}.glb",
                       "export_format": payload.get("export_format", "glb")}
        elif row["type"] == "meshy_generate":
            result = {"model_url": f"https://cdn.voidspace.io/generated/{uuid.uuid4().hex[:8]}.glb",
                       "prompt": payload.get("prompt")}
        else:
            result = {"preview_png": f"https://cdn.voidspace.io/previews/{uuid.uuid4().hex[:8]}.png"}
        conn.execute("UPDATE jobs SET status='completed', result=?, updated_at=? WHERE id=?",
                     (json.dumps(result), int(time.time()), job_id))
        conn.commit()
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    result = {"job_id": row["id"], "type": row["type"], "status": row["status"],
              "result": json.loads(row["result"])}
    conn.close()
    return jsonify(result)


@app.post("/api/v1/publish/eon-reality")
@require_auth("publish")
def publish_eon_reality():
    body = request.get_json(silent=True) or {}
    asset_id = body.get("asset_id")
    if not asset_id:
        return error("asset_id is required")
    conn = get_db()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (str(asset_id).replace("ast_", ""),)).fetchone()
    if not row:
        conn.close()
        return error("Asset not found", 404, "not_found")
    if row["status"] != "approved":
        conn.close()
        return error("Only approved assets can be published to EoN Reality", 409, "not_approved")
    conn.close()
    return jsonify({
        "publish_id": f"eon_{uuid.uuid4().hex[:8]}",
        "status": "published",
        "xr_manifest_url": f"https://cdn.voidspace.io/xr/{uuid.uuid4().hex[:8]}/manifest.json",
        "module_url": f"https://eon-reality.example.com/modules/{uuid.uuid4().hex[:8]}",
    })


# ═══════════════════════════ MISC ═══════════════════════════

@app.get("/api/v1/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "voidspace-api",
        "time": int(time.time()),
        "ai_assistant": "connected" if ai_assistant.ANTHROPIC_API_KEY else "offline-fallback (no ANTHROPIC_API_KEY set)",
    })


if __name__ == "__main__":
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    init_db()
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    print(f"VOID·SPACE running at http://localhost:{port}")
    print(f"AI Assistant: {'Claude API (' + ai_assistant.ANTHROPIC_MODEL + ')' if ai_assistant.ANTHROPIC_API_KEY else 'offline fallback — set ANTHROPIC_API_KEY to enable live AI enrichment'}")
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
