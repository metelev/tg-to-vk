"""Telegram channel to VK community cross-posting."""

import json
import sqlite3


class Store:
    def __init__(self, path, chat_id):
        self.chat_id = chat_id
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, messages TEXT NOT NULL, due REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            post_id INTEGER, error TEXT)""")
        self.db.commit()

    def close(self):
        self.db.close()

    def offset(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return row[0] if row else None

    def ingest(self, updates, now):
        with self.db:
            for update in updates:
                update_id = update["update_id"]
                if self.offset() is not None and update_id < self.offset():
                    continue
                message = update.get("channel_post")
                if message and message.get("chat", {}).get("id") == self.chat_id:
                    if message.get("text") or message.get("caption") or message.get("photo") or message.get("video"):
                        key = ("album:" + message["media_group_id"] if message.get("media_group_id")
                               else "message:" + str(message["message_id"]))
                        row = self.db.execute("SELECT messages, status FROM jobs WHERE key=?", (key,)).fetchone()
                        if row is None:
                            self.db.execute("INSERT INTO jobs(key,messages,due) VALUES(?,?,?)",
                                            (key, json.dumps([message]), now + (5 if message.get("media_group_id") else 0)))
                        elif row[1] == "pending":
                            messages = json.loads(row[0])
                            if not any(m["message_id"] == message["message_id"] for m in messages):
                                messages.append(message)
                                self.db.execute("UPDATE jobs SET messages=?, due=? WHERE key=?",
                                                (json.dumps(messages), now + 5, key))
                self.db.execute("INSERT INTO meta(key,value) VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (update_id + 1,))

    def ready(self, now):
        rows = self.db.execute("SELECT key,messages FROM jobs WHERE status='pending' AND due<=? ORDER BY due,key", (now,)).fetchall()
        return [(key, sorted(json.loads(raw), key=lambda m: m["message_id"])) for key, raw in rows]

    def done(self, key, post_id):
        with self.db:
            self.db.execute("UPDATE jobs SET status='done',post_id=?,error=NULL WHERE key=?", (post_id, key))

    def retry(self, key, error, now):
        with self.db:
            self.db.execute("UPDATE jobs SET attempts=attempts+1,due=?,error=? WHERE key=?",
                            (now + 60, str(error)[:300], key))

    def fail(self, key, error):
        with self.db:
            self.db.execute("UPDATE jobs SET status='failed',error=? WHERE key=?", (str(error)[:300], key))


def extract_post(messages):
    text = next((m.get("caption") or m.get("text") for m in messages if m.get("caption") or m.get("text")), "")
    media = []
    for m in messages:
        if m.get("photo"):
            photo = max(m["photo"], key=lambda p: p.get("file_size", p.get("width", 0) * p.get("height", 0)))
            media.append(("photo", photo["file_id"], photo.get("file_size", 0)))
        elif m.get("video"):
            video = m["video"]
            media.append(("video", video["file_id"], video.get("file_size", 0)))
    return text, media


def publish(vk, key, messages):
    text, media = extract_post(messages)
    attachments = [vk.upload(kind, file_id) for kind, file_id, _ in media]
    return vk.post(text, attachments, "tg-to-vk:" + key)
