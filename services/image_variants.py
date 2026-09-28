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

from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

# The only widths the image route will produce. A fixed allow-list keeps the
# cache keyspace bounded and stops ``?w=`` from being used to make the server
# resize to arbitrary sizes.
VARIANT_WIDTHS: tuple[int, ...] = (400, 800, 1200)

WEBP_QUALITY = 80

# Pinterest's recommended pin shape (KAN-284): 2:3 portrait at 1000x1500.
PIN_SIZE: tuple[int, int] = (1000, 1500)
PIN_JPEG_QUALITY = 85
PIN_BACKGROUND_BLUR = 40

# Generated recipe images are currently 1408x768. Keep a generous ceiling for
# legacy sources, but reject oversized headers before Pillow decodes pixel data.
MAX_SOURCE_PIXELS = 25_000_000


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


def parse_pin_flag(raw: str | None) -> bool:
    """``False`` when absent, ``True`` for ``"1"``; ``ValueError`` for anything else."""
    if raw is None:
        return False
    if raw == "1":
        return True
    raise ValueError(f"unsupported pin value: {raw!r}")


def _open_source(image_bytes: bytes) -> Image.Image:
    """Decode ``image_bytes`` upright in RGB/RGBA, refusing oversized sources first.

    Raises ``OSError``/``ValueError``/``DecompressionBombError`` for the
    callers to turn into a ``None`` (fall back to the original bytes).
    """
    with Image.open(io.BytesIO(image_bytes)) as source:
        if source.width * source.height > MAX_SOURCE_PIXELS:
            raise ValueError(
                f"source image exceeds {MAX_SOURCE_PIXELS} pixels: "
                f"{source.width}x{source.height}"
            )
        image = ImageOps.exif_transpose(source)
        if image.mode not in ("RGB", "RGBA"):
            has_alpha = image.mode in ("LA", "PA") or (
                image.mode == "P" and "transparency" in image.info
            )
            image = image.convert("RGBA" if has_alpha else "RGB")
        # exif_transpose can hand back the lazily loaded source itself; load
        # the pixels before the ``with`` closes it.
        image.load()
        return image


def make_webp_variant(image_bytes: bytes, width: int) -> bytes | None:
    """Encode ``image_bytes`` as a WebP whose intrinsic width is exactly ``width``.

    Exact sizing keeps HTML ``srcset`` width descriptors truthful even for a
    narrow legacy source. Returns ``None`` when the bytes cannot be decoded or
    exceed the application pixel limit, so the caller can fall back to serving
    the original rather than failing the request.
    """
    try:
        image = _open_source(image_bytes)
        if image.width != width:
            height = max(1, round(image.height * width / image.width))
            if width * height > MAX_SOURCE_PIXELS:
                raise ValueError(
                    f"output image exceeds {MAX_SOURCE_PIXELS} pixels: {width}x{height}"
                )
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
        return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        # OSError covers PIL.UnidentifiedImageError and truncated files.
        logger.warning("Could not build %dw image variant: %s", width, exc)
        return None


def make_pin_variant(image_bytes: bytes) -> bytes | None:
    """A 1000x1500 (2:3) JPEG of the photo for Pinterest pins (KAN-284).

    Generated photos are 1408x768 landscape or 1024x1024 square. A 2:3 centre
    crop of a landscape source keeps a 512 px sliver of the dish and upscales it
    2x, so instead the whole photo is fitted into the frame and centred on a
    blurred, cover-scaled copy of itself: the familiar "blurred pad" pin shape.
    JPEG has no alpha, so a transparent source is composited onto that
    background rather than onto black.

    Returns ``None`` when the bytes cannot be decoded, like ``make_webp_variant``.
    """
    pin_w, pin_h = PIN_SIZE
    try:
        photo = _open_source(image_bytes).convert("RGBA")
        background_layer = ImageOps.fit(
            photo, PIN_SIZE, Image.Resampling.LANCZOS
        ).filter(ImageFilter.GaussianBlur(PIN_BACKGROUND_BLUR))
        # RGBA -> RGB drops alpha instead of compositing it, which turns fully
        # transparent pixels black. Composite the blurred pad onto an opaque
        # canvas first so transparent sources cannot leak black/hidden RGB.
        background = Image.new("RGB", PIN_SIZE, "white")
        background.paste(background_layer, (0, 0), background_layer)
        scale = min(pin_w / photo.width, pin_h / photo.height)
        fitted_size = (
            max(1, round(photo.width * scale)),
            max(1, round(photo.height * scale)),
        )
        fitted = photo.resize(fitted_size, Image.Resampling.LANCZOS)
        offset = ((pin_w - fitted_size[0]) // 2, (pin_h - fitted_size[1]) // 2)
        background.paste(fitted, offset, fitted)
        out = io.BytesIO()
        background.save(out, format="JPEG", quality=PIN_JPEG_QUALITY, optimize=True)
        return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        logger.warning("Could not build pin image variant: %s", exc)
        return None
