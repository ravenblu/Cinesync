"""User-adjustable playback settings; FPS belongs to the manifest."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerSettings:
    cache_megabytes: int = 256
    prefetch_frames: int = 30

    def __post_init__(self) -> None:
        for name, minimum in (("cache_megabytes", 1), ("prefetch_frames", 0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
