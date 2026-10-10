"""Song selection algorithm.

Inputs (SongRequest): mood, genre, language, era, energy, keyword, artist.
Steps: build search queries -> query every enabled provider -> drop duplicates and
songs already used -> score each candidate -> return a ranked list.
"""
from __future__ import annotations

import random
import re

from . import music
from .models import SongRequest, Track
from .utils import log

ANY = {"", "any", "none", "random"}

MOOD_TERMS = {
    "happy": ["happy", "feel good", "sunshine"],
    "sad": ["sad", "heartbreak", "tears"],
    "romantic": ["love", "romantic", "kiss"],
    "chill": ["chill", "relax", "lofi"],
    "energetic": ["party", "dance", "energy"],
    "motivational": ["motivation", "rise up", "winner"],
    "nostalgic": ["nostalgia", "memories", "throwback"],
    "dark": ["dark", "midnight", "shadow"],
    "focus": ["focus", "ambient", "study"],
}
ENERGY_TERMS = {"low": ["acoustic", "slow"], "medium": [], "high": ["upbeat", "dance"]}


def is_any(value: str | None) -> bool:
    return (value or "").strip().lower() in ANY


def parse_era(era: str | None):
    """'90s' -> (1990,1999); '2010s' -> (2010,2019); '2015-2022' -> (2015,2022)."""
    e = (era or "").strip().lower()
    if e in ANY:
        return None
    m = re.search(r"(\d{4})\s*(?:-|–|to)\s*(\d{4})", e)
    if m:
        return int(m[1]), int(m[2])
    m = re.fullmatch(r"(\d{4})s", e)
    if m:
        y = int(m[1])
        return y, y + 9
    m = re.fullmatch(r"(\d{2})s", e)
    if m:
        n = int(m[1])
        y = 1900 + n if n >= 30 else 2000 + n
        return y, y + 9
    m = re.fullmatch(r"(\d{4})", e)
    if m:
        return int(m[1]), int(m[1])
    return None


def build_queries(req: SongRequest) -> list[str]:
    mood = (req.mood or "").strip().lower()
    mood_terms = MOOD_TERMS.get(mood) or ([mood] if not is_any(mood) else [])
    energy_terms = ENERGY_TERMS.get((req.energy or "").strip().lower(), [])
    lang = "" if is_any(req.language) or req.language.strip().lower() == "english" else req.language.strip()
    genre = "" if is_any(req.genre) else req.genre.strip()
    kw = (req.keyword or "").strip()
    artist = (req.artist or "").strip()

    def join(*parts):
        words, seen = [], set()
        for w in " ".join(p for p in parts if p).split():
            if w.lower() not in seen:  # avoid "lofi lofi"
                seen.add(w.lower())
                words.append(w)
        return " ".join(words)

    queries: list[str] = []
    if artist:
        queries.append(join(artist, kw))
    if kw:
        queries.append(join(kw, genre, lang))
    for term in mood_terms[:3]:
        queries.append(join(genre, lang, term))
    if genre or lang:
        queries.append(join(genre, lang, *(energy_terms[:1])))
    if not queries:
        queries = random.sample(["top hits", "indie pop", "chill vibes", "dance hits"], 2)

    seen, out = set(), []
    for q in queries:
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").lower()))


def score_track(track: Track, req: SongRequest) -> float:
    score = 0.0
    era = parse_era(req.era)
    kw_tokens = _tokens(req.keyword)
    artist_tokens = _tokens(req.artist)
    genre = "" if is_any(req.genre) else req.genre.lower()

    score += 3.0 * len(artist_tokens & _tokens(track.artist))
    score += 1.5 * len(kw_tokens & _tokens(track.title))
    score += 0.7 * len(kw_tokens & _tokens(track.album))
    if era and track.year:
        score += 2.0 if era[0] <= track.year <= era[1] else -3.0
    if genre and genre in track.genre.lower():
        score += 2.0
    if track.artwork_url:
        score += 0.5
    if not track.explicit:
        score += 0.5
    if track.full_audio:
        score += 0.5  # full track lets us sync lyrics to the real timeline
    score += random.uniform(0, 1.5)  # jitter: different pick each run
    return score


def rank_tracks(req: SongRequest, cfg, history: set[str] | None = None) -> list[Track]:
    history = history or set()
    providers = music.get_providers(cfg, req.royalty_free_only)
    pool: dict[tuple[str, str], Track] = {}
    errors: dict[str, str] = {}
    attempts = 0

    for query in build_queries(req):
        for provider in providers:
            attempts += 1
            try:
                found = provider.search(query, req, limit=25)
            except Exception as exc:  # free APIs fail sometimes; keep going
                log.warning("%s search failed for %r: %s", provider.name, query, exc)
                errors[provider.name] = type(exc).__name__
                continue
            for t in found:
                if not t.audio_url or t.key() in history:
                    continue
                pool.setdefault((t.title.lower(), t.artist.lower()), t)
        if len(pool) >= 40:
            break

    if not pool and errors and len(errors) == len(providers):
        raise RuntimeError(
            "Every music provider failed (" + ", ".join(f"{k}: {v}" for k, v in errors.items())
            + "). Check your internet connection / API access."
        )

    tracks = list(pool.values())
    for t in tracks:
        t.score = round(score_track(t, req), 3)
    tracks.sort(key=lambda t: t.score, reverse=True)
    return tracks
