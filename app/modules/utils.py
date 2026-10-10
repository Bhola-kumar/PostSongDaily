from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("videogen")

_session: requests.Session | None = None


def http() -> requests.Session:
    """Shared requests session with light retry for flaky free APIs."""
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.headers["User-Agent"] = "videogen/0.1 (personal automation tool)"
        _session = s
    return _session


def clean_title(title: str) -> str:
    """'Song (feat. X) - Remastered 2011' -> 'Song' (better lyric lookup)."""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "")
    t = re.sub(r"\s+-\s+.*$", "", t)
    return t.strip() or (title or "").strip()


def primary_artist(artist: str) -> str:
    return re.split(r",|\bfeat\.?\b|\bft\.?\b|\s&\s|\sx\s", artist or "", flags=re.I)[0].strip()


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit] or "untitled"


ROOT = Path(__file__).resolve().parents[2]  # project root (folder containing run.py)
_EXE = ".exe" if os.name == "nt" else ""


def _resolve_tool(name: str, env_var: str) -> str | None:
    """Lookup order: env var (file or folder) -> <project>/ffmpeg[/bin] -> system PATH."""
    env = os.getenv(env_var, "").strip().strip('"')
    if env:
        p = Path(env)
        candidates = [p] if p.is_file() else [p / (name + _EXE), p / name, p / (name + ".exe"), p / "bin" / (name + _EXE)]
        for c in candidates:
            if c.is_file():
                return str(c)
    for folder in (ROOT / "ffmpeg", ROOT / "ffmpeg" / "bin"):
        for c in (folder / (name + _EXE), folder / name, folder / (name + ".exe")):
            if c.is_file():
                return str(c)
    return shutil.which(name)


def ffmpeg_bin() -> str:
    path = _resolve_tool("ffmpeg", "FFMPEG_PATH")
    if path:
        return path
    try:  # optional last resort: pip install imageio-ffmpeg
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            f"ffmpeg not found. Put ffmpeg(.exe) and ffprobe(.exe) in {ROOT / 'ffmpeg'}, "
            "set FFMPEG_PATH in .env, or add ffmpeg to PATH."
        ) from exc


def ffprobe_bin() -> str | None:
    return _resolve_tool("ffprobe", "FFPROBE_PATH")


def ffmpeg_status() -> dict:
    try:
        ff = ffmpeg_bin()
    except RuntimeError:
        ff = None
    error = None
    if ff:
        try:
            subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            error = ffmpeg_launch_error(exc, ff)
            ff = None
    return {"ffmpeg": ff, "ffprobe": ffprobe_bin(), "ffmpeg_error": error}


def ffmpeg_launch_error(exc: OSError | subprocess.SubprocessError, executable: str) -> str:
    """Turn executable launch failures into actionable setup guidance."""
    winerror = getattr(exc, "winerror", None)
    if winerror == 4551:
        return (
            f"Windows Application Control blocked ffmpeg at {executable}. "
            "Use an ffmpeg executable approved by your organization, then set "
            "FFMPEG_PATH in .env to that executable (or its containing folder) and restart the app."
        )
    return f"Could not start ffmpeg at {executable}: {exc}"


def probe_duration(path: str | Path) -> float:
    """Media duration in seconds via ffprobe; falls back to parsing `ffmpeg -i` output."""
    probe = ffprobe_bin()
    if probe:
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise RuntimeError(ffmpeg_launch_error(exc, probe)) from exc
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip())
    ff = ffmpeg_bin()
    try:
        res = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(ffmpeg_launch_error(exc, ff)) from exc
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
    if not m:
        raise RuntimeError(f"Could not read duration of {path}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def find_font(preferred: str = "") -> str | None:
    """Preferred path, then assets/fonts/*.ttf, then common system fonts."""
    if preferred and Path(preferred).exists():
        return preferred
    assets = Path(__file__).resolve().parents[2] / "assets" / "fonts"
    if assets.exists():
        for f in sorted(assets.glob("*.[to]tf")):
            return str(f)
    for c in _FONT_CANDIDATES:
        if Path(c).exists():
            return c
    return None
