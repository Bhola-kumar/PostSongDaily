"""Free music providers. Each returns Track objects with a playable audio_url.

- iTunes Search API: no key, 30s previews
- Deezer API:        no key, 30s previews
- Jamendo:           free client id, FULL-length royalty-free (CC) tracks
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .models import SongRequest, Track
from .utils import ffmpeg_bin, ffmpeg_launch_error, http, log

LANG_COUNTRY = {
    "english": "US", "hindi": "IN", "punjabi": "IN", "tamil": "IN", "telugu": "IN",
    "spanish": "ES", "korean": "KR", "japanese": "JP", "french": "FR",
    "portuguese": "BR", "german": "DE", "italian": "IT", "arabic": "AE",
}


class Provider:
    name = ""

    def __init__(self, cfg):
        self.cfg = cfg

    def enabled(self) -> bool:
        return True

    def search(self, query: str, req: SongRequest, limit: int = 25) -> list[Track]:
        raise NotImplementedError


class ITunes(Provider):
    name = "itunes"

    def search(self, query, req, limit=25):
        country = LANG_COUNTRY.get((req.language or "").lower(), self.cfg.ITUNES_COUNTRY)
        r = http().get(
            "https://itunes.apple.com/search",
            params={"term": query, "media": "music", "entity": "song", "limit": limit, "country": country},
            timeout=20,
        )
        r.raise_for_status()
        out = []
        for it in r.json().get("results", []):
            if not it.get("previewUrl"):
                continue
            year = None
            if it.get("releaseDate"):
                try:
                    year = int(it["releaseDate"][:4])
                except ValueError:
                    pass
            out.append(
                Track(
                    provider=self.name,
                    track_id=str(it.get("trackId")),
                    title=it.get("trackName", ""),
                    artist=it.get("artistName", ""),
                    album=it.get("collectionName", ""),
                    audio_url=it["previewUrl"],
                    artwork_url=(it.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb"),
                    song_duration=(it.get("trackTimeMillis") or 0) / 1000,
                    year=year,
                    genre=it.get("primaryGenreName", ""),
                    explicit=it.get("trackExplicitness") == "explicit",
                    full_audio=False,
                    source_url=it.get("trackViewUrl", ""),
                )
            )
        return out


class Deezer(Provider):
    name = "deezer"

    def search(self, query, req, limit=25):
        r = http().get("https://api.deezer.com/search", params={"q": query, "limit": limit}, timeout=20)
        r.raise_for_status()
        out = []
        for it in r.json().get("data", []):
            if not it.get("preview"):
                continue
            album = it.get("album") or {}
            out.append(
                Track(
                    provider=self.name,
                    track_id=str(it.get("id")),
                    title=it.get("title", ""),
                    artist=(it.get("artist") or {}).get("name", ""),
                    album=album.get("title", ""),
                    audio_url=it["preview"],
                    artwork_url=album.get("cover_xl", ""),
                    song_duration=float(it.get("duration") or 0),
                    explicit=bool(it.get("explicit_lyrics")),
                    full_audio=False,
                    source_url=it.get("link", ""),
                )
            )
        return out


class Jamendo(Provider):
    name = "jamendo"

    def enabled(self):
        return bool(self.cfg.JAMENDO_CLIENT_ID)

    def search(self, query, req, limit=25):
        r = http().get(
            "https://api.jamendo.com/v3.0/tracks/",
            params={
                "client_id": self.cfg.JAMENDO_CLIENT_ID,
                "format": "json",
                "limit": limit,
                "search": query,
                "audioformat": "mp32",
                "order": "popularity_month",
            },
            timeout=20,
        )
        r.raise_for_status()
        out = []
        for it in r.json().get("results", []):
            if not it.get("audio"):
                continue
            year = None
            if it.get("releasedate"):
                try:
                    year = int(it["releasedate"][:4])
                except ValueError:
                    pass
            out.append(
                Track(
                    provider=self.name,
                    track_id=str(it.get("id")),
                    title=it.get("name", ""),
                    artist=it.get("artist_name", ""),
                    album=it.get("album_name", ""),
                    audio_url=it["audio"],
                    artwork_url=it.get("image", ""),
                    song_duration=float(it.get("duration") or 0),
                    year=year,
                    full_audio=True,
                    source_url=it.get("shareurl", ""),
                )
            )
        return out


_REGISTRY = {"itunes": ITunes, "deezer": Deezer, "jamendo": Jamendo}


def get_providers(cfg, royalty_free_only: bool = False) -> list[Provider]:
    names = ["jamendo"] if royalty_free_only else cfg.MUSIC_SOURCES
    providers = [_REGISTRY[n](cfg) for n in names if n in _REGISTRY]
    active = [p for p in providers if p.enabled()]
    if not active:
        raise RuntimeError("No music provider is enabled (set JAMENDO_CLIENT_ID for royalty-free only mode).")
    return active


def download_audio(track: Track, dest: Path) -> Path:
    with http().get(track.audio_url, timeout=90, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
    return dest


def prepare_clip(src: Path, dest: Path, start: float, seconds: float, fade_in: float = 0.8, fade_out: float = 1.5) -> Path:
    """Trim to [start, start+seconds] with fades; re-encode to mp3."""
    fade_out_start = max(0.0, seconds - fade_out)
    af = f"afade=t=in:st=0:d={fade_in},afade=t=out:st={fade_out_start:.2f}:d={fade_out}"
    cmd = [
        ffmpeg_bin(), "-y", "-loglevel", "error",
        "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}", "-i", str(src),
        "-af", af, "-c:a", "libmp3lame", "-b:a", "192k", str(dest),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(ffmpeg_launch_error(exc, cmd[0])) from exc
    if res.returncode != 0:
        log.error("ffmpeg clip failed: %s", res.stderr)
        raise RuntimeError(f"Could not prepare audio clip: {res.stderr.strip()[:300]}")
    return dest
