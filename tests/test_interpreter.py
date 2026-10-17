import unittest

from coordinator.interpreter import rule_interpret

TASKS = [{"id": 1, "title": "Connect the payment API", "status": "Pending", "blocker": None},
         {"id": 2, "title": "Write project report", "status": "Pending", "blocker": None}]

# (reply, intent, status, blocker substring)
CASES = [
    ("connected, but waiting for test credentials", "blocked", "Blocked", "waiting for test credentials"),
    ("done!", "complete", "Complete", None),
    ("Finished the integration and pushed to GitHub", "complete", "Complete", None),
    ("I'm about 60% done", "progress", "In Progress", None),
    ("almost done", "progress", "In Progress", None),
    ("started working on it", "progress", "In Progress", None),
    ("haven't started yet", "progress", "Pending", None),
    ("not done yet, will finish by tomorrow", "progress", "In Progress", None),
    ("stuck on the login bug, can someone help", "blocked", "Blocked", "stuck on the login bug"),
    ("done but the tests are failing", "blocked", "Blocked", "tests are failing"),
    ("No blockers, going well", "progress", "In Progress", None),
    ("got the credentials, continuing now", "unblocked", "In Progress", None),
    ("fixed the error, all done now", "complete", "Complete", None),
    ("I'll do it tomorrow", "progress", None, None),
    ("ok", "unclear", None, None),
    ("when is the deadline?", "question", None, None),
    ("100%", "complete", "Complete", None),
    # Hinglish
    ("sab ho gaya", "complete", "Complete", None),
    ("kaam khatam ho gaya", "complete", "Complete", None),
    ("kaam chal raha hai", "progress", "In Progress", None),
    ("abhi pura nahi hua", "progress", "In Progress", None),
    ("shuru nahi kiya abhi", "progress", "Pending", None),
    ("API connect ho gaya par credentials nahi mile", "blocked", "Blocked", "credentials nahi mile"),
    ("access nahi mil raha, wait kar raha hu", "blocked", "Blocked", None),
    ("credentials mil gaye, ab kaam chal raha hai", "unblocked", "In Progress", None),
    ("deadline kab hai?", "question", None, None),
]


class InterpreterTests(unittest.TestCase):
    def test_cases(self):
        for text, intent, status, blocker in CASES:
            with self.subTest(text=text):
                r = rule_interpret(text, TASKS, 1)
                self.assertEqual(r.intent, intent)
                self.assertEqual(r.status, status)
                if blocker:
                    self.assertIn(blocker.lower(), (r.blocker or "").lower())
                elif status != "Blocked":
                    self.assertIsNone(r.blocker)

    def test_blocker_never_complete(self):
        r = rule_interpret("connected, but waiting for test credentials", TASKS, 1)
        self.assertNotEqual(r.status, "Complete")
        self.assertEqual(r.note.lower(), "connected")


if __name__ == "__main__":
    unittest.main()
