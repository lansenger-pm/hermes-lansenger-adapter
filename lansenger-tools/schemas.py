"""Tool schemas for lansenger-tools — what the LLM sees.

Lansenger (蓝信) has multiple message/card types with different capabilities:

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

  NOTE: formatText and text both support @mention (reminder) on newer Lansenger versions,
  with reminder.all, reminder.userIds, and reminder.botIds fields.
  The API automatically prepends @displayName to the message based on the reminder
  parameters — do NOT manually write @name in the text content.
  Older Lansenger versions silently accept the reminder field but do NOT
  trigger client-side @mention notifications. Private chat supports reminder but it is unnecessary.

  Card types:
  - appCard: supports isDynamic + headStatusInfo (dynamic update), single language
  - i18nAppCard: supports 5 languages, no dynamic update, reserved for future use
  - appArticles: multi-article card (图文卡片)
  - linkCard: rich link preview card (description, iconLink, fromName, fromIconLink are required)

This constraint shapes all tool designs below:
- send_text:       msgType=text   → plain text + optional file/image/video attachment
- send_markdown:   msgType=formatText → Markdown text, NO attachments (reminder supported by API but not exposed in tool)
- send_file:       msgType=text   → file/image/video only, optional plain-text caption
- send_image_url:  msgType=text   → image from URL, optional plain-text caption
- revoke_message:  retracts previously sent messages
- send_link_card:  msgType=linkCard → rich link preview card (6 required fields per spec)
- send_app_articles: msgType=appArticles → multi-article card
- send_app_card:    msgType=appCard → rich card with div-style formatting + dynamic update
- update_dynamic_card: POST /v1/messages/dynamic/update → update appCard status
- query_groups:     GET /v2/groups/fetch → list bot's groups
"""

# ─── Text message (msgType=text) ───────────────────────────────────────────
# Supports: plain text, @mentions, file/image/video attachments
# Does NOT support: Markdown formatting

LANSENGER_SEND_TEXT = {
    "name": "lansenger_send_text",
    "description": (
        "Send a text message to a Lansenger (蓝信) user or group. "
        "Uses msgType=text: supports plain text, optional @mentions, and optional "
        "file/image/video attachments. Does NOT support Markdown formatting. "
        "If you need Markdown, use lansenger_send_markdown instead. "
        "The API automatically prepends @displayName when reminder is provided — "
        "do NOT write @name in the text content. "
        "If you need both Markdown AND a file, send them as two separate messages "
        "(lansenger_send_markdown for the formatted text, then lansenger_send_file for the attachment)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": (
                    "Recipient user ID or group chat ID on Lansenger. "
                    "For private messages, use the user's UID. For groups, use the group chat ID."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Plain text content. No Markdown."
                ),
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Optional local file/image/video to attach. "
                    "If provided, the text serves as a caption for the attachment. "
                    "Supported: images (jpg/png/gif/webp), videos (mp4/mov), documents (pdf/xlsx/docx/zip etc). "
                    "File size limits are determined by the organization's Lansenger configuration."
                ),
            },
            "media_type": {
                "type": "integer",
                "description": (
                    "Media type hint for the attachment: 1=video, 2=image, 3=file/document. "
                    "Auto-detected from file_path extension if omitted. "
                    "NOTE: For video (mediaType=1), the Lansenger API requires 2 mediaIds in the message body: "
                    "[videoId, coverImageId]. The adapter auto-extracts the first frame "
                    "as a cover image using ffmpeg and uploads it. If ffmpeg is unavailable, "
                    "the video is sent with a single mediaId which may fail."
                ),
                "enum": [1, 2, 3],
            },
            "reminder_all": {
                "type": "boolean",
                "description": (
                    "Set to true to @mention all members in a group chat. "
                    "The API automatically prepends @displayName — do NOT write @all in text."
                ),
            },
            "reminder_user_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of user IDs to @mention in a group chat. "
                    "The API automatically prepends @displayName based on these IDs — "
                    "do NOT write @name in the text content. "
                    "Matches the reminder.userIds field in the Lansenger API."
                ),
            },
            "reminder_bot_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of bot IDs to @mention in a group chat. "
                    "The API automatically prepends @displayName based on these IDs — "
                    "do NOT write @name in the text content. "
                    "Matches the reminder.botIds field in the Lansenger API."
                ),
            },
            "ref_msg_id": {
                "type": "string",
                "description": (
                    "Optional message ID (msgId) to quote/reply to. "
                    "The quoted message appears above your reply in the Lansenger client."
                ),
            },
        },
        "required": ["chat_id", "content"],
    },
}

# ─── Markdown message (msgType=formatText) ──────────────────────────────────
# Supports: Markdown formatting
# Does NOT support: @mentions, file/image/video attachments

LANSENGER_SEND_MARKDOWN = {
    "name": "lansenger_send_markdown",
    "description": (
        "Send a Markdown-formatted message to a Lansenger (蓝信) user or group. "
        "Uses msgType=formatText: supports Markdown formatting (headings, bold, italic, "
        "code blocks, lists, links, etc) and optional @mentions (reminder). "
        "The API automatically prepends @displayName based on reminder parameters — "
        "do NOT write @name in the text content. "
        "Older Lansenger versions silently accept the reminder field without "
        "triggering client-side @mention notifications. "
        "Does NOT support file/image/video attachments. "
        "If you need both Markdown AND a file, send them as two separate messages "
        "(this tool for the formatted text, then lansenger_send_file for the attachment)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Recipient user ID or group chat ID on Lansenger.",
            },
            "content": {
                "type": "string",
                "description": (
                    "Markdown-formatted content. "
                    "Supports: headings, bold, italic, code blocks, inline code, "
                    "lists, links, tables. "
                    "Does NOT support: attachments."
                ),
            },
            "reminder_all": {
                "type": "boolean",
                "description": (
                    "Set to true to @mention all members in a group chat. "
                    "The API automatically prepends @displayName — do NOT write @all in text. "
                    "Older Lansenger versions silently accept this without triggering notification."
                ),
            },
            "reminder_user_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of user IDs to @mention in a group chat. "
                    "The API automatically prepends @displayName based on these IDs — "
                    "do NOT write @name in the text content. "
                    "Older Lansenger versions silently accept this without triggering notification."
                ),
            },
            "reminder_bot_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of bot IDs to @mention in a group chat. "
                    "The API automatically prepends @displayName based on these IDs — "
                    "do NOT write @name in the text content. "
                    "Older Lansenger versions silently accept this without triggering notification."
                ),
            },
            "ref_msg_id": {
                "type": "string",
                "description": (
                    "Optional message ID (msgId) to quote/reply to. "
                    "The quoted message appears above your reply in the Lansenger client."
                ),
            },
        },
        "required": ["chat_id", "content"],
    },
}

# ─── File/image/video only (msgType=text, no text body) ────────────────────

LANSENGER_SEND_FILE = {
    "name": "lansenger_send_file",
    "description": (
        "Send a local file, image, or video to a Lansenger (蓝信) user or group. "
        "Uses msgType=text with attachment only (no text body). "
        "For text+attachment combo, use lansenger_send_text instead. "
        "Supported: images (jpg/png/gif/webp), videos (mp4/mov/avi), documents (pdf/xlsx/docx/zip etc). "
        "File size limits are determined by the organization's Lansenger configuration. "
        "For sending an image from a URL, use lansenger_send_image_url instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": (
                    "Recipient user ID or group chat ID on Lansenger."
                ),
            },
            "file_path": {
                "type": "string",
                "description": "Absolute or relative path to the local file. Must exist on disk.",
            },
            "caption": {
                "type": "string",
                "description": (
                    "Optional plain-text caption for the file (no Markdown). "
                    "If you need Markdown text alongside a file, "
                    "use lansenger_send_markdown first, then this tool separately."
                ),
            },
            "media_type": {
                "type": "integer",
                "description": (
                    "Media type hint: 1=video, 2=image, 3=file/document, 4=audio (default: auto-detect from extension)."
                ),
                "enum": [1, 2, 3, 4],
            },
            "width": {
                "type": "integer",
                "description": (
                    "Video/image width in pixels (optional; auto-detected via ffprobe if available). "
                    "Required for video uploads if ffprobe is not installed."
                ),
            },
            "height": {
                "type": "integer",
                "description": (
                    "Video/image height in pixels (optional; auto-detected via ffprobe if available). "
                    "Required for video uploads if ffprobe is not installed."
                ),
            },
            "duration": {
                "type": "integer",
                "description": (
                    "Video/audio duration in seconds (optional; auto-detected via ffprobe if available). "
                    "Required for video/audio uploads if ffprobe is not installed."
                ),
            },
        },
        "required": ["chat_id", "file_path"],
    },
}

LANSENGER_SEND_IMAGE_URL = {
    "name": "lansenger_send_image_url",
    "description": (
        "Send an image from a URL to a Lansenger (蓝信) user or group. "
        "Uses msgType=text with image attachment only. "
        "The image is downloaded first, then uploaded to Lansenger's media server. "
        "For a local file, use lansenger_send_file instead. "
        "For text+image combo, use lansenger_send_text instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Recipient user ID or group chat ID on Lansenger.",
            },
            "image_url": {
                "type": "string",
                "description": "URL of the image to download and send.",
            },
            "caption": {
                "type": "string",
                "description": "Optional plain-text caption (no Markdown).",
            },
        },
        "required": ["chat_id", "image_url"],
    },
}

# ─── Message management ────────────────────────────────────────────────────

LANSENGER_REVOKE_MESSAGE = {
    "name": "lansenger_revoke_message",
    "description": (
        "Revoke a previously sent Lansenger (蓝信) message. "
        "Shows a fixed system message to the receiver indicating the message was revoked. "
        "Supports both private and group chat. "
        "If chat_id is provided, the adapter auto-detects whether it's a group or private chat. "
        "Note: Custom sysMsg text/icon is NOT supported — the system message is fixed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of message IDs to revoke",
            },
            "chat_id": {
                "type": "string",
                "description": (
                    "The chat ID where the messages were sent. "
                    "If provided, the adapter auto-detects whether it's a group or private chat. "
                    "In group chats, pass the current group's chat_id from context."
                ),
            },
        },
        "required": ["message_ids"],
    },
}

LANSENGER_REPLY_MESSAGE = {
    "name": "lansenger_reply_message",
    "description": (
        "React to a previously sent Lansenger (蓝信) message with an emoji reply "
        "(requires Lansenger server v4.6.17+; older servers return NOT_SUPPORTED). "
        "Supported emoji_id values: TYPE_THUMB (点赞), TYPE_OK (OK), TYPE_ADD_ONE (+1), "
        "TYPE_FIREWORK (烟花), TYPE_LOVE (爱心), TYPE_DOUBT (疑问). "
        "To retract a reaction pass cancel=true — the same emoji_id is still required "
        "(the server rejects an emojiless cancel with 40060)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "Message ID to react to (e.g. from a message you sent or received)",
            },
            "emoji_id": {
                "type": "string",
                "enum": ["TYPE_THUMB", "TYPE_OK", "TYPE_ADD_ONE", "TYPE_FIREWORK", "TYPE_LOVE", "TYPE_DOUBT"],
                "description": "Emoji reaction to set, or to clear when cancel=true",
            },
            "cancel": {
                "type": "boolean",
                "description": "Set true to retract the emoji reaction instead of adding it (emoji_id still required)",
            },
        },
        "required": ["message_id", "emoji_id"],
    },
}

LANSENGER_SEND_LINK_CARD = {
    "name": "lansenger_send_link_card",
    "description": (
        "Send a linkCard message to a Lansenger (蓝信) user or group. "
        "Use this to send a rich link preview card with title, description, icon, and source info. "
        "Per the Lansenger API spec, title, description, iconLink, link, fromName, and fromIconLink "
        "are all required fields. pc_link is optional."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Recipient user ID or group chat ID on Lansenger",
            },
            "title": {
                "type": "string",
                "description": "Card title (required per API spec)",
            },
            "link": {
                "type": "string",
                "description": "Card click-through link (required per API spec)",
            },
            "description": {
                "type": "string",
                "description": "Card description text (required per API spec)",
            },
            "icon_link": {
                "type": "string",
                "description": "Card icon image URL (required per API spec)",
            },
            "from_name": {
                "type": "string",
                "description": "Card source name (required per API spec)",
            },
            "from_icon_link": {
                "type": "string",
                "description": "Source icon image URL (required per API spec)",
            },
            "pc_link": {
                "type": "string",
                "description": "PC client redirect link (optional)",
            },
        },
        "required": ["chat_id", "title", "link", "description", "icon_link", "from_name", "from_icon_link"],
    },
}

LANSENGER_SEND_APP_ARTICLES = {
    "name": "lansenger_send_app_articles",
    "description": (
        "Send an appArticles (图文卡片) multi-article card to a Lansenger (蓝信) user or group. "
        "Each article entry has an image, title, and clickable link. "
        "Use this to share a collection of articles or news items in a single card. "
        "For a single link card, use lansenger_send_link_card instead. "
        "For rich formatted cards with dynamic updates, use lansenger_send_app_card instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Recipient user ID or group chat ID on Lansenger",
            },
            "articles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "imgUrl": {
                            "type": "string",
                            "description": "Article image URL (required)",
                        },
                        "title": {
                            "type": "string",
                            "description": "Article title (required)",
                        },
                        "url": {
                            "type": "string",
                            "description": "Article content link URL (required)",
                        },
                        "pcUrl": {
                            "type": "string",
                            "description": "PC client content link URL (optional per API spec)",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Optional article summary text",
                        },
                    },
                    "required": ["imgUrl", "title", "url"],
                },
                "description": "List of article entries (1 or more). Each must have imgUrl, title, url. pcUrl is optional.",
            },
        },
        "required": ["chat_id", "articles"],
    },
}

LANSENGER_SEND_APP_CARD = {
    "name": "lansenger_send_app_card",
    "description": (
        "Send an appCard (应用卡片) rich formatted card to a Lansenger (蓝信) user or group. "
        "appCard supports div-style HTML formatting (color, font-size, text-align, text-indent) "
        "in bodyTitle, bodySubTitle, bodyContent, and signature fields. "
        "font-size MUST use pt unit (e.g. 14pt), NOT px — px is rejected by the API. "
        "Set is_dynamic=true to enable in-place status updates via lansenger_update_dynamic_card "
        "(e.g. approval workflows: pending → approved/rejected). "
        "bodyContent text-indent must always be 0em (bare 0 causes API failure)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Recipient user ID or group chat ID on Lansenger",
            },
            "body_title": {
                "type": "string",
                "description": (
                    "Card body title (required, max 600 bytes). "
                    "Supports div-style: color, font-size (pt only), text-align."
                ),
            },
            "head_title": {
                "type": "string",
                "description": "Card header title (max 96 bytes)",
            },
            "body_sub_title": {
                "type": "string",
                "description": (
                    "Card body subtitle (max 1200 bytes). "
                    "Supports div-style: color, font-size (pt only), text-align."
                ),
            },
            "body_content": {
                "type": "string",
                "description": (
                    "Card body content (max 3000 bytes). "
                    "Supports div-style: color, font-size (pt only), text-align, text-indent. "
                    "Always use text-indent:0em to avoid unwanted indentation."
                ),
            },
            "signature": {
                "type": "string",
                "description": "Card signature line (max 96 bytes). Supports color div-style only (no font-size, no text-align).",
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                    },
                },
                "description": (
                    "Key-value pairs (max 10 pairs). "
                    "Key max 18 bytes, value max 192 bytes per pair. "
                    "Both support color div-style only (no font-size, no text-align)."
                ),
            },
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                    },
                },
                "description": "Link entries (max 3 pairs). Title supports color and text-align div-style only (no font-size).",
            },
            "is_dynamic": {
                "type": "boolean",
                "description": (
                    "Enable dynamic card updates (default: false). "
                    "When true, the card can be updated in-place via lansenger_update_dynamic_card."
                ),
                "default": False,
            },
            "head_status_info": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Status description (max 30 bytes, required when is_dynamic=true). Supports color div-style.",
                    },
                    "colour": {
                        "type": "string",
                        "description": "Solid circle status color (e.g. #FFB116 amber, #198754 green, #dc3545 red)",
                    },
                    "iconLink": {
                        "type": "string",
                        "description": "Status icon URL",
                    },
                },
                "description": "Dynamic card status info. Required when is_dynamic=true.",
            },
            "card_link": {
                "type": "string",
                "description": "Card click-through link",
            },
            "pc_card_link": {
                "type": "string",
                "description": "PC client click-through link",
            },
            "staff_id": {
                "type": "string",
                "description": "Staff openId for showing sender avatar",
            },
            "head_icon_url": {
                "type": "string",
                "description": "Header icon URL",
            },
        },
        "required": ["chat_id", "body_title"],
    },
}

LANSENGER_SEND_APPROVE_CARD = {
    "name": "lansenger_send_approve_card",
    "description": (
        "Send an approveCard (审批卡片) native Lansenger card with clickable buttons. "
        "approveCard uses markdown-formatted body content and supports button callbacks "
        "via WebSocket events. Suitable for interactive workflows (approvals, confirmations, choices). "
        "Body content supports Markdown syntax."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Recipient user ID or group chat ID on Lansenger",
            },
            "head_title": {
                "type": "string",
                "description": "Card header title (max 96 bytes)",
            },
            "body_title": {
                "type": "string",
                "description": "Card body title",
            },
            "body_content": {
                "type": "string",
                "description": "Markdown body text (supports full Markdown syntax)",
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                    },
                },
                "description": "Key-value pairs displayed in the card body (max 10 pairs)",
            },
            "buttons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Button display text",
                        },
                        "button_theme": {
                            "type": "integer",
                            "description": "Button style: 1=primary(blue), 2=secondary(white/blue), 3=secondary(white/black), 4=danger(red)",
                            "default": 1,
                        },
                        "callback_info": {
                            "type": "string",
                            "description": "Callback string passed back via WebSocket when button is clicked. Use a unique identifier to distinguish which button was pressed.",
                        },
                    },
                    "required": ["text"],
                },
                "description": "Action buttons (max 4, displayed in order)",
            },
            "expire_time": {
                "type": "integer",
                "description": "Card expiry in seconds (default: 3600 = 1 hour)",
                "default": 3600,
            },
            "head_status": {
                "type": "string",
                "description": "Status description shown in card header badge (max 30 bytes)",
            },
            "head_status_color": {
                "type": "string",
                "description": "Hex color for status badge (default: #FFB116 amber). Common: #198754 green, #dc3545 red, #0d6efd blue",
                "default": "#FFB116",
            },
        },
        "required": ["chat_id", "head_title", "body_title"],
    },
}

LANSENGER_UPDATE_DYNAMIC_CARD = {
    "name": "lansenger_update_dynamic_card",
    "description": (
        "Update a dynamic card's status in-place (appCard or approveCard). "
        "For appCard: the card must have been sent with is_dynamic=true via lansenger_send_app_card. "
        "For approveCard: pass card_type='approveCard'. "
        "Uses the Lansenger dynamic update API to change headStatusInfo and optionally links."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "msg_id": {
                "type": "string",
                "description": "The message ID from the original lansenger_send_app_card response (required)",
            },
            "chat_id": {
                "type": "string",
                "description": (
                    "Optional chat ID (recipient user ID or group ID) where the card was sent. "
                    "Used to detect group vs DM routing for the update endpoint."
                ),
            },
            "head_status_info": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Updated status description (max 30 bytes). Supports color div-style.",
                    },
                    "colour": {
                        "type": "string",
                        "description": "Updated status color (e.g. #198754 green for approved, #dc3545 red for rejected)",
                    },
                    "iconLink": {
                        "type": "string",
                        "description": "Updated status icon URL",
                    },
                },
                "description": "Updated status info for the card header.",
            },
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                    },
                },
                "description": "Updated link entries (max 3 pairs). Title supports color and text-align.",
            },
            "is_last_update": {
                "type": "boolean",
                "description": (
                    "True = final state update, card becomes static and cannot be updated further "
                    "(default: false). Set true for approved/rejected final states."
                ),
                "default": False,
            },
            "card_type": {
                "type": "string",
                "description": "Card type to update: 'appCard' (default) or 'approveCard'. Required when updating an approveCard sent via lansenger_send_approve_card.",
                "enum": ["appCard", "approveCard"],
                "default": "appCard",
            },
        },
        "required": ["msg_id"],
    },
}

LANSENGER_QUERY_GROUPS = {
    "name": "lansenger_query_groups",
    "description": (
        "Query the bot's group ID list on Lansenger (蓝信). "
        "Returns the total number of groups and a list of group IDs. "
        "Use this to discover available group chat IDs before sending messages to groups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "page_offset": {
                "type": "integer",
                "description": "Page number starting from 0 (default: 0)",
                "default": 0,
            },
            "page_size": {
                "type": "integer",
                "description": "Number of groups per page (max 100, default: 100)",
                "default": 100,
            },
        },
    },
}

# ─── Group info tools ─────────────────────────────────────────────────────

LANSENGER_GET_GROUP_INFO = {
    "name": "lansenger_get_group_info",
    "description": (
        "Get detailed information about a Lansenger (蓝信) group. "
        "Returns group name, description, owner, total members, max members, "
        "group state (normal/disbanded), and other settings. "
        "Use this to look up group details when you know the group_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "The group ID to query",
            },
        },
        "required": ["group_id"],
    },
}

LANSENGER_DOWNLOAD_MEDIA = {
    "name": "lansenger_download_media",
    "description": (
        "Download a media file (image, file, video, voice) from Lansenger (蓝信) by its media_id. "
        "Use this when you need to re-download a file that was previously referenced in a message "
        "but the temporary file is no longer available (e.g. after a restart). "
        "The file is saved to a local temp path and returned. "
        "media_ids are embedded in inbound messages as '[Image: {media_id}]', '[File: {media_id}]', etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "media_id": {
                "type": "string",
                "description": "The media_id to download (e.g. '13107200-abc123...'). Found in inbound message tags like [File: media_id].",
            },
        },
        "required": ["media_id"],
    },
}

LANSENGER_GET_GROUP_MEMBERS = {
    "name": "lansenger_get_group_members",
    "description": (
        "Get the member list of a Lansenger (蓝信) group. "
        "Returns total member count and a list of members with their staffId, "
        "name, orgName, avatarUrl, and role (0=member, 1=assistant admin, 2=owner). "
        "Supports pagination via page_offset and page_size."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "The group ID to query",
            },
            "page_offset": {
                "type": "integer",
                "description": "Page number starting from 0 (default: 0)",
                "default": 0,
            },
            "page_size": {
                "type": "integer",
                "description": "Number of members per page (max 100, default: 100)",
                "default": 100,
            },
        },
        "required": ["group_id"],
    },
}

LANSENGER_CHECK_IN_GROUP = {
    "name": "lansenger_check_in_group",
    "description": (
        "Check whether a specific staff (user) or bot is in a Lansenger (蓝信) group. "
        "If staff_id is not provided, checks whether the current bot is in the group. "
        "Returns true/false. Useful for membership verification before sending messages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "The group ID to check",
            },
            "staff_id": {
                "type": "string",
                "description": (
                    "Optional staff ID to check. If omitted, checks the bot itself. "
                    "Priority: staff_id > user_token > app_token (bot)."
                ),
            },
        },
        "required": ["group_id"],
    },
}