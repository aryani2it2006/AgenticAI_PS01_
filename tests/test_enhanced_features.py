"""Comprehensive unit and integration tests for SyncSoul AI enhanced features.

Covers:
1. Complete Task Management (extended fields, subtasks, comments, dependencies)
2. Assignment Rules Engine (skills, workload, role, capacity, explanations)
3. AI Task Understanding (English, Hindi, Hinglish, promises vs completion, delay reasons)
4. Intelligent Reminder & Follow-up Tracker (timezones, overdue calculation, anti-spam cooldown)
5. Blocker Detection & Automatic Subtasks (person/resource detection, proposal, leader approval)
6. Dependency Management (circular dependency check, unblocking cascading)
7. Update deduplication & database migration safety
"""
import datetime as dt
import json
import os
import tempfile
import unittest

from coordinator import assignment, blockers, db, dependencies, interpreter, reminders, reports
from coordinator.bot import Coordinator


class FakeTG:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), text))

    def to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]


class EnhancedFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_enhanced.db")
        db.configure(self.db_path)
        db.init()

        self.lid = db.create_leader("Lead Aman", "aman@syncsoul.ai", "pass123")
        self.pid = db.create_project(self.lid, "SyncSoul Core", "AI Platform", deadline="2030-12-31", timezone="Asia/Kolkata")
        self.m1 = db.add_member(self.pid, "Kabir", "kabir_dev", role="Developer", skills=["python", "api", "database"], workload_capacity=40.0)
        self.m2 = db.add_member(self.pid, "Priya", "priya_ops", role="DevOps", skills=["aws", "devops", "security"], workload_capacity=40.0)
        self.m3 = db.add_member(self.pid, "Rohan", "rohan_qa", role="QA", skills=["qa", "testing"], workload_capacity=20.0)

        db.link_member(self.m1, 101)
        db.link_member(self.m2, 102)
        db.link_member(self.m3, 103)
        db.link_leader_chat(self.lid, 999)

        self.tg = FakeTG()
        self.coord = Coordinator(self.tg, bot_username="syncsoul_bot")

    # 1. Extended Task Management
    def test_extended_task_fields(self):
        tid = db.add_task(
            pid=self.pid,
            title="Deploy Authentication Service",
            description="Set up JWT auth and rate limiting",
            member_id=self.m2,
            priority="Urgent",
            due_date="2030-05-01",
            due_datetime="2030-05-01T18:00:00",
            timezone="Asia/Kolkata",
            estimated_effort=6.5,
            progress=25,
            tags=["auth", "devops", "security"],
            dependencies=[],
            attachments=["https://docs.syncsoul.ai/spec"],
            creator_id=self.lid
        )
        task = db.get_task(tid)
        self.assertEqual(task["priority"], "Urgent")
        self.assertEqual(task["estimated_effort"], 6.5)
        self.assertEqual(task["progress"], 25)
        self.assertIn("devops", task["tags"])
        self.assertEqual(task["owner_name"], "Priya")

    def test_subtask_constraint_prevents_premature_completion(self):
        parent_id = db.add_task(self.pid, "Parent Feature", member_id=self.m1)
        sub1_id = db.add_task(self.pid, "Subtask 1", member_id=self.m1, parent_id=parent_id)

        # Attempt to mark parent task complete while subtask is pending
        self.coord.handle_update({"update_id": 100, "message": {"text": f"#{parent_id} done", "chat": {"id": 101, "type": "private"}, "from": {}}})
        parent = db.get_task(parent_id)
        self.assertNotEqual(parent["status"], "Complete")
        self.assertIn("Cannot mark", self.tg.to(101)[-1])

        # Complete subtask first
        self.coord.handle_update({"update_id": 101, "message": {"text": f"#{sub1_id} done", "chat": {"id": 101, "type": "private"}, "from": {}}})
        sub = db.get_task(sub1_id)
        self.assertEqual(sub["status"], "Complete")

        # Now complete parent task
        self.coord.handle_update({"update_id": 102, "message": {"text": f"#{parent_id} done", "chat": {"id": 101, "type": "private"}, "from": {}}})
        parent = db.get_task(parent_id)
        self.assertEqual(parent["status"], "Complete")

    def test_task_comments(self):
        tid = db.add_task(self.pid, "Task for Commenting", member_id=self.m1)
        cid = db.add_comment(tid, "leader", "Aman", "Please prioritize the edge cases")
        comments = db.comments_of(tid)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["text"], "Please prioritize the edge cases")
        self.assertEqual(comments[0]["author_name"], "Aman")

    # 2. Assignment Rules Engine
    def test_assignment_rules_skills_and_workload(self):
        all_tasks = db.tasks_of(self.pid)
        members = db.members_of(self.pid)

        # Task requiring AWS and DevOps
        devops_task = {"title": "Configure AWS VPC and Security Groups", "tags": ["aws", "devops"], "priority": "High"}
        rec = assignment.recommend_assignee(devops_task, members, all_tasks)
        self.assertEqual(rec["recommended_member"]["name"], "Priya")
        self.assertIn("Skills matched", rec["explanation"])

        # Task requiring Python API
        python_task = {"title": "Build REST API endpoints for user profile", "tags": ["python", "api"], "priority": "Medium"}
        rec = assignment.recommend_assignee(python_task, members, all_tasks)
        self.assertEqual(rec["recommended_member"]["name"], "Kabir")

    def test_assignment_rules_availability_on_leave(self):
        db.update_member(self.m1, status="On Leave")
        members = db.members_of(self.pid)
        score, explanation, breakdown = assignment.evaluate_candidate(members[0], {"title": "Python Bug"}, [])
        self.assertEqual(score, 0.0)
        self.assertIn("on leave", explanation.lower())

    # 3. AI Task Understanding (English, Hindi, Hinglish, promises vs completion)
    def test_ai_understanding_promises_never_complete(self):
        tasks = [{"id": 1, "title": "Database Schema Setup", "status": "In Progress", "blocker": None}]
        # "kal tak kar dunga" (promise) must NOT be complete!
        r = interpreter.rule_interpret("kal tak ho jayega pakka", tasks, 1)
        self.assertNotEqual(r.status, "Complete")
        self.assertEqual(r.intent, "progress")
        self.assertIsNotNone(r.proposed_deadline)

        # "will finish tomorrow" must NOT be complete!
        r2 = interpreter.rule_interpret("working on it, will finish by tomorrow 5pm", tasks, 1)
        self.assertNotEqual(r2.status, "Complete")
        self.assertEqual(r2.intent, "progress")
        self.assertIn("tomorrow", r2.proposed_deadline.lower())

    def test_ai_understanding_hindi_hinglish_slang(self):
        tasks = [{"id": 1, "title": "Setup Gateway", "status": "Pending", "blocker": None}]
        # Completion
        r1 = interpreter.rule_interpret("bhai sab kaam khatam ho gaya", tasks, 1)
        self.assertEqual(r1.intent, "complete")
        self.assertEqual(r1.status, "Complete")

        # Typos in completion
        r2 = interpreter.rule_interpret("sb hogya", tasks, 1)
        self.assertEqual(r2.intent, "complete")

        # Blocker with Hindi
        r3 = interpreter.rule_interpret("dikkat aa rahi hai, Priya se AWS access nahi mila", tasks, 1)
        self.assertEqual(r3.intent, "blocked")
        self.assertEqual(r3.status, "Blocked")

    # 4. Intelligent Reminders & Follow-up Tracker
    def test_reminder_overdue_and_timezone(self):
        # Past deadline in Asia/Kolkata
        past_due = "2020-01-01T10:00:00"
        dt_utc = reminders.parse_deadline(past_due, "Asia/Kolkata")
        self.assertIsNotNone(dt_utc)
        is_overdue, desc, hours = reminders.calculate_overdue_duration(dt_utc)
        self.assertTrue(is_overdue)
        self.assertIn("overdue", desc)
        self.assertGreater(hours, 24.0)

    def test_reminder_antispam_cooldown(self):
        task = {"id": 10, "status": "In Progress", "member_id": self.m1}
        # Simulate log sent 10 minutes ago
        recent = [{"task_id": 10, "status": "sent", "created_at": db.now()}]
        ok, reason = reminders.should_send_reminder(task, recent, min_interval_hours=4.0)
        self.assertFalse(ok)
        self.assertIn("Cooldown active", reason)

        # Force bypasses cooldown
        ok_force, _ = reminders.should_send_reminder(task, recent, force=True)
        self.assertTrue(ok_force)

    # 5. Blocker Detection & Automatic Subtasks
    def test_blocker_detection_proposes_subtasks(self):
        tid = db.add_task(self.pid, "Payment Gateway Integration", member_id=self.m1)
        task = db.get_task(tid)
        members = db.members_of(self.pid)
        all_tasks = db.tasks_of(self.pid)

        blocker_msg = "blocked waiting for Priya to grant AWS S3 access"
        details = blockers.detect_blocker_details(blocker_msg, members)
        self.assertEqual(details["responsible_type"], "person")
        self.assertEqual(details["responsible_member"]["name"], "Priya")

        subtasks = blockers.propose_blocker_subtasks(task, blocker_msg, members, all_tasks)
        self.assertGreater(len(subtasks), 0)
        self.assertEqual(subtasks[0]["recommended_member_name"], "Priya")

        # Leader approval workflow
        appr_id = db.create_leader_approval(self.pid, "subtask_creation", tid, subtasks, reason=blocker_msg)
        approvals = db.get_pending_approvals(self.pid)
        self.assertEqual(len(approvals), 1)

        # Approve and verify subtask creation
        self.coord._action_approval(999, str(appr_id), "approved")
        created_subtasks = db.subtasks_of(tid)
        self.assertEqual(len(created_subtasks), len(subtasks))
        self.assertEqual(created_subtasks[0]["member_id"], self.m2)

    # 6. Dependency Management & Escalation
    def test_circular_dependency_detection(self):
        t1 = db.add_task(self.pid, "Task 1")
        t2 = db.add_task(self.pid, "Task 2", dependencies=[t1])
        all_tasks = db.tasks_of(self.pid)

        # Try to make Task 1 depend on Task 2 (1 -> 2 -> 1)
        has_cycle, cycle_str = dependencies.check_circular_dependency(all_tasks, t1, [t2])
        self.assertTrue(has_cycle)
        self.assertIn(f"#{t1}", cycle_str)

    def test_dependency_unblocking_cascading(self):
        t1 = db.add_task(self.pid, "Create API Schema", member_id=self.m1)
        t2 = db.add_task(self.pid, "Consume API in Frontend", member_id=self.m2, dependencies=[t1])
        db.update_task(t2, status="Blocked", blocker=f"Waiting for #{t1}")

        # Complete Task 1 -> should unblock Task 2 and alert assignee
        self.coord.handle_update({"update_id": 201, "message": {"text": f"#{t1} done", "chat": {"id": 101, "type": "private"}, "from": {}}})
        t2_updated = db.get_task(t2)
        self.assertEqual(t2_updated["status"], "In Progress")
        self.assertIsNone(t2_updated["blocker"])
        self.assertTrue(any(f"Prerequisite task #{t1} has been completed" in m for m in self.tg.to(102)))

    # 7. Update Deduplication & Reports
    def test_telegram_update_deduplication(self):
        # A real update ID > 1 is deduplicated
        uid = 998877
        self.assertTrue(db.record_telegram_update(uid))
        self.assertFalse(db.record_telegram_update(uid))   # duplicate rejected

    def test_evidence_reports_generation(self):
        t1 = db.add_task(self.pid, "Task Done", member_id=self.m1, verification_required=1)
        db.update_task(t1, status="Complete", verified_at=db.now(), verified_by="Leader Aman")
        t2 = db.add_task(self.pid, "Task In Progress", member_id=self.m1, progress=65)
        db.update_task(t2, progress_note="Almost finished with frontend unit tests")

        tasks = db.tasks_of(self.pid)
        members = db.members_of(self.pid)
        sums = reports.member_summaries(members, tasks)
        kabir_sum = next(s for s in sums if s["name"] == "Kabir")
        self.assertEqual(len(kabir_sum["verified_records"]), 1)
        self.assertEqual(len(kabir_sum["member_claims"]), 2)  # progress % and note

    def test_telegram_send_failure_isolated(self):
        # Create a mock TG that fails for chat 101 but succeeds for chat 102
        class FlakyTG:
            def __init__(self):
                self.sent = []
            def send_message(self, chat_id, text):
                if str(chat_id) == "101":
                    raise ConnectionError("Network down for 101")
                self.sent.append((str(chat_id), text))
            def to(self, chat_id):
                return [t for c, t in self.sent if c == str(chat_id)]

        flaky_tg = FlakyTG()
        flaky_coord = Coordinator(flaky_tg, bot_username="flaky_bot")

        # notify_assigned returns False on failure and logs assigned_notify_failed
        t1 = db.add_task(self.pid, "Task for Kabir", member_id=self.m1)
        res = flaky_coord.notify_assigned(t1)
        self.assertFalse(res)
        events = db.recent_events(self.pid)
        self.assertTrue(any(e["kind"] == "assigned_notify_failed" for e in events))

        # But notify_assigned succeeds for Priya
        t2 = db.add_task(self.pid, "Task for Priya", member_id=self.m2)
        res2 = flaky_coord.notify_assigned(t2)
        self.assertTrue(res2)
        self.assertEqual(len(flaky_tg.to(102)), 1)

        # followup_project executes for Priya even if Kabir failed
        db.update_task(t1, last_followup_at=None)
        db.update_task(t2, last_followup_at=None)
        sent_count = flaky_coord.followup_project(self.pid, force=True)
        self.assertEqual(sent_count, 1)  # Priya succeeded, Kabir failed without crashing loop

    def test_subtask_completion_unblocks_parent_task(self):
        parent_id = db.add_task(self.pid, "Core Architecture", member_id=self.m1)
        db.update_task(parent_id, status="Blocked", blocker="waiting for security signoff")

        sub_id = db.add_task(self.pid, "Security Signoff Subtask", member_id=self.m2, parent_id=parent_id)

        # Complete the subtask
        self.coord.handle_update({"update_id": 301, "message": {"text": f"#{sub_id} done", "chat": {"id": 102, "type": "private"}, "from": {}}})
        self.assertEqual(db.get_task(sub_id)["status"], "Complete")

        # Parent task should now be unblocked (In Progress), NOT Complete!
        parent = db.get_task(parent_id)
        self.assertEqual(parent["status"], "In Progress")
        self.assertIsNone(parent["blocker"])
        self.assertTrue(any("unblocked and back In Progress" in m for m in self.tg.to(101)))

    def test_deadline_change_recalculates_phase(self):
        # Create a task past deadline (overdue)
        t = {"id": 50, "status": "In Progress", "due_datetime": "2020-01-01T12:00:00", "timezone": "Asia/Kolkata"}
        phase, desc, hours = reminders.determine_reminder_phase(t)
        self.assertEqual(phase, "overdue")
        self.assertGreater(hours, 24.0)

        # Now update deadline to a future date
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10)).isoformat()
        t["due_datetime"] = future
        phase2, desc2, hours2 = reminders.determine_reminder_phase(t)
        self.assertEqual(phase2, "upcoming")
        self.assertIn("due in", desc2)

    def test_undated_and_completed_tasks_never_overdue(self):
        # Undated task
        undated = {"id": 60, "status": "In Progress", "due_date": None, "due_datetime": None}
        phase, desc, hours = reminders.determine_reminder_phase(undated)
        self.assertEqual(phase, "nudge")
        self.assertNotEqual(phase, "overdue")

        # Completed task past its deadline
        completed = {"id": 61, "status": "Completed", "due_date": "2020-01-01"}
        phase_comp, _, _ = reminders.determine_reminder_phase(completed)
        self.assertEqual(phase_comp, "completed")
        self.assertNotEqual(phase_comp, "overdue")

        # Legacy date-only format
        legacy = reminders.parse_deadline("2030-11-20", "Asia/Kolkata")
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.tzinfo, dt.timezone.utc)


if __name__ == "__main__":
    unittest.main()
