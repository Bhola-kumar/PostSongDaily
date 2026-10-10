import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # dotenv is optional
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _list(name: str, default: str) -> list[str]:
    return [s.strip().lower() for s in _env(name, default).split(",") if s.strip()]


class Config:
    BASE_DIR = BASE_DIR
    OUTPUT_DIR = Path(_env("OUTPUT_DIR") or BASE_DIR / "outputs")
    if not OUTPUT_DIR.is_absolute():
        OUTPUT_DIR = BASE_DIR / OUTPUT_DIR

    DAILY_LIMIT = _int("DAILY_LIMIT", 6)
    MAX_BATCH_SIZE = _int("MAX_BATCH_SIZE", 5)
    API_KEY = _env("API_KEY")
    CLIP_SECONDS = _int("CLIP_SECONDS", 30)
    FPS = _int("FPS", 24)
    MAX_SONG_TRIES = _int("MAX_SONG_TRIES", 4)
    FONT_PATH = _env("FONT_PATH")

    MUSIC_SOURCES = _list("MUSIC_SOURCES", "itunes,deezer,jamendo")
    ITUNES_COUNTRY = _env("ITUNES_COUNTRY", "US")
    JAMENDO_CLIENT_ID = _env("JAMENDO_CLIENT_ID")

    IMAGE_PROVIDERS = _list("IMAGE_PROVIDERS", "pollinations,huggingface,procedural")
    POLLINATIONS_TOKEN = _env("POLLINATIONS_TOKEN")
    HF_TOKEN = _env("HF_TOKEN")
    HF_IMAGE_MODEL = _env("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
    IMAGE_TIMEOUT = _int("IMAGE_TIMEOUT", 120)

    GEMINI_API_KEY = _env("GEMINI_API_KEY")
    GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.8-flash")

    # ---- trending hashtags: providers are tried in order until enough tags are collected ----
    TRENDING_PROVIDERS = _list("TRENDING_PROVIDERS", "trendgetter,brightdata,custom")
    TRENDING_CACHE_HOURS = _int("TRENDING_CACHE_HOURS", 12)

    TRENDGETTER_BASE_URL = _env("TRENDGETTER_BASE_URL", "https://trendgetter.vercel.app")
    TRENDGETTER_COUNTRY = _env("TRENDGETTER_COUNTRY", "US")   # TikTok country_code
    TRENDGETTER_X_LOCATION = _env("TRENDGETTER_X_LOCATION", "")  # X trends location, empty = worldwide

    BRIGHTDATA_API_KEY = _env("BRIGHTDATA_API_KEY")
    BRIGHTDATA_DATASET_ID = _env("BRIGHTDATA_DATASET_ID")     # hashtag scraper id from your Bright Data dashboard
    BRIGHTDATA_INPUT_JSON = _env("BRIGHTDATA_INPUT_JSON")     # optional: full input array, overrides the template below
    BRIGHTDATA_SEED_TAGS = _list("BRIGHTDATA_SEED_TAGS", "music,lyrics,viral")
    BRIGHTDATA_URL_TEMPLATE = _env("BRIGHTDATA_URL_TEMPLATE", "https://www.tiktok.com/tag/{tag}")
    BRIGHTDATA_LIMIT_PER_INPUT = _int("BRIGHTDATA_LIMIT_PER_INPUT", 15)  # keeps free credits (5K/mo) safe
    BRIGHTDATA_MAX_WAIT = _int("BRIGHTDATA_MAX_WAIT", 120)

    # generic "your own endpoint" provider (kept from v1)
    TRENDING_TAGS_URL = _env("TRENDING_TAGS_URL")
    TRENDING_TAGS_API_KEY = _env("TRENDING_TAGS_API_KEY")
    TRENDING_TAGS_AUTH_HEADER = _env("TRENDING_TAGS_AUTH_HEADER", "x-api-key")

    # optional explicit ffmpeg locations (default: ./ffmpeg folder, then PATH)
    FFMPEG_PATH = _env("FFMPEG_PATH")
    FFPROBE_PATH = _env("FFPROBE_PATH")
