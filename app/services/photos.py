"""Photo normalisation: HEIC -> JPEG, fix rotation, strip EXIF (GPS!), resize, thumbnails."""

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".3gp"}


@dataclass
class ProcessedPhoto:
    rel_path: str
    rel_thumb: str
    width: int
    height: int


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_rgb(src: Path) -> Image.Image:
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)  # apply orientation BEFORE metadata is dropped
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert("RGB")
    return img.convert("RGB")


def _fit(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) <= max_side:
        return img.copy()
    out = img.copy()
    out.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return out


def process_photo(src: Path, data_dir: Path, item_id: int, index: int, max_side: int, thumb_side: int) -> ProcessedPhoto:
    """Writes photos/{item_id}/{index}.jpg and thumb_{index}.jpg. Saved without any EXIF."""
    rel_dir = Path("photos") / str(item_id)
    out_dir = data_dir / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    img = _open_rgb(src)
    main = _fit(img, max_side)
    thumb = _fit(img, thumb_side)

    rel_main = rel_dir / f"{index}.jpg"
    rel_thumb = rel_dir / f"thumb_{index}.jpg"
    # No exif= argument -> Pillow writes no EXIF block, so GPS/device data is gone.
    main.save(data_dir / rel_main, "JPEG", quality=85, optimize=True, progressive=True)
    thumb.save(data_dir / rel_thumb, "JPEG", quality=80, optimize=True)
    return ProcessedPhoto(str(rel_main), str(rel_thumb), main.width, main.height)


def ai_jpeg_bytes(path: Path, max_side: int) -> bytes:
    """Smaller JPEG for Claude vision (long edge <= max_side keeps tokens and latency down)."""
    img = _fit(_open_rgb(path), max_side)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()
