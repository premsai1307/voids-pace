"""
ai_assistant.py — Claude API integration for AI-assisted asset tagging.

Implements SRS FR-6.1..FR-6.7 and SDD §4.4: when a new asset is uploaded,
the backend asynchronously asks the Claude API for a structured, JSON-only
classification aid (suggested tags, a short description, quality flags,
and a confidence score). The result is advisory only — it is stored in a
separate AI_INSIGHTS table (see db.py) and is never used to silently
overwrite Creator-authored metadata or to auto-approve an asset. A human
Assessor decision is always required (FR-6.6).

Degrades gracefully (NFR-REL.1): if ANTHROPIC_API_KEY is not set, the
network is unavailable, or Claude's response doesn't match the expected
schema, a deterministic local fallback is used instead so the upload and
review workflow is never blocked by third-party availability.
"""
import os
import json
import re
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip()
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
REQUEST_TIMEOUT = 20

SYSTEM_PROMPT = (
    "You are a 3D asset classification assistant embedded in a digital asset "
    "management system called VOID\u00b7SPACE. Given an asset's name, category, "
    "file format, polycount, source filename, and any creator-supplied tags, "
    "respond with ONLY a single JSON object — no markdown code fences, no "
    "prose before or after — matching EXACTLY this schema:\n\n"
    '{"suggested_tags": ["tag1", "tag2", ...],'
    ' "suggested_description": "one or two sentence human-readable summary",'
    ' "quality_flags": ["flag1", ...],'
    ' "confidence": 0.0}\n\n'
    "Rules:\n"
    "- suggested_tags: 3 to 8 short lowercase classification tags.\n"
    "- suggested_description: concise, factual, no marketing language.\n"
    "- quality_flags: 0 to 4 items drawn from concerns like 'high_polycount', "
    "'missing_textures', 'not_xr_ready', 'needs_lod', 'unclear_scale' — use "
    "polycount as a signal (over ~100,000 tris is high for real-time XR use); "
    "return an empty array if nothing stands out.\n"
    "- confidence: your self-rated confidence in these suggestions, 0.0-1.0.\n"
    "Return nothing but the JSON object."
)


def _build_user_prompt(name, category, filename, tags, fmt, polycount):
    return (
        f"Asset name: {name}\n"
        f"Category: {category or 'unspecified'}\n"
        f"File format: {fmt or 'unknown'}\n"
        f"Polycount: {polycount if polycount else 'unknown'}\n"
        f"Creator-supplied tags: {', '.join(tags) if tags else '(none)'}\n"
        f"Source filename: {filename or 'n/a'}\n\n"
        "Classify this asset per the schema in your instructions."
    )


def offline_fallback(name, category, tags, model_version="offline-fallback"):
    """Deterministic, network-free result — used for demo seeding and as the
    safety-net when the Claude API is unavailable or unconfigured."""
    base = list(dict.fromkeys((tags or []) + [(category or "asset").lower()]))[:6]
    return {
        "suggested_tags": base or ["uncategorized"],
        "suggested_description": f"{name} — a {category.lower() if category else 'general'} 3D asset awaiting AI-assisted review.",
        "quality_flags": [],
        "confidence": 0.0,
        "model_version": model_version,
    }


def _extract_json(text):
    text = text.strip()
    # Defensive strip of accidental markdown fences, even though instructed not to use them.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def analyze_asset(name, category, filename, tags, fmt=None, polycount=None):
    """Returns a dict: {suggested_tags, suggested_description, quality_flags,
    confidence, model_version}. Never raises — always returns a usable result
    so callers (e.g. the background enrichment thread) don't need try/except."""
    if not ANTHROPIC_API_KEY:
        return offline_fallback(name, category, tags, model_version="offline-fallback (no ANTHROPIC_API_KEY set)")

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    user_prompt = _build_user_prompt(name, category, filename, tags, fmt, polycount)
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 400,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    last_error = None
    for attempt in range(2):  # one retry with a stricter nudge, per FR-6.3
        try:
            resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            text = "".join(
                block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
            )
            parsed = _extract_json(text)
            if not isinstance(parsed.get("suggested_tags"), list) or "suggested_description" not in parsed:
                raise ValueError("Claude response missing required schema fields")
            parsed["suggested_tags"] = [str(t) for t in parsed["suggested_tags"]][:8]
            parsed["quality_flags"] = [str(f) for f in parsed.get("quality_flags", [])][:6]
            parsed["confidence"] = float(parsed.get("confidence", 0.5))
            parsed["model_version"] = ANTHROPIC_MODEL
            return parsed
        except Exception as exc:  # noqa: BLE001 — intentionally broad; must never crash the caller
            last_error = exc
            if attempt == 0:
                payload["messages"][0]["content"] = (
                    user_prompt + "\n\nReminder: respond with ONLY the raw JSON object, nothing else."
                )
                continue

    return offline_fallback(name, category, tags, model_version=f"offline-fallback (error: {last_error})")
