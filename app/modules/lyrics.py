"""Lyrics from free sources (LRCLIB first, lyrics.ovh as fallback) plus helpers that
turn lyrics into timed on-screen lines."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .models import LyricLine, Track
from .utils import clean_title, http, log, primary_artist

LRC_RE = re.compile(r"\[(\d{1,3}):(\d{2}(?:[.:]\d{1,3})?)\]")


@dataclass
class LyricsResult:
    source: str = ""
    synced: list[LyricLine] = field(default_factory=list)
    plain: list[str] = field(default_factory=list)
    instrumental: bool = False

    @property
    def found(self) -> bool:
        return bool(self.synced or self.plain)


def _norm(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def parse_lrc(text: str) -> list[LyricLine]:
    out: list[LyricLine] = []
    for raw in text.splitlines():
        stamps = LRC_RE.findall(raw)
        body = LRC_RE.sub("", raw).strip()
        if not stamps or not body:
            continue
        for mm, ss in stamps:
            out.append(LyricLine(text=body, start=int(mm) * 60 + float(ss.replace(":", "."))))
    out.sort(key=lambda l: l.start)
    for i, line in enumerate(out):
        nxt = out[i + 1].start if i + 1 < len(out) else line.start + 4
        line.end = min(nxt - 0.05, line.start + 7)
    return out


def _plain_from_text(text: str) -> list[str]:
    lines = [l.strip() for l in (text or "").splitlines()]
    if lines and lines[0].lower().startswith("paroles de la chanson"):
        lines = lines[1:]
    return [l for l in lines if l]


def _from_lrclib_item(item: dict) -> LyricsResult:
    res = LyricsResult(source="lrclib", instrumental=bool(item.get("instrumental")))
    if item.get("syncedLyrics"):
        res.synced = parse_lrc(item["syncedLyrics"])
        res.plain = [l.text for l in res.synced]
    elif item.get("plainLyrics"):
        res.plain = _plain_from_text(item["plainLyrics"])
    return res


def _lrclib(track: Track) -> LyricsResult | None:
    title, artist = clean_title(track.title), primary_artist(track.artist)
    params = {"track_name": title, "artist_name": artist}
    if track.album:
        params["album_name"] = track.album
    if track.song_duration:
        params["duration"] = int(round(track.song_duration))
    r = http().get("https://lrclib.net/api/get", params=params, timeout=20)
    if r.status_code == 200:
        res = _from_lrclib_item(r.json())
        if res.found:
            return res

    r = http().get(
        "https://lrclib.net/api/search", params={"track_name": title, "artist_name": artist}, timeout=20
    )
    if r.status_code != 200:
        return None
    items = r.json() or []

    def rank(it):
        d = abs((it.get("duration") or 0) - track.song_duration) if track.song_duration else 0
        return (0 if it.get("syncedLyrics") else 1, d)

    for it in sorted((i for i in items if i.get("syncedLyrics") or i.get("plainLyrics")), key=rank):
        res = _from_lrclib_item(it)
        if res.found:
            return res
    return None


def _lyrics_ovh(track: Track) -> LyricsResult | None:
    from urllib.parse import quote

    url = f"https://api.lyrics.ovh/v1/{quote(primary_artist(track.artist))}/{quote(clean_title(track.title))}"
    r = http().get(url, timeout=20)
    if r.status_code != 200:
        return None
    plain = _plain_from_text(r.json().get("lyrics", ""))
    return LyricsResult(source="lyrics.ovh", plain=plain) if plain else None


def fetch_lyrics(track: Track) -> LyricsResult:
    for fn in (_lrclib, _lyrics_ovh):
        try:
            res = fn(track)
            if res and res.found:
                return res
        except Exception as exc:
            log.warning("lyrics source %s failed: %s", fn.__name__, exc)
    return LyricsResult()


# ---------- turning lyrics into timed lines ----------

def pick_hook(lines: list[str], max_lines: int = 8) -> list[str]:
    """Most repeated lines (usually the chorus) in order of first appearance;
    falls back to a block from the first third of the song."""
    cleaned = [
        l.strip() for l in lines if l.strip() and not re.fullmatch(r"[\[\(].*?[\]\)]", l.strip())
    ]
    counts = Counter(_norm(l) for l in cleaned if len(_norm(l)) > 3)
    top = {n for n, c in counts.most_common(max_lines) if c >= 2}

    chosen, seen = [], set()
    for l in cleaned:
        n = _norm(l)
        if n in top and n not in seen:
            chosen.append(l)
            seen.add(n)
        if len(chosen) >= max_lines:
            break
    if len(chosen) < min(4, max_lines):
        for l in cleaned[len(cleaned) // 3:]:
            n = _norm(l)
            if n in seen:
                continue
            chosen.append(l)
            seen.add(n)
            if len(chosen) >= max_lines:
                break
    return chosen


def schedule_even(texts: list[str], duration: float, lead: float = 1.0, tail: float = 1.2,
                  min_per_line: float = 2.4) -> list[LyricLine]:
    """Spread lines evenly over the clip (used when real timestamps can't be aligned)."""
    span = max(1.0, duration - lead - tail)
    texts = texts[: max(1, int(span / min_per_line))]
    per = span / len(texts)
    return [
        LyricLine(text=t, start=lead + i * per, end=lead + (i + 1) * per - 0.05)
        for i, t in enumerate(texts)
    ]


def choose_start(synced: list[LyricLine]) -> float:
    """For full-length audio: start ~1s before the first occurrence of the most repeated line."""
    counts = Counter(_norm(l.text) for l in synced if len(_norm(l.text)) > 3)
    if not counts or counts.most_common(1)[0][1] < 2:
        return max(0.0, synced[len(synced) // 3].start - 1.0) if synced else 0.0
    best = counts.most_common(1)[0][0]
    first = next(l.start for l in synced if _norm(l.text) == best)
    return max(0.0, first - 1.0)


def window_synced(synced: list[LyricLine], start: float, seconds: float) -> list[LyricLine]:
    out = []
    for l in synced:
        if l.start >= start and l.start < start + seconds - 0.5:
            out.append(
                LyricLine(text=l.text, start=l.start - start, end=min(l.end, start + seconds) - start)
            )
    return out
