"""Trending hashtags, independent of the song.

Providers (order set by TRENDING_PROVIDERS, default trendgetter,brightdata,custom). Tags are
collected provider by provider and the chain stops as soon as `limit` tags are found, so the
paid/limited Bright Data credits are only spent when Trendgetter alone isn't enough.

- trendgetter : free public API (https://github.com/Zivsteve/trendgetter-api)
                /api/x/tags (X trending hashtags) + /api/tiktok/videos (hashtags found in trending videos)
- brightdata  : Web Scraper API (POST /datasets/v3/scrape, async fallback via snapshot). The hashtag
                scrapers return posts, so hashtags are counted across the returned records.
- custom      : any endpoint returning JSON via TRENDING_TAGS_URL
- seed        : static list, always used to fill the remainder

Results are cached (TRENDING_CACHE_HOURS) so a couple of videos per day cost almost nothing.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

import requests

from .utils import http, log

SEED_TAGS = ["music", "lyrics", "lyricvideo", "songoftheday", "vibes", "musiclovers", "viral", "trending", "reels", "shorts"]

HASHTAG_RE = re.compile(r"#(\w{2,40})", re.UNICODE)
LIST_KEYS = ("tags", "hashtags", "data", "results", "items", "trending", "trends", "videos", "posts")
TAG_KEYS = ("tag", "hashtag", "hashtag_name", "name", "title", "keyword", "topic", "query")
POST_TAG_KEYS = {"hashtags", "hashtag", "challenges", "hashtag_names", "tags", "tag_list", "text_extra"}
BD_BASE = "https://api.brightdata.com/datasets/v3"


# ---------------------------------------------------------------- parsing helpers

def normalize(tag) -> str:
    t = re.sub(r"[^\w]", "", str(tag).strip().lstrip("#").lower())
    return t if 2 <= len(t) <= 30 and not t.isdigit() else ""


def tags_from_list(payload) -> list[str]:
    """Endpoints that return a list of tags/topics (strings or objects)."""
    while isinstance(payload, dict):
        for k in LIST_KEYS:
            if k in payload:
                payload = payload[k]
                break
        else:
            break
    out: list[str] = []
    if isinstance(payload, list):
        for it in payload:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                for k in TAG_KEYS:
                    if isinstance(it.get(k), str) and it[k].strip():
                        out.append(it[k])
                        break
    seen, clean = set(), []
    for t in map(normalize, out):
        if t and t not in seen:
            seen.add(t)
            clean.append(t)
    return clean


def tags_from_posts(payload) -> list[str]:
    """Endpoints that return posts/videos: count hashtags in explicit hashtag fields and in any
    text (#word). Most frequent first."""
    counter: Counter = Counter()
    order: dict[str, int] = {}

    def add(raw):
        n = normalize(raw)
        if n:
            counter[n] += 1
            order.setdefault(n, len(order))

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k).lower())
        elif isinstance(node, list):
            for v in node:
                if key in POST_TAG_KEYS and isinstance(v, str):
                    add(v)
                elif key in POST_TAG_KEYS and isinstance(v, dict):
                    for tk in ("name", "title", "tag", "hashtag_name", "hashtag"):
                        if isinstance(v.get(tk), str):
                            add(v[tk])
                            break
                else:
                    walk(v, key)
        elif isinstance(node, str):
            for m in HASHTAG_RE.findall(node):
                add(m)

    walk(payload)
    return [t for t, _ in sorted(counter.items(), key=lambda kv: (-kv[1], order[kv[0]]))]


def _interleave(groups: list[list[str]], limit: int) -> list[str]:
    out, seen = [], set()
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i < len(g) and g[i] not in seen:
                seen.add(g[i])
                out.append(g[i])
    return out[:limit]


# ---------------------------------------------------------------- providers

def fetch_trendgetter(cfg, limit: int) -> list[str]:
    base = cfg.TRENDGETTER_BASE_URL.rstrip("/")
    x_params = {"location": cfg.TRENDGETTER_X_LOCATION} if cfg.TRENDGETTER_X_LOCATION else {}
    calls = [
        ("x/tags", f"{base}/api/x/tags", x_params, lambda p: tags_from_list(p) or tags_from_posts(p)),
        ("tiktok/videos", f"{base}/api/tiktok/videos",
         {"period": 7, "country_code": cfg.TRENDGETTER_COUNTRY, "limit": 20}, tags_from_posts),
    ]
    groups, errors = [], []
    for name, url, params, parser in calls:
        try:
            r = http().get(url, params=params, timeout=30)
            r.raise_for_status()
            found = parser(r.json())
            if not found:
                raise ValueError("no hashtags found in response")
            groups.append(found)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if not groups:
        raise RuntimeError("; ".join(errors))
    return _interleave(groups, limit)


def _records(text: str) -> list:
    text = (text or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:  # NDJSON
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows


def _bd_inputs(cfg) -> list[dict]:
    if cfg.BRIGHTDATA_INPUT_JSON:
        data = json.loads(cfg.BRIGHTDATA_INPUT_JSON)
        if isinstance(data, dict):
            data = data.get("input", [data])
        return data
    return [{"url": cfg.BRIGHTDATA_URL_TEMPLATE.format(tag=t.lstrip("#"))} for t in cfg.BRIGHTDATA_SEED_TAGS]


def _bd_wait_snapshot(snapshot_id: str, headers: dict, cfg) -> list:
    deadline = time.time() + cfg.BRIGHTDATA_MAX_WAIT
    while time.time() < deadline:
        p = requests.get(f"{BD_BASE}/progress/{snapshot_id}", headers=headers, timeout=30)
        p.raise_for_status()
        status = p.json().get("status")
        if status == "ready":
            s = requests.get(f"{BD_BASE}/snapshot/{snapshot_id}", params={"format": "json"}, headers=headers, timeout=60)
            s.raise_for_status()
            return _records(s.text)
        if status == "failed":
            raise RuntimeError("Bright Data job failed")
        time.sleep(8)
    raise RuntimeError(f"Bright Data job not ready after {cfg.BRIGHTDATA_MAX_WAIT}s (snapshot {snapshot_id})")


def fetch_brightdata(cfg, limit: int) -> list[str]:
    if not (cfg.BRIGHTDATA_API_KEY and cfg.BRIGHTDATA_DATASET_ID):
        raise RuntimeError("BRIGHTDATA_API_KEY / BRIGHTDATA_DATASET_ID not set")
    headers = {"Authorization": f"Bearer {cfg.BRIGHTDATA_API_KEY}", "Content-Type": "application/json"}
    body: dict = {"input": _bd_inputs(cfg)}
    if cfg.BRIGHTDATA_LIMIT_PER_INPUT > 0:
        body["limit_per_input"] = cfg.BRIGHTDATA_LIMIT_PER_INPUT
    # plain requests (no auto-retry) so a failed call can't be billed twice
    r = requests.post(
        f"{BD_BASE}/scrape", params={"dataset_id": cfg.BRIGHTDATA_DATASET_ID, "format": "json"},
        headers=headers, json=body, timeout=95,
    )
    if r.status_code == 202:
        records = _bd_wait_snapshot(r.json()["snapshot_id"], headers, cfg)
    elif r.status_code == 200:
        records = _records(r.text)
    else:
        raise RuntimeError(f"Bright Data HTTP {r.status_code}: {r.text[:200]}")
    tags = tags_from_posts(records)
    if not tags:
        raise RuntimeError(f"Bright Data returned {len(records)} record(s) but no hashtags were found in them")
    return tags[:limit]


def fetch_custom(cfg, limit: int) -> list[str]:
    headers = {cfg.TRENDING_TAGS_AUTH_HEADER: cfg.TRENDING_TAGS_API_KEY} if cfg.TRENDING_TAGS_API_KEY else {}
    r = http().get(cfg.TRENDING_TAGS_URL, headers=headers, timeout=25)
    r.raise_for_status()
    payload = r.json()
    tags = tags_from_list(payload) or tags_from_posts(payload)
    if not tags:
        raise RuntimeError("no hashtags found in response")
    return tags[:limit]


PROVIDERS = {"trendgetter": fetch_trendgetter, "brightdata": fetch_brightdata, "custom": fetch_custom}


def _configured(name: str, cfg) -> bool:
    if name == "brightdata":
        return bool(cfg.BRIGHTDATA_API_KEY and cfg.BRIGHTDATA_DATASET_ID)
    if name == "custom":
        return bool(cfg.TRENDING_TAGS_URL)
    return name in PROVIDERS


# ---------------------------------------------------------------- public API

def get_trending_tags(cfg, limit: int = 15, use_cache: bool = True) -> dict:
    """Returns {"tags": [...], "source": "trendgetter+seed", "errors": {provider: message}}."""
    signature = ",".join(p for p in cfg.TRENDING_PROVIDERS if _configured(p, cfg))
    cache = Path(cfg.OUTPUT_DIR) / ".cache" / "trending.json"
    ttl = cfg.TRENDING_CACHE_HOURS * 3600
    if use_cache:
        try:
            if cache.exists() and time.time() - cache.stat().st_mtime < ttl:
                cached = json.loads(cache.read_text())
                if cached.get("signature") == signature:
                    return {"tags": cached["tags"][:limit], "source": "cache:" + cached.get("source", ""), "errors": {}}
        except Exception:
            pass

    tags: list[str] = []
    used: list[str] = []
    errors: dict[str, str] = {}
    for name in cfg.TRENDING_PROVIDERS:
        if len(tags) >= limit:
            break
        if name not in PROVIDERS or not _configured(name, cfg):
            continue
        try:
            found = PROVIDERS[name](cfg, limit)
        except Exception as exc:
            errors[name] = str(exc)[:250]
            log.warning("trending provider %s failed: %s", name, exc)
            continue
        used.append(name)
        for t in found:
            if t not in tags:
                tags.append(t)

    live = list(tags)
    for t in SEED_TAGS:  # fill the remainder
        if t not in tags:
            tags.append(t)
    source = "+".join(used + (["seed"] if len(live) < limit else [])) or "seed"

    if live:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"tags": tags, "source": source, "signature": signature}))
        except Exception:
            pass
    return {"tags": tags[:limit], "source": source, "errors": errors}
