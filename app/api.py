from __future__ import annotations

import hmac
from pathlib import Path
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request, send_from_directory, url_for

from .jobs import JOB_ID_RE
from .modules import imagegen, lyrics as lyr, metadata, selector, tags as tags_mod
from .modules.models import SongRequest, Track
from .modules.selector import MOOD_TERMS
from .modules.utils import ffmpeg_status, slugify

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.before_request
def require_api_key():
    if request.endpoint in {"api.healthz", "api.keepalive"} or not _cfg().API_KEY:
        return None
    supplied = request.headers.get("Authorization", "")
    token = supplied[7:].strip() if supplied.lower().startswith("bearer ") else request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(token, _cfg().API_KEY):
        return _err("Unauthorized: provide a valid Bearer token.", 401)
    return None


def _cfg():
    return current_app.config["VG"]


def _jobs():
    return current_app.extensions["jobs"]


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


@bp.get("/health")
def health():
    cfg = _cfg()
    tools = ffmpeg_status()
    return jsonify(
        {
            "ffmpeg": bool(tools["ffmpeg"]),
            "ffmpeg_path": tools["ffmpeg"],
            "ffmpeg_error": tools["ffmpeg_error"],
            "ffprobe_path": tools["ffprobe"],
            "gemini_key": bool(cfg.GEMINI_API_KEY),
            "jamendo_key": bool(cfg.JAMENDO_CLIENT_ID),
            "trending_providers": [p for p in cfg.TRENDING_PROVIDERS if tags_mod._configured(p, cfg)],
            "image_providers": cfg.IMAGE_PROVIDERS,
            "music_sources": cfg.MUSIC_SOURCES,
            "jobs_today": _jobs().used_today(),
            "daily_limit": cfg.DAILY_LIMIT,
            "max_batch_size": cfg.MAX_BATCH_SIZE,
            "job_counts": _jobs().counts(),
            "api_key_required": bool(cfg.API_KEY),
        }
    )


@bp.get("/healthz")
def healthz():
    """Unauthenticated readiness probe for Render, including the required encoder."""
    tools = ffmpeg_status()
    if not tools["ffmpeg"]:
        return jsonify({"status": "unavailable", "error": tools["ffmpeg_error"] or "ffmpeg is missing"}), 503
    return jsonify({"status": "ok"})


@bp.get("/keepalive")
def keepalive():
    """Cheap public ping target for an external uptime monitor or cron scheduler."""
    return jsonify({"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()})


@bp.get("/options")
def options():
    return jsonify({"moods": sorted(MOOD_TERMS), "aspects": list(imagegen.ASPECTS)})


# ---------------- full pipeline ----------------

@bp.post("/jobs")
def create_job():
    cfg, store = _cfg(), _jobs()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _err("Request body must be a JSON object.")
    body = dict(body)
    count = body.pop("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > cfg.MAX_BATCH_SIZE:
        return _err(f"count must be an integer from 1 to {cfg.MAX_BATCH_SIZE}.")
    allowed = {"mood", "genre", "language", "era", "energy", "keyword", "artist",
               "royalty_free_only", "aspect", "clip_seconds"}
    unknown = set(body) - allowed
    if unknown:
        return _err(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    if any(not isinstance(body[key], str) for key in ("mood", "genre", "language", "era", "energy", "keyword", "artist", "aspect") if key in body):
        return _err("Text input fields must be strings.")
    if "royalty_free_only" in body and not isinstance(body["royalty_free_only"], bool):
        return _err("royalty_free_only must be a boolean.")
    if "clip_seconds" in body and (not isinstance(body["clip_seconds"], int) or isinstance(body["clip_seconds"], bool)):
        return _err("clip_seconds must be an integer.")
    if "clip_seconds" in body and not 10 <= body["clip_seconds"] <= 60:
        return _err("clip_seconds must be from 10 to 60.")
    if "aspect" in body and body["aspect"] not in imagegen.ASPECTS:
        return _err(f"aspect must be one of: {', '.join(imagegen.ASPECTS)}.")
    try:
        batch_id, jobs = store.submit_batch(body, count, cfg.DAILY_LIMIT)
    except ValueError as exc:
        return _err(str(exc), 429)
    payload = {"batch_id": batch_id, "count": count,
               "batch_status_url": url_for("api.batch_status", batch_id=batch_id),
               "jobs": [{"job_id": job.id, "status_url": url_for("api.job_status", job_id=job.id)} for job in jobs]}
    if count == 1:
        payload["job_id"] = jobs[0].id
        payload["status_url"] = url_for("api.job_status", job_id=jobs[0].id)
    return jsonify(payload), 202


@bp.get("/batches/<batch_id>")
def batch_status(batch_id: str):
    if not JOB_ID_RE.match(batch_id):
        return _err("Bad batch id", 400)
    batch = _jobs().batch(batch_id)
    if not batch:
        return _err("Batch not found", 404)
    for job in batch["jobs"]:
        if job["status"] == "done" and job.get("result"):
            job["urls"] = {k: url_for("api.job_file", job_id=job["id"], name=v)
                           for k, v in job["result"].get("files", {}).items()}
    return jsonify(batch)


@bp.get("/jobs/<job_id>")
def job_status(job_id: str):
    job = _jobs().get(job_id)
    if not job:
        return _err("Job not found", 404)
    data = job.to_dict()
    if job.status == "done" and job.result:
        data["urls"] = {
            k: url_for("api.job_file", job_id=job.id, name=v)
            for k, v in job.result.get("files", {}).items()
        }
    return jsonify(data)


@bp.get("/files/<job_id>/<name>")
def job_file(job_id: str, name: str):
    if not JOB_ID_RE.match(job_id):
        return _err("Bad job id", 400)
    return send_from_directory(Path(_cfg().OUTPUT_DIR) / job_id, name)


# ---------------- individual steps (reuse each module on its own) ----------------

@bp.post("/steps/song")
def step_song():
    body = request.get_json(silent=True) or {}
    req = SongRequest.from_dict(body)
    ranked = selector.rank_tracks(req, _cfg())
    return jsonify({"count": len(ranked), "top": [t.to_dict() for t in ranked[: int(body.get("limit", 5))]]})


def _track_from(body: dict) -> Track:
    if not body.get("title") or not body.get("artist"):
        raise ValueError("title and artist are required")
    return Track(
        provider="manual", track_id=slugify(body["title"]), title=body["title"], artist=body["artist"],
        album=body.get("album", ""), song_duration=float(body.get("duration") or 0), genre=body.get("genre", ""),
    )


@bp.post("/steps/lyrics")
def step_lyrics():
    try:
        track = _track_from(request.get_json(silent=True) or {})
    except ValueError as e:
        return _err(str(e))
    res = lyr.fetch_lyrics(track)
    return jsonify(
        {
            "found": res.found, "source": res.source, "synced": bool(res.synced),
            "line_count": len(res.plain), "hook": lyr.pick_hook(res.plain) if res.plain else [],
        }
    )


@bp.post("/steps/image")
def step_image():
    body = request.get_json(silent=True) or {}
    try:
        track = _track_from({"artist": "-", **body})
    except ValueError as e:
        return _err(str(e))
    out_dir = Path(_cfg().OUTPUT_DIR) / "_steps"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{slugify(track.title)}.jpg"
    info = imagegen.generate_image(track, SongRequest.from_dict(body), dest, body.get("aspect", "portrait"), _cfg())
    return jsonify({"provider": info["provider"], "prompt": info["prompt"],
                    "url": url_for("api.job_file", job_id="_steps", name=dest.name)})


@bp.get("/steps/tags")
def step_tags():
    """Trending hashtags. ?refresh=1 skips the cache; shows per-provider errors for debugging."""
    limit = int(request.args.get("limit", 15))
    return jsonify(tags_mod.get_trending_tags(_cfg(), limit=limit, use_cache=request.args.get("refresh") != "1"))


@bp.post("/steps/metadata")
def step_metadata():
    body = request.get_json(silent=True) or {}
    try:
        track = _track_from(body)
    except ValueError as e:
        return _err(str(e))
    return jsonify(metadata.generate_metadata(track, SongRequest.from_dict(body), _cfg(), body.get("aspect", "portrait")))
