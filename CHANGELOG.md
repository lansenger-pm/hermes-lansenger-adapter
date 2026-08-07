# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [2.9.17] - 2026-08-07

### Added

- **Typed send-error classification (`error_kind`)**: all `SendResult` failure paths in `adapter.py` and `approval.py` now populate the `error_kind` field introduced in Hermes Agent v0.19.0. This lets the gateway's delivery-obligation ledger distinguish transient network errors, forbidden/permission failures, rate-limiting, and not-found conditions instead of relying on substring matching. A new `classify_lansenger_error()` helper (in `_constants.py`) maps Lansenger's Chinese `errMsg` strings to the standard `SEND_ERROR_KINDS` vocabulary, deferring to the framework's `classify_send_error()` for connection-level and HTTP-status classification.

### Changed

- **Hermes Agent compatibility floor updated**: the adapter now gracefully supports Hermes v0.17 through v0.20+. The `classify_send_error` import is optional — on Hermes <0.19 (which lacks typed send errors) the adapter degrades to `error_kind="unknown"` instead of failing to load.
- Verified against Hermes Agent v0.20.0: all 184 tests pass.

## [2.9.16] - 2026-07-16

### Fixed
- **Encoding crash on Chinese Windows**: 6 file read/write calls in `adapter.py` (owner ID, chat type map, `.env` config) were missing `encoding="utf-8"`, causing `UnicodeDecodeError` on systems where the default encoding is GBK. All file I/O now explicitly uses UTF-8.

## [2.9.15] - 2026-07-15

### Changed
- **`LANSENGER_API_GATEWAY_URL` is now required**: the previous default URL (`https://open.e.lanxin.cn/open/apigw`) was never a valid endpoint. Users must now provide their API Gateway URL explicitly via env var or config.
- All documentation, skills, and config examples now use `https://your-api-gateway-url` as a placeholder instead of exposing real endpoint addresses.

### Fixed
- 14 documentation issues: typos (French "Rebots"→"Robots", double parenthesis), translation errors (zhHant/HK "瀏覽狀態"→"狀態"), unclosed code blocks (after-install.fr/zhHant/zhHantHK), missing sub-plugin warnings (after-install.fr/zhHantHK), inconsistent Multi-Workspace section (after-install.zhHant), missing `lansenger_download_media` tool in all 5 README tool tables.

## [2.9.14] - 2026-07-15

### Fixed
- **NameError in `_send_appcard_approval`** (#10): `token` variable was undefined, causing the approveCard → appCard fallback path to crash. Added `token = await self._get_app_token()` before use.
- **HTTP connection leak in lansenger-tools** (#11): ephemeral adapters created a new `httpx.AsyncClient` per tool invocation but never closed it. All 14 `_xxx_async()` functions now close the client in a `finally` block.
- **Inbound silence timeout too aggressive** (#8): `INBOUND_SILENCE_TIMEOUT` increased from 1800s (30min) to 7200s (2h, matching ticket TTL). Now configurable via `LANSENGER_INBOUND_SILENCE_TIMEOUT` env var.

### Removed
- Dead code: `_shared_http_client` singleton (`_get_shared_http_client()` / `_close_shared_http_client()`) — never called since `_create_ephemeral_adapter()` intentionally creates a fresh client per invocation (#13).

## [2.9.13] - 2026-07-15

### Fixed
- **DM routing when owner_id unknown**: `_is_group_chat()` now consults the chat type map (populated from inbound WS events) as fallback when `_owner_id` is not yet set. Prevents DM messages from being incorrectly routed to the group chat API, which would cause send failures before the first inbound DM.

## [2.9.12] - 2026-07-10

### Added
- **`approveCard` message support**: inbound approveCard messages now parsed with title and text.
- **`box` (聊天记录/合并转发) message support**: direct box messages display sender, time, and content per item; referenced boxes use compact format.
- **`forward` message support**: standalone forward messages (as reference) now show the forward title.
- **Sender & time in references**: all quoted messages now include sender ID and send time.
- **Sender & time in box items**: each sub-message in direct box messages now annotated with sender and timestamp.
- **`content` field for image/video/file/voice**: direct inbound media messages now extract the optional `content` field (text description).

### Changed
- Reference box messages now use compact single-line format to avoid double-tag clutter.

## [2.9.11] - 2026-07-09

### Added
- **`lansenger_download_media` tool**: download media files by media_id — enables Agent to re-download files lost on restart.
- **MediaId in inbound messages**: all media types (image/video/file/voice) now include their media_id in the message text as `[Type: {media_id}]`.

### Changed
- **Images routed to `media_urls`**: inbound images are now passed via `event.media_urls` for vision processing, not embedded as text paths.
- **Reference messages enhanced**: format/sticker/file/image/video/voice references now download media, extract content, and include media_id.
- **Original filenames preserved**: downloaded media files now use the API-returned filename as temp file prefix.

### Fixed
- `_extract_reference_text` now supports missing reference types (format, sticker, file with content).
- Media download failures retain the media_id in message text for later re-download.

## [2.9.10] - 2026-07-07

### Fixed
- **WebSocket stability**: Pong timeout raised to 15s minimum (from 10s), per Lansenger server team recommendation.
- **WebSocket close**: all `ws.close()` calls now await completion with timeout before reconnecting.

## [2.9.9] - 2026-07-06

### Added
- `source.is_bot` metadata on inbound messages to distinguish bot vs human senders.
- Skill guidance: use `reminder.userIds` for humans, `reminder.botIds` for bots.

### Fixed
- **tools.py**: ephemeral adapter now creates a fresh `httpx.AsyncClient` per invocation — eliminates `Event loop is closed` errors in tool calls.
- **auto @reply / auto quote race condition**: switched from global mutable dict to `contextvars` for per-task sender isolation.

## [2.9.8] - 2026-07-01

### Changed
- **Code structure refactoring**: Split `adapter.py` (4128 lines) into 9 modular files using Mixin pattern — `_constants.py`, `ws_lifecycle.py`, `message_handler.py`, `token_manager.py`, `media.py`, `group_query.py`, `cards.py`, `approval.py`, `i18n_utils.py`. No functional changes.

### Fixed
- Resolved ABC abstract method conflict: added explicit `connect`/`disconnect` delegation methods in `adapter.py` to satisfy `BasePlatformAdapter`'s abstract method requirements.

## [2.9.7] - 2026-07-01

### Fixed

- **Hermes v0.17+ compatibility**: `connect()` and `disconnect()` now accept `**kwargs` to match Hermes Agent's updated `connect(self, *, is_reconnect: bool = False)` signature. Without this, gateway runner fails with `TypeError` on startup.

### Added

- **`supports_code_blocks` capability flag**: Set to `True` — Lansenger formatText natively renders Markdown fenced code blocks. Gateway-aware code-block formatting is now enabled.
- **`splits_long_messages` capability flag**: Set to `True` — Adapter's `send()` handles long-message splitting internally. Gateway no longer truncates messages before delivery.

## [2.9.6] - 2026-06-26

### Fixed

- **Token cross-bot reuse**: Persisted token now includes `app_id` and is validated on load. Switching bots via `hermes setup gateway` no longer reuses the old bot's valid token. Fixes outbound messages being sent with wrong bot credentials.

### Security

- Token file (`lansenger_token.json`) now stores `app_id` for cross-bot identity validation across both adapter and tools plugin paths.

## [2.9.5] - 2026-06-26

### Fixed

- **Slash command i18n**: Changed field name from `description_i18n` to `i18nDescription` to match the Lansenger API spec.
- **Command registration refresh**: Gateway now deletes old commands before re-registering, ensuring field name changes take effect. Added retry loop (3 attempts, 30s intervals) for transient failures.

### Changed

- **Language detection**: Simplified `_detect_lang()` — removed CJK punctuation range to avoid false positives from Japanese text. Only CJK Unified Ideographs trigger `zh`.

## [2.9.4] - 2026-06-25

### Changed

- **Simplified group/DM routing**: `_is_group_chat()` now uses `owner_id` exclusively: `chat_id == owner_id` → DM, everything else → group. Removes dependency on `_chat_type_map` cache and `group:` prefix heuristic. Correctly handles new groups with no prior inbound history.
- **Simplified revoke_message**: Replaced `chat_type` + `sender_id` parameters with `chat_id`. Adapter auto-detects group vs DM using the new routing logic. `sender_id` is no longer required (API revokes bot's own messages). Removed dead `_resolve_chat_type()` method.

### Fixed

- **Revoke message routing**: Previously hardcoded to `bot` type by default, causing group message revoke to fail unless LLM explicitly passed `chat_type="group"`. Now auto-detected from `chat_id`.

### Documentation

- Updated all 5 READMEs, `lansenger-tools/__init__.py`, schemas, and `lansenger-messaging` skill to reflect the simplified revoke API.

## [2.9.3] - 2026-06-25

### Added

- **Group info tools**: Three new tools for Lansenger group management:
  - `lansenger_get_group_info` — Get detailed group information (name, description, member count, state, avatar)
  - `lansenger_get_group_members` — Get group member list with pagination (name, org, role: member/assistant-admin/owner)
  - `lansenger_check_in_group` — Check whether a staff or bot is in a specific group
- **Group context injection**: Group info and member list (≤100 members) are automatically injected into the Hermes system prompt via `chat_topic` on `SessionSource`. Cached for 5 minutes per group.
- **`lansenger_send_approve_card`** added to all READMEs (was missing from documentation)

### Fixed

- **Tool adapter loading**: Fixed `_get_adapter_class()` in `lansenger-tools` to try `hermes_plugins.lansenger_platform.adapter` as the first import path, fixing "Cannot load LansengerAdapter" errors during tool execution
- **First-message group context**: Changed from fire-and-forget to synchronous await for group info on first message, so `chat_topic` is available from the very first turn in a new session

### Changed

- **Skills updated**: `lansenger-messaging` v2.0.0 → v2.1.0 (group context injection docs, new tools in decision tree); `lansenger-setup` v1.4.0 → v1.5.0 (group tools reference, auto-injection notes)

## [2.9.2] - 2026-06-25

### Added

- **`respond_to_at_all`** config option: Controls whether `@all` messages bypass `require_mention`. Default `false` (backward incompatible — @all is now blocked by default; set to `true` to restore old behavior). Supports per-group override via `groups.<chatId>.respond_to_at_all`.

### Fixed

- **Slash command registration**: Filter out commands with special characters (hyphens, dots, etc.) that the API rejects. 4 commands with hyphens (`set-home`, `codex-runtime`, `reload-mcp`, `reload-skills`) are now excluded from registration.
- **scopeType mapping**: Fixed `SCOPE_SINGLE_CHAT` from value `1` (which maps to "single group member" requiring `staffId`) to value `3` ("single chat" requiring only `chatId`+`chatType`), fixing `ChatId invalid` errors on command registration.

## [2.9.1] - 2026-06-25

### Added

- **Approval permission control**: Only the bot owner and users in `approval_allow_from` can approve dangerous commands. Three-layer defense:
  - `permissionScope.permittedStaffs` on approveCard buttons (Lansenger server-side enforcement, buttons disabled for unauthorized users)
  - Intercept `/approve`/`/deny` text commands in adapter before reaching Hermes core (which has no built-in permission check)
  - `_check_approval_permission()` in card update flow as secondary defense
- **`LANSENGER_APPROVAL_ALLOW_FROM`** config option (env and `config.yaml`) for listing additional approvers beyond the bot owner
- **approveCard button callback handling (Phase 2)**: Parse `approve_card_callback` WebSocket events, resolve approvals via `resolve_gateway_approval()`, and update cards in-place. Button click now fully works end-to-end.
- **Approval card persistence**: Pending approval cards are saved to `lansenger_approvals.json` and restored on restart. Expired entries are cleaned automatically.
- **`_approval_card_msgs`** dict keyed by `approval_id` for precise button-to-card matching (no conflict when multiple approvals are pending in the same session)

### Changed

- `_pending_approval_msgs` now stores `(msg_id, trigger_sender_id)` tuples for tracking who triggered an approval

## [2.9.0] - 2026-06-24

### Changed

- **`group_allow_from` semantic change (BREAKING)**: Changed from group-ID-level whitelist to sender-level whitelist, aligning with OpenClaw's semantics. In `allowlist` mode, the `groups` config map keys now serve as the group allowlist (only listed groups are allowed). `group_allow_from` now controls which senders can trigger the bot within allowed groups. Per-group `allow_from` remains available for finer-grained sender control.

### Added

- **`is_at_all` bypass for mention gate**: Messages with `isAtAll=true` now pass the `require_mention` gate without requiring a direct @mention.
- **`@botName` stripping only for slash command detection**: The trailing `@botName` that Lansenger appends to group messages is now only stripped for slash command (e.g. `/status`) detection. Non-command messages pass the full text (including `@botName`) to the agent.

### Fixed

- **Group policy documentation aligned across all languages**: Updated all 5 `after-install` translations, `SKILL.md`, and `plugin.yaml` to reflect the new policy model. zhHant after-install was still on the old `deny`/`allow` model — now fully migrated to `open`/`allowlist`/`disabled`.

## [2.8.2] - 2026-06-24

### Fixed

- **CLOSE_WAIT zombie connection detection**: When the Lansenger server closes the WS connection but the OS socket lingers in CLOSE_WAIT, `async for message in ws` blocks forever and the adapter never triggers reconnect. Added application-layer keepalive (`_ws_keepalive`) that sends a custom ping every 120s via the WS; if `send()` hangs or throws (dead socket), the WS is forcibly closed to trigger the reconnect loop. Plus a watchdog task (`_ws_watchdog`, every 60s) that restarts the WS task if it dies unexpectedly. Both complement the websockets library's TCP-level ping/pong.
- **Event loop closed crash protection**: Added RuntimeError guards in `_run_ws` (reconnect block), `_on_ws_task_done` (restart scheduling), and `_get_app_token` (HTTP pre-check) to prevent unhandled crashes when the asyncio event loop is closed — the adapter now logs a fatal status and exits cleanly instead of dying silently.

## [2.8.1] - 2026-06-23

### New Features

- **approveCard support**: Native Lansenger approveCard with 4 clickable buttons (Allow Once / Session / Always / Deny) for dangerous command approval. Falls back to appCard when the API does not support approveCard.
- **Card status update on /approve**: Approval cards are automatically updated in-place when the user replies `/approve`, `/approve session`, `/approve always`, or `/deny`. Status bar changes to "Approved"/"Denied" and buttons are replaced with a single greyed-out choice label (e.g. "Allowed once").
- **Built-in command registration**: Hermes's 53 built-in gateway commands (e.g. `/approve`, `/status`, `/help`) are now auto-registered alongside plugin commands. CLI-only commands are filtered out; gateway-only commands default to owner-scoped visibility.
- **Multi-language descriptions**: 40+ command descriptions now include `description_i18n` with zhHans/zhHant/zhHantHK/en/fr translations, matching Lansenger's native i18n support.

### Changed

- `approveCard` is tried first in `send_exec_approval`; falls back to appCard on failure.
- `update_approval_status` now supports both `approveCardUpdateMsg` and `appCardUpdateMsg` based on the card type.

## [2.8.0] - 2026-06-23

### New Features

- **Slash Command auto-registration**: Automatically registers all Hermes plugin slash commands with the Lansenger Bot API on startup. Commands are grouped by permission level (`owner`/`admin`/`everyone`) and registered with appropriate Lansenger `scopeType` values. Cleanup is performed on disconnect.
- **Permission-based command visibility**: Three-tier permission model configurable via `config.yaml` `extra.command_permissions`. `owner` — only in bot owner's private chat; `admin` — owner + all group admins; `everyone` — owner + all groups. Default is `everyone`.
- **Slash command dispatch**: Incoming messages starting with `/` are matched against registered plugin commands and dispatched directly to their handlers, bypassing the LLM. Permission checks are enforced at execution time.
- **New module `platforms/lansenger/commands.py`**: Contains `register_all_commands()`, `delete_all_commands()`, `dispatch_slash_command()`, `resolve_command_permissions()`, and `build_command_payloads()`.

## [2.7.1] - 2026-06-22

### Fixed

- **`page_offset` defaults to 1 instead of 0**: The V2 `/v2/groups/fetch` API expects `page_offset` to start from 0, but the adapter, tool handler, and schema all defaulted to 1, causing the first page of group results to be skipped. Changed default to 0 in all three locations.

## [2.7.0] - 2026-06-22

### New Features

- **Group chat policy**: open/allowlist/disabled with per-group overrides. Each group supports `enabled`, `require_mention`, `auto_mention_reply`, `auto_quote_reply`, and `allow_from` (sender ID whitelist). Five-step decision priority: group-level config > global config > defaults.
- **Auto @mention reply (`auto_mention_reply`)**: Automatically @mention the sender in group replies. Respects `fromType` field: `0` (user) → `reminder.userIds`, `1` (app/bot) → `reminder.botIds`.
- **Auto quote reply (`auto_quote_reply`)**: Automatically include `refMsgId` referencing the inbound message. Works in both group chats and DMs.
- **Tools expose `reminder_bot_ids` and `ref_msg_id`**: `lansenger_send_text` and `lansenger_send_markdown` now accept `reminder_bot_ids` (for @mentioning bots) and `ref_msg_id` (for quoting/reply). Adapter `send_text` and `send_text_with_media` also support `ref_msg_id`.
- **`lansenger-setup` skill**: Teaches the Agent how to configure the Lansenger plugin — finding group IDs, staff IDs, and applying group policies.
- **FormatText inbound parsing**: Correctly parses WS events with `msgType=format` (Markdown messages from OpenClaw and other bots).
- **Multi-profile support**: Token, chat_type, and owner files now respect `HERMES_HOME` env var, enabling multiple isolated bot profiles (`hermes profile create`).

### Fixed

- **`update_dynamic_card_status` group/DM routing**: Added `chat_id` parameter through the full chain (tool → handler → adapter) so dynamic appCard status updates can distinguish group vs DM context. `lansenger_update_dynamic_card` tool now accepts optional `chat_id`.

### Changed

- **YAML native booleans**: All boolean config fields now accept YAML native `true`/`false` (no quotes) in addition to string `"true"`/`"false"` and env vars.
- **Updated all translations**: README and after-install updated in zhHans, zhHant, zhHantHK, and French with new feature docs.

## [2.6.15] - 2026-06-22

### Fixed

- **_mark_disconnected() killed the reconnect loop (Issue #6)**: `_mark_disconnected()` from base.py sets `self._running = False`, but `_run_ws()` uses `while self._running` to control its reconnect loop. After one disconnect the loop exited immediately — backoff, ticket refresh, everything was skipped. Replaced with direct `_write_runtime_status_safe()` calls in both the normal disconnect path and crash handler.

## [2.6.14] - 2026-06-18

### Fixed

- **Rolled back v2.6.12 Fix 2 (stale ticket detection)**: The Lansenger API returns the same WebSocket ticket for its entire 2-hour validity window. The stale ticket guard blocked all reconnects during the window because the API never returns a different ticket until the old one expires. Replaced with simple reconnect: accept whatever URL the API returns, reconnect with it.
- **Rolled back v2.6.12 Fix 3 (600s idle timeout)**: Unnecessary — the websockets library's built-in ping/pong keep-alive already detects dead connections. Forcing reconnect after 10 minutes of inactivity disrupted healthy connections.
- **Empty try blocks in lansenger-tools (SyntaxError)**: 9 async message handlers had empty `try: / except Exception: pass` wrappers that were syntactically invalid in Python 3, preventing the lansenger-tools module from loading and causing all tool calls to fail.

### Changed

- **Unified plugin.yaml field names**: `lansenger-tools/plugin.yaml` changed `secret` → `password` and added missing `prompt` fields, matching `platforms/lansenger/plugin.yaml` and Hermes built-in plugin conventions
- **Author attribution**: Replaced "Lansenger PM Team" with "Lanxin Mobile (Beijing) Technology Co., ltd." across all `plugin.yaml` files, matching `pyproject.toml`

## [2.6.13] - 2026-06-18

### Fixed

- **Infinite reconnect loop from stale ticket guard (Issue #5)**: v2.6.12's Fix 2 had a control-flow defect where `continue` skipped the `ws_url` update, causing the loop to retry with the same expired URL. Two failure modes:
  - **Zombie connection** — server accepted the handshake but the session was dead, leading to 600s idle timeout → repeat
  - **Timeout spiral** — ticket fully expired, 15s connect timeout → repeat
  Added stale retry counter (limit 3) with progressive backoff (10s, 20s, 30s). After 3 retries, forces a clean reconnect cycle to fetch a fresh ticket. Counter resets on successful connection.

### Changed

- **Unified plugin.yaml field names**: `lansenger-tools/plugin.yaml` changed `secret` → `password` and added missing `prompt` fields, matching `platforms/lansenger/plugin.yaml` and Hermes built-in plugin conventions
- **Author attribution**: Replaced "Lansenger PM Team" with "Lanxin Mobile (Beijing) Technology Co., ltd." across all `plugin.yaml` files, matching `pyproject.toml`

## [2.6.12] - 2026-06-15

### Fixed

- **WebSocket reconnect hang (Issue #3)**: Three fixes for the reconnection loop that caused silent, permanent disconnection:
  - **Fix 1 — httpx client refresh**: Close and recreate `AsyncClient` on each reconnect attempt to avoid stale connection pool zombies (30s hang on `_get_websocket_url()`)
  - **Fix 2 — stale ticket detection**: Compare new ticket UUID against the previous one; skip connect and retry if Lansenger API returns the same expired ticket
  - **Fix 3 — idle timeout**: Wrap `async for message in ws` with `asyncio.timeout(600)` (10 min) to force reconnect when the connection opens but delivers no data

## [2.6.11] - 2026-06-11

### New Features

- **Plugin Hooks for monitoring and observability**: Registered 4 event hooks via `ctx.register_hook()` using Hermes core `invoke_hook`-triggered hook names:
  - `pre_llm_call` — Log LLM call initiation (platform, session_id, sender, model, first_turn)
  - `pre_tool_call` — Log tool calls before execution
  - `post_tool_call` — Log tool execution results (status)
  - `pre_gateway_dispatch` — Log messages before gateway dispatch

- **Hook logging toggle**: Hook logging can be controlled via:
  - Environment variable: `LANSENGER_HOOK_LOGGING=true/false`
  - config.yaml: `platforms.lansenger.extra.hook_logging: true/false`
  - Priority: env var > config > default (true)

### Changed

- **Credential priority adjusted to match Hermes convention**: Environment variables now take precedence over config.yaml `extra` fields. This aligns with 12-Factor App principles and simplifies containerized deployments.
  - Before: `config.yaml extra` > `env var`
  - After: `env var` > `config.yaml extra`
  - Affected fields: `LANSENGER_APP_ID`, `LANSENGER_APP_SECRET`, `LANSENGER_API_GATEWAY_URL`

### Compatibility

- **Hermes v0.16.0 compatible**: Verified compatibility with Hermes Agent v0.16.0 (The Surface Release). No breaking changes required for:
  - Configuration format v28 → v29 (write_approval migration)
  - Gateway post-delivery behavior changes
  - Streaming socket read timeout adjustments

## [2.6.10] - 2026-06-04

### Bug Fixes (Issue #2 — WebSocket Reconnect Hang)

- **WebSocket reconnect hangs after keepalive ping timeout**. When the Lansenger server sends a `1011 (keepalive ping timeout)` close frame, the adapter reconnects with a new ticket but `websockets.connect()` hangs indefinitely during the HTTP upgrade handshake — the server accepts TCP but stalls the 101 response. Added `asyncio.wait_for(timeout=15)` safety net around `websockets.connect()` so a stalled handshake triggers `TimeoutError` and falls through to the reconnect loop instead of hanging forever. Also added explicit `open_timeout=10` parameter to `websockets.connect()`.

### Bug Fixes (Issue #1 — Code Audit)

- **Bug 1: `_run_async` thread pool deadlock** (tools.py). Replaced `ThreadPoolExecutor(max_workers=1)` fallback with `asyncio.run_coroutine_threadsafe(coro, loop)` — injects coroutines into the gateway's existing event loop instead of spinning a new thread+loop per tool call. Eliminates single-worker queue deadlock and 30s timeout on large file uploads. Timeout raised to 120s.
- **Bug 2: `_chat_type_map` routing failure for unknown chat IDs**. Added `_is_group_chat(chat_id)` helper with heuristic fallback: `group:` prefix → group, otherwise → DM (with warning log). Replaced all 6 `self._chat_type_map.get(chat_id) == "group"` checks.
- **Bug 3: `_persist_chat_type_map` writes on every inbound message**. Introduced `_chat_type_map_dirty` flag — `_persist_chat_type_map()` now skips disk I/O when the map hasn't changed. Persists once at the end of `_on_message` (per-batch) instead of per-event.
- **Bug 4: NOT A BUG**. `send_text_with_media` uses numeric `mediaType` (1/2/3) per Lansenger send API spec; `upload_media_file` uses string type (`video`/`image`/`file`/`audio`) per upload API spec. These are two different APIs with different parameter types — both correct.
- **Bug 5: Ephemeral adapter creates new httpx per tool call**. Introduced `_get_shared_http_client()` singleton with `atexit` cleanup — all tool invocations reuse the same TCP+TLS connection pool. Removed all `await adapter._http_client.aclose()` calls from tool handlers.
- **Bug 6: `_send_image_url_async` lacks separate connect/read timeouts**. Changed `timeout=30` → `httpx.Timeout(10.0, read=60.0)` — 10s connect timeout, 60s read timeout for large image downloads.
- **Bug 7: Approval/confirm/update_prompt cards hardcoded to private endpoint**. Added `_build_send_url(chat_id, token)` and `_build_app_card_payload(chat_id, app_card_data)` helpers. All three card methods now route to the correct endpoint (private vs group) based on `_is_group_chat(chat_id)`.
- **Bug 8: `send_app_card` missing text-indent fix**. Added `_fix_text_indent()` method (regex: `text-indent:0` → `text-indent:0em`) and `_fix_app_card_styles(field, is_body_content=False)` that applies both px→pt conversion and text-indent fix. `bodyContent` now gets `is_body_content=True` so bare-zero text-indent is always fixed.
- **Bug 9: `_probe_duration` truncates to int → duration=0 risk**. Changed `int(float(val))` → `max(1, round(float(val)))` — 0.5s video no longer becomes `duration=0`; minimum duration is 1.

### Code Quality (Issue #1 — Low Priority)

- **Issue 10**: `_escape_html` now also escapes `&` → `&amp;` (prevents misinterpretation as HTML entity references in div-style content).
- **Issue 11**: `_detect_lang` merged double-loop (unicodedata + CJK range) into single loop over CJK Unicode ranges only — removes `unicodedata` dependency and halves iteration count.
- **Issue 12**: Removed redundant `import json` inside `_load_owner_id`, `_save_owner_id`, `_on_message`, `_load_chat_type_map`, `_persist_chat_type_map`, `_convert_font_px_to_pt`, `_get_agent_name`. `json` and `re` now at top-level imports only.
- **Issue 13**: Changed `str | None` → `Optional[str]` in `_prompt_field` for Python <3.10 compatibility.
- **Issue 14**: Token expiry double-offset refactored — `persist_expiry = timestamp + expires_in; cache_expiry = persist_expiry - 300`. No longer `self._token_expiry + 300` confusing restore.

## [2.6.9] - 2026-06-03

### Tool Progress Formatting

- **`format_tool_event` override**: Return Markdown-formatted strings instead of the base class plain-text output. Tool call progress now renders with bold tool names (`**web_search**`), inline code for argument keys (`\`['query', 'limit']\``), and bullet-style argument display in verbose mode. Sent via formatText (msgType=formatText) to leverage Lansenger's Markdown renderer.

  | Mode | Before (plain text) | After (Markdown) |
  |---|---|---|
  | all/new | `🔍 web_search: "蓝信断连"` | `🔍 **web_search**：蓝信断连` |
  | verbose | `🔍 web_search(["query"]) {"query":"蓝信断连","limit":3}` | `🔍 **web_search** \`['query', 'limit']\`\n**query**：蓝信断连\n**limit**：3` |
  | no preview | `🔍 bash...` | `🔍 **bash** ...` |

### extract_local_files — Decision

- **No override**: Default regex-based implementation + existing `send_file`/`send_video` chain already covers the full delivery path:
  - Lansenger-supported image formats (jpg/jpeg/png/webp/gif) → inline preview via `send_file(media_type=2)`
  - Unsupported image formats (bmp/tiff/svg) → file delivery via `send_document(media_type=3)` — still deliverable, just no inline preview
  - Video → `send_video` → `send_file(media_type=1)` with auto cover frame extraction via ffmpeg (`_extract_video_cover`) producing dual mediaIds `[videoId, coverImageId]`
- Images and videos can always be sent as generic files (media_type=3) — the native format only adds inline preview.

## [2.6.8] - 2026-06-03

### Bug Fixes

- **Tempfile leak fix**: `_save_media_temp` and `_extract_video_cover` now clean up temporary files properly — previously, cover images and inbound media temp files were never deleted, causing disk space leaks over long-running sessions.
- **File descriptor cleanup**: `upload_media_file` reads the entire file into memory via `open().read()` and closes the handle immediately; previously the `with open()` block was inside the `files=...` multipart upload, keeping the FD open until the HTTP request completed.
- **Crash guard in send_file**: `send_file` wraps the cover-image upload in `try/finally` to guarantee temp-file cleanup even on upload failure; previously a cover upload exception left the temp file orphaned.

## [2.6.7] - 2026-05-28

### Bug Fixes

- **WS reconnect — stale task reference**: After `_ws_task.cancel()`, the adapter set `_ws_task = None` before the cancelled task finished executing. The `_run_ws` coroutine's `finally` block then set `_connected = False`, but the adapter's `_is_ws_alive()` check compared against `_ws_task is not None and not _ws_task.done()`, causing a false "alive" report during reconnect. Fixed by clearing `_ws_task` only after confirming the task is fully done.
- **WS reconnect — reconnect loop exit**: `_reconnect_ws()` created a new `_ws_task` but didn't reset `_running` flag when the old task was cancelled during an active reconnect. On rare timing, `_run_ws` checked `_running` and exited immediately, silently dropping the connection. Fixed by ensuring `_running` stays True throughout reconnect.
- **WS logging improvements**: Log full endpoint response body on connection failure (previously truncated); log reconnect attempt count and backoff delay; log HTTP error details on ticket fetch failure.

## [2.6.6] - 2026-05-28

### Bug Fixes

- **Silent WS task crash on ticket expiry**: When `get_ws_endpoint()` returned an expired ticket, `_run_ws` raised `websockets.exceptions.InvalidStatusCode` but the async task had no exception handler — the task silently died and `_connected` stayed True forever. Fixed by wrapping the initial connection in a try/except that logs the error and triggers reconnect instead of crashing the task.

## [2.6.5] - 2026-05-26

### Media Upload

- **Switch to `/v1/app/medias/create`**: Previous `/v1/medias/create` API was limited to 1 MB and intended for avatar uploads only. The new endpoint supports files up to 10–20 MB and uses string type parameter (`image`/`video`/`file`/`audio`) instead of numeric media type.
- **Video cover image auto-extraction**: Lansenger video messages require `mediaIds=[videoId, coverImageId]` (2 elements). `send_file(media_type=1)` now auto-extracts the first frame via ffmpeg (`_extract_video_cover`) and uploads it as a cover image, producing the required dual-mediaId payload.
- **ffprobe auto-detection**: `upload_media_file` auto-detects video/image width/height and audio/video duration via ffprobe when not explicitly provided. `_probe_video_size` and `_probe_duration` probe media metadata before upload.
- **`send_video` convenience method**: Wraps `send_file` with `media_type=1` for cleaner agent tool calls.

### Bug Fixes

- **`home_channel` missing platform field**: Gateway startup builds a `SessionSource` from `home_channel`, which requires a `platform` field. When `home_channel` was configured without the field (e.g. from manual config edits), it triggered a `KeyError` crash. Fixed by adding a fallback platform value.

## [2.6.4] - 2026-05-15

### appCard Formatting

- **div-style per API spec**: appCard `description` field now supports div-style inline formatting per Lansenger API specification — `font-size` (px→pt auto-conversion), `text-indent` (must specify unit, e.g. `0em` not bare `0`). Previous implementation used plain text for description and didn't handle unit requirements.
- **headStatusInfo semantics clarification**: `headStatusInfo.description` is plain text only (<30 bytes); `headStatusInfo.colour` controls the status dot color only — these are two independent parts, not mixed.

### Cleanup

- **Remove OpenClaw content**: Strayed OpenClaw-specific configuration examples and references removed from Hermes plugin docs.

## [2.6.3] - 2026-05-15

### Skill & Plugin Installation

- **SKILL restructured to Hermes directory format**: `lansenger-messaging.md` → `lansenger-messaging/SKILL.md + references/`. Slimmed SKILL to ~60 lines of actionable decision rules; detailed API reference moved to `references/` subdirectory.
- **Expand script auto-installs skill**: `__init__.py` auto-expand now copies the `skills/` directory alongside sub-plugins, eliminating manual skill install steps from docs.

### Bug Fixes

- **`import json` missing**: `adapter.py` used `json` module but didn't import it — caused `NameError` on token caching paths.
- **`_running` flag**: Set `_running = True` before creating WS task; without this, `_run_ws` exited immediately because the loop checked `_running` at entry.

### Tests

- **74/74 unit tests passing**: Added test framework covering outbound (text/formatText/linkCard/appArticles/appCard/revoke), media upload, inbound, lifecycle, token, and dynamic card updates.

## [2.6.2] - 2026-05-15

### Bug Fixes

- **WS connection lifecycle logging**: Log full endpoint response on connection (previously truncated wsEndpoint URL); log reconnect attempt tracking; log HTTP error details on ticket fetch failure.

## [2.6.1] - 2026-05-14

### Bug Fixes

- **Message body audit**: Fixed multiple message assembly bugs discovered during API field validation:
  - `linkCard` — added missing required fields (`iconLink`, `imageUrl`)
  - `formatText` — reminder field correctly nested inside `formatText` object (not at top level)
  - `revoke` — `chatType` limited to `bot`/`group` only per API spec (removed invalid `staff` type)
  - Removed duplicate WS heartbeat (ping interval from endpoint response already drives pings)
- **`chat_type_map` persistence**: Chat type (group vs DM) now persisted to `~/.hermes/lansenger_chat_types.json` so ephemeral tools route group messages correctly even after gateway restart.

### Group Chat

- **Group routing for all send methods**: All 6 outbound methods (`send_text`, `send_format_text`, `send_text_with_media`, `send_app_card`, `send_app_articles`, `send_link_card`) now correctly route to the group endpoint (`/v1/messages/group/create`) when `chat_type_map` indicates a group chat.

### Infrastructure

- **appToken persistent caching**: Token now cached in `~/.hermes/lansenger_token.json` with auto-refresh across gateway processes. Eliminates redundant API calls for `tenant_access_token` on every restart.

## [2.6.0] - 2026-05-12

### Approval Workflow

- **Dynamic appCard with in-place status update**: Approval workflow upgraded from static i18nAppCard to dynamic appCard (`isDynamic=true` + `headStatusInfo`). Cards can be updated in-place via `updateDynamicCard` API (`/v1/messages/dynamic/update`) — no need to send a new card when approval state changes.
- **`headStatusInfo` field**: Controls status dot color (`grey`/`green`/`red`) and optional short description (<30 bytes, plain text). Two independent parts — description text + dot color.
- **`lansenger_update_dynamic_card` tool**: Agent-callable tool for updating dynamic card status (pending → approved/denied).

### appCard Formatting

- **font-size px→pt auto-conversion**: Lansenger appCard `font-size` requires pt units; px values render incorrectly. `sendAppCard` now auto-converts CSS `font-size` from px to pt.

### Internationalization

- **User language detection**: Adapter detects per-user language preference (zh/en) from inbound messages and caches it. Approval cards and formatText messages use the detected language for UI labels.
- **Multi-language READMEs**: Added zhHans, zhHant, zhHantHK, French translations with language switcher links.

## [2.5.0] - 2026-05-12

### Rich Message Types

- **appArticles**: Multi-article card with title, summary, and external link per article. Agent tool: `lansenger_send_app_articles`.
- **appCard**: Rich card with head title, body title, div-style content, and optional dynamic status. Agent tool: `lansenger_send_app_card`.
- **Dynamic card update**: In-place status update for previously sent dynamic appCards. Agent tool: `lansenger_update_dynamic_card`.

### Group Chat

- **Group routing**: All send methods route to group endpoint when `chat_type_map` indicates a group chat. Populated from inbound messages.
- **Group ID query**: `lansenger_query_groups` tool for searching groups by name or listing all groups the bot belongs to.

## [2.4.2] - 2026-05-12

### Home Channel

- **Auto-upgrade**: Home channel now auto-upgrades from group to DM when the first DM arrives. Group home channels are less useful for personal bot interactions; DMs provide a more natural primary channel.

## [2.4.1] - 2026-05-12

### Infrastructure

- **send_update_prompt**: Hook for gateway to prompt agent for follow-up updates (e.g. cron-triggered responses).
- **Dynamic agent signature**: Agent response signature includes timestamp and platform metadata for out-of-process delivery.

## [2.4.0] - 2026-05-12

### Bundle & Installation

- **Module-level expand**: Bundle `__init__.py` auto-expands sub-plugins on first import, eliminating the need for a separate expansion step. In-place sub-plugin loading (hyphens → underscores in Python module names).
- **`expand_sub_plugins.py`**: Standalone expansion script for manual or CI-triggered installs.
- **Plugin kind = standalone**: Changed from `bundle` to `standalone` to suppress Hermes "not a valid plugin" warning for the root package.

## [2.3.2] - 2026-05-12

### Bug Fixes

- **PlatformConfig constructor**: Removed invalid `platform` parameter from `PlatformConfig()` call — Hermes core provides the platform object via `register(ctx)`.

## [2.3.1] - 2026-05-12

### Bug Fixes

- **Adapter class loading path**: Fixed module import path for `LansengerAdapter` — was using incorrect package structure.

## [2.3.0] - 2026-05-12

### Plugin Architecture

- **Bundle auto-expand + simplified install**: Single `hermes plugins install` clones the repo and auto-expands both sub-plugins (platform adapter + tools plugin) plus the messaging skill. No manual file copying needed.

## [2.2.0] - 2026-05-11

### Group Chat @Mention

- **Reminder (@mentions)**: `send_text` and `send_text_with_media` support `reminder` field per Lansenger API spec — `reminder_all` (bool) for @all members, `reminder_userIds` (list) for targeted @mentions. Group chat recommended to include @姓名 in text content alongside the reminder field.
- **formatText reminder**: `send_format_text` also supports `reminder` for group @mentions. Newer Lansenger API versions trigger client-side @mention notifications; older versions silently accept the field without effect.

## [2.1.0] - 2026-05-11

### Initial Hermes Plugin Release

- **Plugin mode migration**: Converted from standalone script to Hermes Agent plugin — zero core modification required. Registered via `ctx.register_platform()` in `register(ctx)` entry point.
- **WebSocket inbound**: Long-connection WebSocket for real-time message reception via `/v1/ws/endpoint/create`.
- **HTTP outbound**: Text, formatText (Markdown), image, file, linkCard message types via `/v1/bot/messages/create` (private) and `/v1/messages/group/create` (group).
- **Agent tools**: `lansenger_send_file`, `lansenger_send_image_url`, `lansenger_send_text`, `lansenger_send_markdown` — Agent-callable tools for outbound messaging.
- **lansenger-messaging skill**: SKILL.md teaches the Agent how to choose the right message type based on content characteristics (text vs formatText boundary, media attachment constraints).
- **Token management**: `tenant_access_token` fetch with 2-hour expiry; interactive setup wizard for `hermes setup gateway`.
- **Multi-language READMEs**: English, zhHans, zhHant, zhHantHK, French.