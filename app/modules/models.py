from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Optional


@dataclass
class SongRequest:
    """Inputs that drive the song-selection algorithm."""

    mood: str = "any"          # happy, sad, romantic, chill, energetic, motivational, nostalgic, dark, focus
    genre: str = "any"         # pop, rock, hip hop, lofi, edm, ...
    language: str = "english"  # english, hindi, spanish, korean, ...
    era: str = "any"           # "any", "90s", "2010s", "2015-2022"
    energy: str = "any"        # low | medium | high | any
    keyword: str = ""          # free text: theme / lyric word / title fragment
    artist: str = ""           # optional artist preference
    royalty_free_only: bool = False  # only use Jamendo (CC / royalty-free) tracks

    @classmethod
    def from_dict(cls, data: dict | None) -> "SongRequest":
        names = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in names and v is not None}
        return cls(**clean)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Track:
    provider: str
    track_id: str
    title: str
    artist: str
    album: str = ""
    audio_url: str = ""
    artwork_url: str = ""
    song_duration: float = 0.0   # full song length in seconds (from provider metadata)
    year: Optional[int] = None
    genre: str = ""
    explicit: bool = False
    full_audio: bool = False     # True: audio_url is the whole song; False: ~30s preview
    source_url: str = ""
    score: float = 0.0

    def key(self) -> str:
        return f"{self.provider}:{self.track_id}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LyricLine:
    text: str
    start: float = 0.0
    end: float = 0.0
