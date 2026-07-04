"""Removal of Gemini's visible corner watermark via exact reverse alpha blending.

Recent Gemini image models (Nano Banana / Nano Banana Pro = Gemini 3 Pro Image)
stamp a small, semi-transparent four-point *gradient sparkle* into the
bottom-right corner. It is composited over the generated pixels:

    watermarked = alpha * logo + (1 - alpha) * original

where both the per-pixel ``alpha`` and the sparkle's own gradient colour are
fixed. ``assets/sparkle.npz`` holds those two maps — ``alpha`` (H,W) and the
premultiplied colour ``P = alpha * logo`` (H,W,3) — solved from flat-colour
calibration captures. Removal inverts the blend exactly (lossless apart from
8-bit quantisation), recovering the true pixels underneath — no smudge, even
across texture and hard edges:

    original = (watermarked - P) / (1 - alpha)

The sparkle is a FIXED 96x96 bitmap at a FIXED 192px margin from the bottom-right
corner, constant across every Gemini output size observed (2048px–3168px). It
does NOT scale with the image, so a single map positioned by a constant offset
serves all sizes.

This strips only the *visible* logo. It does NOT touch SynthID, the invisible
watermark Gemini embeds during generation.
"""

from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

ASSETS_DIR = Path(__file__).parent / "assets"

LOGO_SIZE = 96   # the sparkle bitmap is a fixed 96x96
LOGO_MARGIN = 192  # fixed gap from the right and bottom edges

_maps: tuple[np.ndarray, np.ndarray] | None = None  # (P, one_minus_alpha)


def _sparkle_maps() -> tuple[np.ndarray, np.ndarray]:
    """Load and cache (premultiplied colour P, 1 - alpha) for the sparkle."""

    global _maps
    if _maps is not None:
        return _maps

    path = ASSETS_DIR / "sparkle.npz"
    if not path.exists():
        raise FileNotFoundError(f"Sparkle map not found: {path}")

    with np.load(path) as data:
        premultiplied = data["P"].astype(np.float32)
        # Cap alpha below 1 so the (1 - alpha) divisor never approaches zero.
        alpha = np.minimum(data["alpha"].astype(np.float32), 0.995)

    _maps = (premultiplied, (1.0 - alpha)[:, :, None])  # broadcast over RGB
    return _maps


def remove_watermark(image: Image.Image) -> Image.Image:
    """Return a copy of ``image`` with the visible corner watermark removed."""

    rgb = image.convert("RGB")
    width, height = rgb.size

    x = width - LOGO_MARGIN - LOGO_SIZE
    y = height - LOGO_MARGIN - LOGO_SIZE
    if x < 0 or y < 0:
        return rgb  # too small to carry the watermark at this offset

    premultiplied, one_minus_alpha = _sparkle_maps()
    pixels = np.asarray(rgb, dtype=np.float32)
    region = pixels[y : y + LOGO_SIZE, x : x + LOGO_SIZE, :]

    recovered = (region - premultiplied) / one_minus_alpha
    pixels[y : y + LOGO_SIZE, x : x + LOGO_SIZE, :] = np.clip(recovered, 0, 255)

    return Image.fromarray(np.rint(pixels).astype(np.uint8))  # (H, W, 3) → RGB


def dewatermark_file(path: str) -> None:
    """Strip the visible watermark from an image file, rewriting it in place.

    The original container format is preserved so the file's mime type and
    extension stay consistent for downstream streaming / base64 encoding.
    """

    with Image.open(path) as src:
        fmt = src.format or "PNG"
        cleaned = remove_watermark(src)

    save_kwargs: dict = {}
    if fmt == "JPEG":
        save_kwargs = {"quality": 95, "optimize": True}
    cleaned.save(path, format=fmt, **save_kwargs)


def dewatermark_bytes(data: bytes, output_format: str = "PNG") -> bytes:
    """Strip the visible watermark from encoded image ``data``; return new bytes."""

    with Image.open(BytesIO(data)) as src:
        cleaned = remove_watermark(src)

    buffer = BytesIO()
    cleaned.save(buffer, format=output_format)
    return buffer.getvalue()
