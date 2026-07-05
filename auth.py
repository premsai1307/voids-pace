"""
auth.py — JWT issuance/verification, password check, and the auth decorator.

Two ways to get a bearer token, matching the frontend's two login paths:
  1. User session login  -> POST /api/v1/auth/login   (email + password)
                             POST /api/v1/auth/google  (mocked SSO — no real
                             Google verification is done, matching the
                             frontend's simulated Google popup)
  2. Server-to-server     -> POST /api/v1/auth/token   (api_key + scope),
                             exactly as documented on the frontend's API Docs page.
"""
import time
import jwt
from functools import wraps
from flask import request, jsonify, g

from werkzeug.security import check_password_hash

from db import get_db

JWT_SECRET = "voidspace-dev-secret-change-in-production"
JWT_ALGO = "HS256"
TOKEN_TTL_SECONDS = 3600


def make_token(*, sub, kind, role=None, scopes="read", name=None):
    now = int(time.time())
    payload = {
        "sub": sub,
        "kind": kind,          # "user" | "api_key"
        "role": role,          # "creator" | "assessor" | None for api_key clients
        "scopes": scopes,      # space-separated string, e.g. "read write publish"
        "name": name,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)
    return token, TOKEN_TTL_SECONDS


def decode_token(token):
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])


def login_with_password(email, password):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not user or not user["password_hash"]:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return dict(user)


def login_with_google(email, name, role):
    """Mocked Google SSO: trusts the client-asserted profile (this is a demo
    app — the real frontend simulates the Google popup client-side too).
    Creates the account on first sign-in if it doesn't exist yet."""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        now = int(time.time())
        initials = "".join([w[0] for w in name.split()][:2]).upper()
        conn.execute(
            "INSERT INTO users (name,email,password_hash,role,provider,color,initials,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, email, None, role, "google", "#00d4ff", initials, now),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    return dict(user)


def scopes_for_role(role):
    return "read write publish" if role == "assessor" else "read write"


def require_auth(required_scope=None):
    """Decorator: validates the Bearer token and (optionally) a required scope.
    Populates flask.g.auth with the decoded token claims."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            authz = request.headers.get("Authorization", "")
            if not authz.startswith("Bearer "):
                return jsonify({"error": "unauthorized", "message": "Missing bearer token"}), 401
            token = authz.split(" ", 1)[1]
            try:
                claims = decode_token(token)
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "token_expired", "message": "Token has expired"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "invalid_token", "message": "Could not validate token"}), 401

            if required_scope:
                granted = set(claims.get("scopes", "").split())
                if required_scope not in granted:
                    return jsonify({
                        "error": "insufficient_scope",
                        "message": f"This action requires the '{required_scope}' scope"
                    }), 403

            g.auth = claims
            return fn(*args, **kwargs)
        return wrapper
    return decorator
