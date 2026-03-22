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
    python product_automation_script.py --dry-run            # simulate — no API calls
    python product_automation_script.py --force              # re-process already-completed SKUs
    python product_automation_script.py --failed-only        # re-run only SKUs that failed last time
    python product_automation_script.py --reset-step 3       # clear step 3 state + output files
"""

import argparse
import concurrent.futures
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
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from PIL import Image

# ---------------------------------------------------------------------------
# Environment & logging
# ---------------------------------------------------------------------------

try:
    load_dotenv(override=False)
except (AssertionError, Exception) as e:
    # load_dotenv may fail in certain execution contexts (Cloud Run, subprocess, etc)
    # This is OK — env vars should be injected via Secret Manager on Cloud Run
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tipcat")

# ---------------------------------------------------------------------------
# Configuration loader (loads from JSON config files)
# ---------------------------------------------------------------------------

def load_product_config(config_name: str = "tipcat-phonecases") -> Dict[str, Any]:
    """
    Load product-specific configuration from JSON file.
    
    Args:
        config_name: Name of config file (without .json extension),
                     e.g. "tipcat-phonecases", "tipcat-mousepads"
    
    Returns:
        Dictionary with all configuration values
    
    Raises:
        FileNotFoundError: If config file doesn't exist
        json.JSONDecodeError: If config file is invalid JSON
    """
    config_path = Path(__file__).parent / "configs" / f"{config_name}.json"
    
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    try:
        with open(config_path) as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(f"Invalid JSON in {config_path}: {e.msg}", e.doc, e.pos)
    
    # Validate required top-level keys
    required_keys = ["name", "product", "store", "gcs", "printify", "gemini", "prompts", "shopify"]
    missing = [k for k in required_keys if k not in config]
    if missing:
        raise ValueError(f"Config {config_path} missing required keys: {missing}")
    
    return config

def apply_config(config: Dict[str, Any]) -> None:
    """
    Apply configuration from loaded JSON config file to global variables.
    Overrides environment variables and hardcoded defaults.
    
    Args:
        config: Configuration dictionary loaded from JSON
    """
    global GEMINI_API_KEY, GEMINI_MODEL, GEMINI_IMAGE_MODEL
    global PRINTIFY_API_KEY, PRINTIFY_SHOP_ID, PRINTIFY_BLUEPRINT, VARIANT_MAP
    global PRINTFUL_API_KEY, PRINTFUL_STORE_ID, PRINTFUL_PRODUCT_ID, PRINTFUL_VARIANT_MAP
    global SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_API_VERSION
    global GCS_BUCKET, GOOGLE_CLOUD_PROJECT
    global PRODUCT_PRICE, TABLE_PROMPT, HAND_PROMPT
    global METADATA_PROMPT
    
    # Gemini configuration
    GEMINI_MODEL = config["gemini"].get("model", "gemini-2.5-flash")
    GEMINI_IMAGE_MODEL = config["gemini"].get("image_model", "gemini-3.1-flash-image-preview")
    GEMINI_API_KEY = os.environ.get(config["gemini"]["api_key_env"], "")
    
    # Printify configuration
    PRINTIFY_API_KEY = os.environ.get(config["printify"]["api_key_env"], "")
    PRINTIFY_SHOP_ID = config["printify"].get("shop_id", "")
    PRINTIFY_BLUEPRINT = config["printify"].get("blueprint_id", 269)
    VARIANT_MAP = config["printify"].get("variants", {})
    PRODUCT_PRICE = config["printify"].get("price", "18.00")

    # Printful configuration (optional, used for fulfillment sync)
    printful_cfg = config.get("printful", {}) if isinstance(config, dict) else {}
    if printful_cfg:
        PRINTFUL_API_KEY = os.environ.get(printful_cfg.get("api_key_env", "PRINTFUL_API_KEY"), "")
        PRINTFUL_STORE_ID = os.environ.get(printful_cfg.get("store_id_env", "PRINTFUL_STORE_ID"), "") or str(printful_cfg.get("store_id", ""))
        PRINTFUL_PRODUCT_ID = int(printful_cfg.get("product_id", 0) or 0)
        PRINTFUL_VARIANT_MAP = printful_cfg.get("variants", {}) if isinstance(printful_cfg.get("variants", {}), dict) else {}
    else:
        PRINTFUL_API_KEY = os.environ.get("PRINTFUL_API_KEY", "")
        PRINTFUL_STORE_ID = os.environ.get("PRINTFUL_STORE_ID", "")
        PRINTFUL_PRODUCT_ID = 0
        PRINTFUL_VARIANT_MAP = {}
    
    # Shopify configuration
    SHOPIFY_STORE = config["store"]["url"]
    SHOPIFY_CLIENT_ID = os.environ.get(config["store"]["client_id_env"], "")
    SHOPIFY_CLIENT_SECRET = os.environ.get(config["store"]["client_secret_env"], "")
    SHOPIFY_API_VERSION = config["store"].get("api_version", "2025-01")
    
    # GCS configuration
    GCS_BUCKET = config["gcs"]["bucket"]
    GOOGLE_CLOUD_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "tipcat-automation")
    
    # Prompts - substitute product name into templates
    product_type = config["product"]["type"]
    product_name = config["product"]["name"]
    
    TABLE_PROMPT = config["prompts"]["lifestyle_table"].replace(
        "{product_name}", product_name
    ).replace("{product_type}", product_type)
    
    HAND_PROMPT = config["prompts"]["lifestyle_hand"].replace(
        "{product_name}", product_name
    ).replace("{product_type}", product_type)
    
    METADATA_PROMPT = config["prompts"]["metadata"].replace(
        "{product_name}", product_name
    ).replace("{product_type}", product_type)
    
    log.info(f"✓ Applied config: {config['name']}")
    log.info(f"  Product: {product_type}")
    log.info(f"  Store: {SHOPIFY_STORE}")
    log.info(f"  GCS bucket: {GCS_BUCKET}")
    log.info(f"  Printify shop: {PRINTIFY_SHOP_ID}")
    log.info(f"  Variants: {list(VARIANT_MAP.keys())}")
    if PRINTFUL_STORE_ID:
        log.info(f"  Printful store: {PRINTFUL_STORE_ID}")

# ---------------------------------------------------------------------------
# Configuration (env vars — injected by Secret Manager on Cloud Run)
# ---------------------------------------------------------------------------

GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL          = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_IMAGE_MODEL    = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image-preview")

PRINTIFY_API_KEY      = os.environ.get("PRINTIFY_API_KEY", "")
PRINTIFY_SHOP_ID      = os.environ.get("PRINTIFY_SHOP_ID", "26630208")

PRINTFUL_API_KEY      = os.environ.get("PRINTFUL_API_KEY", "")
PRINTFUL_STORE_ID     = os.environ.get("PRINTFUL_STORE_ID", "")
PRINTFUL_PRODUCT_ID   = int(os.environ.get("PRINTFUL_PRODUCT_ID", "0") or 0)
PRINTFUL_VARIANT_MAP  = {}
PRINTFUL_SYNC_WAIT_SECONDS = int(os.environ.get("PRINTFUL_SYNC_WAIT_SECONDS", "600"))
PRINTFUL_SYNC_POLL_SECONDS = int(os.environ.get("PRINTFUL_SYNC_POLL_SECONDS", "20"))

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

# Telegram approval settings
TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
APPROVAL_TIMEOUT      = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "600"))
APPROVAL_MAX_REGEN    = int(os.environ.get("APPROVAL_MAX_REGENERATIONS", "3"))


# =========================================================================
#  Telegram approval helper (GCS-based signaling)
# =========================================================================

class TelegramApproval:
    """
    Send generated content (metadata or images) to Telegram for human review.

    The pipeline sends a message with inline-keyboard buttons (Approve / Regenerate),
    writes a pending request to GCS, then polls GCS until the Telegram bot writes
    back a decision.  Completely decoupled from the bot's own update loop.
    """

    def __init__(self, enabled: bool = True):
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.api = f"https://api.telegram.org/bot{self.bot_token}"
        self.enabled = enabled and bool(self.bot_token and self.chat_id)
        self._bucket = None
        if self.enabled:
            log.info("✓ Telegram approval enabled (chat %s)", self.chat_id)
        else:
            log.info("  Telegram approval disabled — will auto-approve all content")

    # -- GCS helpers --

    @property
    def bucket(self):
        if self._bucket is None:
            from google.cloud import storage as _gcs
            self._bucket = _gcs.Client(project=GOOGLE_CLOUD_PROJECT).bucket(GCS_BUCKET)
        return self._bucket

    def _write_request(self, request_id: str, data: dict):
        blob = self.bucket.blob(f"approvals/{request_id}.json")
        blob.upload_from_string(json.dumps(data), content_type="application/json")

    def _read_request(self, request_id: str) -> Optional[dict]:
        blob = self.bucket.blob(f"approvals/{request_id}.json")
        try:
            return json.loads(blob.download_as_text())
        except Exception:
            return None

    def _delete_request(self, request_id: str):
        try:
            self.bucket.blob(f"approvals/{request_id}.json").delete()
        except Exception:
            pass

    # -- Telegram API helpers --

    def _send_message(self, text: str, reply_markup: dict = None) -> Optional[int]:
        """Send a text message; returns message_id on success."""
        payload: dict = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            r = requests.post(f"{self.api}/sendMessage", json=payload, timeout=15)
            return r.json().get("result", {}).get("message_id")
        except Exception as exc:
            log.warning("  Telegram sendMessage failed: %s", exc)
            return None

    def _send_photo(self, photo_path: str, caption: str = "") -> Optional[int]:
        """Send a photo; returns message_id on success."""
        try:
            with open(photo_path, "rb") as f:
                data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
                r = requests.post(f"{self.api}/sendPhoto", data=data, files={"photo": f}, timeout=30)
                return r.json().get("result", {}).get("message_id")
        except Exception as exc:
            log.warning("  Telegram sendPhoto failed: %s", exc)
            return None

    def _build_keyboard(self, request_id: str) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{request_id}"},
                {"text": "🔄 Regenerate", "callback_data": f"regen:{request_id}"},
            ]]
        }

    # -- Poll GCS for decision --

    def _poll_decision(self, request_id: str, timeout: int = APPROVAL_TIMEOUT) -> str:
        """Block until the bot writes a decision or timeout is reached."""
        start = time.time()
        while time.time() - start < timeout:
            data = self._read_request(request_id)
            if data and data.get("status") in ("approved", "regenerate"):
                log.info("    Telegram decision: %s", data["status"])
                self._delete_request(request_id)
                return data["status"]
            time.sleep(5)
        log.warning("  Approval timeout (%ds) — auto-approving", timeout)
        self._delete_request(request_id)
        return "approved"

    # -- Public API --

    def request_metadata_approval(self, sku: str, metadata: dict, attempt: int = 1) -> str:
        """
        Send generated metadata to Telegram for review.
        Returns 'approved' or 'regenerate'.
        """
        if not self.enabled:
            return "approved"

        request_id = f"meta_{sku}_{int(time.time())}"
        title = metadata.get("title", "(no title)")
        teaser = metadata.get("teaser", "")
        tags = metadata.get("tags", [])
        desc = metadata.get("full_description", "")
        # Truncate description for Telegram (max ~4096 chars per msg)
        if len(desc) > 500:
            desc = desc[:500] + "…"

        msg = (
            f"📝 <b>Metadata Review — SKU {sku}</b>  (attempt {attempt})\n\n"
            f"<b>Title:</b> {title}\n\n"
            f"<b>Teaser:</b> {teaser}\n\n"
            f"<b>Description:</b>\n{desc}\n\n"
            f"<b>Tags ({len(tags)}):</b> {', '.join(str(t) for t in tags[:13])}"
        )

        self._write_request(request_id, {"status": "pending", "type": "metadata", "sku": sku})
        self._send_message(msg, reply_markup=self._build_keyboard(request_id))
        return self._poll_decision(request_id)

    def request_image_approval(self, sku: str, image_paths: List[str], attempt: int = 1) -> str:
        """
        Send generated lifestyle images to Telegram for review.
        Returns 'approved' or 'regenerate'.
        """
        if not self.enabled:
            return "approved"

        request_id = f"img_{sku}_{int(time.time())}"

        # Send each image as a photo
        for i, path in enumerate(image_paths):
            label = Path(path).stem.replace(f"{sku}_", "").replace("_", " ").title()
            self._send_photo(path, caption=f"SKU {sku} — {label}")

        msg = (
            f"🖼 <b>Lifestyle Image Review — SKU {sku}</b>  (attempt {attempt})\n"
            f"Approve these {len(image_paths)} lifestyle images?"
        )
        self._write_request(request_id, {"status": "pending", "type": "image", "sku": sku})
        self._send_message(msg, reply_markup=self._build_keyboard(request_id))
        return self._poll_decision(request_id)


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
    """Persist checkpoint state locally and to GCS so the Telegram bot can read it."""
    STATE_PATH.write_text(json.dumps(state, indent=2))
    try:
        os.chmod(STATE_PATH, 0o600)
    except Exception:
        pass
    # Mirror to GCS (non-blocking best-effort) so the Telegram bot /status works
    try:
        from google.cloud import storage as _gcs
        _bucket = _gcs.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "tipcat-automation")) \
            .bucket(os.environ.get("GCS_BUCKET", "tipcat-product-designs"))
        _bucket.blob("output/pipeline_state.json").upload_from_string(
            STATE_PATH.read_text(), content_type="application/json"
        )
    except Exception as _e:
        log.debug("save_state GCS sync skipped: %s", _e)


def _needs_retry(step_key: str, sku: str, state: dict) -> bool:
    """Return True if a SKU should be retried (failed, partial, or never attempted)."""
    val = state.get(step_key, {}).get(sku, "")
    return not val or val.startswith("failed") or val == "partial"


def _is_step_success(step_num: int, value: str) -> bool:
    if not isinstance(value, str):
        return False
    if step_num == 1:
        return value == "success"
    if step_num in (2, 3, 5):
        return value == "done"
    if step_num == 4:
        return value.startswith("gid://") or value.startswith("printful:")
    return False


def _is_step_issue(step_num: int, value: str) -> bool:
    if not value:
        return True
    if isinstance(value, str) and value.startswith("failed"):
        return True
    if value in ("partial", "needs_review"):
        return True
    return not _is_step_success(step_num, value)


def _scoped_rows(rows: List[dict], single_sku: Optional[str] = None, limit: Optional[int] = None) -> List[dict]:
    scoped = []
    for row in rows:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        if not sku:
            continue
        if single_sku and sku != single_sku:
            continue
        scoped.append(row)
    if limit and limit > 0:
        return scoped[:limit]
    return scoped


def verify_step_completion(step_num: int, rows: List[dict], state: dict, single_sku: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    step_key = f"step{step_num}"
    scoped = _scoped_rows(rows, single_sku=single_sku, limit=limit)
    total = len(scoped)
    success = 0
    issues = []

    for row in scoped:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        value = state.get(step_key, {}).get(sku, "")
        if _is_step_success(step_num, value):
            success += 1
        else:
            issues.append((sku, value or "pending"))

    return {
        "step": step_num,
        "step_key": step_key,
        "total": total,
        "success": success,
        "issues": len(issues),
        "issue_examples": issues[:10],
        "ok": (issues == []),
    }


def classify_row_status(step_values: Dict[str, str]) -> Tuple[str, str, str]:
    """Return (overall_status, blocking_step, blocking_reason)."""
    for step_num in range(1, 6):
        key = f"step{step_num}"
        val = step_values.get(key, "")
        if _is_step_success(step_num, val):
            continue
        if not val:
            return ("blocked", key, "pending")
        return ("blocked", key, val)
    return ("complete", "", "")


def compute_file_hash(path: str) -> str:
    """Compute SHA256 hash of a file — used for detecting design PNG changes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_png_for_printify(local_path: str, sku: str) -> bool:
    """
    Validate a PNG meets Printify requirements before uploading.
    - Minimum 1500x1500 px (hard block)
    - Below 2000x2000 gets a warning
    - File size must be < 200 MB
    Returns True if the file is safe to upload, False to skip.
    """
    file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
    if file_size_mb > 200:
        log.error("  [%s] image too large (%.1f MB > 200 MB limit) — skipping", sku, file_size_mb)
        return False
    try:
        with Image.open(local_path) as img:
            w, h = img.size
            mode = img.mode
        if w < 1500 or h < 1500:
            log.error("  [%s] image too small (%dx%d) — Printify min is 1500x1500 — skipping", sku, w, h)
            return False
        if w < 2000 or h < 2000:
            log.warning("  [%s] image %dx%d is above minimum but below recommended 2000x2000", sku, w, h)
        if mode not in ("RGB", "RGBA"):
            log.warning("  [%s] image mode is %s — may need conversion before upload", sku, mode)
        log.info("  [%s] validated: %dx%d %s %.1f MB ✓", sku, w, h, mode, file_size_mb)
    except Exception as exc:
        log.error("  [%s] could not read image for validation: %s — skipping", sku, exc)
        return False
    return True


def write_pipeline_report(rows: List[dict], state: dict) -> str:
    """
    Write output/pipeline_report.csv with per-SKU status for all 5 steps.
    Also uploads a copy to GCS at output/pipeline_report.csv.
    """
    report_path = OUTPUT_DIR / "pipeline_report.csv"
    metadata_list = json.loads(METADATA_PATH.read_text()) if METADATA_PATH.exists() else []
    meta_by_sku = {m["sku"]: m for m in metadata_list}
    fieldnames = [
        "sku", "design_name", "title",
        "step1", "step2", "step3", "step4", "step5",
        "overall_status", "blocking_step", "blocking_reason",
        "shopify_url", "report_time"
    ]
    rows_out = []
    report_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for row in rows:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        if not sku:
            continue
        image_path = row.get("Image Path", "")
        design_name = Path(image_path).stem if image_path else ""
        meta = meta_by_sku.get(sku, {})
        title = meta.get("analysis", {}).get("metadata", {}).get("title", row.get("Product Name", ""))
        step4_val = state.get("step4", {}).get(sku, "")
        shopify_url = ""
        if step4_val and step4_val.startswith("gid://"):
            numeric_id = step4_val.split("/")[-1]
            shopify_url = f"https://{SHOPIFY_STORE}/admin/products/{numeric_id}"
        row_steps = {
            "step1": state.get("step1", {}).get(sku, "pending"),
            "step2": state.get("step2", {}).get(sku, "pending"),
            "step3": state.get("step3", {}).get(sku, "pending"),
            "step4": step4_val or "pending",
            "step5": state.get("step5", {}).get(sku, "pending"),
        }
        overall_status, blocking_step, blocking_reason = classify_row_status(row_steps)

        rows_out.append({
            "sku": sku,
            "design_name": design_name,
            "title": title,
            "step1": row_steps["step1"],
            "step2": row_steps["step2"],
            "step3": row_steps["step3"],
            "step4": row_steps["step4"],
            "step5": row_steps["step5"],
            "overall_status": overall_status,
            "blocking_step": blocking_step,
            "blocking_reason": blocking_reason,
            "shopify_url": shopify_url,
            "report_time": report_time,
        })
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)
    log.info("Report written: %s (%d rows)", report_path, len(rows_out))
    done = sum(1 for r in rows_out if r["overall_status"] == "complete")
    blocked = sum(1 for r in rows_out if r["overall_status"] == "blocked")
    failed = sum(1 for r in rows_out if any(str(r[f"step{n}"]).startswith("failed") for n in range(1, 6)))
    log.info("  Summary: %d fully done, %d blocked, %d with explicit failures, %d total", done, blocked, failed, len(rows_out))
    try:
        upload_to_gcs(str(report_path), "output/pipeline_report.csv")
        log.info("  Uploaded to GCS: output/pipeline_report.csv")
    except Exception as exc:
        log.warning("  Could not upload report to GCS: %s", exc)
    return str(report_path)


def load_csv() -> List[dict]:
    """DEPRECATED: Load product CSV rows."""
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# =========================================================================
#  Stable SKU Map  —  persistent design → SKU assignment in GCS
# =========================================================================
#  File: gs://<bucket>/sku_map.json
#  {
#    "next_sku": 100,
#    "designs": {
#      "Bug_pattern.png": {"sku": "1", "status": "active", "added": "..."},
#      "newdesign.png":   {"sku": "99", "status": "pending", "added": "..."},
#    }
#  }
#
#  status values:
#    pending  – newly discovered, not yet processed
#    active   – being processed or already finished
#    skip     – user excluded via /design skip <name>
#    archived – design file deleted from GCS
# =========================================================================

SKU_MAP_BLOB = "sku_map.json"
SKU_MAP_LOCAL = OUTPUT_DIR / "sku_map.json"


def _load_sku_map() -> dict:
    """Load the persistent SKU map from GCS (falls back to local cache)."""
    try:
        blob = gcs_bucket().blob(SKU_MAP_BLOB)
        if blob.exists():
            data = json.loads(blob.download_as_text())
            # Also cache locally
            SKU_MAP_LOCAL.write_text(json.dumps(data, indent=2))
            return data
    except Exception as exc:
        log.warning("_load_sku_map: GCS read failed (%s), trying local cache", exc)
    # Fallback to local file
    if SKU_MAP_LOCAL.exists():
        return json.loads(SKU_MAP_LOCAL.read_text())
    return {"next_sku": 1, "designs": {}}


def _save_sku_map(sku_map: dict) -> None:
    """Persist the SKU map to GCS and local cache."""
    payload = json.dumps(sku_map, indent=2)
    SKU_MAP_LOCAL.write_text(payload)
    try:
        gcs_bucket().blob(SKU_MAP_BLOB).upload_from_string(
            payload, content_type="application/json"
        )
    except Exception as exc:
        log.warning("_save_sku_map: GCS write failed: %s", exc)


def _scan_gcs_designs() -> Dict[str, str]:
    """Scan GCS designs/ folder and return {filename: full_blob_path}."""
    from google.cloud import storage
    client = storage.Client(project=GOOGLE_CLOUD_PROJECT)
    bucket = client.bucket(GCS_BUCKET)
    blobs = list(bucket.list_blobs(prefix="designs/"))
    found = {}
    for blob in blobs:
        # Skip subfolders (only top-level files in designs/)
        if blob.name.count("/") > 1:
            continue
        if not blob.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        filename = blob.name.split("/")[-1]
        found[filename] = blob.name
    return found


def sync_sku_map(sku_map: dict = None, save: bool = True) -> dict:
    """
    Synchronise the SKU map with actual GCS bucket contents.
      • New files in GCS → added as 'pending' with next available SKU.
      • Files removed from GCS → marked 'archived' (SKU never recycled).
      • Existing entries are never renumbered.
    Returns the updated map.
    """
    if sku_map is None:
        sku_map = _load_sku_map()

    gcs_files = _scan_gcs_designs()
    designs = sku_map.setdefault("designs", {})
    next_sku = sku_map.get("next_sku", 1)
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    changed = False

    # 1. Register any new files found in GCS
    for filename in sorted(gcs_files.keys()):
        if filename not in designs:
            designs[filename] = {
                "sku": str(next_sku),
                "status": "pending",
                "added": now,
            }
            log.info("  SKU map: new design %s → SKU %d (pending)", filename, next_sku)
            next_sku += 1
            changed = True

    # 2. Mark removed files as archived (but keep their SKU)
    for filename, entry in designs.items():
        if filename not in gcs_files and entry.get("status") not in ("archived", "skip"):
            entry["status"] = "archived"
            log.info("  SKU map: %s (SKU %s) → archived (file removed from GCS)", filename, entry["sku"])
            changed = True

    sku_map["next_sku"] = next_sku

    if changed and save:
        _save_sku_map(sku_map)

    return sku_map


def list_designs_from_gcs(include_all: bool = False) -> List[dict]:
    """
    Build pipeline rows from the stable SKU map.

    By default returns only 'active' and 'pending' designs.
    Set include_all=True to also include 'skip' / 'archived'.
    """
    sku_map = sync_sku_map()     # discover new files, persist
    gcs_files = _scan_gcs_designs()
    designs = sku_map.get("designs", {})

    rows = []
    for filename, entry in designs.items():
        status = entry.get("status", "pending")
        if not include_all and status in ("skip", "archived"):
            continue
        sku = entry["sku"]
        blob_path = gcs_files.get(filename, f"designs/{filename}")
        rows.append({
            "SKU #": sku,
            "SKU": sku,
            "Image Path": f"gs://{GCS_BUCKET}/{blob_path}",
            "Product Name": "",
            "Design Theme": "",
            "Primary Colors": "",
            "Keywords / Tags": "",
            "_design_file": filename,
            "_design_status": status,
        })

    # Sort by numeric SKU so log output is in order
    rows.sort(key=lambda r: int(r["SKU #"]))
    return rows


def set_design_status(filename: str, status: str) -> str:
    """Change a design's status in the SKU map. Returns a confirmation message."""
    if status not in ("active", "pending", "skip", "archived"):
        return f"Invalid status '{status}'. Use: active, pending, skip, archived."
    sku_map = _load_sku_map()
    designs = sku_map.get("designs", {})
    if filename not in designs:
        # Try matching without extension or partial match
        matches = [k for k in designs if k.startswith(filename) or k.replace(".png", "") == filename]
        if len(matches) == 1:
            filename = matches[0]
        elif matches:
            return f"Ambiguous: {', '.join(matches)}"
        else:
            return f"Design '{filename}' not found in SKU map."
    old_status = designs[filename].get("status")
    designs[filename]["status"] = status
    _save_sku_map(sku_map)
    return f"{filename} (SKU {designs[filename]['sku']}): {old_status} → {status}"


def get_sku_map_summary() -> dict:
    """Return summary counts by status."""
    sku_map = _load_sku_map()
    designs = sku_map.get("designs", {})
    counts = {"pending": 0, "active": 0, "skip": 0, "archived": 0, "total": len(designs)}
    for entry in designs.values():
        s = entry.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    counts["next_sku"] = sku_map.get("next_sku", 1)
    return counts


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


def upload_to_gcs(local_path: str, blob_name: str, public: bool = False) -> str:
    """Upload file to GCS and return gs:// path (or public URL when public=True)."""
    bucket = gcs_bucket()
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)
    if public:
        blob.make_public()
        return blob.public_url
    return f"gs://{bucket.name}/{blob_name}"


def upload_bytes_to_gcs(data: bytes, blob_name: str, content_type: str = "image/png", public: bool = False) -> str:
    """Upload raw bytes to GCS and return gs:// path (or public URL when public=True)."""
    bucket = gcs_bucket()
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)
    if public:
        blob.make_public()
        return blob.public_url
    return f"gs://{bucket.name}/{blob_name}"


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
  "full_description": "1-2 sentences only — a concise, benefit-oriented hook about this specific design. Do NOT list features or specs.",
  "tags": ["exactly 13 tags pulling from aesthetics, motifs, personas, occasions, emotional utility"],
  "category": "product category",
  "mood": "emotional mood/vibe",
  "design_highlights": ["highlight 1", "highlight 2", "highlight 3"],
  "finish_keywords": ["matte" or "glossy"],
  "color_keywords": ["primary colors"],
  "shopify_html": "<p>Short HTML description — 1-2 sentences about this design only. Do NOT include features, specs, shipping info, or store info.</p>"
}}

Rules:
1. Analyze ONLY the image - generate all metadata from visual analysis
2. Return ONLY valid JSON - no other text, no markdown blocks
3. Tags must be exactly 13 items
4. full_description and shopify_html must be SHORT (1-2 sentences max) — only describe the design itself
5. Keep tone feminine, cute, aspirational
6. Include emotional-utility language (calming/joyful/mysterious)
7. Identify colors, themes, motifs, and style purely from the design
8. Do NOT include any product features, specs, shipping details, or boilerplate — those are added automatically

Return the JSON object now:"""

# Standard boilerplate appended to every Shopify product description
SHOPIFY_DESCRIPTION_BOILERPLATE = """
<br><br>
<p>Tipcat Studios is a small family business formed in 2025. We offer cute and trendy phone cases that are unique and hopefully bring you joy. Thank you for supporting us!</p>

<p><strong>Key Features:</strong></p>
<ul>
  <li>Supports wireless charging (not including MagSafe)</li>
  <li>UV Protected</li>
  <li>Dual Layer Design: strong, durable, impact resistant, and shock absorbent</li>
  <li>Glossy or Matte Finish</li>
  <li>Material is 100% polycarbonate shell and 100% TPU silicone lining</li>
  <li>Interior rubber liner for extra protection (appearance may vary across phone models)</li>
  <li>Clear open ports for connectivity</li>
</ul>

<p><strong>When shopping with our store, please keep in mind:</strong></p>
<ul>
  <li>Items typically ship within 2-7 business days after your order is received</li>
  <li>The colors on the case may vary slightly from the listing photo due to the printing technology</li>
  <li>Some design elements may be cut off depending on the phone case model due to differing sizes/shapes of each phone case</li>
  <li>Please double check your address. Make sure that your saved address on Etsy is your current address. Your order will ship by default to your saved address.</li>
</ul>

<p>🔄 <strong>Need to Return Your Case?</strong> To support our commitment to sustainability and waste reduction, we minimize returns. Returns are accepted only for products that are faulty, defective, or damaged in transit, as each product is custom-made and not mass-produced.</p>

<p>💬 <strong>Questions?</strong> Message us and we'll be happy to help.</p>
""".strip()


def _extract_json(text: str) -> dict:
    """Pull JSON object from Gemini response (may be wrapped in markdown)."""
    text = text.strip()
    
    # Strategy 1: Direct JSON
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass  # Try other strategies
    
    # Strategy 2: Extract from markdown code blocks (various formats)
    for pattern in [
        r"```(?:json)?\s*([\s\S]*?)```",  # Standard markdown
        r"```\s*\n([\s\S]*?)\n```",          # With newlines
        r"^```json\s*$\n([\s\S]*?)\n^```",  # json on separate line (multiline)
    ]:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            json_text = m.group(1).strip()
            try:
                return json.loads(json_text)
            except json.JSONDecodeError:
                continue  # Try next pattern
    
    # Strategy 2b: Look for any ```...``` block
    m = re.search(r"```([\s\S]*?)```", text)
    if m:
        json_text = m.group(1).strip()
        # Remove language identifier if present (e.g., "json" on first line)
        lines = json_text.split('\n')
        if lines and not lines[0].strip().startswith('{'):
            json_text = '\n'.join(lines[1:])
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


def step1_generate_metadata(rows: List[dict], state: dict, single_sku: str = None, limit: int = None, dry_run: bool = False, force: bool = False, failed_only: bool = False, approval: TelegramApproval = None):
    """
    Step 1: For each design PNG, call Gemini 2.5 Flash to produce
    title / description / tags / shopify_html.
    If a TelegramApproval instance is provided, sends metadata to Telegram
    for human review; regenerates if rejected (up to APPROVAL_MAX_REGEN times).
    """
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY)

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
        if sku in done_skus and not force:
            skipped_count += 1
            continue
        if failed_only and not _needs_retry("step1", sku, state):
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

        if dry_run:
            log.info("  [DRY RUN] [%s] would call Gemini for metadata (design: %s)", sku, image_path)
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

        # Use image-only analysis - no CSV context
        prompt = METADATA_PROMPT
        processed = _preprocess_image(image_path)

        image_filename = os.path.basename(image_path)
        log.info("  [%d/%d] [%s] %s — analysing image only...", idx, total_to_process, sku, image_filename)
        try:
            img = Image.open(processed)

            # --- Approval loop: generate → review → regenerate if needed ---
            max_regen = APPROVAL_MAX_REGEN if approval else 0
            meta = None
            for regen_attempt in range(1, max_regen + 2):  # at least 1 attempt
                # Generate metadata via Gemini
                def _call():
                    resp = client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=[prompt, img],
                        config=types.GenerateContentConfig(
                            temperature=0.4,
                            max_output_tokens=2048,
                        ),
                    )
                    return resp.text

                raw = retry(_call)

                # Try to parse JSON with retry on parse errors
                parse_attempts = 0
                max_parse_attempts = 2

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
                        reformat_prompt = """The previous response had a JSON formatting error. Please provide ONLY a valid JSON object with no markdown, no code blocks, no extra text.

Required JSON structure:
{
  "title": "string (max 60 chars)",
  "teaser": "string (1-2 sentences)",
  "full_description": "string (1-2 sentences about the design only)",
  "tags": ["exactly 13 tags"],
  "shopify_html": "<p>1-2 sentences about the design only</p>"
}

Return ONLY the JSON object:"""

                        resp = client.models.generate_content(
                            model=GEMINI_MODEL,
                            contents=[reformat_prompt, img],
                            config=types.GenerateContentConfig(
                                temperature=0.2,
                                max_output_tokens=2048,
                            ),
                        )
                        raw = resp.text

                errors = _validate_metadata(meta)

                # --- Telegram approval gate ---
                human_approved = False
                if approval and regen_attempt <= max_regen:
                    decision = approval.request_metadata_approval(sku, meta, attempt=regen_attempt)
                    if decision == "regenerate":
                        log.info("    ↻ Regenerating metadata for SKU %s (attempt %d/%d)", sku, regen_attempt, max_regen)
                        continue  # loop back and re-call Gemini
                    human_approved = True  # user explicitly approved
                break  # approved (or no approval object, or last attempt)

            # Human approval overrides validation issues
            if human_approved:
                status = "success"
            else:
                status = "success" if not errors else "needs_review"

            results.append({
                "sku": sku,
                "gcs_path": row.get("Image Path", ""),
                "analysis": {
                    "status": status,
                    "metadata": meta,
                    "validation_errors": errors,
                },
                "generated_timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            })
            state.setdefault("step1", {})[sku] = status
            success_count += 1
            log.info("    ✓ Success (%d/%d complete)", success_count, total_to_process)
            # Mark design as active in SKU map now that it has metadata
            design_file = row.get("_design_file", "")
            if design_file:
                set_design_status(design_file, "active")
        except Exception as exc:
            log.error("  [%s] failed: %s", sku, exc)
            state.setdefault("step1", {})[sku] = f"failed: {exc}"
            failed_count += 1

        save_state(state)
        METADATA_PATH.write_text(json.dumps(results, indent=2))

    log.info("Step 1 complete — %d success, %d failed, %d skipped", 
             success_count, failed_count, skipped_count)
    log.info("  Output: %s (%d total products)", METADATA_PATH, len(results))
    
    # Upload metadata to GCS for persistence
    try:
        gcs_metadata_path = upload_to_gcs(str(METADATA_PATH), "output/generated_metadata.json")
        log.info("  ✓ Uploaded to GCS: %s", gcs_metadata_path)
    except Exception as exc:
        log.warning("  Could not upload to GCS: %s", exc)
    
    return results


# =========================================================================
#  Step 2 — Printify mockup generation
# =========================================================================

def step2_generate_printify_mockups(rows: List[dict], state: dict, single_sku: str = None, dry_run: bool = False, force: bool = False, failed_only: bool = False, workers: int = 3):
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

    def _extract_mockup_urls(product_payload: dict) -> List[str]:
        images = product_payload.get("images", []) if isinstance(product_payload, dict) else []
        return [img.get("src") for img in images if isinstance(img, dict) and img.get("src")]

    def _wait_for_mockup_urls(temp_product_id: str, initial_payload: dict, model_name: str, timeout_seconds: int = 90) -> List[str]:
        mockup_urls = _extract_mockup_urls(initial_payload)
        if mockup_urls:
            return mockup_urls

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                time.sleep(4)
                r = requests.get(
                    f"{base}/shops/{PRINTIFY_SHOP_ID}/products/{temp_product_id}.json",
                    headers=headers,
                    timeout=30,
                )
                r.raise_for_status()
                product_data = r.json()
                mockup_urls = _extract_mockup_urls(product_data)
                if mockup_urls:
                    return mockup_urls
            except Exception as exc:
                log.warning("      [%s] polling mockup URLs failed: %s", model_name, exc)

        return []

    def _mark_step2_auth_failure(reason: str) -> None:
        for row in rows:
            sku = row.get("SKU #", row.get("SKU", "")).strip()
            if not sku:
                continue
            if single_sku and sku != single_sku:
                continue
            state.setdefault("step2", {})[sku] = reason
        save_state(state)

    if not dry_run:
        try:
            r = requests.get(f"{base}/shops.json", headers=headers, timeout=30)
            if r.status_code == 401:
                reason = "failed: printify_auth (401 unauthorized)"
                log.error("Step 2 preflight failed: Printify API unauthorized (401). Check PRINTIFY_API_KEY secret.")
                _mark_step2_auth_failure(reason)
                return mockup_meta
            r.raise_for_status()
            shops = r.json() if isinstance(r.json(), list) else []
            if PRINTIFY_SHOP_ID and not any(str(shop.get("id")) == str(PRINTIFY_SHOP_ID) for shop in shops if isinstance(shop, dict)):
                reason = f"failed: printify_shop_access ({PRINTIFY_SHOP_ID})"
                log.error("Step 2 preflight failed: Printify shop '%s' not accessible to provided API key.", PRINTIFY_SHOP_ID)
                _mark_step2_auth_failure(reason)
                return mockup_meta
        except Exception as exc:
            reason = f"failed: printify_preflight ({exc})"
            log.error("Step 2 preflight failed: %s", exc)
            _mark_step2_auth_failure(reason)
            return mockup_meta

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
                state.setdefault("step2", {})[sku] = f"failed: download ({exc})"
                save_state(state)
                continue
        
        if not os.path.isfile(local_path):
            log.warning("  [%s] image not found: %s", sku, local_path)
            state.setdefault("step2", {})[sku] = "failed: no_image"
            save_state(state)
            continue

        # Validate dimensions and file size before hitting Printify API
        if not validate_png_for_printify(local_path, sku):
            state.setdefault("step2", {})[sku] = "failed: image validation"
            save_state(state)
            continue

        design_name = Path(local_path).stem

        # Hash tracking: detect if design PNG changed since last run and auto-invalidate mockup cache
        current_hash = compute_file_hash(local_path)
        stored_hash = state.get("design_hashes", {}).get(sku, "")
        if stored_hash and stored_hash != current_hash:
            log.info("  [%s] design PNG changed (hash mismatch) — invalidating mockup + lifestyle cache", sku)
            mockup_meta.pop(sku, None)
            state.get("step2", {}).pop(sku, None)
            state.get("step3", {}).pop(sku, None)
            log.warning("  [%s] step2 + step3 state cleared — lifestyle mockups will also regenerate", sku)
        state.setdefault("design_hashes", {})[sku] = current_hash

        # Skip if we already have all mockups for this SKU
        existing_entry = mockup_meta.get(sku, {})
        existing_models = existing_entry.get("models", {}) if isinstance(existing_entry, dict) else {}
        if len(existing_models) >= len(VARIANT_MAP) and not force:
            log.info("  [%s] mockups exist — skipping", sku)
            state.setdefault("step2", {})[sku] = "done"
            save_state(state)
            continue

        if dry_run:
            log.info("  [DRY RUN] [%s] would generate Printify mockups for %d variants: %s", sku, len(VARIANT_MAP), list(VARIANT_MAP.keys()))
            continue
        if failed_only and not _needs_retry("step2", sku, state):
            log.info("  [%s] not failed — skipping (--failed-only)", sku)
            continue

        # Upload design to GCS (use separate folder to avoid duplicating source designs)
        log.info("  [%s] uploading to GCS...", sku)
        try:
            design_url = upload_to_gcs(local_path, f"designs/step2-uploads/{design_name}.png", public=True)
        except Exception as exc:
            log.error("  [%s] GCS upload failed: %s", sku, exc)
            state.setdefault("step2", {})[sku] = f"failed: gcs_upload ({exc})"
            save_state(state)
            continue

        mockup_meta[sku] = {
            "sku": sku,
            "design_name": design_name,
            "gcs_folder": f"output/mockups/{sku}/",
            "source_design_gcs_url": design_url,
            "models": existing_models,
        }

        def _run_variant(model_name: str, variant_id: int):
            """Fetch one Printify mockup variant. Returns (model_name, info_dict or None)."""
            # 1. Upload design image to Printify
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
                log.error("      [%s] Printify upload failed: %s", model_name, exc)
                return model_name, None
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
                temp_product_id = product.get("id")
            except Exception as exc:
                log.error("      [%s] Printify product create failed: %s", model_name, exc)
                return model_name, None
            # 3. Download mockup image
            result = None
            mockup_urls = _wait_for_mockup_urls(temp_product_id, product, model_name)
            if mockup_urls:
                safe_model = model_name.replace(" ", "_").replace("/", "_")
                out_path = MOCKUP_DIR / f"{design_name}_{safe_model}.jpg"
                try:
                    img_r = requests.get(mockup_urls[0], timeout=30)
                    img_r.raise_for_status()
                    out_path.write_bytes(img_r.content)
                    gcs_blob_name = f"output/mockups/{sku}/{safe_model}.jpg"
                    gcs_url = upload_to_gcs(str(out_path), gcs_blob_name)
                    log.info("      [%s] saved %s", sku, out_path.name)
                    result = {
                        "local_path": str(out_path),
                        "gcs_path": gcs_blob_name,
                        "gcs_url": gcs_url,
                        "variant_id": variant_id,
                        "printify_url": mockup_urls[0],
                    }
                except Exception as exc:
                    log.error("      [%s] download failed: %s", model_name, exc)
            else:
                log.error("      [%s] no mockup URL returned after waiting", model_name)
            # 4. Delete temp product
            try:
                requests.delete(
                    f"{base}/shops/{PRINTIFY_SHOP_ID}/products/{temp_product_id}.json",
                    headers=headers,
                    timeout=30,
                )
            except Exception:
                pass
            return model_name, result

        n_workers = min(workers, len(VARIANT_MAP))
        log.info("  [%s] generating %d variants (%d workers)...", sku, len(VARIANT_MAP), n_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(_run_variant, mn, vid): mn for mn, vid in VARIANT_MAP.items()}
            for future in concurrent.futures.as_completed(futures):
                mn_result, info = future.result()
                if info:
                    mockup_meta[sku]["models"][mn_result] = info

        # Save progress after each design
        (OUTPUT_DIR / "mockup_metadata.json").write_text(json.dumps(mockup_meta, indent=2))
        generated = len(mockup_meta[sku].get("models", {}))
        expected = len(VARIANT_MAP)
        if generated >= expected:
            state.setdefault("step2", {})[sku] = "done"
        else:
            state.setdefault("step2", {})[sku] = f"failed: partial_mockups ({generated}/{expected})"
            log.error("  [%s] incomplete mockups: %d/%d", sku, generated, expected)
        save_state(state)
        time.sleep(2)

    log.info("Step 2 complete — mockups in %s", MOCKUP_DIR)
    
    # Upload mockup metadata to GCS for persistence
    try:
        mockup_meta_path = OUTPUT_DIR / "mockup_metadata.json"
        gcs_path = upload_to_gcs(str(mockup_meta_path), "output/mockup_metadata.json")
        log.info("  ✓ Uploaded to GCS: %s", gcs_path)
    except Exception as exc:
        log.warning("  Could not upload to GCS: %s", exc)
    
    return mockup_meta


# =========================================================================
#  Step 3 — Gemini 3.1 Flash lifestyle mockup generation
# =========================================================================

def step3_generate_lifestyle_mockups(rows: List[dict], state: dict, single_sku: str = None, dry_run: bool = False, force: bool = False, failed_only: bool = False, workers: int = 3, approval: TelegramApproval = None):
    """
    Step 3: Take the iPhone 16 Pro Max mockup from Printify, pass it as a
    reference image to Gemini 3.1 Flash Image Preview, and generate two
    lifestyle scenes (table flat + hand holding).
    If a TelegramApproval instance is provided, sends images to Telegram
    for review; regenerates if rejected (up to APPROVAL_MAX_REGEN times).
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
        if table_out.exists() and hand_out.exists() and not force:
            log.info("  [%s] lifestyle mockups exist — skipping", sku)
            state.setdefault("step3", {})[sku] = "done"
            save_state(state)
            continue

        if dry_run:
            log.info("  [DRY RUN] [%s] would generate lifestyle mockups (table + hand) via Gemini", sku)
            continue
        if failed_only and not _needs_retry("step3", sku, state):
            log.info("  [%s] not failed — skipping (--failed-only)", sku)
            continue

        # Find the iPhone 16 Pro Max mockup as source reference
        entry = mockup_meta.get(sku)
        if isinstance(entry, dict) and "models" in entry:
            design_data = entry.get("models", {})
        else:
            design_data = mockup_meta.get(design_name, {})

        source_info = design_data.get("iPhone 16 Pro Max") or design_data.get("iPhone_16_Pro_Max")
        if not source_info:
            # Try any available mockup
            if design_data:
                source_info = next(iter(design_data.values()))
            else:
                log.warning("  [%s] no Printify mockups found — skipping", sku)
                state.setdefault("step3", {})[sku] = "failed: missing_step2_mockup"
                save_state(state)
                continue

        source_path = source_info.get("local_path") or source_info.get("path", "")
        if not os.path.isfile(source_path):
            gcs_source = source_info.get("gcs_url") or source_info.get("url") or source_info.get("gcs_path", "")
            if gcs_source:
                try:
                    source_path = download_from_gcs(gcs_source)
                except Exception as exc:
                    log.warning("  [%s] failed to download source mockup: %s", sku, exc)
                    state.setdefault("step3", {})[sku] = f"failed: source_download ({exc})"
                    save_state(state)
                    continue
            else:
                log.warning("  [%s] source mockup file missing: %s", sku, source_path)
                state.setdefault("step3", {})[sku] = "failed: source_mockup_missing"
                save_state(state)
                continue

        log.info("  [%s] %s", sku, row.get("Product Name", design_name))
        source_img = Image.open(source_path)

        # --- Approval loop: generate scenes → review → regenerate if needed ---
        max_regen = APPROVAL_MAX_REGEN if approval else 0
        for regen_attempt in range(1, max_regen + 2):

            # --- Scenes: Table flat + Hand holding (run in parallel) ---
            def _gen_table():
                resp = client.models.generate_content(
                    model=GEMINI_IMAGE_MODEL,
                    contents=[TABLE_PROMPT, source_img],
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=types.ImageConfig(aspect_ratio="3:4"),
                    ),
                )
                for part in resp.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                        return part.inline_data.data
                raise RuntimeError("No image in response")

            def _gen_hand():
                resp = client.models.generate_content(
                    model=GEMINI_IMAGE_MODEL,
                    contents=[HAND_PROMPT, source_img],
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=types.ImageConfig(aspect_ratio="4:5"),
                    ),
                )
                for part in resp.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                        return part.inline_data.data
                raise RuntimeError("No image in response")

            scenes: Dict[str, Any] = {}
            # On regeneration, always regenerate both scenes
            if regen_attempt > 1 or not table_out.exists():
                scenes["table"] = (table_out, _gen_table)
            if regen_attempt > 1 or not hand_out.exists():
                scenes["hand"] = (hand_out, _gen_hand)

            if scenes:
                s_workers = min(workers, len(scenes))
                log.info("  [%s] generating %d scene(s) (%d workers)...", sku, len(scenes), s_workers)
                with concurrent.futures.ThreadPoolExecutor(max_workers=s_workers) as ex:
                    fut_map = {ex.submit(retry, fn): (name, out) for name, (out, fn) in scenes.items()}
                    for future in concurrent.futures.as_completed(fut_map):
                        name, out_path = fut_map[future]
                        try:
                            out_path.write_bytes(future.result())
                            log.info("    saved %s", out_path.name)
                        except Exception as exc:
                            log.error("    %s scene failed: %s", name, exc)

            # --- Telegram approval gate ---
            if approval and table_out.exists() and hand_out.exists() and regen_attempt <= max_regen:
                decision = approval.request_image_approval(
                    sku, [str(table_out), str(hand_out)], attempt=regen_attempt
                )
                if decision == "regenerate":
                    log.info("    ↻ Regenerating lifestyle images for SKU %s (attempt %d/%d)", sku, regen_attempt, max_regen)
                    # Delete existing files so they're regenerated
                    for p in (table_out, hand_out):
                        if p.exists():
                            p.unlink()
                    continue  # loop back and re-generate
            break  # approved (or no approval, or last attempt)

        if table_out.exists() and hand_out.exists():
            state.setdefault("step3", {})[sku] = "done"
        else:
            produced = int(table_out.exists()) + int(hand_out.exists())
            state.setdefault("step3", {})[sku] = f"failed: partial_lifestyle ({produced}/2)"
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
        """Create Shopify product with explicit Model variants via REST."""
        rest_url = f"https://{self.store}/admin/api/{self.api_version}/products.json"
        payload = {
            "product": {
                "title": title,
                "body_html": html,
                "vendor": "Tip Cat Studios",
                "product_type": "Phone Case",
                "tags": ", ".join([str(t) for t in tags if str(t).strip()]),
                "options": [{"name": "Model"}],
                "variants": [
                    {
                        "option1": v["option1"],
                        "price": str(v["price"]),
                        "sku": str(v["sku"]),
                        "requires_shipping": True,
                        "inventory_policy": "continue",
                    }
                    for v in variants
                ],
            }
        }

        response = self.session.post(rest_url, json=payload, timeout=20)
        response.raise_for_status()
        product = response.json().get("product", {})
        if not product or not product.get("id"):
            raise RuntimeError(f"Shopify product create failed: invalid response {response.text}")

        product_id = f"gid://shopify/Product/{product['id']}"
        option_to_numeric = {}
        for variant in product.get("variants", []):
            option_value = str(variant.get("option1", "")).strip()
            variant_id = str(variant.get("id", "")).strip()
            if option_value and variant_id:
                option_to_numeric[option_value] = variant_id

        return {
            "id": product_id,
            "title": title,
            "option_variant_gids": {
                option_value: f"gid://shopify/ProductVariant/{variant_id}"
                for option_value, variant_id in option_to_numeric.items()
            },
            "option_variant_ids": option_to_numeric,
        }

    def _shopify_request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make a Shopify REST request with retry on 429 rate limits."""
        max_retries = 4
        for attempt in range(max_retries):
            r = self.session.request(method, url, **kwargs)
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", 2))
                wait = max(retry_after, 2 * (attempt + 1))
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
            return r
        return r  # return last response even if 429

    def update_variants_fulfillment(self, variant_ids: List[str], fulfillment_service: str = "printful") -> int:
        """
        Move Shopify variant inventory to a fulfillment service location.

        Connects each variant's inventory item to the fulfillment service location
        and disconnects from the default manual location so Shopify routes
        FulfillmentOrders to the correct provider.

        Returns the number of variants successfully updated.
        """
        # Look up the fulfillment service location ID
        fs_url = f"https://{self.store}/admin/api/{self.api_version}/fulfillment_services.json?scope=all"
        r = self._shopify_request_with_retry("GET", fs_url, timeout=15)
        r.raise_for_status()
        fs_location_id = None
        for svc in r.json().get("fulfillment_services", []):
            if svc.get("handle") == fulfillment_service:
                fs_location_id = svc.get("location_id")
                break
        if not fs_location_id:
            log.warning("  Fulfillment service '%s' not found in Shopify — skipping inventory migration", fulfillment_service)
            return 0

        updated = 0
        for vid in variant_ids:
            try:
                # Get inventory_item_id for this variant
                var_url = f"https://{self.store}/admin/api/{self.api_version}/variants/{vid}.json?fields=id,inventory_item_id"
                r = self._shopify_request_with_retry("GET", var_url, timeout=15)
                r.raise_for_status()
                inv_item_id = r.json().get("variant", {}).get("inventory_item_id")
                if not inv_item_id:
                    log.warning("  Variant %s has no inventory_item_id", vid)
                    continue

                # Connect inventory item to fulfillment service location
                connect_url = f"https://{self.store}/admin/api/{self.api_version}/inventory_levels/connect.json"
                connect_payload = {
                    "location_id": fs_location_id,
                    "inventory_item_id": inv_item_id,
                }
                r = self._shopify_request_with_retry("POST", connect_url, json=connect_payload, timeout=15)
                if r.status_code not in (200, 201, 422):
                    r.raise_for_status()

                # Remove inventory from other locations (so orders route only to fulfiller)
                levels_url = f"https://{self.store}/admin/api/{self.api_version}/inventory_levels.json?inventory_item_ids={inv_item_id}"
                r = self._shopify_request_with_retry("GET", levels_url, timeout=15)
                r.raise_for_status()
                for level in r.json().get("inventory_levels", []):
                    loc_id = level.get("location_id")
                    if loc_id and loc_id != fs_location_id:
                        del_url = f"https://{self.store}/admin/api/{self.api_version}/inventory_levels.json?inventory_item_id={inv_item_id}&location_id={loc_id}"
                        dr = self._shopify_request_with_retry("DELETE", del_url, timeout=15)
                        if dr.status_code in (200, 204):
                            log.debug("    Removed inventory from location %s for variant %s", loc_id, vid)

                updated += 1
                time.sleep(0.5)  # pace to stay within Shopify rate limits
            except Exception as exc:
                log.warning("  Failed to update variant %s fulfillment: %s", vid, exc)
        return updated

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

    def find_product_by_handle(self, title: str) -> Optional[str]:
        """
        Check if a Shopify product with this title's auto-generated handle already exists.
        Returns the product GID (gid://shopify/Product/...) if found, None otherwise.
        """
        handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
        rest_url = f"https://{self.store}/admin/api/{self.api_version}/products.json?handle={handle}&fields=id,title,handle"
        try:
            r = self.session.get(rest_url, timeout=15)
            r.raise_for_status()
            products = r.json().get("products", [])
            if products:
                return f"gid://shopify/Product/{products[0]['id']}"
        except Exception as exc:
            log.warning("  duplicate check failed for handle '%s': %s", handle, exc)
        return None

    def get_product_variant_ids_by_model(self, product_id: str) -> Dict[str, str]:
        result = self.gql("""
            query productVariants($id: ID!) {
                product(id: $id) {
                    variants(first: 100) {
                        edges {
                            node {
                                id
                                selectedOptions { name value }
                            }
                        }
                    }
                }
            }
        """, {"id": product_id})

        edges = (
            result.get("product", {})
            .get("variants", {})
            .get("edges", [])
        )
        mapping: Dict[str, str] = {}
        for edge in edges:
            node = edge.get("node", {})
            gid = str(node.get("id", ""))
            if not gid:
                continue
            numeric_id = gid.split("/")[-1]
            for opt in node.get("selectedOptions", []):
                if opt.get("name") == "Model":
                    mapping[str(opt.get("value", ""))] = numeric_id
        return mapping

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


class PrintfulClient:
    """Printful client for Ecommerce Platform Sync operations."""

    def __init__(self):
        self.api_key = PRINTFUL_API_KEY
        self.store_id = str(PRINTFUL_STORE_ID).strip()
        self.base_url = "https://api.printful.com"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-PF-Store-Id": self.store_id,
        })

    def _json_or_text(self, response: requests.Response) -> dict:
        try:
            return response.json()
        except Exception:
            return {"result": response.text}

    def get_sync_product_by_external(self, external_product_id: str) -> dict:
        external = str(external_product_id).strip()
        if not external:
            return {}

        r = self.session.get(
            f"{self.base_url}/sync/products/@{external}",
            timeout=30,
        )
        body = self._json_or_text(r)
        if r.status_code == 404:
            return {}
        if r.status_code >= 400 or body.get("code", 200) >= 400:
            raise RuntimeError(f"Printful sync product lookup failed ({r.status_code}): {body.get('result') or body.get('error')}")

        result = body.get("result", {})
        if not isinstance(result, dict):
            return {}
        return result

    def update_sync_variant_by_external(self, external_variant_id: str, variant_id: int, design_url: str, retail_price: str, sku: str) -> dict:
        external = str(external_variant_id).strip()
        payload = {
            "variant_id": int(variant_id),
            "retail_price": str(retail_price),
            "sku": str(sku),
            "is_ignored": False,
            "files": [{"url": design_url}],
        }

        max_retries = 4
        for attempt in range(max_retries):
            r = self.session.put(
                f"{self.base_url}/sync/variant/@{external}",
                json=payload,
                timeout=60,
            )
            body = self._json_or_text(r)

            # Handle rate limiting with exponential backoff
            if r.status_code == 429:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt * 15  # 15s, 30s, 60s
                    log.warning("    Printful rate limited on variant %s, retrying in %ds (attempt %d/%d)", external, wait, attempt + 1, max_retries)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Printful rate limited after {max_retries} retries for variant @{external}")

            if r.status_code >= 400 or body.get("code", 200) >= 400:
                raise RuntimeError(f"Printful sync variant update failed ({r.status_code}): {body.get('result') or body.get('error')}")

            result = body.get("result", {})
            if not isinstance(result, dict):
                raise RuntimeError(f"Printful sync variant update returned invalid response: {body}")
            return result


def _to_public_design_url(image_path: str) -> str:
    """Convert design path (gs://, GCS URL, or local path) to a public HTTPS URL for Printful files."""
    source = (image_path or "").strip()
    if not source:
        raise ValueError("missing_image_path")

    if source.startswith("gs://"):
        bucket_object = source[5:]
        if "/" not in bucket_object:
            raise ValueError(f"invalid_gs_path: {source}")
        bucket, obj = bucket_object.split("/", 1)
        return f"https://storage.googleapis.com/{bucket}/{quote(obj)}"

    if "storage.googleapis.com" in source:
        return source

    if os.path.isfile(source):
        design_name = Path(source).name
        return upload_to_gcs(source, f"designs/step4-printful/{design_name}", public=True)

    raise ValueError(f"unsupported_image_path: {source}")


def step4_create_shopify_products(rows: List[dict], state: dict, single_sku: str = None, dry_run: bool = False, force: bool = False, failed_only: bool = False, allow_fallback_metadata: bool = False):
    """
    Step 4: Create Shopify products from metadata.
    """
    if not METADATA_PATH.exists():
        log.error("No metadata file — run step 1 first")
        return

    metadata_list = json.loads(METADATA_PATH.read_text())
    meta_by_sku = {m["sku"]: m for m in metadata_list}

    shopify = ShopifyClient()
    use_printful_sync = bool(PRINTFUL_API_KEY and PRINTFUL_STORE_ID and isinstance(PRINTFUL_VARIANT_MAP, dict) and PRINTFUL_VARIANT_MAP)
    printful = PrintfulClient() if use_printful_sync else None

    for row in rows:
        sku = row.get("SKU #", row.get("SKU", "")).strip()
        if not sku:
            continue
        if single_sku and sku != single_sku:
            continue

        # Check if already created
        existing = state.get("step4", {}).get(sku)
        existing_gid = ""
        if isinstance(existing, str) and existing.startswith("gid://"):
            existing_gid = existing
            if not force:
                if not use_printful_sync:
                    log.info("  [%s] already created — skipping", sku)
                    continue
                if state.get("step4_printful", {}).get(sku) == "done":
                    log.info("  [%s] already created and Printful-synced — skipping", sku)
                    continue

        meta_entry = meta_by_sku.get(sku)
        if not meta_entry:
            log.warning("  [%s] no metadata — skipping", sku)
            state.setdefault("step4", {})[sku] = "failed: missing_metadata"
            save_state(state)
            continue

        analysis = meta_entry.get("analysis", {})
        meta = analysis.get("metadata", {})
        status = analysis.get("status", "unknown")

        # Determine title/html — strict by default; fallback only when explicitly allowed
        meta_title = (meta.get("title") or "").strip()
        if status == "success" and meta_title:
            title = meta_title
            generated_html = meta.get("shopify_html", f"<p>{meta.get('full_description', '')}</p>")
            html = generated_html + "\n" + SHOPIFY_DESCRIPTION_BOILERPLATE
            tags = meta.get("tags", []) if isinstance(meta.get("tags", []), list) else []
        else:
            if not allow_fallback_metadata:
                state.setdefault("step4", {})[sku] = f"failed: metadata_not_ready ({status})"
                save_state(state)
                log.error("  [%s] metadata status=%s — refusing Shopify create (use --allow-fallback-metadata to override)", sku, status)
                continue
            product_name = (row.get("Product Name") or f"Phone Case {sku}").strip()
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

        # Build Printful variant map by model (if configured)
        printful_variant_by_model: Dict[str, int] = {}
        if use_printful_sync:
            missing_models = []
            for model_name in VARIANT_MAP:
                vid = PRINTFUL_VARIANT_MAP.get(model_name)
                if not vid:
                    missing_models.append(model_name)
                    continue
                printful_variant_by_model[model_name] = int(vid)
            if missing_models:
                state.setdefault("step4", {})[sku] = f"failed: printful_variant_mapping_missing ({', '.join(missing_models)})"
                save_state(state)
                log.error("  [%s] missing Printful variant mapping for: %s", sku, ", ".join(missing_models))
                continue

        if dry_run:
            if use_printful_sync:
                if existing_gid:
                    log.info("  [DRY RUN] [%s] would configure Printful sync variants for existing Shopify product %s", sku, existing_gid)
                else:
                    log.info("  [DRY RUN] [%s] would create Shopify product and configure Printful sync variants", sku)
            else:
                log.info("  [DRY RUN] [%s] would create Shopify product: %s (%d variants)", sku, title, len(variants))
            continue
        if failed_only and not _needs_retry("step4", sku, state):
            log.info("  [%s] not failed — skipping (--failed-only)", sku)
            continue

        # Duplicate guard: detect if product already exists in Shopify (e.g. after state reset)
        if not force and not existing_gid:
            existing_gid = shopify.find_product_by_handle(title)
            if existing_gid:
                log.info("  [%s] already exists in Shopify (%s) — saving GID", sku, existing_gid)
                state.setdefault("step4", {})[sku] = existing_gid
                save_state(state)

        product_id = existing_gid
        product_variant_ids_by_model: Dict[str, str] = {}

        try:
            if not product_id:
                log.info("  [%s] creating: %s", sku, title)
                product = shopify.create_product(title, html, tags, variants)
                product_id = product["id"]
                product_variant_ids_by_model = dict(product.get("option_variant_ids", {}))
                state.setdefault("step4", {})[sku] = product_id
                save_state(state)
                log.info("    created %s", product_id)
            elif use_printful_sync:
                product_variant_ids_by_model = shopify.get_product_variant_ids_by_model(product_id)

            if use_printful_sync:
                image_path = row.get("Image Path", "").strip()
                design_url = _to_public_design_url(image_path)
                if not product_id:
                    raise RuntimeError("missing_shopify_product_id")

                shopify_product_numeric_id = product_id.split("/")[-1]
                expected_external_variant_ids = {
                    str(product_variant_ids_by_model.get(model_name, "")).strip()
                    for model_name in VARIANT_MAP
                    if str(product_variant_ids_by_model.get(model_name, "")).strip().isdigit()
                }

                sync_info = {}
                external_to_sync = {}
                poll_every = max(5, PRINTFUL_SYNC_POLL_SECONDS)
                deadline = time.time() + max(0, PRINTFUL_SYNC_WAIT_SECONDS)
                missing_external_ids = set(expected_external_variant_ids)

                while True:
                    sync_info = printful.get_sync_product_by_external(shopify_product_numeric_id)
                    sync_variants = sync_info.get("sync_variants", []) if isinstance(sync_info, dict) else []
                    external_to_sync = {}
                    for item in sync_variants:
                        if not isinstance(item, dict):
                            continue
                        # Printful returns external_id either directly on the item
                        # or nested under a "sync_variant" sub-object depending on endpoint
                        ext_id = str(item.get("external_id", "")).strip()
                        if not ext_id:
                            ext_id = str(item.get("sync_variant", {}).get("external_id", "")).strip()
                        if ext_id:
                            external_to_sync[ext_id] = item
                    missing_external_ids = {
                        external_id for external_id in expected_external_variant_ids
                        if external_id not in external_to_sync
                    }

                    if sync_info and not missing_external_ids:
                        break
                    if time.time() >= deadline:
                        break
                    remaining = int(deadline - time.time())
                    if not sync_info:
                        log.info("    waiting for Shopify→Printful product import (%ds remaining)...", remaining)
                    else:
                        log.info("    waiting for Shopify→Printful variant import (%d missing, %ds remaining)...", len(missing_external_ids), remaining)
                    time.sleep(poll_every)

                if not sync_info:
                    raise RuntimeError("awaiting_printful_import")
                if missing_external_ids:
                    raise RuntimeError(f"awaiting_printful_variant_import ({', '.join(sorted(missing_external_ids))})")

                sync_errors = []
                for model_name in VARIANT_MAP:
                    external_variant_id = str(product_variant_ids_by_model.get(model_name, "")).strip()
                    if not external_variant_id or not external_variant_id.isdigit():
                        sync_errors.append(f"missing_shopify_variant_id:{model_name}")
                        continue

                    if external_variant_id not in external_to_sync:
                        sync_errors.append(f"variant_not_imported:{model_name}")
                        continue

                    safe = model_name.replace(" ", "-").lower()
                    printful.update_sync_variant_by_external(
                        external_variant_id=external_variant_id,
                        variant_id=printful_variant_by_model[model_name],
                        design_url=design_url,
                        retail_price=PRODUCT_PRICE,
                        sku=f"TC-{sku}-{safe}",
                    )
                    time.sleep(1.5)  # pace requests to avoid Printful rate limits

                if sync_errors:
                    raise RuntimeError(f"printful_sync_incomplete ({', '.join(sync_errors)})")

                # Update Shopify variants to use Printful fulfillment service
                all_variant_ids = [
                    str(product_variant_ids_by_model[m])
                    for m in VARIANT_MAP
                    if str(product_variant_ids_by_model.get(m, "")).strip().isdigit()
                ]
                if all_variant_ids:
                    updated_count = shopify.update_variants_fulfillment(all_variant_ids, fulfillment_service="printful")
                    log.info("    updated %d/%d Shopify variants → Printful fulfillment", updated_count, len(all_variant_ids))

                state.setdefault("step4_printful", {})[sku] = "done"
                save_state(state)
                log.info("    synced in Printful for Shopify product %s", product_id)
        except Exception as exc:
            log.error("  [%s] Step 4 create failed: %s", sku, exc)
            if product_id and str(product_id).startswith("gid://"):
                state.setdefault("step4", {})[sku] = product_id
            else:
                state.setdefault("step4", {})[sku] = f"failed: {exc}"
            if use_printful_sync:
                state.setdefault("step4_printful", {})[sku] = f"failed: {exc}"
            save_state(state)

        time.sleep(0.5)

    log.info("Step 4 complete")


# =========================================================================
#  Step 5 — Upload images to Shopify products
# =========================================================================

def step5_upload_shopify_images(rows: List[dict], state: dict, single_sku: str = None, dry_run: bool = False, force: bool = False, failed_only: bool = False):
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
            log.warning("  [%s] no Shopify product from step 4", sku)
            state.setdefault("step5", {})[sku] = "failed: missing_shopify_product"
            save_state(state)
            continue

        # Check if already uploaded
        if state.get("step5", {}).get(sku) == "done" and not force:
            log.info("  [%s] images already uploaded — skipping", sku)
            continue

        image_path = row.get("Image Path", "").strip()
        design_name = Path(image_path).stem if image_path else ""
        product_name = row.get("Product Name", design_name)

        media_items = []

        # 1. Main Printify mockup → upload to GCS
        entry = mockup_meta.get(sku)
        if isinstance(entry, dict) and "models" in entry:
            design_data = entry.get("models", {})
        else:
            design_data = mockup_meta.get(design_name, {})

        main_info = design_data.get("iPhone 16 Pro Max") or design_data.get("iPhone_16_Pro_Max")
        if not main_info:
            main_info = next(iter(design_data.values()), None) if design_data else None

        main_local_path = ""
        if main_info:
            main_local_path = main_info.get("local_path") or main_info.get("path", "")

        if main_info and not os.path.isfile(main_local_path):
            gcs_source = main_info.get("gcs_url") or main_info.get("url") or main_info.get("gcs_path", "")
            if gcs_source:
                try:
                    main_local_path = download_from_gcs(gcs_source)
                except Exception as exc:
                    log.warning("  [%s] failed to download main mockup: %s", sku, exc)
                    main_local_path = ""

        if main_info and os.path.isfile(main_local_path):
            gcs_url = upload_to_gcs(main_local_path, f"mockups/{sku}_main.jpg", public=True)
            media_items.append({
                "url": gcs_url,
                "alt": f"{product_name} iPhone Case",
            })

        # 2. Table lifestyle
        table_path = LIFESTYLE_DIR / f"{sku}_table_flat.png"
        if table_path.exists():
            gcs_url = upload_to_gcs(str(table_path), f"lifestyle/{sku}_table.png", public=True)
            media_items.append({
                "url": gcs_url,
                "alt": f"{product_name} - Table Scene",
            })

        # 3. Hand lifestyle
        hand_path = LIFESTYLE_DIR / f"{sku}_hand_holding.png"
        if hand_path.exists():
            gcs_url = upload_to_gcs(str(hand_path), f"lifestyle/{sku}_hand.png", public=True)
            media_items.append({
                "url": gcs_url,
                "alt": f"{product_name} - Lifestyle",
            })

        if not media_items:
            log.warning("  [%s] no images to upload", sku)
            state.setdefault("step5", {})[sku] = "failed: no_media_items"
            save_state(state)
            continue

        if len(media_items) < 3:
            log.warning("  [%s] incomplete media set (%d/3) — not uploading", sku, len(media_items))
            state.setdefault("step5", {})[sku] = f"failed: incomplete_media ({len(media_items)}/3)"
            save_state(state)
            continue

        if dry_run:
            log.info("  [DRY RUN] [%s] would upload %d images to Shopify product %s", sku, len(media_items), product_id)
            continue
        if failed_only and not _needs_retry("step5", sku, state):
            log.info("  [%s] not failed — skipping (--failed-only)", sku)
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
    parser.add_argument("--config", type=str, default="tipcat-phonecases", help="Config name (e.g., 'tipcat-phonecases', 'tipcat-mousepads')")
    parser.add_argument("--step", type=int, choices=[1, 2, 3, 4, 5], help="Run a single step")
    parser.add_argument("--sku", type=str, help="Process a single SKU only")
    parser.add_argument("--limit", type=int, help="Process only first N products")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint (default behaviour)")
    parser.add_argument("--cleanup-shopify", action="store_true", help="Delete all Shopify products first")
    parser.add_argument("--dry-run", action="store_true", help="Simulate pipeline — log actions without making any API calls")
    parser.add_argument("--force", action="store_true", help="Re-process SKUs already marked complete in state")
    parser.add_argument("--failed-only", action="store_true", help="Re-run only SKUs that failed or are incomplete")
    parser.add_argument("--reset-step", type=int, choices=[1, 2, 3, 4, 5], help="Clear state and output files for a specific step, then exit (or continue with --step)")
    parser.add_argument("--workers", type=int, default=3, help="Parallel workers for Steps 2 and 3 (default: 3, max recommended: 5)")
    parser.add_argument("--continue-on-issues", action="store_true", help="Continue to next steps even when a step has failures/pending items (default: stop)")
    parser.add_argument("--allow-fallback-metadata", action="store_true", help="Allow Step 4 to create products from fallback text when metadata status is not success")
    parser.add_argument("--auto-approve", action="store_true", help="Skip Telegram approval for metadata and images (auto-approve everything)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.dry_run:
        log.info("*** DRY RUN MODE — no API calls will be made ***")
    if args.force:
        log.info("*** FORCE MODE — re-processing already-completed SKUs ***")
    if args.failed_only:
        log.info("*** FAILED-ONLY MODE — processing only failed/incomplete SKUs ***")
    if args.continue_on_issues:
        log.info("*** CONTINUE-ON-ISSUES MODE — pipeline will not stop at step gates ***")
    if args.allow_fallback_metadata:
        log.info("*** FALLBACK METADATA ENABLED — Step 4 may publish with generic metadata ***")
    if args.auto_approve:
        log.info("*** AUTO-APPROVE MODE — skipping Telegram approval prompts ***")

    log.info("=" * 70)
    log.info("Tip Cat Studios — Product Automation Pipeline")
    log.info("=" * 70)
    log.info("Working directory: %s", os.getcwd())
    log.info("Script location: %s", os.path.abspath(__file__))
    
    # Load and apply product configuration
    config = load_product_config(args.config)
    apply_config(config)
    
    # Validate critical env vars (now from config)
    missing = []
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not PRINTIFY_API_KEY:
        missing.append("PRINTIFY_API_KEY")
    if isinstance(config.get("printful"), dict) and config.get("printful"):
        if not PRINTFUL_API_KEY:
            missing.append("PRINTFUL_API_KEY")
        if not str(PRINTFUL_STORE_ID).strip():
            missing.append("PRINTFUL_STORE_ID")
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        missing.append("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET")

    if missing:
        log.error("Missing env vars: %s", ", ".join(missing))
        sys.exit(1)

    log.info("✓ All required credentials present")
    
    # Discover designs from GCS bucket via stable SKU map
    log.info("Scanning GCS bucket gs://%s/designs/ and syncing SKU map...", GCS_BUCKET)
    rows = list_designs_from_gcs()
    state = load_state()
    sku_summary = get_sku_map_summary()
    log.info(
        "✓ SKU map: %d total designs (%d active, %d pending, %d skip, %d archived) — next SKU: %d",
        sku_summary["total"], sku_summary["active"], sku_summary["pending"],
        sku_summary["skip"], sku_summary["archived"], sku_summary["next_sku"],
    )
    log.info("✓ %d designs eligible for processing", len(rows))

    # Optional: reset state + output files for a specific step
    if args.reset_step:
        import shutil
        step_key = f"step{args.reset_step}"
        cleared = state.pop(step_key, {})
        log.info("\n--- Resetting Step %d ---", args.reset_step)
        log.info("  Cleared %d state entries", len(cleared))
        if args.reset_step == 1:
            if METADATA_PATH.exists():
                METADATA_PATH.unlink()
                log.info("  Deleted %s", METADATA_PATH)
        elif args.reset_step == 2:
            meta = OUTPUT_DIR / "mockup_metadata.json"
            if meta.exists():
                meta.unlink()
                log.info("  Deleted %s", meta)
            if MOCKUP_DIR.exists():
                shutil.rmtree(str(MOCKUP_DIR))
                MOCKUP_DIR.mkdir(parents=True)
                log.info("  Cleared %s", MOCKUP_DIR)
        elif args.reset_step == 3:
            if LIFESTYLE_DIR.exists():
                shutil.rmtree(str(LIFESTYLE_DIR))
                LIFESTYLE_DIR.mkdir(parents=True)
                log.info("  Cleared %s", LIFESTYLE_DIR)
        elif args.reset_step in (4, 5):
            log.warning("  Step %d data lives in Shopify — use --cleanup-shopify to delete products", args.reset_step)
        save_state(state)
        log.info("  State saved.")
        if not args.step:
            log.info("  Reset complete. Run with --step %d to re-run this step.", args.reset_step)
            sys.exit(0)

    # Optional cleanup
    if args.cleanup_shopify:
        log.info("\n--- Cleaning up Shopify ---")
        cleanup_shopify()
        # Clear step 4+5 state
        state.pop("step4", None)
        state.pop("step5", None)
        save_state(state)

    run_rows = _scoped_rows(rows, single_sku=args.sku, limit=args.limit)
    if args.limit:
        log.info("Applying run scope: first %d item(s) after filters (%d selected)", args.limit, len(run_rows))

    # Telegram approval (disabled with --auto-approve or if env vars missing)
    approval = TelegramApproval(enabled=not args.auto_approve)

    steps_to_run = [args.step] if args.step else [1, 2, 3, 4, 5]

    for step_num in steps_to_run:
        log.info("\n--- Step %d ---", step_num)
        if step_num == 1:
            step1_generate_metadata(run_rows, state, single_sku=None, limit=None, dry_run=args.dry_run, force=args.force, failed_only=args.failed_only, approval=approval)
        elif step_num == 2:
            step2_generate_printify_mockups(run_rows, state, single_sku=None, dry_run=args.dry_run, force=args.force, failed_only=args.failed_only, workers=args.workers)
        elif step_num == 3:
            step3_generate_lifestyle_mockups(run_rows, state, single_sku=None, dry_run=args.dry_run, force=args.force, failed_only=args.failed_only, workers=args.workers, approval=approval)
        elif step_num == 4:
            step4_create_shopify_products(
                run_rows,
                state,
                single_sku=None,
                dry_run=args.dry_run,
                force=args.force,
                failed_only=args.failed_only,
                allow_fallback_metadata=args.allow_fallback_metadata,
            )
        elif step_num == 5:
            step5_upload_shopify_images(run_rows, state, single_sku=None, dry_run=args.dry_run, force=args.force, failed_only=args.failed_only)

        gate = verify_step_completion(
            step_num,
            run_rows,
            state,
            single_sku=None,
            limit=None,
        )
        log.info(
            "Step %d verification: %d/%d passed, %d issues",
            step_num,
            gate["success"],
            gate["total"],
            gate["issues"],
        )
        if gate["issues"]:
            for sku, reason in gate["issue_examples"]:
                log.warning("  issue sku=%s reason=%s", sku, reason)
            if not args.continue_on_issues:
                log.error("Stopping at step %d due to verification issues. Use --continue-on-issues to override.", step_num)
                write_pipeline_report(rows, state)
                sys.exit(2)

    log.info("\n" + "=" * 70)
    log.info("Pipeline complete!")
    log.info("=" * 70)

    # Write end-of-run report
    write_pipeline_report(rows, state)


if __name__ == "__main__":
    try:
        # Print immediately to stdout to confirm container is running
        print("🚀 Container started, initializing...", flush=True)
        main()
    except Exception as exc:
        import traceback
        error_msg = f"\n\n{'='*70}\nFATAL ERROR\n{'='*70}\n{traceback.format_exc()}\n{'='*70}\n"
        print(error_msg, file=sys.stderr)
        log.error(error_msg)
        sys.exit(1)
