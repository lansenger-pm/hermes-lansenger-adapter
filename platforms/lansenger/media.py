"""
Media mixin for LansengerAdapter.
Handles media download, probing (ffprobe/ffmpeg), upload, and sending.
"""

import logging
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import httpx

from gateway.platforms.base import SendResult

from ._constants import API_ENDPOINTS, EMOJI_IDS, REPLY_TYPE_CANCEL, REPLY_TYPE_REPLY

logger = logging.getLogger(__name__)


class MediaMixin:
    """Media handling methods for LansengerAdapter."""

    async def _download_media(self, media_id: str) -> Optional[Tuple[bytes, Optional[str]]]:
        """Download media file by media ID.

        Returns (bytes, filename) tuple or None.  Filename is extracted from
        the Content-Disposition response header when available.
        """
        token = await self._get_app_token()
        if not token:
            return None

        try:
            url = f"{self._api_gateway_url}/v1/medias/{media_id}/fetch"
            params = {"app_token": token}
            response = await self._http_client.get(url, params=params)
            response.raise_for_status()
            filename = None
            cd = response.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                import re
                m = re.search(r'filename[^=]*=([^;]+)', cd)
                if m:
                    filename = m.group(1).strip().strip('"\'')
            return response.content, filename
        except Exception as e:
            logger.error("[Lansenger] Download media error: %s", e)
            return None

    async def _save_media_temp(self, media_bytes: bytes, media_type: str = "file",
                                original_name: Optional[str] = None) -> str:
        """Save media bytes to temp file, return file path.

        Uses original filename when available, preserves extension for
        image type detection.
        """
        import tempfile

        ext_map = {"image": ".jpg", "video": ".mp4", "file": ".dat", "voice": ".amr"}
        suffix = ext_map.get(media_type, ".dat")

        # Use original filename if provided (and safe)
        prefix = f"lansenger_{media_type}_"
        if original_name and '/' not in original_name and '\x00' not in original_name:
            prefix = f"lansenger_{os.path.splitext(original_name)[0]}_"

        # Detect image type from magic bytes
        if media_type == "image" and len(media_bytes) >= 8:
            if media_bytes[:2] == b'\xff\xd8': suffix = ".jpg"
            elif media_bytes[:8] == b'\x89PNG\r\n\x1a\n': suffix = ".png"
            elif media_bytes[:6] in (b'GIF87a', b'GIF89a'): suffix = ".gif"

        fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(media_bytes)
            logger.info("[Lansenger] Saved media to %s", path)
            return path
        except Exception as e:
            logger.error("[Lansenger] Save media error: %s", e)
            try: os.unlink(path)
            except: pass
            return ""

    def _probe_video_size(self, file_path: str) -> tuple:
        """Try to extract width/height from video/image via ffprobe."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
                 str(file_path)],
                capture_output=True, text=True, timeout=5,
            )
            parts = result.stdout.strip().split("x")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return None, None

    def _probe_duration(self, file_path: str) -> Optional[int]:
        """Try to extract duration in seconds via ffprobe."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration", "-of", "csv=s=x:p=0",
                 str(file_path)],
                capture_output=True, text=True, timeout=5,
            )
            val = result.stdout.strip()
            if val and val.replace(".", "", 1).isdigit():
                return max(1, round(float(val)))
        except Exception:
            pass
        return None

    def _extract_video_cover(self, file_path: str) -> Optional[str]:
        """Extract the first frame of a video as a JPEG cover image using ffmpeg.
        
        Returns the temp file path of the cover image, or None if ffmpeg is unavailable.
        """
        try:
            tmp = tempfile.mktemp(suffix=".jpg")
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(file_path),
                 "-vframes", "1", "-f", "image2", tmp],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                return tmp
            try:
                os.unlink(tmp)
            except Exception:
                pass
        except Exception:
            pass
        return None

    async def send_text_with_media(self, chat_id: str, content: str, media_type: int, media_ids: List[str], reminder: dict = None, ref_msg_id: str = None) -> SendResult:
        """Send a text message with media attachment (file/image/video), optionally with @mentions.
        
        Args:
            chat_id: Recipient user ID or chat ID
            content: Text content (caption)
            media_type: 1=video, 2=image, 3=file
            media_ids: List of media IDs from upload_media_file()
            reminder: Optional dict with 'all' (bool) and 'userIds' (list) for @mentions.
                      Only works in group/staff chat; private chat does not support @mentions.
            
        Note: Uses msgType='text' (not formatText) because formatText doesn't support media.
              Markdown is NOT supported when sending media.
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        try:
            is_group = self._is_group_chat(chat_id)

            if is_group:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
            else:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"

            text_data = {
                "content": content,
                "mediaType": media_type,
                "mediaIds": media_ids
            }
            if reminder:
                text_data["reminder"] = reminder

            if is_group:
                payload = {
                    "groupId": chat_id,
                    "msgType": "text",
                    "msgData": {"text": text_data},
                }
                if ref_msg_id:
                    payload["refMsgId"] = ref_msg_id
            else:
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
                return SendResult(success=False, error=data.get("errMsg"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] Text+media message sent to %s (group=%s)", chat_id, is_group)
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except Exception as e:
            logger.error("[Lansenger] Send text+media error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def upload_media_file(self, file_path: str, media_type: int,
                                  width: int = None, height: int = None, duration: int = None) -> Optional[str]:
        """Upload a media file to Lansenger and return mediaId.

        Uses /v1/app/medias/create (4.5.4) — supports larger files
        (image up to 10MB, others up to 20MB, per EMC org config).

        Args:
            file_path: Path to the local file
            media_type: 1=video, 2=image, 3=file, 4=audio
            width: Video/image width (auto-detected via ffprobe if not provided)
            height: Video/image height (auto-detected via ffprobe if not provided)
            duration: Video/audio duration in seconds (auto-detected via ffprobe if not provided)

        Returns:
            mediaId string on success, None on failure
        """
        token = await self._get_app_token()
        if not token:
            logger.error("[Lansenger] No access token for media upload")
            return None

        type_map = {1: "video", 2: "image", 3: "file", 4: "audio"}
        type_str = type_map.get(media_type, "file")

        extra_params = {}
        if type_str in ("video", "image"):
            w, h = (width, height) if width and height else self._probe_video_size(file_path)
            if w:
                extra_params["width"] = w
            if h:
                extra_params["height"] = h
        if type_str in ("video", "audio"):
            d = duration or self._probe_duration(file_path)
            if d:
                extra_params["duration"] = d

        try:
            query = f"type={type_str}&app_token={token}"
            for k, v in extra_params.items():
                query += f"&{k}={v}"
            url = f"{self._api_gateway_url}/v1/app/medias/create?{query}"

            with open(file_path, 'rb') as f:
                file_content = f.read()

            filename = os.path.basename(file_path)
            files = {'media': (filename, file_content)}

            response = await self._http_client.post(url, files=files)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                logger.error("[Lansenger] Upload media error: %s", data.get("errMsg"))
                return None

            media_id = data.get("data", {}).get("mediaId")
            logger.info("[Lansenger] Media uploaded: %s (%s)", filename, media_id)
            return media_id
        except Exception as e:
            logger.error("[Lansenger] Upload media error: %s", e)
            return None

    async def send_file(self, chat_id: str, file_path: str, caption: str = "", media_type: int = 3,
                          width: int = None, height: int = None, duration: int = None) -> SendResult:
        """Send a file/image/video message.
        
        Args:
            chat_id: Recipient user ID
            file_path: Path to the local file
            caption: Optional caption text (plain text, Markdown NOT supported with media)
            media_type: 1=video, 2=image, 3/file, 4=audio (default: 3)
            width: Video/image width (auto-detected via ffprobe if not provided)
            height: Video/image height (auto-detected via ffprobe if not provided)
            duration: Video/audio duration in seconds (auto-detected via ffprobe if not provided)
            
        Returns:
            SendResult with success status
            
        Note: Uses msgType='text' which doesn't support Markdown. For Markdown, send separately.
        """
        if not os.path.isfile(file_path):
            logger.warning("[Lansenger] File not found: %s — skipping", file_path)
            return SendResult(success=False, error=f"File not found: {file_path}")

        media_id = await self.upload_media_file(file_path, media_type,
                                                 width=width, height=height, duration=duration)
        if not media_id:
            return SendResult(success=False, error="Failed to upload file")

        media_ids = [media_id]

        if media_type == 1:
            cover_path = self._extract_video_cover(file_path)
            if cover_path:
                try:
                    cover_id = await self.upload_media_file(cover_path, 2,
                                                             width=width, height=height)
                    if cover_id:
                        media_ids = [media_id, cover_id]
                finally:
                    try:
                        os.unlink(cover_path)
                    except Exception:
                        pass
            else:
                logger.warning("[Lansenger] Could not extract video cover frame — sending with single mediaId")

        return await self.send_text_with_media(chat_id, caption, media_type=media_type, media_ids=media_ids)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None, **kwargs) -> SendResult:
        """Send a local image file.
        
        Args:
            chat_id: Recipient user ID
            image_path: Path to the local image file
            caption: Optional caption text (plain text, Markdown NOT supported with media)
            
        Returns:
            SendResult with success status
            
        Note: Uses media_type=2 (image) for upload and sending.
        """
        return await self.send_file(chat_id, image_path, caption or "", media_type=2)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None, **kwargs) -> SendResult:
        """Send an image by URL.
        
        Note: Downloads image first, then uploads to Lansenger.
        """
        import tempfile
        import httpx
        
        temp_path = None
        try:
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.get(image_url, timeout=30)
                except httpx.ConnectError as e:
                    return SendResult(success=False, error=f"Image URL unreachable: {image_url} — network error: {e}")
                except httpx.TimeoutException:
                    return SendResult(success=False, error=f"Image URL timed out: {image_url}")
                
                if resp.status_code == 404:
                    return SendResult(success=False, error=f"Image URL not found (404): {image_url}")
                if resp.status_code >= 400:
                    return SendResult(success=False, error=f"Image URL returned HTTP {resp.status_code}: {image_url}")
                
                content_type = resp.headers.get("content-type", "")
                if content_type and not content_type.startswith("image/"):
                    return SendResult(success=False, error=f"URL returned non-image content ({content_type}): {image_url}")
                
                image_bytes = resp.content
            
            fd, temp_path = tempfile.mkstemp(suffix='.jpg', prefix='lansenger_image_')
            os.write(fd, image_bytes)
            os.close(fd)
            
            return await self.send_file(chat_id, temp_path, caption or "", media_type=2)
        except Exception as e:
            logger.error("[Lansenger] Send image error: %s", e)
            return SendResult(success=False, error=str(e))
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None, **kwargs) -> SendResult:
        """Send a document file.
        
        Note: Falls back to send_file with media_type=3 (file).
        """
        return await self.send_file(chat_id, file_path, caption or "", media_type=3)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None, **kwargs) -> SendResult:
        """Send a video file natively via the platform API.

        Uses send_file with media_type=1 (video).
        """
        return await self.send_file(chat_id, video_path, caption or "", media_type=1)

    async def send_voice(self, chat_id: str, audio_path: str, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        """Send a voice/audio file.

        Uses send_file with media_type=3 (file) as voice messages
        need a specific format not guaranteed here.
        """
        return await self.send_file(chat_id, audio_path, "", media_type=3)

    async def revoke_message(
        self, 
        message_ids: List[str], 
        chat_id: str = "",
    ) -> SendResult:
        """Revoke previously sent messages.

        Auto-detects chat_type based on chat_id:
        - chat_id == owner_id → "bot" (private chat)
        - otherwise → "group"

        Note: Lansenger displays a fixed system message after revocation.
              Custom sysMsg content/icon is NOT supported. sender_id is not
              required — the API revokes the bot's own messages.
        """
        chat_type = "group" if (chat_id and self._is_group_chat(chat_id)) else "bot"

        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="Failed to get token")
        
        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['message']['revoke']}?app_token={token}"
            payload = {"chatType": chat_type, "messageIds": message_ids}
            
            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            
            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg", "Unknown error"))
            
            logger.info("[Lansenger] Message(s) revoked: %s (chatType=%s)", message_ids, chat_type)
            return SendResult(success=True, message_id=None, raw_response=data)
        except Exception as e:
            logger.error("[Lansenger] Revoke error: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)

    async def reply_message(
        self,
        message_id: str,
        emoji_id: str,
        reply_type: int = REPLY_TYPE_REPLY,
    ) -> SendResult:
        """React to a message with an emoji reply (Lansenger server v4.6.17+).

        reply_type 1 sets the reaction, 2 retracts it.  Cancel still requires
        emoji_id — the server rejects an emojiless cancel with errCode 40060
        ("emojiId is empty"), so callers must always pass the emoji.
        Older servers return errCode 10000 NOT_SUPPORTED.
        """
        if not message_id:
            return SendResult(success=False, error="message_id is required")
        if not emoji_id:
            return SendResult(success=False, error="emoji_id is required (also required when cancelling)")
        if emoji_id not in EMOJI_IDS:
            return SendResult(
                success=False,
                error=f"invalid emoji_id: {emoji_id!r} not in {', '.join(EMOJI_IDS)}",
            )
        if reply_type not in (REPLY_TYPE_REPLY, REPLY_TYPE_CANCEL):
            return SendResult(success=False, error=f"invalid reply_type: {reply_type}")

        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="Failed to get token")

        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['message']['reply_create']}?app_token={token}"
            payload = {"replyType": reply_type, "msgId": message_id, "emojiId": emoji_id}

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg", "Unknown error"))

            logger.info("[Lansenger] Emoji reply %s on %s (replyType=%s)", emoji_id, message_id, reply_type)
            return SendResult(success=True, message_id=message_id, raw_response=data)
        except Exception as e:
            logger.error("[Lansenger] Reply error: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)
