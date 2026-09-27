"""Sized WebP variants of recipe photos (KAN-271).

Recipe photos are stored as the generator produced them: 1408×768 JPEG/PNG at
0.9–2.0 MB. Served as-is to a phone that paints the hero at ~400 CSS px, that
was the whole mobile LCP budget several times over (Lighthouse 7.8–7.9 s on
``/browse`` and ``/r/<slug>``). A resized WebP at quality 80 is typically
5–15× smaller for no visible loss at the rendered size.

Variants are produced on demand by the image route and cached; nothing is
written back to storage, so there is no migration and no backfill.
"""

import io
import logging

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# The only widths the image route will produce. A fixed allow-list keeps the
# cache keyspace bounded and stops ``?w=`` from being used to make the server
# resize to arbitrary sizes.
VARIANT_WIDTHS: tuple[int, ...] = (400, 800, 1200)

WEBP_QUALITY = 80


def parse_variant_width(raw: str | None) -> int | None:
    """``None`` when absent; the width when allow-listed; ``ValueError`` otherwise."""
    if raw is None:
        return None
    try:
        width = int(raw)
    except ValueError:
        raise ValueError(f"unsupported width: {raw!r}") from None
    if width not in VARIANT_WIDTHS:
        raise ValueError(f"unsupported width: {width}")
    return width


def make_webp_variant(image_bytes: bytes, width: int) -> bytes | None:
    """Encode ``image_bytes`` as WebP, downscaled to at most ``width`` pixels wide.

    Never upscales: a source narrower than ``width`` is re-encoded at its own
    size (still much smaller than the JPEG/PNG original). Returns ``None`` when
    the bytes cannot be decoded, so the caller can fall back to serving the
    original rather than failing the request.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode not in ("RGB", "RGBA"):
                has_alpha = image.mode in ("LA", "PA") or (
                    image.mode == "P" and "transparency" in image.info
                )
                image = image.convert("RGBA" if has_alpha else "RGB")
            if image.width > width:
                height = max(1, round(image.height * width / image.width))
                image = image.resize((width, height), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        # OSError covers PIL.UnidentifiedImageError and truncated files.
        logger.warning("Could not build %dw image variant: %s", width, exc)
        return None
