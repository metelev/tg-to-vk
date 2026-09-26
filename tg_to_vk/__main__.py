import argparse
import logging
import tempfile
import time
from pathlib import Path

import requests

from . import MAX_FILE, PermanentError, Store, publish


LOG = logging.getLogger("tg_to_vk")
class ApiError(Exception):
    pass


def load_env(path):
    if not path.exists():
        raise RuntimeError(f"Missing settings file: {path}")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "VK_USER_TOKEN", "VK_GROUP_ID"):
        if not values.get(key):
            raise RuntimeError(f"Missing {key} in {path}")
    return values


class Telegram:
    def __init__(self, token, proxy=None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def call(self, method, **params):
        try:
            response = self.session.post(self.base + method, data=params, timeout=(15, 35))
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ApiError(f"Telegram {method}: {type(exc).__name__}") from None
        if not data.get("ok"):
            raise ApiError(f"Telegram {method}: {data.get('description', 'API error')}")
        return data["result"]

    def download(self, file_id, path):
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > MAX_FILE:
            raise PermanentError("Telegram file exceeds Bot API download limit (20 MB)")
        file_path = info["file_path"]
        if file_path.startswith("/") or ".." in file_path.split("/"):
            raise PermanentError("Invalid Telegram file path")
        try:
            with self.session.get(self.file_base + file_path, stream=True, timeout=(15, 120)) as response:
                response.raise_for_status()
                size = 0
                with open(path, "wb") as target:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_FILE:
                            raise PermanentError("Telegram file exceeds Bot API download limit (20 MB)")
                        target.write(chunk)
        except requests.RequestException as exc:
            raise ApiError(f"Telegram download: {type(exc).__name__}") from None


class VK:
    def __init__(self, token, group_id, telegram, version="5.199", post_token=None):
        self.token = token
        self.post_token = post_token or token
        self.group_id = group_id
        self.version = version
        self.telegram = telegram
        self.session = requests.Session()

    def call(self, method, *, access_token=None, **params):
        try:
            response = self.session.post("https://api.vk.com/method/" + method,
                                         data={**params, "access_token": access_token or self.token, "v": self.version},
                                         timeout=(15, 45))
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ApiError(f"VK {method}: {type(exc).__name__}") from None
        if "error" in data:
            error = data["error"]
            raise ApiError(f"VK {method}: {error.get('error_code')} {error.get('error_msg')}")
        return data["response"]

    def upload(self, kind, file_id):
        if kind not in ("photo", "video"):
            raise PermanentError(f"Unsupported attachment: {kind}")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ("media.jpg" if kind == "photo" else "media.mp4")
            self.telegram.download(file_id, path)
            if kind == "photo":
                for attempt in range(3):
                    server = self.call("photos.getWallUploadServer", group_id=self.group_id)
                    with path.open("rb") as source:
                        uploaded = self._upload(server["upload_url"], "photo", source)
                    if uploaded.get("photo"):
                        break
                    LOG.warning("VK returned an empty photo upload, attempt %s/3", attempt + 1)
                else:
                    raise ApiError("VK photo upload returned empty photo after 3 attempts")
                saved = self.call("photos.saveWallPhoto", group_id=self.group_id,
                                  photo=uploaded["photo"], server=uploaded["server"], hash=uploaded["hash"])
                photo = saved[0]
                return f"photo{photo['owner_id']}_{photo['id']}"
            server = self.call("video.save", group_id=self.group_id, name="Видео из Telegram", wallpost=0)
            with path.open("rb") as source:
                self._upload(server["upload_url"], "video_file", source)
            return f"video{server['owner_id']}_{server['video_id']}"

    def _upload(self, url, field, source):
        if not url.startswith("https://"):
            raise ApiError("VK returned a non-HTTPS upload URL")
        try:
            response = self.session.post(url, files={field: source}, timeout=(15, 180))
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ApiError(f"VK upload: {type(exc).__name__}") from None
        if data.get("error"):
            raise ApiError(f"VK upload: {data['error']}")
        return data

    def post(self, text, attachments, guid):
        result = self.call("wall.post", access_token=self.post_token, owner_id=-self.group_id, from_group=1,
                           message=text, attachments=",".join(attachments), guid=guid)
        return result["post_id"]


def run(config):
    chat_id = int(config["TELEGRAM_CHAT_ID"])
    group_id = abs(int(config["VK_GROUP_ID"]))
    data_path = Path(config.get("DATABASE_PATH", "data/state.sqlite3"))
    data_path.parent.mkdir(parents=True, exist_ok=True)
    tg = Telegram(config["TELEGRAM_BOT_TOKEN"], config.get("TELEGRAM_PROXY_URL"))
    vk = VK(config["VK_USER_TOKEN"], group_id, tg, config.get("VK_API_VERSION", "5.199"),
            config.get("VK_GROUP_TOKEN"))
    store = Store(data_path, chat_id)
    LOG.info("Started for Telegram chat %s and VK group %s", chat_id, group_id)
    try:
        while True:
            try:
                params = {"timeout": 15, "limit": 100, "allowed_updates": '["channel_post"]'}
                if store.offset() is not None:
                    params["offset"] = store.offset()
                updates = tg.call("getUpdates", **params)
                store.ingest(updates, time.time())
                for key, messages in store.ready(time.time()):
                    try:
                        post_id = publish(vk, key, messages)
                        store.done(key, post_id)
                        LOG.info("Published %s as VK post %s", key, post_id)
                    except PermanentError as exc:
                        store.fail(key, exc)
                        LOG.error("Skipped %s: %s", key, exc)
                    except Exception as exc:
                        store.retry(key, exc, time.time())
                        LOG.error("Will retry %s: %s", key, exc)
            except Exception as exc:
                LOG.error("Polling failed: %s", exc)
                time.sleep(10)
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Telegram channel to VK community")
    parser.add_argument("command", choices=("run", "check"), nargs="?", default="run")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_env(args.env)
    if args.command == "check":
        tg = Telegram(config["TELEGRAM_BOT_TOKEN"], config.get("TELEGRAM_PROXY_URL"))
        bot = tg.call("getMe")
        chat = tg.call("getChat", chat_id=config["TELEGRAM_CHAT_ID"])
        vk = VK(config["VK_USER_TOKEN"], abs(int(config["VK_GROUP_ID"])), tg,
                config.get("VK_API_VERSION", "5.199"), config.get("VK_GROUP_TOKEN"))
        vk.call("users.get")
        print(f"Connected to Telegram bot @{bot['username']}, channel {chat['id']} and VK API; no post sent")
    else:
        run(config)


if __name__ == "__main__":
    main()
