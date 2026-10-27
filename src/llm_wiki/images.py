"""Image triage: size check and perceptual hash, before any LLM sees the image."""

from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image

from .errors import RefusedSource

Image.MAX_IMAGE_PIXELS = 200_000_000
MIN_SIDE = 32


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    phash: str
    reason: str | None


def triage(path: Path) -> ImageInfo:
    try:
        with Image.open(path) as img:
            width, height = img.size
            phash = str(imagehash.phash(img))
    except (OSError, Image.DecompressionBombError) as exc:
        raise RefusedSource(f"unreadable image: {path.name}") from exc
    reason = "image too small" if min(width, height) < MIN_SIDE else None
    return ImageInfo(width, height, phash, reason)


def near_duplicate(
    phash: str, others: dict[str, str], max_distance: int = 5
) -> str | None:
    """Key of the closest entry in `others` within max_distance (Hamming), else None."""
    mine = imagehash.hex_to_hash(phash)
    best: tuple[int, str] | None = None
    for key, other in others.items():
        dist = mine - imagehash.hex_to_hash(other)
        if dist <= max_distance and (best is None or dist < best[0]):
            best = (dist, key)
    return best[1] if best else None
