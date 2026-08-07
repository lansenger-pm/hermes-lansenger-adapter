"""
Approval mixin for LansengerAdapter.
Handles the complete approval workflow: approveCard, appCard fallback,
button callback handling, and dynamic status updates.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from gateway.platforms.base import SendResult

from ._constants import API_ENDPOINTS, classify_lansenger_error

logger = logging.getLogger(__name__)


class ApprovalMixin:
    """Approval workflow methods for LansengerAdapter."""

    async def send_to_owner(self, content: str, format: str = "text") -> SendResult:
        """Send a text message to the bot owner (or home_channel if owner not set).
        
        Args:
            content: Message content
            format: 'text' for plain text, 'formatText' for Markdown
        """
        # Use home_channel as fallback if owner_id not set
        target_id = self._owner_id or self._home_channel_id
        if not target_id:
            return SendResult(success=False, error="Owner ID and home_channel not set", error_kind="unknown")
        if format == "formatText":
            return await self.send_format_text(target_id, content)
        return await self.send_text(target_id, content)

    async def send_exec_approval(
        self, chat_id: str, command: str, session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an approval card for a dangerous command.

        Tries approveCard first (native buttons + text instructions).
        Falls back to appCard if approveCard is not supported.
        """
        logger.info("[Lansenger] send_exec_approval: chat_id=%s, session=%s", chat_id, session_key[:16])

        # ── Step 1: Try native approveCard ──
        try:
            result = await self._send_approve_card(chat_id, command, description, session_key)
            if result.success:
                return result
            logger.info(
                "[Lansenger] approveCard not supported (%s), falling back to appCard",
                result.error,
            )
        except Exception as exc:
            logger.info(
                "[Lansenger] approveCard failed (%s), falling back to appCard",
                exc,
            )

        # ── Step 2: Fall back to dynamic appCard ──
        return await self._send_appcard_approval(chat_id, command, session_key, description)

    async def send_approve_card(
        self, chat_id: str, head_title: str, body_title: str,
        body_content: str = "", fields: Optional[List[dict]] = None,
        buttons: Optional[List[dict]] = None, expire_time: int = 3600,
        head_status: str = "", head_status_color: str = "#FFB116",
    ) -> SendResult:
        """Send a generic approveCard with buttons.

        approveCard is a native Lansenger card type with clickable buttons.
        Unlike appCard, it uses markdown-formatted body content and supports
        button callbacks via WebSocket events.

        Args:
            chat_id: Recipient user ID or group chat ID.
            head_title: Card header title (max 96 bytes).
            body_title: Card body title.
            body_content: Markdown body text.
            fields: [{key, value}] pairs displayed in the card body.
            buttons: [{text, button_theme, callback_info}] button array.
                     button_theme: 1=primary(blue), 2=secondary(white/blue),
                     3=secondary(white/black), 4=danger(red).
                     callback_info: arbitrary string passed back via WebSocket.
            expire_time: Card expiry in seconds (default 3600).
            head_status: Status description shown in card header (max 30 bytes).
            head_status_color: Hex color for status badge (default #FFB116).
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token", error_kind="unknown")

        is_group = self._is_group_chat(chat_id)

        approve_card_data: Dict[str, Any] = {
            "head": {
                "title": head_title[:96],
            },
            "body": {
                "title": body_title,
                "content": {
                    "formatType": 1,  # MARK_DOWN
                    "text": body_content,
                },
            },
            "buttons": buttons or [],
            "expireTime": expire_time,
        }

        if head_status:
            approve_card_data["head"]["headStatus"] = {
                "describe": head_status[:30],
                "statusIcon": 1,
                "colour": head_status_color,
            }

        if fields:
            approve_card_data["body"]["fields"] = fields[:10]

        if is_group:
            payload = {
                "groupId": chat_id,
                "msgType": "approveCard",
                "msgData": {"approveCard": approve_card_data},
            }
            url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
        else:
            payload = {
                "userIdList": [chat_id],
                "msgType": "approveCard",
                "msgData": {"approveCard": approve_card_data},
            }
            url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"

        logger.info(
            "[Lansenger] Sending generic approveCard (group=%s): %s",
            is_group, json.dumps(payload, ensure_ascii=False)[:500],
        )

        try:
            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response", error_kind="unknown")

            data = response.json()
            err_code = data.get("errCode", -1)
            if err_code != 0:
                logger.warning(
                    "[Lansenger] approveCard API error: errCode=%s, errMsg=%s",
                    err_code, data.get("errMsg", ""),
                )
                return SendResult(success=False, error=(_e := data.get("errMsg", f"errCode={err_code}")), error_kind=classify_lansenger_error(_e or ""))

            msg_id = data.get("data", {}).get("msgId")
            if msg_id:
                self._card_type_map[msg_id] = "approveCard"
                logger.info("[Lansenger] ✅ approveCard sent — msg_id=%s", msg_id)
                return SendResult(success=True, message_id=msg_id, raw_response=data)
            else:
                return SendResult(success=False, error="No msgId in response", error_kind="unknown")

        except httpx.HTTPStatusError as exc:
            logger.warning("[Lansenger] approveCard HTTP %s: %s", exc.response.status_code, exc)
            return SendResult(success=False, error=f"HTTP {exc.response.status_code}", error_kind=classify_lansenger_error(str(exc), exc))
        except Exception as exc:
            logger.warning("[Lansenger] approveCard send error: %s", exc)
            return SendResult(success=False, error=str(exc), error_kind=classify_lansenger_error(str(exc), exc))

    # ── approveCard (Phase 1 — button-observation) ────────────────────────

    async def _send_approve_card(
        self, chat_id: str, command: str, description: str, session_key: str,
    ) -> SendResult:
        """Send a native Lansenger approveCard with clickable buttons.

        Encodes ``ea:{choice}:{approval_id}`` in each button's ``callbackInfo``
        field so Phase 2 can resolve the approval when button-callback data
        arrives via WebSocket.
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token", error_kind="unknown")

        lang = self._get_lang(chat_id)
        approval_id = str(next(self._approval_counter))
        cmd_preview = command[:500] + "..." if len(command) > 500 else command

        if lang == "zh":
            head_title = "⚠️ 危险命令审批"
            body_title = "以下命令需要审批才能执行"
            body_text = (
                f"**命令:**\n```\n{cmd_preview}\n```\n\n"
                "**审批方式:** 点击下方按钮或回复以下命令:\n"
                "- `/approve` — 批准执行一次\n"
                "- `/approve session` — 本次会话有效\n"
                "- `/approve always` — 永久允许\n"
                "- `/deny` — 拒绝执行"
            )
            status_desc = "待审批"
            fields = [
                {"key": "风险说明", "value": description},
                {"key": "会话 ID", "value": session_key[:32]},
            ]
            btn_once = "批准一次"
            btn_session = "本会话有效"
            btn_always = "永久允许"
            btn_deny = "拒绝"
        else:
            head_title = "⚠️ Command Approval"
            body_title = "Dangerous Command Approval Request"
            body_text = (
                f"**Command:**\n```\n{cmd_preview}\n```\n\n"
                "**How to approve:** Click a button below or reply:\n"
                "- `/approve` — Execute once\n"
                "- `/approve session` — Allow this session\n"
                "- `/approve always` — Always allow\n"
                "- `/deny` — Deny"
            )
            status_desc = "Pending"
            fields = [
                {"key": "Reason", "value": description},
                {"key": "Session ID", "value": session_key[:32]},
            ]
            btn_once = "Allow Once"
            btn_session = "This Session"
            btn_always = "Always"
            btn_deny = "Deny"

        # Compute allowed approvers for group chats
        is_group = self._is_group_chat(chat_id)
        allowed_approvers: Optional[list] = None
        if is_group:
            allowed_approvers = []
            if self._owner_id:
                allowed_approvers.append(self._owner_id)
            for uid in self._approval_allow_from:
                if uid not in allowed_approvers:
                    allowed_approvers.append(uid)

        approve_card_data = {
            "head": {
                "title": head_title,
                "headStatus": {
                    "describe": status_desc,
                    "statusIcon": 1,
                    "colour": "#FFB116",
                },
            },
            "body": {
                "title": body_title,
                "content": {
                    "formatType": 1,  # MARK_DOWN
                    "text": body_text,
                },
                "fields": fields,
            },
            "buttons": [
                {
                    "text": btn_once,
                    "buttonTheme": 1,  # 主按钮 (蓝底白字)
                    "state": 0,
                    "callbackInfo": f"ea:once:{approval_id}",
                    **({"permissionScope": {"permittedStaffs": allowed_approvers}, "prohibitedState": 1} if allowed_approvers else {}),
                },
                {
                    "text": btn_session,
                    "buttonTheme": 2,  # 次按钮 (白底蓝字)
                    "state": 0,
                    "callbackInfo": f"ea:session:{approval_id}:{session_key}",
                    **({"permissionScope": {"permittedStaffs": allowed_approvers}, "prohibitedState": 1} if allowed_approvers else {}),
                },
                {
                    "text": btn_always,
                    "buttonTheme": 3,  # 次按钮 (白底黑字)
                    "state": 0,
                    "callbackInfo": f"ea:always:{approval_id}:{session_key}",
                    **({"permissionScope": {"permittedStaffs": allowed_approvers}, "prohibitedState": 1} if allowed_approvers else {}),
                },
                {
                    "text": btn_deny,
                    "buttonTheme": 4,  # 警告按钮 (红色)
                    "state": 0,
                    "callbackInfo": f"ea:deny:{approval_id}",
                    **({"permissionScope": {"permittedStaffs": allowed_approvers}, "prohibitedState": 1} if allowed_approvers else {}),
                },
            ],
            "expireTime": self._gateway_approval_timeout + 60,  # align with Hermes core gateway_timeout + 60s buffer
        }

        if is_group:
            payload = {
                "groupId": chat_id,
                "msgType": "approveCard",
                "msgData": {"approveCard": approve_card_data},
            }
            url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
        else:
            payload = {
                "userIdList": [chat_id],
                "msgType": "approveCard",
                "msgData": {"approveCard": approve_card_data},
            }
            url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"

        logger.info(
            "[Lansenger] Sending approveCard (approval_id=%s, group=%s, expireTime=%ss): %s",
            approval_id, is_group, approve_card_data.get("expireTime"),
            json.dumps(payload, ensure_ascii=False)[:800],
        )

        try:
            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response", error_kind="unknown")

            data = response.json()
            err_code = data.get("errCode", -1)
            if err_code != 0:
                logger.warning(
                    "[Lansenger] approveCard API error: errCode=%s, errMsg=%s",
                    err_code, data.get("errMsg", ""),
                )
                return SendResult(success=False, error=(_e := data.get("errMsg", f"errCode={err_code}")), error_kind=classify_lansenger_error(_e or ""))

            msg_id = data.get("data", {}).get("msgId")
            if msg_id:
                # Store for Phase 2 callback handling
                self._approval_state[approval_id] = session_key
                self._card_type_map[msg_id] = "approveCard"  # track for dynamic update
                # Extract trigger_sender_id from session_key for permission check
                # session_key format: agent:main:lansenger:group:{chat_id}:{sender_id} or
                #                      agent:main:lansenger:dm:{chat_id}
                trigger_sender_id = self._extract_trigger_sender_from_session(session_key)
                self._pending_approval_msgs[session_key] = (msg_id, trigger_sender_id)
                # Store by approval_id for precise button-callback matching
                self._approval_card_msgs[approval_id] = (msg_id, chat_id)
                self._save_approvals()
                logger.info(
                    "[Lansenger] ✅ approveCard sent — approval_id=%s, msg_id=%s, trigger_sender=%s. "
                    "Buttons: once/session/deny. Waiting for callback via WebSocket...",
                    approval_id, msg_id, trigger_sender_id[:16] if trigger_sender_id else "N/A",
                )
                return SendResult(success=True, message_id=msg_id, raw_response=data)
            else:
                return SendResult(success=False, error="No msgId in response", error_kind="unknown")

        except httpx.HTTPStatusError as exc:
            logger.warning(
                "[Lansenger] approveCard HTTP %s: %s", exc.response.status_code, exc,
            )
            return SendResult(success=False, error=f"HTTP {exc.response.status_code}", error_kind=classify_lansenger_error(str(exc), exc))
        except Exception as exc:
            logger.warning("[Lansenger] approveCard send error: %s", exc)
            return SendResult(success=False, error=str(exc), error_kind=classify_lansenger_error(str(exc), exc))

    # ── approveCard button callback handler ────────────────────────────

    async def _handle_approve_card_callback(self, event_data: Dict[str, Any]) -> None:
        """Process ``approve_card_callback`` events from approveCard button clicks.

        Event format::

            {
              "type": "approve_card_callback",
              "data": {
                "eventData": "ea:once:2",
                "staffId": "13107200-K2uBlTReymO6C27owEgC7kJkdIngvlk",
              }
            }

        ``eventData`` is the ``callbackInfo`` from the clicked button:
        ``ea:{choice}:{approval_id}`` or ``ea:{choice}:{approval_id}:{session_key}``.
        """
        callback_data = event_data.get("data", {})
        if not isinstance(callback_data, dict):
            return

        raw_event_data = callback_data.get("eventData", "")
        if not raw_event_data or not raw_event_data.startswith("ea:"):
            logger.warning("[Lansenger] approve_card_callback with unexpected eventData: %s", raw_event_data)
            return

        staff_id = callback_data.get("staffId", "")
        if not staff_id:
            logger.warning("[Lansenger] approve_card_callback missing staffId")
            return

        # Parse ea:{choice}:{approval_id}[:{session_key}]
        parts = raw_event_data.split(":", 1)  # ["ea", "choice:id[:session_key]"]
        if len(parts) < 2:
            return
        suffix = parts[1]  # e.g. "once:2" or "session:2:agent:main:lansenger:dm:..."

        # Split into choice, approval_id, and optional session_key
        colon_idx1 = suffix.find(":")
        if colon_idx1 == -1:
            return
        choice = suffix[:colon_idx1]
        remainder = suffix[colon_idx1 + 1:]

        colon_idx2 = remainder.find(":")
        if colon_idx2 == -1:
            # No session_key in callbackInfo (once/deny): remainder is just approval_id
            approval_id = remainder
            session_key: Optional[str] = None
        else:
            # session_key present (session/always): remainder = "approval_id:session_key"
            approval_id = remainder[:colon_idx2]
            session_key = remainder[colon_idx2 + 1:]

        logger.info(
            "[Lansenger] 🎯 approve_card_callback: choice=%s approval_id=%s staff=%s session=%s",
            choice, approval_id, staff_id[:16], (session_key or "N/A")[:60],
        )

        # Permission check
        if not self._check_approval_permission(staff_id):
            logger.warning(
                "[Lansenger] approve_card_callback permission DENIED: staff=%s",
                staff_id[:16],
            )
            return

        # Look up session_key if not embedded in callbackInfo
        if not session_key:
            session_key = self._approval_state.get(approval_id)
            if not session_key:
                logger.warning(
                    "[Lansenger] approve_card_callback: unknown approval_id=%s (no session_key in _approval_state)",
                    approval_id,
                )
                return

        # ── Resolve approval via Hermes core ──
        try:
            from tools.approval import resolve_gateway_approval
            resolve_gateway_approval(session_key, choice)
        except Exception:
            logger.exception(
                "[Lansenger] Failed to resolve gateway approval for session=%s choice=%s",
                session_key[:60], choice,
            )
            return

        # ── Update the approval card UI (using approval_id for precise match) ──
        card_info = self._approval_card_msgs.get(approval_id)
        if card_info:
            msg_id, chat_id = card_info
            self._approval_card_msgs.pop(approval_id, None)
            # Also clean up the session_key mapping
            self._pending_approval_msgs.pop(session_key, None)
            status = "denied" if choice == "deny" else "approved"
            logger.info(
                "[Lansenger] Updating approval card after button click: msg_id=%s chat=%s choice=%s status=%s",
                msg_id, chat_id[:20], choice, status,
            )
            result = await self.update_approval_status(chat_id, msg_id, status, choice)
            if result.success:
                self._save_approvals()
                logger.info("[Lansenger] Approval card updated: msg_id=%s", msg_id)
            else:
                logger.warning("[Lansenger] Failed to update approval card after button click: %s", result.error)

        logger.info(
            "[Lansenger] ✅ approve_card_callback resolved: choice=%s session=%s",
            choice, session_key[:60],
        )

    # ── Post-approval card updater ──────────────────────────────────────

    async def _maybe_update_approval_card(
        self, chat_id: str, sender_id: str, text: str, is_group: bool,
    ) -> None:
        """Update the approval card if *text* is an /approve or /deny command.

        Hermes resolves the approval internally in its slash command handler,
        but never notifies the adapter to update the card.  This hook fills
        that gap by checking if a pending approval card exists for this chat.

        Permission check: only owner_id or users in approval_allow_from can approve.
        """
        if not text.startswith("/"):
            return
        cmd = text.split()[0].lower().lstrip("/")
        if cmd not in ("approve", "deny"):
            return

        # Parse the approval variant from the suffix
        # /approve           → once
        # /approve session   → session
        # /approve always    → always
        # /deny              → deny
        if cmd == "deny":
            choice = "deny"
        else:
            suffix = text[len("/approve"):].strip().lower()
            if suffix in ("always", "permanent", "permanently"):
                choice = "always"
            elif suffix in ("session", "ses"):
                choice = "session"
            else:
                choice = "once"

        # Reconstruct session_key matching Hermes's build_session_key() format
        chat_type = "group" if is_group else "dm"
        if is_group:
            session_key = f"agent:main:lansenger:{chat_type}:{chat_id}:{sender_id}"
        else:
            session_key = f"agent:main:lansenger:{chat_type}:{chat_id}"

        pending = self._pending_approval_msgs.get(session_key)
        if not pending:
            logger.debug(
                "[Lansenger] No pending approval msg for session=%s (cmd=%s) — skipping card update",
                session_key[:60], cmd,
            )
            return

        msg_id, trigger_sender_id = pending

        # ── Permission check ──
        # Only owner_id or users in approval_allow_from can approve
        if not self._check_approval_permission(sender_id):
            logger.warning(
                "[Lansenger] Approval permission denied: sender=%s is not owner (%s) or in allowlist",
                sender_id[:16], self._owner_id[:16] if self._owner_id else "N/A",
            )
            # Send a brief rejection message
            lang = self._get_lang(chat_id)
            if lang == "zh":
                reject_msg = "⚠️ 您没有审批权限，只有机器人主人或配置的审批者可以审批命令。"
            else:
                reject_msg = "⚠️ You don't have approval permission. Only the bot owner or configured approvers can approve commands."
            await self.send_text(chat_id, reject_msg)
            return

        logger.info(
            "[Lansenger] Updating approval card after /%s (choice=%s): msg_id=%s, session=%s, approver=%s",
            cmd, choice, msg_id, session_key[:60], sender_id[:16],
        )
        result = await self.update_approval_status(chat_id, msg_id, "approved" if cmd == "approve" else "denied", choice)
        if result.success:
            self._pending_approval_msgs.pop(session_key, None)
            # Also clean up approval_card_msgs (find by msg_id)
            for aid, (cid_msg_id, _) in list(self._approval_card_msgs.items()):
                if cid_msg_id == msg_id:
                    self._approval_card_msgs.pop(aid, None)
            self._save_approvals()
        else:
            logger.warning(
                "[Lansenger] Failed to update approval card: %s", result.error,
            )

    # ── appCard fallback (text-based /approve) ────────────────────────────

    async def _send_appcard_approval(
        self, chat_id: str, command: str, session_key: str, description: str,
    ) -> SendResult:
        """Send a dynamic appCard approval card with isDynamic=True.

        NOTE: This uses appCard (not i18nAppCard).  appCard supports
        isDynamic + headStatusInfo for in-place status updates, but does
        NOT support multi-language (i18n).  i18nAppCard supports 5
        languages but cannot be dynamically updated and has no
        headStatusInfo — it is reserved for future use.

        Uses the user's cached language preference (from inbound messages)
        to select card content language.  Default: Chinese.

        After the user replies /approve, /approve session, /approve always,
        or /deny, the gateway intercepts those text replies and calls
        update_approval_status(), which uses the dynamic update API to
        change the card status in-place (待审批 → 已批准/已拒绝).
        """

        lang = self._get_lang(chat_id)
        cmd_preview = command[:300] + "..." if len(command) > 300 else command

        # --- Build appCard content in the user's language ---
        if lang == "zh":
            head_title = "⚠️ 危险命令审批"
            body_title = f"确认 {cmd_preview[:20]}"
            body_sub_title = description
            body_content = f"会话 ID: {session_key[:32]}\n命令:\n{cmd_preview}"
            status_desc = "待审批"
            signature = self._get_agent_signature("zh")
            fields = [
                {"key": "执行一次", "value": "/approve"},
                {"key": "本会话有效", "value": "/approve session"},
                {"key": "永久允许", "value": "/approve always"},
                {"key": "拒绝执行", "value": "/deny"},
            ]
        else:
            head_title = "⚠️ Command Approval"
            body_title = "Dangerous Command Approval Request"
            body_sub_title = description
            body_content = f"Session ID: {session_key[:32]}\nCommand:\n{cmd_preview}"
            status_desc = "Pending"
            signature = self._get_agent_signature("en")
            fields = [
                {"key": "Execute Once", "value": "/approve"},
                {"key": "This Session", "value": "/approve session"},
                {"key": "Always Allow", "value": "/approve always"},
                {"key": "Deny", "value": "/deny"},
            ]

        # Escape HTML in dynamic content to prevent accidental div parsing
        body_content = self._escape_html(body_content)
        body_sub_title = self._escape_html(body_sub_title)

        # Dynamic card: head status info shows "待审批" (amber)
        head_status_info = {
            "description": self._build_status_div(status_desc, "#FFB116"),
            "colour": "#FFB116",
        }

        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token", error_kind="unknown")

        try:
            url = self._build_send_url(chat_id, token)
            app_card_data = {
                "headTitle": head_title,
                "headIconUrl": "",
                "isDynamic": True,
                "headStatusInfo": head_status_info,
                "bodyTitle": f'<div style="color:#000;font-size:15pt;text-align:left">{body_title}</div>',
                "bodySubTitle": f'<div style="color:rgba(0,0,0,.47);font-size:13pt;text-align:left">{body_sub_title}</div>',
                "bodyContent": f'<div style="color:#000;font-size:13pt;text-align:left;text-indent:0em">{body_content}</div>',
                "signature": f'<div style="color:rgba(0,0,0,.47)">{signature}</div>',
                "fields": fields,
                "cardLink": "",
                "pcCardLink": "",
            }

            payload = self._build_app_card_payload(chat_id, app_card_data)

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response", retryable=True, error_kind="transient")

            data = response.json()
            if data.get("errCode") != 0:
                return SendResult(success=False, error=(_e := data.get("errMsg")), error_kind=classify_lansenger_error(_e or ""))

            msg_id = data.get("data", {}).get("msgId")
            self._card_type_map[msg_id] = "appCard"
            # Extract trigger_sender_id from session_key for permission check
            trigger_sender_id = self._extract_trigger_sender_from_session(session_key)
            self._pending_approval_msgs[session_key] = (msg_id, trigger_sender_id)
            logger.info("[Lansenger] appCard approval sent to %s, msgId=%s, trigger_sender=%s, lang=%s", chat_id, msg_id, trigger_sender_id[:16] if trigger_sender_id else "N/A", lang)
            return SendResult(success=True, message_id=msg_id, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Send appCard approval error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True, error_kind=classify_lansenger_error(str(e), e))

    def _build_app_card_payload(self, chat_id: str, app_card_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the outer payload for an appCard message with correct routing."""
        is_group = self._is_group_chat(chat_id)
        if is_group:
            return {
                "groupId": chat_id,
                "msgType": "appCard",
                "msgData": {"appCard": app_card_data},
            }
        return {
            "userIdList": [chat_id],
            "msgType": "appCard",
            "msgData": {"appCard": app_card_data},
        }

    def _build_send_url(self, chat_id: str, token: str) -> str:
        """Build the correct endpoint URL based on chat type."""
        is_group = self._is_group_chat(chat_id)
        if is_group:
            return f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
        return f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a dynamic appCard slash-command confirmation card.

        NOTE: This uses appCard (not i18nAppCard).  See send_exec_approval
        docstring for the appCard vs i18nAppCard distinction.

        Uses the user's cached language preference to select content language.

        Used by the gateway's ``_maybe_confirm_destructive_slash`` gate for
        /new, /reset, /undo.  Lansenger does not support inline button
        callbacks like Telegram, so this card displays the confirmation
        request with fields showing the text-based reply options
        (/approve, /always, /cancel).

        The gateway's text intercept recognises /approve, /always, /cancel
        and routes them through ``slash_confirm.resolve()``.

        Returns SendResult(success=True) so the gateway skips the
        redundant text fallback.
        """
        logger.info("[Lansenger] send_slash_confirm: chat_id=%s, title=%s, confirm_id=%s", chat_id, title, confirm_id)
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token", error_kind="unknown")

        lang = self._get_lang(chat_id)
        command_name = title.strip() if title else "unknown"

        if lang == "zh":
            head_title = f"🔄 {command_name} 确认"
            body_title = "会话操作确认请求"
            body_content = self._escape_html(message or "此操作将修改当前会话。")
            status_desc = "待确认"
            signature = self._get_agent_signature("zh")
            fields = [
                {"key": "确认执行", "value": "/approve"},
                {"key": "本会话免确认", "value": "/always"},
                {"key": "取消", "value": "/cancel"},
            ]
        else:
            head_title = f"🔄 {command_name} Confirm"
            body_title = "Session Action Confirmation"
            body_content = self._escape_html(message or "This action will modify your current session.")
            status_desc = "Pending"
            signature = self._get_agent_signature("en")
            fields = [
                {"key": "Approve Once", "value": "/approve"},
                {"key": "Always This Session", "value": "/always"},
                {"key": "Cancel", "value": "/cancel"},
            ]

        head_status_info = {
            "description": self._build_status_div(status_desc, "#FFB116"),
            "colour": "#FFB116",
        }

        try:
            url = self._build_send_url(chat_id, token)
            app_card_data = {
                "headTitle": head_title,
                "headIconUrl": "",
                "isDynamic": True,
                "headStatusInfo": head_status_info,
                "bodyTitle": f'<div style="color:#000;font-size:15pt;text-align:left">{body_title}</div>',
                "bodyContent": f'<div style="color:#000;font-size:13pt;text-align:left;text-indent:0em">{body_content}</div>',
                "signature": f'<div style="color:rgba(0,0,0,.47)">{signature}</div>',
                "fields": fields,
                "cardLink": "",
                "pcCardLink": "",
            }

            payload = self._build_app_card_payload(chat_id, app_card_data)

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response", retryable=True, error_kind="transient")

            data = response.json()
            if data.get("errCode") != 0:
                logger.error("[Lansenger] Slash confirm card API error: errCode=%s, errMsg=%s", data.get("errCode"), data.get("errMsg"))
                return SendResult(success=False, error=(_e := data.get("errMsg")), error_kind=classify_lansenger_error(_e or ""))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] Slash confirm appCard sent to %s, msgId=%s", chat_id, msg_id)
            return SendResult(success=True, message_id=msg_id, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Send slash confirm appCard error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True, error_kind=classify_lansenger_error(str(e), e))

    async def update_approval_status(
        self, chat_id: str, message_id: str,
        status: str, choice: str = "",
        card_type: str = "",
    ) -> SendResult:
        """Update a dynamic approval card status in-place.

        Supports both approveCard (via ``approveCardUpdateMsg``) and
        appCard (via ``appCardUpdateMsg``).  Card type is auto-detected
        from the internal ``_card_type_map``; defaults to appCard mode
        for backwards compatibility.  Pass ``card_type`` explicitly
        to override auto-detection (e.g. ``"approveCard"``).

        When *choice* is provided (``once``/``session``/``always``/``deny``),
        the card's buttons are replaced with a single greyed-out button
        showing the chosen action (e.g. "已允许执行一次" / "已拒绝执行").

        Args:
            chat_id: Recipient user ID (used to determine language)
            message_id: The message ID of the original card to update
            status: One of 'pending', 'approved', 'denied'
            choice: Optional — 'once', 'session', 'always', 'deny'
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token", error_kind="unknown")

        card_type = card_type or self._card_type_map.get(message_id, "appCard")
        lang = self._get_lang(chat_id)

        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['message']['dynamic_update']}?app_token={token}"

            # Status label (short, fits in header)
            if lang == "zh":
                status_text = {"pending": "待审批", "approved": "已批准", "denied": "已拒绝"}.get(status, "待审批")
            else:
                status_text = {"pending": "Pending", "approved": "Approved", "denied": "Denied"}.get(status, "Pending")
            status_color = {"pending": "#FFB116", "approved": "#198754", "denied": "#dc3545"}.get(status, "#FFB116")
            is_final = status != "pending"

            if card_type == "approveCard":
                # Build language-specific result button text
                choice_labels: Dict[str, Dict[str, str]] = {
                    "once":    {"zh": "已允许执行一次", "en": "Allowed once"},
                    "session": {"zh": "已允许本会话有效", "en": "Allowed this session"},
                    "always":  {"zh": "已永久允许", "en": "Allowed permanently"},
                    "deny":    {"zh": "已拒绝执行", "en": "Denied"},
                }
                buttons = []
                if choice and is_final:
                    label = choice_labels.get(choice, {}).get(lang, choice_labels.get(choice, {}).get("en", choice))
                    buttons = [{
                        "text": label,
                        "buttonTheme": 3,  # 次按钮 (白底黑字)
                        "state": 1,        # 禁用
                    }]

                payload = {
                    "msgId": message_id,
                    "msgType": "approveCard",
                    "msgData": {
                        "approveCardUpdateMsg": {
                            "headStatus": {
                                "describe": status_text,
                                "statusIcon": 1,
                                "colour": status_color,
                            },
                            "buttons": buttons,
                        }
                    }
                }
            else:
                # appCardUpdateMsg — dynamic update for appCard (legacy)
                payload = {
                    "msgId": message_id,
                    "msgType": "appCard",
                    "msgData": {
                        "appCardUpdateMsg": {
                            "isLastUpdate": is_final,
                            "headStatusInfo": {
                                "description": self._build_status_div(status_text, status_color),
                                "colour": status_color,
                            },
                        }
                    }
                }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                logger.warning(
                    "[Lansenger] Update card status failed (type=%s): errCode=%s, errMsg=%s",
                    card_type, data.get("errCode"), data.get("errMsg"),
                )
                return SendResult(success=False, error=(_e := data.get("errMsg")), error_kind=classify_lansenger_error(_e or ""))

            logger.info(
                "[Lansenger] Card status updated to %s (type=%s, lang=%s, choice=%s)",
                status, card_type, lang, choice or "-",
            )
            return SendResult(success=True, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Update appCard status error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True, error_kind=classify_lansenger_error(str(e), e))
