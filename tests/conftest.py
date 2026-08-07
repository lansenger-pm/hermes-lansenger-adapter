import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent / "platforms"))


class _StubPlatform:
    value = "lansenger"
    def __init__(self, value="lansenger"):
        self.value = value
    @classmethod
    def _missing_(cls, value):
        return cls(value)


class _StubHomeChannel:
    def __init__(self, chat_id=None, name="test"):
        self.platform = _StubPlatform()
        self.chat_id = chat_id
        self.name = name
        self.thread_id = None


class _StubPlatformConfig:
    def __init__(self, enabled=True, extra=None, home_channel=None):
        self.enabled = enabled
        self.extra = extra or {}
        self.home_channel = home_channel
        self.reply_to_mode = "first"
        self.gateway_restart_notification = True


class _StubSendResult:
    def __init__(self, success=True, message_id=None, error=None, raw_response=None,
                 retryable=False, retry_after=None, continuation_message_ids=(),
                 error_kind=None, **kwargs):
        self.success = success
        self.message_id = message_id
        self.error = error
        self.raw_response = raw_response
        self.retryable = retryable
        self.retry_after = retry_after
        self.continuation_message_ids = continuation_message_ids
        self.error_kind = error_kind


class _StubMessageType:
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"


class _StubMessageEvent:
    def __init__(self, text="", message_type=None, source=None, message_id=None, raw_message=None, timestamp=None, media_urls=None, media_types=None, reply_to_message_id=None):
        self.text = text
        self.message_type = message_type or _StubMessageType.TEXT
        self.source = source
        self.message_id = message_id
        self.raw_message = raw_message
        self.timestamp = timestamp
        self.media_urls = media_urls or []
        self.media_types = media_types or []
        self.reply_to_message_id = reply_to_message_id


class _StubMessageDeduplicator:
    def __init__(self, max_size=1000):
        self._seen = {}
        self._max_size = max_size
    def is_duplicate(self, msg_id):
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = True
        if len(self._seen) > self._max_size:
            self._seen.clear()
        return False
    def clear(self):
        self._seen.clear()


class _StubSessionSource:
    def __init__(self, platform="lansenger", chat_id="", chat_name="", chat_type="private", user_id="", user_name=""):
        self.platform = platform
        self.chat_id = chat_id
        self.chat_name = chat_name
        self.chat_type = chat_type
        self.user_id = user_id
        self.user_name = user_name


class _StubBasePlatformAdapter:
    MAX_MESSAGE_LENGTH = 4000
    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self._running = False
        self._connected = False
        self._message_handler = None
    def set_message_handler(self, handler):
        self._message_handler = handler
    def _mark_connected(self):
        self._connected = True
    def _mark_disconnected(self):
        self._connected = False
    def _write_runtime_status_safe(self, status, platform_state=None, **kwargs):
        pass
    def build_source(self, chat_id, chat_name, chat_type, user_id, user_name, **kw):
        return _StubSessionSource(platform="lansenger", chat_id=chat_id, chat_name=chat_name, chat_type=chat_type, user_id=user_id, user_name=user_name)
    async def handle_message(self, event):
        if self._message_handler:
            await self._message_handler(event)


def _stub_cache_image_from_bytes(*a, **kw):
    return "/tmp/stub_image.jpg"

def _stub_cache_document_from_bytes(*a, **kw):
    return "/tmp/stub_document.pdf"


gw_mod = type(sys)("gateway")
gw_mod.Platform = _StubPlatform
gw_mod.PlatformConfig = _StubPlatformConfig
gw_mod.HomeChannel = _StubHomeChannel
sys.modules["gateway"] = gw_mod
sys.modules["gateway.config"] = gw_mod

gw_plat = type(sys)("gateway.platforms")
sys.modules["gateway.platforms"] = gw_plat

gw_helpers = type(sys)("gateway.platforms.helpers")
gw_helpers.MessageDeduplicator = _StubMessageDeduplicator
sys.modules["gateway.platforms.helpers"] = gw_helpers

gw_base = type(sys)("gateway.platforms.base")
gw_base.BasePlatformAdapter = _StubBasePlatformAdapter
gw_base.MessageEvent = _StubMessageEvent
gw_base.MessageType = _StubMessageType
gw_base.SendResult = _StubSendResult
gw_base.SessionSource = _StubSessionSource
gw_base.cache_image_from_bytes = _stub_cache_image_from_bytes
gw_base.cache_document_from_bytes = _stub_cache_document_from_bytes

# Stub classify_send_error for adapters that use typed send errors (Hermes v0.19+).
# The real implementation lives in gateway.platforms.base; the stub keeps tests
# independent of the installed Hermes version.
def _stub_classify_send_error(exc=None, error_text=""):
    return "unknown"
gw_base.classify_send_error = _stub_classify_send_error
gw_base.SEND_ERROR_KINDS = frozenset({
    "too_long", "bad_format", "forbidden", "not_found",
    "rate_limited", "transient", "unknown",
})
sys.modules["gateway.platforms.base"] = gw_base

from lansenger.adapter import (
    API_ENDPOINTS,
    LansengerAdapter,
    RECONNECT_BACKOFF,
)


@pytest.fixture
def tmp_hermes_dir(tmp_path):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    return hermes


@pytest.fixture
def make_adapter(tmp_hermes_dir):
    def _make(app_id="test-app-id", app_secret="test-secret", api_gateway="https://apigw.test.com", home_chat_id=None):
        extra = {
            "app_id": app_id,
            "app_secret": app_secret,
            "api_gateway_url": api_gateway,
        }
        home_channel = _StubHomeChannel(chat_id=home_chat_id) if home_chat_id else None
        config = _StubPlatformConfig(enabled=True, extra=extra, home_channel=home_channel)
        adapter = LansengerAdapter(config)
        adapter._chat_type_file = tmp_hermes_dir / "lansenger_chat_types.json"
        adapter._token_file = tmp_hermes_dir / "lansenger_token.json"
        adapter._owner_id_file = tmp_hermes_dir / "lansenger_owner.json"
        adapter.handle_message = AsyncMock()
        return adapter
    return _make


@pytest.fixture
def adapter(make_adapter):
    return make_adapter()


WS_TICKET_URL = "wss://apigw.test.com/open/wss/v1?ticket=abcd1234-5678-9012-3456-789012345678"

WS_ENDPOINT_SUCCESS = {
    "errCode": 0,
    "errMsg": "OK",
    "data": {
        "wsEndpoint": WS_TICKET_URL,
        "expiresIn": 7200,
        "pingInterval": 50,
    },
}

WS_ENDPOINT_FAILURE = {
    "errCode": 10001,
    "errMsg": "invalid appId",
}

TOKEN_SUCCESS = {
    "errCode": 0,
    "errMsg": "OK",
    "data": {
        "appToken": "test-app-token-12345",
        "expiresIn": 7200,
    },
}

TOKEN_FAILURE = {
    "errCode": 10001,
    "errMsg": "invalid credentials",
}