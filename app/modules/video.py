"""Lyric-video renderer: slow zoom on the artwork + karaoke-style animated lyrics.

Frames are drawn with Pillow and piped straight into ffmpeg (no temp frame files).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

from .imagegen import fit_cover
from .models import LyricLine
from .utils import ffmpeg_bin, ffmpeg_launch_error, find_font, log, probe_duration

ACCENT = (255, 214, 102)
WHITE = (255, 255, 255)


def _ease_out(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def _font(path: str | None, size: int) -> ImageFont.FreeTypeFont:
    try:
        if path:
            return ImageFont.truetype(path, size)
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _fit_text(text: str, font, max_w: float) -> str:
    while len(text) > 3 and font.getlength(text) > max_w:
        text = text[:-2].rstrip() + "…"
    return text


def _make_overlay(W, H, title, artist, small) -> Image.Image:
    """Static layer: darkening gradient (strong at top/bottom) + song credit."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for y in range(H):
        k = abs(2 * y / H - 1) ** 2
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(95 + 120 * k)))
    x = int(W * 0.08)
    d.text((x, int(H * 0.885)), _fit_text(title, small, W * 0.84), font=small, fill=(255, 255, 255, 235))
    d.text((x, int(H * 0.92)), _fit_text(artist, small, W * 0.84), font=small, fill=(255, 255, 255, 150))
    return ov


def _layout(text: str, font, max_w: float):
    """Wrap into rows of (word, width); returns rows and per-row pixel widths."""
    space = font.getlength(" ")
    rows, cur, cur_w = [], [], 0.0
    for w in text.split():
        ww = font.getlength(w)
        add = ww if not cur else space + ww
        if cur and cur_w + add > max_w:
            rows.append((cur, cur_w))
            cur, cur_w = [(w, ww)], ww
        else:
            cur.append((w, ww))
            cur_w += add
    if cur:
        rows.append((cur, cur_w))
    return rows, space


def render_video(
    image_path: str | Path,
    audio_path: str | Path,
    lines: list[LyricLine],
    out_path: str | Path,
    size: tuple[int, int] = (1080, 1350),
    fps: int = 24,
    title: str = "",
    artist: str = "",
    font_path: str = "",
    progress_cb: Callable[[float], None] | None = None,
) -> dict:
    W, H = size
    duration = probe_duration(audio_path)
    n_frames = int(duration * fps)

    zoom = 1.10
    big = fit_cover(Image.open(image_path).convert("RGB"), (int(W * zoom), int(H * zoom)))
    fp = find_font(font_path)
    if not fp:
        log.warning("No TTF font found; using Pillow default. Put a font in assets/fonts/ for best results.")
    lyric_font = _font(fp, int(W * 0.072))
    small = _font(fp, int(W * 0.032))
    line_h = int(lyric_font.size * 1.38)
    overlay = _make_overlay(W, H, title, artist, small)

    # pre-compute wrapped layouts + per-word timing weights
    layouts = []
    for ln in lines:
        rows, space = _layout(ln.text, lyric_font, W * 0.82)
        words = [w for row, _ in rows for w, _ in row]
        total = sum(len(w) for w in words) or 1
        cum, acc = [], 0
        for w in words:
            cum.append((acc / total, (acc + len(w)) / total))
            acc += len(w)
        layouts.append((rows, space, cum))

    cmd = [
        ffmpeg_bin(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
        "-i", str(audio_path),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.3f}", "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise RuntimeError(ffmpeg_launch_error(exc, cmd[0])) from exc
    black = Image.new("RGB", (W, H), (0, 0, 0))

    try:
        for i in range(n_frames):
            t = i / fps
            p = t / duration

            # slow Ken-Burns zoom: window shrinks from full image to W x H
            scale = 1 + (zoom - 1) * p
            ww, wh = big.width / scale, big.height / scale
            l, tp = (big.width - ww) / 2, (big.height - wh) / 2
            frame = big.resize((W, H), Image.BILINEAR, box=(l, tp, l + ww, tp + wh)).convert("RGBA")
            frame.alpha_composite(overlay)

            # active lyric line
            for ln, (rows, space, cum) in zip(lines, layouts):
                if not (ln.start <= t < ln.end):
                    continue
                span = max(0.3, ln.end - ln.start)
                fade_in = _ease_out((t - ln.start) / 0.35)
                fade_out = min(1.0, (ln.end - t) / 0.3)
                a = max(0.0, min(fade_in, fade_out))
                rise = (1 - fade_in) * 40
                reveal = max(0.0, min(1.0, (t - ln.start) / (0.8 * span)))

                layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                d = ImageDraw.Draw(layer)
                y = H * 0.5 - (len(rows) * line_h) / 2 + rise
                wi = 0
                for row, row_w in rows:
                    x = (W - row_w) / 2
                    for word, wwid in row:
                        c0, c1 = cum[wi]
                        if reveal >= c1:
                            color, alpha = WHITE, a
                        elif reveal >= c0:
                            color, alpha = ACCENT, a
                        else:
                            color, alpha = WHITE, a * 0.42
                        d.text(
                            (x, y), word, font=lyric_font,
                            fill=color + (int(255 * alpha),),
                            stroke_width=max(2, W // 400),
                            stroke_fill=(0, 0, 0, int(190 * alpha)),
                        )
                        x += wwid + space
                        wi += 1
                    y += line_h
                frame.alpha_composite(layer)
                break

            # thin progress bar
            bar = ImageDraw.Draw(frame)
            bx0, bx1, by = int(W * 0.08), int(W * 0.92), int(H * 0.965)
            bar.rectangle((bx0, by, bx1, by + 4), fill=(95, 95, 108, 255))  # opaque: ImageDraw doesn't blend alpha
            bar.rectangle((bx0, by, bx0 + int((bx1 - bx0) * p), by + 4), fill=ACCENT + (255,))

            rgb = frame.convert("RGB")
            f = min(1.0, t / 0.6, (duration - t) / 0.8)  # fade in / out of the whole video
            if f < 1.0:
                rgb = Image.blend(black, rgb, max(0.0, f))
            proc.stdin.write(rgb.tobytes())

            if progress_cb and i % fps == 0:
                progress_cb(i / n_frames)

        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed ({rc}): {err[:400]}")
    return {"path": str(out_path), "duration": duration, "frames": n_frames, "fps": fps, "size": size}
