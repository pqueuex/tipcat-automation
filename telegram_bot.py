#!/usr/bin/env python3
"""
Tip Cat Studios — Telegram Pipeline Bot
========================================

Run this locally or on any always-on server to control the pipeline via Telegram.

Required env vars:
  TELEGRAM_BOT_TOKEN        — from @BotFather
  TELEGRAM_ADMIN_CHAT_ID    — admin's numeric Telegram user ID (also bootstraps first admin)

Optional env vars:
  PIPELINE_CONFIG           — config name, default "tipcat-phonecases"
  GCS_BUCKET                — GCS bucket for state/report reads
  GOOGLE_CLOUD_PROJECT      — GCP project ID
  CLOUD_RUN_JOB             — Cloud Run job name (if deploying there)
  CLOUD_RUN_REGION          — Cloud Run region, default "us-central1"

Commands:
  /run                  Run full pipeline (all steps, auto-resume)
  /run step 2           Run a single step
  /run sku 5            Process a single SKU
  /run dry              Dry run — log actions, no API calls
  /run force            Re-process already-completed SKUs
  /run failed           Re-run only failed/incomplete SKUs
  /run workers 5        Set parallel worker count
  /status               Current per-step counts from pipeline state
  /report               Send pipeline_report.csv
  /reset 3              Clear state + output files for step N
  /cancel               Stop running pipeline process
  /help                 Show this message
"""

import json
import io
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from html import escape
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request as flask_request

# Load .env before reading any os.environ values.
# override=False means real env vars (e.g. from Secret Manager) always win.
load_dotenv(Path(__file__).parent.parent / ".env", override=False)
load_dotenv(Path(__file__).parent / ".env", override=False)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID     = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
WEBHOOK_SECRET    = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")   # random string you choose
PIPELINE_CONFIG   = os.environ.get("PIPELINE_CONFIG", "tipcat-phonecases")
GCS_BUCKET        = os.environ.get("GCS_BUCKET", "tipcat-product-designs")
GCP_PROJECT       = os.environ.get("GOOGLE_CLOUD_PROJECT", "tipcat-automation")
PIPELINE_CLOUD_RUN_JOB     = os.environ.get("PIPELINE_CLOUD_RUN_JOB", os.environ.get("CLOUD_RUN_JOB", ""))
PIPELINE_CLOUD_RUN_REGION  = os.environ.get("PIPELINE_CLOUD_RUN_REGION", os.environ.get("CLOUD_RUN_REGION", "us-central1"))
PORT              = int(os.environ.get("PORT", "8080"))

SCRIPT_DIR        = Path(__file__).parent
PIPELINE_SCRIPT   = SCRIPT_DIR / "product_automation_script.py"
OUTPUT_DIR        = SCRIPT_DIR / "output"
STATE_PATH        = OUTPUT_DIR / "pipeline_state.json"
REPORT_PATH       = OUTPUT_DIR / "pipeline_report.csv"
BOT_STATE_PATH    = OUTPUT_DIR / "telegram_bot_state.json"

TELEGRAM_API      = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

_last_update_id: int = 0
_current_process: subprocess.Popen = None
_current_run_meta: dict = {}
_seen_chats: dict = {}   # {chat_id_str: username} — populated by incoming messages
_pending_run: dict = {}
PENDING_RUN_TTL_SECONDS: int = int(os.environ.get("PENDING_RUN_TTL_SECONDS", "120"))

# Active store config name — changed via /store command.
# Defaults to PIPELINE_CONFIG env var (set at deploy time), falls back to "tipcat-phonecases".
_active_config: str = PIPELINE_CONFIG
_active_store_key: str = ""
_active_product_by_store: dict = {}

# ---------------------------------------------------------------------------
# Multi-user system
# ---------------------------------------------------------------------------
# Per-user context: {user_id_str: {"store_key": ..., "config": ..., "product_by_store": {...}}}
_user_contexts: dict = {}

# User config cache (loaded from GCS bot_users.json)
_users_config: dict = {"users": {}, "allowed_groups": []}
_users_config_loaded: bool = False


def _load_users_config() -> dict:
    """Load bot_users.json from GCS. Returns default if not found."""
    global _users_config, _users_config_loaded
    try:
        from google.cloud import storage
        client = storage.Client(project=GCP_PROJECT)
        blob = client.bucket(GCS_BUCKET).blob("bot_users.json")
        if blob.exists():
            _users_config = json.loads(blob.download_as_text())
            _users_config_loaded = True
            return _users_config
    except Exception as exc:
        print(f"_load_users_config failed: {exc}")
    # Bootstrap from ADMIN_CHAT_ID if no config exists
    if ADMIN_CHAT_ID and not _users_config.get("users"):
        _users_config = {
            "users": {
                str(ADMIN_CHAT_ID): {
                    "name": "admin",
                    "role": "admin",
                    "stores": ["*"],
                }
            },
            "allowed_groups": [],
        }
    _users_config_loaded = True
    return _users_config


def _save_users_config() -> None:
    """Write bot_users.json to GCS."""
    try:
        from google.cloud import storage
        client = storage.Client(project=GCP_PROJECT)
        blob = client.bucket(GCS_BUCKET).blob("bot_users.json")
        blob.upload_from_string(json.dumps(_users_config, indent=2), content_type="application/json")
    except Exception as exc:
        print(f"_save_users_config failed: {exc}")
        raise


def _get_user(user_id: str) -> dict:
    """Get user entry or empty dict. Loads config on first call."""
    if not _users_config_loaded:
        _load_users_config()
    return _users_config.get("users", {}).get(str(user_id), {})


def _is_admin(user_id: str) -> bool:
    return _get_user(user_id).get("role") == "admin"


def _is_authorized(user_id: str, chat_id: str) -> bool:
    """Check if a user is allowed to use the bot.
    In private chats: user_id must be in users list.
    In group chats: user_id must be in users list AND group must be allowed."""
    if not _users_config_loaded:
        _load_users_config()
    user = _get_user(user_id)
    if not user:
        return False
    # Private chat — user is authorized
    if str(user_id) == str(chat_id):
        return True
    # Group chat — group must also be allowed
    allowed_groups = [str(g) for g in _users_config.get("allowed_groups", [])]
    return str(chat_id) in allowed_groups


def _user_can_access_store(user_id: str, store_key: str) -> bool:
    """Check if user has access to a particular store."""
    user = _get_user(user_id)
    if not user:
        return False
    stores = user.get("stores", [])
    return "*" in stores or store_key in stores


def _get_user_context(user_id: str) -> dict:
    """Get per-user active store/product context."""
    uid = str(user_id)
    if uid not in _user_contexts:
        _user_contexts[uid] = {
            "store_key": "",
            "config": PIPELINE_CONFIG,
            "product_by_store": {},
        }
    return _user_contexts[uid]


def _set_user_store(user_id: str, store_key: str, config: str) -> None:
    ctx = _get_user_context(user_id)
    ctx["store_key"] = store_key
    ctx["config"] = config


def _set_user_product(user_id: str, store_key: str, product_key: str, config: str) -> None:
    ctx = _get_user_context(user_id)
    ctx["product_by_store"][store_key] = product_key
    ctx["config"] = config


def _load_bot_state() -> dict:
    if BOT_STATE_PATH.exists():
        try:
            return json.loads(BOT_STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_bot_state(state: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    BOT_STATE_PATH.write_text(json.dumps(state, indent=2))
    try:
        os.chmod(BOT_STATE_PATH, 0o600)
    except Exception:
        pass


def _persist_bot_state() -> None:
    """Persist runtime state that should survive process restarts."""
    _save_bot_state({
        "last_update_id": _last_update_id,
        "active_config": _active_config,
        "active_store_key": _active_store_key,
        "active_product_by_store": _active_product_by_store,
        "user_contexts": _user_contexts,
    })


_boot_state = _load_bot_state()
if _boot_state.get("active_config"):
    _active_config = str(_boot_state.get("active_config"))
if isinstance(_boot_state.get("active_product_by_store"), dict):
    _active_product_by_store = dict(_boot_state.get("active_product_by_store"))
if _boot_state.get("active_store_key"):
    _active_store_key = str(_boot_state.get("active_store_key"))
if isinstance(_boot_state.get("user_contexts"), dict):
    _user_contexts = dict(_boot_state.get("user_contexts"))


# ---------------------------------------------------------------------------
# Multi-store helpers
# ---------------------------------------------------------------------------

def _normalize_key(value: str) -> str:
    value = (value or "").strip().lower()
    out = []
    prev_dash = False
    for ch in value:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")

def _get_available_configs() -> list:
    """Return list of config names found in configs/*.json (excluding template)."""
    configs_dir = SCRIPT_DIR / "configs"
    if not configs_dir.exists():
        return [PIPELINE_CONFIG]
    names = sorted([
        p.stem for p in configs_dir.glob("*.json")
        if p.stem not in ("template",)
    ])
    return names if names else [PIPELINE_CONFIG]


def _config_env_key(config_name: str) -> str:
    return config_name.upper().replace("-", "_")


def _get_config_data(config_name: str) -> dict:
    """Load and return the raw config JSON dict for *config_name*."""
    config_path = SCRIPT_DIR / "configs" / f"{config_name}.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except Exception:
            return {}
    return {}


def _get_config_runtime(config_name: str) -> dict:
    data = _get_config_data(config_name)
    runtime = data.get("runtime", {}) if isinstance(data, dict) else {}

    env_key = _config_env_key(config_name)
    job = (
        runtime.get("cloud_run_job")
        or os.environ.get(f"PIPELINE_CLOUD_RUN_JOB_{env_key}", "")
        or PIPELINE_CLOUD_RUN_JOB
        or f"{config_name}-pipeline"
    )
    region = (
        runtime.get("cloud_run_region")
        or os.environ.get(f"PIPELINE_CLOUD_RUN_REGION_{env_key}", "")
        or PIPELINE_CLOUD_RUN_REGION
        or "us-central1"
    )
    return {"job": job, "region": region}


def _get_store_product_registry() -> list:
    """Build store/product registry from config files."""
    registry = []
    for config_name in _get_available_configs():
        data = _get_config_data(config_name)
        store_name = data.get("store", {}).get("name") or data.get("store", {}).get("url") or config_name
        product_name = data.get("product", {}).get("type") or data.get("product", {}).get("name") or config_name
        runtime = _get_config_runtime(config_name)
        registry.append({
            "config": config_name,
            "store_name": store_name,
            "store_key": _normalize_key(store_name),
            "product_name": product_name,
            "product_key": _normalize_key(product_name),
            "bucket": data.get("gcs", {}).get("bucket") or GCS_BUCKET,
            "description": data.get("description", ""),
            "job": runtime["job"],
            "region": runtime["region"],
        })
    return registry


def _find_registry_entry_by_config(config_name: str) -> dict:
    for entry in _get_store_product_registry():
        if entry["config"] == config_name:
            return entry
    return {}


def _get_store_groups() -> dict:
    """Return grouped stores with their products."""
    stores = {}
    for entry in _get_store_product_registry():
        key = entry["store_key"]
        if key not in stores:
            stores[key] = {
                "store_key": key,
                "store_name": entry["store_name"],
                "products": [],
            }
        stores[key]["products"].append(entry)

    for value in stores.values():
        value["products"] = sorted(value["products"], key=lambda p: (p["product_name"], p["config"]))
    return stores


def _resolve_store_key(query: str) -> str:
    query_n = _normalize_key(query)
    stores = _get_store_groups()
    if query_n in stores:
        return query_n
    for key, value in stores.items():
        if query_n and (query_n in key or query_n in _normalize_key(value["store_name"])):
            return key
    return ""


def _resolve_product_entry(store_key: str, query: str) -> dict:
    query_n = _normalize_key(query)
    store = _get_store_groups().get(store_key)
    if not store:
        return {}
    for entry in store["products"]:
        if query_n in (entry["product_key"], _normalize_key(entry["config"])):
            return entry
    for entry in store["products"]:
        if query_n and (
            query_n in entry["product_key"]
            or query_n in _normalize_key(entry["product_name"])
            or query_n in _normalize_key(entry["config"])
        ):
            return entry
    return {}


def _get_active_entry(user_id: str = None) -> dict:
    global _active_store_key, _active_config

    stores = _get_store_groups()
    if not stores:
        return {}

    # If user_id provided, use per-user context
    if user_id:
        ctx = _get_user_context(user_id)
        u_store_key = ctx.get("store_key", "")
        u_config = ctx.get("config", PIPELINE_CONFIG)
        u_product_by_store = ctx.get("product_by_store", {})

        if not u_store_key:
            by_config = _find_registry_entry_by_config(u_config)
            if by_config:
                u_store_key = by_config["store_key"]
                u_product_by_store[u_store_key] = by_config["product_key"]
                ctx["store_key"] = u_store_key
                ctx["product_by_store"] = u_product_by_store

        if u_store_key not in stores:
            # Fall back to first store the user has access to
            user_stores = _get_user(user_id).get("stores", [])
            if "*" in user_stores:
                u_store_key = sorted(stores.keys())[0]
            else:
                accessible = [k for k in sorted(stores.keys()) if k in user_stores]
                u_store_key = accessible[0] if accessible else sorted(stores.keys())[0]
            ctx["store_key"] = u_store_key

        store = stores[u_store_key]
        product_key = u_product_by_store.get(u_store_key, "")

        chosen = None
        if product_key:
            for entry in store["products"]:
                if entry["product_key"] == product_key:
                    chosen = entry
                    break
        if chosen is None:
            chosen = store["products"][0]
            u_product_by_store[u_store_key] = chosen["product_key"]
            ctx["product_by_store"] = u_product_by_store

        ctx["config"] = chosen["config"]
        return chosen

    # Legacy path (no user_id) — uses global context
    # Bootstrap active context from active config if needed.
    if not _active_store_key:
        by_config = _find_registry_entry_by_config(_active_config)
        if by_config:
            _active_store_key = by_config["store_key"]
            _active_product_by_store[_active_store_key] = by_config["product_key"]

    if _active_store_key not in stores:
        _active_store_key = sorted(stores.keys())[0]

    store = stores[_active_store_key]
    product_key = _active_product_by_store.get(_active_store_key, "")

    chosen = None
    if product_key:
        for entry in store["products"]:
            if entry["product_key"] == product_key:
                chosen = entry
                break

    if chosen is None:
        chosen = store["products"][0]
        _active_product_by_store[_active_store_key] = chosen["product_key"]

    _active_config = chosen["config"]
    return chosen


def _format_run_preview(config_name: str, extra_args: list) -> str:
    entry = _find_registry_entry_by_config(config_name)
    runtime = _get_config_runtime(config_name)
    args_display = " ".join(extra_args) if extra_args else "(full pipeline)"
    safety = []
    if "--continue-on-issues" in extra_args:
        safety.append("continue-on-issues: <b>enabled</b>")
    else:
        safety.append("continue-on-issues: <b>disabled</b> (fail-fast)")
    if "--allow-fallback-metadata" in extra_args:
        safety.append("fallback metadata: <b>enabled</b>")
    else:
        safety.append("fallback metadata: <b>disabled</b>")

    return (
        "<b>Confirm pipeline run</b>\n"
        f"Store: <b>{escape(entry.get('store_name', '?'))}</b>\n"
        f"Product: <b>{escape(entry.get('product_name', '?'))}</b>\n"
        f"Config: <code>{escape(config_name)}</code>\n"
        f"Bucket: <code>{escape(entry.get('bucket', _get_gcs_bucket(config_name)))}</code>\n"
        f"Job: <code>{escape(runtime.get('job', PIPELINE_CLOUD_RUN_JOB or 'local'))}</code>\n"
        f"Region: <code>{escape(runtime.get('region', PIPELINE_CLOUD_RUN_REGION))}</code>\n"
        f"Args: <code>{escape(args_display)}</code>\n"
        f"Confirmation window: <b>{PENDING_RUN_TTL_SECONDS}s</b>\n"
        f"Safety: {', '.join(safety)}\n\n"
        "Reply <code>/confirm</code> to start, or <code>/cancel</code> to discard."
    )


def _pending_run_is_expired() -> bool:
    if not _pending_run:
        return False
    created = int(_pending_run.get("created_at", 0) or 0)
    if created <= 0:
        return True
    return int(time.time()) > (created + PENDING_RUN_TTL_SECONDS)


def _pending_run_remaining_seconds() -> int:
    if not _pending_run:
        return 0
    created = int(_pending_run.get("created_at", 0) or 0)
    if created <= 0:
        return 0
    remaining = (created + PENDING_RUN_TTL_SECONDS) - int(time.time())
    return max(0, remaining)


def _get_gcs_bucket(config_name: str) -> str:
    """Return the GCS bucket name for *config_name*, falling back to GCS_BUCKET env var."""
    data = _get_config_data(config_name)
    return data.get("gcs", {}).get("bucket") or GCS_BUCKET


def _get_designs_prefix(config_name: str) -> str:
    """Return the GCS designs prefix for *config_name*."""
    data = _get_config_data(config_name)
    return data.get("gcs", {}).get("designs_prefix", "designs/")


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def send_message(chat_id: str, text: str, parse_mode: str = "HTML") -> None:
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=15,
        )
    except Exception as exc:
        print(f"[send_message] error: {exc}")


def send_photo_bytes(chat_id: str, photo_data: bytes, caption: str = "", filename: str = "image.jpg") -> None:
    """Send a photo from bytes to a Telegram chat."""
    try:
        requests.post(
            f"{TELEGRAM_API}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": (filename, photo_data)},
            timeout=60,
        )
    except Exception as exc:
        print(f"[send_photo_bytes] error: {exc}")


def send_document(chat_id: str, file_path: str, caption: str = "") -> None:
    try:
        with open(file_path, "rb") as f:
            requests.post(
                f"{TELEGRAM_API}/sendDocument",
                data={"chat_id": chat_id, "caption": caption},
                files={"document": f},
                timeout=30,
            )
    except Exception as exc:
        send_message(chat_id, f"❌ Failed to send file: {exc}")


def get_updates(offset: int = 0):
    try:
        r = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
            timeout=40,
        )
        return r.json().get("result", [])
    except Exception as exc:
        print(f"[get_updates] error: {exc}")
        return []


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Acknowledge a callback query (dismiss the spinner on the button)."""
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except Exception as exc:
        print(f"[answer_callback_query] error: {exc}")


def edit_message_reply_markup(chat_id: str, message_id: int, reply_markup: dict = None) -> None:
    """Remove or update inline keyboard on an existing message."""
    try:
        payload: dict = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        requests.post(
            f"{TELEGRAM_API}/editMessageReplyMarkup",
            json=payload,
            timeout=10,
        )
    except Exception as exc:
        print(f"[edit_message_reply_markup] error: {exc}")


# ---------------------------------------------------------------------------
# Pipeline approval callback handler (GCS-based signaling)
# ---------------------------------------------------------------------------

def _handle_approval_callback(callback_query: dict) -> None:
    """
    Handle ✅ Approve / 🔄 Regenerate button presses from pipeline approval
    messages.  Writes the decision to GCS so the running pipeline can read it.

    callback_data format:  "approve:{request_id}" or "regen:{request_id}"
    """
    cb_id = callback_query.get("id", "")
    data = callback_query.get("data", "")
    msg = callback_query.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")

    if ":" not in data:
        answer_callback_query(cb_id, "Unknown action")
        return

    action, request_id = data.split(":", 1)

    if action == "approve":
        decision = "approved"
        label = "✅ Approved"
    elif action == "regen":
        decision = "regenerate"
        label = "🔄 Regenerating"
    else:
        answer_callback_query(cb_id, "Unknown action")
        return

    # Write decision to GCS
    try:
        from google.cloud import storage as _gcs
        bucket = _gcs.Client(project=GCP_PROJECT).bucket(GCS_BUCKET)
        blob = bucket.blob(f"approvals/{request_id}.json")
        blob.upload_from_string(
            json.dumps({"status": decision, "decided_at": time.time()}),
            content_type="application/json",
        )
        answer_callback_query(cb_id, label)
        send_message(chat_id, f"{label} — <code>{request_id}</code>")
        # Remove buttons from original message
        if message_id:
            edit_message_reply_markup(chat_id, message_id)
    except Exception as exc:
        answer_callback_query(cb_id, f"Error: {exc}")
        print(f"[approval_callback] error writing to GCS: {exc}")



# ---------------------------------------------------------------------------
# Command parser
# ---------------------------------------------------------------------------

def parse_run_command(text: str) -> list:
    """
    Convert '/run [options]' into (config_name, CLI args) for product_automation_script.py.
    If 'store <name>' / 'product <name>' appear in options, they override current context for this run only.

    Examples:
      /run                       → []
      /run step 3                → ["--step", "3"]
      /run sku 7                 → ["--sku", "7"]
      /run dry                   → ["--dry-run"]
      /run force                 → ["--force"]
      /run failed                → ["--failed-only"]
      /run step 2 sku 5 dry      → ["--step", "2", "--sku", "5", "--dry-run"]
      /run workers 5             → ["--workers", "5"]
      /run store tipcat product mouse-pad step 1 → config="tipcat-mousepads", ["--step", "1"]
      /run continue              → ["--continue-on-issues"]
      /run fallback              → ["--allow-fallback-metadata"]
    """
    tokens = text.strip().lower().split()[1:]  # drop "/run"
    args = []
    run_store = ""
    run_product = ""
    run_config = ""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "store" and i + 1 < len(tokens):
            run_store = tokens[i + 1]
            i += 2
        elif t in ("product", "prod") and i + 1 < len(tokens):
            run_product = tokens[i + 1]
            i += 2
        elif t == "config" and i + 1 < len(tokens):
            run_config = tokens[i + 1]
            i += 2
        elif t == "step" and i + 1 < len(tokens):
            args += ["--step", tokens[i + 1]]
            i += 2
        elif t in ("sku", "skus") and i + 1 < len(tokens):
            args += ["--sku", tokens[i + 1]]
            i += 2
        elif t == "workers" and i + 1 < len(tokens):
            args += ["--workers", tokens[i + 1]]
            i += 2
        elif t == "dry":
            args.append("--dry-run")
            i += 1
        elif t == "force":
            args.append("--force")
            i += 1
        elif t in ("failed", "retry"):
            args.append("--failed-only")
            i += 1
        elif t in ("continue", "continue-on-issues"):
            args.append("--continue-on-issues")
            i += 1
        elif t in ("fallback", "allow-fallback"):
            args.append("--allow-fallback-metadata")
            i += 1
        elif t.isdigit():
            args += ["--step", t]
            i += 1
        else:
            i += 1

    # Resolve run config from explicit config, or from store/product context overrides.
    resolved_config = ""
    if run_config:
        match = _find_registry_entry_by_config(run_config)
        resolved_config = match.get("config", run_config)
    elif run_store or run_product:
        active = _get_active_entry()
        target_store_key = _resolve_store_key(run_store) if run_store else active.get("store_key", "")
        if target_store_key:
            if run_product:
                product_entry = _resolve_product_entry(target_store_key, run_product)
                resolved_config = product_entry.get("config", "")
            else:
                preferred_key = _active_product_by_store.get(target_store_key, "")
                if preferred_key:
                    preferred = _resolve_product_entry(target_store_key, preferred_key)
                    resolved_config = preferred.get("config", "")
                if not resolved_config:
                    store = _get_store_groups().get(target_store_key, {})
                    if store.get("products"):
                        resolved_config = store["products"][0]["config"]

    return (resolved_config or None, args)


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------

def build_cmd(extra_args: list) -> list:
    """Build the full command list for subprocess."""
    active = _get_active_entry()
    return build_cmd_for(extra_args, active.get("config", _active_config))


def build_cmd_for(extra_args: list, config_name: str) -> list:
    """Build the full command list for subprocess using a specific config."""
    return [
        sys.executable,
        str(PIPELINE_SCRIPT),
        "--config", config_name,
    ] + extra_args


def run_pipeline_local(extra_args: list, chat_id: str, config_name: str = None) -> None:
    """Launch pipeline as a background subprocess."""
    global _current_process, _current_run_meta
    if _current_process and _current_process.poll() is None:
        send_message(chat_id, "⚠️ Pipeline already running. Use /cancel to stop it.")
        return
    active = _get_active_entry()
    effective_config = config_name or active.get("config", _active_config)
    entry = _find_registry_entry_by_config(effective_config)
    cmd = build_cmd_for(extra_args, effective_config)
    display = " ".join(extra_args) or "(full pipeline)"
    _current_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(SCRIPT_DIR),
    )
    _current_run_meta = {
        "config": effective_config,
        "args": list(extra_args),
        "chat_id": chat_id,
        "source": "Local pipeline",
    }
    send_message(
        chat_id,
        f"🚀 Pipeline started [<b>{escape(effective_config)}</b>]"
        f" ({escape(entry.get('store_name', '?'))} / {escape(entry.get('product_name', '?'))})"
        f": <code>{display}</code>\nPID: {_current_process.pid}",
    )


def trigger_cloud_run(extra_args: list, chat_id: str, config_name: str = None) -> None:
    """Trigger a Cloud Run job execution with overridden args."""
    active = _get_active_entry()
    effective_config = config_name or active.get("config", _active_config)
    entry = _find_registry_entry_by_config(effective_config)
    runtime = _get_config_runtime(effective_config)
    target_job = runtime["job"]
    target_region = runtime["region"]
    display = " ".join(extra_args) or "(full pipeline)"

    # Preferred path: Cloud Run Jobs REST API (works inside Cloud Run container,
    # no gcloud binary required).
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(GoogleAuthRequest())

        endpoint = (
            f"https://run.googleapis.com/v2/projects/{GCP_PROJECT}"
            f"/locations/{target_region}/jobs/{target_job}:run"
        )

        body = {
            "overrides": {
                "containerOverrides": [
                    {
                        "args": ["--config", effective_config] + extra_args
                    }
                ]
            }
        }

        r = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )

        if r.ok:
            payload = r.json() if r.content else {}
            op_name = payload.get("name", "")
            suffix = f"\nOperation: <code>{escape(op_name)}</code>" if op_name else ""
            send_message(
                chat_id,
                f"🚀 Cloud Run job <b>{escape(target_job)}</b> triggered.\n"
                f"[<b>{escape(effective_config)}</b>] ({escape(entry.get('store_name', '?'))} / {escape(entry.get('product_name', '?'))}) "
                f"<code>{display}</code>{suffix}",
            )
            if op_name:
                monitor = threading.Thread(
                    target=_monitor_cloud_run_operation,
                    args=(op_name, chat_id, effective_config, list(extra_args), target_job, target_region),
                    daemon=True,
                )
                monitor.start()
            return

        # If API call fails and gcloud exists, fall back to CLI for diagnostics.
        api_err = (r.text or "")[:800]
        if shutil.which("gcloud"):
            cmd = [
                "gcloud", "run", "jobs", "execute", target_job,
                f"--region={target_region}",
                f"--project={GCP_PROJECT}",
                "--async",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                send_message(chat_id, f"🚀 Cloud Run job <b>{escape(target_job)}</b> triggered.\n<code>{display}</code>")
                return
            cli_err = (result.stderr or result.stdout or "")[:600]
            send_message(
                chat_id,
                "❌ Cloud Run trigger failed.\n"
                f"API: <code>{escape(api_err)}</code>\n"
                f"CLI: <code>{escape(cli_err)}</code>",
            )
            return

        send_message(chat_id, f"❌ Cloud Run trigger failed:\n<code>{escape(api_err)}</code>")
    except Exception as exc:
        send_message(chat_id, f"❌ Error triggering Cloud Run: {exc}")


def dispatch_run(extra_args: list, chat_id: str, config_name: str = None) -> None:
    """Route to Cloud Run or local subprocess based on config."""
    if PIPELINE_CLOUD_RUN_JOB:
        trigger_cloud_run(extra_args, chat_id, config_name)
    else:
        run_pipeline_local(extra_args, chat_id, config_name)


# ---------------------------------------------------------------------------
# Pipeline status
# ---------------------------------------------------------------------------

def _step_target_from_args(extra_args: list) -> int:
    for idx, arg in enumerate(extra_args):
        if arg == "--step" and idx + 1 < len(extra_args):
            try:
                return int(extra_args[idx + 1])
            except Exception:
                return 0
    return 0


def _load_state_for_config(config_name: str) -> dict:
    if STATE_PATH.exists() and config_name == _get_active_entry().get("config", _active_config):
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    try:
        from google.cloud import storage

        client = storage.Client(project=GCP_PROJECT)
        bucket = _get_gcs_bucket(config_name)
        blob = client.bucket(bucket).blob("output/pipeline_state.json")
        return json.loads(blob.download_as_text())
    except Exception:
        return {}


def _summarize_step_state(step_num: int, state: dict) -> tuple:
    step_key = f"step{step_num}"
    step_state = state.get(step_key, {}) or {}
    values = list(step_state.items())
    total = len(values)
    if step_num == 1:
        done = sum(1 for _, v in values if v == "success")
        review = sum(1 for _, v in values if v == "needs_review")
    elif step_num == 4:
        done = sum(1 for _, v in values if isinstance(v, str) and v.startswith("gid://"))
        review = 0
    else:
        done = sum(1 for _, v in values if v == "done")
        review = sum(1 for _, v in values if v == "partial")
    failed_items = [(sku, v) for sku, v in values if isinstance(v, str) and v.startswith("failed")]
    return total, done, review, failed_items


def _build_completion_summary(config_name: str, extra_args: list, success: bool, source: str, error_text: str = "") -> str:
    entry = _find_registry_entry_by_config(config_name)
    state = _load_state_for_config(config_name)
    target_step = _step_target_from_args(extra_args)
    args_display = " ".join(extra_args) if extra_args else "(full pipeline)"
    icon = "✅" if success else "❌"
    lines = [
        f"{icon} <b>{escape(source)}</b> finished",
        f"Store/Product: <b>{escape(entry.get('store_name', '?'))}</b> / <b>{escape(entry.get('product_name', '?'))}</b>",
        f"Config: <code>{escape(config_name)}</code>",
        f"Args: <code>{escape(args_display)}</code>",
    ]

    step_labels = {
        1: "Step 1 — Metadata",
        2: "Step 2 — Printify mockups",
        3: "Step 3 — Lifestyle mockups",
        4: "Step 4 — Shopify create",
        5: "Step 5 — Shopify images",
    }

    if target_step in step_labels:
        total, done, review, failed_items = _summarize_step_state(target_step, state)
        lines.append(f"\n<b>{step_labels[target_step]}</b>")
        if total == 0:
            lines.append("No state entries found yet.")
        else:
            detail = f"Passed: <b>{done}</b> / {total}"
            if review:
                detail += f"  Review/partial: <b>{review}</b>"
            if failed_items:
                detail += f"  Failed: <b>{len(failed_items)}</b>"
            lines.append(detail)
            for sku, reason in failed_items[:5]:
                lines.append(f"  • <code>{escape(str(sku))}</code>: {escape(str(reason))}")
    else:
        lines.append("\n<b>Current pipeline state</b>")
        for step_num in range(1, 6):
            total, done, review, failed_items = _summarize_step_state(step_num, state)
            label = step_labels[step_num]
            if total == 0:
                lines.append(f"  {label}: <i>not started</i>")
            else:
                detail = f"  {label}: <b>{done}</b> / {total}"
                if review:
                    detail += f"  review/partial {review}"
                if failed_items:
                    detail += f"  failed {len(failed_items)}"
                lines.append(detail)

    if error_text:
        lines.append(f"\nError: <code>{escape(error_text[:500])}</code>")
    return "\n".join(lines)


def _monitor_cloud_run_operation(op_name: str, chat_id: str, config_name: str, extra_args: list, target_job: str, target_region: str) -> None:
    """Poll Cloud Run operation until completion, then send a result summary."""
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        endpoint = f"https://run.googleapis.com/v2/{op_name}"

        while True:
            credentials.refresh(GoogleAuthRequest())
            response = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {credentials.token}"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
            if payload.get("done"):
                error = payload.get("error") or {}
                success = not error
                if success:
                    time.sleep(3)
                send_message(
                    chat_id,
                    _build_completion_summary(
                        config_name,
                        extra_args,
                        success=success,
                        source=f"Cloud Run job {target_job}",
                        error_text=error.get("message", ""),
                    ),
                )
                return
            time.sleep(8)
    except Exception as exc:
        send_message(chat_id, f"⚠️ Could not monitor Cloud Run completion for <code>{escape(target_job)}</code>: {escape(str(exc))}")

def _load_state() -> dict:
    """Load pipeline state from local file or GCS."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    try:
        from google.cloud import storage
        client = storage.Client(project=GCP_PROJECT)
        active = _get_active_entry()
        bucket = active.get("bucket") or _get_gcs_bucket(active.get("config", _active_config))
        blob = client.bucket(bucket).blob("output/pipeline_state.json")
        return json.loads(blob.download_as_text())
    except Exception:
        return {}


def get_status_message() -> str:
    active = _get_active_entry()
    active_config = active.get("config", _active_config)
    state = _load_state()
    if not state:
        return (
            f"No pipeline state found for <b>{escape(active_config)}</b>"
            f" ({escape(active.get('store_name', '?'))} / {escape(active.get('product_name', '?'))}).\n"
            "Run /run to start the pipeline, or use /store and /product to switch context."
        )

    lines = [
        f"<b>Pipeline: {escape(active_config)}</b>",
        f"Store: <b>{escape(active.get('store_name', '?'))}</b>",
        f"Product: <b>{escape(active.get('product_name', '?'))}</b>",
        f"Bucket: <code>{escape(active.get('bucket', _get_gcs_bucket(active_config)))}</code>",
    ]
    step_labels = {
        "step1": "Step 1 — Metadata (Gemini)",
        "step2": "Step 2 — Printify mockups",
        "step3": "Step 3 — Lifestyle mockups",
        "step4": "Step 4 — Shopify create",
        "step5": "Step 5 — Shopify images",
    }
    for key, label in step_labels.items():
        step_state = state.get(key, {})
        total = len(step_state)
        if total == 0:
            lines.append(f"  {label}: <i>not started</i>")
            continue
        done = sum(
            1 for v in step_state.values()
            if isinstance(v, str) and (
                v in ("done", "success", "needs_review", "partial")
                or v.startswith("gid://")
            )
        )
        failed = sum(
            1 for v in step_state.values()
            if isinstance(v, str) and v.startswith("failed")
        )
        lines.append(f"  {label}: ✅ {done} / {total}{f'  ❌ {failed} failed' if failed else ''}")

    # Running process indicator
    if _current_process and _current_process.poll() is None:
        lines.append(f"\n▶️ Pipeline running (PID {_current_process.pid})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

HELP_TEXT = """<b>Tip Cat Pipeline Bot</b>

<u>Run commands:</u>
  /run                  Full pipeline (all steps)
  /run step 2           Single step
  /run sku 5            Single SKU
  /run dry              Dry run (no API calls)
  /run force            Re-process completed SKUs
  /run failed           Only failed/incomplete SKUs
  /run workers 5        Set parallel worker count
    /run store tipcat      One-off store override for this run
    /run product mouse-pad One-off product override for this run
    /run continue         Continue to next steps even if a step has issues
    /run fallback         Allow Step 4 fallback metadata publish
    /confirm              Confirm and launch staged /run command

<u>Store / product context:</u>
    /store                 List stores + current
    /store tipcat          Switch active store
    /product               List products for current store
    /product mouse-pad     Switch product for current store

<u>Designs:</u>
    /design list          List designs in active store GCS bucket
    /design map           Show SKU map summary (pending/active/skip counts)
    /design map all       Full list of all designs + SKU assignments
    /design skip &lt;name&gt;   Exclude a design from pipeline processing
    /design activate &lt;name&gt;  Re-include a skipped/archived design
    /design delete &lt;name&gt;  Delete design from GCS + SKU map
    /design add &lt;url&gt; [name]  Download image from URL → GCS designs/
    /upload [name]        Upload attached photo/image file to current context
    Send a photo or image file with caption "/design [name]" or "/upload [name]"
    Send a ZIP file with caption "/upload" to batch-upload designs

<u>AI Design Generation:</u>
    /generate &lt;prompt&gt;     Generate a design image with Nano Banana Pro
    Attach a photo with /generate caption to use it as reference

<u>Info:</u>
  /status               Per-step progress counts
  /report               Download pipeline_report.csv
  /whoami               Show your user ID, role, allowed stores

<u>Admin:</u>
  /users                List authorized users + groups
  /adduser &lt;id&gt; &lt;name&gt; [stores] [role]  Grant bot access
  /removeuser &lt;id&gt;     Revoke access
  /addgroup [chat_id]   Allow a group chat (or run in the group)
  /removegroup [chat_id] Remove a group

<u>Control:</u>
  /reset 3              Clear step N state + output files
    /cancel               Stop running pipeline / discard staged run
  /help                 This message"""


# ---------------------------------------------------------------------------
# /generate — AI design generation via Gemini Nano Banana Pro
# ---------------------------------------------------------------------------

GENERATE_MODEL = "nano-banana-pro-preview"

def _handle_generate(prompt_text: str, chat_id: str, reference_photo: bytes = None) -> None:
    """
    Generate a design image using Gemini Nano Banana Pro Preview.
    Optionally accepts a reference photo for image-to-image generation.
    """
    if not prompt_text.strip():
        send_message(
            chat_id,
            "Usage: <code>/generate &lt;prompt&gt;</code>\n\n"
            "Examples:\n"
            "  <code>/generate cute kawaii cat pattern with pastel colors on white background</code>\n"
            "  <code>/generate dark souls bonfire pixel art phone case design</code>\n\n"
            "You can also attach/reply with a photo to use it as a reference.",
        )
        return

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        send_message(chat_id, "❌ GEMINI_API_KEY not configured.")
        return

    send_message(chat_id, f"🎨 Generating design...\n<i>{escape(prompt_text[:200])}</i>")

    try:
        from google import genai
        from google.genai import types
        from PIL import Image as PILImage

        client = genai.Client(api_key=gemini_key)

        contents = [prompt_text]
        if reference_photo:
            img = PILImage.open(io.BytesIO(reference_photo))
            contents.append(img)

        resp = client.models.generate_content(
            model=GENERATE_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
            ),
        )

        image_sent = False
        text_parts = []
        for part in resp.candidates[0].content.parts:
            if hasattr(part, "inline_data") and part.inline_data and part.inline_data.mime_type.startswith("image/"):
                ext = "jpg" if "jpeg" in part.inline_data.mime_type else "png"
                send_photo_bytes(
                    chat_id,
                    part.inline_data.data,
                    caption=f"🖼 {escape(prompt_text[:200])}",
                    filename=f"design.{ext}",
                )
                image_sent = True
            elif hasattr(part, "text") and part.text:
                text_parts.append(part.text)

        if text_parts:
            send_message(chat_id, escape("\n".join(text_parts)[:1000]))

        if not image_sent:
            send_message(chat_id, "⚠️ Model returned no image. Try rephrasing your prompt.")

    except Exception as exc:
        send_message(chat_id, f"❌ Generation failed: <code>{escape(str(exc)[:300])}</code>")


def _upload_design_bytes(data: bytes, filename: str, chat_id: str) -> None:
    """Upload raw image bytes to the active config's GCS designs/ prefix."""
    try:
        upload = _store_design_bytes(data, filename)
        size_kb = len(data) // 1024

        # Register in SKU map immediately so it gets a stable SKU
        sku_msg = ""
        try:
            sku_map = _load_sku_map_from_gcs()
            designs = sku_map.setdefault("designs", {})
            if filename not in designs:
                next_sku = sku_map.get("next_sku", 1)
                import datetime as _dt
                designs[filename] = {
                    "sku": str(next_sku),
                    "status": "pending",
                    "added": _dt.datetime.utcnow().isoformat() + "Z",
                }
                sku_map["next_sku"] = next_sku + 1
                _save_sku_map_to_gcs(sku_map)
                sku_msg = f"\nSKU assignment: <b>#{next_sku}</b> (pending)"
            else:
                existing = designs[filename]
                sku_msg = f"\nSKU assignment: <b>#{existing['sku']}</b> ({existing.get('status', '?')})"
        except Exception as map_exc:
            print(f"SKU map update after upload failed: {map_exc}")

        send_message(
            chat_id,
            f"✅ Design uploaded!\n"
            f"<code>gs://{escape(upload['bucket'])}/{escape(upload['blob_path'])}</code> ({size_kb} KB)\n"
            f"Store/Product: <b>{escape(upload['active'].get('store_name', '?'))}</b> / <b>{escape(upload['active'].get('product_name', '?'))}</b>\n"
            f"Config: <code>{escape(upload['config'])}</code>"
            f"{sku_msg}\n\n"
            f"Use <code>/run step 1</code> to generate metadata for new designs.",
        )
    except Exception as exc:
        send_message(chat_id, f"❌ GCS upload failed: {exc}")


def _store_design_bytes(data: bytes, filename: str) -> dict:
    """Upload raw image bytes to the active context and return upload metadata."""
    from google.cloud import storage

    active = _get_active_entry()
    active_config = active.get("config", _active_config)
    bucket_name = active.get("bucket") or _get_gcs_bucket(active_config)
    prefix = _get_designs_prefix(active_config)
    blob_path = f"{prefix}{filename}"
    client = storage.Client(project=GCP_PROJECT)
    content_type = (
        "image/png" if filename.lower().endswith(".png")
        else "image/webp" if filename.lower().endswith(".webp")
        else "image/jpeg"
    )
    client.bucket(bucket_name).blob(blob_path).upload_from_string(data, content_type=content_type)
    return {
        "active": active,
        "config": active_config,
        "bucket": bucket_name,
        "blob_path": blob_path,
        "content_type": content_type,
    }


def _download_telegram_file_bytes(file_id: str) -> tuple:
    """Download a Telegram file and return (bytes, file_path)."""
    response = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=15)
    response.raise_for_status()
    file_path = response.json().get("result", {}).get("file_path", "")
    if not file_path:
        raise RuntimeError("Could not get file path from Telegram")
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    payload = requests.get(file_url, timeout=120)
    payload.raise_for_status()
    return payload.content, file_path


def _upload_design_from_url(url: str, filename: str, chat_id: str) -> None:
    """Download image from URL and upload to the active config's GCS designs/ bucket."""
    try:
        send_message(chat_id, f"⬇️ Downloading <code>{escape(filename)}</code>…")
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        _upload_design_bytes(response.content, filename, chat_id)
    except Exception as exc:
        send_message(chat_id, f"❌ Download failed: {exc}")


def _upload_design_from_telegram_photo(file_id: str, filename: str, chat_id: str) -> None:
    """Download a Telegram photo and upload it to GCS designs/."""
    try:
        img_bytes, file_path = _download_telegram_file_bytes(file_id)
        if not filename:
            filename = Path(file_path).name

        if not any(filename.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            filename += ".jpg"

        send_message(chat_id, f"⬇️ Downloading photo as <code>{escape(filename)}</code>…")
        _upload_design_bytes(img_bytes, filename, chat_id)
    except Exception as exc:
        send_message(chat_id, f"❌ Photo upload failed: {exc}")


def _upload_designs_from_zip(file_id: str, archive_name: str, chat_id: str) -> None:
    """Download a ZIP archive, extract supported images, and upload them in batch."""
    try:
        send_message(chat_id, f"📦 Downloading archive <code>{escape(archive_name or 'designs.zip')}</code>…")
        zip_bytes, file_path = _download_telegram_file_bytes(file_id)
        display_name = archive_name or Path(file_path).name or "designs.zip"
        uploaded = []
        skipped = []
        seen_names = set()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if not members:
                send_message(chat_id, "❌ ZIP archive is empty.")
                return

            for member in members:
                inner_name = member.filename.split("/")[-1]
                if not inner_name or inner_name.startswith(".") or member.filename.startswith("__MACOSX/"):
                    continue
                lower = inner_name.lower()
                if not lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    skipped.append(inner_name)
                    continue

                base_name = _sanitize_design_filename(inner_name, fallback_ext=Path(inner_name).suffix.lower() or ".png")
                final_name = base_name
                counter = 2
                while final_name.lower() in seen_names:
                    stem = Path(base_name).stem
                    suffix = Path(base_name).suffix or ".png"
                    final_name = f"{stem}-{counter}{suffix}"
                    counter += 1
                seen_names.add(final_name.lower())

                with zf.open(member) as fh:
                    payload = fh.read()
                meta = _store_design_bytes(payload, final_name)
                uploaded.append(meta["blob_path"])

        if not uploaded:
            send_message(
                chat_id,
                "❌ No supported image files found in ZIP.\n"
                "Supported formats: <code>.png</code>, <code>.jpg</code>, <code>.jpeg</code>, <code>.webp</code>",
            )
            return

        active = _get_active_entry()
        lines = [
            f"✅ ZIP upload complete: <b>{escape(display_name)}</b>",
            f"Uploaded: <b>{len(uploaded)}</b> design(s)",
            f"Store/Product: <b>{escape(active.get('store_name', '?'))}</b> / <b>{escape(active.get('product_name', '?'))}</b>",
            f"Config: <code>{escape(active.get('config', _active_config))}</code>",
        ]
        if skipped:
            lines.append(f"Skipped non-image files: <b>{len(skipped)}</b>")
        preview = uploaded[:10]
        if preview:
            lines.append("\nFirst uploaded files:")
            lines.extend([f"  • <code>{escape(p)}</code>" for p in preview])
            if len(uploaded) > len(preview):
                lines.append(f"  … and {len(uploaded) - len(preview)} more")
        lines.append("\nUse <code>/run step 1</code> to generate metadata for the new designs.")
        send_message(chat_id, "\n".join(lines))
    except zipfile.BadZipFile:
        send_message(chat_id, "❌ Invalid ZIP archive.")
    except Exception as exc:
        send_message(chat_id, f"❌ ZIP upload failed: {exc}")


def _sanitize_design_filename(filename: str, fallback_ext: str = ".jpg") -> str:
    """Sanitize a user-supplied filename for design uploads."""
    value = (filename or "").strip()
    if not value:
        value = f"design-{int(time.time())}{fallback_ext}"
    value = value.replace("/", "-").replace("\\", "-")
    value = "-".join(value.split())
    allowed = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            allowed.append(ch)
    value = "".join(allowed).strip("-._") or f"design-{int(time.time())}"
    lower = value.lower()
    if not lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        value += fallback_ext
    return value


def _extract_design_upload_name(text: str, attachment_name: str = "") -> str:
    """Extract optional filename from /design or /upload caption text."""
    text = (text or "").strip()
    if not text:
        return _sanitize_design_filename(attachment_name)

    parts = text.split(maxsplit=2)
    if not parts:
        return _sanitize_design_filename(attachment_name)

    cmd = parts[0].lower().split("@")[0]
    if cmd not in ("/design", "/upload"):
        return _sanitize_design_filename(attachment_name)

    candidate = ""
    if len(parts) >= 3 and parts[1].lower() == "add":
        candidate = parts[2]
    elif len(parts) >= 2:
        candidate = parts[1]

    if candidate.startswith("http://") or candidate.startswith("https://"):
        candidate = attachment_name

    return _sanitize_design_filename(candidate or attachment_name)


# ---------------------------------------------------------------------------
# SKU map helpers — talk to the pipeline's sku_map.json in GCS
# ---------------------------------------------------------------------------

def _load_sku_map_from_gcs() -> dict:
    """Read sku_map.json from GCS (or return empty structure)."""
    try:
        from google.cloud import storage
        active = _get_active_entry()
        bucket_name = active.get("bucket") or _get_gcs_bucket(active.get("config", _active_config))
        client = storage.Client(project=GCP_PROJECT)
        blob = client.bucket(bucket_name).blob("sku_map.json")
        if blob.exists():
            return json.loads(blob.download_as_text())
    except Exception as exc:
        print(f"_load_sku_map_from_gcs failed: {exc}")
    return {"next_sku": 1, "designs": {}}


def _save_sku_map_to_gcs(sku_map: dict) -> None:
    """Write sku_map.json back to GCS."""
    from google.cloud import storage
    active = _get_active_entry()
    bucket_name = active.get("bucket") or _get_gcs_bucket(active.get("config", _active_config))
    client = storage.Client(project=GCP_PROJECT)
    client.bucket(bucket_name).blob("sku_map.json").upload_from_string(
        json.dumps(sku_map, indent=2), content_type="application/json"
    )


def _handle_design_map(parts: list, chat_id: str) -> None:
    """Show SKU map summary or full listing."""
    sku_map = _load_sku_map_from_gcs()
    designs = sku_map.get("designs", {})
    if not designs:
        send_message(chat_id, "SKU map is empty. Run the pipeline once or <code>/design list</code> to populate it.")
        return

    show_all = len(parts) >= 3 and parts[2].lower() == "all"

    counts = {"pending": 0, "active": 0, "skip": 0, "archived": 0}
    for entry in designs.values():
        s = entry.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1

    lines = [
        f"<b>SKU Map</b>  ({len(designs)} designs, next SKU: {sku_map.get('next_sku', '?')})",
        f"  🟡 Pending: <b>{counts['pending']}</b>",
        f"  🟢 Active:  <b>{counts['active']}</b>",
        f"  ⏭ Skip:    <b>{counts['skip']}</b>",
        f"  📦 Archived: <b>{counts['archived']}</b>",
    ]

    if show_all:
        lines.append("")
        # Group by status
        for status_label, emoji in [("pending", "🟡"), ("active", "🟢"), ("skip", "⏭"), ("archived", "📦")]:
            entries = [(fname, e) for fname, e in designs.items() if e.get("status") == status_label]
            if not entries:
                continue
            entries.sort(key=lambda x: int(x[1]["sku"]))
            lines.append(f"\n{emoji} <b>{status_label.title()}</b>:")
            for fname, e in entries[:30]:
                lines.append(f"  SKU {e['sku']}: <code>{escape(fname)}</code>")
            if len(entries) > 30:
                lines.append(f"  … and {len(entries) - 30} more")

    lines.append(f"\n<code>/design skip &lt;name&gt;</code> to exclude")
    lines.append(f"<code>/design activate &lt;name&gt;</code> to re-include")
    send_message(chat_id, "\n".join(lines))


def _resolve_design_name(identifier: str, designs: dict) -> tuple:
    """Resolve an identifier (SKU number or filename) to the actual design filename.
    Returns (filename, error_message). If error_message is set, filename is None."""
    # Check if it's a SKU number (digits only, or #NNN)
    sku_str = identifier.lstrip("#")
    if sku_str.isdigit():
        sku_num = int(sku_str)
        for fname, entry in designs.items():
            if entry.get("sku") == sku_num:
                return fname, None
        return None, f"❌ No design found with SKU #{sku_num}."

    # Exact match
    if identifier in designs:
        return identifier, None

    # Partial/fuzzy matches
    matches = [
        k for k in designs
        if k.startswith(identifier)
        or k.replace(".png", "") == identifier
        or k.replace(".jpg", "") == identifier
    ]
    if len(matches) == 1:
        return matches[0], None
    elif len(matches) > 1:
        lines = ["Ambiguous — did you mean one of these?"]
        for m in matches[:10]:
            lines.append(f"  • <code>{escape(m)}</code>")
        return None, "\n".join(lines)
    else:
        return None, f"❌ Design <code>{escape(identifier)}</code> not found in SKU map."


def _handle_design_delete(filename: str, chat_id: str) -> None:
    """Delete a design from GCS and remove it from the SKU map."""
    from google.cloud import storage

    sku_map = _load_sku_map_from_gcs()
    designs = sku_map.get("designs", {})

    resolved, err = _resolve_design_name(filename, designs)
    if err:
        send_message(chat_id, err)
        return
    filename = resolved

    entry = designs[filename]
    sku_num = entry["sku"]

    # Delete the file from GCS
    try:
        active = _get_active_entry()
        bucket_name = active.get("bucket") or _get_gcs_bucket(active.get("config", _active_config))
        client = storage.Client(project=GCP_PROJECT)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(f"designs/{filename}")
        if blob.exists():
            blob.delete()
    except Exception as exc:
        send_message(chat_id, f"⚠️ Could not delete file from GCS: {exc}\nRemoving from SKU map anyway.")

    # Remove from SKU map
    del designs[filename]
    try:
        _save_sku_map_to_gcs(sku_map)
        send_message(
            chat_id,
            f"🗑 Deleted <code>{escape(filename)}</code> (SKU #{sku_num})\n"
            f"File removed from GCS and SKU map.",
        )
    except Exception as exc:
        send_message(chat_id, f"❌ File deleted but failed to update SKU map: {exc}")


def _handle_design_set_status(filename: str, new_status: str, chat_id: str) -> None:
    """Change a design's status in the GCS SKU map."""
    sku_map = _load_sku_map_from_gcs()
    designs = sku_map.get("designs", {})

    resolved, err = _resolve_design_name(filename, designs)
    if err:
        send_message(chat_id, err)
        return
    filename = resolved

    old = designs[filename].get("status", "?")
    designs[filename]["status"] = new_status
    try:
        _save_sku_map_to_gcs(sku_map)
        send_message(
            chat_id,
            f"✅ <code>{escape(filename)}</code> (SKU {designs[filename]['sku']}): "
            f"<b>{old}</b> → <b>{new_status}</b>",
        )
    except Exception as exc:
        send_message(chat_id, f"❌ Failed to save SKU map: {exc}")


def _handle_design(parts: list, text: str, chat_id: str, attachment: dict = None) -> None:
    """Handle /design subcommands: list, add <url>, or photo upload."""
    attachment = attachment or {}
    photo_file_id = attachment.get("file_id")
    attachment_name = attachment.get("file_name", "")
    attachment_kind = attachment.get("kind", "")
    subcommand = parts[1].lower() if len(parts) > 1 else ""

    if subcommand == "list" or (not subcommand and not photo_file_id):
        try:
            from google.cloud import storage

            active = _get_active_entry()
            active_config = active.get("config", _active_config)
            bucket_name = active.get("bucket") or _get_gcs_bucket(active_config)
            prefix = _get_designs_prefix(active_config)
            client = storage.Client(project=GCP_PROJECT)
            blobs = list(client.bucket(bucket_name).list_blobs(prefix=prefix, max_results=30))
            if not blobs:
                send_message(chat_id, f"No designs found in <code>gs://{escape(bucket_name)}/{escape(prefix)}</code>")
                return

            names = [b.name.replace(prefix, "") for b in blobs if not b.name.endswith("/")][:25]
            lines = [f"<b>Designs in {escape(active_config)}</b> ({len(names)}):"]
            lines += [f"  • {escape(n)}" for n in names]
            if len(blobs) >= 30:
                lines.append("  … (showing first 25)")
            send_message(chat_id, "\n".join(lines))
        except Exception as exc:
            send_message(chat_id, f"❌ Failed to list designs: {exc}")
        return

    if subcommand == "map":
        _handle_design_map(parts, chat_id)
        return

    if subcommand == "skip":
        if len(parts) < 3:
            send_message(chat_id, "Usage: <code>/design skip &lt;filename&gt;</code>")
            return
        _handle_design_set_status(parts[2], "skip", chat_id)
        return

    if subcommand == "activate":
        if len(parts) < 3:
            send_message(chat_id, "Usage: <code>/design activate &lt;filename&gt;</code>")
            return
        _handle_design_set_status(parts[2], "pending", chat_id)
        return

    if subcommand in ("delete", "rm", "remove"):
        if len(parts) < 3:
            send_message(chat_id, "Usage: <code>/design delete &lt;filename&gt;</code>")
            return
        _handle_design_delete(parts[2], chat_id)
        return

    if subcommand == "add":
        # If there's an attached photo/file, treat remaining text as filename
        if photo_file_id and attachment_kind != "zip":
            filename = " ".join(parts[2:]) if len(parts) >= 3 else None
            if not filename:
                filename = attachment_name or "design.png"
            filename = _sanitize_design_filename(filename)
            _upload_design_from_telegram_photo(photo_file_id, filename, chat_id)
            return

        if len(parts) < 3:
            send_message(chat_id, "Usage: <code>/design add &lt;image-url&gt; [filename]</code>\nOr send a photo with caption <code>/design add &lt;name&gt;</code>")
            return

        url = parts[2]
        filename = parts[3] if len(parts) >= 4 else (url.split("?")[0].split("/")[-1] or "design.jpg")
        filename = _sanitize_design_filename(filename)
        _upload_design_from_url(url, filename, chat_id)
        return

    if photo_file_id:
        if attachment_kind == "zip":
            _upload_designs_from_zip(photo_file_id, attachment_name, chat_id)
            return
        filename = _extract_design_upload_name(text, attachment_name)
        _upload_design_from_telegram_photo(photo_file_id, filename, chat_id)
        return

    send_message(
        chat_id,
        "Usage:\n"
        "  <code>/design list</code> — list designs in GCS\n"
        "  <code>/design add &lt;url&gt; [name]</code> — upload from URL\n"
        "  Send a photo or image file with caption <code>/design [name]</code>\n"
        "  Send a ZIP file with caption <code>/upload</code> to batch-upload designs\n"
        "  Or use <code>/upload [name]</code> as a shortcut",
    )


def handle_command(text: str, chat_id: str, attachment: dict = None, sender_id: str = None) -> None:
    global _current_process
    global _active_config
    global _active_store_key
    global _pending_run
    attachment = attachment or {}
    sender_id = sender_id or chat_id  # fallback for backward compat

    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]  # strip @botname if present

    if cmd in ("/help", "/start"):
        send_message(chat_id, HELP_TEXT)

    elif cmd == "/run":
        if _pending_run and _pending_run_is_expired():
            _pending_run = {}
        run_config, extra_args = parse_run_command(text)
        active = _get_active_entry(user_id=sender_id)
        target_config = run_config or active.get("config", _get_user_context(sender_id).get("config", _active_config))
        # Check store access
        target_entry = _find_registry_entry_by_config(target_config)
        if target_entry and not _user_can_access_store(sender_id, target_entry.get("store_key", "")):
            send_message(chat_id, f"❌ You don't have access to store <b>{escape(target_entry.get('store_name', '?'))}</b>.")
            return
        _pending_run = {
            "config": target_config,
            "args": list(extra_args),
            "chat_id": chat_id,
            "sender_id": sender_id,
            "created_at": int(time.time()),
        }
        send_message(chat_id, _format_run_preview(target_config, extra_args))

    elif cmd == "/confirm":
        if not _pending_run:
            send_message(chat_id, "No staged run found. Send <code>/run ...</code> first.")
            return
        if _pending_run_is_expired():
            _pending_run = {}
            send_message(
                chat_id,
                "⌛ Staged run expired.\n"
                "Please send <code>/run ...</code> again, then <code>/confirm</code> within the confirmation window.",
            )
            return
        remaining = _pending_run_remaining_seconds()
        pending = dict(_pending_run)
        _pending_run = {}
        send_message(chat_id, f"✅ Confirmed ({remaining}s left). Launching job now...")
        dispatch_run(pending.get("args", []), chat_id, config_name=pending.get("config"))

    elif cmd == "/store":
        stores = _get_store_groups()
        active = _get_active_entry(user_id=sender_id)
        if len(parts) == 1:
            lines = ["<b>Available stores</b>"]
            for store_key, store in sorted(stores.items()):
                access = "✅" if _user_can_access_store(sender_id, store_key) else "🔒"
                marker = "▶️" if store_key == active.get("store_key", "") else "  "
                lines.append(f"{marker} {access} <code>{escape(store['store_name'])}</code> ({len(store['products'])} product types)")
            lines.append(
                f"\nCurrent: <b>{escape(active.get('store_name', '?'))}</b> / <b>{escape(active.get('product_name', '?'))}</b>"
                f"\nConfig: <code>{escape(active.get('config', _active_config))}</code>"
                "\nSwitch store: <code>/store &lt;name&gt;</code>"
            )
            send_message(chat_id, "\n".join(lines))
        else:
            match_store = _resolve_store_key(parts[1])
            if match_store:
                if not _user_can_access_store(sender_id, match_store):
                    send_message(chat_id, f"❌ You don't have access to store <code>{escape(parts[1])}</code>.")
                    return
                chosen = None
                store = stores.get(match_store, {})
                if store:
                    chosen = store["products"][0]
                    _set_user_store(sender_id, match_store, chosen["config"])
                    _set_user_product(sender_id, match_store, chosen["product_key"], chosen["config"])
                    # Also update legacy globals for backward compat
                    _active_store_key = match_store
                    _active_config = chosen.get("config", _active_config)
                    _persist_bot_state()
                    send_message(
                        chat_id,
                        f"✅ Active store → <b>{escape(chosen.get('store_name', '?'))}</b>\n"
                        f"Active product: <b>{escape(chosen.get('product_name', '?'))}</b>\n"
                        f"Config: <code>{escape(chosen.get('config', ''))}</code>\n"
                        f"Bucket: <code>{escape(chosen.get('bucket', ''))}</code>",
                    )
                else:
                    send_message(chat_id, f"❌ Store not found: <code>{escape(parts[1])}</code>")
            else:
                send_message(
                    chat_id,
                    f"❌ Unknown store: <code>{escape(parts[1])}</code>",
                )

    elif cmd == "/product":
        active = _get_active_entry(user_id=sender_id)
        store = _get_store_groups().get(active.get("store_key", ""), {})
        products = store.get("products", [])
        if len(parts) == 1:
            lines = [f"<b>Products for {escape(active.get('store_name', '?'))}</b>"]
            for entry in products:
                marker = "▶️" if entry["config"] == active.get("config") else "  "
                lines.append(f"{marker} <code>{escape(entry['product_name'])}</code> → <i>{escape(entry['config'])}</i>")
            lines.append("\nSwitch: <code>/product &lt;name&gt;</code>")
            send_message(chat_id, "\n".join(lines))
        else:
            target = _resolve_product_entry(active.get("store_key", ""), parts[1])
            if not target:
                send_message(chat_id, f"❌ Unknown product for this store: <code>{escape(parts[1])}</code>")
            else:
                _set_user_product(sender_id, active.get("store_key", ""), target["product_key"], target["config"])
                _active_product_by_store[active.get("store_key", "")] = target["product_key"]
                _active_config = target["config"]
                _persist_bot_state()
                send_message(
                    chat_id,
                    f"✅ Active product → <b>{escape(target['product_name'])}</b>\n"
                    f"Store: <b>{escape(target['store_name'])}</b>\n"
                    f"Config: <code>{escape(target['config'])}</code>\n"
                    f"Bucket: <code>{escape(target['bucket'])}</code>",
                )

    elif cmd == "/generate":
        prompt = text[len(parts[0]):].strip()
        ref_file_id = attachment.get("file_id") if attachment else None
        threading.Thread(
            target=_handle_generate,
            args=(prompt, chat_id, ref_file_id),
            daemon=True,
        ).start()

    elif cmd in ("/design", "/upload"):
        if cmd == "/upload" and (len(parts) == 1 or (len(parts) >= 2 and parts[1].lower() != "list")):
            # Remap /upload foo.png -> /design foo.png for shared handling
            remapped = "/design" + text[len(parts[0]):]
            _handle_design(remapped.strip().split(), remapped.strip(), chat_id, attachment)
        else:
            _handle_design(parts, text, chat_id, attachment)

    elif cmd == "/status":
        send_message(chat_id, get_status_message())
        if _current_process and _current_process.poll() is not None:
            rc = _current_process.returncode
            send_message(
                chat_id,
                _build_completion_summary(
                    _current_run_meta.get("config", _get_active_entry().get("config", _active_config)),
                    _current_run_meta.get("args", []),
                    success=(rc == 0),
                    source=_current_run_meta.get("source", "Local pipeline"),
                    error_text=(f"exit code {rc}" if rc != 0 else ""),
                ),
            )
            _current_process = None
            _current_run_meta = {}

    elif cmd == "/report":
        active = _get_active_entry(user_id=sender_id)
        if REPORT_PATH.exists():
            send_document(chat_id, str(REPORT_PATH), caption="Pipeline Report")
        else:
            try:
                from google.cloud import storage

                client = storage.Client(project=GCP_PROJECT)
                blob = client.bucket(active.get("bucket") or _get_gcs_bucket(active.get("config", _active_config))).blob("output/pipeline_report.csv")
                tmp = OUTPUT_DIR / "pipeline_report_tmp.csv"
                blob.download_to_filename(str(tmp))
                send_document(chat_id, str(tmp), caption=f"Pipeline Report ({active.get('store_name', '?')} / {active.get('product_name', '?')})")
            except Exception as exc:
                send_message(chat_id, f"No report found locally or in GCS: {exc}")

    elif cmd == "/reset":
        if len(parts) >= 2 and parts[1].isdigit():
            step_n = parts[1]
            result = subprocess.run(
                build_cmd(["--reset-step", step_n]),
                capture_output=True,
                text=True,
                cwd=str(SCRIPT_DIR),
                timeout=60,
            )
            out = (result.stdout or result.stderr or "")[-600:]
            send_message(chat_id, f"{'✅' if result.returncode == 0 else '❌'} Reset step {step_n}\n<code>{escape(out)}</code>")
        else:
            send_message(chat_id, "Usage: <code>/reset N</code>  (e.g. /reset 3)")

    elif cmd == "/cancel":
        pending_discarded = False
        if _pending_run:
            _pending_run = {}
            pending_discarded = True
        if _current_process and _current_process.poll() is None:
            _current_process.terminate()
            msg = f"🛑 Pipeline (PID {_current_process.pid}) terminated."
            if pending_discarded:
                msg += "\n🧹 Staged run discarded."
            send_message(chat_id, msg)
            _current_process = None
        else:
            if pending_discarded:
                send_message(chat_id, "🧹 Staged run discarded.")
            else:
                send_message(chat_id, "No pipeline process is currently running.")

    elif cmd == "/whoami":
        user_entry = _get_user(sender_id)
        if user_entry:
            stores_str = ", ".join(user_entry.get("stores", [])) or "none"
            send_message(
                chat_id,
                f"<b>Your profile</b>\n"
                f"User ID: <code>{sender_id}</code>\n"
                f"Name: <b>{escape(user_entry.get('name', '?'))}</b>\n"
                f"Role: <b>{user_entry.get('role', '?')}</b>\n"
                f"Stores: <code>{escape(stores_str)}</code>",
            )
        else:
            send_message(chat_id, f"User ID: <code>{sender_id}</code>\nNot in user config.")

    elif cmd == "/users":
        if not _is_admin(sender_id):
            send_message(chat_id, "❌ Admin only.")
            return
        if not _users_config_loaded:
            _load_users_config()
        users = _users_config.get("users", {})
        groups = _users_config.get("allowed_groups", [])
        lines = [f"<b>Authorized users</b> ({len(users)})"]
        for uid, u in sorted(users.items(), key=lambda x: x[1].get("name", "")):
            stores_str = ", ".join(u.get("stores", []))
            lines.append(f"  {'👑' if u.get('role') == 'admin' else '👤'} <b>{escape(u.get('name', '?'))}</b> (<code>{uid}</code>) — {u.get('role', '?')} — stores: <code>{escape(stores_str)}</code>")
        if groups:
            lines.append(f"\n<b>Allowed groups:</b> {', '.join(str(g) for g in groups)}")
        lines.append(f"\n<code>/adduser &lt;user_id&gt; &lt;name&gt; &lt;store&gt;</code>")
        lines.append(f"<code>/removeuser &lt;user_id&gt;</code>")
        lines.append(f"<code>/addgroup &lt;group_chat_id&gt;</code>")
        send_message(chat_id, "\n".join(lines))

    elif cmd == "/adduser":
        if not _is_admin(sender_id):
            send_message(chat_id, "❌ Admin only.")
            return
        # /adduser <user_id> <name> [store1,store2,...] [role]
        if len(parts) < 3:
            send_message(
                chat_id,
                "Usage: <code>/adduser &lt;user_id&gt; &lt;name&gt; [stores] [role]</code>\n"
                "Examples:\n"
                "  <code>/adduser 123456789 Alex tipcat</code>\n"
                "  <code>/adduser 123456789 Alex * admin</code>\n"
                "  <code>/adduser 123456789 Alex store1,store2</code>",
            )
            return
        new_uid = parts[1]
        new_name = parts[2]
        new_stores = parts[3].split(",") if len(parts) >= 4 else ["*"]
        new_role = parts[4] if len(parts) >= 5 else "user"
        if new_role not in ("admin", "user"):
            send_message(chat_id, f"❌ Invalid role: <code>{escape(new_role)}</code>. Use 'admin' or 'user'.")
            return
        if not _users_config_loaded:
            _load_users_config()
        _users_config.setdefault("users", {})[new_uid] = {
            "name": new_name,
            "role": new_role,
            "stores": new_stores,
        }
        try:
            _save_users_config()
            stores_str = ", ".join(new_stores)
            send_message(
                chat_id,
                f"✅ User added: <b>{escape(new_name)}</b> (<code>{new_uid}</code>)\n"
                f"Role: <b>{new_role}</b>\n"
                f"Stores: <code>{escape(stores_str)}</code>",
            )
        except Exception as exc:
            send_message(chat_id, f"❌ Failed to save user config: {exc}")

    elif cmd == "/removeuser":
        if not _is_admin(sender_id):
            send_message(chat_id, "❌ Admin only.")
            return
        if len(parts) < 2:
            send_message(chat_id, "Usage: <code>/removeuser &lt;user_id&gt;</code>")
            return
        rm_uid = parts[1]
        if rm_uid == sender_id:
            send_message(chat_id, "❌ You can't remove yourself.")
            return
        if not _users_config_loaded:
            _load_users_config()
        users = _users_config.get("users", {})
        if rm_uid not in users:
            send_message(chat_id, f"❌ User <code>{escape(rm_uid)}</code> not found.")
            return
        removed = users.pop(rm_uid)
        try:
            _save_users_config()
            send_message(chat_id, f"✅ Removed user <b>{escape(removed.get('name', '?'))}</b> (<code>{rm_uid}</code>)")
        except Exception as exc:
            send_message(chat_id, f"❌ Failed to save: {exc}")

    elif cmd == "/addgroup":
        if not _is_admin(sender_id):
            send_message(chat_id, "❌ Admin only.")
            return
        if len(parts) < 2:
            # If in a group chat, use current chat_id
            if chat_id.startswith("-"):
                group_id = chat_id
            else:
                send_message(chat_id, "Usage: <code>/addgroup &lt;group_chat_id&gt;</code>\nOr send /addgroup in the group itself.")
                return
        else:
            group_id = parts[1]
        if not _users_config_loaded:
            _load_users_config()
        groups = _users_config.setdefault("allowed_groups", [])
        if group_id not in [str(g) for g in groups]:
            groups.append(group_id)
            try:
                _save_users_config()
                send_message(chat_id, f"✅ Group <code>{escape(group_id)}</code> added to allowed list.")
            except Exception as exc:
                send_message(chat_id, f"❌ Failed to save: {exc}")
        else:
            send_message(chat_id, f"Group <code>{escape(group_id)}</code> already allowed.")

    elif cmd == "/removegroup":
        if not _is_admin(sender_id):
            send_message(chat_id, "❌ Admin only.")
            return
        if len(parts) < 2:
            if chat_id.startswith("-"):
                group_id = chat_id
            else:
                send_message(chat_id, "Usage: <code>/removegroup &lt;group_chat_id&gt;</code>")
                return
        else:
            group_id = parts[1]
        if not _users_config_loaded:
            _load_users_config()
        groups = _users_config.get("allowed_groups", [])
        str_groups = [str(g) for g in groups]
        if group_id in str_groups:
            _users_config["allowed_groups"] = [g for g in groups if str(g) != group_id]
            try:
                _save_users_config()
                send_message(chat_id, f"✅ Group <code>{escape(group_id)}</code> removed.")
            except Exception as exc:
                send_message(chat_id, f"❌ Failed to save: {exc}")
        else:
            send_message(chat_id, f"Group <code>{escape(group_id)}</code> not in allowed list.")

    else:
        send_message(chat_id, f"Unknown command: <code>{cmd}</code>\nType /help for commands.")


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Single-update processor (shared by polling loop AND webhook)
# ---------------------------------------------------------------------------

def process_update(update: dict) -> None:
    """Handle one Telegram update dict."""
    global _current_process

    # --- Handle callback queries (approval buttons) ---
    cb = update.get("callback_query")
    if cb:
        cb_from = cb.get("from", {})
        cb_sender_id = str(cb_from.get("id", ""))
        cb_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        if _is_authorized(cb_sender_id, cb_chat_id):
            _handle_approval_callback(cb)
        else:
            answer_callback_query(cb.get("id", ""), "Unauthorized")
        return

    msg = update.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    user = msg.get("from", {})
    sender_id = str(user.get("id", ""))
    username = user.get("username") or user.get("first_name") or "unknown"

    # Extract largest photo (Telegram sends multiple sizes; last = largest)
    photos = msg.get("photo", [])
    photo_file_id = photos[-1]["file_id"] if photos else None
    attachment = {}
    if photo_file_id:
        attachment = {
            "file_id": photo_file_id,
            "file_name": "",
            "mime_type": "image/jpeg",
            "kind": "photo",
        }
    # Also handle document images
    doc = msg.get("document", {})
    if not photo_file_id and doc.get("mime_type", "").startswith("image/"):
        photo_file_id = doc.get("file_id")
        attachment = {
            "file_id": photo_file_id,
            "file_name": doc.get("file_name", ""),
            "mime_type": doc.get("mime_type", ""),
            "kind": "document",
        }
    elif not photo_file_id and doc:
        doc_name = (doc.get("file_name", "") or "").lower()
        doc_mime = doc.get("mime_type", "") or ""
        is_zip = doc_name.endswith(".zip") or doc_mime in (
            "application/zip",
            "application/x-zip-compressed",
            "multipart/x-zip",
        )
        if is_zip:
            photo_file_id = doc.get("file_id")
            attachment = {
                "file_id": photo_file_id,
                "file_name": doc.get("file_name", "designs.zip"),
                "mime_type": doc_mime,
                "kind": "zip",
            }

    if not chat_id:
        return

    # Track all senders
    _seen_chats[sender_id] = username

    # Must have either text/caption or a photo to do anything
    if not text and not attachment.get("file_id"):
        return

    # Security: multi-user auth check
    if not _is_authorized(sender_id, chat_id):
        # If no users configured at all, show setup message
        if not _users_config.get("users"):
            send_message(
                chat_id,
                f"👋 <b>Setup required</b>\n"
                f"Your user ID is: <code>{sender_id}</code>\n\n"
                f"Add to Secret Manager or <code>.env</code>:\n"
                f"<code>TELEGRAM_ADMIN_CHAT_ID={sender_id}</code>\n\n"
                f"Then redeploy and you'll have full bot access.",
            )
        return  # silently ignore unauthorised senders once configured

    # Photo with /design caption (or bare photo → auto-upload)
    if attachment.get("file_id"):
        caption_cmd = (text.split()[0].lower().split("@")[0] if text else "")
        if caption_cmd == "/generate":
            # Route to handle_command so /generate gets the attachment
            handle_command(text, chat_id, attachment=attachment, sender_id=sender_id)
            return
        if caption_cmd in ("/design", "/upload") or not text:
            # Enter /design flow with photo
            design_text = text if text.startswith(("/design", "/upload")) else "/upload"
            parts = design_text.strip().split()
            _handle_design(parts, design_text, chat_id, attachment)
            return

    if text.startswith("/"):
        handle_command(text, chat_id, attachment=attachment, sender_id=sender_id)
    else:
        send_message(chat_id, "Send a command. Type /help for the list.")


# ---------------------------------------------------------------------------
# Flask app — webhook mode (Cloud Run)
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Load user config on module import (covers webhook mode via gunicorn)
try:
    _load_users_config()
    print(f"Users config loaded: {len(_users_config.get('users', {}))} users, {len(_users_config.get('allowed_groups', []))} groups")
except Exception as _e:
    print(f"Users config load deferred: {_e}")


@app.route("/health", methods=["GET"])
def health():
    """Cloud Run health / readiness probe."""
    running = bool(_current_process and _current_process.poll() is None)
    return jsonify({
        "status": "ok",
        "admin_configured": bool(ADMIN_CHAT_ID),
        "pipeline_running": running,
    }), 200


@app.route("/setup", methods=["GET"])
def setup_page():
    """
    Visit this URL in a browser after sending any message to your bot.
    Shows the chat IDs Telegram has delivered so you can set TELEGRAM_ADMIN_CHAT_ID.
    Protected by ?secret=TELEGRAM_WEBHOOK_SECRET when that var is set.
    """
    if WEBHOOK_SECRET:
        if flask_request.args.get("secret", "") != WEBHOOK_SECRET:
            abort(403)
    lines = ["<h3>TipCat Bot — Chat ID Setup</h3>"]
    if _seen_chats:
        lines.append("<b>Chats that have messaged this bot:</b><pre>")
        for cid, uname in _seen_chats.items():
            lines.append(f"  chat_id={cid}  user=@{uname}")
        lines.append("</pre>")
        first_id = next(iter(_seen_chats))
        lines.append(
            f"<p>Add to GCP Secret Manager:<br>"
            f"<code>echo -n '{first_id}' | gcloud secrets versions add "
            f"TELEGRAM_ADMIN_CHAT_ID --data-file=-</code></p>"
        )
    else:
        lines.append(
            "<p>⏳ No messages received yet.<br>"
            "Send any message to your bot in Telegram, then refresh this page.</p>"
        )
    lines.append(f"<p>Current ADMIN_CHAT_ID: <code>{ADMIN_CHAT_ID or 'NOT SET'}</code></p>")
    return "\n".join(lines), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram webhook endpoint — Telegram POSTs updates here."""
    if WEBHOOK_SECRET:
        token = flask_request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != WEBHOOK_SECRET:
            abort(403)
    update = flask_request.get_json(silent=True)
    if update:
        try:
            process_update(update)
        except Exception as exc:
            print(f"[webhook] error: {exc}")
    return "", 200


# ---------------------------------------------------------------------------
# Discovery mode (local, no Flask needed)
# ---------------------------------------------------------------------------

def discover_chat_id_mode() -> None:
    """
    Run bot in discovery mode: print chat_id of every incoming message.
    Use this once to find your numeric Telegram user ID, then set
    TELEGRAM_ADMIN_CHAT_ID in .env and restart normally.
    """
    print("\n🔍 Chat-ID discovery mode — send any message to your bot now.")
    print("   (Ctrl+C to stop)\n")
    offset = 0
    while True:
        try:
            updates = get_updates(offset=offset + 1 if offset else 0)
            for update in updates:
                offset = update["update_id"]
                msg = update.get("message", {})
                chat = msg.get("chat", {})
                user = msg.get("from", {})
                chat_id = chat.get("id", "?")
                username = user.get("username", "") or user.get("first_name", "?")
                text = msg.get("text", "")
                print(f"  chat_id={chat_id}  user=@{username}  text={text!r}")
                print(f"  ✅ Add this to .env:  TELEGRAM_ADMIN_CHAT_ID={chat_id}\n")
                # Echo back so it's visible in Telegram too
                requests.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={"chat_id": chat_id,
                          "text": f"Your chat ID is: <code>{chat_id}</code>\n"
                                  f"Add to .env:\n<code>TELEGRAM_ADMIN_CHAT_ID={chat_id}</code>",
                          "parse_mode": "HTML"},
                    timeout=10,
                )
        except KeyboardInterrupt:
            print("Discovery mode stopped.")
            break
        except Exception as exc:
            print(f"error: {exc}")
            time.sleep(3)


def main() -> None:
    global _last_update_id
    global _active_config
    global _active_store_key
    global _current_run_meta
    global _current_process

    # ── CLI flag: --discover ─────────────────────────────────────────────────
    if "--discover" in sys.argv:
        if not BOT_TOKEN:
            print("ERROR: TELEGRAM_BOT_TOKEN not set — add it to .env first")
            sys.exit(1)
        discover_chat_id_mode()
        return

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        print("  1. Message @BotFather on Telegram → /newbot")
        print("  2. Copy the token into .env:  TELEGRAM_BOT_TOKEN=<token>")
        return
    if not ADMIN_CHAT_ID:
        print("ERROR: TELEGRAM_ADMIN_CHAT_ID not set")
        print("  Run:  python telegram_bot.py --discover")
        print("  Then send any message to your bot — your chat ID will be printed.")
        return

    state = _load_bot_state()
    _last_update_id = int(state.get("last_update_id", 0) or 0)
    if state.get("active_config"):
        _active_config = str(state.get("active_config"))
    if state.get("active_store_key"):
        _active_store_key = str(state.get("active_store_key"))
    if isinstance(state.get("active_product_by_store"), dict):
        _active_product_by_store.update(state.get("active_product_by_store"))

    # Load multi-user config from GCS
    _load_users_config()
    user_count = len(_users_config.get("users", {}))
    group_count = len(_users_config.get("allowed_groups", []))
    print(f"Bot starting — admin chat: {ADMIN_CHAT_ID} — {user_count} users, {group_count} groups")
    send_message(ADMIN_CHAT_ID, "🤖 <b>Tip Cat Pipeline Bot</b> started.\nType /help for commands.")

    while True:
        try:
            updates = get_updates(offset=_last_update_id + 1)
            for update in updates:
                _last_update_id = update["update_id"]
                _persist_bot_state()
                process_update(update)

            # Check if running local subprocess finished + notify
            if _current_process and _current_process.poll() is not None:
                rc = _current_process.returncode
                send_message(
                    ADMIN_CHAT_ID,
                    _build_completion_summary(
                        _current_run_meta.get("config", _get_active_entry().get("config", _active_config)),
                        _current_run_meta.get("args", []),
                        success=(rc == 0),
                        source=_current_run_meta.get("source", "Local pipeline"),
                        error_text=(f"exit code {rc}" if rc != 0 else ""),
                    ),
                )
                _current_process = None
                _current_run_meta = {}

        except KeyboardInterrupt:
            print("Bot stopped.")
            break
        except Exception as exc:
            print(f"[main loop] error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
