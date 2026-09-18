"""
CandidateProfiler — Image Preprocessor
Enforces global image size limits before any image is sent to an API.
Handles: PNG/JPEG resize, PDF text extraction with image fallback, base64 encoding.

GLOBAL LIMITS:
  - Max width:     4096 px
  - Max height:    4096 px
  - Max file size: 5 MB
"""

import io
import os
import base64
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# ── Global limits ───────────────────────────────────────────────────────────
MAX_WIDTH = 4096
MAX_HEIGHT = 4096
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

# Screenshot / web-capture target resolution
SCREENSHOT_MAX_WIDTH = 1920
SCREENSHOT_MAX_HEIGHT = 1080

# PDF rasterization DPI (used only as fallback when text extraction fails)
PDF_FALLBACK_DPI = 72  # keep low to stay within pixel limits

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}


# ── Core resize logic ──────────────────────────────────────────────────────

def resize_image_to_fit(
    img: Image.Image,
    max_width: int = MAX_WIDTH,
    max_height: int = MAX_HEIGHT,
) -> Image.Image:
    """
    Resize a PIL Image to fit within max_width × max_height,
    preserving aspect ratio.  Returns the original if already compliant.
    """
    w, h = img.size
    if w <= max_width and h <= max_height:
        return img

    ratio = min(max_width / w, max_height / h)
    new_w = int(w * ratio)
    new_h = int(h * ratio)

    logger.info(f"Resizing image from {w}×{h} → {new_w}×{new_h} (ratio={ratio:.4f})")
    return img.resize((new_w, new_h), Image.LANCZOS)


# ── Bytes-level preprocessing ──────────────────────────────────────────────

def preprocess_image_bytes(
    image_bytes: bytes,
    max_width: int = MAX_WIDTH,
    max_height: int = MAX_HEIGHT,
    max_file_size: int = MAX_FILE_SIZE_BYTES,
    output_format: str = "PNG",
) -> bytes:
    """
    Validate and resize raw image bytes.
    Returns compliant image bytes (PNG or JPEG).
    """
    img = Image.open(io.BytesIO(image_bytes))

    # Convert palette / RGBA for JPEG output
    if output_format.upper() == "JPEG" and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    img = resize_image_to_fit(img, max_width, max_height)

    # Encode
    buf = io.BytesIO()
    img.save(buf, format=output_format)
    result = buf.getvalue()

    # If still over the file-size limit, re-encode as JPEG with quality reduction
    if len(result) > max_file_size:
        logger.warning(f"Image {len(result)} bytes > {max_file_size} limit, compressing as JPEG")
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        for quality in (85, 70, 50, 30):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            result = buf.getvalue()
            if len(result) <= max_file_size:
                break

    if len(result) > max_file_size:
        logger.warning(f"Image still {len(result)} bytes after compression — further resize")
        shrink = (max_file_size / len(result)) ** 0.5
        new_size = (int(img.size[0] * shrink), int(img.size[1] * shrink))
        img = img.resize(new_size, Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        result = buf.getvalue()

    logger.info(f"Preprocessed image: {img.size[0]}×{img.size[1]}, {len(result)/1024:.0f} KB")
    return result


# ── Base64 preprocessing ──────────────────────────────────────────────────

def preprocess_base64_image(
    b64_data: str,
    max_width: int = MAX_WIDTH,
    max_height: int = MAX_HEIGHT,
) -> str:
    """
    Decode a base64 image string, resize if needed, re-encode to base64.
    """
    raw = base64.b64decode(b64_data)
    processed = preprocess_image_bytes(raw, max_width, max_height)
    return base64.b64encode(processed).decode("ascii")


# ── PDF handling ───────────────────────────────────────────────────────────

def extract_pdf_text_or_constrained_images(
    file_bytes: bytes,
    prefer_text: bool = True,
) -> list[dict]:
    """
    For PDFs, prefer text extraction.  Only rasterize a page as a
    constrained-resolution image if it yields no text.

    Returns a list of dicts:
      [{"type": "text", "content": "..."}, ...]
      or
      [{"type": "image", "content": <bytes>, "page": N}, ...]
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    results = []

    for page_num, page in enumerate(doc):
        text = page.get_text().strip()

        if text and prefer_text:
            results.append({"type": "text", "content": text, "page": page_num})
        else:
            # Rasterize at a safe DPI that keeps dimensions under the limit
            rect = page.rect
            w_pts, h_pts = rect.width, rect.height

            # Calculate the max DPI that keeps both dimensions under MAX
            max_dpi_w = MAX_WIDTH / (w_pts / 72)
            max_dpi_h = MAX_HEIGHT / (h_pts / 72)
            safe_dpi = min(PDF_FALLBACK_DPI, max_dpi_w, max_dpi_h)
            zoom = safe_dpi / 72

            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")

            # Double-check with PIL and resize if still over
            img_bytes = preprocess_image_bytes(img_bytes)

            results.append({"type": "image", "content": img_bytes, "page": page_num})
            logger.info(
                f"PDF page {page_num}: rasterized at {safe_dpi:.0f} DPI "
                f"(original {w_pts:.0f}×{h_pts:.0f} pts)"
            )

    doc.close()
    return results


# ── Screenshot / web capture helper ────────────────────────────────────────

def preprocess_screenshot(image_bytes: bytes) -> bytes:
    """
    Resize a screenshot or web-page capture to max 1920×1080,
    then enforce the global 4096 / 5 MB limits.
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = resize_image_to_fit(img, SCREENSHOT_MAX_WIDTH, SCREENSHOT_MAX_HEIGHT)

    buf = io.BytesIO()
    fmt = "PNG" if img.mode == "RGBA" else "JPEG"
    img.save(buf, format=fmt, quality=85)
    result = buf.getvalue()

    # Final pass through the global limiter
    if len(result) > MAX_FILE_SIZE_BYTES:
        result = preprocess_image_bytes(result)

    return result


# ── Batch directory processing ─────────────────────────────────────────────

def process_all_images_in_directory(
    directory: str,
    max_width: int = MAX_WIDTH,
    max_height: int = MAX_HEIGHT,
    backup: bool = True,
) -> dict:
    """
    Scan a directory for image files and resize any that exceed limits.
    Creates a .bak backup of originals if backup=True.
    Returns a summary dict.
    """
    summary = {"scanned": 0, "resized": 0, "skipped": 0, "errors": []}
    dir_path = Path(directory)

    for f in dir_path.iterdir():
        if f.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            continue
        if f.name.endswith(".bak"):
            continue

        summary["scanned"] += 1

        try:
            img = Image.open(f)
            w, h = img.size

            if w <= max_width and h <= max_height:
                file_size = f.stat().st_size
                if file_size <= MAX_FILE_SIZE_BYTES:
                    summary["skipped"] += 1
                    logger.info(f"  ✅ {f.name}: {w}×{h}, {file_size/1024:.0f} KB — OK")
                    continue

            # Resize needed
            logger.info(f"  🔧 {f.name}: {w}×{h} — resizing...")

            if backup:
                bak_path = f.with_suffix(f.suffix + ".bak")
                if not bak_path.exists():
                    import shutil
                    shutil.copy2(f, bak_path)
                    logger.info(f"     Backup → {bak_path.name}")

            raw = f.read_bytes()
            out_format = "JPEG" if f.suffix.lower() in (".jpg", ".jpeg") else "PNG"
            processed = preprocess_image_bytes(raw, max_width, max_height, output_format=out_format)

            f.write_bytes(processed)
            new_img = Image.open(io.BytesIO(processed))
            logger.info(f"     ✅ Resized → {new_img.size[0]}×{new_img.size[1]}, {len(processed)/1024:.0f} KB")
            summary["resized"] += 1

        except Exception as e:
            logger.error(f"  ❌ {f.name}: {e}")
            summary["errors"].append({"file": f.name, "error": str(e)})

    return summary


# ── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    print(f"\n🔍 Scanning images in: {os.path.abspath(target_dir)}")
    print(f"   Limits: {MAX_WIDTH}×{MAX_HEIGHT} px, {MAX_FILE_SIZE_BYTES/1024/1024:.0f} MB\n")

    result = process_all_images_in_directory(target_dir)

    print(f"\n{'='*50}")
    print(f"  Scanned: {result['scanned']}")
    print(f"  Resized: {result['resized']}")
    print(f"  Already OK: {result['skipped']}")
    if result['errors']:
        print(f"  Errors: {len(result['errors'])}")
        for err in result['errors']:
            print(f"    - {err['file']}: {err['error']}")
    print(f"{'='*50}\n")
