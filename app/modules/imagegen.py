"""AI artwork for the video. Provider chain (configurable): pollinations -> huggingface -> procedural.
The procedural fallback needs no network, so the pipeline never dies on an image failure."""
from __future__ import annotations

import hashlib
import io
import random
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFilter

from .models import SongRequest, Track
from .utils import http, log

# final size, generation size (multiples of 16 for diffusion models)
ASPECTS = {
    "portrait": {"size": (1080, 1350), "gen": (1024, 1280)},  # Instagram feed 4:5
    "square": {"size": (1080, 1080), "gen": (1024, 1024)},
    "story": {"size": (1080, 1920), "gen": (720, 1280)},      # Reels / Shorts / Stories 9:16
}

MOOD_STYLE = {
    "happy": "warm golden hour light, vibrant pastel colors, joyful",
    "sad": "rainy blue tones, melancholic, soft fog, moody",
    "romantic": "soft pink and red glow, dreamy bokeh, intimate",
    "chill": "calm sunset gradient, lofi aesthetic, cozy, soft grain",
    "energetic": "neon colors, dynamic motion, vivid contrast, electric",
    "motivational": "epic sunrise over mountains, powerful, dramatic sky",
    "nostalgic": "retro film look, faded warm colors, vintage",
    "dark": "dark cinematic, deep shadows, moody purple and teal",
    "focus": "minimal abstract shapes, soft gradients, clean calm",
}

MOOD_PALETTE = {
    "happy": [(255, 190, 80), (255, 120, 120), (255, 230, 150)],
    "sad": [(40, 70, 130), (90, 110, 170), (20, 30, 60)],
    "romantic": [(220, 80, 120), (255, 150, 170), (110, 30, 70)],
    "chill": [(90, 140, 200), (240, 150, 120), (120, 90, 160)],
    "energetic": [(255, 40, 140), (40, 220, 255), (255, 220, 40)],
    "motivational": [(255, 140, 40), (200, 60, 40), (40, 40, 90)],
    "nostalgic": [(200, 150, 100), (150, 110, 90), (230, 200, 160)],
    "dark": [(60, 20, 100), (20, 90, 110), (10, 10, 30)],
    "focus": [(120, 160, 190), (200, 210, 220), (70, 90, 120)],
}


def fit_cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize + center-crop to exactly `size` (CSS object-fit: cover)."""
    tw, th = size
    scale = max(tw / img.width, th / img.height)
    resized = img.resize((max(tw, round(img.width * scale)), max(th, round(img.height * scale))), Image.LANCZOS)
    left, top = (resized.width - tw) // 2, (resized.height - th) // 2
    return resized.crop((left, top, left + tw, top + th))


def build_prompt(track: Track, req: SongRequest | None, extra: str = "") -> str:
    mood = (req.mood if req else "").lower()
    style = MOOD_STYLE.get(mood, "cinematic, atmospheric, rich colors, soft lighting")
    genre = track.genre or (req.genre if req and req.genre.lower() not in ("", "any") else "music")
    return (
        f"Abstract cinematic artwork, {style}, {genre} vibe, inspired by the theme of "
        f"'{track.title}'. {extra} Vertical composition, highly detailed, no text, no letters, "
        f"no logos, no watermark, no faces."
    ).strip()


def _pollinations(prompt: str, gen_size, seed: int, cfg) -> Image.Image:
    headers = {"Authorization": f"Bearer {cfg.POLLINATIONS_TOKEN}"} if cfg.POLLINATIONS_TOKEN else {}
    r = http().get(
        "https://image.pollinations.ai/prompt/" + quote(prompt, safe=""),
        params={"width": gen_size[0], "height": gen_size[1], "seed": seed, "nologo": "true", "model": "flux"},
        headers=headers,
        timeout=cfg.IMAGE_TIMEOUT,
    )
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        raise RuntimeError(f"pollinations returned {r.status_code} {r.headers.get('content-type')}")
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _huggingface(prompt: str, gen_size, seed: int, cfg) -> Image.Image:
    if not cfg.HF_TOKEN:
        raise RuntimeError("HF_TOKEN not set")
    r = http().post(
        f"https://router.huggingface.co/hf-inference/models/{cfg.HF_IMAGE_MODEL}",
        headers={"Authorization": f"Bearer {cfg.HF_TOKEN}"},
        json={"inputs": prompt, "parameters": {"width": gen_size[0], "height": gen_size[1], "seed": seed}},
        timeout=cfg.IMAGE_TIMEOUT,
    )
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        raise RuntimeError(f"huggingface returned {r.status_code}: {r.text[:150]}")
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def procedural_image(title: str, mood: str, size: tuple[int, int]) -> Image.Image:
    """Offline gradient + blurred color blobs, seeded by the title so it's stable per song."""
    rng = random.Random(int(hashlib.md5(title.encode()).hexdigest(), 16))
    palette = MOOD_PALETTE.get((mood or "").lower()) or [
        tuple(rng.randint(40, 255) for _ in range(3)) for _ in range(3)
    ]
    w, h = size
    base = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(base)
    c1, c2 = palette[0], palette[-1]
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
    blobs = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    for _ in range(7):
        r = rng.randint(int(w * 0.18), int(w * 0.42))
        x, y = rng.randint(0, w), rng.randint(0, h)
        col = rng.choice(palette) + (rng.randint(110, 190),)
        bd.ellipse((x - r, y - r, x + r, y + r), fill=col)
    blobs = blobs.filter(ImageFilter.GaussianBlur(w * 0.09))
    base.paste(blobs, (0, 0), blobs)
    return base


def generate_image(track: Track, req: SongRequest | None, dest: Path, aspect: str, cfg, extra: str = "") -> dict:
    spec = ASPECTS.get(aspect, ASPECTS["portrait"])
    prompt = build_prompt(track, req, extra)
    seed = random.randint(1, 10_000_000)
    img, used = None, "procedural"

    for name in cfg.IMAGE_PROVIDERS:
        try:
            if name == "pollinations":
                img = _pollinations(prompt, spec["gen"], seed, cfg)
            elif name == "huggingface":
                img = _huggingface(prompt, spec["gen"], seed, cfg)
            elif name == "procedural":
                img = procedural_image(track.title, (req.mood if req else ""), spec["size"])
            else:
                continue
            used = name
            break
        except Exception as exc:
            log.warning("image provider %s failed: %s", name, exc)

    if img is None:  # chain exhausted -> guarantee an image
        img = procedural_image(track.title, (req.mood if req else ""), spec["size"])
        used = "procedural"

    fit_cover(img, spec["size"]).save(dest, "JPEG", quality=95)
    return {"path": str(dest), "provider": used, "prompt": prompt, "size": spec["size"]}
