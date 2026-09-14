"""iLink Bot API client for WeChat ClawBot.

Handles QR code login, message polling, and message sending
via the official iLink API (https://ilinkai.weixin.qq.com).
No OpenClaw dependency — talks directly to the iLink relay.
"""

import base64
import hashlib
import json
import logging
import os
import random
import stat
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "1.0.2"
from paths import CONFIG_DIR
TOKEN_FILE = CONFIG_DIR / "token.json"
CURSOR_FILE = CONFIG_DIR / "cursor.dat"
POLL_TIMEOUT_S = 40  # slightly above server's 35s hold
MAX_MEDIA_SEND_SIZE = 100 * 1024 * 1024  # 100 MB sanity cap for uploads

# UploadMediaType (proto enum values used by getuploadurl)
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_FILE = 3
MEDIA_TYPE_VOICE = 4


def _random_uin() -> str:
    """Generate X-WECHAT-UIN header value (random uint32 -> base64)."""
    val = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(val).encode()).decode()


def _auth_headers(bot_token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_uin(),
        "Authorization": f"Bearer {bot_token}",
    }


def _validate_base_url(url: str) -> str:
    """Validate that base_url belongs to official WeChat domains."""
    parsed = urlparse(url)
    allowed_suffixes = (".weixin.qq.com", ".wechat.com")
    if parsed.scheme != "https":
        logger.warning("Rejecting non-HTTPS base_url: %s", url)
        return ILINK_BASE_URL
    if not any(
        parsed.hostname and parsed.hostname.endswith(s) for s in allowed_suffixes
    ):
        logger.warning("Rejecting untrusted base_url: %s", url)
        return ILINK_BASE_URL
    return url


def _save_token(data: dict) -> None:
    """Persist token with restricted file permissions (owner-only read/write)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, stat.S_IRWXU)  # 0700
    except OSError:
        pass
    TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    try:
        os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass


def _load_token() -> dict | None:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load token: %s", e)
    return None


class ILinkClient:
    """Minimal, secure iLink Bot API client."""

    def __init__(self) -> None:
        self.bot_token: str | None = None
        self.base_url: str = ILINK_BASE_URL
        self._cursor: str = ""  # getupdates cursor
        self._bad_rets = 0  # consecutive session-rejected polls
        self._client = httpx.Client(
            timeout=POLL_TIMEOUT_S + 5,
            trust_env=False,  # system proxy (e.g. 127.0.0.1:7897) hangs long-polls
        )
        self._try_restore_token()
        self._cursor = self._load_cursor()
        self._typing_ticket: str = ""

    def _try_restore_token(self) -> None:
        data = _load_token()
        if data:
            self.bot_token = data.get("bot_token")
            self.base_url = _validate_base_url(data.get("base_url", ILINK_BASE_URL))
            logger.info("Restored saved token.")

    def _maybe_reload_token(self) -> bool:
        """Pick up a re-login another process wrote to token.json.

        Returns True if the in-memory token changed.
        """
        data = _load_token()
        if data and data.get("bot_token") != self.bot_token:
            self.bot_token = data.get("bot_token")
            self.base_url = _validate_base_url(
                data.get("base_url", ILINK_BASE_URL)
            )
            logger.info("Token changed on disk — reloaded.")
            return True
        return False

    def _load_cursor(self) -> str:
        try:
            if CURSOR_FILE.exists():
                return CURSOR_FILE.read_text().strip()
        except OSError as e:
            logger.warning("Failed to load cursor: %s", e)
        return ""

    def _save_cursor(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CURSOR_FILE.write_text(self._cursor)
        try:
            os.chmod(CURSOR_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @property
    def is_logged_in(self) -> bool:
        return self.bot_token is not None

    def _headers(self) -> dict[str, str]:
        if not self.bot_token:
            raise RuntimeError("Not logged in. Call login() first.")
        return _auth_headers(self.bot_token)

    # -- Login Flow ----------------------------------------------------------

    def login(self) -> None:
        """Interactive QR code login. Displays QR in terminal."""
        # Step 1: Get QR code
        resp = self._client.get(
            f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
        )
        resp.raise_for_status()
        data = resp.json()

        qrcode_id = data["qrcode"]
        qr_content = data.get("qrcode_img_content", "")

        # Display QR code in terminal
        self._display_qr(qrcode_id, qr_content)

        # Step 2: Poll for scan confirmation
        print("\nWaiting for WeChat scan...")
        max_wait = 300  # 5 minutes
        start = time.monotonic()
        while time.monotonic() - start < max_wait:
            resp = self._client.get(
                f"{ILINK_BASE_URL}/ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode_id},
                headers={"iLink-App-ClientVersion": "1"},
            )
            resp.raise_for_status()
            status_data = resp.json()

            status = status_data.get("status", "")
            if status == "confirmed":
                self.bot_token = status_data["bot_token"]
                self.base_url = _validate_base_url(
                    status_data.get("baseurl", ILINK_BASE_URL)
                )
                _save_token(
                    {
                        "bot_token": self.bot_token,
                        "base_url": self.base_url,
                        "login_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                )
                print("Login successful!")
                return
            elif status == "expired":
                raise TimeoutError("QR code expired. Please try again.")

            time.sleep(2)
        raise TimeoutError("QR code login timed out after 5 minutes.")

    def _display_qr(self, qrcode_id: str, qr_content: str) -> None:
        """Display QR code for WeChat scanning.

        qr_content is the text/URL to encode as a QR code (NOT base64 image).
        qrcode_id is only used for polling status.
        """
        content = qr_content or qrcode_id

        # Render QR code in terminal
        try:
            import qrcode as qr_lib

            qr = qr_lib.QRCode(border=1)
            qr.add_data(content)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            print("\nScan the QR code above with WeChat.")
        except ImportError:
            pass

        # Also save as PNG for manual scanning
        try:
            import qrcode as qr_lib

            qr_path = CONFIG_DIR / "qr.png"
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            img = qr_lib.make(content)
            img.save(str(qr_path))
            print(f"QR image also saved to: {qr_path}")
        except Exception:
            print(f"QR content: {content}")

    def logout(self) -> None:
        """Clear saved credentials."""
        self.bot_token = None
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        logger.info("Logged out.")

    # -- Message Polling -----------------------------------------------------

    def poll_messages(self) -> list[dict]:
        """Long-poll for new messages. Returns list of message dicts.

        Each message contains:
        - from_user_id: sender ID
        - context_token: required for replies
        - item_list: list of content items (text, image, etc.)
        """
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/getupdates",
            headers=self._headers(),
            json={
                "get_updates_buf": self._cursor,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            timeout=POLL_TIMEOUT_S + 5,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.warning("Non-JSON response from getupdates")
            return []

        ret = data.get("ret")
        if ret is not None and ret != 0:
            self._bad_rets += 1
            # Stale updates cursor from a dead session is rejected — drop
            # it and let the loop retry from the live buffer.
            if self._cursor:
                logger.info("getupdates rejected — resetting stale cursor.")
                self._cursor = ""
                self._save_cursor()
                self._bad_rets = 0
                return []
            # Next suspect: a re-login elsewhere refreshed token.json.
            if self._maybe_reload_token():
                self._bad_rets = 0
                return []
            logger.warning(
                "getupdates: ret=%s errmsg=%s (%d/3)",
                ret, data.get("errmsg", ""), self._bad_rets,
            )
            if self._bad_rets >= 3:
                self._bad_rets = 0
                raise RuntimeError(
                    f"getupdates rejected (ret={ret}) — session invalid"
                )
            time.sleep(2)
            return []
        self._bad_rets = 0

        # Update cursor for next poll
        new_cursor = data.get("get_updates_buf", "")
        if new_cursor:
            self._cursor = new_cursor
            self._save_cursor()

        all_msgs = data.get("msgs", [])
        if all_msgs:
            logger.info("Received %d raw messages", len(all_msgs))
            for m in all_msgs:
                for item in m.get("item_list", []):
                    it = item.get("type")
                    keys = [k for k in item.keys() if k != "type"]
                    logger.info(
                        "  msg_type=%s item_type=%s keys=%s",
                        m.get("message_type"),
                        it,
                        keys,
                    )
                    # Dump non-text items fully for debugging
                    if it != 1:
                        import copy

                        debug_item = copy.deepcopy(item)
                        # Truncate long values
                        for k, v in debug_item.items():
                            if isinstance(v, str) and len(v) > 100:
                                debug_item[k] = v[:100] + "..."
                            elif isinstance(v, dict):
                                for k2, v2 in v.items():
                                    if isinstance(v2, str) and len(v2) > 100:
                                        v[k2] = v2[:100] + "..."
                        logger.info("  FULL ITEM: %s", debug_item)

        # Filter for inbound user messages (message_type=1)
        messages = []
        for msg in all_msgs:
            mt = msg.get("message_type")
            if mt == 1:
                messages.append(msg)
            else:
                logger.debug(
                    "Skipping message_type=%s from=%s",
                    mt,
                    msg.get("from_user_id", "?")[:16],
                )

        return messages

    # -- Message Sending -----------------------------------------------------

    def send_text(self, to_user_id: str, context_token: str, text: str) -> bool:
        """Send a text message back to the user."""
        chunks = self._split_text(text, max_len=4000)
        for chunk in chunks:
            client_id = f"kami:{uuid.uuid4().hex[:16]}"
            payload = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [
                        {
                            "type": 1,
                            "text_item": {"text": chunk},
                        }
                    ],
                },
                "base_info": {"channel_version": CHANNEL_VERSION},
            }
            resp = self._client.post(
                f"{self.base_url}/ilink/bot/sendmessage",
                headers=self._headers(),
                json=payload,
            )
            try:
                resp_data = resp.json()
            except (json.JSONDecodeError, ValueError):
                logger.error(
                    "Non-JSON response from sendmessage: status=%d", resp.status_code
                )
                return False
            ret = resp_data.get("ret")
            if resp.status_code != 200 or (ret is not None and ret != 0):
                logger.error(
                    "Failed to send message: ret=%s, errmsg=%s",
                    resp_data.get("ret"),
                    resp_data.get("errmsg", resp.text[:200]),
                )
                return False
        return True

    # -- Media Upload / Send ---------------------------------------------------

    @staticmethod
    def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
        """AES-128-ECB encrypt with PKCS7 padding (WeChat CDN convention)."""
        pad = 16 - (len(data) % 16)
        padded = data + bytes([pad]) * pad
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def upload_media(self, to_user_id: str, path: Path, media_type: int) -> tuple[str, bytes]:
        """Upload a local file to the WeChat CDN (AES-128-ECB encrypted).

        Returns (encrypt_query_param, aeskey) for building the message item.
        """
        raw = path.read_bytes()
        rawsize = len(raw)
        if rawsize == 0:
            raise ValueError(f"Cannot send empty file: {path}")
        if rawsize > MAX_MEDIA_SEND_SIZE:
            raise ValueError(
                f"File too large ({rawsize} bytes, limit {MAX_MEDIA_SEND_SIZE})"
            )
        filesize = ((rawsize + 1) + 15) // 16 * 16  # PKCS7 padded size
        aeskey = os.urandom(16)
        filekey = os.urandom(16).hex()
        rawfilemd5 = hashlib.md5(raw).hexdigest()

        resp = self._client.post(
            f"{self.base_url}/ilink/bot/getuploadurl",
            headers=self._headers(),
            json={
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": filesize,
                "no_need_thumb": True,
                "aeskey": aeskey.hex(),
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        ret = data.get("ret")
        if ret is not None and ret != 0:
            raise RuntimeError(f"getuploadurl ret={ret}: {data.get('errmsg', '')}")

        upload_url = (data.get("upload_full_url") or "").strip()
        if not upload_url:
            upload_param = data.get("upload_param", "")
            if not upload_param:
                raise RuntimeError("getuploadurl returned no upload URL")
            upload_url = (
                f"{CDN_BASE_URL}/upload"
                f"?encrypted_query_param={quote(upload_param)}&filekey={quote(filekey)}"
            )

        ciphertext = self._aes_ecb_encrypt(raw, aeskey)
        up = self._client.post(
            upload_url,
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=300,
        )
        if up.status_code != 200:
            err = up.headers.get("x-error-message", up.text[:200])
            raise RuntimeError(f"CDN upload failed ({up.status_code}): {err}")
        param = up.headers.get("x-encrypted-param")
        if not param:
            raise RuntimeError("CDN upload response missing x-encrypted-param")
        logger.info("Uploaded %s (%d bytes) to CDN", path.name, rawsize)
        return param, aeskey

    def _send_media_item(
        self,
        to_user_id: str,
        context_token: str,
        item: dict,
    ) -> bool:
        client_id = f"kami:{uuid.uuid4().hex[:16]}"
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [item],
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/sendmessage",
            headers=self._headers(),
            json=payload,
        )
        try:
            resp_data = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.error("Non-JSON response from sendmessage: %d", resp.status_code)
            return False
        ret = resp_data.get("ret")
        if resp.status_code != 200 or (ret is not None and ret != 0):
            logger.error(
                "Failed to send media: ret=%s, errmsg=%s",
                ret,
                resp_data.get("errmsg", resp.text[:200]),
            )
            return False
        return True

    def send_file(
        self,
        to_user_id: str,
        context_token: str,
        path: Path,
        caption: str = "",
    ) -> bool:
        """Send a local file as a WeChat file message."""
        path = Path(path)
        if not path.is_file():
            logger.error("send_file: not a file: %s", path)
            return False
        try:
            param, aeskey = self.upload_media(to_user_id, path, MEDIA_TYPE_FILE)
        except Exception as e:
            logger.error("send_file upload failed: %s", e)
            return False
        if caption:
            self.send_text(to_user_id, context_token, caption)
        item = {
            "type": 4,
            "file_item": {
                "media": {
                    "encrypt_query_param": param,
                    "aes_key": base64.b64encode(aeskey).decode(),
                    "encrypt_type": 1,
                },
                "file_name": path.name,
                "len": str(path.stat().st_size),
            },
        }
        return self._send_media_item(to_user_id, context_token, item)

    def send_image(
        self,
        to_user_id: str,
        context_token: str,
        path: Path,
        caption: str = "",
    ) -> bool:
        """Send a local image as a WeChat image message."""
        path = Path(path)
        if not path.is_file():
            logger.error("send_image: not a file: %s", path)
            return False
        try:
            param, aeskey = self.upload_media(to_user_id, path, MEDIA_TYPE_IMAGE)
        except Exception as e:
            logger.error("send_image upload failed: %s", e)
            return False
        if caption:
            self.send_text(to_user_id, context_token, caption)
        item = {
            "type": 2,
            "image_item": {
                "media": {
                    "encrypt_query_param": param,
                    "aes_key": base64.b64encode(aeskey).decode(),
                    "encrypt_type": 1,
                },
                "mid_size": ((path.stat().st_size + 1) + 15) // 16 * 16,
            },
        }
        return self._send_media_item(to_user_id, context_token, item)

    def send_typing(self, to_user_id: str, context_token: str) -> None:
        """Send typing indicator to show bot is processing."""
        try:
            if not self._typing_ticket:
                config_resp = self._client.post(
                    f"{self.base_url}/ilink/bot/getconfig",
                    headers=self._headers(),
                    json={"to_user_id": to_user_id},
                    timeout=5,
                )
                if config_resp.status_code == 200:
                    data = config_resp.json()
                    self._typing_ticket = data.get("typing_ticket", "")
                    if not self._typing_ticket:
                        # API doesn't support typing, stop trying
                        self._typing_ticket = "__disabled__"
            if self._typing_ticket and self._typing_ticket != "__disabled__":
                self._client.post(
                    f"{self.base_url}/ilink/bot/sendtyping",
                    headers=self._headers(),
                    json={
                        "to_user_id": to_user_id,
                        "context_token": context_token,
                        "typing_ticket": self._typing_ticket,
                    },
                    timeout=5,
                )
        except Exception:
            pass

    @staticmethod
    def _split_text(text: str, max_len: int = 4000) -> list[str]:
        """Split long text into chunks at line boundaries."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Find last newline within limit
            split_at = text.rfind("\n", 0, max_len)
            if split_at <= 0:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks

    @staticmethod
    def extract_text(message: dict) -> str:
        """Extract plain text content from a message."""
        parts = []
        for item in message.get("item_list", []):
            if item.get("type") == 1:
                text_item = item.get("text_item", {})
                parts.append(text_item.get("text", ""))
        return "\n".join(parts)

    @staticmethod
    def extract_media(message: dict) -> list[dict]:
        """Extract media items (images, files, voice) from a message."""
        media = []
        for item in message.get("item_list", []):
            item_type = item.get("type")
            if item_type == 2:  # Image
                img = item.get("image_item", {})
                # iLink API uses 'url' for image URL and 'aeskey' (no underscore)
                # Also check media.aes_key (base64 encoded) and media.encrypt_query_param
                img_media = img.get("media", {})
                cdn_url = img.get("url", img.get("cdn_img_url", img.get("cdn_url", "")))
                # aes_key: try media.aes_key (base64 of hex key), then top-level aeskey
                aes_key = (
                    img_media.get("aes_key", "")
                    or img.get("aeskey", "")
                    or img.get("aes_key", "")
                )
                encrypt_param = img_media.get("encrypt_query_param", "")
                media.append(
                    {
                        "type": "image",
                        "cdn_url": cdn_url,
                        "aes_key": aes_key,
                        "encrypt_query_param": encrypt_param,
                        "width": img.get("thumb_width", img.get("width", 0)),
                        "height": img.get("thumb_height", img.get("height", 0)),
                        "hd_size": img.get("hd_size", 0),
                    }
                )
            elif item_type == 3:  # Voice
                voice = item.get("voice_item", {})
                media.append(
                    {
                        "type": "voice",
                        "text": voice.get("text", ""),  # transcribed text
                    }
                )
            elif item_type == 4:  # File
                file_item = item.get("file_item", {})
                f_media = file_item.get("media", {})
                media.append(
                    {
                        "type": "file",
                        # Inbound file_item carries media.encrypt_query_param /
                        # media.aes_key / media.full_url — same shape as images.
                        "cdn_url": f_media.get("full_url", file_item.get("cdn_url", "")),
                        "aes_key": f_media.get("aes_key", ""),
                        "encrypt_query_param": f_media.get("encrypt_query_param", ""),
                        "filename": file_item.get("file_name", "unknown"),
                        "size": int(file_item.get("len", "0") or 0),
                        "md5": file_item.get("md5", ""),
                    }
                )
        return media

    def download_media(
        self,
        cdn_url: str,
        aes_key: str,
        save_path: Path,
        encrypt_query_param: str = "",
    ) -> bool:
        """Download and decrypt media file from WeChat CDN (AES-128-ECB).

        Args:
            cdn_url: CDN URL or WeChat internal media reference.
            aes_key: AES key (hex string or base64-encoded hex string).
            save_path: Path to save the decrypted file.
            encrypt_query_param: Encrypted query parameter for CDN download.
        """
        if not cdn_url and not encrypt_query_param:
            return False
        try:
            # Build download URL
            if encrypt_query_param:
                download_url = (
                    f"https://novac2c.cdn.weixin.qq.com/c2c/download"
                    f"?encrypted_query_param={encrypt_query_param}"
                )
            elif cdn_url.startswith("http"):
                download_url = cdn_url
            else:
                download_url = (
                    f"https://novac2c.cdn.weixin.qq.com/c2c/download?fileid={cdn_url}"
                )

            logger.info("Downloading media from: %s...", download_url[:80])
            resp = self._client.get(download_url, timeout=30)
            resp.raise_for_status()

            encrypted = resp.content
            if not encrypted:
                logger.warning("Empty response from CDN")
                return False

            # Determine AES key
            # aes_key might be: hex string, base64(hex string), or empty
            key_bytes = None
            if aes_key:
                try:
                    # Try base64 decode first (media.aes_key is base64 of hex)
                    decoded = base64.b64decode(aes_key)
                    hex_str = decoded.decode("ascii")
                    key_bytes = bytes.fromhex(hex_str)
                except Exception:
                    try:
                        # Try as raw hex string
                        key_bytes = bytes.fromhex(aes_key)
                    except ValueError:
                        try:
                            # Try as raw base64
                            key_bytes = base64.b64decode(aes_key)
                        except Exception:
                            logger.warning("Cannot parse AES key, saving raw")

            if not key_bytes or len(key_bytes) != 16:
                # No valid key, save raw data
                save_path.write_bytes(encrypted)
                return True

            if len(encrypted) % 16 != 0:
                logger.warning("Data not 16-byte aligned, saving raw")
                save_path.write_bytes(encrypted)
                return True

            cipher = Cipher(algorithms.AES(key_bytes), modes.ECB())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted) + decryptor.finalize()

            # Remove PKCS7 padding
            if decrypted:
                pad_len = decrypted[-1]
                if 1 <= pad_len <= 16 and all(
                    b == pad_len for b in decrypted[-pad_len:]
                ):
                    decrypted = decrypted[:-pad_len]

            save_path.write_bytes(decrypted)
            return True
        except Exception as e:
            logger.error("Failed to download media: %s", e)
            return False

    def close(self) -> None:
        self._client.close()
