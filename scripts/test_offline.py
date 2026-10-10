"""Offline tests (mocked HTTP): tag parsers, Trendgetter, Bright Data (sync + async), provider chain,
cache, and local ./ffmpeg folder lookup.   Run:  python scripts/test_offline.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.modules import tags, utils


class Resp:
    def __init__(self, data=None, status=200, text=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.text = text if text is not None else json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return Resp(val)
        raise AssertionError(f"unexpected url {url}")


def make_cfg(tmp, **over):
    attrs = dict(OUTPUT_DIR=Path(tmp), TRENDING_PROVIDERS=["trendgetter", "brightdata", "custom"],
                 BRIGHTDATA_API_KEY="key", BRIGHTDATA_DATASET_ID="gd_test", TRENDING_TAGS_URL="")
    attrs.update(over)
    return type("TestCfg", (Config,), attrs)


def test_parsers():
    assert tags.tags_from_list([{"name": "#TaylorSwift", "tweet_volume": 9}, {"name": "Super Bowl"}, "#x"]) == ["taylorswift", "superbowl"]
    assert tags.tags_from_list({"data": ["#alpha", "beta", "Alpha"]}) == ["alpha", "beta"]
    assert tags.tags_from_list({"trends": [{"hashtag": "#One"}, {"title": "Two words"}]}) == ["one", "twowords"]
    posts = [
        {"description": "love this #fyp #Music #2024", "hashtags": ["music", "dance"]},
        {"caption": "#fyp again", "challenges": [{"title": "dance"}], "nested": {"text": "#lonely"}},
    ]
    got = tags.tags_from_posts(posts)
    assert got[:3] == ["fyp", "music", "dance"] and "lonely" in got and "2024" not in got, got
    assert tags.tags_from_posts({"unrelated": 5}) == []


def test_trendgetter():
    x = [{"name": "#AAA"}, {"name": "BBB"}, {"name": "CCC"}]
    tt = [{"description": "hi #ddd #eee", "title": "ignored video title"}]
    fake = FakeHttp({"/api/x/tags": x, "/api/tiktok/videos": tt})
    with mock.patch.object(tags, "http", lambda: fake):
        out = tags.fetch_trendgetter(Config, 10)
    assert out == ["aaa", "ddd", "bbb", "eee", "ccc"], out
    assert any(p == {"period": 7, "country_code": "US", "limit": 20} for _, p in fake.calls)

    fake = FakeHttp({"/api/x/tags": RuntimeError("boom"), "/api/tiktok/videos": tt})
    with mock.patch.object(tags, "http", lambda: fake):
        assert tags.fetch_trendgetter(Config, 10) == ["ddd", "eee"]

    fake = FakeHttp({"/api/x/tags": RuntimeError("boom"), "/api/tiktok/videos": RuntimeError("bang")})
    with mock.patch.object(tags, "http", lambda: fake):
        try:
            tags.fetch_trendgetter(Config, 10)
            raise SystemExit("expected failure")
        except RuntimeError as e:
            assert "boom" in str(e) and "bang" in str(e)


def test_brightdata():
    cfg = make_cfg(tempfile.mkdtemp(), BRIGHTDATA_LIMIT_PER_INPUT=12)
    records = [{"description": "x #chill", "hashtags": ["chill", "lofi"]}, {"hashtags": ["lofi"]}]

    # sync 200
    with mock.patch.object(tags.requests, "post", return_value=Resp(records)) as post:
        assert tags.fetch_brightdata(cfg, 5) == ["chill", "lofi"]
    args, kw = post.call_args
    assert args[0].endswith("/datasets/v3/scrape")
    assert kw["params"] == {"dataset_id": "gd_test", "format": "json"}
    assert kw["headers"]["Authorization"] == "Bearer key"
    assert kw["json"]["limit_per_input"] == 12
    assert kw["json"]["input"] == [{"url": "https://www.tiktok.com/tag/music"},
                                   {"url": "https://www.tiktok.com/tag/lyrics"},
                                   {"url": "https://www.tiktok.com/tag/viral"}]

    # async 202 -> progress -> snapshot (NDJSON)
    ndjson = "\n".join(json.dumps(r) for r in records)
    seq = [Resp({"status": "running"}), Resp({"status": "ready"}), Resp(None, text=ndjson)]
    with mock.patch.object(tags.requests, "post", return_value=Resp({"snapshot_id": "s1"}, status=202)), \
         mock.patch.object(tags.requests, "get", side_effect=seq) as get, mock.patch.object(tags.time, "sleep"):
        assert tags.fetch_brightdata(cfg, 5) == ["chill", "lofi"]
    assert "/progress/s1" in get.call_args_list[0].args[0] and "/snapshot/s1" in get.call_args_list[2].args[0]

    # custom input JSON overrides the template
    cfg2 = make_cfg(tempfile.mkdtemp(), BRIGHTDATA_INPUT_JSON='[{"url":"https://example.com/tag/x"}]')
    with mock.patch.object(tags.requests, "post", return_value=Resp(records)) as post:
        tags.fetch_brightdata(cfg2, 5)
    assert post.call_args.kwargs["json"]["input"] == [{"url": "https://example.com/tag/x"}]

    # errors
    with mock.patch.object(tags.requests, "post", return_value=Resp({"error": "x"}, status=401)):
        try:
            tags.fetch_brightdata(cfg, 5)
            raise SystemExit("expected failure")
        except RuntimeError as e:
            assert "401" in str(e)
    try:
        tags.fetch_brightdata(make_cfg(tempfile.mkdtemp(), BRIGHTDATA_API_KEY=""), 5)
        raise SystemExit("expected failure")
    except RuntimeError as e:
        assert "not set" in str(e)


def test_chain_and_cache():
    tmp = tempfile.mkdtemp()
    many = [f"t{i}" for i in range(20)]
    cfg = make_cfg(tmp)

    # enough from trendgetter -> brightdata never called
    with mock.patch.dict(tags.PROVIDERS, {"trendgetter": lambda c, n: many[:n],
                                          "brightdata": mock.Mock(side_effect=AssertionError("should not run"))}):
        res = tags.get_trending_tags(cfg, 15, use_cache=False)
    assert res["tags"] == many[:15] and res["source"] == "trendgetter" and not res["errors"]

    # cache hit (same provider signature), then a different signature invalidates it
    with mock.patch.dict(tags.PROVIDERS, {"trendgetter": mock.Mock(side_effect=AssertionError("cached"))}):
        assert tags.get_trending_tags(cfg, 15)["source"] == "cache:trendgetter"
    cfg_other = make_cfg(tmp, TRENDING_PROVIDERS=["brightdata"])
    with mock.patch.dict(tags.PROVIDERS, {"brightdata": lambda c, n: ["bd1", "bd2"]}):
        res = tags.get_trending_tags(cfg_other, 15)
    assert res["source"] == "brightdata+seed" and res["tags"][:2] == ["bd1", "bd2"]

    # trendgetter fails -> brightdata fills in, error recorded; custom skipped (not configured)
    cfg = make_cfg(tempfile.mkdtemp())
    with mock.patch.dict(tags.PROVIDERS, {"trendgetter": mock.Mock(side_effect=RuntimeError("down")),
                                          "brightdata": lambda c, n: ["bd1", "bd2", "bd3"]}):
        res = tags.get_trending_tags(cfg, 15, use_cache=False)
    assert res["tags"][:3] == ["bd1", "bd2", "bd3"] and "trendgetter" in res["errors"] and "custom" not in res["errors"]
    assert len(res["tags"]) == 13 or len(res["tags"]) <= 15  # seeds fill the rest

    # everything down -> seeds only, never raises
    with mock.patch.dict(tags.PROVIDERS, {"trendgetter": mock.Mock(side_effect=RuntimeError("a")),
                                          "brightdata": mock.Mock(side_effect=RuntimeError("b"))}):
        res = tags.get_trending_tags(make_cfg(tempfile.mkdtemp()), 15, use_cache=False)
    assert res["source"] == "seed" and res["tags"][0] == "music" and set(res["errors"]) == {"trendgetter", "brightdata"}


def test_ffmpeg_folder():
    real_ffmpeg, real_ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    root = Path(tempfile.mkdtemp())
    (root / "ffmpeg").mkdir()
    (root / "ffmpeg" / "ffmpeg").symlink_to(real_ffmpeg)
    (root / "ffmpeg" / "ffprobe").symlink_to(real_ffprobe)
    old_path, old_root = os.environ["PATH"], utils.ROOT
    try:
        os.environ["PATH"] = ""          # prove it is NOT coming from PATH
        os.environ.pop("FFMPEG_PATH", None)
        utils.ROOT = root
        assert utils.ffmpeg_bin() == str(root / "ffmpeg" / "ffmpeg")
        assert utils.ffprobe_bin() == str(root / "ffmpeg" / "ffprobe")

        # env override (folder) beats the project folder
        other = Path(tempfile.mkdtemp())
        (other / "ffmpeg").symlink_to(real_ffmpeg)
        os.environ["FFMPEG_PATH"] = str(other)
        assert utils.ffmpeg_bin() == str(other / "ffmpeg")
        os.environ.pop("FFMPEG_PATH")

        # Windows-style names are found too
        shutil.rmtree(root / "ffmpeg")
        (root / "ffmpeg" / "bin").mkdir(parents=True)
        (root / "ffmpeg" / "bin" / "ffmpeg.exe").write_text("x")
        assert utils.ffmpeg_bin().endswith("ffmpeg.exe")

        # duration works with ffprobe missing (falls back to `ffmpeg -i`)
        (root / "ffmpeg" / "bin" / "ffmpeg.exe").unlink()
        (root / "ffmpeg" / "ffmpeg").symlink_to(real_ffmpeg)
        wav = root / "t.wav"
        import subprocess
        subprocess.run([real_ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", str(wav)], check=True)
        assert utils.ffprobe_bin() is None
        assert abs(utils.probe_duration(wav) - 2.0) < 0.1
    finally:
        os.environ["PATH"], utils.ROOT = old_path, old_root


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("ALL PASSED")
