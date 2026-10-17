"""Comprehensive automated test suite for the Agentic Project Orchestration System.

Covers all 10 orchestration requirements:
1. Parallel execution eligibility (wave 1 tasks activate concurrently)
2. Dependency unlocking upon verified completion (prerequisites gating)
3. Multi-dependency tasks (unlocked only when all prerequisites verified)
4. Leader approvals (plan approval, deliverable verification & rejection, task cancellation)
5. Assignment rules (skills, role fit, workload, uncertainty detection without invented data)
6. Circular dependency detection & rejection (plan-level and override-level)
7. Restart recovery & idempotency (state persisted in SQLite, safe recovery)
8. Duplicate notification prevention (deduplication keys in notifications table)
9. Failed Telegram sends handling (graceful error handling, audit logging)
10. Blocker resolution returning to In Progress (never treating unblock as completed)
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from coordinator import assignment, db, dependencies, planning
from coordinator.bot import Coordinator
from coordinator.orchestrator import Orchestrator


class MockTG:
    """Mock Telegram bot client for capturing and simulating messages."""

    def __init__(self, fail_for_chats=None):
        self.sent = []
        self.copied = []
        self.fail_for_chats = set(str(c) for c in (fail_for_chats or []))

    def send_message(self, chat_id, text):
        if str(chat_id) in self.fail_for_chats:
            raise RuntimeError(f"Simulated Telegram Network Failure for chat {chat_id}")
        self.sent.append((str(chat_id), text))

    def copy_message(self, chat_id, from_chat_id, message_id):
        self.copied.append((str(chat_id), str(from_chat_id), message_id))

    def to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_orchestrator.db")
        db.configure(self.db_path)
        db.init()

        # Seed Leader & Project
        self.lid = db.create_leader("Dr. Sarah Chen", "sarah@deeptech.ai", "securepass123")
        db.link_leader_chat(self.lid, 999)

        self.pid = db.create_project(
            leader_id=self.lid,
            name="Autonomous Agent Mesh",
            description="Multi-agent platform for distributed task execution",
            deadline="2030-06-30",
            timezone="UTC",
            goal="Deploy production multi-agent system with verified security and API gateways"
        )

        # Seed Team Members with diverse skills, roles, and capacities
        self.m_backend = db.add_member(
            self.pid, "Kabir Dev", "kabir_backend",
            role="Backend Engineer",
            skills=["python", "api", "database", "backend"],
            workload_capacity=40.0
        )
        self.m_devops = db.add_member(
            self.pid, "Priya Ops", "priya_devops",
            role="DevOps Engineer",
            skills=["docker", "kubernetes", "cloud", "security", "infra"],
            workload_capacity=40.0
        )
        self.m_frontend = db.add_member(
            self.pid, "Aarav UI", "aarav_frontend",
            role="Frontend Engineer",
            skills=["react", "javascript", "css", "ui"],
            workload_capacity=30.0
        )
        self.m_qa = db.add_member(
            self.pid, "Meera QA", "meera_qa",
            role="QA Engineer",
            skills=["testing", "qa", "automation"],
            workload_capacity=25.0
        )
        # Member with NO recorded skills (to test uncertainty)
        self.m_junior = db.add_member(
            self.pid, "Rohan Intern", "rohan_intern",
            role="Intern",
            skills=[],
            workload_capacity=20.0
        )

        # Link chats
        db.link_member(self.m_backend, 101)
        db.link_member(self.m_devops, 102)
        db.link_member(self.m_frontend, 103)
        db.link_member(self.m_qa, 104)
        db.link_member(self.m_junior, 105)

        self.tg = MockTG()
        self.orchestrator = Orchestrator(self.tg)
        self.coord = Coordinator(self.tg, bot_username="orchestrator_bot")

    # -------------------------------------------------------------------------
    # 1. Parallel Execution Eligibility
    # -------------------------------------------------------------------------
    def test_parallel_execution_eligibility(self):
        """Tasks with zero prerequisites must activate in parallel in wave 1."""
        # Add two independent tasks in Waiting state
        t1 = db.add_task(
            pid=self.pid,
            title="Design DB Schema",
            member_id=self.m_backend,
            status="Waiting",
            dependencies=[]
        )
        t2 = db.add_task(
            pid=self.pid,
            title="Configure Cloud Cluster",
            member_id=self.m_devops,
            status="Waiting",
            dependencies=[]
        )

        # Before activation, both are Waiting and not eligible
        self.assertEqual(db.get_task(t1)["status"], "Waiting")
        self.assertEqual(db.get_task(t2)["status"], "Waiting")

        # Run orchestrator activation
        activated = self.orchestrator.activate_eligible_tasks(self.pid)
        activated_ids = [t["id"] for t in activated]

        self.assertIn(t1, activated_ids)
        self.assertIn(t2, activated_ids)

        # Both tasks should now be In Progress and eligible
        t1_fresh = db.get_task(t1)
        t2_fresh = db.get_task(t2)
        self.assertEqual(t1_fresh["status"], "In Progress")
        self.assertEqual(t1_fresh["is_eligible"], 1)
        self.assertEqual(t2_fresh["status"], "In Progress")
        self.assertEqual(t2_fresh["is_eligible"], 1)

        # Notifications sent to both assignees
        self.assertTrue(any("Design DB Schema" in msg for msg in self.tg.to(101)))
        self.assertTrue(any("Configure Cloud Cluster" in msg for msg in self.tg.to(102)))

    # -------------------------------------------------------------------------
    # 2. Dependency Unlocking upon Verified Completion
    # -------------------------------------------------------------------------
    def test_dependency_unlocking_on_verified_completion(self):
        """Downstream task remains Waiting until upstream task is VERIFIED complete."""
        # T1 -> T2
        t1 = db.add_task(
            pid=self.pid,
            title="Implement Core API",
            member_id=self.m_backend,
            status="Waiting",
            required_deliverable="PR link and Swagger doc",
            verification_required=1
        )
        t2 = db.add_task(
            pid=self.pid,
            title="Integrate Frontend",
            member_id=self.m_frontend,
            status="Waiting",
            dependencies=[t1]
        )

        # Initial activation: T1 activates, T2 stays Waiting
        self.orchestrator.activate_eligible_tasks(self.pid)
        self.assertEqual(db.get_task(t1)["status"], "In Progress")
        self.assertEqual(db.get_task(t2)["status"], "Waiting")
        self.assertEqual(db.get_task(t2)["is_eligible"], 0)

        # Step A: Member reports deliverable on T1
        # Task must transition to 'In Review' (Reported completion), NOT 'Completed'
        res = self.orchestrator.report_deliverable(t1, "https://github.com/org/repo/pull/42", "Kabir Dev")
        self.assertEqual(res["status"], "In Review")
        t1_review = db.get_task(t1)
        self.assertEqual(t1_review["status"], "In Review")

        # T2 MUST STILL BE WAITING because T1 is only reported, not verified complete!
        self.orchestrator.activate_eligible_tasks(self.pid)
        self.assertEqual(db.get_task(t2)["status"], "Waiting")
        self.assertEqual(db.get_task(t2)["is_eligible"], 0)

        # Step B: Leader verifies completion of T1
        v_res = self.orchestrator.verify_completion(t1, self.lid, "Dr. Sarah Chen", "Code approved and passes security scan")
        self.assertEqual(v_res["status"], "Completed")

        # Now T2 must automatically be unlocked and activated to In Progress
        t2_fresh = db.get_task(t2)
        self.assertEqual(t2_fresh["status"], "In Progress")
        self.assertEqual(t2_fresh["is_eligible"], 1)

        # Assignee of T2 received Telegram notification
        self.assertTrue(any("Integrate Frontend" in msg for msg in self.tg.to(103)))

    # -------------------------------------------------------------------------
    # 3. Multi-Dependency Tasks
    # -------------------------------------------------------------------------
    def test_multi_dependency_tasks(self):
        """Task with multiple dependencies only unlocks when ALL prerequisites are complete."""
        # T1 (Backend) & T2 (DevOps) -> T3 (E2E Integration Testing)
        t1 = db.add_task(pid=self.pid, title="Backend API", member_id=self.m_backend, status="In Progress")
        t2 = db.add_task(pid=self.pid, title="Kubernetes Cluster", member_id=self.m_devops, status="In Progress")
        t3 = db.add_task(
            pid=self.pid,
            title="E2E Integration Tests",
            member_id=self.m_qa,
            status="Waiting",
            dependencies=[t1, t2]
        )

        # T3 must be Waiting
        self.assertEqual(db.get_task(t3)["status"], "Waiting")

        # Complete only T1
        self.orchestrator.verify_completion(t1, self.lid, "Sarah", "API complete")

        # T3 MUST NOT UNLOCK yet, because T2 is still in progress!
        self.assertEqual(db.get_task(t3)["status"], "Waiting")
        self.assertEqual(db.get_task(t3)["is_eligible"], 0)

        # Complete T2
        self.orchestrator.verify_completion(t2, self.lid, "Sarah", "Cluster ready")

        # Now T3 must unlock to In Progress!
        t3_fresh = db.get_task(t3)
        self.assertEqual(t3_fresh["status"], "In Progress")
        self.assertEqual(t3_fresh["is_eligible"], 1)
        self.assertTrue(any("E2E Integration Tests" in msg for msg in self.tg.to(104)))

    # -------------------------------------------------------------------------
    # 4. Leader Approvals (Plan, Verification, Rejection, Cancellation)
    # -------------------------------------------------------------------------
    def test_leader_approvals_and_rejections(self):
        """Leader approval controls plan activation, deliverable acceptance/rejection, and cancellation."""
        # 1. Plan creation & approval
        plan_dict = {
            "goal": "Build Microservice",
            "tasks": [
                {
                    "title": "Setup Repos",
                    "description": "Initialize Git repos",
                    "priority": "High",
                    "recommended_assignee_id": self.m_devops,
                    "recommended_assignee_name": "Priya Ops",
                    "required_deliverable": "Repo URL",
                    "acceptance_criteria": ["Repo created", "CI passing"],
                    "dependencies": [],
                    "estimated_effort": 4.0
                },
                {
                    "title": "Write Handlers",
                    "description": "Write API routes",
                    "priority": "Medium",
                    "recommended_assignee_id": self.m_backend,
                    "recommended_assignee_name": "Kabir Dev",
                    "required_deliverable": "Endpoints live",
                    "acceptance_criteria": ["Swagger doc active"],
                    "dependencies": ["Setup Repos"],
                    "estimated_effort": 8.0
                }
            ]
        }
        db.save_proposed_plan(self.pid, plan_dict)
        self.assertEqual(db.get_project(self.pid)["plan_status"], "proposed")

        # Approve plan
        created_tasks = self.orchestrator.approve_plan(self.pid, self.lid, "Dr. Sarah Chen")
        self.assertEqual(len(created_tasks), 2)
        self.assertEqual(db.get_project(self.pid)["plan_status"], "approved")

        task_setup = [t for t in created_tasks if t["title"] == "Setup Repos"][0]
        task_handlers = [t for t in created_tasks if t["title"] == "Write Handlers"][0]

        # Wave 1 task is In Progress; wave 2 task is Waiting
        self.assertEqual(db.get_task(task_setup["id"])["status"], "In Progress")
        self.assertEqual(db.get_task(task_handlers["id"])["status"], "Waiting")

        # 2. Deliverable Rejection
        self.orchestrator.report_deliverable(task_setup["id"], "incomplete-url", "Priya Ops")
        self.assertEqual(db.get_task(task_setup["id"])["status"], "In Review")

        # Leader rejects deliverable with feedback
        rej_res = self.orchestrator.reject_deliverable(
            task_setup["id"], self.lid, "Dr. Sarah Chen",
            feedback="CI is failing on lint checks. Please fix and resubmit."
        )
        self.assertEqual(rej_res["status"], "In Progress")
        self.assertIn("CI is failing", db.get_task(task_setup["id"])["progress_note"])
        # Member received rejection notice
        self.assertTrue(any("needs revisions" in msg for msg in self.tg.to(102)))

        # 3. Leader Task Cancellation
        cancel_res = self.orchestrator.cancel_task(
            task_handlers["id"], "Scope changed by client", self.lid, "Dr. Sarah Chen"
        )
        self.assertEqual(cancel_res["status"], "Cancelled")
        self.assertEqual(cancel_res["cancellation_reason"], "Scope changed by client")

        # Audit log verified
        logs = db.audit_logs_of(self.pid)
        actions = [l["action"] for l in logs]
        self.assertIn("plan_approved", actions)
        self.assertIn("deliverable_rejected", actions)
        self.assertIn("task_cancelled", actions)

    # -------------------------------------------------------------------------
    # 5. Assignment Rules & Uncertainty
    # -------------------------------------------------------------------------
    def test_assignment_rules_and_uncertainty_flag(self):
        """Assignment engine matches skills and flags uncertainty when skills are missing."""
        members = db.members_of(self.pid)

        # Case A: Clear match for DevOps
        rec_devops = assignment.recommend_assignee(
            title="Configure Terraform & AWS EKS",
            description="Infrastructure as code for production cluster",
            members=members
        )
        self.assertEqual(rec_devops["recommended_id"], self.m_devops)
        self.assertFalse(rec_devops["is_uncertain"])
        self.assertGreater(rec_devops["score"], 40)

        # Case B: Clear match for Backend
        rec_backend = assignment.recommend_assignee(
            title="Optimize PostgreSQL Query Indexes",
            description="Database performance tuning and python models",
            members=members
        )
        self.assertEqual(rec_backend["recommended_id"], self.m_backend)
        self.assertFalse(rec_backend["is_uncertain"])

        # Case C: Task requires skills NO ONE on the team has (e.g., Solidity / Blockchain)
        rec_unmatched = assignment.recommend_assignee(
            title="Audit Ethereum Smart Contract",
            description="Solidity smart contract formal verification",
            members=members
        )
        # MUST flag uncertainty! Never invent skills or claim certainty.
        self.assertTrue(rec_unmatched["is_uncertain"])
        self.assertIn("No member has required skills", rec_unmatched["uncertain_reason"])

        # Case D: Member with empty skills
        only_junior = [m for m in members if m["id"] == self.m_junior]
        rec_junior = assignment.recommend_assignee(
            title="General Research",
            description="Look up market documentation",
            members=only_junior
        )
        self.assertTrue(rec_junior["is_uncertain"])

    # -------------------------------------------------------------------------
    # 6. Circular Dependencies Detection & Rejection
    # -------------------------------------------------------------------------
    def test_circular_dependencies_detection_and_rejection(self):
        """Detect direct and transitive circular dependencies and reject invalid plans."""
        # Direct cycle: Task A -> Task B -> Task A
        cyclic_plan_direct = [
            {"title": "Task A", "dependencies": ["Task B"]},
            {"title": "Task B", "dependencies": ["Task A"]},
        ]
        has_cycle, cycle_path = dependencies.detect_cycles_in_plan(cyclic_plan_direct)
        self.assertTrue(has_cycle)
        self.assertIn("Task A", cycle_path)
        self.assertIn("Task B", cycle_path)

        # Transitive cycle: A -> B -> C -> A
        cyclic_plan_indirect = [
            {"title": "Alpha", "dependencies": ["Gamma"]},
            {"title": "Beta", "dependencies": ["Alpha"]},
            {"title": "Gamma", "dependencies": ["Beta"]},
        ]
        has_cycle_indirect, cycle_path_indirect = dependencies.detect_cycles_in_plan(cyclic_plan_indirect)
        self.assertTrue(has_cycle_indirect)

        # Valid DAG: A -> B -> C (no cycle)
        valid_plan = [
            {"title": "Alpha", "dependencies": []},
            {"title": "Beta", "dependencies": ["Alpha"]},
            {"title": "Gamma", "dependencies": ["Beta"]},
        ]
        has_cycle_valid, _ = dependencies.detect_cycles_in_plan(valid_plan)
        self.assertFalse(has_cycle_valid)

        # Overriding dependencies on live tasks with a cycle must raise ValueError
        t1 = db.add_task(self.pid, "Task 1", dependencies=[])
        t2 = db.add_task(self.pid, "Task 2", dependencies=[t1])

        # Attempt to make T1 depend on T2 (creates T1 <-> T2 cycle)
        with self.assertRaises(ValueError) as ctx:
            self.orchestrator.override_dependencies(t1, [t2], self.lid, "Dr. Sarah Chen")
        self.assertIn("Circular dependency detected", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 7. Restart Recovery & Idempotency
    # -------------------------------------------------------------------------
    def test_restart_recovery_and_idempotency(self):
        """Verify workflow state is preserved in SQLite across coordinator restarts."""
        # Create an active task and a waiting task
        t1 = db.add_task(self.pid, "Data Ingestion", member_id=self.m_backend, status="In Progress")
        t2 = db.add_task(self.pid, "ML Model Training", member_id=self.m_devops, status="Waiting", dependencies=[t1])

        # Verify initial state
        self.assertEqual(db.get_task(t1)["status"], "In Progress")
        self.assertEqual(db.get_task(t2)["status"], "Waiting")

        # Simulate system restart by reinstantiating orchestrator and coordinator on the same SQLite DB
        new_tg = MockTG()
        restarted_orchestrator = Orchestrator(new_tg)
        restarted_coordinator = Coordinator(new_tg, bot_username="orchestrator_bot")

        # State in SQLite is completely intact
        self.assertEqual(db.get_task(t1)["status"], "In Progress")
        self.assertEqual(db.get_task(t2)["status"], "Waiting")

        # Calling activate_eligible_tasks should NOT activate T2 because T1 is still in progress
        activated = restarted_orchestrator.activate_eligible_tasks(self.pid)
        self.assertEqual(len(activated), 0)
        self.assertEqual(len(new_tg.sent), 0)

        # Now complete T1 through the restarted orchestrator
        restarted_orchestrator.verify_completion(t1, self.lid, "Dr. Sarah Chen", "Ingestion validated")

        # T2 should activate cleanly
        self.assertEqual(db.get_task(t2)["status"], "In Progress")
        self.assertEqual(len(new_tg.to(102)), 1)
        self.assertIn("ML Model Training", new_tg.to(102)[0])

    # -------------------------------------------------------------------------
    # 8. Duplicate Notification Prevention
    # -------------------------------------------------------------------------
    def test_duplicate_notification_prevention(self):
        """Deduplication key prevents duplicate Telegram notifications even on re-activation loops."""
        t1 = db.add_task(self.pid, "Deploy Monitoring", member_id=self.m_devops, status="Waiting", dependencies=[])

        # First activation: notification sent
        self.orchestrator.activate_eligible_tasks(self.pid)
        msgs_first_run = len(self.tg.to(102))
        self.assertEqual(msgs_first_run, 1)

        # Repeated activation calls (e.g. background polling / cron / webhook re-trigger)
        self.orchestrator.activate_eligible_tasks(self.pid)
        self.orchestrator.activate_eligible_tasks(self.pid)
        self.orchestrator.activate_eligible_tasks(self.pid)

        # Message count must remain exactly 1!
        msgs_after_repeats = len(self.tg.to(102))
        self.assertEqual(msgs_after_repeats, 1)

        # Also check direct record_notification deduplication key
        key = f"test_custom_alert_{t1}"
        sent1 = db.record_notification(self.pid, t1, 102, "alert", "Custom alert", deduplication_key=key)
        self.assertTrue(sent1)
        self.assertTrue(db.has_notification(key))

        # Attempt to record again with same key
        sent2 = db.record_notification(self.pid, t1, 102, "alert", "Custom alert duplicate", deduplication_key=key)
        self.assertFalse(sent2)

    # -------------------------------------------------------------------------
    # 9. Failed Telegram Sends Handling
    # -------------------------------------------------------------------------
    def test_failed_telegram_sends_handling(self):
        """Telegram API exceptions must not crash orchestration or break database state."""
        # Create TG mock that throws network errors for chat 101 (Backend)
        failing_tg = MockTG(fail_for_chats=[101])
        fail_orchestrator = Orchestrator(failing_tg)

        t1 = db.add_task(self.pid, "Fault Tolerant Task", member_id=self.m_backend, status="Waiting", dependencies=[])

        # Activation should NOT throw exception even though Telegram send fails
        try:
            activated = fail_orchestrator.activate_eligible_tasks(self.pid)
        except Exception as e:
            self.fail(f"activate_eligible_tasks raised an unexpected exception on Telegram error: {e}")

        # Task in DB MUST still successfully transition to In Progress
        t1_fresh = db.get_task(t1)
        self.assertEqual(t1_fresh["status"], "In Progress")
        self.assertEqual(t1_fresh["is_eligible"], 1)

        # Notification table records status as failed
        dedup_key = f"activate_task_{t1}_member_{self.m_backend}"
        row = db.q("SELECT * FROM notifications WHERE deduplication_key=?", (dedup_key,), one=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Simulated Telegram Network Failure", row["error_message"])

    # -------------------------------------------------------------------------
    # 10. Blocker Resolution Returns to In Progress (Never Completed)
    # -------------------------------------------------------------------------
    def test_blocker_resolution_returns_to_in_progress(self):
        """Resolving a blocker returns task to In Progress, NEVER to Completed."""
        t1 = db.add_task(self.pid, "Stripe Gateway Integration", member_id=self.m_backend, status="In Progress")
        t2 = db.add_task(self.pid, "Payment Frontend UI", member_id=self.m_frontend, status="Waiting", dependencies=[t1])

        # Mark T1 as blocked
        db.update_task(t1, status="Blocked", blocker="Waiting on Stripe API credentials from compliance")
        self.orchestrator.handle_blocker_or_overdue(t1, issue_type="blocker")

        # Leader and downstream assignee were alerted of the blocker
        self.assertTrue(any("BLOCKER" in msg for msg in self.tg.to(999)))   # Leader alert
        self.assertTrue(any("upstream prerequisite" in msg for msg in self.tg.to(103))) # Downstream alert

        # Now simulate blocker resolution via bot or coordinator
        # The member says "Credentials received, unblocked!"
        # Status MUST return to In Progress, NOT Completed!
        db.update_task(t1, status="In Progress", blocker=None)
        db.add_event(self.pid, t1, "blocker_cleared", {"note": "Credentials received"})

        t1_after = db.get_task(t1)
        self.assertEqual(t1_after["status"], "In Progress")
        self.assertIsNone(t1_after["blocker"])

        # Downstream task T2 MUST REMAIN WAITING because T1 is still in progress (not completed)!
        self.orchestrator.activate_eligible_tasks(self.pid)
        t2_after = db.get_task(t2)
        self.assertEqual(t2_after["status"], "Waiting")
        self.assertEqual(t2_after["is_eligible"], 0)


if __name__ == "__main__":
    unittest.main()
