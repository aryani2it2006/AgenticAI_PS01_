"""Orchestration Engine for SyncSoul Agentic Project Orchestration System.

Responsibilities:
- Plan approval & transactional multi-phase task instantiation
- Eligibility detection & atomic task activation
- Dependency graph tracking & verification-gated unlocking
- Transitive downstream impact alerts when tasks are blocked or overdue
- Deduplicated notifications across restarts and concurrent calls
- Strict separation of reported completion vs. verified completion
- Full audit logging of all leader controls (reassignment, overrides, cancellation, rescheduling)
"""
import datetime as dt
import json
from typing import Dict, List, Optional, Tuple

from . import db, dependencies


class OrchestrationEngine:
    def __init__(self, coord):
        if hasattr(coord, "send"):
            self.coord = coord
        else:
            class _CoordAdapter:
                def __init__(self, tg):
                    self.tg = tg

                def send(self, chat_id, text, pid=None, tid=None):
                    return self.tg.send_message(chat_id, text)

                def _alert_leader(self, pid, tid, text):
                    p = db.get_project(pid) if pid else None
                    if p:
                        leader = db.leader_by_id(p["leader_id"])
                        if leader and leader.get("chat_id"):
                            return self.tg.send_message(leader["chat_id"], text)

                def notify_assigned(self, task_id):
                    t = db.get_task(task_id)
                    if t and t.get("owner_chat_id"):
                        return self.tg.send_message(t["owner_chat_id"], f"📌 You have been assigned #{t['id']} {t['title']}")

            self.coord = _CoordAdapter(coord)

    # -------------------------------------------------------------------------
    # Plan Approval & Instantiation
    # -------------------------------------------------------------------------
    def approve_plan(self, project_id: int, leader_id: int, leader_or_plan=None, plan_or_leader=None) -> List[dict]:
        """
        Atomically approve a proposed plan and create all tasks in SQLite.
        Maps planned indices or titles to real task IDs, verifies circularity,
        and immediately activates eligible wave-1 tasks.
        """
        plan_data = None
        leader_name = "Leader"

        if isinstance(leader_or_plan, dict):
            plan_data = leader_or_plan
            if isinstance(plan_or_leader, str):
                leader_name = plan_or_leader
        elif isinstance(leader_or_plan, str):
            leader_name = leader_or_plan
            if isinstance(plan_or_leader, dict):
                plan_data = plan_or_leader

        if plan_data is None:
            p = db.get_project(project_id)
            if p and p.get("proposed_plan"):
                plan_data = p["proposed_plan"]

        if isinstance(plan_data, str):
            try:
                plan_data = json.loads(plan_data)
            except Exception:
                pass

        if not plan_data:
            raise ValueError("No proposed plan found for project")

        raw_tasks = plan_data.get("tasks", [])
        if not raw_tasks:
            raise ValueError("Plan contains no tasks to instantiate")

        # Verify no cycles
        has_cycle, cycle_str = dependencies.detect_cycles_in_plan(raw_tasks)
        if has_cycle:
            raise ValueError(f"Cannot approve plan with circular dependencies: {cycle_str}")

        created_tasks = []
        index_to_real_id: Dict[int, int] = {}
        title_to_real_id: Dict[str, int] = {}
        all_members = {m["id"]: m for m in db.members_of(project_id)}

        # 1. Update project state
        db.ex(
            "UPDATE projects SET goal=?, plan_status='approved', plan_approved_at=?, status='active' WHERE id=?",
            (plan_data.get("goal") or "", db.now(), project_id)
        )

        # 2. Insert all tasks initially with empty dependencies to get real IDs
        for idx, pt in enumerate(raw_tasks):
            title = pt.get("title", f"Task {idx+1}")
            desc = pt.get("description", "")
            prio = pt.get("priority", "Medium")
            effort = float(pt.get("estimated_effort") or 4.0)
            mid = pt.get("recommended_member_id") or pt.get("recommended_assignee_id")
            due = pt.get("due_date")
            tags = pt.get("tags") or []
            deliv = pt.get("required_deliverable") or "Deliverable submission"
            ac = pt.get("acceptance_criteria") or []
            uncertain = 1 if pt.get("assignment_uncertain") else 0
            rationale = pt.get("assignment_explanation") or pt.get("assignment_rationale") or ""

            tid = db.add_task(
                pid=project_id,
                title=title,
                description=desc,
                member_id=mid if mid in all_members else None,
                due_date=due,
                priority=prio,
                estimated_effort=effort,
                tags=tags,
                dependencies=[],
                verification_required=1,
                creator_id=leader_id,
                status="Waiting",
                required_deliverable=deliv,
                acceptance_criteria=ac,
                assignment_uncertain=uncertain,
                assignment_rationale=rationale
            )
            index_to_real_id[idx] = tid
            title_to_real_id[title] = tid
            if "plan_index" in pt:
                index_to_real_id[pt["plan_index"]] = tid

        # 3. Update real task dependencies
        for idx, pt in enumerate(raw_tasks):
            tid = index_to_real_id.get(idx)
            raw_deps = pt.get("dependencies") or []
            real_deps = []
            for d in raw_deps:
                if d in index_to_real_id:
                    real_deps.append(index_to_real_id[d])
                elif isinstance(d, int) and d in index_to_real_id.values():
                    real_deps.append(d)
                elif isinstance(d, str) and d in title_to_real_id:
                    real_deps.append(title_to_real_id[d])

            db.ex("UPDATE tasks SET dependencies=? WHERE id=?", (json.dumps(real_deps), tid))
            created_tasks.append(db.get_task(tid))

        # 4. Record audit log
        db.add_audit_log(
            project_id=project_id,
            task_id=None,
            action="plan_approved",
            detail={"total_tasks": len(created_tasks), "goal": plan_data.get("goal")},
            actor_id=leader_id,
            actor_name=leader_name
        )
        db.add_event(project_id, None, "plan_approved", {"total_tasks": len(created_tasks)})

        # 5. Activate wave-1 eligible tasks
        self.activate_eligible_tasks(project_id)
        return db.tasks_of(project_id)

    # -------------------------------------------------------------------------
    # Task Activation & Prerequisite Gating
    # -------------------------------------------------------------------------
    def activate_eligible_tasks(self, project_id: int) -> List[dict]:
        """
        Examine all waiting/not started tasks. For each task whose prerequisites
        are verified complete, atomically transition to In Progress and notify the assignee.
        """
        all_tasks = db.tasks_of(project_id)
        activated = []

        for t in all_tasks:
            st = t.get("status")
            if st not in ("Waiting", "Not Started"):
                continue

            # Check prerequisites
            prereqs = dependencies.check_prerequisites(t, all_tasks)
            if prereqs["all_completed"]:
                # Atomically transition to In Progress
                was_activated = db.activate_task_atomic(t["id"])
                if was_activated:
                    fresh_t = db.get_task(t["id"])
                    activated.append(fresh_t)
                    db.add_event(project_id, t["id"], "task_activated", {
                        "title": t["title"],
                        "member": fresh_t.get("owner_name")
                    })
                    db.add_audit_log(
                        project_id=project_id,
                        task_id=t["id"],
                        action="task_activated",
                        detail={"title": t["title"], "assignee": fresh_t.get("owner_name")}
                    )

                    # Deduplicated Telegram notification
                    mid = fresh_t.get("member_id")
                    chat_id = fresh_t.get("owner_chat_id")
                    dedup_key = f"activate_task_{fresh_t['id']}_member_{mid}"

                    if chat_id and not db.has_notification(dedup_key):
                        deliv_info = f"\n📦 Required Deliverable: {fresh_t.get('required_deliverable')}" if fresh_t.get("required_deliverable") else ""
                        due_info = f"\nDue: {fresh_t.get('due_datetime') or fresh_t.get('due_date') or 'not set'}"
                        text = (
                            f"🚀 TASK UNLOCKED & READY TO START!\n"
                            f"#{fresh_t['id']} {fresh_t['title']} ({fresh_t['project_name']})\n"
                            f"All prerequisite tasks have been verified complete.{due_info}{deliv_info}\n\n"
                            f"Reply with updates anytime: \"#{fresh_t['id']} 50% done\" or submit your deliverable with /deliver {fresh_t['id']} <link>."
                        )
                        try:
                            sent = self.coord.send(chat_id, text, project_id, fresh_t["id"])
                            db.record_notification(
                                project_id=project_id,
                                task_id=fresh_t["id"],
                                member_id=mid,
                                chat_id=chat_id,
                                notification_type="task_activated",
                                dedup_key=dedup_key,
                                status="sent" if sent else "failed",
                                error_message=None if sent else "Failed to send telegram message"
                            )
                        except Exception as e:
                            db.record_notification(
                                project_id=project_id,
                                task_id=fresh_t["id"],
                                member_id=mid,
                                chat_id=chat_id,
                                notification_type="task_activated",
                                dedup_key=dedup_key,
                                status="failed",
                                error_message=str(e)
                            )

        return activated

    # -------------------------------------------------------------------------
    # Deliverables & Completion Verification
    # -------------------------------------------------------------------------
    def report_deliverable(self, task_id: int, deliverable_submission: str, member_name: str = "Member") -> dict:
        """
        Record deliverable submission from assignee.
        Transitions task to 'In Review' (Reported Complete) and alerts leader for verification.
        Does NOT unlock downstream tasks until verified by leader!
        """
        t = db.get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")

        pid = t["project_id"]
        fields = {
            "status": "In Review",
            "deliverable_submission": deliverable_submission,
            "completion_reported_at": db.now(),
            "progress": 100
        }
        db.update_task(task_id, **fields)
        db.add_event(pid, task_id, "deliverable_submitted", {
            "from": member_name,
            "submission": deliverable_submission
        })
        db.add_audit_log(
            project_id=pid,
            task_id=task_id,
            action="deliverable_submitted",
            detail={"member": member_name, "deliverable": deliverable_submission}
        )

        # Alert the project leader
        self.coord._alert_leader(
            pid, task_id,
            f"📋 DELIVERABLE SUBMITTED FOR REVIEW:\n"
            f"#{t['id']} \"{t['title']}\" by {member_name} ({t['project_name']})\n"
            f"Submission: {deliverable_submission}\n"
            f"Required Deliverable: {t.get('required_deliverable') or 'Standard delivery'}\n\n"
            f"Review on the dashboard or verify via Telegram: /verify {t['id']}"
        )
        return db.get_task(task_id)

    def verify_completion(self, task_id: int, *args, verified_by: str = "Leader", notes: str = "", **kwargs) -> dict:
        """
        Leader verification of task deliverable.
        Transitions task to 'Completed' (Verified), satisfies acceptance criteria,
        and automatically unlocks eligible downstream tasks.
        """
        t = db.get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")

        leader_id = kwargs.get("leader_id")
        if len(args) >= 3:
            leader_id = args[0]
            verified_by = str(args[1])
            notes = str(args[2])
        elif len(args) == 2:
            if isinstance(args[0], int):
                leader_id = args[0]
                verified_by = str(args[1])
            else:
                verified_by = str(args[0])
                notes = str(args[1])
        elif len(args) == 1:
            if isinstance(args[0], int):
                leader_id = args[0]
            else:
                verified_by = str(args[0])

        if "notes" in kwargs:
            notes = kwargs["notes"]
        if "verified_by" in kwargs:
            verified_by = kwargs["verified_by"]

        # Subtasks check: never verify a parent task complete while required subtasks remain incomplete
        subtasks = db.subtasks_of(task_id)
        incomplete = [st for st in subtasks if st.get("status") not in ("Complete", "Completed")]
        if incomplete:
            sub_ids = ", ".join(f"#{st['id']}" for st in incomplete)
            raise ValueError(f"Cannot verify #{task_id} complete while subtask(s) {sub_ids} are incomplete")

        pid = t["project_id"]
        db.update_task(
            task_id,
            status="Completed",
            verified_at=db.now(),
            verified_by=verified_by,
            progress=100,
            blocker=None
        )
        db.add_event(pid, task_id, "task_verified_complete", {
            "verified_by": verified_by,
            "notes": notes
        })
        db.add_audit_log(
            project_id=pid,
            task_id=task_id,
            action="task_verified_complete",
            detail={"verified_by": verified_by, "notes": notes},
            actor_id=leader_id,
            actor_name=verified_by
        )

        # Notify assignee that deliverable has been verified
        if t.get("owner_chat_id"):
            try:
                self.coord.send(
                    t["owner_chat_id"],
                    f"🌟 Deliverable Verified! #{t['id']} \"{t['title']}\" has been verified and marked Complete by {verified_by}. Great job!",
                    pid, task_id
                )
            except Exception:
                pass

        # Automatically unlock downstream tasks
        unlocked = self.activate_eligible_tasks(pid)

        # Check if entire project is complete
        all_p_tasks = db.tasks_of(pid)
        if all_p_tasks and all(tk["status"] in ("Complete", "Completed", "Cancelled") for tk in all_p_tasks):
            db.update_project(pid, status="completed")
            self.coord._alert_leader(pid, None, f"🎉 PROJECT COMPLETE: All tasks in '{t['project_name']}' are verified complete!")
            db.add_audit_log(pid, None, "project_completed", {"message": "All project tasks verified"})

        updated = dict(db.get_task(task_id))
        updated["unlocked_downstream"] = unlocked
        return updated

    def reject_deliverable(self, task_id: int, *args, feedback: str = "", reviewer: str = "Leader", **kwargs) -> dict:
        """
        Leader rejects completion proof and returns task to 'In Progress' with feedback.
        """
        t = db.get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")

        leader_id = kwargs.get("leader_id")
        if len(args) >= 2:
            if isinstance(args[0], int):
                leader_id = args[0]
                reviewer = str(args[1])
            else:
                feedback = str(args[0])
                reviewer = str(args[1])
        elif len(args) == 1:
            if isinstance(args[0], int):
                leader_id = args[0]
            else:
                feedback = str(args[0])

        if "feedback" in kwargs:
            feedback = kwargs["feedback"]
        if "reviewer" in kwargs:
            reviewer = kwargs["reviewer"]

        pid = t["project_id"]
        db.update_task(task_id, status="In Progress", progress_note=f"Rework requested: {feedback}")
        db.add_event(pid, task_id, "deliverable_rejected", {
            "by": reviewer,
            "feedback": feedback
        })
        db.add_audit_log(
            project_id=pid,
            task_id=task_id,
            action="deliverable_rejected",
            detail={"reviewer": reviewer, "feedback": feedback},
            actor_id=leader_id,
            actor_name=reviewer
        )

        if t.get("owner_chat_id"):
            try:
                self.coord.send(
                    t["owner_chat_id"],
                    f"⚠️ Deliverable needs revisions on #{t['id']} \"{t['title']}\":\n"
                    f"Feedback: {feedback}\n"
                    f"Task is back In Progress. Please update and resubmit via /deliver {t['id']}.",
                    pid, task_id
                )
            except Exception:
                pass
        return db.get_task(task_id)

    # -------------------------------------------------------------------------
    # Downstream Impact & Blocker Escalation
    # -------------------------------------------------------------------------
    def handle_blocker_or_overdue(self, task_id: int, reason: Optional[str] = None, is_overdue: bool = False, issue_type: Optional[str] = None):
        """
        Identifies all affected downstream tasks when a prerequisite is blocked or overdue.
        Notifies responsible downstream members and the project leader.
        """
        if issue_type:
            is_overdue = (issue_type.lower() == "overdue")

        task = db.get_task(task_id)
        if not task:
            return

        problem_kind = "overdue" if is_overdue else "blocker"
        if not reason:
            reason = task.get("blocker") or task.get("progress_note") or ("Overdue deadline" if is_overdue else "Blocker encountered")

        pid = task["project_id"]
        all_tasks = db.tasks_of(pid)
        affected = dependencies.find_affected_downstream_tasks(task_id, all_tasks)

        if not affected:
            affected_summary = "None"
        else:
            affected_summary = ", ".join(f"#{a['id']} ({a['title']})" for a in affected)
        today_str = dt.date.today().isoformat()

        # 1. Alert affected downstream assignees
        if affected:
            for aff in affected:
                aff_mid = aff.get("member_id")
                aff_chat = aff.get("owner_chat_id")
                if aff_chat and aff_mid:
                    dedup_key = f"impact_{task_id}_{aff['id']}_{problem_kind}_{today_str}"
                    if not db.has_notification(dedup_key):
                        alert_text = (
                            f"⚠️ DEPENDENCY ALERT:\n"
                            f"upstream prerequisite #{task_id} \"{task['title']}\" is {problem_kind.upper()}: {reason}\n"
                            f"Your downstream task #{aff['id']} \"{aff['title']}\" is currently on hold waiting for it."
                        )
                        try:
                            sent = self.coord.send(aff_chat, alert_text, pid, aff["id"])
                        except Exception:
                            sent = False
                        db.record_notification(
                            project_id=pid,
                            task_id=aff["id"],
                            member_id=aff_mid,
                            chat_id=aff_chat,
                            notification_type=f"downstream_{problem_kind}_alert",
                            dedup_key=dedup_key,
                            status="sent" if sent else "failed"
                        )

        # 2. Alert project leader
        leader_dedup_key = f"leader_impact_{task_id}_{problem_kind}_{today_str}"
        if not db.has_notification(leader_dedup_key):
            leader_msg = (
                f"🚨 BLOCKER / CASCADING IMPACT ALERT:\n"
                f"Task #{task_id} \"{task['title']}\" ({task.get('owner_name') or 'Unassigned'}) is {problem_kind.upper()}: {reason}\n"
                f"Affected downstream tasks ({len(affected)}): {affected_summary}"
            )
            try:
                self.coord._alert_leader(pid, task_id, leader_msg)
            except Exception:
                pass
            db.record_notification(
                project_id=pid,
                task_id=task_id,
                member_id=None,
                chat_id=None,
                notification_type=f"leader_downstream_{problem_kind}",
                dedup_key=leader_dedup_key,
                status="sent"
            )

        db.add_event(pid, task_id, f"downstream_{problem_kind}_escalation", {
            "affected_count": len(affected),
            "affected_task_ids": [a["id"] for a in affected]
        })

    # -------------------------------------------------------------------------
    # Leader Controls (Reassignment, Overrides, Cancellation, Rescheduling)
    # -------------------------------------------------------------------------
    def change_assignment(self, task_id: int, new_member_id: Optional[int], leader_id: int, leader_name: str = "Leader") -> dict:
        t = db.get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")

        pid = t["project_id"]
        old_member = t.get("owner_name") or "Unassigned"
        new_m = db.get_member(new_member_id) if new_member_id else None
        new_name = new_m["name"] if new_m else "Unassigned"

        db.update_task(task_id, member_id=new_member_id, assignment_uncertain=0)
        db.add_audit_log(
            project_id=pid,
            task_id=task_id,
            action="assignment_changed",
            detail={"from": old_member, "to": new_name},
            actor_id=leader_id,
            actor_name=leader_name
        )
        db.add_event(pid, task_id, "assignment_changed", {"from": old_member, "to": new_name})

        # If task is In Progress, notify new assignee
        if t["status"] in ("In Progress", "Waiting"):
            self.coord.notify_assigned(task_id)

        return db.get_task(task_id)

    def override_dependencies(self, task_id: int, new_dependencies: List[int], leader_id: int, leader_name: str = "Leader") -> dict:
        t = db.get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")

        pid = t["project_id"]
        all_tasks = db.tasks_of(pid)

        # Cycle check
        has_cycle, cycle_str = dependencies.check_circular_dependency(all_tasks, task_id, new_dependencies)
        if has_cycle:
            raise ValueError(f"Circular dependency detected: {cycle_str}")

        old_deps = t.get("dependencies") or []
        db.update_task(task_id, dependencies=new_dependencies)
        db.add_audit_log(
            project_id=pid,
            task_id=task_id,
            action="dependency_overridden",
            detail={"old_dependencies": old_deps, "new_dependencies": new_dependencies},
            actor_id=leader_id,
            actor_name=leader_name
        )
        db.add_event(pid, task_id, "dependency_override", {
            "from": old_deps,
            "to": new_dependencies
        })

        # Re-evaluate eligibility for this and downstream tasks
        self.activate_eligible_tasks(pid)
        return db.get_task(task_id)

    def cancel_task(self, task_id: int, reason: str, leader_id: int, leader_name: str = "Leader") -> dict:
        t = db.get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")

        pid = t["project_id"]
        db.update_task(task_id, status="Cancelled", cancellation_reason=reason)
        db.add_audit_log(
            project_id=pid,
            task_id=task_id,
            action="task_cancelled",
            detail={"reason": reason},
            actor_id=leader_id,
            actor_name=leader_name
        )
        db.add_event(pid, task_id, "task_cancelled", {"reason": reason})

        if t.get("owner_chat_id"):
            self.coord.send(
                t["owner_chat_id"],
                f"ℹ️ Task #{t['id']} \"{t['title']}\" has been cancelled by {leader_name}.\nReason: {reason}",
                pid, task_id
            )
        return db.get_task(task_id)

    def reschedule_deadline(self, task_id: int, new_deadline: str, leader_id: int, leader_name: str = "Leader") -> dict:
        t = db.get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")

        pid = t["project_id"]
        old_due = t.get("due_datetime") or t.get("due_date")
        db.update_task(task_id, due_date=new_deadline, due_datetime=new_deadline)
        db.add_audit_log(
            project_id=pid,
            task_id=task_id,
            action="deadline_rescheduled",
            detail={"from": old_due, "to": new_deadline},
            actor_id=leader_id,
            actor_name=leader_name
        )
        db.add_event(pid, task_id, "deadline_changed", {"from": old_due, "to": new_deadline})

        if t.get("owner_chat_id"):
            self.coord.send(
                t["owner_chat_id"],
                f"📅 Schedule Update: Deadline for #{t['id']} \"{t['title']}\" changed to {new_deadline}.",
                pid, task_id
            )
        return db.get_task(task_id)


# Export canonical alias
Orchestrator = OrchestrationEngine
