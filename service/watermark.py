"""Removal of Gemini's visible corner watermark via reverse alpha blending.

Gemini composites a white logo into the bottom-right corner of generated images:

    watermarked = alpha * logo + (1 - alpha) * original

With the per-pixel ``alpha`` map captured in ``assets/bg_{size}.png`` we invert
that exactly (lossless apart from 8-bit quantisation):

    original = (watermarked - alpha * logo) / (1 - alpha)

This strips only the *visible* logo. It does NOT touch SynthID, the invisible
watermark Gemini embeds during generation.

Ported from https://github.com/VimalMollyn/Gemini-Watermark-Remover-Python
(vectorised here so the whole region is recovered in one numpy operation).
"""

from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

ASSETS_DIR = Path(__file__).parent / "assets"

ALPHA_THRESHOLD = 0.002  # below this the logo is invisible — leave the pixel be
MAX_ALPHA = 0.99  # cap so 1 - alpha never approaches zero
LOGO_VALUE = 255.0  # the watermark logo is white

_alpha_cache: dict[int, np.ndarray] = {}


def _alpha_map(logo_size: int) -> np.ndarray:
    """Per-pixel alpha in [0, 1] for the given logo size, cached after first load."""

    cached = _alpha_cache.get(logo_size)
    if cached is not None:
        return cached

    bg_path = ASSETS_DIR / f"bg_{logo_size}.png"
    if not bg_path.exists():
        raise FileNotFoundError(f"Alpha map not found: {bg_path}")

    with Image.open(bg_path) as bg:
        channels = np.asarray(bg.convert("RGB"), dtype=np.float32)

    alpha = np.max(channels, axis=2) / 255.0
    alpha[alpha < ALPHA_THRESHOLD] = 0.0  # noise floor → identity (1 - 0 = 1)
    np.minimum(alpha, MAX_ALPHA, out=alpha)

    _alpha_cache[logo_size] = alpha
    return alpha


def _logo_config(width: int, height: int) -> tuple[int, int]:
    """Return (logo_size, margin) for the image dimensions, per Gemini's rules."""

    if width > 1024 and height > 1024:
        return 96, 64
    return 48, 32


def remove_watermark(image: Image.Image) -> Image.Image:
    """Return a copy of ``image`` with the visible corner watermark removed."""

    rgb = image.convert("RGB")
    width, height = rgb.size
    logo_size, margin = _logo_config(width, height)

    x = width - margin - logo_size
    y = height - margin - logo_size
    if x < 0 or y < 0:
        return rgb  # too small to carry a watermark

    alpha = _alpha_map(logo_size)[:, :, None]  # (size, size, 1) broadcasts over RGB
    pixels = np.asarray(rgb, dtype=np.float32)
    region = pixels[y : y + logo_size, x : x + logo_size, :]

    recovered = (region - alpha * LOGO_VALUE) / (1.0 - alpha)
    pixels[y : y + logo_size, x : x + logo_size, :] = np.clip(recovered, 0, 255)

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
