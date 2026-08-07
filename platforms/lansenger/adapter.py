"""
Lansenger (蓝信) platform adapter — Hermes Agent plugin version.

Uses Lansenger Smart Bot API for real-time message reception via WebSocket.
Responses are sent via Lansenger's HTTP API.

Requires:
    pip install websockets httpx
    LANSENGER_APP_ID and LANSENGER_APP_SECRET env vars

Configuration in config.yaml:
    platforms:
      lansenger:
        enabled: true
        extra:
          app_id: "your-app-id"        # or LANSENGER_APP_ID env var
          app_secret: "your-secret"    # or LANSENGER_APP_SECRET env var
          api_gateway_url: "https://your-api-gateway-url"  # required

This is a PLUGIN adapter — registered via ctx.register_platform() in the
register(ctx) entry point.  No modifications to core Hermes code are needed.
"""

import asyncio
import contextvars
import itertools
import logging
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

# ── Lazy imports from Hermes core ──────────────────────────────────────────
# These live in the main repo; we import at module level because the gateway
# guarantees the package is on sys.path before the plugin is loaded.
from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import BasePlatformAdapter, SendResult
try:
    from gateway.platforms.base import classify_send_error  # Hermes v0.19+
except ImportError:  # pragma: no cover — older Hermes without typed send errors
    def classify_send_error(exc=None, error_text=""):
        return "unknown"

logger = logging.getLogger(__name__)

# ── Mixin imports ─────────────────────────────────────────────────────────
from .ws_lifecycle import WsLifecycleMixin
from .message_handler import MessageHandlerMixin
from .token_manager import TokenManagerMixin
from .media import MediaMixin
from .group_query import GroupQueryMixin
from .cards import CardMixin
from .approval import ApprovalMixin
from .i18n_utils import I18nUtilsMixin

# Constants (shared via _constants.py to avoid circular imports with mixin modules)
from ._constants import (
    API_ENDPOINTS,
    INBOUND_SILENCE_TIMEOUT,
    MAX_MESSAGE_LENGTH,
    RECONNECT_BACKOFF,
    classify_lansenger_error,
)


# check_requirements is defined at the bottom of this file (near register()).


# Per-task sender context for auto @reply and auto quote reply.
# contextvars ensures each asyncio task sees its own copy — no race
# when message B arrives while the Agent is still processing message A.
_current_sender_cache: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "lansenger_sender_cache", default={}
)


class LansengerAdapter(
    BasePlatformAdapter,
    WsLifecycleMixin,
    MessageHandlerMixin,
    TokenManagerMixin,
    MediaMixin,
    GroupQueryMixin,
    CardMixin,
    ApprovalMixin,
    I18nUtilsMixin,
):
    """Lansenger chatbot adapter using WebSocket long-connection."""

    # Markdown code blocks render natively in Lansenger formatText messages.
    supports_code_blocks: bool = True
    # Adapter's send() handles long-message splitting internally.
    splits_long_messages: bool = True

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("lansenger"))

        extra = config.extra or {}
        # Priority: env var > config.yaml extra (matches Hermes convention)
        self._app_id: str = os.getenv("LANSENGER_APP_ID") or extra.get("app_id", "")
        self._app_secret: str = os.getenv("LANSENGER_APP_SECRET") or extra.get("app_secret", "")
        self._api_gateway_url: str = os.getenv("LANSENGER_API_GATEWAY_URL") or extra.get("api_gateway_url", "")
        # Store extra config for commands.py permission lookups
        self._config_extra: dict = extra

        # Home channel from PlatformConfig.home_channel (standard Hermes structure)
        self._home_channel_id: Optional[str] = None
        if config.home_channel:
            self._home_channel_id = config.home_channel.chat_id

        self._ws_client: Optional[Any] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._ws_url: Optional[str] = None
        self._ws_ping_interval: int = 50
        self._last_inbound_time: float = 0.0  # last WS inbound message timestamp

        # Inbound silence timeout — env var override (default: 7200s = ticket TTL)
        _silence_timeout = os.getenv("LANSENGER_INBOUND_SILENCE_TIMEOUT") or extra.get("inbound_silence_timeout", "")
        try:
            self._inbound_silence_timeout: int = int(_silence_timeout) if _silence_timeout else INBOUND_SILENCE_TIMEOUT
        except ValueError:
            self._inbound_silence_timeout = INBOUND_SILENCE_TIMEOUT

        # Message deduplication
        self._dedup = MessageDeduplicator(max_size=1000)

        # Chat type cache: maps chat_id → "group" or "dm"
        # Populated from inbound messages so outbound can route correctly
        self._chat_type_map: Dict[str, str] = {}
        self._chat_type_map_dirty: bool = False
        _hermes_home = self._resolve_hermes_home()
        self._chat_type_file = _hermes_home / "lansenger_chat_types.json"
        self._load_chat_type_map()

        # User language cache: maps chat_id → "zh" or "en"
        # Populated from inbound messages so approval cards use the right language
        self._user_lang_map: Dict[str, str] = {}

        # Token cache
        self._app_token: Optional[str] = None
        self._token_expiry: float = 0
        self._token_file = _hermes_home / "lansenger_token.json"

        # Owner ID (the user who bound the bot)
        self._owner_id: Optional[str] = None
        self._owner_id_file = _hermes_home / "lansenger_owner.json"
        self._load_owner_id()

        # Auto-sethome: first DM becomes home channel if none configured.
        # If an existing home is a group (group:xxx), the first DM overrides it.
        _existing_home = self._home_channel_id or ""
        self._auto_sethome_done: bool = bool(_existing_home) and not _existing_home.startswith("group:")

        # Pairing state
        self._pending_pairings: Dict[str, Dict[str, Any]] = {}

        # Group chat policy — env var > config.yaml extra (Hermes convention)
        _group_policy = os.getenv("LANSENGER_GROUP_POLICY") or extra.get("group_policy", "open")
        self._group_policy: str = _group_policy if _group_policy in ("open", "allowlist", "disabled") else "open"

        _group_allow_from = os.getenv("LANSENGER_GROUP_ALLOW_FROM") or extra.get("group_allow_from", "")
        self._group_allow_senders: List[str] = [g.strip() for g in _group_allow_from.split(",") if g.strip()] if _group_allow_from else []

        _require_mention = os.getenv("LANSENGER_REQUIRE_MENTION") or extra.get("require_mention", "true")
        self._require_mention: bool = str(_require_mention).lower() in ("true", "1", "yes")

        # Per-group overrides (config.yaml extra.groups only, no env var equivalent)
        # Format: {"chat_id": {"enabled": bool, "require_mention": bool, "allow_from": [sender_ids]}}
        _raw_groups = extra.get("groups", {}) or {}
        self._groups_config: Dict[str, Dict[str, Any]] = {}
        for gid, cfg in _raw_groups.items():
            if isinstance(cfg, dict):
                self._groups_config[str(gid)] = cfg

        # Auto @mention reply in groups
        _auto_mention = os.getenv("LANSENGER_AUTO_MENTION_REPLY") or extra.get("auto_mention_reply", "false")
        self._auto_mention_reply: bool = str(_auto_mention).lower() in ("true", "1", "yes")

        # Auto quote reply (refMsgId) — groups and private chats
        _auto_quote = os.getenv("LANSENGER_AUTO_QUOTE_REPLY") or extra.get("auto_quote_reply", "false")
        self._auto_quote_reply: bool = str(_auto_quote).lower() in ("true", "1", "yes")

        # Respond to @all — whether @all messages bypass require_mention (default: false)
        _respond_at_all = os.getenv("LANSENGER_RESPOND_TO_AT_ALL") or extra.get("respond_to_at_all", "false")
        self._respond_to_at_all: bool = str(_respond_at_all).lower() in ("true", "1", "yes")

        # Approval allow list — who can approve dangerous commands
        # Default: owner_id only. Config can add additional approvers.
        _approval_allow = os.getenv("LANSENGER_APPROVAL_ALLOW_FROM") or extra.get("approval_allow_from", "")
        self._approval_allow_from: List[str] = [a.strip() for a in _approval_allow.split(",") if a.strip()] if _approval_allow else []

        # Last sender per chat (for autoMentionReply)
        self._chat_last_sender: Dict[str, str] = {}
        self._chat_last_from_type: Dict[str, str] = {}
        self._chat_last_msg_id: Dict[str, str] = {}

        # Slash command registration state
        self._commands_registered: bool = False

        # Approval state for approveCard button callbacks
        # approval_id (str) → session_key
        self._approval_state: Dict[str, str] = {}
        self._approval_counter = itertools.count(1)

        # Card type tracking for dynamic status updates
        # msg_id → "approveCard" | "appCard"
        self._card_type_map: Dict[str, str] = {}

        # Pending approval card tracking: session_key → (msg_id, trigger_sender_id)
        # Used to update the card when the user replies /approve or /deny
        # trigger_sender_id is extracted from session_key for permission check
        self._pending_approval_msgs: Dict[str, tuple] = {}
        # approval_id → (msg_id, chat_id) — button callback uses approval_id for precise match
        self._approval_card_msgs: Dict[str, tuple] = {}
        # Persistent approval state (survives gateway restarts)
        self._approvals_file = _hermes_home / "lansenger_approvals.json"
        self._load_approvals()

        # Hermes core gateway approval timeout (seconds, default 300)
        self._gateway_approval_timeout = self._read_gateway_approval_timeout()

        # Group info cache: group_id → {"info": dict, "members": list, "fetched_at": float}
        # Cache TTL: 300s (5 min). Members only prefetched if total <= 100.
        self._group_cache: Dict[str, Dict[str, Any]] = {}
        self._group_cache_ttl: float = 300.0
        # Track in-flight group fetches to avoid concurrent duplicate API calls
        self._group_fetch_locks: Dict[str, asyncio.Lock] = {}

    # -- Connection lifecycle (delegated to WsLifecycleMixin) ----------------

    async def connect(self, **kwargs) -> bool:
        """Connect to Lansenger via WebSocket (delegated to mixin)."""
        return await WsLifecycleMixin.connect(self, **kwargs)

    async def disconnect(self, **kwargs) -> None:
        """Disconnect from Lansenger (delegated to mixin)."""
        return await WsLifecycleMixin.disconnect(self, **kwargs)

    # -- Persistence helpers -------------------------------------------------

    @staticmethod
    def _resolve_hermes_home() -> Path:
        """Resolve Hermes home directory, respecting HERMES_HOME env var.

        Uses the HERMES_HOME environment variable (set by profiles system)
        so that different profiles get independent token/chat_type/owner files.
        Falls back to ~/.hermes when HERMES_HOME is not set.
        """
        env_home = os.environ.get("HERMES_HOME", "").strip()
        if env_home:
            return Path(env_home)
        return Path.home() / ".hermes"

    def _load_owner_id(self) -> None:
        """Load owner ID from file."""
        try:
            if self._owner_id_file.exists():
                data = json.loads(self._owner_id_file.read_text(encoding="utf-8"))
                self._owner_id = data.get("owner_id")
                if self._owner_id:
                    logger.info("[Lansenger] Loaded owner ID: %s", self._owner_id[:20] if self._owner_id else None)
        except Exception as e:
            logger.warning("[Lansenger] Failed to load owner ID: %s", e)

    def _save_owner_id(self) -> None:
        """Save owner ID to file."""
        try:
            self._owner_id_file.parent.mkdir(parents=True, exist_ok=True)
            self._owner_id_file.write_text(json.dumps({"owner_id": self._owner_id}, indent=2), encoding="utf-8")
            logger.info("[Lansenger] Saved owner ID: %s", self._owner_id[:20] if self._owner_id else None)
        except Exception as e:
            logger.error("[Lansenger] Failed to save owner ID: %s", e)

    def _load_approvals(self) -> None:
        """Load persisted approval state from disk.  Cleans expired entries."""
        try:
            if not self._approvals_file.exists():
                return
            data = json.loads(self._approvals_file.read_text(encoding="utf-8"))
            cards = data.get("cards", {})
            now = time.time()
            loaded = 0
            max_counter = 0
            for approval_id_str, info in cards.items():
                expires_at = info.get("expires_at", 0)
                if expires_at > 0 and expires_at < now:
                    logger.debug("[Lansenger] Purging expired approval card: id=%s", approval_id_str)
                    continue
                msg_id = info.get("msg_id", "")
                chat_id = info.get("chat_id", "")
                session_key = info.get("session_key", "")
                trigger_sender_id = info.get("trigger_sender_id")
                card_type = info.get("card_type", "approveCard")

                if not msg_id or not chat_id:
                    continue

                self._approval_state[approval_id_str] = session_key
                self._card_type_map[msg_id] = card_type
                self._approval_card_msgs[approval_id_str] = (msg_id, chat_id)
                if trigger_sender_id and session_key:
                    self._pending_approval_msgs[session_key] = (msg_id, trigger_sender_id)

                # Track max approval_id for counter recovery
                try:
                    aid = int(approval_id_str)
                    if aid > max_counter:
                        max_counter = aid
                except ValueError:
                    pass
                loaded += 1

            # Restore counter so next approval_id continues from max+1
            self._approval_counter = itertools.count(max_counter + 1)

            logger.info("[Lansenger] Loaded %d pending approvals from %s (counter=%d)",
                        loaded, self._approvals_file, max_counter + 1)
        except Exception as e:
            logger.warning("[Lansenger] Failed to load approvals: %s", e)

    def _save_approvals(self) -> None:
        """Persist approval state to disk."""
        try:
            cards: Dict[str, dict] = {}
            for approval_id, (msg_id, chat_id) in self._approval_card_msgs.items():
                session_key = self._approval_state.get(approval_id, "")
                trigger_sender_id = None
                if session_key and session_key in self._pending_approval_msgs:
                    trigger_sender_id = self._pending_approval_msgs[session_key][1]

                cards[approval_id] = {
                    "msg_id": msg_id,
                    "chat_id": chat_id,
                    "session_key": session_key,
                    "trigger_sender_id": trigger_sender_id,
                    "card_type": self._card_type_map.get(msg_id, "approveCard"),
                    "expires_at": time.time() + self._gateway_approval_timeout + 60,
                }

            data = {"version": 1, "cards": cards}
            self._approvals_file.parent.mkdir(parents=True, exist_ok=True)
            self._approvals_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.debug("[Lansenger] Persisted %d approvals to %s", len(cards), self._approvals_file)
        except Exception as e:
            logger.debug("[Lansenger] Failed to persist approvals: %s", e)

    @staticmethod
    def _read_gateway_approval_timeout() -> int:
        """Read Hermes core's approval gateway_timeout from config.  Default 300s."""
        try:
            from hermes_cli.config import load_config
            config = load_config()
            timeout = config.get("approvals", {}).get("gateway_timeout", 300)
            return int(timeout)
        except Exception:
            return 300

    def _load_chat_type_map(self) -> None:
        try:
            if self._chat_type_file.exists():
                data = json.loads(self._chat_type_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._chat_type_map.update(data)
                    logger.info("[Lansenger] Loaded %d chat type mappings from file", len(data))
        except Exception as e:
            logger.warning("[Lansenger] Failed to load chat type map: %s", e)

    def _persist_chat_type_map(self) -> None:
        if not self._chat_type_map_dirty:
            return
        self._chat_type_map_dirty = False
        try:
            self._chat_type_file.parent.mkdir(parents=True, exist_ok=True)
            self._chat_type_file.write_text(json.dumps(self._chat_type_map, indent=2), encoding="utf-8")
            logger.debug("[Lansenger] Persisted %d chat type mappings", len(self._chat_type_map))
        except Exception as e:
            logger.error("[Lansenger] Failed to persist chat type map: %s", e)

    # -- Tool event formatting (formatText / Markdown) ----------------------

    def format_tool_event(self, event: Any, *, mode: str = "all",
                          preview_max_len: int = 40) -> Optional[str]:
        """Return Markdown-formatted tool chrome for Lansenger formatText.

        Overrides the base class plain-text output to leverage Lansenger's
        Markdown renderer (bold tool names, code blocks for args, bullet
        lists for results).
        """
        from gateway.stream_events import ToolCallChunk
        if not isinstance(event, ToolCallChunk):
            return None

        from agent.display import get_tool_emoji
        emoji = get_tool_emoji(event.tool_name, default="⚙️")

        if mode == "verbose":
            if event.args:
                arg_lines = []
                for k, v in event.args.items():
                    val_str = str(v)
                    if preview_max_len > 0 and len(val_str) > preview_max_len:
                        val_str = val_str[:preview_max_len - 3] + "..."
                    arg_lines.append(f"**{k}**：{val_str}")
                header = f"{emoji} **{event.tool_name}** `{list(event.args.keys())}`"
                return header + "\n" + "\n".join(arg_lines)
            if event.preview:
                preview = event.preview
                if preview_max_len > 0 and len(preview) > preview_max_len:
                    preview = preview[:preview_max_len - 3] + "..."
                return f"{emoji} **{event.tool_name}**：{preview}"
            return f"{emoji} **{event.tool_name}** ..."

        preview = event.preview
        if preview:
            cap = preview_max_len if preview_max_len > 0 else 40
            if len(preview) > cap:
                preview = preview[:cap - 3] + "..."
            return f"{emoji} **{event.tool_name}**：{preview}"
        return f"{emoji} **{event.tool_name}** ..."

    def _is_group_chat(self, chat_id: str) -> bool:
        """Check if chat_id is a group chat.

        Routes by priority:
        1. chat_id == owner_id → DM (fast path for personal bot's owner)
        2. chat_type_map lookup → definitive type from inbound events
        3. Fallback → assume group chat
        """
        # 1. Fast path: owner's DM
        if self._owner_id and chat_id == self._owner_id:
            return False
        # 2. Query chat type map (populated from inbound WS events)
        chat_type = self._chat_type_map.get(chat_id)
        if chat_type == "dm":
            return False
        if chat_type == "group":
            return True
        # 3. Fallback: assume group chat
        return True

    # -- Outbound message sending -------------------------------------------

    async def send(self, chat_id: str, content: str, **kwargs) -> SendResult:
        """Send a message (alias for send_format_text).

        Auto-populates reminder with the last sender's ID when
        auto_mention_reply is enabled and this is a group chat.
        Uses userIds for users, botIds for bots based on fromType (0=user, 1=app).
        Explicit reminder kwarg always wins.

        Auto-populates refMsgId when auto_quote_reply is enabled.
        Supports both group and private chats.
        """
        reminder = kwargs.get("reminder")
        if not reminder and self._is_group_chat(chat_id):
            # Check auto_mention_reply: per-group > global
            per_group = self._groups_config.get(chat_id, {})
            auto_mention = per_group.get("auto_mention_reply", self._auto_mention_reply)
            if isinstance(auto_mention, str):
                auto_mention = str(auto_mention).lower() in ("true", "1", "yes")
            if auto_mention:
                # Use contextvar (per-task) to avoid race when concurrent messages
                # update the global _chat_last_sender dict.
                sender_cache = _current_sender_cache.get()
                sender_info = sender_cache.get(chat_id)
                if sender_info:
                    last_sender = sender_info["sender_id"]
                    from_type = sender_info["from_type"]
                    if str(from_type) == "1":
                        reminder = {"botIds": [last_sender]}
                    else:
                        reminder = {"userIds": [last_sender]}

        # Auto quote reply: per-group > global (groups), global only (private)
        ref_msg_id = None
        if self._is_group_chat(chat_id):
            per_group = self._groups_config.get(chat_id, {})
            auto_quote = per_group.get("auto_quote_reply", self._auto_quote_reply)
        else:
            auto_quote = self._auto_quote_reply
        if isinstance(auto_quote, str):
            auto_quote = str(auto_quote).lower() in ("true", "1", "yes")
        if auto_quote:
            sender_cache = _current_sender_cache.get()
            sender_info = sender_cache.get(chat_id)
            if sender_info:
                ref_msg_id = sender_info.get("msg_id")

        return await self.send_format_text(chat_id, content, reminder=reminder, ref_msg_id=ref_msg_id)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Send typing indicator (not supported by Lansenger)."""
        pass  # Lansenger doesn't support typing indicators

    async def send_text(self, chat_id: str, content: str, reminder: dict = None, ref_msg_id: str = None) -> SendResult:
        """Send a plain text message, optionally with @mentions and reply quote.
        
        Routes to /v1/messages/group/create for group chats,
        /v1/bot/messages/create for private chats.
        
        Args:
            chat_id: Recipient user ID or chat ID
            content: Text content. In group chat, recommended to include @姓名
                     when replying to someone (e.g. "@张三 请查收").
            reminder: Optional dict with 'all' (bool), 'userIds' (list), 'botIds' (list) for @mentions.
                      Private chat supports this but it is unnecessary (only one participant).
            ref_msg_id: Optional msgId to quote/reply to.
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token", error_kind="unknown")

        try:
            is_group = self._is_group_chat(chat_id)

            if is_group:
                # Group message: use /v1/messages/group/create
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
                text_data = {"content": content}
                if reminder:
                    text_data["reminder"] = reminder
                payload = {
                    "groupId": chat_id,
                    "msgType": "text",
                    "msgData": {"text": text_data},
                }
                if ref_msg_id:
                    payload["refMsgId"] = ref_msg_id
            else:
                # Private message: use /v1/bot/messages/create
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
                text_data = {"content": content}
                if reminder:
                    text_data["reminder"] = reminder
                payload = {
                    "userIdList": [chat_id],
                    "msgType": "text",
                    "msgData": {"text": text_data},
                }
                if ref_msg_id:
                    payload["refMsgId"] = ref_msg_id

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                _err = data.get("errMsg")
                return SendResult(success=False, error=_err, error_kind=classify_lansenger_error(_err or ""))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] Text message sent to %s (group=%s)", chat_id, is_group)
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except Exception as e:
            logger.error("[Lansenger] Send text error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True, error_kind=classify_lansenger_error(str(e), e))

    async def send_format_text(self, chat_id: str, content: str, reminder: dict = None, ref_msg_id: str = None) -> SendResult:
            """Send a formatted text message (Markdown support), optionally with @mentions and reply ref.

            Routes to /v1/messages/group/create for group chats,
            /v1/bot/messages/create for private chats.

            Args:
                chat_id: Recipient user ID or chat ID
                content: Markdown-formatted text content.
                reminder: Optional dict with 'all' (bool), 'userIds' (list), and
                          'botIds' (list) for @mentions.
                ref_msg_id: Optional msgId to quote/reply to.
            """
            token = await self._get_app_token()
            if not token:
                return SendResult(success=False, error="No access token", error_kind="unknown")

            try:
                is_group = self._is_group_chat(chat_id)

                format_text_data: Dict[str, Any] = {
                    "formatType": 1,
                    "text": content,
                }
                if reminder:
                    format_text_data["reminder"] = reminder

                if is_group:
                    url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
                    payload = {
                        "groupId": chat_id,
                        "msgType": "formatText",
                        "msgData": {"formatText": format_text_data},
                    }
                    if ref_msg_id:
                        payload["refMsgId"] = ref_msg_id
                else:
                    url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
                    payload = {
                        "userIdList": [chat_id],
                        "msgType": "formatText",
                        "msgData": {"formatText": format_text_data},
                    }
                    if ref_msg_id:
                        payload["refMsgId"] = ref_msg_id

                response = await self._http_client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()

                if data.get("errCode") != 0:
                    _err = data.get("errMsg")
                    return SendResult(success=False, error=_err, error_kind=classify_lansenger_error(_err or ""))

                msg_id = data.get("data", {}).get("msgId")
                logger.info("[Lansenger] FormatText sent to %s (group=%s, reminder=%s)",
                            chat_id, is_group, bool(reminder))
                return SendResult(success=True, message_id=msg_id, raw_response=data)
            except Exception as e:
                logger.error("[Lansenger] Send formatText error: %s", e, exc_info=True)
                return SendResult(success=False, error=str(e), retryable=True, error_kind=classify_lansenger_error(str(e), e))

    # -- Helper methods -----------------------------------------------------

    def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get chat info (stub - Lansenger API doesn't provide this)."""
        return {"name": chat_id, "type": "unknown", "chat_id": chat_id}


def check_requirements() -> bool:
    """Check if Lansenger dependencies are available."""
    if not WEBSOCKETS_AVAILABLE:
        logger.warning("[Lansenger] websockets not installed. Run: pip install websockets")
        return False
    if not HTTPX_AVAILABLE:
        logger.warning("[Lansenger] httpx not installed. Run: pip install httpx")
        return False
    return True


def validate_config(config) -> bool:
    """Check if Lansenger is properly configured (env vars or config.yaml extra)."""
    extra = getattr(config, "extra", None) or {}
    # Priority: env var > config.yaml extra (matches Hermes convention)
    app_id = os.getenv("LANSENGER_APP_ID") or extra.get("app_id", "")
    app_secret = os.getenv("LANSENGER_APP_SECRET") or extra.get("app_secret", "")
    return bool(app_id and app_secret)


def is_connected(config) -> bool:
    """Check if Lansenger appears to be connected/enabled."""
    return bool(config and getattr(config, "enabled", False))


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars (for env-only setups).

    Called during _apply_env_overrides BEFORE the adapter is constructed,
    so ``hermes gateway status`` can reflect env-only configuration.
    """
    app_id = os.getenv("LANSENGER_APP_ID")
    app_secret = os.getenv("LANSENGER_APP_SECRET")
    if not app_id or not app_secret:
        return None

    extra = {
        "app_id": app_id,
        "app_secret": app_secret,
    }
    api_url = os.getenv("LANSENGER_API_GATEWAY_URL")
    if api_url:
        extra["api_gateway_url"] = api_url

    home_channel = os.getenv("LANSENGER_HOME_CHANNEL")
    if home_channel:
        return {"extra": extra, "home_channel": {"chat_id": home_channel}}
    return {"extra": extra}


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None,
                           media_files=None, force_document=False) -> dict:
    """Out-of-process delivery for cron jobs when the gateway is not running.

    Creates an ephemeral adapter, sends the message, and tears down.
    """
    if not check_requirements() or not validate_config(pconfig):
        return {"error": "Lansenger dependencies or config not available"}

    _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}

    media_files = media_files or []

    try:
        adapter = LansengerAdapter(pconfig)
        # Connect just enough to send (get token + HTTP client)
        adapter._http_client = httpx.AsyncClient(timeout=30.0)

        last_result = None
        # Send caption text as formatText (Markdown)
        if message.strip():
            last_result = await adapter.send(chat_id=chat_id, content=message)
            if not last_result.success:
                await adapter._http_client.aclose()
                return {"error": f"Lansenger send failed: {last_result.error}"}

        # Send each media file
        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                await adapter._http_client.aclose()
                return {"error": f"Media file not found: {media_path}"}

            ext = os.path.splitext(media_path)[1].lower()
            if ext in _IMAGE_EXTS:
                last_result = await adapter.send_file(chat_id, media_path, caption="", media_type=2)
            elif ext in _VIDEO_EXTS:
                last_result = await adapter.send_file(chat_id, media_path, caption="", media_type=1)
            else:
                last_result = await adapter.send_file(chat_id, media_path, caption="", media_type=3)

            if not last_result.success:
                await adapter._http_client.aclose()
                return {"error": f"Lansenger media send failed: {last_result.error}"}

        await adapter._http_client.aclose()

        if last_result is None:
            return {"error": "No deliverable text or media"}

        return {
            "success": True,
            "platform": "lansenger",
            "chat_id": chat_id,
            "message_id": last_result.message_id,
        }
    except Exception as e:
        return {"error": f"Lansenger standalone send failed: {e}"}


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="lansenger",
        label="Lansenger (蓝信)",
        adapter_factory=lambda cfg: LansengerAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["LANSENGER_APP_ID", "LANSENGER_APP_SECRET"],
        install_hint="pip install websockets httpx",
        setup_fn=_interactive_setup,
        # Env-driven auto-configuration
        env_enablement_fn=_env_enablement,
        # Cron home-channel delivery support
        cron_deliver_env_var="LANSENGER_HOME_CHANNEL",
        # Out-of-process cron delivery
        standalone_sender_fn=_standalone_send,
        # Auth env vars
        allowed_users_env="LANSENGER_ALLOWED_USERS",
        allow_all_env="LANSENGER_ALLOW_ALL_USERS",
        # Message limit
        max_message_length=4000,
        # Display
        emoji="💠",
        # Lansenger uses opaque user IDs, not phone numbers
        pii_safe=True,
        allow_update_command=True,
        # LLM guidance
        platform_hint=(
            "You are chatting via Lansenger (蓝信), an enterprise messaging platform. "
            "You can send Markdown-formatted text using the "
            "formatText msgType, and send files/images/videos via send_file().  "
            "Messages have a ~4000 character limit.  Dynamic appCard is used for "
            "approval workflows (status updates in-place).  Keep responses concise and professional."
        ),
    )

    # Register hooks for monitoring and observability
    _register_lansenger_hooks(ctx)


def _register_lansenger_hooks(ctx):
    """Register plugin hooks for Lansenger adapter monitoring.
    
    Hook logging can be controlled via:
        1. Environment variable: LANSENGER_HOOK_LOGGING=true/false
        2. config.yaml: platforms.lansenger.extra.hook_logging: true/false
    
    Priority: env var > config > default (true)
    
    Hermes core invoke_hook kwargs per event:
        pre_tool_call:   tool_name, args, task_id, session_id, tool_call_id,
                         turn_id, api_request_id, middleware_trace
        post_tool_call:  tool_name, args, result, task_id, session_id,
                         tool_call_id, turn_id, api_request_id, duration_ms,
                         status, error_type, error_message
        pre_llm_call:    session_id, task_id, turn_id, user_message,
                         conversation_history, is_first_turn, model, platform,
                         sender_id
        pre_gateway_dispatch: event(MessageEvent), gateway(GatewayRunner),
                                session_store

    Note: on_session_start / on_session_end are context engine methods,
    NOT triggered via invoke_hook — do not register them.
    """
    # Check if hook logging is enabled
    # Priority: env var > config > default (true)
    hook_logging_env = os.getenv("LANSENGER_HOOK_LOGGING", "").lower()
    if hook_logging_env:
        hook_logging_enabled = hook_logging_env in ("true", "1", "yes")
    else:
        # Try to read from config.yaml: platforms.lansenger.extra.hook_logging
        try:
            from hermes_constants import get_hermes_home
            import yaml
            config_path = get_hermes_home() / "config.yaml"
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                hook_logging_cfg = cfg.get("platforms", {}).get("lansenger", {}).get("extra", {}).get("hook_logging")
                if hook_logging_cfg is not None:
                    hook_logging_enabled = bool(hook_logging_cfg)
                else:
                    hook_logging_enabled = True  # default
            else:
                hook_logging_enabled = True  # default
        except Exception:
            hook_logging_enabled = True  # default on error

    def _on_pre_llm_call(**kwargs):
        """Log LLM call initiation (fires per turn, useful as session-start indicator)."""
        if not hook_logging_enabled:
            return
        platform = kwargs.get("platform")
        if platform != "lansenger":
            return
        session_id = str(kwargs.get("session_id", ""))[:16]
        sender_id = kwargs.get("sender_id", "")
        model = kwargs.get("model", "")
        is_first_turn = kwargs.get("is_first_turn", False)
        logger.info(
            "[Lansenger Hook] LLM call: platform=%s, session=%s, sender=%s, model=%s, first_turn=%s",
            platform, session_id, sender_id, model, is_first_turn
        )

    def _on_pre_tool_call(**kwargs):
        """Log tool calls before execution for Lansenger sessions."""
        if not hook_logging_enabled:
            return
        platform = kwargs.get("platform")
        if platform != "lansenger":
            return
        tool_name = kwargs.get("tool_name")
        session_id = str(kwargs.get("session_id", ""))[:16]
        logger.info(
            "[Lansenger Hook] Pre tool call: platform=%s, tool=%s, session=%s",
            platform, tool_name, session_id
        )

    def _on_post_tool_call(**kwargs):
        """Log tool execution results for Lansenger sessions."""
        if not hook_logging_enabled:
            return
        platform = kwargs.get("platform")
        if platform != "lansenger":
            return
        tool_name = kwargs.get("tool_name")
        status = kwargs.get("status")
        session_id = str(kwargs.get("session_id", ""))[:16]
        logger.info(
            "[Lansenger Hook] Post tool call: platform=%s, tool=%s, session=%s, status=%s",
            platform, tool_name, session_id, status
        )

    def _on_pre_gateway_dispatch(**kwargs):
        """Log messages before dispatch to Lansenger gateway."""
        if not hook_logging_enabled:
            return
        event = kwargs.get("event")
        if event is None:
            return
        platform = getattr(event, "platform", None)
        if platform != "lansenger":
            return
        chat_id = getattr(event, "chat_id", None)
        message_type = getattr(event, "message_type", "text")
        logger.info(
            "[Lansenger Hook] Pre gateway dispatch: platform=%s, chat_id=%s, type=%s",
            platform, chat_id, message_type
        )

    # Register hooks: callback(event_name, callable)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)


def _interactive_setup():
    """Interactive setup wizard for Lansenger credentials.
    
    Called by `hermes setup gateway` when the user selects Lansenger.
    Prompts for APP_ID, APP_SECRET, and optional API_GATEWAY_URL,
    then writes them to ~/.hermes/.env (idempotent — won't duplicate).
    """
    from pathlib import Path
    
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    env_file = hermes_home / ".env"
    
    # ANSI colors for terminal output
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    
    print()
    print(f"  {CYAN}─── 💠 Lansenger (蓝信) Setup ───{RESET}")
    print()
    print(f"  {YELLOW}Where to find your credentials:{RESET}")
    print(f"  Lansenger desktop → Contacts → Smart Bot → Personal Bot → ℹ️ icon")
    print(f"  (Mobile client does not support viewing credentials)")
    print()
    
    # Read existing .env
    existing_lines = []
    existing_values = {}
    if env_file.exists():
        with open(env_file, encoding="utf-8") as f:
            existing_lines = f.readlines()
        for line in existing_lines:
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                existing_values[key.strip()] = value.strip()
    
    def _prompt_field(env_key: str, label: str, default: str = "", sensitive: bool = False) -> Optional[str]:
        """Prompt for a single env var. Returns new value or None if unchanged."""
        current = existing_values.get(env_key, "")
        if current:
            # Mask sensitive values for display
            display = current if not sensitive else (current[:4] + "****" if len(current) > 4 else "****")
            print(f"  {DIM}Current {label}: {display}{RESET}")
            print(f"  {BOLD}New {label}{RESET} [press Enter to keep current]: ", end="", flush=True)
            new_value = input().strip()
            if not new_value:
                print(f"  {GREEN}✓ Keeping current value{RESET}")
                return None  # unchanged
            return new_value
        else:
            print(f"  {BOLD}{label}:{RESET} ", end="", flush=True)
            new_value = input().strip()
            if not new_value:
                if default:
                    return default
                print(f"  {YELLOW}Skipped — you can set it later in ~/.hermes/.env{RESET}")
                return None
            return new_value
    
    # Prompt for credentials
    app_id = _prompt_field("LANSENGER_APP_ID", "App ID")
    app_secret = _prompt_field("LANSENGER_APP_SECRET", "App Secret", sensitive=True)
    gateway_url = _prompt_field("LANSENGER_API_GATEWAY_URL", "API Gateway URL")
    
    # Build updated .env content — replace existing keys or append new ones
    changes = {}
    if app_id is not None:
        changes["LANSENGER_APP_ID"] = app_id
    if app_secret is not None:
        changes["LANSENGER_APP_SECRET"] = app_secret
    if gateway_url is not None:
        changes["LANSENGER_API_GATEWAY_URL"] = gateway_url
    
    if changes:
        # Rewrite .env: replace changed keys, keep others intact
        output_lines = []
        keys_replaced = set()
        for line in existing_lines:
            if "=" in line and not line.startswith("#"):
                key = line.split("=")[0].strip()
                if key in changes:
                    output_lines.append(f"{key}={changes[key]}\n")
                    keys_replaced.add(key)
                else:
                    output_lines.append(line)
            else:
                output_lines.append(line)
        
        # Append new keys that weren't in the file before
        for key, value in changes.items():
            if key not in keys_replaced:
                output_lines.append(f"{key}={value}\n")
        
        with open(env_file, "w", encoding="utf-8") as f:
            f.writelines(output_lines)
        
        print()
        print(f"  {GREEN}✓ Credentials saved to ~/.hermes/.env{RESET}")
        print(f"  {GREEN}✓ Run 'hermes gateway restart' to activate{RESET}")
    else:
        print()
        print(f"  {YELLOW}No changes to save.{RESET}")
    
    print()
