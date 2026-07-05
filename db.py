"""
db.py — SQLite schema, connection helper, and demo seed data for VOID·SPACE.

Mirrors the mock data baked into the original frontend (CREDS, G_ACCOUNTS,
ASSETS) so the integrated app behaves identically to the original demo on
first run, but backed by a real, persistent database — and adds an
AI_INSIGHTS table (see SDD §3.2.3) to hold Claude-API-generated tag/
description suggestions separately from human-authored asset fields.
"""
import sqlite3
import json
import time
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), "voidspace.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    role          TEXT NOT NULL CHECK(role IN ('creator','assessor')),
    provider      TEXT NOT NULL DEFAULT 'email',   -- 'email' | 'google'
    color         TEXT DEFAULT '#00d4ff',
    initials      TEXT DEFAULT '',
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('draft','pending','approved','rejected','revision')),
    icon            TEXT DEFAULT '📦',
    shape           TEXT DEFAULT 'box',
    tags            TEXT DEFAULT '[]',      -- JSON array
    category        TEXT,
    color           TEXT DEFAULT '#00d4ff',
    comment         TEXT DEFAULT '',
    format          TEXT DEFAULT 'glb',
    polycount       INTEGER DEFAULT 0,
    source_tool     TEXT DEFAULT 'other',
    xr_ready        INTEGER DEFAULT 0,
    file_path       TEXT,
    creator_id      INTEGER NOT NULL REFERENCES users(id),
    reviewer_id     INTEGER REFERENCES users(id),
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_insights (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id                INTEGER NOT NULL UNIQUE REFERENCES assets(id) ON DELETE CASCADE,
    suggested_tags          TEXT DEFAULT '[]',   -- JSON array
    suggested_description   TEXT DEFAULT '',
    quality_flags           TEXT DEFAULT '[]',   -- JSON array
    confidence              REAL DEFAULT 0,
    model_version           TEXT DEFAULT '',
    generated_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,             -- blender_export | meshy_generate | threejs_render
    status      TEXT NOT NULL DEFAULT 'processing',
    payload     TEXT DEFAULT '{}',
    result      TEXT DEFAULT '{}',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    api_key     TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    scopes      TEXT NOT NULL DEFAULT 'read',
    created_at  INTEGER NOT NULL
);
"""


def seed(conn):
    now = int(time.time())

    # name, email, password(None=google SSO only), role, provider, color, initials
    # NOTE: alex.rivera@gmail.com / morgan.ellis@gmail.com double as the
    # login-screen's demo email/password accounts (so seeded demo assets are
    # visible however the user signs in) AND as Google-popup choices.
    users = [
        ("Alex Rivera",  "alex.rivera@gmail.com",  "create3d", "creator",  "email",  "#7c3aed", "AR"),
        ("Sam Chen",     "sam.chen@gmail.com",      None,       "creator",  "google", "#0369a1", "SC"),
        ("Jordan Blake", "jordan.blake@gmail.com",  None,       "creator",  "google", "#059669", "JB"),
        ("Morgan Ellis", "morgan.ellis@gmail.com",  "assess3d", "assessor", "email",  "#b45309", "ME"),
        ("Casey Park",   "casey.park@gmail.com",    None,       "assessor", "google", "#c026d3", "CP"),
    ]
    user_ids = {}
    for name, email, pw, role, provider, color, initials in users:
        pw_hash = generate_password_hash(pw) if pw else None
        conn.execute(
            "INSERT OR IGNORE INTO users (name,email,password_hash,role,provider,color,initials,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, email, pw_hash, role, provider, color, initials, now),
        )
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        user_ids[email] = row["id"]

    alex = user_ids["alex.rivera@gmail.com"]
    sam = user_ids["sam.chen@gmail.com"]
    jordan = user_ids["jordan.blake@gmail.com"]

    assets = [
        ("Sci-Fi Corridor Module", "approved", "🏗️", "box",      ["architecture", "sci-fi", "PBR"],
         "Architecture", "#00d4ff", "Great topology. Approved for XR pipeline.", "glb", 12480, "blender", 1, alex),
        ("Humanoid Rig v3", "pending", "🧍", "capsule",  ["character", "rigged", "glTF"],
         "Character", "#7c3aed", "", "gltf", 34210, "blender", 0, alex),
        ("Ancient Temple Pack", "revision", "🏛️", "pyramid",  ["environment", "historical", "OBJ"],
         "Environment", "#ff6b35", "Polycount too high for XR. Please optimize.", "obj", 210500, "sketchfab", 0, alex),
        ("Hover Vehicle Alpha", "pending", "🚗", "cylinder", ["vehicle", "low-poly", "FBX"],
         "Vehicle", "#39ff14", "", "fbx", 8600, "meshy", 1, alex),
        ("PBR Stone Texture Set", "approved", "🪨", "sphere",   ["texture", "PBR", "4K"],
         "Texture Pack", "#a78bfa", "Excellent quality. Ready for publish.", "glb", 480, "polypizza", 1, alex),
        ("XR Training Room", "draft", "🥽", "torus",    ["XR", "EoN", "environment"],
         "XR Scene", "#00d4ff", "", "glb", 15200, "blender", 1, alex),
        ("Mech Arm Assembly", "pending", "🦾", "box",      ["character", "mech", "rigged"],
         "Character", "#ff6b35", "", "fbx", 22100, "blender", 0, sam),
        ("Forest Environment Pack", "rejected", "🌲", "pyramid",  ["environment", "nature", "FBX"],
         "Environment", "#39ff14", "Missing LOD levels. Not XR-ready.", "fbx", 340000, "sketchfab", 0, jordan),
    ]
    for name, status, icon, shape, tags, cat, color, comment, fmt, poly, tool, xr, creator_id in assets:
        exists = conn.execute("SELECT id FROM assets WHERE name=?", (name,)).fetchone()
        if exists:
            continue
        cur = conn.execute(
            "INSERT INTO assets (name,status,icon,shape,tags,category,color,comment,format,"
            "polycount,source_tool,xr_ready,file_path,creator_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, status, icon, shape, json.dumps(tags), cat, color, comment, fmt,
             poly, tool, xr, None, creator_id, now, now),
        )
        asset_id = cur.lastrowid
        # Pre-populate demo AI insights offline (no network call at seed time) so the
        # UI has something to show immediately even before ANTHROPIC_API_KEY is set.
        from ai_assistant import offline_fallback
        insight = offline_fallback(name, cat, tags)
        conn.execute(
            "INSERT OR IGNORE INTO ai_insights (asset_id,suggested_tags,suggested_description,"
            "quality_flags,confidence,model_version,generated_at) VALUES (?,?,?,?,?,?,?)",
            (asset_id, json.dumps(insight["suggested_tags"]), insight["suggested_description"],
             json.dumps(insight["quality_flags"]), insight["confidence"], insight["model_version"], now),
        )

    # Demo API key — the same one shown in the frontend's API Docs page
    conn.execute(
        "INSERT OR IGNORE INTO api_keys (api_key,label,scopes,created_at) VALUES (?,?,?,?)",
        ("vs_live_3d_xK9mNpQrLv8w2026", "Demo Key", "read write publish", now),
    )
    conn.commit()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    seed(conn)
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
