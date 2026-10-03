"""media.py — helpers for classifying and storing uploaded attachments."""

from pathlib import Path

from .config import MEDIA_KIND_BY_EXT


def classify_media(filename: str) -> str:
    """Return 'image' | 'audio' | 'video' | 'document' based on extension."""
    ext = Path(filename).suffix.lower()
    for kind, exts in MEDIA_KIND_BY_EXT.items():
        if ext in exts:
            return kind
    return "document"


def is_allowed_image_ext(filename: str) -> bool:
    return Path(filename).suffix.lower() in MEDIA_KIND_BY_EXT["image"]
