#!/usr/bin/env python3
"""
Tip Cat Studios — Product Automation Pipeline
==============================================

Runs all 5 steps to go from design PNGs → published Shopify listings:

  Step 1: Gemini 2.5 Flash vision → metadata (title, description, 13 tags)
  Step 2: Printify API → white-background iPhone mockup JPGs
  Step 3: Gemini 3.1 Flash image gen → lifestyle mockups (table + hand scenes)
  Step 4: Shopify → product creation with variants
  Step 5: Shopify → upload 3 images per product (main + 2 lifestyle)

Designed to run as a Cloud Run Job or locally via CLI.

Usage (local):
    python product_automation_script.py                     # full pipeline
    python product_automation_script.py --step 1            # single step
    python product_automation_script.py --sku 1             # single product
    python product_automation_script.py --resume             # resume from checkpoint
    python product_automation_script.py --cleanup-shopify   # delete all products first
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from PIL import Image

# ---------------------------------------------------------------------------
# Environment & logging
# ---------------------------------------------------------------------------

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tipcat")

# ---------------------------------------------------------------------------
# Configuration (env vars — injected by Secret Manager on Cloud Run)
# ---------------------------------------------------------------------------

GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL          = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_IMAGE_MODEL    = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image-preview")

PRINTIFY_API_KEY      = os.environ.get("PRINTIFY_API_KEY", "")
PRINTIFY_SHOP_ID      = os.environ.get("PRINTIFY_SHOP_ID", "26630208")

SHOPIFY_STORE         = os.environ.get("SHOPIFY_STORE", "tipcat-studios.myshopify.com")
SHOPIFY_CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_API_VERSION   = os.environ.get("SHOPIFY_API_VERSION", "2025-01")

GCS_BUCKET            = os.environ.get("GCS_BUCKET", "tipcat-product-designs")
GOOGLE_CLOUD_PROJECT  = os.environ.get("GOOGLE_CLOUD_PROJECT", "tipcat-automation")

# GCS mount path for Cloud Run volume mounts (fallback to local dir)
DESIGNS_DIR           = os.environ.get("DESIGNS_DIR", "phonecases")
CSV_PATH              = os.environ.get("CSV_PATH", "tipcat_phonecase_sheet_with_images.csv")

# Pricing
PRODUCT_PRICE = "18.00"

# Printify blueprint & variants
PRINTIFY_BLUEPRINT = 269  # Tough Phone Cases
PRINTIFY_PROVIDER  = 1    # SPOKE Custom Products
VARIANT_MAP = {
    "iPhone 17 Pro Max": 130117,
    "iPhone 17 Air":     130118,
    "iPhone 16 Pro Max": 112813,
    "iPhone 16":         112814,
    "iPhone 11":         62582,
}

# Gemini lifestyle prompts (proven prompts from user testing)
TABLE_PROMPT = (
    "A realistic professional product photograph of an iphone tough case "
    "using the provided pregenerated mockup attached to place the product "
    "on a wooden table, face down, with a cozy cottagecore vibe, lit by "
    "sunlight. vertical 4:3 aspect ratio"
)
HAND_PROMPT = (
    "realistic iphone tough case product mockup using attached product "
    "design image held by a womans hand with acrylic nails in a sunlit "
    "scene in pov perspective"
)

# Output directories
OUTPUT_DIR          = Path("output")
METADATA_PATH       = OUTPUT_DIR / "generated_metadata.json"
MOCKUP_DIR          = OUTPUT_DIR / "mockups"
LIFESTYLE_DIR       = OUTPUT_DIR / "gemini_mockups"
STATE_PATH          = OUTPUT_DIR / "pipeline_state.json"

for d in (OUTPUT_DIR, MOCKUP_DIR, LIFESTYLE_DIR):
    d.mkdir(parents=True, exist_ok=True)


# =========================================================================
#  Shared utilities
# =========================================================================

def retry(fn, max_attempts=3, base_delay=2.0):
    """Retry with exponential backoff + jitter."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning("  Retry %d/%d after %.1fs — %s", attempt, max_attempts, delay, exc)
            time.sleep(delay)


def load_state() -> dict:
    """Load checkpoint state from disk."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict):
    """Persist checkpoint state."""
    STATE_PATH.write_text(json.dumps(state, indent=2))


def load_csv() -> List[dict]:
    """Load product CSV rows."""
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# =========================================================================
#  GCS helpers
# =========================================================================

_gcs_client = None
_gcs_bucket = None


def gcs_bucket():
    """Lazy-init GCS bucket client."""
    global _gcs_client, _gcs_bucket
    if _gcs_bucket is None:
        from google.cloud import storage
        _gcs_client = storage.Client(project=GOOGLE_CLOUD_PROJECT)
        _gcs_bucket = _gcs_client.bucket(GCS_BUCKET)
    return _gcs_bucket


def upload_to_gcs(local_path: str, blob_name: str) -> str:
    """Upload file to GCS and return public URL."""
    bucket = gcs_bucket()
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)
    blob.make_public()
    return blob.public_url


def upload_bytes_to_gcs(data: bytes, blob_name: str, content_type: str = "image/png") -> str:
    """Upload raw bytes to GCS and return public URL."""
    bucket = gcs_bucket()
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)
    blob.make_public()
    return blob.public_url


def download_from_gcs(gcs_path: str) -> str:
    """
    Download a file from GCS to local temp directory.
    Returns local file path.
    
    gcs_path: gs://bucket/path/to/file OR https://storage.googleapis.com/bucket/path
    """
    import tempfile
    import urllib.parse
    
    # Parse GCS path
    if gcs_path.startswith("gs://"):
        path_parts = gcs_path[5:].split("/", 1)
        bucket_name = path_parts[0]
        blob_name = path_parts[1] if len(path_parts) > 1 else ""
    elif "storage.googleapis.com" in gcs_path:
        parsed = urllib.parse.urlparse(gcs_path)
        parts = parsed.path.lstrip("/").split("/", 1)
        bucket_name = parts[0]
        blob_name = parts[1] if len(parts) > 1 else ""
    else:
        # Not a GCS path, return as-is
        return gcs_path
    
    # Download to temp file
    from google.cloud import storage
    client = storage.Client(project=GOOGLE_CLOUD_PROJECT)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    
    # Create temp file with same extension
    ext = Path(blob_name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        blob.download_to_filename(tmp.name)
        return tmp.name


# =========================================================================
#  Step 1 — Gemini 2.5 Flash metadata generation
# =========================================================================

METADATA_PROMPT = """You are a product description expert. Analyze this phone case design image and return ONLY a valid JSON object. No markdown, no code blocks, no explanations.

Follow this schema exactly:
{{
  "title": "max 60 chars, aesthetic-focused product name",
  "teaser": "1-2 sentence emotional appeal",
  "full_description": "3-4 sentences with benefit-oriented narrative",
  "tags": ["exactly 13 tags pulling from aesthetics, motifs, personas, occasions, emotional utility"],
  "category": "product category",
  "mood": "emotional mood/vibe",
  "design_highlights": ["highlight 1", "highlight 2", "highlight 3"],
  "finish_keywords": ["matte" or "glossy"],
  "color_keywords": ["primary colors"],
  "shopify_html": "<p>HTML formatted product description with Hook + Benefits + Technical specs</p>"
}}

Context:
- Product: {product_name}
- Theme: {design_theme}
- Primary colors: {primary_colors}
- Keywords: {existing_keywords}

Rules:
1. Return ONLY valid JSON - no other text, no markdown blocks
2. Tags must be exactly 13 items
3. Description should emphasize benefits over specs
4. Keep tone feminine, cute, aspirational
5. Include emotional-utility language (calming/joyful/mysterious)

Return the JSON object now:"""


def _extract_json(text: str) -> dict:
    """Pull JSON object from Gemini response (may be wrapped in markdown)."""
    text = text.strip()
    
    # Strategy 1: Direct JSON
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass  # Try other strategies
    
    # Strategy 2: Extract from markdown code blocks
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        json_text = m.group(1).strip()
        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            pass
    
    # Strategy 3: Find JSON object boundaries
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        json_text = m.group(0)
        try:
            return json.loads(json_text)
        except json.JSONDecodeError as e:
            # Strategy 4: Try to fix common issues
            # Remove trailing commas before closing braces/brackets
            cleaned = re.sub(r',\s*([}\]])', r'\1', json_text)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                # Log the specific error and position
                lines = json_text.split('\n')
                error_line = min(e.lineno - 1, len(lines) - 1) if hasattr(e, 'lineno') else 0
                context = lines[error_line] if error_line < len(lines) else "N/A"
                raise ValueError(
                    f"JSON parse error at line {e.lineno}, col {e.colno}: {e.msg}\n"
                    f"Context: {context[:100]}\n"
                    f"First 200 chars: {json_text[:200]}"
                )
    
    raise ValueError(f"No valid JSON found in response. First 200 chars: {text[:200]}")


def _validate_metadata(meta: dict) -> List[str]:
    """Return list of validation error strings (empty = OK)."""
    errors = []
    for key in ("title", "teaser", "full_description", "tags", "shopify_html"):
        if key not in meta:
            errors.append(f"Missing key: {key}")
    tags = meta.get("tags", [])
    if not isinstance(tags, list) or len(tags) != 13:
        errors.append(f"Tags must be a list of 13 (got {type(tags).__name__} len={len(tags) if isinstance(tags, list) else '?'})")
    title = meta.get("title", "")
    if isinstance(title, str) and len(title) > 60:
        errors.append("Title exceeds 60 chars")
    return errors


def _preprocess_image(image_path: str) -> str:
    """Resize image for Gemini if too large, return path to processed copy."""
    out_dir = OUTPUT_DIR / "preprocessed"
    out_dir.mkdir(exist_ok=True)
    with open(image_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:16]
    ext = Path(image_path).suffix.lower()
    out_ext = ".png" if ext == ".png" else ".jpg"
    out_path = out_dir / f"{digest}{out_ext}"
    if out_path.exists():
        return str(out_path)

    img = Image.open(image_path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    w, h = img.size
    if max(w, h) > 2048:
        scale = 2048 / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    if out_ext == ".jpg" and img.mode == "RGBA":
        img = img.convert("RGB")
    img.save(str(out_path), quality=92, optimize=True)
    return str(out_path)


def step1_generate_metadata(rows: List[dict], state: dict, single_sku: str = None, limit: int = None):
    """
    Step 1: For each design PNG, call Gemini 2.5 Flash to produce
    title / description / tags / shopify_html.
    """
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)

    results: List[dict] = []
    success_count = 0
    failed_count = 0
    skipped_count = 0

    # Load existing results if resuming
    if METADATA_PATH.exists():
        existing = json.loads(METADATA_PATH.read_text())
        done_skus = {r["sku"] for r in existing}
        results.extend(existing)
    else:
        done_skus = set()

    # Filter rows for processing
    rows_to_process = []
    for row in rows:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        if not sku:
            continue
        if single_sku and sku != single_sku:
            continue
        if sku in done_skus:
            skipped_count += 1
            continue
        rows_to_process.append(row)
    
    # Apply limit if specified
    if limit and len(rows_to_process) > limit:
        log.info("  Limiting to first %d products (use --limit to change)", limit)
        rows_to_process = rows_to_process[:limit]
    
    total_to_process = len(rows_to_process)
    log.info("  Processing %d products (%d already done, %d skipped)", 
             total_to_process, len(done_skus), skipped_count - len(done_skus))

    for idx, row in enumerate(rows_to_process, 1):
        sku = row.get("SKU #", row.get("SKU", "")).strip()

        image_path = row.get("Image Path", "").strip()
        if not image_path:
            log.warning("  [%s] missing image path", sku)
            state.setdefault("step1", {})[sku] = "failed_no_image"
            save_state(state)
            continue
        
        # Download from GCS if needed
        if image_path.startswith("gs://") or "storage.googleapis.com" in image_path:
            try:
                image_path = download_from_gcs(image_path)
            except Exception as exc:
                log.error("  [%s] failed to download from GCS: %s", sku, exc)
                state.setdefault("step1", {})[sku] = "failed_download"
                save_state(state)
                continue
        
        if not os.path.isfile(image_path):
            log.warning("  [%s] image not found: %s", sku, image_path)
            state.setdefault("step1", {})[sku] = "failed_no_image"
            save_state(state)
            continue

        product_name = row.get("Product Name", "")
        prompt = METADATA_PROMPT.format(
            product_name=product_name,
            design_theme=row.get("Design Theme", ""),
            primary_colors=row.get("Primary Colors", ""),
            existing_keywords=row.get("Keywords / Tags", ""),
        )
        processed = _preprocess_image(image_path)

        log.info("  [%d/%d] [%s] %s — analysing...", idx, total_to_process, sku, product_name)
        try:
            model = genai.GenerativeModel(GEMINI_MODEL)
            img = Image.open(processed)
            
            # First attempt
            def _call():
                resp = model.generate_content(
                    [prompt, img],
                    generation_config={"temperature": 0.4, "max_output_tokens": 2048},
                )
                return resp.text

            raw = retry(_call)
            
            # Try to parse JSON with retry on parse errors
            parse_attempts = 0
            max_parse_attempts = 2
            meta = None
            
            while parse_attempts < max_parse_attempts:
                try:
                    meta = _extract_json(raw)
                    break  # Success
                except (json.JSONDecodeError, ValueError) as parse_err:
                    parse_attempts += 1
                    if parse_attempts >= max_parse_attempts:
                        raise  # Give up
                    
                    log.warning("  [%s] JSON parse error (attempt %d/%d): %s", 
                              sku, parse_attempts, max_parse_attempts, str(parse_err)[:100])
                    
                    # Ask Gemini to reformat - simpler prompt
                    reformat_prompt = f"""The previous response had a JSON formatting error. Please provide ONLY a valid JSON object with no markdown, no code blocks, no extra text.

Required JSON structure:
{{
  "title": "string (max 60 chars)",
  "teaser": "string (1-2 sentences)",
  "full_description": "string (2-3 paragraphs)",
  "tags": ["exactly 13 tags"],
  "shopify_html": "<p>HTML description</p>"
}}

Product: {product_name}
Return ONLY the JSON object:"""
                    
                    resp = model.generate_content(
                        [reformat_prompt, img],
                        generation_config={"temperature": 0.2, "max_output_tokens": 2048},
                    )
                    raw = resp.text
            
            errors = _validate_metadata(meta)
            status = "success" if not errors else "needs_review"

            results.append({
                "sku": sku,
                "context": {
                    "product_name": product_name,
                    "design_theme": row.get("Design Theme", ""),
                },
                "analysis": {
                    "status": status,
                    "metadata": meta,
                    "validation_errors": errors,
                },
                "generated_timestamp": datetime.utcnow().isoformat() + "Z",
            })
            state.setdefault("step1", {})[sku] = status
            success_count += 1
            log.info("    ✓ Success (%d/%d complete)", success_count, total_to_process)
        except Exception as exc:
            log.error("  [%s] failed: %s", sku, exc)
            state.setdefault("step1", {})[sku] = f"failed: {exc}"
            failed_count += 1

        save_state(state)
        METADATA_PATH.write_text(json.dumps(results, indent=2))

    log.info("Step 1 complete — %d success, %d failed, %d skipped", 
             success_count, failed_count, skipped_count)
    log.info("  Output: %s (%d total products)", METADATA_PATH, len(results))
    return results


# =========================================================================
#  Step 2 — Printify mockup generation
# =========================================================================

def step2_generate_printify_mockups(rows: List[dict], state: dict, single_sku: str = None):
    """
    Step 2: Upload each design to GCS → Printify temp product → download mockup JPGs.
    """
    headers = {
        "Authorization": f"Bearer {PRINTIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    base = "https://api.printify.com/v1"

    mockup_meta: Dict[str, dict] = {}
    if (OUTPUT_DIR / "mockup_metadata.json").exists():
        mockup_meta = json.loads((OUTPUT_DIR / "mockup_metadata.json").read_text())

    for row in rows:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        if not sku:
            continue
        if single_sku and sku != single_sku:
            continue

        image_path = row.get("Image Path", "").strip()
        if not image_path:
            continue
        
        # Download from GCS if needed
        local_path = image_path
        if image_path.startswith("gs://") or "storage.googleapis.com" in image_path:
            try:
                local_path = download_from_gcs(image_path)
            except Exception as exc:
                log.error("  [%s] failed to download from GCS: %s", sku, exc)
                continue
        
        if not os.path.isfile(local_path):
            log.warning("  [%s] image not found: %s", sku, local_path)
            continue

        design_name = Path(local_path).stem

        # Skip if we already have all mockups for this design
        if design_name in mockup_meta and len(mockup_meta[design_name]) >= len(VARIANT_MAP):
            log.info("  [%s] mockups exist — skipping", sku)
            continue

        # Upload design to GCS
        log.info("  [%s] uploading to GCS...", sku)
        try:
            design_url = upload_to_gcs(local_path, f"designs/{design_name}.png")
        except Exception as exc:
            log.error("  [%s] GCS upload failed: %s", sku, exc)
            continue

        mockup_meta[design_name] = {}

        for model_name, variant_id in VARIANT_MAP.items():
            log.info("    %s (variant %d)...", model_name, variant_id)

            # 1. Upload image to Printify
            try:
                r = requests.post(
                    f"{base}/uploads/images.json",
                    headers=headers,
                    json={"file_name": "design.png", "url": design_url},
                    timeout=60,
                )
                r.raise_for_status()
                image_id = r.json().get("id")
            except Exception as exc:
                log.error("      Printify upload failed: %s", exc)
                continue

            # 2. Create temp product
            try:
                product_payload = {
                    "title": "Temp Mockup",
                    "description": "auto",
                    "blueprint_id": PRINTIFY_BLUEPRINT,
                    "print_provider_id": PRINTIFY_PROVIDER,
                    "variants": [{"id": variant_id, "price": 1800, "is_enabled": True}],
                    "print_areas": [{
                        "variant_ids": [variant_id],
                        "placeholders": [{
                            "position": "front",
                            "images": [{"id": image_id, "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}],
                        }],
                    }],
                }
                r = requests.post(
                    f"{base}/shops/{PRINTIFY_SHOP_ID}/products.json",
                    headers=headers,
                    json=product_payload,
                    timeout=60,
                )
                r.raise_for_status()
                product = r.json()
                product_id = product.get("id")
            except Exception as exc:
                log.error("      Printify product create failed: %s", exc)
                continue

            # 3. Extract mockup URLs and download
            mockup_urls = [img.get("src") for img in product.get("images", []) if img.get("src")]
            if mockup_urls:
                safe_model = model_name.replace(" ", "_").replace("/", "_")
                out_path = MOCKUP_DIR / f"{design_name}_{safe_model}.jpg"
                try:
                    img_r = requests.get(mockup_urls[0], timeout=30)
                    img_r.raise_for_status()
                    out_path.write_bytes(img_r.content)
                    log.info("      saved %s", out_path.name)
                    mockup_meta[design_name][model_name] = {
                        "path": str(out_path),
                        "variant_id": variant_id,
                        "url": mockup_urls[0],
                    }
                except Exception as exc:
                    log.error("      download failed: %s", exc)

            # 4. Delete temp product
            try:
                requests.delete(
                    f"{base}/shops/{PRINTIFY_SHOP_ID}/products/{product_id}.json",
                    headers=headers,
                    timeout=30,
                )
            except Exception:
                pass

            time.sleep(1)  # rate limiting

        # Save progress after each design
        (OUTPUT_DIR / "mockup_metadata.json").write_text(json.dumps(mockup_meta, indent=2))
        state.setdefault("step2", {})[sku] = "done"
        save_state(state)
        time.sleep(2)

    log.info("Step 2 complete — mockups in %s", MOCKUP_DIR)
    return mockup_meta


# =========================================================================
#  Step 3 — Gemini 3.1 Flash lifestyle mockup generation
# =========================================================================

def step3_generate_lifestyle_mockups(rows: List[dict], state: dict, single_sku: str = None):
    """
    Step 3: Take the iPhone 16 Pro Max mockup from Printify, pass it as a
    reference image to Gemini 3.1 Flash Image Preview, and generate two
    lifestyle scenes (table flat + hand holding).
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Load mockup metadata to find source images
    mockup_meta_path = OUTPUT_DIR / "mockup_metadata.json"
    if not mockup_meta_path.exists():
        log.error("No mockup_metadata.json — run step 2 first")
        return
    mockup_meta = json.loads(mockup_meta_path.read_text())

    for row in rows:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        if not sku:
            continue
        if single_sku and sku != single_sku:
            continue

        image_path = row.get("Image Path", "").strip()
        if not image_path:
            continue
        design_name = Path(image_path).stem

        # Check if already done
        table_out = LIFESTYLE_DIR / f"{sku}_table_flat.png"
        hand_out  = LIFESTYLE_DIR / f"{sku}_hand_holding.png"
        if table_out.exists() and hand_out.exists():
            log.info("  [%s] lifestyle mockups exist — skipping", sku)
            continue

        # Find the iPhone 16 Pro Max mockup as source reference
        design_data = mockup_meta.get(design_name, {})
        source_info = design_data.get("iPhone 16 Pro Max") or design_data.get("iPhone_16_Pro_Max")
        if not source_info:
            # Try any available mockup
            if design_data:
                source_info = next(iter(design_data.values()))
            else:
                log.warning("  [%s] no Printify mockups found — skipping", sku)
                continue

        source_path = source_info.get("path", "")
        if not os.path.isfile(source_path):
            log.warning("  [%s] source mockup file missing: %s", sku, source_path)
            continue

        log.info("  [%s] %s", sku, row.get("Product Name", design_name))
        source_img = Image.open(source_path)

        # --- Scene A: Table flat ---
        if not table_out.exists():
            log.info("    table scene...")
            try:
                def _gen_table():
                    resp = client.models.generate_content(
                        model=GEMINI_IMAGE_MODEL,
                        contents=[TABLE_PROMPT, source_img],
                        config=types.GenerateContentConfig(
                            response_modalities=["IMAGE"],
                            image_config=types.ImageConfig(
                                aspect_ratio="3:4",
                            ),
                        ),
                    )
                    for part in resp.candidates[0].content.parts:
                        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                            return part.inline_data.data
                    raise RuntimeError("No image in response")

                img_bytes = retry(_gen_table)
                table_out.write_bytes(img_bytes)
                log.info("    saved %s", table_out.name)
            except Exception as exc:
                log.error("    table scene failed: %s", exc)

            time.sleep(4)  # rate limiting

        # --- Scene B: Hand holding ---
        if not hand_out.exists():
            log.info("    hand scene...")
            try:
                def _gen_hand():
                    resp = client.models.generate_content(
                        model=GEMINI_IMAGE_MODEL,
                        contents=[HAND_PROMPT, source_img],
                        config=types.GenerateContentConfig(
                            response_modalities=["IMAGE"],
                            image_config=types.ImageConfig(
                                aspect_ratio="4:5",
                            ),
                        ),
                    )
                    for part in resp.candidates[0].content.parts:
                        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                            return part.inline_data.data
                    raise RuntimeError("No image in response")

                img_bytes = retry(_gen_hand)
                hand_out.write_bytes(img_bytes)
                log.info("    saved %s", hand_out.name)
            except Exception as exc:
                log.error("    hand scene failed: %s", exc)

            time.sleep(4)

        state.setdefault("step3", {})[sku] = "done" if (table_out.exists() and hand_out.exists()) else "partial"
        save_state(state)

    log.info("Step 3 complete — lifestyle mockups in %s", LIFESTYLE_DIR)


# =========================================================================
#  Step 4 — Shopify product creation
# =========================================================================

class ShopifyClient:
    """
    Minimal Shopify GraphQL + REST client.
    Uses OAuth client credentials for auth.
    """

    def __init__(self):
        self.store = SHOPIFY_STORE
        self.api_version = SHOPIFY_API_VERSION
        self.graphql_url = f"https://{self.store}/admin/api/{self.api_version}/graphql.json"
        self.access_token = self._get_token()
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token,
        })

    def _get_token(self) -> str:
        r = requests.post(
            f"https://{self.store}/admin/oauth/access_token",
            json={
                "client_id": SHOPIFY_CLIENT_ID,
                "client_secret": SHOPIFY_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    def gql(self, query: str, variables: dict = None) -> dict:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        r = self.session.post(self.graphql_url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {data['errors']}")
        return data.get("data", {})

    # -- product CRUD --

    def create_product(self, title: str, html: str, tags: List[str], variants: List[dict]) -> dict:
        """Create product → add option → set variant SKU/price via REST."""

        # 1. Create bare product
        result = self.gql("""
            mutation productCreate($input: ProductInput!) {
              productCreate(input: $input) {
                product { id title }
                userErrors { field message }
              }
            }
        """, {
            "input": {
                "title": title,
                "descriptionHtml": html,
                "tags": tags,
                "vendor": "Tip Cat Studios",
                "productType": "Phone Case",
            }
        })
        errors = result.get("productCreate", {}).get("userErrors", [])
        if errors:
            raise RuntimeError(f"productCreate errors: {errors}")
        product_id = result["productCreate"]["product"]["id"]

        # 2. Add "Model" option with variant names
        option_values = []
        seen = set()
        for v in variants:
            name = v["option1"]
            if name not in seen:
                option_values.append(name)
                seen.add(name)

        result = self.gql("""
            mutation productOptionsCreate($productId: ID!, $options: [OptionCreateInput!]!) {
              productOptionsCreate(productId: $productId, options: $options) {
                userErrors { field message }
                product {
                  id
                  variants(first: 50) {
                    edges {
                      node {
                        id
                        selectedOptions { name value }
                      }
                    }
                  }
                }
              }
            }
        """, {
            "productId": product_id,
            "options": [{"name": "Model", "values": [{"name": v} for v in option_values]}],
        })

        # Map variant GIDs by option value
        edges = (result.get("productOptionsCreate", {})
                       .get("product", {})
                       .get("variants", {})
                       .get("edges", []))
        opt_to_gid = {}
        for edge in edges:
            node = edge["node"]
            for opt in node.get("selectedOptions", []):
                if opt["name"] == "Model":
                    opt_to_gid[opt["value"]] = node["id"]

        # 3. Set SKU + price via REST per variant
        for v in variants:
            gid = opt_to_gid.get(v["option1"])
            if not gid:
                continue
            numeric_id = gid.split("/")[-1]
            rest_url = f"https://{self.store}/admin/api/{self.api_version}/variants/{numeric_id}.json"
            self.session.put(rest_url, json={
                "variant": {"id": int(numeric_id), "price": v["price"], "sku": v["sku"]}
            }, timeout=15)

        return {"id": product_id, "title": title}

    def upload_product_images(self, product_id: str, media: List[dict]) -> dict:
        """
        Upload images to a product using productCreateMedia.

        media: list of {"url": "https://...", "alt": "...", "filename": "..."}
        """
        media_input = []
        for m in media:
            media_input.append({
                "originalSource": m["url"],
                "alt": m.get("alt", ""),
                "mediaContentType": "IMAGE",
            })

        result = self.gql("""
            mutation productCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
              productCreateMedia(productId: $productId, media: $media) {
                media { alt mediaContentType status }
                mediaUserErrors { field message }
              }
            }
        """, {"productId": product_id, "media": media_input})

        errors = result.get("productCreateMedia", {}).get("mediaUserErrors", [])
        if errors:
            log.warning("  Image upload warnings: %s", errors)
        return result

    def delete_product(self, product_id: str):
        """Delete a single product."""
        self.gql("""
            mutation productDelete($input: ProductDeleteInput!) {
              productDelete(input: $input) {
                deletedProductId
                userErrors { field message }
              }
            }
        """, {"input": {"id": product_id}})

    def list_all_products(self) -> List[dict]:
        """List all products (paginated)."""
        products = []
        cursor = None
        while True:
            after = f', after: "{cursor}"' if cursor else ""
            result = self.gql(f"""
                query {{
                  products(first: 50{after}) {{
                    edges {{
                      node {{ id title }}
                      cursor
                    }}
                    pageInfo {{ hasNextPage }}
                  }}
                }}
            """)
            edges = result.get("products", {}).get("edges", [])
            for e in edges:
                products.append(e["node"])
                cursor = e["cursor"]
            if not result.get("products", {}).get("pageInfo", {}).get("hasNextPage"):
                break
        return products


def step4_create_shopify_products(rows: List[dict], state: dict, single_sku: str = None):
    """
    Step 4: Create Shopify products from metadata.
    """
    if not METADATA_PATH.exists():
        log.error("No metadata file — run step 1 first")
        return

    metadata_list = json.loads(METADATA_PATH.read_text())
    meta_by_sku = {m["sku"]: m for m in metadata_list}

    shopify = ShopifyClient()

    for row in rows:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        if not sku:
            continue
        if single_sku and sku != single_sku:
            continue

        # Check if already created
        existing = state.get("step4", {}).get(sku)
        if existing and existing.startswith("gid://"):
            log.info("  [%s] already created — skipping", sku)
            continue

        meta_entry = meta_by_sku.get(sku)
        if not meta_entry:
            log.warning("  [%s] no metadata — skipping", sku)
            continue

        analysis = meta_entry.get("analysis", {})
        meta = analysis.get("metadata", {})
        status = analysis.get("status", "unknown")

        # Determine title/html — fallback for needs_review
        if status == "success" and meta.get("title"):
            title = meta["title"]
            html = meta.get("shopify_html", f"<p>{meta.get('full_description', '')}</p>")
            tags = meta.get("tags", [])
        else:
            product_name = row.get("Product Name", f"Phone Case {sku}")
            title = f"{product_name} iPhone Case"
            html = f"<p>Beautiful {product_name} design phone case by Tip Cat Studios. Tough, glossy, protective.</p>"
            tags = ["Phone Case", "iPhone Case", "Tip Cat Studios"]
            log.warning("  [%s] using fallback metadata (status=%s)", sku, status)

        # Build variant list
        variants = []
        for model_name in VARIANT_MAP:
            safe = model_name.replace(" ", "-").lower()
            variants.append({
                "option1": model_name,
                "sku": f"TC-{sku}-{safe}",
                "price": PRODUCT_PRICE,
            })

        log.info("  [%s] creating: %s", sku, title)
        try:
            product = shopify.create_product(title, html, tags, variants)
            product_id = product["id"]
            state.setdefault("step4", {})[sku] = product_id
            save_state(state)
            log.info("    created %s", product_id)
        except Exception as exc:
            log.error("  [%s] Shopify create failed: %s", sku, exc)
            state.setdefault("step4", {})[sku] = f"failed: {exc}"
            save_state(state)

        time.sleep(0.5)

    log.info("Step 4 complete")


# =========================================================================
#  Step 5 — Upload images to Shopify products
# =========================================================================

def step5_upload_shopify_images(rows: List[dict], state: dict, single_sku: str = None):
    """
    Step 5: Upload 3 images per product to Shopify:
      1. Printify main mockup (iPhone 16 Pro Max)
      2. Gemini table lifestyle
      3. Gemini hand lifestyle
    """

    shopify = ShopifyClient()

    mockup_meta_path = OUTPUT_DIR / "mockup_metadata.json"
    mockup_meta = json.loads(mockup_meta_path.read_text()) if mockup_meta_path.exists() else {}

    metadata_list = json.loads(METADATA_PATH.read_text()) if METADATA_PATH.exists() else []
    meta_by_sku = {m["sku"]: m for m in metadata_list}

    for row in rows:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        if not sku:
            continue
        if single_sku and sku != single_sku:
            continue

        # Need product ID from step 4
        product_id = state.get("step4", {}).get(sku, "")
        if not product_id or not product_id.startswith("gid://"):
            log.warning("  [%s] no Shopify product — run step 4 first", sku)
            continue

        # Check if already uploaded
        if state.get("step5", {}).get(sku) == "done":
            log.info("  [%s] images already uploaded — skipping", sku)
            continue

        image_path = row.get("Image Path", "").strip()
        design_name = Path(image_path).stem if image_path else ""
        product_name = row.get("Product Name", design_name)

        media_items = []

        # 1. Main Printify mockup → upload to GCS
        design_data = mockup_meta.get(design_name, {})
        main_info = design_data.get("iPhone 16 Pro Max") or design_data.get("iPhone_16_Pro_Max")
        if not main_info:
            main_info = next(iter(design_data.values()), None) if design_data else None

        if main_info and os.path.isfile(main_info.get("path", "")):
            gcs_url = upload_to_gcs(main_info["path"], f"mockups/{sku}_main.jpg")
            media_items.append({
                "url": gcs_url,
                "alt": f"{product_name} iPhone Case",
            })

        # 2. Table lifestyle
        table_path = LIFESTYLE_DIR / f"{sku}_table_flat.png"
        if table_path.exists():
            gcs_url = upload_to_gcs(str(table_path), f"lifestyle/{sku}_table.png")
            media_items.append({
                "url": gcs_url,
                "alt": f"{product_name} - Table Scene",
            })

        # 3. Hand lifestyle
        hand_path = LIFESTYLE_DIR / f"{sku}_hand_holding.png"
        if hand_path.exists():
            gcs_url = upload_to_gcs(str(hand_path), f"lifestyle/{sku}_hand.png")
            media_items.append({
                "url": gcs_url,
                "alt": f"{product_name} - Lifestyle",
            })

        if not media_items:
            log.warning("  [%s] no images to upload", sku)
            continue

        log.info("  [%s] uploading %d images to Shopify...", sku, len(media_items))
        try:
            shopify.upload_product_images(product_id, media_items)
            state.setdefault("step5", {})[sku] = "done"
            save_state(state)
            log.info("    done (%d images)", len(media_items))
        except Exception as exc:
            log.error("  [%s] image upload failed: %s", sku, exc)
            state.setdefault("step5", {})[sku] = f"failed: {exc}"
            save_state(state)

        time.sleep(0.5)

    log.info("Step 5 complete")


# =========================================================================
#  Shopify cleanup
# =========================================================================

def cleanup_shopify():
    """Delete ALL products from Shopify (wipe-and-recreate strategy)."""
    shopify = ShopifyClient()
    products = shopify.list_all_products()
    log.info("Found %d products to delete", len(products))
    for p in products:
        log.info("  deleting %s — %s", p["id"], p.get("title", ""))
        try:
            shopify.delete_product(p["id"])
        except Exception as exc:
            log.error("  delete failed: %s", exc)
        time.sleep(0.3)
    log.info("Shopify cleanup complete")


# =========================================================================
#  CLI entry point
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="Tip Cat Studios Product Automation Pipeline")
    parser.add_argument("--step", type=int, choices=[1, 2, 3, 4, 5], help="Run a single step")
    parser.add_argument("--sku", type=str, help="Process a single SKU only")
    parser.add_argument("--limit", type=int, help="Process only first N products")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint (default behaviour)")
    parser.add_argument("--cleanup-shopify", action="store_true", help="Delete all Shopify products first")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    log.info("=" * 70)
    log.info("Tip Cat Studios — Product Automation Pipeline")
    log.info("=" * 70)

    # Validate critical env vars
    missing = []
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not PRINTIFY_API_KEY:
        missing.append("PRINTIFY_API_KEY")
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        missing.append("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET")

    if missing:
        log.error("Missing env vars: %s", ", ".join(missing))
        sys.exit(1)

    # Load CSV and state
    rows = load_csv()
    state = load_state()
    log.info("Loaded %d products from CSV", len(rows))

    # Optional cleanup
    if args.cleanup_shopify:
        log.info("\n--- Cleaning up Shopify ---")
        cleanup_shopify()
        # Clear step 4+5 state
        state.pop("step4", None)
        state.pop("step5", None)
        save_state(state)

    steps_to_run = [args.step] if args.step else [1, 2, 3, 4, 5]

    for step_num in steps_to_run:
        log.info("\n--- Step %d ---", step_num)
        if step_num == 1:
            step1_generate_metadata(rows, state, single_sku=args.sku, limit=args.limit)
        elif step_num == 2:
            step2_generate_printify_mockups(rows, state, single_sku=args.sku)
        elif step_num == 3:
            step3_generate_lifestyle_mockups(rows, state, single_sku=args.sku)
        elif step_num == 4:
            step4_create_shopify_products(rows, state, single_sku=args.sku)
        elif step_num == 5:
            step5_upload_shopify_images(rows, state, single_sku=args.sku)

    log.info("\n" + "=" * 70)
    log.info("Pipeline complete!")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
