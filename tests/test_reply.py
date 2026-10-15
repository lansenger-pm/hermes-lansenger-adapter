"""Tests for emoji replies (Lansenger server v4.6.17+)."""

import json
from unittest.mock import AsyncMock, MagicMock


def _make_http_response(data, status_code=200):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json = MagicMock(return_value=data)
    mock.raise_for_status = MagicMock()
    mock.text = json.dumps(data)
    return mock


async def _ensure_token(adapter):
    adapter._app_token = "test-token"
    adapter._token_expiry = 9999999999.0


class TestReplyMessage:
    async def test_reply_create_url_and_payload(self, make_adapter):
        adapter = make_adapter()
        await _ensure_token(adapter)
        adapter._http_client = AsyncMock()
        adapter._http_client.post = AsyncMock(
            return_value=_make_http_response({"errCode": 0, "errMsg": "OK"})
        )

        result = await adapter.reply_message("msg-1", "TYPE_OK")

        call_args = adapter._http_client.post.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url")
        assert "/v1/messages/reply/create" in url
        payload = call_args.kwargs.get("json", {})
        assert payload == {"replyType": 1, "msgId": "msg-1", "emojiId": "TYPE_OK"}
        assert result.success is True
        assert result.message_id == "msg-1"

    async def test_cancel_keeps_emoji_and_sets_reply_type_2(self, make_adapter):
        adapter = make_adapter()
        await _ensure_token(adapter)
        adapter._http_client = AsyncMock()
        adapter._http_client.post = AsyncMock(
            return_value=_make_http_response({"errCode": 0, "errMsg": "OK"})
        )

        result = await adapter.reply_message("msg-1", "TYPE_OK", reply_type=2)

        payload = adapter._http_client.post.call_args.kwargs.get("json", {})
        assert payload["replyType"] == 2
        assert payload["emojiId"] == "TYPE_OK"
        assert result.success is True

    async def test_unknown_emoji_rejected_without_http_call(self, make_adapter):
        adapter = make_adapter()
        await _ensure_token(adapter)
        adapter._http_client = AsyncMock()

        result = await adapter.reply_message("msg-1", "TYPE_HEART")

        assert result.success is False
        assert "invalid emoji_id" in result.error
        adapter._http_client.post.assert_not_called()

    async def test_empty_emoji_rejected_locally(self, make_adapter):
        adapter = make_adapter()
        await _ensure_token(adapter)
        adapter._http_client = AsyncMock()

        result = await adapter.reply_message("msg-1", "")

        assert result.success is False
        assert "emoji_id is required" in result.error
        adapter._http_client.post.assert_not_called()

    async def test_empty_message_id_rejected_locally(self, make_adapter):
        adapter = make_adapter()
        await _ensure_token(adapter)
        adapter._http_client = AsyncMock()

        result = await adapter.reply_message("", "TYPE_OK")

        assert result.success is False
        assert "message_id is required" in result.error
        adapter._http_client.post.assert_not_called()

    async def test_invalid_reply_type_rejected(self, make_adapter):
        adapter = make_adapter()
        await _ensure_token(adapter)
        adapter._http_client = AsyncMock()

        result = await adapter.reply_message("msg-1", "TYPE_OK", reply_type=3)

        assert result.success is False
        assert "invalid reply_type" in result.error
        adapter._http_client.post.assert_not_called()

    async def test_server_error_becomes_failure_with_errmsg(self, make_adapter):
        adapter = make_adapter()
        await _ensure_token(adapter)
        adapter._http_client = AsyncMock()
        adapter._http_client.post = AsyncMock(
            return_value=_make_http_response(
                {"errCode": 40060, "errMsg": "消息为空或格式错误(140503)"}
            )
        )

        result = await adapter.reply_message("msg-1", "TYPE_OK")

        assert result.success is False
        assert result.error == "消息为空或格式错误(140503)"
