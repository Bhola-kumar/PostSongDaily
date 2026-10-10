"""Title / description / tags via Gemini (REST), merged with trending tags.
Falls back to a template when no key is set or the call fails, so jobs never block on it."""
from __future__ import annotations

import json
import re

from .models import SongRequest, Track
from .tags import get_trending_tags
from .utils import http, log

YT_TAG_CHAR_LIMIT = 480  # YouTube allows 500 total; keep headroom


def _gemini(prompt: str, cfg) -> dict:
    if not cfg.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    r = http().post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": cfg.GEMINI_API_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
        },
        timeout=60,
    )
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def _prompt(track: Track, req: SongRequest | None, aspect: str) -> str:
    mood = req.mood if req else "any"
    genre = track.genre or (req.genre if req else "")
    return f"""You write YouTube/Instagram metadata for a short lyric-video clip.
Song: "{track.title}" by {track.artist}. Mood: {mood}. Genre: {genre}. Format: {aspect}.
Return ONLY JSON with keys:
  "title": catchy, under 90 characters, includes song and artist, no clickbait lies,
  "description": 2-3 short engaging sentences + a call to action (like / follow), no lyrics quoted,
  "tags": 12-18 search keywords (no # symbol, lowercase),
  "hashtags": 5-8 hashtags with # symbol.
Do not include any lyrics."""


def _fallback(track: Track, req: SongRequest | None) -> dict:
    mood = (req.mood if req and req.mood.lower() != "any" else "music").title()
    base = [track.title, track.artist, f"{track.title} lyrics", f"{track.artist} songs",
            f"{mood.lower()} songs", f"{mood.lower()} vibes", "lyric video", "music"]
    return {
        "title": f"{track.title} – {track.artist} | {mood} Lyrics Vibes"[:95],
        "description": f"{mood} vibes with \"{track.title}\" by {track.artist}. "
                       f"Turn the sound up and enjoy the lyrics. Like and follow for a new song every day!",
        "tags": [t.lower() for t in base],
        "hashtags": ["#lyrics", "#music", f"#{mood.lower()}vibes", "#songoftheday", "#lyricvideo"],
    }


def _merge_tags(own: list[str], trending: list[str]) -> list[str]:
    out, seen, total = [], set(), 0
    for t in [x.strip().lstrip("#").lower() for x in own + trending]:
        if not t or t in seen:
            continue
        if total + len(t) + 1 > YT_TAG_CHAR_LIMIT:
            break
        seen.add(t)
        out.append(t)
        total += len(t) + 1
    return out


def generate_metadata(track: Track, req: SongRequest | None, cfg, aspect: str = "portrait") -> dict:
    source = "gemini"
    try:
        data = _gemini(_prompt(track, req, aspect), cfg)
        for k in ("title", "description", "tags", "hashtags"):
            if k not in data:
                raise ValueError(f"missing {k}")
    except Exception as exc:
        log.warning("Gemini metadata failed (%s); using template", exc)
        data, source = _fallback(track, req), "template"

    trending = get_trending_tags(cfg, limit=15)
    tags = _merge_tags([str(t) for t in data["tags"]], trending["tags"])
    hashtags = [h if h.startswith("#") else f"#{h}" for h in map(str, data["hashtags"])]
    if aspect == "story" and "#shorts" not in [h.lower() for h in hashtags]:
        hashtags.append("#Shorts")

    credit = f"\n\nMusic: {track.title} – {track.artist}"
    if track.source_url:
        credit += f" ({track.source_url})"
    description = f"{str(data['description']).strip()}{credit}\n\n{' '.join(hashtags)}"

    return {
        "title": str(data["title"]).strip()[:100],
        "description": description[:4900],
        "tags": tags,
        "hashtags": hashtags,
        "trending_source": trending["source"],
        "generated_by": source,
        "model": cfg.GEMINI_MODEL if source == "gemini" else None,
    }
