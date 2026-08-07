"""
Shared constants for Lansenger adapter modules.
Import-safe — has no circular dependencies on adapter.py or mixin modules.
"""

MAX_MESSAGE_LENGTH = 4000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
INBOUND_SILENCE_TIMEOUT = 7200  # 2h — matches ticket TTL; no inbound WS message for this long = silent death

# API Endpoints
API_ENDPOINTS = {
    "auth": {
        "tenant_access_token": "/auth/v3/tenant_access_token/internal",
    },
    "websocket": {
        "endpoint": "/v1/ws/endpoint/create",
    },
    "smart_bot": {
        "private_message": "/v1/bot/messages/create",
        "group_message": "/v1/messages/group/create",
    },
    "app": {
        "upload_media": "/v1/app/medias/create",
    },
    "message": {
        "revoke": "/v1/messages/revoke",
        "dynamic_update": "/v1/messages/dynamic/update",
    },
    "groups": {
        "fetch": "/v2/groups/fetch",
        "info": "/v2/groups/{group_id}/info/fetch",
        "members": "/v2/groups/{group_id}/members/fetch",
        "is_in_group": "/v2/groups/{group_id}/members/is_in_group",
    },
}


# ── Lansenger errMsg → SendResult.error_kind mapping ──────────────────────
# classify_send_error() (from gateway.platforms.base) is platform-neutral and
# matches English substrings used by Telegram / major APIs.  Lansenger returns
# Chinese errMsg strings, so we layer a small Lansenger-specific classifier on
# top: callers should try this first, then fall back to classify_send_error()
# for connection-level / HTTP-status classification.
_LANSENGER_ERR_KIND_SUBSTRINGS = (
    (("用户不存在", "user not found", "user does not exist", "成员不存在"), "not_found"),
    (("群不存在", "group not found", "群组不存在", "已经退出", "not in group"), "not_found"),
    (("无权限", "no permission", "forbidden", "权限不足", "不是群成员", "被移出"), "forbidden"),
    (("频率限制", "rate limit", "flood", "过于频繁", "请稍后"), "rate_limited"),
    (("消息过长", "too long", "message too long", "内容超长"), "too_long"),
    (("格式错误", "parse error", "parse entities", "格式不支持", "bad format"), "bad_format"),
)


def classify_lansenger_error(err_text: str = "", exc=None) -> str:
    """Classify a Lansenger send failure into a SEND_ERROR_KINDS value.

    Checks the Chinese/English errMsg first, then defers to the platform-
    neutral ``classify_send_error`` for connection-level and HTTP-status cues.
    Returns one of the strings in ``SEND_ERROR_KINDS``.
    """
    text = (err_text or "").lower()
    for needles, kind in _LANSENGER_ERR_KIND_SUBSTRINGS:
        for n in needles:
            if n.lower() in text:
                return kind
    # Defer to the platform-neutral classifier (handles httpx ConnectError →
    # transient, HTTP 403 → forbidden, etc.)
    try:
        from gateway.platforms.base import classify_send_error
        return classify_send_error(exc, err_text)
    except Exception:
        return "unknown"
