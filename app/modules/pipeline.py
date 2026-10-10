"""End-to-end orchestration: song -> lyrics -> audio clip -> artwork -> video -> metadata."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import imagegen, lyrics as lyr, metadata, music, selector, video
from .models import SongRequest
from .utils import log, probe_duration

Progress = Callable[[str, float, str], None]


class PipelineError(Exception):
    pass


def _history_path(cfg) -> Path:
    return Path(cfg.OUTPUT_DIR) / "history.json"


def load_history(cfg) -> set[str]:
    try:
        return set(json.loads(_history_path(cfg).read_text()))
    except Exception:
        return set()


def save_history(cfg, key: str) -> None:
    hist = load_history(cfg)
    hist.add(key)
    _history_path(cfg).write_text(json.dumps(sorted(hist)))


def run_pipeline(params: dict, cfg, out_dir: Path, progress: Progress | None = None) -> dict:
    progress = progress or (lambda *_: None)
    req = SongRequest.from_dict(params)
    aspect = params.get("aspect") if params.get("aspect") in imagegen.ASPECTS else "portrait"
    try:
        clip_seconds = max(10, min(60, int(params.get("clip_seconds") or cfg.CLIP_SECONDS)))
    except (TypeError, ValueError):
        clip_seconds = cfg.CLIP_SECONDS
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) pick a song -------------------------------------------------------
    progress("select_song", 0.03, "Searching and ranking songs")
    ranked = selector.rank_tracks(req, cfg, load_history(cfg))
    if not ranked:
        raise PipelineError("No songs found for these inputs. Try a broader mood, genre or keyword.")

    # 2) lyrics (try a few ranked songs until one has lyrics) --------------
    progress("lyrics", 0.12, "Fetching lyrics")
    chosen, lyrics_res = None, lyr.LyricsResult()
    for t in ranked[: cfg.MAX_SONG_TRIES]:
        res = lyr.fetch_lyrics(t)
        if res.found and not res.instrumental:
            chosen, lyrics_res = t, res
            break
    if chosen is None:
        chosen = ranked[0]
        log.warning("No lyrics found for top %d songs; continuing with a title-card video", cfg.MAX_SONG_TRIES)

    # 3) audio clip ----------------------------------------------------------
    progress("audio", 0.2, f"Downloading audio: {chosen.title} – {chosen.artist}")
    raw = out_dir / "source_audio"
    music.download_audio(chosen, raw)
    raw_dur = probe_duration(raw)
    clip_len = min(float(clip_seconds), raw_dur)
    start, lines, sync_mode = 0.0, [], "none"

    if chosen.full_audio and lyrics_res.synced:
        start = min(lyr.choose_start(lyrics_res.synced), max(0.0, raw_dur - clip_len))
        lines = lyr.window_synced(lyrics_res.synced, start, clip_len)
        sync_mode = "synced" if lines else "none"

    clip = out_dir / "clip.mp3"
    music.prepare_clip(raw, clip, start, clip_len)
    raw.unlink(missing_ok=True)
    clip_dur = probe_duration(clip)

    if not lines and lyrics_res.plain:
        # 30s previews start at an unknown point of the song, so timestamps can't be aligned:
        # show the song's hook lines evenly spaced across the clip instead.
        lines = lyr.schedule_even(lyr.pick_hook(lyrics_res.plain), clip_dur)
        sync_mode = "evenly_spaced"

    # 4) artwork ---------------------------------------------------------------
    progress("image", 0.3, "Generating artwork")
    image_info = imagegen.generate_image(chosen, req, out_dir / "image.jpg", aspect, cfg)

    # 5) video -----------------------------------------------------------------
    progress("video", 0.4, "Rendering lyric video")
    video_info = video.render_video(
        image_path=image_info["path"],
        audio_path=clip,
        lines=lines,
        out_path=out_dir / "video.mp4",
        size=tuple(image_info["size"]),
        fps=cfg.FPS,
        title=chosen.title,
        artist=chosen.artist,
        font_path=cfg.FONT_PATH,
        progress_cb=lambda f: progress("video", 0.4 + 0.5 * f, f"Rendering video {int(f * 100)}%"),
    )

    # 6) title / description / tags ---------------------------------------------
    progress("metadata", 0.93, "Writing title, description and tags")
    meta = metadata.generate_metadata(chosen, req, cfg, aspect)
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "request": req.to_dict(),
        "aspect": aspect,
        "track": chosen.to_dict(),
        "lyrics": {"source": lyrics_res.source, "sync_mode": sync_mode, "lines_shown": len(lines)},
        "audio": {"start_offset": round(start, 2), "duration": round(clip_dur, 2), "full_track": chosen.full_audio},
        "image": {k: image_info[k] for k in ("provider", "prompt")},
        "video": video_info,
        "metadata": meta,
        "files": {"video": "video.mp4", "image": "image.jpg", "audio": "clip.mp3", "metadata": "metadata.json"},
        "status": "done",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    save_history(cfg, chosen.key())
    progress("done", 1.0, "Finished")
    return manifest
