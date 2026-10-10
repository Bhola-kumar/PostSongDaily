"""Offline smoke test: no network, no API keys.

Builds a synthetic audio clip, a procedural image and ORIGINAL placeholder lyrics, renders the
lyric video, and runs the metadata fallback. Run:  python scripts/smoke_test.py
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.modules import imagegen, lyrics as lyr, metadata, selector, video
from app.modules.models import SongRequest, Track
from app.modules.utils import ffmpeg_bin, probe_duration

DEMO_LYRICS = [
    "Lights are low and the city hums",
    "We keep chasing where the night comes from",
    "Hold on tight we are going out",
    "Hold on tight we are going out",
    "Every road leads back to you",
    "Every road leads back to you",
    "Hold on tight we are going out",
    "Sing it loud till the morning comes",
]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vg-smoke-"))
    cfg = Config
    cfg.GEMINI_API_KEY = ""          # force template metadata
    cfg.TRENDING_TAGS_URL = ""
    cfg.OUTPUT_DIR = tmp

    # selector pieces (pure logic)
    req = SongRequest(mood="chill", genre="lofi", era="2010s", keyword="rain")
    print("queries:", selector.build_queries(req))
    assert selector.parse_era("90s") == (1990, 1999) and selector.parse_era("2015-2022") == (2015, 2022)

    # lyrics helpers
    hook = lyr.pick_hook(DEMO_LYRICS, 4)
    print("hook:", hook)
    lrc = lyr.parse_lrc("[00:01.50] one\n[00:04.00] two\n[00:09.25] three")
    assert [round(l.start, 2) for l in lrc] == [1.5, 4.0, 9.25]

    # synthetic audio (12s tone)
    audio = tmp / "clip.mp3"
    subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=12",
                    "-c:a", "libmp3lame", str(audio)], check=True)
    dur = probe_duration(audio)

    track = Track(provider="demo", track_id="1", title="Night Drive Demo", artist="Test Artist", genre="lofi")
    img = tmp / "image.jpg"
    info = imagegen.generate_image(track, req, img, "portrait", type("C", (), {"IMAGE_PROVIDERS": ["procedural"]})())
    lines = lyr.schedule_even(hook, dur)

    t0 = time.time()
    out = video.render_video(img, audio, lines, tmp / "video.mp4", size=info["size"], fps=24,
                             title=track.title, artist=track.artist)
    print(f"rendered {out['frames']} frames in {time.time() - t0:.1f}s -> {out['path']}")

    meta = metadata.generate_metadata(track, req, cfg, "portrait")
    print(json.dumps(meta, indent=2)[:600])
    print("OK", tmp)


if __name__ == "__main__":
    main()
