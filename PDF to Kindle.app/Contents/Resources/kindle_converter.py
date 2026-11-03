from __future__ import annotations

import html
import csv
import io
import math
import os
import sys
import statistics
import json
import urllib.error
import urllib.request
import base64
import hashlib
import ssl
import queue
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw, ImageFilter, ImageStat

APP_NAME = "PDF to Kindle"
LEGACY_APP_NAME = "PDF til Kindle"  # The app's earlier Danish name.
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME


def migrate_legacy_dirs() -> None:
    """Move settings, caches and logs saved under the old Danish app name."""
    library = Path.home() / "Library"
    for parent in (library / "Application Support", library / "Logs"):
        old, new = parent / LEGACY_APP_NAME, parent / APP_NAME
        try:
            if old.is_dir() and not new.exists():
                old.rename(new)
        except OSError:
            pass


VENDOR_DIRS = (
    Path(__file__).resolve().parent / "vendor",
    Path(__file__).resolve().parent / f"{APP_NAME}.app" / "Contents" / "Resources" / "vendor",
)
for vendor_dir in VENDOR_DIRS:
    if vendor_dir.exists() and str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))
try:
    import pymupdf as fitz
except ImportError:  # Native extraction is an optional optimization.
    try:
        import fitz
    except ImportError:
        fitz = None

logger = logging.getLogger("pdf_to_kindle.converter")


@dataclass
class ConversionOptions:
    title: str
    author: str = ""
    language: str = "eng"
    dpi: int = 220
    preserve_visual_pages: bool = True
    ai_cleanup: bool = False
    ai_illustrations: bool = False
    api_key: str = ""
    ai_model: str = ""
    illustration_model: str = "gpt-5.6-luna"
    illustration_retries: int = 2


class ConversionCancelled(Exception):
    """Raised when the user stops an active conversion."""


class AIServiceError(RuntimeError):
    """Raised when OpenAI is unavailable after safe retries."""

    def __init__(self, message, *, retryable=False, status_code=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status_code = status_code


class AIStructuredOutputError(RuntimeError):
    """Raised when a completed OpenAI call cannot be used as structured output."""


LANGUAGE_DATA_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{code}.traineddata"


def _local_tessdata_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path.home() / "Library" / "Application Support" / APP_NAME / "tessdata"
    return Path(__file__).resolve().parent / "tessdata"


def _resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _bundled_tessdata_dir() -> Path:
    return _resource_root() / "tessdata"


def _command_environment(command: list[str]) -> dict[str, str]:
    environment = os.environ.copy()
    if command and Path(command[0]).name == "tesseract" and _bundled_tessdata_dir().exists():
        environment["TESSDATA_PREFIX"] = str(_bundled_tessdata_dir())
    return environment


def _ssl_context() -> ssl.SSLContext:
    system_bundle = Path("/etc/ssl/cert.pem")
    if system_bundle.exists():
        return ssl.create_default_context(cafile=str(system_bundle))
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch_openai_models(api_key: str) -> list[str]:
    """Return likely general-purpose models visible to the supplied API project."""
    if not api_key.strip():
        raise ValueError("An OpenAI API key is missing.")
    request = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as response:
        payload = json.loads(response.read().decode("utf-8"))
    excluded = ("audio", "realtime", "transcribe", "tts", "image", "moderation",
                "embedding", "search", "codex", "chat", "instruct", "whisper",
                "sora", "babbage", "davinci")
    models = {}
    for item in payload.get("data", []):
        model_id = item.get("id", "") if isinstance(item, dict) else ""
        lowered = model_id.lower()
        if (model_id and not any(term in lowered for term in excluded)
                and not re.search(r"-\d{4}-\d{2}-\d{2}$", lowered)):
            models[model_id] = int(item.get("created", 0) or 0)
    return sorted(models, key=lambda model_id: (models[model_id], model_id), reverse=True)


def recommend_openai_models(api_key: str, model_ids: list[str], needs_images: bool) -> list[dict]:
    """Ask an accessible model to rank three candidates for this document workflow."""
    candidates = list(dict.fromkeys(model_ids))
    if not candidates:
        return []
    recommender = candidates[0]
    schema = {
        "type": "object", "additionalProperties": False, "required": ["recommendations"],
        "properties": {"recommendations": {
            "type": "array", "minItems": min(3, len(candidates)), "maxItems": min(3, len(candidates)),
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["model_id", "profile", "reason", "description", "input_price", "output_price"],
                "properties": {
                    "model_id": {"type": "string", "enum": candidates},
                    "profile": {"type": "string", "enum": ["Best balance", "Lowest price", "Highest quality"]},
                    "reason": {"type": "string", "maxLength": 180},
                    "description": {"type": "string", "maxLength": 220},
                    "input_price": {"type": ["number", "null"], "minimum": 0},
                    "output_price": {"type": ["number", "null"], "minimum": 0},
                },
            },
        }},
    }
    task = ("OCR correction plus page-image analysis, illustration cropping, and layout placement"
            if needs_images else "OCR correction of book text with strict preservation of language and style")
    body = {
        "model": recommender,
        "instructions": (
            "Recommend models for a PDF-to-EPUB application. Select only from the supplied IDs. Return distinct "
            "models. Optimize the three choices respectively for best balance, lowest likely cost, and highest "
            "output quality. For each selected model, provide a short English description. Supply the current standard "
            "USD price per one million input and output tokens only when you can verify it from official OpenAI "
            "information; otherwise return null. Never infer or guess a price. Write each practical reason in English."
        ),
        "input": f"Task: {task}\nAvailable model IDs: {json.dumps(candidates)}",
        "tools": [{"type": "web_search", "filters": {
            "allowed_domains": ["developers.openai.com", "platform.openai.com"]
        }}],
        "text": {"format": {"type": "json_schema", "name": "model_recommendations",
                            "strict": True, "schema": schema}},
    }
    options = ConversionOptions("Model recommendation", "", "eng", api_key=api_key, ai_model=recommender)
    parsed = _model_json(_response_text(_openai_response(
        body, options, max_attempts=2, attempt_timeout=90, retry_delays=(2,))))
    recommendations = parsed.get("recommendations", []) if isinstance(parsed, dict) else []
    valid, used = [], set()
    for item in recommendations:
        if isinstance(item, dict) and item.get("model_id") in candidates and item["model_id"] not in used:
            valid.append(item); used.add(item["model_id"])
    return valid[:3]

@dataclass
class ContentBlock:
    kind: str
    y: int
    text: str = ""
    image: Path | None = None
    css_class: str = ""
    bottom: int = 0
    anchor: int | None = None
    x1: int = 0
    x2: int = 0
    caption: str = ""
    caption_above: bool = False


@dataclass
class IllustrationCandidate:
    candidate_id: int
    bbox: tuple[int, int, int, int]
    page_number: int
    local_score: float
    reasons: list[str]
    source: str = "local"
    asset_path: Path | None = None
    image_type: str = "other"
    verified: bool = False


@dataclass
class BookChapter:
    title: str
    blocks: list[ContentBlock]


def _tool(name: str) -> str:
    bundled_tool = _resource_root() / "tools" / name
    if bundled_tool.is_file():
        return str(bundled_tool)
    path = shutil.which(name)
    if path:
        return path
    for prefix in (Path("/usr/local/bin"), Path("/opt/homebrew/bin")):
        candidate = prefix / name
        if candidate.exists():
            return str(candidate)
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override" / name
    if bundled.exists():
        return str(bundled)
    raise RuntimeError(f"The program '{name}' was not found.")


def available_ocr_languages() -> list[str]:
    try:
        out = subprocess.run([_tool("tesseract"), "--list-langs"], check=True, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=_command_environment([_tool("tesseract")])).stdout
        installed = {line.strip() for line in out.splitlines()[1:] if line.strip()}
        if _local_tessdata_dir().exists():
            installed.update(path.stem for path in _local_tessdata_dir().glob("*.traineddata"))
        return sorted(installed)
    except Exception:
        return ["eng"]


def ensure_ocr_language(code: str, progress=lambda message, fraction: None,
                        cancel_event: threading.Event | None = None) -> Path | None:
    local_file = _local_tessdata_dir() / f"{code}.traineddata"
    if local_file.exists():
        return local_file.parent
    if code in available_ocr_languages():
        return None
    if not re.fullmatch(r"[a-z]{3}(?:_[A-Za-z]+)?", code):
        raise ValueError("Invalid language code.")
    local_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_file.with_suffix(".traineddata.part")
    request = urllib.request.Request(LANGUAGE_DATA_URL.format(code=code), headers={"User-Agent": "PDF-to-Kindle/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response, temporary.open("wb") as stream:
            total = int(response.headers.get("Content-Length", 0))
            received = 0
            while True:
                if cancel_event and cancel_event.is_set():
                    raise ConversionCancelled("The conversion was cancelled.")
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                stream.write(chunk); received += len(chunk)
                fraction = received / total if total else 0
                progress(f"Downloading OCR language ({round(fraction * 100)}%)", fraction)
        if temporary.stat().st_size < 100_000:
            raise RuntimeError("The downloaded language file was invalid or incomplete.")
        temporary.replace(local_file)
    except ConversionCancelled:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Could not download the OCR language: {exc}") from exc
    return local_file.parent


def _run(command: list[str], cancel_event: threading.Event | None = None) -> str:
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=_command_environment(command))
    while process.poll() is None:
        if cancel_event and cancel_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            process.communicate()
            raise ConversionCancelled("The conversion was cancelled.")
        time.sleep(0.08)
    stdout, stderr = process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.strip() or "The conversion failed.")
    return stdout


def _ocr_layout(image: Path, language: str, cancel_event: threading.Event | None = None):
    command = [_tool("tesseract"), str(image), "stdout"]
    local_data = _local_tessdata_dir() / f"{language}.traineddata"
    if local_data.exists():
        command.extend(["--tessdata-dir", str(local_data.parent)])
    command.extend(["-l", language, "--psm", "3", "tsv"])
    raw = _run(command, cancel_event)
    words = []
    for row in csv.DictReader(io.StringIO(raw), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text or row.get("level") != "5":
            continue
        try:
            confidence = float(row["conf"])
            if confidence < 15:
                continue
            words.append({
                "text": text, "left": int(row["left"]), "top": int(row["top"]),
                "width": int(row["width"]), "height": int(row["height"]),
                "block": int(row["block_num"]), "paragraph": int(row["par_num"]),
                "line": int(row["line_num"]),
            })
        except (ValueError, KeyError):
            continue
    return words


def _text_blocks(words: list[dict], page_width: int) -> list[ContentBlock]:
    groups = {}
    for word in words:
        groups.setdefault((word["block"], word["paragraph"]), []).append(word)
    median_height = statistics.median(w["height"] for w in words) if words else 20
    blocks = []
    for key in sorted(groups):
        group = groups[key]
        lines = {}
        for word in group:
            lines.setdefault(word["line"], []).append(word)
        line_texts = []
        for line_words in sorted(lines.values(), key=lambda ws: min(w["top"] for w in ws)):
            line_words.sort(key=lambda w: w["left"])
            line_texts.append(" ".join(w["text"] for w in line_words))
        text = " ".join(line_texts).replace("- ", "")
        left = min(w["left"] for w in group)
        right = max(w["left"] + w["width"] for w in group)
        top = min(w["top"] for w in group)
        average_height = sum(w["height"] for w in group) / len(group)
        short = len(text.split()) <= 14 and len(text) <= 120
        if short and average_height > median_height * 1.18:
            kind = "heading"
        elif re.match(r"^(?:[•●▪◦]|[-–—]|\d+[.)])\s", text):
            kind = "list"
        else:
            kind = "paragraph"
        css_class = "indented" if left > page_width * 0.16 else ""
        bottom = max(w["top"] + w["height"] for w in group)
        blocks.append(ContentBlock(kind, top, text=text, css_class=css_class, bottom=bottom,
                                   x1=left, x2=right))
    return blocks


def _merge_illustrations(text_blocks: list[ContentBlock], illustrations: list[ContentBlock]) -> list[ContentBlock]:
    """Keep Tesseract's reading order and insert each figure near its vertical anchor."""
    if not illustrations:
        return list(text_blocks)
    groups = {}
    for illustration in sorted(illustrations, key=lambda block: block.y):
        anchor = illustration.anchor
        if anchor is None:
            next_index = next((i for i, block in enumerate(text_blocks)
                               if block.kind != "image" and block.y > illustration.y), len(text_blocks))
            anchor = next_index - 1
        groups.setdefault(anchor, []).append(illustration)
    merged = list(groups.pop(-1, []))
    for index, block in enumerate(text_blocks):
        merged.append(block)
        merged.extend(groups.pop(index, []))
    for anchor in sorted(groups):
        merged.extend(groups[anchor])
    return merged


def _local_illustration_candidates(image_path: Path, words: list[dict], page_number: int) -> list[IllustrationCandidate]:
    """Return scored non-text regions without materializing image assets."""
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        scale = min(1.0, 500 / image.width)
        small = image.resize((round(image.width * scale), round(image.height * scale)))
        gray = small.convert("L")
        edges = gray.filter(ImageFilter.FIND_EDGES)
        mask = Image.new("1", small.size, 1)
        draw = ImageDraw.Draw(mask)
        for word in words:
            x1 = max(0, round(word["left"] * scale) - 3)
            y1 = max(0, round(word["top"] * scale) - 3)
            x2 = min(small.width, round((word["left"] + word["width"]) * scale) + 3)
            y2 = min(small.height, round((word["top"] + word["height"]) * scale) + 3)
            draw.rectangle((x1, y1, x2, y2), fill=0)
        tile = 14
        active = set()
        for ty in range(0, small.height, tile):
            for tx in range(0, small.width, tile):
                box = (tx, ty, min(tx + tile, small.width), min(ty + tile, small.height))
                pixels = list(gray.crop(box).getdata())
                visible = list(mask.crop(box).getdata())
                kept = [p for p, allowed in zip(pixels, visible) if allowed]
                if not kept:
                    continue
                dark = sum(p < 225 for p in kept) / len(kept)
                variance = ImageStat.Stat(gray.crop(box)).var[0]
                edge_density = sum(p > 38 for p in edges.crop(box).getdata()) / len(pixels)
                if dark > 0.12 or edge_density > 0.13 or (dark > 0.035 and variance > 550):
                    active.add((tx // tile, ty // tile))
        regions = []
        while active:
            seed = active.pop(); stack = [seed]; component = [seed]
            while stack:
                x, y = stack.pop()
                for neighbour in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                    if neighbour in active:
                        active.remove(neighbour); stack.append(neighbour); component.append(neighbour)
            xs, ys = [p[0] for p in component], [p[1] for p in component]
            box = (min(xs)*tile, min(ys)*tile, min((max(xs)+1)*tile, small.width), min((max(ys)+1)*tile, small.height))
            width, height = box[2]-box[0], box[3]-box[1]
            area = width * height / (small.width * small.height)
            if area >= 0.006 and width >= small.width * 0.08 and height >= small.height * 0.035:
                regions.append((box, area))
        regions.sort(key=lambda item: item[0][1])
        results = []
        for candidate_id, (box, area) in enumerate(regions[:12]):
            original_box = tuple(round(value / scale) for value in box)
            crop = image.crop(original_box)
            crop_gray = crop.convert("L")
            variance = ImageStat.Stat(crop_gray).var[0]
            edge_density = sum(p > 38 for p in crop_gray.filter(ImageFilter.FIND_EDGES).getdata()) / max(1, crop.width * crop.height)
            color_density = sum(1 for r, g, b in crop.getdata() if max(r, g, b)-min(r, g, b) > 28) / max(1, crop.width * crop.height)
            overlap_area = 0
            for word in words:
                wx1, wy1 = word["left"], word["top"]
                wx2, wy2 = wx1 + word["width"], wy1 + word["height"]
                overlap_area += max(0, min(original_box[2], wx2)-max(original_box[0], wx1)) * max(0, min(original_box[3], wy2)-max(original_box[1], wy1))
            ocr_density = overlap_area / max(1, (original_box[2]-original_box[0]) * (original_box[3]-original_box[1]))
            aspect_ratio = max(original_box[2]-original_box[0], original_box[3]-original_box[1]) / max(
                1, min(original_box[2]-original_box[0], original_box[3]-original_box[1]))
            scan_text_residue = (len(words) >= 45 and ocr_density > 0.16 and color_density < 0.015
                                 and area < 0.12 and (edge_density < 0.11 or aspect_ratio > 2.5))
            if scan_text_residue:
                continue
            score = min(0.98, 0.22 + min(area, 0.35) * 1.15 + min(edge_density, 0.25) * 1.4
                        + min(variance / 3000, 0.22) + min(color_density, 0.25) * 0.5 - min(ocr_density, 0.35) * 0.45)
            reasons = [f"area={area:.3f}", f"edges={edge_density:.3f}",
                       f"variance={variance:.0f}", f"color={color_density:.3f}", f"ocr={ocr_density:.3f}"]
            normalized = tuple(round(value / (image.width if i % 2 == 0 else image.height) * 1000)
                               for i, value in enumerate(original_box))
            local_type = "illustration" if color_density < 0.04 and edge_density > 0.035 else "other"
            results.append(IllustrationCandidate(
                candidate_id, normalized, page_number, score, reasons, image_type=local_type))

        # A nearly full-page graphical plate is retained, while a page with
        # substantial OCR text is treated as a scanned text page instead.
        non_white = sum(p < 235 for p in gray.getdata()) / max(1, gray.width * gray.height)
        if not results and len(words) < 45 and non_white > 0.12:
            results.append(IllustrationCandidate(0, (0, 0, 1000, 1000), page_number, 0.62,
                                                 ["full_page_visual", f"ink={non_white:.3f}"]))
        return results


def _candidate_pixels(bbox, width, height):
    return (round(bbox[0] / 1000 * width), round(bbox[1] / 1000 * height),
            round(bbox[2] / 1000 * width), round(bbox[3] / 1000 * height))


def _bbox_iou(first, second):
    intersection = max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1]))
    first_area = max(0, first[2]-first[0]) * max(0, first[3]-first[1])
    second_area = max(0, second[2]-second[0]) * max(0, second[3]-second[1])
    union = first_area + second_area - intersection
    containment = intersection / max(1, min(first_area, second_area))
    return intersection / max(1, union), containment


def _deduplicate_candidates(candidates: list[IllustrationCandidate]) -> list[IllustrationCandidate]:
    priority = {"native": 3, "native_candidate": 2, "verified": 2, "local": 1}
    ordered = sorted(candidates, key=lambda item: (priority.get(item.source, 0), item.local_score), reverse=True)
    kept = []
    for candidate in ordered:
        if any(_bbox_iou(candidate.bbox, other.bbox)[0] >= 0.55 or
               _bbox_iou(candidate.bbox, other.bbox)[1] >= 0.86 for other in kept):
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda item: (item.bbox[1], item.bbox[0]))


def _native_image_candidates(pdf_path: Path, page_word_counts: list[int], output_dir: Path) -> dict[int, list[IllustrationCandidate]]:
    """Extract positioned PDF raster objects; return nothing when PyMuPDF is unavailable."""
    if fitz is None:
        logger.info("Native image extraction unavailable; using raster pipeline")
        return {}
    output_dir.mkdir(parents=True, exist_ok=True)
    results, assets = {}, {}
    try:
        document = fitz.open(str(pdf_path))
        for page_index in range(len(document)):
            page = document[page_index]
            page_area = max(1.0, page.rect.width * page.rect.height)
            page_candidates = []
            for image_info in page.get_images(full=True):
                xref = image_info[0]
                for rect in page.get_image_rects(xref):
                    area_ratio = rect.width * rect.height / page_area
                    if area_ratio < 0.004:
                        continue
                    # A large page-shaped raster is usually the scan substrate,
                    # even when poor OCR found few words. Never accept it as a
                    # native figure; the rendered page still goes through local
                    # candidate detection, which can recover real photographs.
                    if area_ratio >= 0.70:
                        logger.info(
                            "Native page raster deferred to local detection: page=%s area=%.3f ocr_words=%s",
                            page_index + 1, area_ratio, page_word_counts[page_index])
                        continue
                    if xref not in assets:
                        extracted = document.extract_image(xref)
                        extension = extracted.get("ext", "png").lower()
                        data = extracted["image"]
                        if extension not in {"jpg", "jpeg", "png"}:
                            converted = Image.open(io.BytesIO(data)).convert("RGBA")
                            asset = output_dir / f"native-{xref}.png"
                            converted.save(asset, "PNG", optimize=True)
                        else:
                            asset = output_dir / f"native-{xref}.{extension}"
                            asset.write_bytes(data)
                        assets[xref] = asset
                    bbox = (round(rect.x0 / page.rect.width * 1000), round(rect.y0 / page.rect.height * 1000),
                            round(rect.x1 / page.rect.width * 1000), round(rect.y1 / page.rect.height * 1000))
                    aspect = max(rect.width, rect.height) / max(1, min(rect.width, rect.height))
                    unambiguous = area_ratio >= 0.015 and aspect <= 8
                    page_candidates.append(IllustrationCandidate(
                        len(page_candidates), bbox, page_index + 1,
                        min(0.99, (0.86 if unambiguous else 0.62) + min(area_ratio, 0.4) * 0.25),
                        ["native_pdf_image", f"area={area_ratio:.3f}",
                         "unambiguous" if unambiguous else "needs_verification"],
                        source="native" if unambiguous else "native_candidate",
                        asset_path=assets[xref], verified=unambiguous,
                        image_type="photograph" if assets[xref].suffix.lower() in {".jpg", ".jpeg"} else "illustration",
                    ))
            if page_candidates:
                results[page_index + 1] = _deduplicate_candidates(page_candidates)
        document.close()
    except Exception as exc:
        logger.warning("Native image extraction failed; using raster pipeline: %s", exc)
        return {}
    return results


def _refine_illustration_bbox(image: Image.Image, bbox: tuple[int, int, int, int],
                              words: list[dict] | None = None,
                              layout_size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """Snap a verified normalized box to nearby high-resolution visual edges."""
    original = _candidate_pixels(bbox, image.width, image.height)
    margin_x, margin_y = round(image.width * 0.025), round(image.height * 0.025)
    search = (max(0, original[0]-margin_x), max(0, original[1]-margin_y),
              min(image.width, original[2]+margin_x), min(image.height, original[3]+margin_y))
    gray = image.crop(search).convert("L")
    # Suppress OCR text immediately outside the verified figure, while keeping
    # labels whose centres lie inside it (axes, legends and annotations).
    if words and layout_size:
        layout_width, layout_height = layout_size
        mask_draw = ImageDraw.Draw(gray)
        for word in words:
            centre_x = (word["left"] + word["width"] / 2) / max(1, layout_width) * 1000
            centre_y = (word["top"] + word["height"] / 2) / max(1, layout_height) * 1000
            if bbox[0] <= centre_x <= bbox[2] and bbox[1] <= centre_y <= bbox[3]:
                continue
            wx1 = round(word["left"] / max(1, layout_width) * image.width) - search[0] - 2
            wy1 = round(word["top"] / max(1, layout_height) * image.height) - search[1] - 2
            wx2 = round((word["left"] + word["width"]) / max(1, layout_width) * image.width) - search[0] + 2
            wy2 = round((word["top"] + word["height"]) / max(1, layout_height) * image.height) - search[1] + 2
            mask_draw.rectangle((wx1, wy1, wx2, wy2), fill=255)
    edge = gray.filter(ImageFilter.FIND_EDGES)
    width, height = gray.size
    active_rows, active_cols = [], []
    gray_pixels, edge_pixels = gray.load(), edge.load()
    border_x, border_y = max(2, width // 50), max(2, height // 50)
    border_values = []
    for x in range(0, width, max(1, width // 200)):
        border_values.extend(gray_pixels[x, y] for y in range(min(border_y, height)))
        border_values.extend(gray_pixels[x, y] for y in range(max(0, height-border_y), height))
    for y in range(0, height, max(1, height // 200)):
        border_values.extend(gray_pixels[x, y] for x in range(min(border_x, width)))
        border_values.extend(gray_pixels[x, y] for x in range(max(0, width-border_x), width))
    background = statistics.median(border_values) if border_values else 255
    for y in range(border_y, max(border_y + 1, height-border_y)):
        density = sum(abs(gray_pixels[x, y]-background) > 18 or edge_pixels[x, y] > 38
                      for x in range(border_x, max(border_x + 1, width-border_x))) / max(1, width-2*border_x)
        if density > 0.012:
            active_rows.append(y)
    for x in range(border_x, max(border_x + 1, width-border_x)):
        density = sum(abs(gray_pixels[x, y]-background) > 18 or edge_pixels[x, y] > 38
                      for y in range(border_y, max(border_y + 1, height-border_y))) / max(1, height-2*border_y)
        if density > 0.012:
            active_cols.append(x)
    if not active_rows or not active_cols:
        return original
    refined = (search[0] + min(active_cols), search[1] + min(active_rows),
               search[0] + max(active_cols) + 1, search[1] + max(active_rows) + 1)
    original_area = max(1, (original[2]-original[0]) * (original[3]-original[1]))
    refined_area = max(1, (refined[2]-refined[0]) * (refined[3]-refined[1]))
    if not 0.55 <= refined_area / original_area <= 1.55:
        return original
    pad_x, pad_y = max(4, round(image.width * 0.004)), max(4, round(image.height * 0.004))
    return (max(0, refined[0]-pad_x), max(0, refined[1]-pad_y),
            min(image.width, refined[2]+pad_x), min(image.height, refined[3]+pad_y))


CAPTION_PATTERN = re.compile(r"^(?:figure|fig\.|figur|illustration|diagram|chart|map|kort|abb\.)\s*\w*", re.I)


def _caption_for_bbox(blocks: list[ContentBlock], bbox, page_width: int, page_height: int):
    left, top, right, bottom = _candidate_pixels(bbox, page_width, page_height)
    candidates = []
    for index, block in enumerate(blocks):
        if block.kind == "image" or not block.text.strip() or len(block.text.split()) > 35:
            continue
        horizontal = max(0, min(right, block.x2)-max(left, block.x1)) / max(1, min(right-left, block.x2-block.x1))
        distance = min(abs(block.bottom-top), abs(block.y-bottom))
        strong = bool(CAPTION_PATTERN.match(block.text.strip()))
        if horizontal >= 0.25 and distance <= page_height * (0.045 if strong else 0.015):
            if strong or len(block.text.split()) <= 18:
                candidates.append((0 if strong else 1, distance, index, block.text.strip()))
    return min(candidates, default=None)


def _find_illustration_anchor(blocks: list[ContentBlock], bbox, page_width: int, page_height: int) -> int:
    left, top, right, bottom = _candidate_pixels(bbox, page_width, page_height)
    full_width = (right-left) >= page_width * 0.65
    ranked = []
    for index, block in enumerate(blocks):
        if block.kind == "image":
            continue
        overlap = max(0, min(right, block.x2)-max(left, block.x1)) / max(1, right-left)
        center_y = (block.y + block.bottom) / 2
        if center_y <= (top if full_width else (top+bottom)/2) and (full_width or overlap >= 0.18):
            ranked.append((abs(top-block.bottom), index))
    if ranked:
        return min(ranked)[1]
    above = [index for index, block in enumerate(blocks) if block.kind != "image" and block.bottom <= top]
    return above[-1] if above else -1


def _normalized_from_pixels(box, width, height):
    return (round(box[0]/width*1000), round(box[1]/height*1000),
            round(box[2]/width*1000), round(box[3]/height*1000))


def _visual_content_quality(candidate: IllustrationCandidate,
                            rendered_page: Path | None = None) -> tuple[bool, dict]:
    """Reject blank/ghost crops without making semantic image decisions."""
    source_path = candidate.asset_path or rendered_page
    if source_path is None:
        return False, {"reason": "missing_source"}
    with Image.open(source_path) as source:
        image = source.convert("RGB")
        if candidate.asset_path is None:
            image = image.crop(_candidate_pixels(candidate.bbox, image.width, image.height))
        if image.width < 16 or image.height < 16:
            return False, {"reason": "too_small", "width": image.width, "height": image.height}
        image.thumbnail((512, 512))
        gray = image.convert("L")
        inset_x, inset_y = max(2, gray.width // 100), max(2, gray.height // 100)
        if gray.width > inset_x * 2 + 4 and gray.height > inset_y * 2 + 4:
            gray = gray.crop((inset_x, inset_y, gray.width-inset_x, gray.height-inset_y))
        pixels = list(gray.getdata())
        stats = ImageStat.Stat(gray)
        mean, deviation = stats.mean[0], stats.stddev[0]
        edge = gray.filter(ImageFilter.FIND_EDGES)
        edge_inset = max(2, min(edge.width, edge.height) // 100)
        if edge.width > edge_inset * 2 + 4 and edge.height > edge_inset * 2 + 4:
            edge = edge.crop((edge_inset, edge_inset, edge.width-edge_inset, edge.height-edge_inset))
        edge_pixels = list(edge.getdata())
        edge_density = sum(value > 38 for value in edge_pixels) / max(1, len(edge_pixels))
        dominant = sum(abs(value-mean) <= 12 for value in pixels) / max(1, len(pixels))
        meaningful = not (
            deviation < 7.0 or
            (deviation < 18.0 and edge_density < 0.008 and dominant > 0.82)
        )
        return meaningful, {
            "deviation": round(deviation, 2), "edge_density": round(edge_density, 4),
            "dominant": round(dominant, 3), "reason": "ok" if meaningful else "low_information",
        }


def _save_candidate_asset(candidate: IllustrationCandidate, high_res_page: Path | None,
                          output_dir: Path, number: int, words: list[dict] | None = None,
                          layout_size: tuple[int, int] | None = None) -> tuple[Path, tuple[int, int, int, int]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if candidate.asset_path:
        return candidate.asset_path, candidate.bbox
    if high_res_page is None:
        raise RuntimeError("A high-resolution page is missing for the illustration crop.")
    with Image.open(high_res_page) as source:
        image = source.convert("RGB")
        refined = _refine_illustration_bbox(image, candidate.bbox, words, layout_size)
        normalized = _normalized_from_pixels(refined, image.width, image.height)
        crop = image.crop(refined)
        line_art = candidate.image_type in {"diagram", "chart", "map", "illustration"}
        suffix = ".png" if line_art else ".jpg"
        path = output_dir / f"figure-{candidate.page_number:05d}-{number}{suffix}"
        if line_art:
            crop.save(path, "PNG", optimize=True)
        else:
            crop.save(path, "JPEG", quality=92, optimize=True)
        return path, normalized


def _illustrations(image_path: Path, words: list[dict], output_dir: Path, page_number: int) -> list[ContentBlock]:
    """Materialize local candidates for non-AI mode and per-page fallback."""
    candidates = _deduplicate_candidates(_local_illustration_candidates(image_path, words, page_number))
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        for number, candidate in enumerate(candidates[:8], 1):
            box = _candidate_pixels(candidate.bbox, image.width, image.height)
            path = output_dir / f"figure-{page_number:05d}-{number}.jpg"
            image.crop(box).save(path, "JPEG", quality=90, optimize=True)
            results.append(ContentBlock("image", box[1], image=path, bottom=box[3], x1=box[0], x2=box[2]))
    return results


def _margin_key(text: str) -> str:
    text = re.sub(r"\d+", "#", text.casefold())
    return re.sub(r"\W+", " ", text).strip()


def _remove_running_margins(pages: list[list[ContentBlock]], heights: list[int]) -> list[list[ContentBlock]]:
    candidates = []
    for page_number, (blocks, height) in enumerate(zip(pages, heights)):
        for block in blocks:
            if block.kind == "image":
                continue
            zone = "top" if block.y < height * 0.09 else "bottom" if block.bottom > height * 0.91 else ""
            if zone:
                candidates.append((page_number, zone, _margin_key(block.text), block))
    counts = {}
    for page_number, zone, key, _ in candidates:
        counts.setdefault((zone, key), set()).add(page_number)
    repeated_minimum = 2 if len(pages) < 8 else max(3, round(len(pages) * 0.3))
    page_number_pattern = re.compile(r"^(?:side\s*)?[-–—]?\s*(?:\d+|[ivxlcdm]+)\s*[-–—]?$", re.I)
    remove_ids = set()
    for _, zone, key, block in candidates:
        is_page_number = zone == "bottom" and bool(page_number_pattern.fullmatch(block.text.strip()))
        is_repeated = bool(key) and len(counts[(zone, key)]) >= repeated_minimum
        if is_page_number or is_repeated:
            remove_ids.add(id(block))
    return [[block for block in blocks if id(block) not in remove_ids] for blocks in pages]


def _response_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise AIStructuredOutputError("The AI service returned a reply in an unknown format.")
    response_id = payload.get("id", "unknown")
    response_status = str(payload.get("status", "completed") or "completed")
    if response_status == "incomplete":
        details = payload.get("incomplete_details") or {}
        reason = details.get("reason", "unknown reason") if isinstance(details, dict) else "unknown reason"
        logger.warning("OpenAI response incomplete: id=%s reason=%s", response_id, reason)
        raise AIStructuredOutputError(f"The OpenAI reply was incomplete ({reason}).")
    if response_status in {"failed", "cancelled"}:
        error = payload.get("error") or {}
        detail = error.get("message", response_status) if isinstance(error, dict) else str(error)
        raise AIServiceError(f"OpenAI could not finish the reply: {detail}")

    text_parts = []
    content_types = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                content_types.append(str(content.get("type", "unknown")))
                if content.get("type") == "output_text":
                    text_parts.append(str(content.get("text", "")))
                elif content.get("type") == "refusal":
                    refusal = content.get("refusal", "The request was refused.")
                    raise AIServiceError(f"OpenAI refused the request: {refusal}")
    if not text_parts and isinstance(payload.get("output_text"), str):
        text_parts.append(payload["output_text"])
    text = "".join(text_parts)
    logger.info("OpenAI response parsed: id=%s status=%s text_parts=%s chars=%s content_types=%s",
                response_id, response_status, len(text_parts), len(text), content_types)
    if text:
        return text
    raise AIStructuredOutputError("AI-tjenesten returnerede intet tekstresultat.")


def _model_json(text: str):
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for position, character in enumerate(value):
            if character not in "[{":
                continue
            try:
                parsed, _ = decoder.raw_decode(value[position:])
                return parsed
            except json.JSONDecodeError:
                continue
        raise AIStructuredOutputError("The AI service did not return valid structured data.")


def _openai_response(body: dict, options: ConversionOptions,
                     cancel_event: threading.Event | None = None,
                     status=lambda message: None,
                     max_attempts: int = 6,
                     attempt_timeout: int = 180,
                     retry_delays: tuple[int, ...] = (2, 8, 30, 90, 180)) -> dict:
    max_attempts = max(1, int(max_attempts))
    attempt_timeout = max(10, int(attempt_timeout))
    client_request_id = str(uuid.uuid4())
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses", data=json.dumps(body).encode("utf-8"), method="POST",
        headers={
            "Authorization": f"Bearer {options.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": client_request_id,
            "X-Client-Request-Id": client_request_id,
        },
    )
    for attempt in range(max_attempts):
        if cancel_event and cancel_event.is_set():
            raise ConversionCancelled("The conversion was cancelled.")
        result_queue = queue.Queue(maxsize=1)
        logger.info("OpenAI request started: model=%s attempt=%s/%s",
                    options.ai_model, attempt + 1, max_attempts)
        def perform_request():
            try:
                with urllib.request.urlopen(
                        request, timeout=max(10, attempt_timeout - 10), context=_ssl_context()) as response:
                    headers = {
                        key.casefold(): value
                        for key, value in getattr(response, "headers", {}).items()
                    }
                    result_queue.put(("ok", response.read(), headers))
            except urllib.error.HTTPError as exc:
                headers = {key.casefold(): value for key, value in (exc.headers.items() if exc.headers else [])}
                result_queue.put(("http", exc.code, exc.read(), headers))
            except Exception as exc:
                result_queue.put(("error", exc))
        threading.Thread(target=perform_request, daemon=True).start()
        started = time.monotonic()
        last_second = -1
        while True:
            if cancel_event and cancel_event.is_set():
                raise ConversionCancelled("The conversion was cancelled.")
            try:
                result = result_queue.get(timeout=0.1)
                break
            except queue.Empty:
                elapsed = int(time.monotonic() - started)
                if elapsed >= attempt_timeout:
                    result = ("error", TimeoutError(
                        f"The OpenAI call exceeded the time limit of {attempt_timeout} seconds"))
                    break
                if elapsed != last_second:
                    status(f"Waiting for OpenAI · attempt {attempt + 1} of {max_attempts} · {elapsed} s elapsed")
                    last_second = elapsed
        if result[0] == "ok":
            try:
                payload = json.loads(result[1])
                request_id = result[2].get("x-request-id", "")
                logger.info("OpenAI request completed: model=%s attempt=%s/%s elapsed=%.1fs request_id=%s",
                            options.ai_model, attempt + 1, max_attempts,
                            time.monotonic() - started, request_id)
                status("OpenAI reply received")
                return payload
            except json.JSONDecodeError as exc:
                raise AIServiceError("OpenAI returned an invalid HTTP response.", retryable=True) from exc
        if result[0] == "http":
            code, raw_detail, response_headers = result[1], result[2].decode("utf-8", "replace"), result[3]
            error_code, detail = "", raw_detail
            try:
                error = json.loads(raw_detail).get("error", {})
                detail = error.get("message", raw_detail)
                error_code = error.get("code", "") or error.get("type", "")
            except json.JSONDecodeError:
                pass
            retryable = code in {408, 409, 429, 500, 502, 503, 504} and error_code != "insufficient_quota"
            if not retryable or attempt == max_attempts - 1:
                if error_code == "insufficient_quota":
                    detail = "The API account quota is used up."
                logger.error("OpenAI HTTP failure: status=%s code=%s attempt=%s/%s request_id=%s",
                             code, error_code, attempt + 1, max_attempts,
                             response_headers.get("x-request-id", ""))
                raise AIServiceError(
                    f"OpenAI could not complete the processing: {detail}",
                    retryable=retryable, status_code=code)
        else:
            exc = result[1]
            if attempt == max_attempts - 1:
                reason = getattr(exc, "reason", str(exc))
                logger.error("OpenAI connection failure after retries: %s", reason)
                raise AIServiceError(
                    f"The connection to OpenAI failed after several attempts: {reason}", retryable=True)
        configured_delay = retry_delays[min(attempt, len(retry_delays) - 1)] if retry_delays else 0
        retry_after = 0
        if result[0] == "http":
            try:
                retry_after = max(0, round(float(response_headers.get("retry-after", 0))))
            except (TypeError, ValueError):
                retry_after = 0
        delay = max(configured_delay, retry_after)
        logger.warning("OpenAI request retry scheduled: next_attempt=%s/%s delay=%ss client_request_id=%s",
                       attempt + 2, max_attempts, delay, client_request_id)
        deadline = time.monotonic() + delay
        last_remaining = None
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise ConversionCancelled("The conversion was cancelled.")
            remaining = max(1, math.ceil(deadline - time.monotonic()))
            if remaining != last_remaining:
                status(
                    f"Temporary OpenAI error · attempt {attempt + 2} of {max_attempts} "
                    f"starts in {_time_text(remaining)}"
                )
                last_remaining = remaining
            time.sleep(0.1)
    raise AIServiceError("OpenAI could not complete the processing.", retryable=True)


def _ai_request(items: list[dict], options: ConversionOptions, cancel_event: threading.Event | None,
                status=lambda message: None) -> tuple[dict[int, str], dict]:
    if cancel_event and cancel_event.is_set():
        raise ConversionCancelled("The conversion was cancelled.")
    instructions = (
        "You are proofreading text produced by OCR. Correct only obvious OCR recognition errors, broken words, "
        "spacing, and punctuation. Preserve the document's original language, meaning, quotations, names, "
        "numbers, paragraph structure, and writing style. Never summarize, translate, modernize, or rewrite. "
        "Return valid JSON matching the supplied schema, with exactly the same item IDs."
    )
    schema = {
        "type": "object", "additionalProperties": False, "required": ["items"],
        "properties": {"items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "text"],
            "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
        }}},
    }
    body = {
        "model": options.ai_model, "instructions": instructions,
        "input": json.dumps({"items": items}, ensure_ascii=False),
        "text": {"format": {"type": "json_schema", "name": "ocr_corrections",
                              "strict": True, "schema": schema}},
    }
    total_usage = {"input_tokens": 0, "output_tokens": 0}
    expected_ids = {int(item["id"]) for item in items}
    for structured_attempt in range(2):
        result = _openai_response(body, options, cancel_event, status)
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        for key in ("input_tokens", "output_tokens"):
            total_usage[key] += int(usage.get(key, 0) or 0)
        try:
            parsed = _model_json(_response_text(result))
            returned_items = (parsed if isinstance(parsed, list)
                              else parsed.get("items", []) if isinstance(parsed, dict) else [])
            corrected = {
                int(item["id"]): str(item["text"])
                for item in returned_items
                if isinstance(item, dict) and "id" in item and "text" in item
            }
            if set(corrected) != expected_ids:
                raise AIStructuredOutputError(
                    f"The AI reply contained {len(corrected)} of {len(expected_ids)} expected text parts.")
            return corrected, total_usage
        except (AIStructuredOutputError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Structured text response rejected: attempt=%s/2 items=%s error=%s",
                           structured_attempt + 1, len(items), exc)
            if structured_attempt == 1:
                raise AIStructuredOutputError(
                    "The AI service did not return valid structured data after an extra attempt.") from exc
            status("Structured OpenAI reply was incomplete · retrying the text chunk (2 of 2)")
    raise AIStructuredOutputError("The AI service did not return valid structured data.")


VISION_PROMPT = """Identify genuine visual figures that should be preserved when converting a book page to EPUB.

Include:
photographs, drawings, illustrations, maps, diagrams, charts and substantial artwork.

Exclude:
ordinary text, headings, captions outside the figure, page numbers, borders, rules, stains, scanning artefacts and decorative noise.

Each supplied image has a SOURCE_ID.

Return figures relative to the corresponding source image using coordinates 0-1000.

Include the complete figure, including labels, axes and legends that are part of the figure itself.

Do not include surrounding body text or captions.

If a source contains no genuine figure, return no figure for that source."""


def _vision_schema(source_ids):
    return {
        "type": "object", "additionalProperties": False, "required": ["figures"],
        "properties": {"figures": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["source_id", "type", "x1", "y1", "x2", "y2", "needs_more_context"],
            "properties": {
                "source_id": {"type": "integer", "enum": source_ids},
                "type": {"type": "string", "enum": ["photograph", "illustration", "diagram", "chart", "map", "other"]},
                "x1": {"type": "integer", "minimum": 0, "maximum": 1000},
                "y1": {"type": "integer", "minimum": 0, "maximum": 1000},
                "x2": {"type": "integer", "minimum": 0, "maximum": 1000},
                "y2": {"type": "integer", "minimum": 0, "maximum": 1000},
                "needs_more_context": {"type": "boolean"},
            },
        }}},
    }


def _expand_bbox(bbox, ratio):
    width, height = bbox[2]-bbox[0], bbox[3]-bbox[1]
    return (max(0, round(bbox[0]-width*ratio)), max(0, round(bbox[1]-height*ratio)),
            min(1000, round(bbox[2]+width*ratio)), min(1000, round(bbox[3]+height*ratio)))


def _ai_verify_candidates(image_path: Path, candidates: list[IllustrationCandidate], options: ConversionOptions,
                          cancel_event: threading.Event | None, status=lambda message: None,
                          detail="low", context_ratio=0.08) -> tuple[list[IllustrationCandidate], dict]:
    if not candidates:
        return [], {}
    source_ids = list(range(len(candidates)))
    contexts, content = {}, [{"type": "input_text", "text": VISION_PROMPT}]
    with Image.open(image_path) as source:
        page = source.convert("RGB")
        for source_id, candidate in enumerate(candidates):
            context = _expand_bbox(candidate.bbox, context_ratio)
            contexts[source_id] = context
            crop = page.crop(_candidate_pixels(context, page.width, page.height))
            crop.thumbnail((768, 768))
            buffer = io.BytesIO(); crop.save(buffer, "JPEG", quality=76, optimize=True)
            image_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            content.extend([
                {"type": "input_text", "text": f"SOURCE_ID={source_id}"},
                {"type": "input_image", "image_url": image_url, "detail": detail},
            ])
    vision_options = ConversionOptions(
        options.title, options.author, options.language, options.dpi, options.preserve_visual_pages,
        options.ai_cleanup, options.ai_illustrations, options.api_key,
        options.illustration_model or options.ai_model, options.illustration_model,
        options.illustration_retries,
    )
    result = _openai_response({
        "model": vision_options.ai_model,
        "instructions": "Verify candidate figures. Return only the structured result; do not explain.",
        "input": [{"role": "user", "content": content}],
        "reasoning": {"effort": "none"},
        "text": {"verbosity": "low", "format": {"type": "json_schema", "name": "verified_figures",
                                                    "strict": True, "schema": _vision_schema(source_ids)}},
        "max_output_tokens": 600,
    }, vision_options, cancel_event, status, max_attempts=options.illustration_retries)
    try:
        parsed = _model_json(_response_text(result))
    except RuntimeError:
        if detail != "low":
            raise
        logger.info("Vision escalation: page=%s reason=invalid_low_detail_output detail=high",
                    candidates[0].page_number)
        extra, extra_usage = _ai_verify_candidates(
            image_path, candidates, options, cancel_event, status, detail="high", context_ratio=0.22)
        low_usage = result.get("usage", {})
        return extra, {
            "input_tokens": int(low_usage.get("input_tokens", 0)) + int(extra_usage.get("input_tokens", 0)),
            "output_tokens": int(low_usage.get("output_tokens", 0)) + int(extra_usage.get("output_tokens", 0)),
            "high_detail_calls": int(extra_usage.get("high_detail_calls", 0)),
            "full_page_calls": int(extra_usage.get("full_page_calls", 0)),
        }
    figures = parsed.get("figures", []) if isinstance(parsed, dict) else []
    verified, escalation_sources = [], set()
    returned_sources = {int(figure.get("source_id")) for figure in figures
                        if isinstance(figure, dict) and str(figure.get("source_id", "")).isdigit()}
    if detail == "low":
        escalation_sources.update(
            source_id for source_id, candidate in enumerate(candidates)
            if candidate.local_score >= .82 and source_id not in returned_sources)
    for figure in figures:
        try:
            source_id = int(figure["source_id"])
            candidate, context = candidates[source_id], contexts[source_id]
            x1, y1, x2, y2 = (max(0, min(1000, int(figure[key]))) for key in ("x1", "y1", "x2", "y2"))
            if x2 <= x1 or y2 <= y1:
                continue
            mapped = (round(context[0] + x1/1000*(context[2]-context[0])),
                      round(context[1] + y1/1000*(context[3]-context[1])),
                      round(context[0] + x2/1000*(context[2]-context[0])),
                      round(context[1] + y2/1000*(context[3]-context[1])))
            if figure.get("needs_more_context") and detail == "low":
                escalation_sources.add(source_id)
                continue
            verified.append(IllustrationCandidate(
                candidate.candidate_id, candidate.bbox if candidate.asset_path else mapped,
                candidate.page_number, candidate.local_score,
                candidate.reasons + [f"vision_{detail}"], source="verified",
                asset_path=candidate.asset_path, image_type=figure["type"], verified=True,
            ))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    usage = dict(result.get("usage", {}))
    usage.setdefault("high_detail_calls", 1 if detail == "high" else 0)
    usage.setdefault("full_page_calls", int(
        detail == "high" and any(
            (item.bbox[2]-item.bbox[0]) * (item.bbox[3]-item.bbox[1]) >= 850_000
            for item in candidates)))
    if escalation_sources and detail == "low":
        escalation = [candidates[index] for index in sorted(escalation_sources)]
        logger.info("Vision escalation: page=%s sources=%s detail=high", candidates[0].page_number, len(escalation))
        extra, extra_usage = _ai_verify_candidates(image_path, escalation, options, cancel_event, status,
                                                   detail="high", context_ratio=0.22)
        verified.extend(extra)
        usage = {
            "input_tokens": int(usage.get("input_tokens", 0)) + int(extra_usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)) + int(extra_usage.get("output_tokens", 0)),
            "high_detail_calls": int(usage.get("high_detail_calls", 0)) + int(extra_usage.get("high_detail_calls", 0)),
            "full_page_calls": int(usage.get("full_page_calls", 0)) + int(extra_usage.get("full_page_calls", 0)),
        }
    return _deduplicate_candidates(verified), usage


def _verify_candidates_or_fallback(image_path: Path, candidates: list[IllustrationCandidate],
                                   options: ConversionOptions,
                                   cancel_event: threading.Event | None,
                                   status=lambda message: None,
                                   warning=lambda message: None) -> tuple[list[IllustrationCandidate], dict, bool]:
    """Isolate vision failure to one page and preserve credible local candidates."""
    if not candidates:
        return [], {}, False
    page_number = candidates[0].page_number
    try:
        verified, usage = _ai_verify_candidates(
            image_path, candidates, options, cancel_event, status)
        return verified, usage, False
    except ConversionCancelled:
        raise
    except (AIServiceError, RuntimeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        fallback = [item for item in candidates if item.local_score >= .42]
        warning(f"AI image analysis failed on page {page_number}; local candidates were kept.")
        logger.warning("Vision page fallback: page=%s error=%s", page_number, exc)
        return fallback, {}, True


def _estimate_ai_tokens(batches: list[list[dict]]) -> tuple[int, int]:
    # A conservative approximation for Danish/European prose plus JSON and instructions.
    characters = sum(len(item["text"]) for batch in batches for item in batch)
    blocks = sum(len(batch) for batch in batches)
    input_tokens = round(characters / 3.4 + blocks * 9 + len(batches) * 115)
    output_tokens = round(characters / 3.4 * 1.08 + blocks * 7)
    return max(input_tokens, 1), max(output_tokens, 1)


def _time_text(seconds: float) -> str:
    seconds = max(0, round(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def _report_progress(progress, message: str, fraction: float, remaining_seconds=None) -> None:
    """Support the GUI's ETA callback as well as existing two-argument callers."""
    fraction = max(0.0, min(1.0, fraction))
    try:
        progress(message, fraction, remaining_seconds)
    except TypeError:
        progress(message, fraction)


def _text_fragments(text: str, limit: int = 3000) -> list[str]:
    fragments = []
    remaining = text.strip()
    while len(remaining) > limit:
        split_at = max(remaining.rfind(". ", 0, limit), remaining.rfind(" ", 0, limit))
        if split_at < limit // 2:
            split_at = limit
        else:
            split_at += 1
        fragments.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        fragments.append(remaining)
    return fragments


def _ai_cleanup(pages: list[list[ContentBlock]], options: ConversionOptions, progress, cancel_event,
                approval=lambda input_tokens, output_tokens, model: True,
                progress_start=0.0, progress_span=1.0, trailing_seconds=0.0,
                warning=lambda message: None) -> dict:
    text_blocks = [block for page in pages for block in page if block.kind != "image" and block.text.strip()]
    if not text_blocks:
        return {"completed_batches": 0, "failed_batches": 0, "total_batches": 0}
    batches, current, length, fragment_map = [], [], 0, {}
    item_id = 0
    for block_index, block in enumerate(text_blocks):
        for part_index, fragment in enumerate(_text_fragments(block.text)):
            item = {"id": item_id, "text": fragment}
            fragment_map[item_id] = (block_index, part_index)
            item_id += 1
            if current and length + len(fragment) > 3500:
                batches.append(current); current, length = [], 0
            current.append(item); length += len(fragment)
    if current:
        batches.append(current)
    input_tokens, output_tokens = _estimate_ai_tokens(batches)
    if not approval(input_tokens, output_tokens, options.ai_model):
        raise ConversionCancelled("The AI post-processing was not approved.")
    actual_input = actual_output = 0
    pending_corrections = {}
    durations = []
    failed_batches = 0
    completed_batches = 0
    stopped_early = False
    for batch_number, batch in enumerate(batches, 1):
        completed = batch_number - 1
        fraction = progress_start + progress_span * completed / len(batches)
        expected = statistics.mean(durations) if durations else 45.0
        total_remaining = expected * (len(batches) - completed) + trailing_seconds
        confident_eta = total_remaining if len(durations) >= 2 else None
        _report_progress(progress, f"Sending text chunk {batch_number} of {len(batches)} to OpenAI",
                         fraction, confident_eta)
        started = time.monotonic()
        context = f"Text chunk {batch_number} of {len(batches)}"
        def report_text_wait(message):
            elapsed = time.monotonic() - started
            average = statistics.mean(durations) if durations else 45.0
            remaining = max(0, average - elapsed) + average * (len(batches) - batch_number) + trailing_seconds
            live_fraction = progress_start + progress_span * min(
                len(batches), completed + min(0.9, elapsed / max(average, 1))
            ) / len(batches)
            confident_eta = remaining if len(durations) >= 2 else None
            _report_progress(progress, f"{context} · {message}", live_fraction, confident_eta)
        try:
            corrected, usage = _ai_request(batch, options, cancel_event, report_text_wait)
            completed_batches += 1
            actual_input += int(usage.get("input_tokens", 0))
            actual_output += int(usage.get("output_tokens", 0))
            for item in batch:
                if item["id"] in corrected:
                    pending_corrections[item["id"]] = corrected[item["id"]].strip()
        except ConversionCancelled:
            raise
        except AIStructuredOutputError as exc:
            failed_batches += 1
            logger.warning("Text batch kept as OCR after structured output failure: batch=%s/%s error=%s",
                           batch_number, len(batches), exc)
        except AIServiceError as exc:
            failed_batches += len(batches) - batch_number + 1
            stopped_early = True
            logger.warning("Text AI stopped; completed batches preserved: batch=%s/%s error=%s",
                           batch_number, len(batches), exc)
            warning(
                f"The OpenAI connection stopped at text chunk {batch_number} of {len(batches)}. "
                f"Corrections from {completed_batches} finished chunks are kept; the rest uses the OCR text. {exc}"
            )
            break
        finally:
            durations.append(time.monotonic() - started)
    rebuilt = {}
    for fragment_id in sorted(fragment_map):
        block_index, part_index = fragment_map[fragment_id]
        original = next(item["text"] for batch in batches for item in batch if item["id"] == fragment_id)
        rebuilt.setdefault(block_index, []).append((part_index, pending_corrections.get(fragment_id, original)))
    for block_index, parts in rebuilt.items():
        text_blocks[block_index].text = " ".join(text for _, text in sorted(parts))
    if actual_input or actual_output:
        _report_progress(progress, f"AI done: {actual_input + actual_output:,} tokens used",
                         progress_start + progress_span, trailing_seconds if durations else None)
    if failed_batches and not stopped_early:
        logger.info("Text AI completed with local fallback: completed=%s failed=%s total=%s",
                    completed_batches, failed_batches, len(batches))
        warning(
            f"{failed_batches} of {len(batches)} text chunks could not be post-processed with AI. "
            f"The AI corrections from the other {completed_batches} chunks are kept; "
            "the affected chunks use the original OCR text."
        )
    return {"completed_batches": completed_batches, "failed_batches": failed_batches,
            "total_batches": len(batches)}


def _chapter_heading(block: ContentBlock, position: int, accumulated_chars: int) -> bool:
    if block.kind != "heading":
        return False
    title = block.text.strip()
    explicit = re.match(
        r"^(?:kapitel|chapter|del|part|bog|book|afsnit)\s+(?:\d+|[ivxlcdm]+|[a-zæøå])(?:\b|\s*[:.–—-])",
        title, re.I,
    )
    numbered = re.match(r"^(?:\d{1,3}|[ivxlcdm]{1,8})[.:]\s+\S", title, re.I)
    typographic = position == 0 and len(title.split()) <= 10 and accumulated_chars >= 1200
    return bool(explicit or numbered or typographic)


def _detect_chapters(pages: list[list[ContentBlock]], book_title: str) -> list[BookChapter]:
    chapters = []
    current_title = book_title
    current_blocks = []
    accumulated_chars = 0
    for page in pages:
        text_positions = [i for i, block in enumerate(page) if block.kind != "image"]
        candidate_index = None
        for position, index in enumerate(text_positions[:4]):
            if _chapter_heading(page[index], position, accumulated_chars):
                candidate_index = index
                break
        if candidate_index is not None:
            heading = page[candidate_index]
            before = page[:candidate_index]
            if before:
                current_blocks.extend(before)
                accumulated_chars += sum(len(block.text) for block in before)
            if current_blocks:
                chapters.append(BookChapter(current_title, current_blocks))
            current_title = heading.text.strip()
            current_blocks = page[candidate_index + 1:]
            accumulated_chars = sum(len(block.text) for block in current_blocks)
        else:
            current_blocks.extend(page)
            accumulated_chars += sum(len(block.text) for block in page)
    if current_blocks or not chapters:
        chapters.append(BookChapter(current_title, current_blocks))
    return chapters


def _page_count(pdf_path: Path) -> int:
    if fitz is not None:
        try:
            with fitz.open(str(pdf_path)) as document:
                return len(document)
        except Exception as exc:
            logger.warning("PyMuPDF page count failed; falling back to pdfinfo: %s", exc)
    info = _run([_tool("pdfinfo"), str(pdf_path)])
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not read the number of pages in the PDF.")
    return int(match.group(1))


def _render_page(pdf_path: Path, page_number: int, output_prefix: Path, dpi: int,
                 cancel_event: threading.Event | None, image_format: str = "jpeg") -> Path:
    suffix = ".png" if image_format == "png" else ".jpg"
    rendered = output_prefix.with_suffix(suffix)
    # PyMuPDF is bundled in the standalone app and handles both the OCR render
    # and high-resolution final crops. Poppler remains a source-mode fallback.
    if fitz is not None:
        try:
            if cancel_event and cancel_event.is_set():
                raise ConversionCancelled("The conversion was cancelled.")
            with fitz.open(str(pdf_path)) as document:
                page = document[page_number-1]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72), alpha=False)
                mode = "RGB" if pixmap.n == 3 else "RGBA"
                image = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples).convert("RGB")
                if image_format == "png":
                    image.save(rendered, "PNG", optimize=True)
                else:
                    image.save(rendered, "JPEG", quality=86, optimize=True)
            return rendered
        except ConversionCancelled:
            raise
        except Exception as exc:
            logger.warning("PyMuPDF page rendering failed; falling back to pdftoppm: %s", exc)
    command = [_tool("pdftoppm"), "-f", str(page_number), "-l", str(page_number),
               "-singlefile", f"-{image_format}", "-r", str(dpi)]
    if image_format == "jpeg":
        command.extend(["-jpegopt", "quality=86"])
    command.extend([str(pdf_path), str(output_prefix)])
    _run(command, cancel_event)
    if not rendered.exists():
        raise RuntimeError(f"Page {page_number} could not be prepared.")
    return rendered


def _looks_visual(image_path: Path, text: str, words: list[dict] | None = None,
                  scratch_dir: Path | None = None, page_number: int = 0) -> bool:
    """High-recall local prefilter used before spending tokens on vision analysis."""
    with Image.open(image_path) as image:
        sample = image.convert("RGB")
        sample.thumbnail((320, 480))
        stat = ImageStat.Stat(sample)
        color_spread = sum(stat.var) / 3
        color_pixels = sum(1 for r, g, b in sample.getdata() if max(r, g, b) - min(r, g, b) > 28) / (sample.width * sample.height)
        grayscale = sample.convert("L")
        non_white = sum(1 for pixel in grayscale.getdata() if pixel < 235) / (sample.width * sample.height)
    word_boxes = words
    word_count = len(re.findall(r"\w+", text, re.UNICODE))
    visual_terms = re.search(
        r"\b(?:fig(?:ur|ure|\.)|illustration|diagram|chart|graph|map|photo(?:graph)?|"
        r"billede|figur|kort|graf|tabel|zeichnung|abbildung|carte|imagen|foto)\b",
        text, re.IGNORECASE,
    ) is not None
    regions_found = False
    if scratch_dir is not None and word_boxes is not None:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        # OCR boxes are masked before region detection, preventing normal paragraphs
        # from being mistaken for illustrations.
        regions_found = bool(_illustrations(image_path, word_boxes, scratch_dir, page_number))
    likely = (regions_found or color_pixels > 0.055 or
              (visual_terms and non_white > 0.10 and word_count < 350) or
              (non_white > 0.28 and word_count < 180))
    logger.info("Local illustration prefilter: page=%s likely=%s regions=%s terms=%s color_pixels=%.2f variance=%.0f ink=%.2f words=%s",
                page_number, likely, regions_found, visual_terms, color_pixels, color_spread, non_white, word_count)
    return likely


def _paragraphs(text: str) -> list[tuple[str, str]]:
    text = text.replace("\f", "\n").replace("­", "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    blocks: list[str] = []
    current = ""
    for line in lines:
        if not line:
            if current:
                blocks.append(current)
                current = ""
            continue
        if current.endswith("-") and line[:1].islower():
            current = current[:-1] + line
        else:
            current = f"{current} {line}".strip()
    if current:
        blocks.append(current)

    result = []
    for block in blocks:
        word_count = len(block.split())
        heading = word_count <= 12 and len(block) <= 100 and not block.endswith((".", ",", ";"))
        result.append(("h2" if heading else "p", block))
    return result


def _safe_id(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "book"


def _should_process_images(options: ConversionOptions) -> bool:
    """Keep local visual preservation for non-AI runs, but make AI text-only truly text-only."""
    return options.preserve_visual_pages and (not options.ai_cleanup or options.ai_illustrations)


def convert_pdf_to_epub(pdf_path: Path, output_path: Path, options: ConversionOptions,
                        progress=lambda message, fraction: None,
                        cancel_event: threading.Event | None = None,
                        ai_approval=lambda *args: True,
                        warning=lambda message: None) -> Path:
    pdf_path, output_path = Path(pdf_path), Path(output_path)
    logger.info("Conversion started: file=%s ai_text=%s ai_images=%s model=%s",
                pdf_path.name, options.ai_cleanup, options.ai_illustrations, options.ai_model)
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Choose a valid PDF file.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process_images = _should_process_images(options)

    ensure_ocr_language(options.language,
                        lambda message, fraction: progress(message, fraction * 0.02),
                        cancel_event)

    with tempfile.TemporaryDirectory(prefix="pdf-to-kindle-") as temp_name:
        temp = Path(temp_name)
        total_pages = _page_count(pdf_path)
        render_unit = 1.2
        ocr_unit = 2.8
        image_unit = 25.0 if options.ai_illustrations and process_images else 0.0
        estimated_text_batches = max(1, math.ceil(total_pages * 1500 / 3500)) if options.ai_cleanup else 0
        text_unit = 45.0
        epub_unit = 3.0
        planned_total = total_pages * (render_unit + ocr_unit + image_unit) + estimated_text_batches * text_unit + epub_unit

        def planned_fraction(completed_seconds):
            return min(0.985, completed_seconds / max(planned_total, 1.0))

        pages = []
        render_durations = []
        for index in range(1, total_pages + 1):
            if cancel_event and cancel_event.is_set():
                raise ConversionCancelled("The conversion was cancelled.")
            render_average = statistics.mean(render_durations) if render_durations else render_unit
            remaining = (total_pages - index + 1) * render_average + total_pages * (ocr_unit + image_unit) + estimated_text_batches * text_unit + epub_unit
            _report_progress(progress, f"Preparing page {index} of {total_pages}",
                             planned_fraction((index - 1) * render_unit),
                             remaining if len(render_durations) >= 2 and not (options.ai_cleanup or options.ai_illustrations) else None)
            render_started = time.monotonic()
            rendered = _render_page(pdf_path, index, temp / f"page-{index:05d}",
                                    options.dpi, cancel_event)
            pages.append(rendered)
            render_durations.append(time.monotonic() - render_started)

        page_data = []
        page_heights = []
        page_words = []
        ocr_durations = []
        for index, page in enumerate(pages, 1):
            if cancel_event and cancel_event.is_set():
                raise ConversionCancelled("The conversion was cancelled.")
            ocr_average = statistics.mean(ocr_durations) if ocr_durations else ocr_unit
            remaining = (total_pages - index + 1) * ocr_average + total_pages * image_unit + estimated_text_batches * text_unit + epub_unit
            _report_progress(progress, f"OCR: page {index} of {len(pages)}",
                             planned_fraction(total_pages * render_unit + (index - 1) * ocr_unit),
                             remaining if len(ocr_durations) >= 2 and not (options.ai_cleanup or options.ai_illustrations) else None)
            ocr_started = time.monotonic()
            words = _ocr_layout(page, options.language, cancel_event)
            page_words.append(words)
            with Image.open(page) as rendered:
                blocks = _text_blocks(words, rendered.width)
                page_heights.append(rendered.height)
            page_data.append(blocks)
            ocr_durations.append(time.monotonic() - ocr_started)

        page_data = _remove_running_margins(page_data, page_heights)
        native_by_page = {}
        local_by_page = {}
        if process_images:
            native_by_page = _native_image_candidates(
                pdf_path, [len(words) for words in page_words], temp / "native-images")
            for page_number, (page, words) in enumerate(zip(pages, page_words), 1):
                raw_local = _local_illustration_candidates(page, words, page_number)
                merged = _deduplicate_candidates(
                    list(native_by_page.get(page_number, [])) + raw_local)
                local = [item for item in merged if item.source != "native"]
                local_by_page[page_number] = local
                logger.info("Image candidates: page=%s native=%s local_raw=%s local_after_native_dedup=%s scores=%s",
                            page_number, len(native_by_page.get(page_number, [])), len(raw_local), len(local),
                            [round(item.local_score, 3) for item in local])
        vision_pages = [page_number for page_number, candidates in local_by_page.items() if candidates]
        if options.ai_illustrations and process_images:
            _report_progress(
                progress,
                f"Local image filter: {len(vision_pages)} of {total_pages} pages need AI verification",
                planned_fraction(total_pages * (render_unit + ocr_unit)), None,
            )
        text_characters = sum(len(block.text) for page in page_data for block in page
                              if block.kind != "image" and block.text.strip())
        estimated_text_batches = max(1, math.ceil(text_characters / 3500)) if options.ai_cleanup else 0
        local_weight = total_pages * (render_unit + ocr_unit)
        image_weight = len(vision_pages) * image_unit if options.ai_illustrations else 0
        text_weight = estimated_text_batches * text_unit
        planned_total = local_weight + image_weight + text_weight + epub_unit
        # Text correction is deliberately first after approval. It gives the
        # user an immediate visible handoff to OpenAI, while final image crops
        # can be prepared locally afterwards without delaying the text flow.
        text_start = local_weight / planned_total
        text_span = text_weight / planned_total
        image_start = (local_weight + text_weight) / planned_total
        image_span = image_weight / planned_total
        if options.ai_cleanup or (options.ai_illustrations and vision_pages):
            if not options.api_key.strip():
                warning("An OpenAI API key is missing. The AI step is skipped.")
                options.ai_cleanup = False
                options.ai_illustrations = False
            else:
                text_items = [{"id": i, "text": block.text} for i, block in enumerate(
                    block for page in page_data for block in page if block.kind != "image" and block.text.strip()
                )] if options.ai_cleanup else []
                text_estimated_input, text_estimated_output = (
                    _estimate_ai_tokens([text_items]) if text_items else (0, 0))
                candidate_count = sum(len(local_by_page[number]) for number in vision_pages)
                vision_estimated_input = len(vision_pages) * 250 + candidate_count * 120
                vision_estimated_output = candidate_count * 45
                estimated_input = text_estimated_input + vision_estimated_input
                estimated_output = text_estimated_output + vision_estimated_output
                try:
                    selected_model = ai_approval(
                        estimated_input, estimated_output, options.ai_model, len(vision_pages),
                        vision_estimated_input, vision_estimated_output,
                        options.illustration_model or options.ai_model)
                except TypeError:  # Backwards compatibility for non-GUI callers.
                    selected_model = ai_approval(
                        estimated_input, estimated_output, options.ai_model, len(vision_pages))
                if not selected_model:
                    raise ConversionCancelled("The AI processing was not approved.")
                options.ai_model = selected_model

        if options.ai_cleanup:
            try:
                _ai_cleanup(page_data, options, progress, cancel_event, lambda *_: True,
                            text_start, text_span, image_weight + epub_unit, warning)
            except ConversionCancelled:
                raise
            except (AIServiceError, RuntimeError, json.JSONDecodeError) as exc:
                warning(str(exc))
                logger.warning("Text AI failed; preserving OCR text and continuing image pipeline: %s", exc)
                _report_progress(progress, "AI text correction failed – the OCR text is kept", text_start + text_span,
                                 image_weight + epub_unit)

        accepted_by_page = {page_number: [item for item in native_by_page.get(page_number, [])
                                          if item.source == "native"]
                            for page_number in range(1, total_pages + 1)}
        vision_input = vision_output = high_detail_calls = full_page_calls = failed_pages = 0
        materialized_total = low_information_rejections = 0
        image_durations = []
        if process_images:
            for page_number, page in enumerate(pages, 1):
                local_candidates = local_by_page.get(page_number, [])
                accepted_local = local_candidates
                if options.ai_illustrations and local_candidates:
                    position = vision_pages.index(page_number) + 1
                    completed = position - 1
                    average = statistics.mean(image_durations) if image_durations else image_unit
                    fraction = image_start + image_span * completed / max(1, len(vision_pages))
                    remaining = average * (len(vision_pages)-completed) + epub_unit
                    _report_progress(
                        progress,
                        f"Verifying illustrations on PDF page {page_number} ({position} of {len(vision_pages)})",
                        fraction, remaining if len(image_durations) >= 2 else None)
                    started = time.monotonic()

                    def report_image_wait(status_message, page_position=position, begun=started):
                        elapsed = time.monotonic() - begun
                        current_average = statistics.mean(image_durations) if image_durations else image_unit
                        remaining_now = max(0, current_average-elapsed) + current_average * (
                            len(vision_pages)-page_position) + epub_unit
                        live = image_start + image_span * min(
                            len(vision_pages), page_position-1 + min(.9, elapsed/max(current_average, 1))) / max(1, len(vision_pages))
                        _report_progress(progress,
                                         f"Image page {page_position} of {len(vision_pages)} · {status_message}",
                                         live, remaining_now if len(image_durations) >= 2 else None)
                    accepted_local, usage, used_fallback = _verify_candidates_or_fallback(
                        page, local_candidates, options, cancel_event, report_image_wait, warning)
                    failed_pages += int(used_fallback)
                    vision_input += int(usage.get("input_tokens", 0) or 0)
                    vision_output += int(usage.get("output_tokens", 0) or 0)
                    high_detail_calls += int(usage.get("high_detail_calls", 0) or 0)
                    full_page_calls += int(usage.get("full_page_calls", 0) or 0)
                    image_durations.append(time.monotonic()-started)
                accepted_by_page[page_number] = _deduplicate_candidates(
                    accepted_by_page[page_number] + accepted_local)

            figure_dir = temp / "epub-images"
            illustration_pages = [number for number, candidates in accepted_by_page.items() if candidates]
            for materialize_index, page_number in enumerate(illustration_pages, 1):
                candidates = accepted_by_page[page_number]
                if not candidates:
                    continue
                _report_progress(
                    progress,
                    f"Preparing illustrations: page {materialize_index} of {len(illustration_pages)}",
                    min(.975, image_start + image_span), None)
                high_res_page = None
                if any(candidate.asset_path is None for candidate in candidates):
                    high_res_page = _render_page(
                        pdf_path, page_number, temp / f"highres-{page_number:05d}",
                        max(300, options.dpi), cancel_event, "png")
                with Image.open(pages[page_number-1]) as rendered:
                    page_width, page_height = rendered.size
                blocks = list(page_data[page_number-1])
                figures = []
                refined_count = caption_count = 0
                anchors = []
                for figure_number, candidate in enumerate(candidates[:12], 1):
                    meaningful, quality = _visual_content_quality(candidate, high_res_page)
                    if not meaningful:
                        low_information_rejections += 1
                        logger.info(
                            "Image rejected after visual quality check: page=%s source=%s bbox=%s metrics=%s",
                            page_number, candidate.source, candidate.bbox, quality)
                        continue
                    asset, final_bbox = _save_candidate_asset(
                        candidate, high_res_page, figure_dir, figure_number,
                        page_words[page_number-1], (page_width, page_height))
                    refined_count += int(final_bbox != candidate.bbox and candidate.source != "native")
                    pixel_box = _candidate_pixels(final_bbox, page_width, page_height)
                    caption_match = _caption_for_bbox(blocks, final_bbox, page_width, page_height)
                    caption = ""
                    caption_above = False
                    if caption_match:
                        caption_index, caption = caption_match[2], caption_match[3]
                        caption_above = blocks[caption_index].bottom <= pixel_box[1]
                        blocks.pop(caption_index)
                        caption_count += 1
                    anchor = _find_illustration_anchor(blocks, final_bbox, page_width, page_height)
                    anchors.append(anchor)
                    figures.append(ContentBlock(
                        "image", pixel_box[1], image=asset, bottom=pixel_box[3], anchor=anchor,
                        x1=pixel_box[0], x2=pixel_box[2], caption=caption,
                        caption_above=caption_above))
                materialized_total += len(figures)
                page_data[page_number-1] = _merge_illustrations(blocks, figures)
                logger.info(
                    "Image decisions: page=%s native_candidates=%s local_candidates=%s vision_call=%s "
                    "vision_sources=%s vision_detail=low full_page_vision=%s accepted=%s "
                    "bbox_refined=%s caption_found=%s placement_anchors=%s sources=%s",
                    page_number, len(native_by_page.get(page_number, [])),
                    len(local_by_page.get(page_number, [])),
                    bool(options.ai_illustrations and local_by_page.get(page_number)),
                    len(local_by_page.get(page_number, [])) if options.ai_illustrations else 0,
                    any((item.bbox[2]-item.bbox[0]) * (item.bbox[3]-item.bbox[1]) >= 850_000
                        for item in local_by_page.get(page_number, [])),
                    len(figures), refined_count, caption_count, anchors,
                    [item.source for item in candidates[:12]])

        logger.info(
            "Image pipeline summary: pages=%s native=%s local_candidates=%s vision_pages=%s "
            "candidate_crop_calls=%s full_page_calls=%s accepted=%s failed_pages=%s "
            "low_information_rejections=%s high_detail_calls=%s input_tokens=%s output_tokens=%s",
            total_pages, sum(map(len, native_by_page.values())), sum(map(len, local_by_page.values())),
            len(vision_pages) if options.ai_illustrations else 0,
            len(vision_pages) if options.ai_illustrations else 0, full_page_calls,
            materialized_total, failed_pages, low_information_rejections, high_detail_calls,
            vision_input, vision_output)

        if cancel_event and cancel_event.is_set():
            raise ConversionCancelled("The conversion was cancelled.")
        _report_progress(progress, "Building EPUB…", 0.985, epub_unit)
        partial_output = output_path.with_name(f".{output_path.name}.partial")
        partial_output.unlink(missing_ok=True)
        try:
            _write_epub(partial_output, page_data, options)
            validate_epub(partial_output)
            partial_output.replace(output_path)
            logger.info("Conversion completed: output=%s pages=%s", output_path.name, len(pages))
        except Exception:
            partial_output.unlink(missing_ok=True)
            raise
        _report_progress(progress, "Done", 1.0, 0)
    return output_path


def _write_epub(output: Path, pages: list[list[ContentBlock]], options: ConversionOptions) -> None:
    book_id = _safe_id(options.title)
    chapters, manifest, spine, nav_items, images = [], [], [], [], []
    image_assets = {}
    detected_chapters = _detect_chapters(pages, options.title)
    image_counter = 0
    for number, chapter_data in enumerate(detected_chapters, 1):
        blocks = chapter_data.blocks
        name = f"chapter-{number}.xhtml"
        body = [f'<h1 class="chapter-title">{html.escape(chapter_data.title)}</h1>']
        for block in blocks:
            class_name = f' class="{block.css_class}"' if block.css_class else ""
            if block.kind == "heading":
                body.append(f"<h2{class_name}>{html.escape(block.text)}</h2>")
            elif block.kind == "list":
                cleaned = re.sub(r"^(?:[•●▪◦]|[-–—]|\d+[.)])\s*", "", block.text)
                body.append(f"<ul><li>{html.escape(cleaned)}</li></ul>")
            elif block.kind == "image" and block.image:
                image_data = block.image.read_bytes()
                digest = hashlib.sha256(image_data).hexdigest()
                if digest not in image_assets:
                    image_counter += 1
                    suffix = ".png" if block.image.suffix.lower() == ".png" else ".jpg"
                    media_type = "image/png" if suffix == ".png" else "image/jpeg"
                    image_name = f"image-{image_counter:05d}{suffix}"
                    image_assets[digest] = image_name
                    images.append((image_name, image_data))
                    manifest.append(
                        f'<item id="img{image_counter}" href="images/{image_name}" media-type="{media_type}"/>')
                image_name = image_assets[digest]
                caption = (f"<figcaption>{html.escape(block.caption)}</figcaption>"
                           if block.caption else "")
                image_markup = f'<img src="../images/{image_name}" alt="Illustration"/>'
                body.append(f'<figure>{caption}{image_markup}</figure>' if block.caption_above
                            else f'<figure>{image_markup}{caption}</figure>')
            else:
                body.append(f"<p{class_name}>{html.escape(block.text)}</p>")
        chapter = ("<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                   "<html xmlns=\"http://www.w3.org/1999/xhtml\"><head>"
                   f"<title>{html.escape(chapter_data.title)}</title>"
                   "<link rel=\"stylesheet\" type=\"text/css\" href=\"../styles/book.css\"/></head>"
                   f"<body><section>{''.join(body)}</section></body></html>")
        chapters.append((name, chapter))
        manifest.append(f'<item id="c{number}" href="text/{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{number}"/>')
        nav_items.append(f'<li><a href="text/{name}">{html.escape(chapter_data.title)}</a></li>')

    nav = ("<?xml version=\"1.0\" encoding=\"utf-8\"?><html xmlns=\"http://www.w3.org/1999/xhtml\" "
           "xmlns:epub=\"http://www.idpf.org/2007/ops\"><head><title>Contents</title></head><body>"
           f"<nav epub:type=\"toc\"><h1>Contents</h1><ol>{''.join(nav_items)}</ol></nav></body></html>")
    package = ("<?xml version=\"1.0\" encoding=\"utf-8\"?><package xmlns=\"http://www.idpf.org/2007/opf\" "
               "version=\"3.0\" unique-identifier=\"book-id\"><metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\">"
               f"<dc:identifier id=\"book-id\">urn:local:{book_id}</dc:identifier><dc:title>{html.escape(options.title)}</dc:title>"
               f"<dc:language>{html.escape(options.language)}</dc:language><dc:creator>{html.escape(options.author)}</dc:creator>"
               "<meta property=\"dcterms:modified\">2026-09-04T00:00:00Z</meta></metadata><manifest>"
               '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
               '<item id="css" href="styles/book.css" media-type="text/css"/>' + "".join(manifest) +
               "</manifest><spine>" + "".join(spine) + "</spine></package>")
    css = """body{font-family:serif;line-height:1.5;margin:5%;}p{margin:0 0 .85em;text-indent:1.15em;orphans:2;widows:2}p.indented{margin-left:1.8em;margin-right:1.2em;text-indent:0}.chapter-title{font-size:1.8em;text-align:center;margin:18% 0 2em;page-break-after:avoid}h2{font-size:1.35em;margin:1.7em 0 .65em;page-break-after:avoid}ul{margin:.4em 0 .9em 1.4em}li{margin:.2em 0}img{display:block;max-width:100%;height:auto;margin:1em auto}figure{margin:1.35em auto;page-break-inside:avoid;text-align:center}figcaption{font-size:.9em;font-style:italic;line-height:1.35;margin:.45em auto 1em;max-width:92%}section{page-break-after:always}"""
    container = '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'

    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", package)
        archive.writestr("EPUB/nav.xhtml", nav)
        archive.writestr("EPUB/styles/book.css", css)
        for name, content in chapters:
            archive.writestr(f"EPUB/text/{name}", content)
        for name, content in images:
            archive.writestr(f"EPUB/images/{name}", content, compress_type=zipfile.ZIP_STORED)


def validate_epub(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        if archive.namelist()[0] != "mimetype":
            raise RuntimeError("Invalid EPUB: mimetype is in the wrong place.")
        ET.fromstring(archive.read("EPUB/package.opf"))
        ET.fromstring(archive.read("EPUB/nav.xhtml"))
        for name in archive.namelist():
            if name.endswith(".xhtml"):
                ET.fromstring(archive.read(name))
