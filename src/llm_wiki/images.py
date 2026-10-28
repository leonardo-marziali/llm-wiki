"""Image triage: size check and perceptual hash, before any LLM sees the image."""

import io
import tempfile
from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image, ImageOps

from .errors import RefusedSource

Image.MAX_IMAGE_PIXELS = 200_000_000
MIN_SIDE = 32
VISION_MAX_SIDE = 1568  # longer sides are downscaled by the API anyway
VISION_MAX_BYTES = 3_750_000  # base64 inflates by 4/3 against the API's 5 MB image cap


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


# Modes PNG can store; anything else (CMYK, YCbCr, LAB, F, ...) is converted first.
_PNG_MODES = frozenset({"RGB", "RGBA", "L", "LA", "P", "1", "I", "I;16"})


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info


def _png_ready(img: Image.Image) -> Image.Image:
    if img.mode in _PNG_MODES:
        return img
    return img.convert("RGBA" if _has_alpha(img) else "RGB")


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _jpeg_bytes(img: Image.Image) -> bytes:
    flat = img
    if _has_alpha(img):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
    elif img.mode != "RGB":
        flat = img.convert("RGB")
    buf = io.BytesIO()
    flat.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def prepare_for_vision(src: Path, out_dir: Path) -> Path:
    """A copy of `src` the model can take: long side <= 1568 px, PNG, or JPEG if big.

    The result is a new file in `out_dir`; `src` is untouched.
    """
    try:
        with Image.open(src) as opened:
            img = ImageOps.exif_transpose(opened)  # the first frame, upright
            img.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise RefusedSource(f"unreadable image: {src.name}") from exc
    img = _png_ready(img)
    if max(img.size) > VISION_MAX_SIDE:
        img.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE), Image.Resampling.LANCZOS)
    data, suffix = _png_bytes(img), ".png"
    while len(data) > VISION_MAX_BYTES:
        data, suffix = _jpeg_bytes(img), ".jpg"
        if len(data) <= VISION_MAX_BYTES or min(img.size) <= MIN_SIDE:
            break
        img = img.resize((img.width * 3 // 4, img.height * 3 // 4))
    fd, name = tempfile.mkstemp(dir=out_dir, prefix="img-", suffix=suffix)
    out = Path(name)
    with open(fd, "wb") as fh:
        fh.write(data)
    return out
