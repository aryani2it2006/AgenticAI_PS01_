"""End-to-end: leader registers → project → members → tasks → Telegram (fake) → classifier → dashboard data."""
import http.client
import json
import os
import tempfile
import unittest

from coordinator import db, server
from coordinator.bot import Coordinator


class FakeTG:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), text))

    def to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]


def upd(chat_id, text, username=None):
    return {"update_id": 1, "message": {"text": text, "chat": {"id": chat_id, "type": "private"},
                                        "from": {"username": username}}}


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, None

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"X-Requested-With": "fetch", "Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        return r.status, data


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.configure(os.path.join(tempfile.mkdtemp(), "t.db"))
        db.init()
        cls.tg = FakeTG()
        cls.coord = Coordinator(cls.tg, bot_username="testbot")
        cls.srv = server.start(cls.coord, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]

    def test_full_flow(self):
        A = Client(self.port)
        self.assertEqual(A.call("GET", "/api/me")[0], 401)
        self.assertEqual(A.call("POST", "/api/register", {"name": "Lead", "email": "a@x.com", "password": "secret1"})[0], 200)
        self.assertEqual(A.call("POST", "/api/register", {"name": "Lead", "email": "a@x.com", "password": "secret1"})[0], 409)
        _, pj = A.call("POST", "/api/projects", {"name": "Project One", "deadline": "2030-01-01"})
        pid = pj["id"]
        _, m1 = A.call("POST", f"/api/projects/{pid}/members", {"name": "Kabir"})
        _, m2 = A.call("POST", f"/api/projects/{pid}/members", {"name": "Diya", "telegram_username": "@diya_t"})
        state = A.call("GET", f"/api/projects/{pid}")[1]
        token = next(m for m in state["members"] if m["name"] == "Kabir")["invite_token"]
        self.assertFalse(state["members"][0]["connected"])

        # task assigned before the member joined: no Telegram message, event recorded
        _, t1 = A.call("POST", f"/api/projects/{pid}/tasks", {"title": "Connect the payment API", "member_id": m1["id"], "due_date": "2030-01-05"})
        self.assertFalse(t1["notified"])
        # member joins via invite link
        self.coord.handle_update(upd(555, f"/start {token}"))
        self.assertIn("connected to: Project One", self.tg.to(555)[-1])
        # a second task is assigned after joining -> instant Telegram notification
        _, t2 = A.call("POST", f"/api/projects/{pid}/tasks", {"title": "Write the report", "member_id": m1["id"]})
        self.assertTrue(t2["notified"])
        self.assertIn("New task for you", self.tg.to(555)[-1])

        # leader links Telegram for alerts
        code = A.call("GET", "/api/me")[1]["leader"]["link_code"]
        self.coord.handle_update(upd(900, f"/start lead_{code}"))

        # /task lists both
        self.coord.handle_update(upd(555, "/task"))
        listing = self.tg.to(555)[-1]
        self.assertIn(f"#{t1['id']}", listing)
        self.assertIn(f"#{t2['id']}", listing)

        # blocker (explicit task id): Blocked, not Complete; leader alerted
        self.coord.handle_update(upd(555, f"#{t1['id']} connected, but waiting for test credentials"))
        tasks = {t["id"]: t for t in A.call("GET", f"/api/projects/{pid}")[1]["tasks"]}
        self.assertEqual(tasks[t1["id"]]["status"], "Blocked")
        self.assertEqual(tasks[t1["id"]]["blocker"], "waiting for test credentials")
        self.assertTrue(any("Blocker" in m for m in self.tg.to(900)))

        # other task stays pending; completing one does not complete the other
        self.coord.handle_update(upd(555, f"#{t2['id']} ho gaya"))
        tasks = {t["id"]: t for t in A.call("GET", f"/api/projects/{pid}")[1]["tasks"]}
        self.assertEqual(tasks[t2["id"]]["status"], "Complete")
        self.assertEqual(tasks[t1["id"]]["status"], "Blocked")

        # ambiguity: two open tasks, no id -> bot asks which one (only 1 open now, so add another)
        _, t3 = A.call("POST", f"/api/projects/{pid}/tasks", {"title": "Design slides", "member_id": m1["id"]})
        self.coord.handle_update(upd(555, "done"))
        self.assertIn("Which task", self.tg.to(555)[-1])
        self.coord.handle_update(upd(555, f"task {t3['id']} done"))
        self.coord.handle_update(upd(555, "credentials mil gaye, kaam chal raha hai"))  # only #t1 left open
        tasks = {t["id"]: t for t in A.call("GET", f"/api/projects/{pid}")[1]["tasks"]}
        self.assertEqual((tasks[t1["id"]]["status"], tasks[t1["id"]]["blocker"]), ("In Progress", None))
        self.coord.handle_update(upd(555, "ho gaya"))
        state = A.call("GET", f"/api/projects/{pid}")[1]
        self.assertEqual(state["summary"]["percent_complete"], 100)
        self.assertTrue(any("All tasks" in m for m in self.tg.to(900)))
        kinds = {e["kind"] for e in state["events"]}
        self.assertTrue({"task_created", "joined", "reply", "status_change", "alert"} <= kinds)

    def test_isolation_between_leaders(self):
        A, B = Client(self.port), Client(self.port)
        A.call("POST", "/api/register", {"name": "A", "email": "iso-a@x.com", "password": "secret1"})
        B.call("POST", "/api/register", {"name": "B", "email": "iso-b@x.com", "password": "secret1"})
        pid = A.call("POST", "/api/projects", {"name": "Secret"})[1]["id"]
        tid = A.call("POST", f"/api/projects/{pid}/tasks", {"title": "x"})[1]["id"]
        self.assertEqual(B.call("GET", f"/api/projects/{pid}")[0], 404)
        self.assertEqual(B.call("PATCH", f"/api/tasks/{tid}", {"status": "Complete"})[0], 404)
        self.assertEqual(B.call("DELETE", f"/api/projects/{pid}")[0], 404)
        self.assertEqual(B.call("GET", "/api/projects")[1]["projects"], [])

    def test_followup_and_manual_override(self):
        A = Client(self.port)
        A.call("POST", "/api/register", {"name": "C", "email": "c@x.com", "password": "secret1"})
        pid = A.call("POST", "/api/projects", {"name": "P2"})[1]["id"]
        mid = A.call("POST", f"/api/projects/{pid}/members", {"name": "Meera", "telegram_username": "meera_t"})[1]["id"]
        tid = A.call("POST", f"/api/projects/{pid}/tasks", {"title": "Write report", "member_id": mid})[1]["id"]
        self.assertEqual(A.call("POST", f"/api/projects/{pid}/followup")[1]["sent"], 0)  # not joined yet
        self.coord.handle_update(upd(777, "/start", username="Meera_T"))  # username match, no token
        self.assertEqual(A.call("POST", f"/api/projects/{pid}/followup")[1]["sent"], 1)
        self.assertIn("Write report", self.tg.to(777)[-1])
        A.call("PATCH", f"/api/tasks/{tid}", {"status": "Blocked", "blocker": "waiting for data"})
        t = A.call("GET", f"/api/projects/{pid}")[1]["tasks"][0]
        self.assertEqual((t["status"], t["blocker"]), ("Blocked", "waiting for data"))


if __name__ == "__main__":
    unittest.main()
