"""Core-compatibility tests for ``send_exec_approval``.

Hermes core v0.21+ calls
``adapter.send_exec_approval(..., allow_permanent=, allow_session=, smart_denied=)``
from ``gateway/run_turn_runner.py``.  An adapter signature that lacks those
keywords raises ``TypeError`` inside the gateway's button-approval path, which
is caught upstream and silently downgrades every approval request to a
plain-text ``/approve`` prompt — the native approveCard is never sent.

These tests pin the two contracts that keep the card path alive:
the keyword signature, and the button gating derived from the flags.
"""

import inspect
import json
from unittest.mock import AsyncMock, MagicMock

from lansenger.approval import ApprovalMixin


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


CHAT_ID = "2285568-user123"
SESSION_KEY = "agent:main:lansenger:dm:2285568-user123"


def _wire_adapter(adapter, responses):
    """Give the adapter a token + owner-DM routing and scripted HTTP replies."""
    adapter._app_token = "test-token"
    adapter._token_expiry = 9999999999.0
    adapter._owner_id = CHAT_ID
    adapter._http_client = AsyncMock()
    adapter._http_client.post = AsyncMock(side_effect=responses)
    return adapter


def _last_payload(adapter):
    return adapter._http_client.post.call_args.kwargs.get("json", {})


def _button_kinds(card):
    return [button["callbackInfo"].split(":")[1] for button in card["buttons"]]


class TestSendExecApprovalSignature:
    def test_signature_accepts_core_v021_capability_flags(self):
        params = inspect.signature(ApprovalMixin.send_exec_approval).parameters
        for flag in ("allow_permanent", "allow_session", "smart_denied"):
            assert flag in params, (
                f"gateway/run_turn_runner.py passes {flag}= to send_exec_approval; "
                "a missing keyword silently downgrades approvals to text"
            )

    async def test_flags_are_optional_for_old_cores(self, make_adapter):
        """A core that only passes the original five arguments still works."""
        adapter = _wire_adapter(
            make_adapter(),
            [_make_http_response({"errCode": 0, "data": {"msgId": "msg-1"}})],
        )

        result = await adapter.send_exec_approval(
            chat_id=CHAT_ID, command="rm -rf /tmp/x", session_key=SESSION_KEY,
        )

        assert result.success is True
        assert _last_payload(adapter)["msgType"] == "approveCard"


class TestApprovalButtonGating:
    async def _send(self, adapter, **flags):
        adapter = _wire_adapter(
            adapter,
            [_make_http_response({"errCode": 0, "data": {"msgId": "msg-1"}})],
        )
        result = await adapter.send_exec_approval(
            chat_id=CHAT_ID, command="rm -rf /tmp/x", session_key=SESSION_KEY, **flags,
        )
        assert result.success is True
        return _last_payload(adapter)["msgData"]["approveCard"]

    async def test_default_offers_all_four_buttons(self, make_adapter):
        card = await self._send(make_adapter())
        assert _button_kinds(card) == ["once", "session", "always", "deny"]

    async def test_smart_denied_offers_only_once_and_deny(self, make_adapter):
        card = await self._send(make_adapter(), allow_session=False, allow_permanent=False)

        assert _button_kinds(card) == ["once", "deny"]
        body = card["body"]["content"]["text"]
        assert "/approve session" not in body
        assert "/approve always" not in body

    async def test_permanent_off_keeps_session_grant(self, make_adapter):
        card = await self._send(make_adapter(), allow_permanent=False)
        assert _button_kinds(card) == ["once", "session", "deny"]

    async def test_session_off_keeps_permanent_grant(self, make_adapter):
        card = await self._send(make_adapter(), allow_session=False)
        assert _button_kinds(card) == ["once", "always", "deny"]

    async def test_buttons_still_authorise_only_owner_in_group(self, make_adapter):
        """Group cards keep the permittedStaffs scope on every surviving button."""
        adapter = make_adapter()
        adapter._chat_type_map["group-xyz"] = "group"
        adapter._owner_id = "2285568-owner"
        adapter = _wire_adapter(
            adapter, [_make_http_response({"errCode": 0, "data": {"msgId": "msg-1"}})],
        )
        adapter._owner_id = "2285568-owner"

        result = await adapter.send_exec_approval(
            chat_id="group-xyz", command="rm -rf /tmp/x", session_key=SESSION_KEY,
            allow_session=False, allow_permanent=False,
        )

        assert result.success is True
        card = _last_payload(adapter)["msgData"]["approveCard"]
        assert _button_kinds(card) == ["once", "deny"]
        for button in card["buttons"]:
            assert button["permissionScope"]["permittedStaffs"] == ["2285568-owner"]


class TestAppCardFallbackGating:
    async def test_fallback_fields_respect_flags(self, make_adapter):
        """When approveCard is rejected, the appCard fallback gates its fields too."""
        adapter = _wire_adapter(
            make_adapter(),
            [
                _make_http_response({"errCode": 10001, "errMsg": "approveCard not supported"}),
                _make_http_response({"errCode": 0, "data": {"msgId": "msg-fallback"}}),
            ],
        )

        result = await adapter.send_exec_approval(
            chat_id=CHAT_ID, command="rm -rf /tmp/x", session_key=SESSION_KEY,
            allow_permanent=False,
        )

        assert result.success is True
        payload = _last_payload(adapter)
        assert payload["msgType"] == "appCard"
        values = [field["value"] for field in payload["msgData"]["appCard"]["fields"]]
        assert values == ["/approve", "/approve session", "/deny"]
