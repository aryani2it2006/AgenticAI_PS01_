"""Photo proof: decision table, model-output parsing, the API request, and the full bot flow (fake Telegram)."""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from coordinator import config, db, vision
from coordinator.bot import Coordinator
from coordinator.vision import PhotoReview, VisionError, VisionUnavailable

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def R(relevance="matches", completion="complete", correct=True, quality=8, confidence=0.9, **kw):
    return PhotoReview(relevance, completion, correct, quality, confidence,
                       kw.get("observed", "a finished report on a laptop"), kw.get("strengths", []),
                       kw.get("issues", []), kw.get("summary", "Report is complete and well formatted."))


class DecideTests(unittest.TestCase):
    def test_table(self):
        cases = [
            (R(), "complete"),
            (R(relevance="unrelated"), "unrelated"),
            (R(relevance="unclear"), "unclear"),
            (R(completion="cannot_tell"), "unclear"),
            (R(confidence=0.3), "unclear"),                       # not sure at all
            (R(confidence=0.6), "unclear"),                       # looks done but not sure enough to auto-complete
            (R(correct=False, issues=["wrong chart"]), "needs_fixes"),
            (R(quality=4), "needs_fixes"),
            (R(correct=None, quality=None), "complete"),          # nothing to judge correctness on, but it matches
            (R(completion="partial"), "partial"),
            (R(completion="not_done"), "not_done"),
            (R(relevance="unrelated", confidence=0.3), "unclear"),
        ]
        for review, expected in cases:
            self.assertEqual(vision.decide(review), expected, review)


class ParseTests(unittest.TestCase):
    def test_parse_ok_with_chatter_and_clamping(self):
        raw = ('Sure! {"relevance":"Matches","completion":"COMPLETE","correct":true,"quality":11,"confidence":1.4,'
               '"observed":"a report","strengths":["tidy"],"issues":[],"summary":"Looks good."} hope that helps')
        r = vision._parse(raw)
        self.assertEqual((r.relevance, r.completion, r.correct, r.quality, r.confidence), ("matches", "complete", True, 10, 1.0))
        self.assertEqual(r.strengths, ["tidy"])

    def test_parse_rejects_garbage(self):
        for raw in ("no json here", '{"relevance":"maybe","completion":"complete"}', '{"relevance":"matches"}'):
            with self.assertRaises(VisionError):
                vision._parse(raw)

    def test_sniff(self):
        self.assertEqual(vision.sniff_media_type(JPEG), "image/jpeg")
        self.assertEqual(vision.sniff_media_type(b"\x89PNG\r\n\x1a\n" + b"x"), "image/png")
        self.assertEqual(vision.sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPxx"), "image/webp")
        self.assertIsNone(vision.sniff_media_type(b"%PDF-1.7"))


class ApiRequestTests(unittest.TestCase):
    def setUp(self):
        self._key = config.ANTHROPIC_API_KEY
        config.ANTHROPIC_API_KEY = "test-key"

    def tearDown(self):
        config.ANTHROPIC_API_KEY = self._key

    def test_request_shape_and_result(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["body"], seen["headers"] = json.loads(req.data), dict(req.header_items())
            out = {"content": [{"type": "text", "text": json.dumps({
                "relevance": "matches", "completion": "partial", "correct": None, "quality": 5, "confidence": 0.8,
                "observed": "half-written report", "strengths": [], "issues": ["conclusion missing"], "summary": "Half done."})}]}
            return io.BytesIO(json.dumps(out).encode())

        task = {"title": "Write the report", "description": "5 pages", "status": "Pending", "due_date": None}
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            r = vision.analyze(JPEG, "image/jpeg", task, "ho gaya")
        content = seen["body"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/jpeg")
        self.assertIn("Write the report", content[1]["text"])
        self.assertIn("ho gaya", content[1]["text"])
        self.assertEqual(seen["headers"]["X-api-key"], "test-key")
        self.assertEqual((r.completion, r.issues), ("partial", ["conclusion missing"]))

    def test_network_failure_becomes_vision_error(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(VisionError):
                vision.analyze(JPEG, "image/jpeg", {"title": "x"}, "")

    def test_no_key(self):
        config.ANTHROPIC_API_KEY = ""
        with self.assertRaises(VisionUnavailable):
            vision.analyze(JPEG, "image/jpeg", {"title": "x"}, "")


class FakeTG:
    def __init__(self):
        self.sent, self.copied, self.blob = [], [], JPEG

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), text))

    def to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]

    def get_file(self, file_id):
        return {"file_path": f"photos/{file_id}.jpg"}

    def download_file(self, path, max_bytes=0):
        return self.blob

    def copy_message(self, chat_id, from_chat_id, message_id):
        self.copied.append((str(chat_id), str(from_chat_id), message_id))


def photo(chat_id, caption=None, n=1, group=None, reply_text=None, doc=False):
    m = {"message_id": 100 + n, "chat": {"id": chat_id, "type": "private"}, "from": {"username": "u"}}
    if doc:
        m["document"] = {"file_id": f"f{n}", "mime_type": "image/jpeg", "file_size": 1000}
    else:
        m["photo"] = [{"file_id": f"s{n}", "width": 90, "height": 90, "file_size": 1000},
                      {"file_id": f"l{n}", "width": 1280, "height": 960, "file_size": 90000}]
    if caption:
        m["caption"] = caption
    if group:
        m["media_group_id"] = group
    if reply_text:
        m["reply_to_message"] = {"text": reply_text}
    return {"update_id": n, "message": m}


def text(chat_id, t):
    return {"update_id": 1, "message": {"text": t, "chat": {"id": chat_id, "type": "private"}, "from": {}}}


class PhotoFlowTests(unittest.TestCase):
    def setUp(self):
        db.configure(os.path.join(tempfile.mkdtemp(), "p.db"))
        db.init()
        self.tg = FakeTG()
        self.verdict = R()
        self.calls = []

        def analyzer(data, media_type, task, caption):
            self.calls.append((media_type, task["id"], caption))
            if isinstance(self.verdict, Exception):
                raise self.verdict
            return self.verdict

        self.coord = Coordinator(self.tg, bot_username="b", analyze_photo=analyzer)
        lid = db.create_leader("Lead", "l@x.com", "secret1")
        db.link_leader_chat(lid, 900)
        self.pid = db.create_project(lid, "Proj")
        self.mid = db.add_member(self.pid, "Kabir")
        db.link_member(self.mid, 555)
        self.t1 = db.add_task(self.pid, "Write the report", "", self.mid)
        self.t2 = db.add_task(self.pid, "Design the poster", "", self.mid)

    def status(self, tid):
        t = db.get_task(tid)
        return t["status"], t["blocker"]

    def test_verified_complete(self):
        self.coord.handle_update(photo(555, f"#{self.t1} done"))
        self.assertEqual(self.status(self.t1)[0], "Complete")
        self.assertEqual(self.status(self.t2)[0], "Pending")      # other task untouched
        self.assertIn("looks complete and correct", self.tg.to(555)[-1])
        self.assertTrue(any("photo proof checked" in m for m in self.tg.to(900)))  # leader alert
        self.assertEqual(self.tg.copied, [("900", "555", 101)])                    # leader sees the photo
        self.assertEqual(self.calls[0][0], "image/jpeg")
        self.assertIn("Report is complete", db.get_task(self.t1)["progress_note"])   # review shown in the dashboard table
        kinds = {e["kind"] for e in db.recent_events(self.pid)}
        self.assertTrue({"reply", "status_change", "alert"} <= kinds)

    def test_unrelated_photo_changes_nothing(self):
        self.verdict = R(relevance="unrelated", observed="a cat on a sofa")
        self.coord.handle_update(photo(555, f"#{self.t1} done"))
        self.assertEqual(self.status(self.t1)[0], "Pending")
        self.assertIn("doesn't look related", self.tg.to(555)[-1])
        self.assertIn("cat on a sofa", self.tg.to(555)[-1])
        self.assertEqual(self.tg.to(900), [])
        self.assertEqual(self.tg.copied, [])
        self.assertIsNone(db.get_task(self.t1)["progress_note"])

    def test_looks_done_but_wrong_stays_open(self):
        self.verdict = R(correct=False, quality=4, issues=["chart axis labels missing", "typo in title"])
        self.coord.handle_update(photo(555, f"#{self.t1} done"))
        self.assertEqual(self.status(self.t1)[0], "In Progress")   # moved off Pending, not Complete
        msg = self.tg.to(555)[-1]
        self.assertIn("not fully correct", msg)
        self.assertIn("chart axis labels missing", msg)
        self.assertEqual(self.tg.to(900), [])

    def test_partial_progress(self):
        self.verdict = R(completion="partial", issues=["conclusion missing"])
        self.coord.handle_update(photo(555, f"#{self.t1}"))
        self.assertEqual(self.status(self.t1)[0], "In Progress")
        self.assertIn("conclusion missing", self.tg.to(555)[-1])

    def test_partial_does_not_clear_a_blocker(self):
        db.update_task(self.t1, status="Blocked", blocker="waiting for data")
        self.verdict = R(completion="partial")
        self.coord.handle_update(photo(555, f"#{self.t1}"))
        self.assertEqual(self.status(self.t1), ("Blocked", "waiting for data"))

    def test_complete_clears_blocker(self):
        db.update_task(self.t1, status="Blocked", blocker="waiting for data")
        self.coord.handle_update(photo(555, f"#{self.t1}"))
        self.assertEqual(self.status(self.t1), ("Complete", None))

    def test_blurry_photo_asks_for_a_better_one(self):
        self.verdict = R(relevance="unclear", confidence=0.4, summary="Too dark to read.")
        self.coord.handle_update(photo(555, f"#{self.t1} done"))
        self.assertEqual(self.status(self.t1)[0], "Pending")
        self.assertIn("clearer photo", self.tg.to(555)[-1])

    def test_vision_unavailable_never_completes(self):
        self.verdict = VisionUnavailable("photo review needs ANTHROPIC_API_KEY")
        self.coord.handle_update(photo(555, f"#{self.t1} done"))
        self.assertEqual(self.status(self.t1)[0], "Pending")
        self.assertIn("passed it to your leader", self.tg.to(555)[-1])
        self.assertTrue(any("automatic checking wasn't available" in m for m in self.tg.to(900)))
        self.assertEqual(len(self.tg.copied), 1)

    def test_vision_crash_is_handled(self):
        self.verdict = VisionError("boom")
        self.coord.handle_update(photo(555, f"#{self.t1} done"))
        self.assertEqual(self.status(self.t1)[0], "Pending")
        self.assertIn("couldn't check", self.tg.to(555)[-1])

    def test_blocker_in_caption_wins_and_photo_is_not_judged(self):
        self.coord.handle_update(photo(555, f"#{self.t1} done but waiting for the logo file"))
        self.assertEqual(self.status(self.t1)[0], "Blocked")
        self.assertEqual(self.calls, [])                              # model never asked
        self.assertTrue(any("Blocker" in m for m in self.tg.to(900)))
        self.assertEqual(len(self.tg.copied), 1)                      # leader still gets the photo

    def test_which_task_when_ambiguous(self):
        self.coord.handle_update(photo(555))
        self.assertIn("Which task is this photo for", self.tg.to(555)[-1])
        self.assertEqual(self.calls, [])

    def test_single_open_task_needs_no_caption(self):
        db.update_task(self.t2, status="Complete")
        self.coord.handle_update(photo(555))
        self.assertEqual(self.status(self.t1)[0], "Complete")

    def test_last_followup_task_is_used_without_caption(self):
        db.set_active_task(self.mid, self.t2)
        self.coord.handle_update(photo(555))
        self.assertEqual(self.status(self.t2)[0], "Complete")
        self.assertEqual(self.status(self.t1)[0], "Pending")

    def test_reply_to_bot_message_picks_task(self):
        self.coord.handle_update(photo(555, reply_text=f"📌 New task for you: #{self.t2} Design the poster"))
        self.assertEqual(self.status(self.t2)[0], "Complete")

    def test_album_shares_the_task_from_the_captioned_photo(self):
        self.verdict = R(completion="partial")
        self.coord.handle_update(photo(555, f"#{self.t2} progress", n=1, group="g1"))
        self.coord.handle_update(photo(555, None, n=2, group="g1"))     # no caption on 2nd photo
        self.assertEqual([c[1] for c in self.calls], [self.t2, self.t2])
        self.assertNotIn("Which task", " ".join(self.tg.to(555)))

    def test_image_sent_as_file(self):
        self.coord.handle_update(photo(555, f"#{self.t1} done", doc=True))
        self.assertEqual(self.status(self.t1)[0], "Complete")

    def test_not_an_image(self):
        self.tg.blob = b"%PDF-1.7 ..."
        self.coord.handle_update(photo(555, f"#{self.t1} done"))
        self.assertEqual(self.calls, [])
        self.assertIn("doesn't look like an image", self.tg.to(555)[-1])

    def test_too_large(self):
        u = photo(555, f"#{self.t1} done")
        u["message"]["photo"] = [{"file_id": "x", "width": 9000, "height": 9000, "file_size": 9 * 1024 * 1024}]
        self.coord.handle_update(u)
        self.assertIn("too large", self.tg.to(555)[-1])
        self.assertEqual(self.calls, [])

    def test_unknown_user_and_no_open_tasks(self):
        self.coord.handle_update(photo(12345, "#1 done"))
        self.assertIn("I don't know you yet", self.tg.to(12345)[-1])
        db.update_task(self.t1, status="Complete")
        db.update_task(self.t2, status="Complete")
        self.coord.handle_update(photo(555, "done"))
        self.assertIn("no open tasks", self.tg.to(555)[-1])

    def test_rate_limit(self):
        self.verdict = R(relevance="unrelated")
        for i in range(8):
            self.coord.handle_update(photo(555, f"#{self.t1}", n=i + 1))
        self.assertEqual(len(self.calls), 8)
        self.coord.handle_update(photo(555, f"#{self.t1}", n=99))
        self.assertEqual(len(self.calls), 8)
        self.assertIn("wait a few minutes", self.tg.to(555)[-1])

    def test_text_replies_still_work_exactly_as_before(self):
        self.coord.handle_update(text(555, f"#{self.t1} done"))
        self.assertEqual(self.status(self.t1)[0], "Complete")      # text path untouched: no photo needed
        self.assertEqual(self.calls, [])
        self.coord.handle_update(text(555, f"#{self.t2} stuck, waiting for the logo"))
        st, blocker = self.status(self.t2)
        self.assertEqual(st, "Blocked")
        self.assertIn("waiting for the logo", blocker)

    def test_non_image_messages_are_still_ignored(self):
        before = list(self.tg.sent)
        self.coord.handle_update({"update_id": 1, "message": {"chat": {"id": 555, "type": "private"},
                                                               "document": {"file_id": "d", "mime_type": "application/pdf"}}})
        self.coord.handle_update({"update_id": 2, "message": {"chat": {"id": 555, "type": "private"}, "sticker": {}}})
        self.assertEqual(self.tg.sent, before)


if __name__ == "__main__":
    unittest.main()
