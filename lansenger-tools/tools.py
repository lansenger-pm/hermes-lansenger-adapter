"""Tool handlers for lansenger-tools — send messages, files, images, manage messages via Lansenger API.

Lansenger (蓝信) has multiple message types with different capabilities:

  ┌──────────────┬──────────────┬──────────────┬──────────────┬──────────────┐
  │  msgType     │  Markdown    │  @mention    │  Attachments │  Group Chat  │
  ├──────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
  │  text        │  ✗           │  ✓           │  ✓           │  ✓           │
  │  formatText  │  ✓           │  ✓           │  ✗           │  ✓           │
  │  appArticles │  ✗           │  ✗           │  ✗           │  ✓           │
  │  appCard     │  ✗ (div)     │  ✗           │  ✗           │  ✓           │
  │  linkCard    │  ✗           │  ✗           │  ✗           │  ✓           │
  └──────────────┴──────────────┴──────────────┴──────────────┴──────────────┘

  All message types support both private and group chat. The adapter auto-routes
  to the correct endpoint based on the chat_id (private → userIdList, group → groupId).

  NOTE: formatText supports @mention (reminder) on newer Lansenger versions,
  with reminder_all / reminder_user_ids params in lansenger_send_markdown.
  This is a newer API capability — older Lansenger versions silently accept
  the reminder field but do NOT trigger client-side @mention notifications.
  In group chat, recommended to include @姓名 in the text content so people
  know who the reply is for. Private chat supports reminder but it is unnecessary.

  appCard supports div-style formatting (color, font-size, text-align, text-indent).
  appArticles is a multi-article card (图文卡片) with imgUrl/title/url fields.
  linkCard requires: title, description, iconLink, link, fromName, fromIconLink.

This constraint shapes handler implementations:
- send_text:       msgType=text   → plain text + optional file/image/video attachment
- send_markdown:   msgType=formatText → Markdown text, NO attachments (reminder supported by API but not exposed)
- send_file:       msgType=text   → file/image/video only, optional plain-text caption
- send_image_url:  msgType=text   → image from URL, optional plain-text caption
- send_link_card:  msgType=linkCard → link preview card (6 required fields)
- send_app_articles: msgType=appArticles → multi-article card (图文卡片)
- send_app_card:    msgType=appCard → rich card with div-style formatting
- update_dynamic_card: POST /v1/messages/dynamic/update → update appCard status
- query_groups:     GET /v2/groups/fetch → list bot's groups

Design: Uses an ephemeral LansengerAdapter instance per invocation.
Credentials are read from LANSENGER_APP_ID / LANSENGER_APP_SECRET env vars
(not from load_gateway_config), which fixes the "Lansenger not configured"
error that occurred in the old adapter-embedded handlers.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from typing import Any, Optional

import httpx

logger = logging.getLogger("lansenger-tools")

# --- Auto-detect media_type from file extension ---
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}


def _media_type_from_path(file_path: str) -> int:
    """Guess media_type (1=video, 2=image, 3=file) from file extension."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _IMAGE_EXTS:
        return 2
    if ext in _VIDEO_EXTS:
        return 1
    return 3


_ADAPTER_CLASS = None  # lazy cache


def _get_adapter_class():
    """Lazily import LansengerAdapter from the platform plugin.

    Tries multiple import paths for robustness:
    0. hermes_plugins namespace — the actual plugin registration path
    1. Hermes runtime — gateway module registered in sys.modules
    2. Direct import from ~/.hermes/plugins/lansenger-platform/ (expanded by bundle)
    3. Fallback: ~/.hermes/plugins/platforms/lansenger/ (legacy layout)
    """
    global _ADAPTER_CLASS
    if _ADAPTER_CLASS is not None:
        return _ADAPTER_CLASS

    # Path 0: hermes_plugins namespace — the actual plugin registration path
    try:
        from hermes_plugins.lansenger_platform.adapter import LansengerAdapter
        _ADAPTER_CLASS = LansengerAdapter
        logger.debug("Loaded LansengerAdapter from hermes_plugins.lansenger_platform")
        return _ADAPTER_CLASS
    except ImportError:
        pass

    # Path 1: Hermes runtime — gateway module is loaded
    try:
        from gateway.platforms.lansenger.adapter import LansengerAdapter
        _ADAPTER_CLASS = LansengerAdapter
        logger.debug("Loaded LansengerAdapter from gateway.platforms.lansenger")
        return _ADAPTER_CLASS
    except ImportError:
        pass

    # Path 2: Direct import from expanded bundle location
    # The bundle __init__.py copies sub-plugins to <hermes_home>/plugins/<name>/,
    # so lansenger-platform lands at <hermes_home>/plugins/lansenger-platform/
    from pathlib import Path
    _hermes_home = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
    adapter_paths = [
        str(_hermes_home / "plugins" / "lansenger-platform" / "adapter.py"),
        # Legacy fallback: platforms/lansenger subdirectory (pre-bundle layout)
        str(_hermes_home / "plugins" / "platforms" / "lansenger" / "adapter.py"),
    ]

    for path in adapter_paths:
        if not os.path.isfile(path):
            logger.debug("Adapter path not found: %s", path)
            continue
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "lansenger_adapter", path,
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _ADAPTER_CLASS = mod.LansengerAdapter
            logger.debug("Loaded LansengerAdapter from %s", path)
            return _ADAPTER_CLASS
        except Exception as e:
            logger.debug("Failed to load from %s: %s", path, e)
            continue

    logger.error("Cannot load LansengerAdapter from any path")
    raise ImportError(
        "LansengerAdapter not found. "
        "Make sure the lansenger-platform plugin is installed and enabled."
    )


def _check_env() -> dict:
    """Verify Lansenger env vars are available. Returns config dict or error."""
    app_id = os.environ.get("LANSENGER_APP_ID", "").strip()
    app_secret = os.environ.get("LANSENGER_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        return {
            "error": (
                "Lansenger credentials not configured. "
                "Set LANSENGER_APP_ID and LANSENGER_APP_SECRET in .env or config.yaml."
            )
        }
    api_gateway = os.environ.get("LANSENGER_API_GATEWAY_URL", "").strip()
    if not api_gateway:
        return {
            "error": (
                "Lansenger API Gateway URL not configured. "
                "Set LANSENGER_API_GATEWAY_URL in .env or config.yaml."
            )
        }
    return {"app_id": app_id, "app_secret": app_secret, "api_gateway_url": api_gateway}


def _make_config(env_config: dict):
    """Build a config object with an ``.extra`` attribute for LansengerAdapter.

    LansengerAdapter.__init__ accesses ``config.extra``, so we must return
    an object that has that attribute — a plain dict won't work.

    We try to use the real PlatformConfig dataclass from the gateway;
    if that fails (e.g. wrong constructor args, ImportError), we fall back
    to a SimpleNamespace wrapper.

    NOTE: PlatformConfig fields are: enabled, token, api_key, home_channel,
    reply_to_mode, gateway_restart_notification, extra.  It does NOT have a
    ``platform`` field — do not pass one.
    """
    extra_data = {
        "app_id": env_config["app_id"],
        "app_secret": env_config["app_secret"],
        "api_gateway_url": env_config["api_gateway_url"],
    }
    try:
        from gateway.config import PlatformConfig
        return PlatformConfig(enabled=True, extra=extra_data)
    except Exception:
        # Gateway not available or PlatformConfig constructor failed
        # (e.g. TypeError from unknown field) — use namespace
        from types import SimpleNamespace
        return SimpleNamespace(enabled=True, extra=extra_data)


def _run_async(coro):
    """Run an async coroutine from a synchronous context.

    When the gateway event loop is already running, injects the coroutine
    into it via asyncio.run_coroutine_threadsafe instead of spinning a new
    thread+loop (which caused max_workers=1 deadlock and 30s timeout on
    large file uploads).  Falls back to asyncio.run() only when no loop
    exists (standalone CLI / cron mode).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=120)

    return asyncio.run(coro)


# --- Async implementations (shared by all handlers) ---


async def _create_ephemeral_adapter():
    """Create an ephemeral LansengerAdapter with env-based config.

    Pre-loads persisted appToken so the ephemeral adapter can reuse it
    without calling /v1/apptoken/create on every invocation.

    Creates a fresh httpx.AsyncClient per invocation to avoid event-loop
    binding issues across gateway lifecycles.
    """
    LansengerAdapter = _get_adapter_class()
    env_config = _check_env()
    if "error" in env_config:
        raise ValueError(env_config["error"])

    config = _make_config(env_config)
    adapter = LansengerAdapter(config)
    # Always create a fresh http client — the shared one may be bound
    # to a closed event loop from a previous gateway lifecycle.
    adapter._http_client = httpx.AsyncClient(timeout=30.0)

    _load_persisted_token_into_adapter(adapter)
    _load_persisted_chat_types_into_adapter(adapter)

    return adapter


def _load_persisted_token_into_adapter(adapter) -> None:
    """Load persisted token from ~/.hermes/lansenger_token.json into adapter.

    If the persisted token is still valid and matches the current app_id,
    set it on the adapter so _get_app_token() skips the API call.
    If expired, mismatched, or missing, the adapter will fetch a fresh token.
    """
    import json
    from datetime import datetime
    from pathlib import Path

    token_file = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "lansenger_token.json"
    try:
        if not token_file.exists():
            return
        data = json.loads(token_file.read_text(encoding="utf-8"))
        if "app_token" in data and "expires_at" in data:
            # Validate app_id match to prevent cross-bot token reuse
            stored_app_id = data.get("app_id", "")
            current_app_id = getattr(adapter, "_app_id", "")
            if current_app_id and stored_app_id and stored_app_id != current_app_id:
                logger.debug(
                    "lansenger-tools: skipping persisted token — app_id mismatch "
                    "(stored=%s, current=%s)",
                    stored_app_id[:20], current_app_id[:20],
                )
                return
            if datetime.now().timestamp() < data["expires_at"]:
                adapter._app_token = data["app_token"]
                adapter._token_expiry = data["expires_at"]
                logger.debug("lansenger-tools: reused persisted appToken (expires in %ds)",
                             int(data["expires_at"] - datetime.now().timestamp()))
    except Exception as e:
        logger.debug("lansenger-tools: failed to load persisted token: %s", e)


def _load_persisted_chat_types_into_adapter(adapter) -> None:
    """Load persisted chat type map from ~/.hermes/lansenger_chat_types.json.

    The gateway adapter caches chat_id → group/dm from inbound messages and
    persists it. The ephemeral adapter reads it so outbound routing works
    correctly (group chat → groupId endpoint, private → userIdList endpoint).
    """
    import json
    from pathlib import Path

    chat_type_file = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "lansenger_chat_types.json"
    try:
        if not chat_type_file.exists():
            return
        data = json.loads(chat_type_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            adapter._chat_type_map.update(data)
            logger.debug("lansenger-tools: loaded %d chat type mappings", len(data))
    except Exception as e:
        logger.debug("lansenger-tools: failed to load chat type map: %s", e)


async def _send_text_async(chat_id: str, content: str,
                            file_path: str = "", media_type: int = 3,
                            reminder_all: bool = False,
                            reminder_user_ids: list = None,
                            reminder_bot_ids: list = None,
                            ref_msg_id: str = None) -> dict:
    """Async: send plain text message (msgType=text), optionally with attachment, @mentions, and reply quote."""
    adapter = await _create_ephemeral_adapter()
    try:
        # Build reminder dict if any @mention params provided
        reminder = None
        uids = reminder_user_ids or []
        bids = reminder_bot_ids or []
        if reminder_all or uids or bids:
            reminder = {
                "all": reminder_all,
                "userIds": uids,
            }
            if bids:
                reminder["botIds"] = bids

        if file_path and os.path.isfile(file_path):
            # Upload media first, then send text+media
            media_id = await adapter.upload_media_file(file_path, media_type)
            if media_id:
                result = await adapter.send_text_with_media(
                    chat_id, content, media_type, [media_id],
                    reminder=reminder, ref_msg_id=ref_msg_id
                )
            else:
                # Upload failed — fall back to pure text
                logger.warning("[Lansenger] Media upload failed, sending plain text only")
                result = await adapter.send_text(chat_id, content, reminder=reminder, ref_msg_id=ref_msg_id)
        else:
            # Pure text, no attachment
            result = await adapter.send_text(chat_id, content, reminder=reminder, ref_msg_id=ref_msg_id)

        
        return {
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
            "platform": "lansenger",
            "msg_type": "text",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _send_markdown_async(chat_id: str, content: str,
                                reminder_all: bool = False,
                                reminder_user_ids: list = None,
                                reminder_bot_ids: list = None,
                                ref_msg_id: str = None) -> dict:
    """Async: send Markdown-formatted message (msgType=formatText), optionally with @mentions and reply quote."""
    adapter = await _create_ephemeral_adapter()
    try:
        reminder = None
        uids = reminder_user_ids or []
        bids = reminder_bot_ids or []
        if reminder_all or uids or bids:
            reminder = {
                "all": reminder_all,
                "userIds": uids,
            }
            if bids:
                reminder["botIds"] = bids

        result = await adapter.send_format_text(chat_id, content, reminder=reminder, ref_msg_id=ref_msg_id)
        
        return {
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
            "platform": "lansenger",
            "msg_type": "formatText",
        }
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _send_file_async(chat_id: str, file_path: str, caption: str, media_type: int,
                                  width: int = None, height: int = None, duration: int = None) -> dict:
    """Async: send file/image/video only (msgType=text, attachment only)."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.send_file(chat_id, file_path, caption, media_type,
                                         width=width, height=height, duration=duration)
        

        if result.success:
            return {
                "success": True,
                "platform": "lansenger",
                "chat_id": chat_id,
                "file_path": file_path,
                "media_type": media_type,
                "message_id": result.message_id,
            }
        else:
            return {"success": False, "error": result.error}
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _send_image_url_async(chat_id: str, image_url: str, caption: str) -> dict:
    """Async: download image from URL, then send via send_file."""
    import httpx as _httpx

    try:
        async with _httpx.AsyncClient() as client:
            resp = await client.get(image_url, timeout=_httpx.Timeout(10.0, read=60.0), follow_redirects=True)
            resp.raise_for_status()
            image_bytes = resp.content

        ct = resp.headers.get("content-type", "")
        if "png" in ct:
            suffix = ".png"
        elif "gif" in ct:
            suffix = ".gif"
        elif "webp" in ct:
            suffix = ".webp"
        else:
            suffix = ".jpg"

        fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="lansenger_url_image_")
        os.write(fd, image_bytes)
        os.close(fd)
    except Exception as e:
        return {"success": False, "error": f"Failed to download image: {e}"}

    try:
        result = await _send_file_async(chat_id, temp_path, caption, media_type=2)
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return result
    except Exception as e:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return {"success": False, "error": str(e)}


async def _revoke_async(message_ids: list, chat_id: str = "") -> dict:
    """Async: create ephemeral adapter, revoke messages, teardown."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.revoke_message(
            message_ids=message_ids,
            chat_id=chat_id,
        )
        
        return {
            "success": result.success,
            "error": result.error,
            "platform": "lansenger",
            "operation": "revoke",
        }
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _reply_async(message_id: str, emoji_id: str, reply_type: int) -> dict:
    """Async: create ephemeral adapter, emoji-reply a message, teardown."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.reply_message(
            message_id=message_id,
            emoji_id=emoji_id,
            reply_type=reply_type,
        )

        return {
            "success": result.success,
            "error": result.error,
            "platform": "lansenger",
            "operation": "reply",
            "msg_id": message_id,
            "emoji_id": emoji_id,
            "cancelled": reply_type == 2,
        }
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _send_link_card_async(chat_id: str, title: str, link: str,
                                 description: str = "", icon_link: str = "",
                                 pc_link: str = "", from_name: str = "",
                                 from_icon_link: str = "") -> dict:
    """Async: create ephemeral adapter, send linkCard, teardown."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.send_link_card(
            chat_id=chat_id,
            title=title,
            link=link,
            description=description,
            icon_link=icon_link,
            pc_link=pc_link,
            from_name=from_name,
            from_icon_link=from_icon_link,
        )
        
        return {
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
            "platform": "lansenger",
            "operation": "linkCard",
        }
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _send_app_articles_async(chat_id: str, articles: list) -> dict:
    """Async: create ephemeral adapter, send appArticles, teardown."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.send_app_articles(chat_id=chat_id, articles=articles)
        
        return {
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
            "platform": "lansenger",
            "operation": "appArticles",
        }
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _send_app_card_async(
    chat_id: str, head_title: str, body_title: str,
    body_sub_title: str, body_content: str, signature: str,
    fields: list, links: list, card_link: str, pc_card_link: str,
    is_dynamic: bool, head_status_info: dict, staff_id: str, head_icon_url: str) -> dict:
    """Async: create ephemeral adapter, send appCard, teardown."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.send_app_card(
            chat_id=chat_id, head_title=head_title, body_title=body_title,
            body_sub_title=body_sub_title, body_content=body_content,
            signature=signature, fields=fields, links=links,
            card_link=card_link, pc_card_link=pc_card_link,
            is_dynamic=is_dynamic, head_status_info=head_status_info,
            staff_id=staff_id, head_icon_url=head_icon_url,
        )
        
        return {
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
            "platform": "lansenger",
            "operation": "appCard",
        }
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _send_approve_card_async(
    chat_id: str, head_title: str, body_title: str,
    body_content: str, fields: list, buttons: list,
    expire_time: int, head_status: str, head_status_color: str) -> dict:
    """Async: create ephemeral adapter, send approveCard, teardown."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.send_approve_card(
            chat_id=chat_id, head_title=head_title, body_title=body_title,
            body_content=body_content, fields=fields, buttons=buttons,
            expire_time=expire_time, head_status=head_status,
            head_status_color=head_status_color,
        )
        return {
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
            "platform": "lansenger",
            "operation": "approveCard",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _update_dynamic_card_async(
    msg_id: str, head_status_info: dict, links: list,
    is_last_update: bool, chat_id: str = None, card_type: str = "") -> dict:
    """Async: create ephemeral adapter, update dynamic card status, teardown."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.update_dynamic_card_status(
            msg_id=msg_id, head_status_info=head_status_info,
            links=links, is_last_update=is_last_update,
            chat_id=chat_id, card_type=card_type,
        )
        
        return {
            "success": result.success,
            "error": result.error,
            "platform": "lansenger",
            "operation": "dynamic_card_update",
        }
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _query_groups_async(page_offset: int, page_size: int) -> dict:
    """Async: create ephemeral adapter, query groups, teardown."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.query_groups(page_offset=page_offset, page_size=page_size)
        
        return {
            "success": True,
            "total_group_ids": result.get("totalGroupIds", 0),
            "group_ids": result.get("groupIds", []),
            "platform": "lansenger",
            "operation": "query_groups",
        }
    except Exception as e:

        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


# --- Synchronous handlers (called by Hermes tool registry) ---


def lansenger_send_text(args: dict, **kwargs) -> str:
    """Send a plain text message (msgType=text) with optional file/image/video attachment.

    msgType=text supports: plain text, @mentions (group/staff chat only), file/image/video attachments.
    Does NOT support: Markdown formatting. Private chats do not support @mentions.
    """
    chat_id = args.get("chat_id", "").strip()
    content = args.get("content", "").strip()
    file_path = args.get("file_path", "").strip()
    media_type = args.get("media_type")
    reminder_all = args.get("reminder_all", False)
    reminder_user_ids = args.get("reminder_user_ids") or []
    reminder_bot_ids = args.get("reminder_bot_ids") or []
    ref_msg_id = args.get("ref_msg_id") or None

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    if not content and not file_path:
        return json.dumps({"error": "content or file_path is required (need at least one)"})

    # Resolve file path
    if file_path:
        if not os.path.isabs(file_path):
            file_path = os.path.abspath(file_path)
        if not os.path.isfile(file_path):
            return json.dumps({"error": f"File not found: {file_path}"})
        if media_type is None:
            media_type = _media_type_from_path(file_path)

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_send_text_async(chat_id, content, file_path,
                                              media_type or 3, reminder_all, reminder_user_ids,
                                              reminder_bot_ids, ref_msg_id))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


async def _download_media_async(media_id: str) -> dict:
    """Async: download a media file by media_id and save to temp file."""
    import tempfile

    adapter = await _create_ephemeral_adapter()
    try:
        token = await adapter._get_app_token()
        if not token:
            return {"success": False, "error": "Failed to get app token"}

        url = f"{adapter._api_gateway_url}/v1/medias/{media_id}/fetch"
        params = {"app_token": token}
        response = await adapter._http_client.get(url, params=params)
        response.raise_for_status()

        # Extract filename from Content-Disposition
        filename = None
        cd = response.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            import re
            m = re.search(r'filename[^=]*=([^;]+)', cd)
            if m:
                filename = m.group(1).strip().strip('"\'')

        # Save to temp file
        prefix = f"lansenger_download_{media_id[:12]}_"
        suffix = ".dat"
        if filename and '.' in filename and '/' not in filename:
            suffix = os.path.splitext(filename)[1] or ".dat"
            prefix = f"lansenger_{os.path.splitext(filename)[0]}_"

        fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(response.content)
            return {
                "success": True,
                "path": path,
                "size": len(response.content),
                "filename": filename,
                "media_id": media_id,
                "platform": "lansenger",
                "operation": "download_media",
            }
        except Exception as e:
            try:
                os.unlink(path)
            except Exception:
                pass
            return {"success": False, "error": f"Failed to save file: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


def lansenger_download_media(args: dict, **kwargs) -> str:
    """Download a media file from Lansenger by media_id. Returns the local file path."""
    media_id = args.get("media_id", "").strip()
    if not media_id:
        return json.dumps({"error": "media_id is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_download_media_async(media_id))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_send_markdown(args: dict, **kwargs) -> str:
    """Send a Markdown-formatted message (msgType=formatText), optionally with @mentions.

    msgType=formatText supports: Markdown formatting and optional @mentions (reminder).
    @mentions are a newer API capability — if the server doesn't support them, they will
    be silently dropped. Does NOT support file/image/video attachments.
    """
    chat_id = args.get("chat_id", "").strip()
    content = args.get("content", "").strip()
    reminder_all = args.get("reminder_all", False)
    reminder_user_ids = args.get("reminder_user_ids") or []
    reminder_bot_ids = args.get("reminder_bot_ids") or []
    ref_msg_id = args.get("ref_msg_id") or None

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    if not content:
        return json.dumps({"error": "content is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_send_markdown_async(chat_id, content, reminder_all,
                                                  reminder_user_ids, reminder_bot_ids, ref_msg_id))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_send_file(args: dict, **kwargs) -> str:
    """Send a local file/image/video only (msgType=text, no text body)."""
    chat_id = args.get("chat_id", "").strip()
    file_path = args.get("file_path", "").strip()
    caption = args.get("caption", "").strip()
    media_type = args.get("media_type")
    width = args.get("width")
    height = args.get("height")
    duration = args.get("duration")

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    if not file_path:
        return json.dumps({"error": "file_path is required"})

    if not os.path.isabs(file_path):
        file_path = os.path.abspath(file_path)

    if not os.path.isfile(file_path):
        return json.dumps({"error": f"File not found: {file_path}"})

    if media_type is None:
        media_type = _media_type_from_path(file_path)

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_send_file_async(chat_id, file_path, caption, media_type, width, height, duration))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_send_image_url(args: dict, **kwargs) -> str:
    """Send an image from a URL to a Lansenger user/group."""
    chat_id = args.get("chat_id", "").strip()
    image_url = args.get("image_url", "").strip()
    caption = args.get("caption", "").strip()

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    if not image_url:
        return json.dumps({"error": "image_url is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_send_image_url_async(chat_id, image_url, caption))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_revoke_message(args: dict, **kwargs) -> str:
    """Revoke a previously sent Lansenger (蓝信) message.

    Pass chat_id to let the adapter auto-detect group vs private chat.
    """
    message_ids = args.get("message_ids", [])
    chat_id = args.get("chat_id", "").strip()

    if not message_ids:
        return json.dumps({"error": "message_ids is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_revoke_async(message_ids, chat_id))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_reply_message(args: dict, **kwargs) -> str:
    """React to a Lansenger (蓝信) message with an emoji (server v4.6.17+).

    Pass cancel=true to retract the reaction; the same emoji_id is still
    required for cancel (the server rejects an emojiless cancel with 40060).
    """
    message_id = args.get("message_id", "").strip()
    emoji_id = args.get("emoji_id", "").strip()
    cancel = bool(args.get("cancel", False))

    if not message_id:
        return json.dumps({"error": "message_id is required"})
    if not emoji_id:
        return json.dumps({"error": "emoji_id is required (also required when cancel=true)"})

    reply_type = 2 if cancel else 1

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_reply_async(message_id, emoji_id, reply_type))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_send_link_card(args: dict, **kwargs) -> str:
    """Send a linkCard message to a Lansenger (蓝信) user or group."""
    chat_id = args.get("chat_id", "").strip()
    title = args.get("title", "").strip()
    link = args.get("link", "").strip()
    description = args.get("description") or ""
    icon_link = args.get("icon_link") or ""
    pc_link = args.get("pc_link") or ""
    from_name = args.get("from_name") or ""
    from_icon_link = args.get("from_icon_link") or ""

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    if not title:
        return json.dumps({"error": "title is required"})
    if not link:
        return json.dumps({"error": "link is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(
            _send_link_card_async(chat_id, title, link, description,
                                  icon_link, pc_link, from_name, from_icon_link)
        )
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_send_app_articles(args: dict, **kwargs) -> str:
    """Send an appArticles (图文卡片) message with multiple article entries.

    Each article must have: imgUrl, title, url, pcUrl. Optional: summary, attach.
    """
    chat_id = args.get("chat_id", "").strip()
    articles = args.get("articles", [])

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    if not articles:
        return json.dumps({"error": "articles is required (list of dicts with imgUrl/title/url/pcUrl)"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_send_app_articles_async(chat_id, articles))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _fix_div_style_fields(body_title, body_sub_title, body_content):
    """Auto-fix appCard div-style fields per API spec tag support.

    Per Lansenger API spec (4.6.4.7 appCard):
    - bodyTitle, bodySubTitle, bodyContent: support color, font-size, text-align
    - bodyContent: additionally supports text-indent
    - signature: only supports color (no font-size, no text-indent)
    - fields key/value: only support color (no font-size, no text-indent)
    - links.title: only supports color and text-align (no font-size, no text-indent)
    - headStatusInfo.description: only supports color (no font-size, no text-indent)

    Therefore font-size px→pt and text-indent bare-0→0em fixes
    are only applied to bodyTitle, bodySubTitle, bodyContent.
    """
    def _px_to_pt(m):
        px_val = float(m.group(1))
        pt_val = px_val * 0.75
        if pt_val == int(pt_val):
            return f"font-size:{int(pt_val)}pt"
        return f"font-size:{pt_val}pt"

    def _fix_font_size(text):
        if not text:
            return text
        return re.sub(r'font-size:(\d+(?:\.\d+)?)px', _px_to_pt, text)

    def _fix_text_indent(text):
        if not text:
            return text
        return re.sub(r'text-indent:0(?![\d.em])', 'text-indent:0em', text)

    body_title = _fix_font_size(body_title)
    body_sub_title = _fix_font_size(body_sub_title)
    body_content = _fix_font_size(body_content)
    body_content = _fix_text_indent(body_content)

    return body_title, body_sub_title, body_content


def lansenger_send_app_card(args: dict, **kwargs) -> str:
    """Send an appCard (应用卡片) message with rich formatting.

    appCard supports div-style formatting (color, font-size, text-align).
    body_title is required. Set is_dynamic=true for approval workflows.
    """
    chat_id = args.get("chat_id", "").strip()
    body_title = args.get("body_title", "").strip()
    head_title = args.get("head_title", "").strip()
    body_sub_title = args.get("body_sub_title", "").strip()
    body_content = args.get("body_content", "").strip()
    signature = args.get("signature", "").strip()
    fields = args.get("fields") or []
    links = args.get("links") or []
    card_link = args.get("card_link", "").strip()
    pc_card_link = args.get("pc_card_link", "").strip()
    is_dynamic = args.get("is_dynamic", False)
    head_status_info = args.get("head_status_info") or None
    staff_id = args.get("staff_id", "").strip()
    head_icon_url = args.get("head_icon_url", "").strip()

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    if not body_title:
        return json.dumps({"error": "body_title is required for appCard"})

    # Auto-fix div-style: px→pt for font-size in bodyTitle/SubTitle/Content, text-indent in bodyContent
    body_title, body_sub_title, body_content = \
        _fix_div_style_fields(body_title, body_sub_title, body_content)

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_send_app_card_async(
            chat_id, head_title, body_title, body_sub_title, body_content,
            signature, fields, links, card_link, pc_card_link,
            is_dynamic, head_status_info, staff_id, head_icon_url))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_send_approve_card(args: dict, **kwargs) -> str:
    """Send an approveCard (审批卡片) with clickable buttons.

    approveCard uses markdown-formatted body content and supports WebSocket button callbacks.
    Suitable for interactive workflows (approvals, confirmations, choices).
    """
    chat_id = args.get("chat_id", "").strip()
    head_title = args.get("head_title", "").strip()
    body_title = args.get("body_title", "").strip()
    body_content = args.get("body_content", "").strip()
    fields = args.get("fields") or []
    buttons = args.get("buttons") or []
    expire_time = args.get("expire_time", 3600)
    head_status = args.get("head_status", "").strip()
    head_status_color = args.get("head_status_color", "#FFB116").strip()

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    if not head_title:
        return json.dumps({"error": "head_title is required"})
    if not body_title:
        return json.dumps({"error": "body_title is required"})
    if not buttons:
        return json.dumps({"error": "at least one button is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_send_approve_card_async(
            chat_id, head_title, body_title, body_content,
            fields, buttons, expire_time, head_status, head_status_color))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_update_dynamic_card(args: dict, **kwargs) -> str:
    """Update a dynamic card's status in-place (appCard or approveCard).

    The card must have been sent with is_dynamic=true (appCard) or via
    lansenger_send_approve_card. Uses /v1/messages/dynamic/update.
    """
    msg_id = args.get("msg_id", "").strip()
    chat_id = args.get("chat_id") or None
    head_status_info = args.get("head_status_info") or None
    links = args.get("links") or None
    is_last_update = args.get("is_last_update", False)
    card_type = args.get("card_type", "").strip()

    if not msg_id:
        return json.dumps({"error": "msg_id is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_update_dynamic_card_async(msg_id, head_status_info, links,
                                                        is_last_update, chat_id, card_type))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_query_groups(args: dict, **kwargs) -> str:
    """Query the bot's group ID list via GET /v2/groups/fetch.

    Returns totalGroupIds (int) and groupIds (list of str).
    """
    page_offset = args.get("page_offset", 0)
    page_size = args.get("page_size", 100)

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_query_groups_async(page_offset, page_size))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# --- Group info async implementations ---


async def _get_group_info_async(group_id: str) -> dict:
    """Async: get group basic info via GET /v2/groups/{group_id}/info/fetch."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.get_group_info(group_id)
        if "error" in result:
            return {"success": False, "error": result["error"], "platform": "lansenger"}
        return {
            "success": True,
            "name": result.get("name"),
            "description": result.get("description"),
            "total_members": result.get("totalMembers"),
            "max_members": result.get("maxMembers"),
            "state": "正常" if result.get("state") == 0 else "已解散",
            "avatar_url": result.get("avatarUrl"),
            "platform": "lansenger",
            "operation": "get_group_info",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _get_group_members_async(group_id: str, page_offset: int = 0,
                                    page_size: int = 100) -> dict:
    """Async: get group member list via GET /v2/groups/{group_id}/members/fetch."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.get_group_members(group_id, page_offset, page_size)
        if "error" in result:
            return {"success": False, "error": result["error"], "platform": "lansenger"}
        members = result.get("members", [])
        return {
            "success": True,
            "total_members": result.get("totalMembers", 0),
            "members": members,
            "member_count": len(members),
            "platform": "lansenger",
            "operation": "get_group_members",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


async def _check_in_group_async(group_id: str, staff_id: str = "") -> dict:
    """Async: check if staff/bot is in a group via GET /v2/groups/{group_id}/members/is_in_group."""
    adapter = await _create_ephemeral_adapter()
    try:
        result = await adapter.check_in_group(group_id, staff_id)
        if "error" in result:
            return {"success": False, "error": result["error"], "platform": "lansenger"}
        return {
            "success": True,
            "is_in_group": result.get("isInGroup", False),
            "group_id": group_id,
            "platform": "lansenger",
            "operation": "check_in_group",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await adapter._http_client.aclose()


# --- Synchronous handlers ---


def lansenger_get_group_info(args: dict, **kwargs) -> str:
    """Get detailed information about a Lansenger (蓝信) group."""
    group_id = args.get("group_id", "").strip()

    if not group_id:
        return json.dumps({"error": "group_id is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_get_group_info_async(group_id))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_get_group_members(args: dict, **kwargs) -> str:
    """Get the member list of a Lansenger (蓝信) group."""
    group_id = args.get("group_id", "").strip()
    page_offset = args.get("page_offset", 0)
    page_size = args.get("page_size", 100)

    if not group_id:
        return json.dumps({"error": "group_id is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_get_group_members_async(group_id, page_offset, page_size))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def lansenger_check_in_group(args: dict, **kwargs) -> str:
    """Check whether a staff/bot is in a Lansenger (蓝信) group."""
    group_id = args.get("group_id", "").strip()
    staff_id = args.get("staff_id", "").strip()

    if not group_id:
        return json.dumps({"error": "group_id is required"})

    env_result = _check_env()
    if "error" in env_result:
        return json.dumps(env_result)

    try:
        result = _run_async(_check_in_group_async(group_id, staff_id))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})