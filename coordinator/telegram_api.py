"""Minimal Telegram Bot API client (standard library only)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"

    def _call(self, method, params=None, timeout=35):
        data = urllib.parse.urlencode(params or {}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + method, data=data), timeout=timeout) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as e:
            raise TelegramError(f"{method}: HTTP {e.code} {e.read().decode(errors='ignore')[:200]}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise TelegramError(f"{method}: {e}")
        if not payload.get("ok"):
            raise TelegramError(f"{method}: {payload}")
        return payload["result"]

    def get_me(self):
        return self._call("getMe")

    def delete_webhook(self):
        return self._call("deleteWebhook")

    def get_updates(self, offset=None, timeout=25):
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, timeout=timeout + 10)

    def send_message(self, chat_id, text):
        return self._call("sendMessage", {"chat_id": chat_id, "text": text[:4000]})

    def get_file(self, file_id):
        return self._call("getFile", {"file_id": file_id})

    def download_file(self, file_path, max_bytes=5 * 1024 * 1024):
        """Download a file Telegram knows about (from get_file). Refuses anything bigger than max_bytes."""
        try:
            with urllib.request.urlopen(self.file_base + urllib.parse.quote(file_path, safe="/"), timeout=30) as r:
                data = r.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError) as e:
            raise TelegramError(f"download: {e}")
        if len(data) > max_bytes:
            raise TelegramError("download: file too large")
        return data

    def copy_message(self, chat_id, from_chat_id, message_id):
        """Send a copy of an existing message (used to show the leader a member's photo proof)."""
        return self._call("copyMessage", {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id})

    def poll_forever(self, handler):
        """Long-poll and pass each update to handler(update). Survives network errors."""
        offset = None
        while True:
            try:
                for upd in self.get_updates(offset):
                    offset = upd["update_id"] + 1
                    try:
                        handler(upd)
                    except Exception as e:  # keep the bot alive on a bad update
                        print(f"[bot] error handling update: {e!r}")
            except TelegramError as e:
                print(f"[telegram] {e}; retrying in 5s")
                time.sleep(5)
