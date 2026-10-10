"""Check each trending-tag provider on its own and show exactly what worked / failed.

    python scripts/check_tags.py            # all configured providers
    python scripts/check_tags.py trendgetter

Bright Data calls spend free credits (limited by BRIGHTDATA_LIMIT_PER_INPUT).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.modules import tags


def main():
    names = sys.argv[1:] or [p for p in Config.TRENDING_PROVIDERS if tags._configured(p, Config)]
    for name in names:
        print(f"\n== {name} ==")
        if name not in tags.PROVIDERS or not tags._configured(name, Config):
            print("not configured")
            continue
        try:
            found = tags.PROVIDERS[name](Config, 20)
            print(f"{len(found)} tags:", ", ".join(found))
        except Exception as exc:
            print("FAILED:", exc)

    print("\n== combined (cache bypassed) ==")
    res = tags.get_trending_tags(Config, limit=15, use_cache=False)
    print(res["source"], "->", ", ".join(res["tags"]))
    if res["errors"]:
        print("errors:", res["errors"])


if __name__ == "__main__":
    main()
