import tempfile
import unittest
from pathlib import Path

from tg_to_vk import PermanentError, Store, extract_post, publish


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite3", -10042)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def update(self, update_id, message_id, **extra):
        return {"update_id": update_id, "channel_post": {
            "chat": {"id": -10042}, "message_id": message_id, "date": 100, **extra}}

    def test_unrelated_channel_is_ignored_but_offset_advances(self):
        item = self.update(9, 1, text="private")
        item["channel_post"]["chat"]["id"] = -10099
        self.store.ingest([item], now=100)
        self.assertEqual(self.store.offset(), 10)
        self.assertEqual(self.store.ready(now=200), [])

    def test_album_collects_once_and_survives_reopen(self):
        photo = [{"file_id": "small", "width": 10}, {"file_id": "large", "width": 100}]
        self.store.ingest([self.update(1, 10, media_group_id="album", photo=photo, caption="Hello")], now=100)
        self.store.ingest([self.update(2, 11, media_group_id="album", photo=photo)], now=101)
        self.assertEqual(self.store.ready(now=104), [])
        self.assertEqual(len(self.store.ready(now=110)), 1)
        key, messages = self.store.ready(now=110)[0]
        self.assertEqual(key, "album:album")
        self.assertEqual([m["message_id"] for m in messages], [10, 11])
        self.store.ingest([self.update(2, 11, media_group_id="album", photo=photo)], now=111)
        self.assertEqual(len(self.store.ready(now=120)[0][1]), 2)

    def test_text_and_video_extract(self):
        text, media = extract_post([{"message_id": 1, "text": "Hi"}])
        self.assertEqual((text, media), ("Hi", []))
        text, media = extract_post([{"message_id": 2, "caption": "Clip", "video": {"file_id": "v", "file_size": 10}}])
        self.assertEqual(text, "Clip")
        self.assertEqual(media, [("video", "v", 10)])

    def test_publish_uses_stable_guid_and_all_attachments(self):
        class Fake:
            def __init__(self):
                self.calls = []
            def upload(self, kind, file_id):
                return f"{kind}1_{file_id}"
            def post(self, text, attachments, guid):
                self.calls.append((text, attachments, guid))
                return 17
        vk = Fake()
        messages = [{"message_id": 1, "caption": "Hi", "photo": [{"file_id": "p", "file_size": 5}]},
                    {"message_id": 2, "video": {"file_id": "v", "file_size": 5}}]
        self.assertEqual(publish(vk, "album:x", messages), 17)
        self.assertEqual(vk.calls, [("Hi", ["photo1_p", "video1_v"], "tg-to-vk:album:x")])

    def test_oversized_video_is_rejected_before_upload(self):
        class Fake:
            def upload(self, kind, file_id):
                raise AssertionError("upload must not be called")
        with self.assertRaises(PermanentError):
            publish(Fake(), "message:1", [{"message_id": 1, "video": {
                "file_id": "too-large", "file_size": 21 * 1024 * 1024}}])

    def test_done_job_is_not_republished(self):
        self.store.ingest([self.update(1, 1, text="Hi")], now=100)
        key, _ = self.store.ready(now=100)[0]
        self.store.done(key, 17)
        self.store.ingest([self.update(1, 1, text="Hi")], now=101)
        self.assertEqual(self.store.ready(now=200), [])


if __name__ == "__main__":
    unittest.main()
