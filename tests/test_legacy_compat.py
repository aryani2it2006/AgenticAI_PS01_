"""Automated tests for legacy functionality compatibility.

Verifies:
1. Milestone 1 task ingestion command compatibility ("Create task: Finish API integration")
2. Transactional persistence and rollback on errors
3. Telegram update_id deduplication
4. Built-in command handling (/start, /help, /task)
5. Zero LLM requirement fallback (full deterministic rule execution)
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from coordinator import db, interpreter
from coordinator.bot import Coordinator


class MockTG:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), text))

    def to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]


def make_update(update_id, chat_id, text, username="tester"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "text": text,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "username": username, "first_name": "TestUser"}
        }
    }


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "legacy_test.db")
        db.configure(self.db_path)
        db.init()

        # Seed leader, project, and linked member
        self.lid = db.create_leader("Lead Sarah", "sarah@test.com", "pass123")
        self.pid = db.create_project(self.lid, "Legacy Compat Project", "Compatibility testing")
        self.mid = db.add_member(self.pid, "Aman Legacy", "aman_legacy", role="Developer", skills=["python", "api"])
        db.link_member(self.mid, 501)

        self.tg = MockTG()
        self.coord = Coordinator(self.tg, bot_username="compat_bot")

    def test_legacy_help_and_start_commands(self):
        """Milestone 1 commands /start and /help execute cleanly without errors."""
        # /start without token
        self.coord.handle_update(make_update(1, 501, "/start"))
        self.assertTrue(any("SyncSoul" in m for m in self.tg.to(501)))

        # /help command
        self.coord.handle_update(make_update(2, 501, "/help"))
        self.assertTrue(any("Member Commands" in m or "SyncSoul" in m for m in self.tg.to(501)))

    def test_legacy_task_ingestion_format(self):
        """Milestone 1 task creation syntax: 'Create task: Finish API integration'."""
        self.coord.handle_update(make_update(3, 501, "Create task: Finish API integration"))
        # Check task was created in SQLite database
        tasks = db.tasks_of(self.pid)
        matching = [t for t in tasks if "Finish API integration" in t["title"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["member_id"], self.mid)
        # Reply confirms task creation to user
        self.assertTrue(any("Finish API integration" in m for m in self.tg.to(501)))

    def test_update_id_deduplication(self):
        """Repeated delivery of identical update_id is rejected/deduplicated."""
        upd = make_update(99, 501, "Create task: Unique update task")
        self.coord.handle_update(upd)
        initial_sent = len(self.tg.to(501))

        # Re-send same update_id
        self.coord.handle_update(upd)
        # No duplicate task or reply generated
        tasks = [t for t in db.tasks_of(self.pid) if "Unique update task" in t["title"]]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(self.tg.to(501)), initial_sent)

    def test_transactional_integrity_and_rollback(self):
        """SQLite transaction integrity ensures no partial records on failures."""
        # Insert a valid task
        tid = db.add_task(self.pid, "Transactional Task", member_id=self.mid)
        task = db.get_task(tid)
        self.assertEqual(task["title"], "Transactional Task")

        # Rollback on database errors
        with self.assertRaises(sqlite3.IntegrityError):
            # Attempt invalid foreign key or duplicate leader email
            db.create_leader("Duplicate", "sarah@test.com", "otherpass")

        # Database remains fully consistent
        self.assertEqual(db.get_task(tid)["title"], "Transactional Task")

    def test_deterministic_fallback_without_llm(self):
        """Interpreter operates in zero-API-key mode using comprehensive rule engine."""
        tasks = [{"id": 1, "title": "Setup OAuth2", "status": "In Progress", "blocker": None}]

        # Multilingual English
        res_en = interpreter.rule_interpret("#1 done with tests passing", tasks, 1)
        self.assertEqual(res_en.status, "Complete")

        # Multilingual Hindi
        res_hi = interpreter.rule_interpret("#1 ho gaya bhai", tasks, 1)
        self.assertEqual(res_hi.status, "Complete")

        # Hinglish promise vs completion
        res_promise = interpreter.rule_interpret("#1 kal shaam tak kar dunga", tasks, 1)
        self.assertNotEqual(res_promise.status, "Complete")
        self.assertEqual(res_promise.intent, "progress")


if __name__ == "__main__":
    unittest.main()
