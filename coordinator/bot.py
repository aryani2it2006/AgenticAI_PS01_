"""Telegram side: invite linking, /task, reply → classifier → task update, follow-ups, leader alerts,
blocker subtasks, dependency unblocking, and leader approvals.
"""
import datetime as dt
import json
import re
import threading
import time

from . import assignment, blockers, config, db, dependencies, orchestrator, planning, reminders, reports, vision
from .db import BLOCKED, COMPLETE, COMPLETED, IN_PROGRESS, NOT_STARTED, PENDING, WAITING
from .interpreter import interpret as default_interpret, rule_interpret

ID_RE = re.compile(r"(?:#|\btask\s*(?:no\.?|number|id)?\s*)(\d{1,6})\b", re.I)
WORD_RE = re.compile(r"[a-z]{4,}")
PHOTO_LIMIT = 8        # photo reviews allowed per member per 10 minutes (protects API cost)

HELP = ("SyncSoul AI — Agentic Project Orchestration System\n\n"
        "Member Commands:\n"
        "/task – see your active tasks & deadlines\n"
        "/deliver <id> <link/notes> – submit completed deliverable\n"
        "/status – project status and risk report\n\n"
        "Leader Controls:\n"
        "/plan <project_id> <goal> – generate agentic task plan from goal\n"
        "/approve_plan <project_id> – approve plan & activate wave 1\n"
        "/verify <id> – verify deliverable & unlock dependencies\n"
        "/reject_proof <id> <feedback> – request rework on deliverable\n"
        "/cancel_task <id> <reason> – cancel a task\n"
        "/approvals – view pending leader approvals\n"
        "/approve <id> – approve a pending action\n"
        "/reject <id> – reject a pending action\n"
        "/help – show this guide\n\n"
        "Reply in English, Hindi, or Hinglish:\n"
        "• #12 done  /  #12 ho gaya\n"
        "• #12 60% done, will finish by tomorrow\n"
        "• #12 stuck, waiting for Priya to give AWS credentials")


def _fmt_due(t):
    due = t.get("due_datetime") or t.get("due_date")
    prio = f" [{t.get('priority')}]" if t.get("priority") and t.get("priority") != "Medium" else ""
    return f" (due {due}{prio})" if due else f"{prio}"


def followup_text(task, name):
    tid = task["id"]
    t_title = task["title"]
    due = _fmt_due(task)
    if task["status"] == BLOCKED:
        return (f"Hi {name}! #{tid} \"{t_title}\" was blocked: {task.get('blocker') or 'reason not recorded'}.\n"
                "Is that sorted out now? Reply with the latest status or any remaining obstacles.")
    if task["status"] in (IN_PROGRESS, "In Progress"):
        return (f"Hi {name}! Check-in on #{tid} \"{t_title}\"{due}:\n"
                "1. What is your current progress %?\n"
                "2. What work remains?\n"
                "3. Any blockers?\n"
                "4. What is your realistic completion estimate?\n"
                "Reply directly, e.g. \"#{tid} 70% done, need 1 more day\".")
    return (f"Hi {name}! Quick check-in on #{tid} \"{t_title}\"{due}. "
            "Have you started? Reply: done, in progress, or anything blocking you.")


def assigned_text(task, name):
    desc = f"\n{task['description']}" if task.get("description") else ""
    prio = f"\nPriority: {task.get('priority', 'Medium')}"
    due = task.get("due_datetime") or task.get("due_date") or "not set"
    deliv = f"\nRequired Deliverable: {task.get('required_deliverable')}" if task.get("required_deliverable") else ""
    return (f"📌 New task for you, {name} ({task['project_name']}):\n#{task['id']} {task['title']}{desc}{prio}{deliv}\n"
            f"Due: {due}\n\n"
            f"Reply here when you make progress, e.g. \"#{task['id']} done\" or \"#{task['id']} blocked, waiting for …\".")


class Coordinator:
    def __init__(self, tg, interpret=default_interpret, bot_username="", analyze_photo=vision.analyze):
        self.tg = tg
        self.interpret = interpret
        self.bot_username = bot_username
        self.analyze_photo = analyze_photo
        self._photo_times = {}   # chat_id -> recent photo-review timestamps
        self._album_task = {}    # media_group_id -> task id (photos sent together share one task)
        self.orchestrator = orchestrator.OrchestrationEngine(self)

    # ------------------------------------------------------------ sending
    def send(self, chat_id, text, project_id=None, task_id=None):
        try:
            self.tg.send_message(chat_id, text)
            db.add_message("out", chat_id, text, project_id, task_id)
            return True
        except Exception as e:
            print(f"[send error] chat {chat_id}: {e}")
            db.add_message("out", chat_id, f"[FAILED: {e}] {text}", project_id, task_id)
            return False

    def _alert_leader(self, project_id, task_id, text):
        leader, sent = db.leader_of_project(project_id), False
        if leader and leader.get("chat_id"):
            try:
                sent = self.send(leader["chat_id"], text, project_id, task_id)
            except Exception as e:
                print(f"[alert] {e!r}")
        db.add_event(project_id, task_id, "alert", {"message": text, "sent_to_leader": sent})

    def notify_assigned(self, task_id):
        t = db.get_task(task_id)
        if not t or not t["member_id"]:
            return False
        if not t["owner_chat_id"]:
            db.add_event(t["project_id"], t["id"], "assign_pending",
                         {"reason": f"{t['owner_name']} hasn't joined the bot yet – share their invite link"})
            return False
        sent = self.send(t["owner_chat_id"], assigned_text(t, t["owner_name"]), t["project_id"], t["id"])
        if sent:
            db.add_event(t["project_id"], t["id"], "assigned_notified", {"to": t["owner_name"]})
            return True
        else:
            db.add_event(t["project_id"], t["id"], "assigned_notify_failed",
                         {"to": t["owner_name"], "reason": "Failed to deliver Telegram message"})
            return False

    # ------------------------------------------------------------ follow-ups
    def send_followup(self, task, force=False):
        if not task.get("member_id") or not task.get("owner_chat_id"):
            who = task.get("owner_name") or "nobody"
            db.add_event(task["project_id"], task["id"], "followup_skipped",
                         {"reason": f"{who} is not connected to the bot (needs invite link)" if task.get("member_id")
                          else "task has no assignee"})
            return False

        # Classify reminder phase & overdue info
        due_str = task.get("due_datetime") or task.get("due_date")
        rem_type, rem_desc, diff_hours = reminders.determine_reminder_phase(task)

        # Anti-spam and cooldown checks
        recent_logs = db.recent_reminders(task["id"], limit=10)
        cooldown_hours = max(0.5, (config.CHECKIN_INTERVAL_MIN or 240) / 60.0)
        ok, reason = reminders.should_send_reminder(task, recent_logs, min_interval_hours=cooldown_hours, force=force)
        if not ok:
            db.add_event(task["project_id"], task["id"], "followup_skipped", {"reason": reason})
            return False

        text = reminders.generate_reminder_prompt(task, task["owner_name"], rem_type, rem_desc)

        sent = self.send(task["owner_chat_id"], text, task["project_id"], task["id"])
        status_str = "sent" if sent else "failed"
        err_msg = None if sent else "Telegram delivery failed"
        db.add_reminder_log(task["id"], task["member_id"], rem_type, status=status_str,
                            scheduled_for=due_str, error_message=err_msg)
        if sent:
            db.touch_followup(task["id"])
            db.set_active_task(task["member_id"], task["id"])
            db.add_event(task["project_id"], task["id"], "followup_sent",
                         {"to": task["owner_name"], "text": text, "type": rem_type})
        else:
            db.add_event(task["project_id"], task["id"], "followup_failed",
                         {"to": task["owner_name"], "reason": "Failed to deliver Telegram message"})

        # Overdue escalation to leader and affected downstream tasks
        if rem_type == "overdue":
            self.orchestrator.handle_blocker_or_overdue(task["id"], rem_desc, is_overdue=True)
            if diff_hours >= 24.0:
                self._alert_leader(task["project_id"], task["id"],
                                   f"🚨 ESCALATION: Task #{task['id']} \"{task['title']}\" assigned to {task['owner_name']} "
                                   f"is {rem_desc}! Please review.")

        return sent


    def followup_project(self, project_id, force=True, interval_min=None):
        """One follow-up per member: their least recently chased unfinished task."""
        interval = interval_min if interval_min is not None else config.CHECKIN_INTERVAL_MIN
        by_member, sent = {}, 0
        for t in db.tasks_of(project_id):
            if t["status"] not in (COMPLETE, COMPLETED):
                by_member.setdefault(t["member_id"], []).append(t)
        for mid, tasks in by_member.items():
            try:
                tasks.sort(key=lambda t: (t["last_followup_at"] or "", t.get("due_datetime") or t.get("due_date") or ""))
                t = tasks[0]
                if not force and t["last_followup_at"]:
                    last = dt.datetime.fromisoformat(t["last_followup_at"].replace("Z", "+00:00"))
                    if (dt.datetime.now(dt.timezone.utc) - last).total_seconds() < interval * 60:
                        continue
                sent += 1 if self.send_followup(t, force=force) else 0
            except Exception as e:
                print(f"[followup member {mid} error] {e!r}")
        return sent

    def scheduler_loop(self):
        while True:
            time.sleep(30)
            if config.CHECKIN_INTERVAL_MIN > 0:
                for p in db.all_projects():
                    try:
                        self.followup_project(p["id"], force=False)
                    except Exception as e:
                        print(f"[scheduler] {e!r}")

    def start_scheduler(self):
        threading.Thread(target=self.scheduler_loop, daemon=True).start()

    # ------------------------------------------------------------ incoming
    def handle_update(self, update):
        # Update deduplication
        uid = update.get("update_id")
        if uid and not db.record_telegram_update(uid):
            # Duplicate update safely skipped
            return

        msg = update.get("message")
        if (msg and not msg.get("text") and msg["chat"].get("type") == "private"
                and (msg.get("photo") or self._image_document(msg))):
            return self._photo(msg)
        if not msg or not msg.get("text") or msg["chat"].get("type") != "private":
            return
        chat_id, user, text = msg["chat"]["id"], msg.get("from", {}), msg["text"].strip()
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            return self._command(chat_id, user, cmd.split("@")[0].lower(), arg.strip())
        self._reply(chat_id, text)

    def _command(self, chat_id, user, cmd, arg):
        if cmd == "/start":
            return self._start(chat_id, user, arg)
        if cmd in ("/task", "/tasks"):
            return self._tasks(chat_id)
        if cmd == "/status":
            return self._status(chat_id)
        if cmd == "/approvals":
            return self._approvals(chat_id)
        if cmd == "/approve":
            return self._action_approval(chat_id, arg, "approved")
        if cmd == "/reject":
            return self._action_approval(chat_id, arg, "rejected")
        if cmd == "/plan":
            return self._plan_cmd(chat_id, arg)
        if cmd in ("/approve_plan", "/approveplan"):
            return self._approve_plan_cmd(chat_id, arg)
        if cmd == "/verify":
            return self._verify_cmd(chat_id, arg)
        if cmd in ("/reject_proof", "/rejectproof"):
            return self._reject_proof_cmd(chat_id, arg)
        if cmd == "/deliver":
            return self._deliver_cmd(chat_id, arg)
        if cmd in ("/cancel_task", "/canceltask"):
            return self._cancel_task_cmd(chat_id, arg)
        return self.send(chat_id, HELP)

    def _get_leader_for_chat(self, chat_id):
        return db.q("SELECT * FROM leaders WHERE chat_id=?", (str(chat_id),), one=True)

    def _plan_cmd(self, chat_id, arg):
        leader = self._get_leader_for_chat(chat_id)
        if not leader:
            return self.send(chat_id, "Only registered team leaders can generate project plans. Link your leader account from the dashboard first.")

        parts = arg.split(" ", 1)
        pid = None
        goal_text = ""
        if parts and parts[0].isdigit():
            pid = int(parts[0])
            goal_text = parts[1].strip() if len(parts) > 1 else ""
        else:
            goal_text = arg.strip()
            projs = db.projects_of(leader["id"])
            if len(projs) == 1:
                pid = projs[0]["id"]
            elif len(projs) > 1:
                p_list = ", ".join(f"#{p['id']} ({p['name']})" for p in projs)
                return self.send(chat_id, f"Please specify project ID: /plan <project_id> <goal>\nYour projects: {p_list}")
            else:
                return self.send(chat_id, "No project found. Create a project on the dashboard first.")

        p = db.get_project(pid)
        if not p or p["leader_id"] != leader["id"]:
            return self.send(chat_id, f"Project #{pid} not found or you are not its leader.")

        members = db.members_of(pid)
        all_tasks = db.tasks_of(pid)
        goal = goal_text or p.get("goal") or p.get("description")
        if not goal:
            return self.send(chat_id, "Please provide a goal: /plan <project_id> <high-level goal>")

        plan = planning.generate_plan(p, members, goal, all_tasks=all_tasks)
        db.update_project(pid, goal=goal, proposed_plan=json.dumps(plan), plan_status="proposed")

        task_summaries = []
        for t in plan["tasks"]:
            assignee = t.get("recommended_member_name") or "Leader Choice Required"
            deps = ", ".join(f"T{d+1}" for d in t["dependencies"]) or "None (Wave 1 Parallel)"
            task_summaries.append(f"• T{t['plan_index']+1}: {t['title']} [{t['priority']}] -> {assignee} (Deps: {deps})")

        msg = (
            f"🎯 PROPOSED PLAN FOR '{p['name']}':\n"
            f"Goal: {goal}\n\n" +
            "\n".join(task_summaries) +
            f"\n\nTotal tasks: {len(plan['tasks'])} | Parallel Waves: {plan['parallel_waves']}\n"
            f"To activate this plan and notify assignees, send:\n/approve_plan {pid}"
        )
        self.send(chat_id, msg)

    def _approve_plan_cmd(self, chat_id, arg):
        leader = self._get_leader_for_chat(chat_id)
        if not leader:
            return self.send(chat_id, "Only team leaders can approve plans.")
        if not arg or not arg.strip().isdigit():
            return self.send(chat_id, "Please specify project ID: /approve_plan <project_id>")
        pid = int(arg.strip())
        p = db.get_project(pid)
        if not p or p["leader_id"] != leader["id"]:
            return self.send(chat_id, f"Project #{pid} not found.")

        plan_data = None
        if p.get("proposed_plan"):
            try:
                plan_data = json.loads(p["proposed_plan"])
            except Exception:
                pass

        if not plan_data:
            members = db.members_of(pid)
            all_tasks = db.tasks_of(pid)
            goal = p.get("goal") or p.get("description") or "Complete project goal"
            plan_data = planning.generate_plan(p, members, goal, all_tasks=all_tasks)

        try:
            tasks = self.orchestrator.approve_plan(pid, leader["id"], plan_data, leader["name"])
            self.send(chat_id, f"✅ PLAN APPROVED & ACTIVATED!\nProject '{p['name']}' has {len(tasks)} tasks created. Wave 1 parallel tasks have been unlocked and assignees notified.")
        except Exception as e:
            self.send(chat_id, f"Error activating plan: {e}")

    def _verify_cmd(self, chat_id, arg):
        leader = self._get_leader_for_chat(chat_id)
        if not leader:
            return self.send(chat_id, "Only team leaders can verify task completion.")
        parts = arg.strip().split(" ", 1)
        if not parts or not parts[0].isdigit():
            return self.send(chat_id, "Usage: /verify <task_id> [optional verification notes]")
        tid = int(parts[0])
        notes = parts[1].strip() if len(parts) > 1 else ""
        t = db.get_task(tid)
        if not t:
            return self.send(chat_id, f"Task #{tid} not found.")
        p = db.get_project(t["project_id"])
        if not p or p["leader_id"] != leader["id"]:
            return self.send(chat_id, "You are not the leader of this task's project.")

        try:
            res = self.orchestrator.verify_completion(tid, leader["name"], notes)
            unlocked = res.get("unlocked_downstream", [])
            unlocked_msg = f" 🚀 {len(unlocked)} downstream task(s) unlocked!" if unlocked else ""
            self.send(chat_id, f"✅ Verified! Task #{tid} \"{t['title']}\" is verified Complete.{unlocked_msg}")
        except Exception as e:
            self.send(chat_id, f"Verification failed: {e}")

    def _reject_proof_cmd(self, chat_id, arg):
        leader = self._get_leader_for_chat(chat_id)
        if not leader:
            return self.send(chat_id, "Only team leaders can review deliverables.")
        parts = arg.strip().split(" ", 1)
        if not parts or not parts[0].isdigit():
            return self.send(chat_id, "Usage: /reject_proof <task_id> <feedback/reason>")
        tid = int(parts[0])
        feedback = parts[1].strip() if len(parts) > 1 else "Deliverable needs improvement"
        t = db.get_task(tid)
        if not t:
            return self.send(chat_id, f"Task #{tid} not found.")
        p = db.get_project(t["project_id"])
        if not p or p["leader_id"] != leader["id"]:
            return self.send(chat_id, "You are not the leader of this task's project.")

        self.orchestrator.reject_deliverable(tid, feedback, leader["name"])
        self.send(chat_id, f"Task #{tid} rejected with feedback. Assignee has been notified to rework.")

    def _deliver_cmd(self, chat_id, arg):
        members = db.members_by_chat(chat_id)
        if not members:
            return self.send(chat_id, "I don't know you yet – open the invite link from your team leader first.")
        open_tasks = db.open_tasks_for_chat(chat_id)
        if not open_tasks:
            return self.send(chat_id, "You have no open tasks right now.")

        parts = arg.strip().split(" ", 1)
        tid = None
        submission = ""
        if parts and parts[0].isdigit():
            candidate_id = int(parts[0])
            matched = [t for t in open_tasks if t["id"] == candidate_id]
            if matched:
                tid = candidate_id
                submission = parts[1].strip() if len(parts) > 1 else ""
        if not tid:
            tid = open_tasks[0]["id"]
            submission = arg.strip()

        if not submission:
            submission = "Deliverable link / files submitted"

        t = db.get_task(tid)
        self.orchestrator.report_deliverable(tid, submission, members[0]["name"])
        self.send(chat_id, f"📦 Deliverable for #{tid} \"{t['title']}\" submitted! Your leader has been notified for verification.")

    def _cancel_task_cmd(self, chat_id, arg):
        leader = self._get_leader_for_chat(chat_id)
        if not leader:
            return self.send(chat_id, "Only team leaders can cancel tasks.")
        parts = arg.strip().split(" ", 1)
        if not parts or not parts[0].isdigit():
            return self.send(chat_id, "Usage: /cancel_task <task_id> <reason>")
        tid = int(parts[0])
        reason = parts[1].strip() if len(parts) > 1 else "Cancelled by leader"
        t = db.get_task(tid)
        if not t:
            return self.send(chat_id, f"Task #{tid} not found.")
        p = db.get_project(t["project_id"])
        if not p or p["leader_id"] != leader["id"]:
            return self.send(chat_id, "You are not the leader of this task's project.")

        self.orchestrator.cancel_task(tid, reason, leader["id"], leader["name"])
        self.send(chat_id, f"Task #{tid} has been cancelled.")


    def _start(self, chat_id, user, arg):
        if arg.startswith("lead_"):
            leader = db.leader_by_code(arg[5:])
            if not leader:
                return self.send(chat_id, "That leader link is not valid. Copy it again from the dashboard.")
            db.link_leader_chat(leader["id"], chat_id)
            return self.send(chat_id, f"Hi {leader['name']}! ✅ Telegram linked. I'll alert you here about blockers and completed tasks.")
        if arg:
            m = db.member_by_token(arg)
            members = [m] if m else []
            if not members:
                return self.send(chat_id, "That invite link is not valid. Ask your team leader for a fresh one.")
        else:
            members = db.members_by_username(user.get("username")) or db.members_by_chat(chat_id)
            if not members:
                return self.send(chat_id, "Welcome! Open the invite link your team leader gave you (it looks like t.me/<bot>?start=…).")
        for m in members:
            db.link_member(m["id"], chat_id, user.get("username"))
            db.add_event(m["project_id"], None, "joined", {"name": m["name"]})
        names = ", ".join(sorted({db.get_project(m["project_id"])["name"] for m in members}))
        self.send(chat_id, f"Hi {members[0]['name']}, you're connected to: {names} 👋\nSend /task to see your tasks.\n\n" + HELP)

    def _tasks(self, chat_id):
        if not db.members_by_chat(chat_id):
            return self.send(chat_id, "I don't know you yet – open the invite link from your team leader first.")
        tasks = db.open_tasks_for_chat(chat_id)
        if not tasks:
            return self.send(chat_id, "You have no open tasks right now. 🎉")
        lines, last = [], None
        for t in tasks:
            if t["project_name"] != last:
                lines.append(f"\n📁 {t['project_name']}")
                last = t["project_name"]
            extra = f" – blocker: {t['blocker']}" if t["status"] == BLOCKED and t.get("blocker") else ""
            prio = f" [{t.get('priority')}]" if t.get("priority") and t.get("priority") != "Medium" else ""
            lines.append(f"#{t['id']} {t['title']} [{t['status']}]{_fmt_due(t)}{extra}{prio}")
        lines.append("\nReply like \"#<number> done\" or \"#<number> stuck, waiting for …\".")
        self.send(chat_id, "Your open tasks:" + "\n".join(lines))

    def _status(self, chat_id):
        members = db.members_by_chat(chat_id)
        if not members:
            return self.send(chat_id, "I don't know you yet – open the invite link from your team leader first.")
        for pid in sorted({m["project_id"] for m in members}):
            self.send(chat_id, reports.status_text(db.get_project(pid)["name"], db.tasks_of(pid)), pid)

    def _approvals(self, chat_id):
        members = db.members_by_chat(chat_id)
        if not members:
            return self.send(chat_id, "Open the invite link from your team leader first.")
        out = []
        for m in members:
            pid = m["project_id"]
            approvals = db.get_pending_approvals(pid)
            if approvals:
                out.append(f"📁 Approvals for Project #{pid}:")
                for a in approvals:
                    out.append(f"• ID #{a['id']} ({a['request_type']}): {a.get('reason', '')} — /approve {a['id']} or /reject {a['id']}")
        if not out:
            return self.send(chat_id, "No pending leader approvals. Everything is up to date! ✨")
        self.send(chat_id, "\n".join(out))

    def _action_approval(self, chat_id, arg, action):
        if not arg or not arg.isdigit():
            return self.send(chat_id, f"Please specify the approval ID, e.g. /{action} 12")
        appr_id = int(arg)
        approvals = db.q("SELECT * FROM leader_approvals WHERE id=?", (appr_id,), one=True)
        if not approvals:
            return self.send(chat_id, f"Approval #{appr_id} not found.")
        db.action_leader_approval(appr_id, action)
        if action == "approved" and approvals.get("request_type") == "subtask_creation":
            # Create the approved subtasks
            subtasks = json.loads(approvals.get("proposed_data", "[]"))
            parent_id = approvals.get("task_id")
            parent_task = db.get_task(parent_id)
            pid = approvals["project_id"]
            created_count = 0
            for st in subtasks:
                sub_tid = db.add_task(
                    pid=pid,
                    title=st.get("title", "Resolution Subtask"),
                    description=st.get("description", ""),
                    member_id=st.get("recommended_member_id"),
                    priority=st.get("priority", "High"),
                    parent_id=parent_id,
                    estimated_effort=st.get("estimated_effort", 2.0)
                )
                created_count += 1
                if st.get("recommended_member_id"):
                    self.notify_assigned(sub_tid)
            db.add_event(pid, parent_id, "subtasks_approved", {"count": created_count})
            return self.send(chat_id, f"✅ Approval #{appr_id} accepted! {created_count} subtasks created and assigned.")
        self.send(chat_id, f"✅ Approval #{appr_id} has been {action}.")

    # ------------------------------------------------------------ replies
    def _resolve(self, text, tasks):
        """Which open task is this reply about? Returns a task or None (= ask)."""
        by_id = {t["id"]: t for t in tasks}
        for m in ID_RE.finditer(text):
            if int(m.group(1)) in by_id:
                return by_id[int(m.group(1))]
        words, best, score = set(WORD_RE.findall(text.lower())), None, 0
        for t in tasks:
            s = len(words & set(WORD_RE.findall(t["title"].lower())))
            if s > score:
                best, score = t, s
        if best:
            return best
        if len(tasks) == 1:
            return tasks[0]
        active = sorted((t for t in tasks if t.get("member_active") == t["id"]),
                        key=lambda t: t.get("last_followup_at") or "", reverse=True)
        return active[0] if active else None

    def _reply(self, chat_id, text):
        m_task_create = re.match(r"^create\s+task\s*:\s*(.+)$", text, re.I)
        if m_task_create:
            title = m_task_create.group(1).strip()
            members = db.members_by_chat(chat_id)
            if members:
                pid = members[0]["project_id"]
                mid = members[0]["id"]
                tid = db.add_task(pid, title=title, member_id=mid, status="In Progress")
                return self.send(chat_id, f"✅ Task #{tid} \"{title}\" created and persisted.", pid, tid)
            leader = self._get_leader_for_chat(chat_id)
            if leader:
                pjs = db.projects_of(leader["id"])
                if pjs:
                    pid = pjs[0]["id"]
                    tid = db.add_task(pid, title=title, creator_id=leader["id"], status="Waiting")
                    return self.send(chat_id, f"✅ Task #{tid} \"{title}\" created and persisted.", pid, tid)

        members = db.members_by_chat(chat_id)
        if not members:
            return self.send(chat_id, "I don't know you yet – open the invite link from your team leader first.")
        db.add_message("in", chat_id, text, members[0]["project_id"])
        tasks = db.open_tasks_for_chat(chat_id)
        if not tasks:
            return self.send(chat_id, "You have no open tasks right now – nice work! 🎉")
        task = self._resolve(text, tasks)
        if task is None:
            lines = "\n".join(f"#{t['id']} {t['title']} ({t['project_name']})" for t in tasks)
            return self.send(chat_id, f"Which task is this about? Put its number in your message, e.g. \"#{tasks[0]['id']} done\".\n{lines}")

        pid = task["project_id"]
        res = self.interpret(text, [task], task["id"])
        db.add_event(pid, task["id"], "reply", {"from": task["owner_name"], "text": text, **res.to_dict()})

        if res.intent == "question":
            return self.send(chat_id, "Good question – I've noted it for your leader. If you have an update on "
                                      f"#{task['id']}, send it any time.", pid, task["id"])
        if res.intent == "unclear" or (res.status is None and res.intent != "progress"):
            q = res.clarifying_question or (f"Thanks! For #{task['id']} \"{task['title']}\": is it done, in progress, or blocked by something?")
            return self.send(chat_id, q, pid, task["id"])

        # Check subtasks constraint before completing parent task:
        # Never mark parent task complete while required subtasks remain incomplete
        if res.status in (COMPLETE, COMPLETED):
            subtasks = db.subtasks_of(task["id"])
            incomplete_subtasks = [st for st in subtasks if st["status"] not in (COMPLETE, COMPLETED)]
            if incomplete_subtasks:
                sub_ids = ", ".join(f"#{st['id']}" for st in incomplete_subtasks)
                return self.send(chat_id,
                                 f"⚠️ Cannot mark #{task['id']} complete yet because subtask(s) {sub_ids} are still incomplete. "
                                 f"Please finish all subtasks before closing the parent task.",
                                 pid, task["id"])

        old_status, old_blocker = task["status"], task.get("blocker")
        new_status = res.status or old_status
        blocker = (res.blocker or old_blocker or "reason not given") if new_status == BLOCKED else None
        fields = {"status": new_status, "blocker": blocker}
        if res.note:
            fields["progress_note"] = res.note
        if res.proposed_deadline:
            fields["proposed_deadline"] = res.proposed_deadline
        if res.delay_reason:
            fields["delay_reason"] = res.delay_reason
        if res.progress_pct is not None:
            fields["progress"] = res.progress_pct

        db.update_task(task["id"], **fields)
        db.set_active_task(task["member_id"], task["id"])
        if old_status != new_status or old_blocker != blocker:
            db.add_event(pid, task["id"], "status_change",
                         {"from": old_status, "to": new_status, "blocker": blocker, "note": res.note})
        self._confirm(chat_id, task, new_status, blocker, res)

        # Blocker handling and automatic subtask generation
        if new_status == BLOCKED and (old_status != BLOCKED or old_blocker != blocker):
            all_proj_members = db.members_of(pid)
            all_proj_tasks = db.tasks_of(pid)
            proposed_subtasks = blockers.propose_blocker_subtasks(task, blocker, all_proj_members, all_proj_tasks)
            db.add_blocker_resolution(task["id"], blocker, proposed_subtasks=proposed_subtasks)
            # Create leader approval request
            appr_id = db.create_leader_approval(pid, "subtask_creation", task["id"], proposed_subtasks, reason=blocker)
            self._alert_leader(pid, task["id"],
                               f"🚧 Blocker in {task['project_name']}: {task['owner_name']} – "
                               f"#{task['id']} \"{task['title']}\": {blocker}\n"
                               f"AI proposed {len(proposed_subtasks)} resolution subtask(s). "
                               f"Review on dashboard or reply /approve {appr_id}.")
            # Notify affected downstream tasks
            self.orchestrator.handle_blocker_or_overdue(task["id"], blocker, is_overdue=False)

        # Completion handling and dependency unblocking
        elif new_status in (COMPLETE, COMPLETED) and old_status not in (COMPLETE, COMPLETED):
            if task.get("verification_required"):
                # Gated verification required: report deliverable for leader verification (does not unlock downstream yet)
                subm = res.note or text
                self.orchestrator.report_deliverable(task["id"], subm, task["owner_name"])
            else:
                self._alert_leader(pid, task["id"], f"✅ {task['owner_name']} completed #{task['id']} \"{task['title']}\" ({task['project_name']})")
                # Dependency resolution
                self._resolve_dependencies(pid, task["id"])
                # Subtask resolution: if this was a blocker subtask, unblock the parent task
                if task.get("parent_id"):
                    self._resolve_subtask_completion(pid, task["parent_id"])
                if all(t["status"] in (COMPLETE, COMPLETED) for t in db.tasks_of(pid)):
                    self._alert_leader(pid, None, f"🎉 All tasks in {task['project_name']} are complete!")

    def _resolve_subtask_completion(self, pid, parent_id):
        """When a subtask completes, check if parent task blocker can be cleared without falsely completing the parent task."""
        parent = db.get_task(parent_id)
        if not parent:
            return
        subtasks = db.subtasks_of(parent_id)
        if all(st["status"] in (COMPLETE, COMPLETED) for st in subtasks):
            if parent["status"] == BLOCKED:
                db.update_task(parent_id, status=IN_PROGRESS, blocker=None)
                db.add_event(pid, parent_id, "blocker_subtasks_resolved",
                             {"detail": f"Resolution subtasks completed; #{parent_id} is unblocked and back In Progress"})
                if parent.get("owner_chat_id"):
                    self.send(parent["owner_chat_id"],
                              f"🎉 All resolution subtasks for #{parent_id} \"{parent['title']}\" are completed! "
                              f"Your task is now unblocked and back In Progress.",
                              pid, parent_id)

    def _resolve_dependencies(self, pid, completed_task_id):
        """Unblock tasks that were waiting on completed_task_id."""
        self.orchestrator.activate_eligible_tasks(pid)
        tasks = db.tasks_of(pid)
        for t in tasks:
            if t["status"] in (COMPLETE, COMPLETED):
                continue

            deps = t.get("dependencies") or []
            if completed_task_id in deps:
                # Check if all other prerequisites are complete
                prereqs = dependencies.check_prerequisites(t, tasks)
                if prereqs["all_completed"]:
                    if t["status"] == BLOCKED:
                        db.update_task(t["id"], status=IN_PROGRESS, blocker=None)
                        db.add_event(pid, t["id"], "dependency_cleared",
                                     {"cleared_by": completed_task_id, "new_status": IN_PROGRESS})
                    if t.get("owner_chat_id"):
                        self.send(t["owner_chat_id"],
                                  f"🎉 Prerequisite task #{completed_task_id} has been completed! "
                                  f"You can now proceed with #{t['id']} \"{t['title']}\".",
                                  pid, t["id"])

    def _confirm(self, chat_id, task, status, blocker, res):
        t, pid = f"#{task['id']} \"{task['title']}\"", task["project_id"]
        if status == BLOCKED:
            msg = f"Got it – {t} is marked Blocked: {blocker}. I've told your leader and formulated resolution subtasks. Tell me when it's sorted."
            if res.note:
                msg = f"Nice progress ({res.note}). " + msg
        elif status in (COMPLETE, COMPLETED):
            msg = f"Great work, {task['owner_name']}! {t} is marked Complete. 🎉"
        elif status in (PENDING, NOT_STARTED):
            msg = f"Noted – {t} stays Pending. Message me when you start."
        elif res.intent == "unblocked":
            msg = f"Good, the blocker is cleared – {t} is back In Progress."
        else:
            msg = f"Thanks! {t} is marked In Progress. It stays open until you tell me it's complete."
        self.send(chat_id, msg, pid, task["id"])

    # ------------------------------------------------------------ photo proof
    def _image_document(self, msg):
        doc = msg.get("document") or {}
        return doc if (doc.get("mime_type") or "") in vision.IMAGE_TYPES else None

    def _photo_allowed(self, chat_id):
        now_time = time.time()
        recent = [t for t in self._photo_times.get(chat_id, []) if now_time - t < 600]
        ok = len(recent) < PHOTO_LIMIT
        if ok:
            recent.append(now_time)
        self._photo_times[chat_id] = recent
        return ok

    def _forward_evidence(self, pid, tid, msg):
        """Show the leader the member's photo (best effort)."""
        leader = db.leader_of_project(pid)
        if not (leader and leader.get("chat_id") and msg.get("message_id")):
            return
        try:
            self.tg.copy_message(leader["chat_id"], msg["chat"]["id"], msg["message_id"])
            db.add_message("out", leader["chat_id"], "📷 (photo proof forwarded)", pid, tid)
        except Exception as e:
            print(f"[photo] could not forward to leader: {e!r}")

    def _photo(self, msg):
        """Photo proof: find the task, have the vision model check it, update the task only if it holds up."""
        chat_id, caption = msg["chat"]["id"], (msg.get("caption") or "").strip()
        if not db.members_by_chat(chat_id):
            return self.send(chat_id, "I don't know you yet – open the invite link from your team leader first.")
        tasks = db.open_tasks_for_chat(chat_id)
        if not tasks:
            return self.send(chat_id, "You have no open tasks right now – nice work! 🎉")

        # which task? album sibling -> caption / replied-to message -> the only open task -> last asked about
        group, task = msg.get("media_group_id"), None
        if group and not caption:
            task = next((t for t in tasks if t["id"] == self._album_task.get(group)), None)
        if task is None:
            replied = (msg.get("reply_to_message") or {}).get("text") or ""
            task = self._resolve(f"{caption} {replied}".strip(), tasks)
        if task is None:
            lines = "\n".join(f"#{t['id']} {t['title']} ({t['project_name']})" for t in tasks)
            return self.send(chat_id, f"Which task is this photo for? Send it again with the task number in the "
                                      f"caption, e.g. \"#{tasks[0]['id']}\".\n{lines}")
        if group:
            if len(self._album_task) > 50:
                self._album_task.clear()
            self._album_task[group] = task["id"]
        pid, tid = task["project_id"], task["id"]

        # a blocker in the caption always wins
        if caption and rule_interpret(caption, [task], tid).intent == "blocked":
            self._reply(chat_id, caption if ID_RE.search(caption) else f"#{tid} {caption}")
            return self._forward_evidence(pid, tid, msg)

        if not self._photo_allowed(chat_id):
            return self.send(chat_id, "I can review a few photos at a time – please wait a few minutes and send it "
                                      "again.", pid, tid)
        db.add_message("in", chat_id, f"📷 {caption or '(photo)'}", pid, tid)

        doc = self._image_document(msg)
        if doc:
            file_id, size = doc["file_id"], doc.get("file_size") or 0
        else:
            sizes = msg["photo"]
            fit = [p for p in sizes if (p.get("file_size") or 0) <= vision.MAX_IMAGE_BYTES] or sizes
            best = max(fit, key=lambda p: (p.get("width", 0) * p.get("height", 0), p.get("file_size") or 0))
            file_id, size = best["file_id"], best.get("file_size") or 0
        if size > vision.MAX_IMAGE_BYTES:
            return self.send(chat_id, "That image is too large (max 5 MB). Send it as a normal photo or a smaller "
                                      "file.", pid, tid)

        if self.analyze_photo is not vision.analyze or vision.available():
            self.send(chat_id, f"🔍 Got your photo for #{tid} \"{task['title']}\" – checking it now…", pid, tid)
        review, reason = None, ""
        try:
            data = self.tg.download_file(self.tg.get_file(file_id)["file_path"], vision.MAX_IMAGE_BYTES)
            media_type = vision.sniff_media_type(data)
            if media_type is None:
                return self.send(chat_id, "That file doesn't look like an image I can read (JPG, PNG, WEBP or GIF). "
                                          "Please send a photo.", pid, tid)
            review = self.analyze_photo(data, media_type, task, caption)
        except vision.VisionUnavailable as e:
            reason = str(e)
        except Exception as e:
            print(f"[photo] review failed: {e!r}")
            reason = "automatic check failed"
        if review is None:
            return self._photo_unverified(chat_id, msg, task, caption, reason)
        self._apply_review(chat_id, msg, task, caption, review)

    def _photo_unverified(self, chat_id, msg, task, caption, reason):
        pid, tid, name = task["project_id"], task["id"], task["owner_name"]
        db.add_event(pid, tid, "reply", {"from": name, "text": f"📷 {caption or '(photo proof)'}",
                                         "intent": "photo_unverified", "status": None, "blocker": None,
                                         "note": reason, "source": "vision", "confidence": 0})
        self.send(chat_id, f"📷 Thanks! I couldn't check your photo for #{tid} \"{task['title']}\" automatically, so "
                           "I've passed it to your leader to review. The task stays open until it's confirmed.",
                  pid, tid)
        self._alert_leader(pid, tid, f"📷 {name} sent photo proof for #{tid} \"{task['title']}\" "
                                     f"({task['project_name']}), but automatic checking wasn't available "
                                     f"({reason}). Please review it and update the task.")
        self._forward_evidence(pid, tid, msg)

    def _apply_review(self, chat_id, msg, task, caption, review):
        pid, tid, name = task["project_id"], task["id"], task["owner_name"]
        action = vision.decide(review)
        old_status, new_status = task["status"], task["status"]
        if action == "complete":
            new_status = COMPLETE
        elif action in ("partial", "needs_fixes") and old_status in (PENDING, NOT_STARTED):
            new_status = IN_PROGRESS

        if action not in ("unrelated", "unclear"):
            fields = {"progress_note": f"📷 {review.summary or review.observed}"[:200]}
            if new_status != old_status:
                fields["status"] = new_status
                if new_status == COMPLETE:
                    fields["blocker"] = None
                    fields["verified_at"] = db.now()
                    fields["verified_by"] = "Vision AI (Confidence " + str(round(review.confidence * 100)) + "%)"
            db.update_task(tid, **fields)
            db.set_active_task(task["member_id"], tid)
        db.add_event(pid, tid, "reply", {"from": name, "text": f"📷 {caption or '(photo proof)'}",
                                         "intent": f"photo_{action}", "status": new_status if new_status != old_status else None,
                                         "blocker": None, "source": "vision", "confidence": review.confidence,
                                         "photo_review": review.to_dict()})
        if new_status != old_status:
            db.add_event(pid, tid, "status_change", {"from": old_status, "to": new_status, "blocker": None,
                                                     "note": review.summary})
        self.send(chat_id, self._photo_message(action, task, review, new_status), pid, tid)

        if action == "complete":
            q = f", quality {review.quality}/10" if review.quality is not None else ""
            self._alert_leader(pid, tid, f"✅ {name} completed #{tid} \"{task['title']}\" ({task['project_name']}) – "
                                         f"photo proof checked ({round(review.confidence * 100)}% sure{q}): "
                                         f"{review.summary or review.observed}")
            self._forward_evidence(pid, tid, msg)
            self._resolve_dependencies(pid, tid)
            if all(t["status"] in (COMPLETE, COMPLETED) for t in db.tasks_of(pid)):
                self._alert_leader(pid, None, f"🎉 All tasks in {task['project_name']} are complete!")

    def _photo_message(self, action, task, review, new_status):
        t = f"#{task['id']} \"{task['title']}\""
        say = review.summary or review.observed
        bullets = lambda items: "".join(f"\n• {i}" for i in items)
        if action == "complete":
            q = f" (quality {review.quality}/10)" if review.quality is not None else ""
            return (f"✅ Photo checked – {t} looks complete and correct{q}. {say}\n"
                    f"Marked Complete – great work, {task['owner_name']}! 🎉 Your leader has been notified.")
        if action == "needs_fixes":
            return (f"🔍 I checked your photo for {t}. It looks finished, but not fully correct:"
                    f"{bullets(review.issues) or chr(10) + say}\n"
                    f"It stays open (status: {new_status}). Fix it and send a new photo when it's ready.")
        if action == "partial":
            more = f"\nStill to do:{bullets(review.issues)}" if review.issues else ""
            return (f"📷 Thanks! From your photo, {t} looks partly done: {say}{more}\n"
                    f"Status: {new_status}. Send another photo when it's finished.")
        if action == "not_done":
            return (f"🔍 I can't see {t} as finished in this photo: {say}\n"
                    "Send a photo that shows the completed work.")
        if action == "unrelated":
            seen = f" I see: {review.observed}." if review.observed else ""
            return (f"🤔 This photo doesn't look related to {t}.{seen}\n"
                    "Please send a photo of the actual work, or put the right task number in the caption, "
                    f"e.g. \"#{task['id']}\".")
        return (f"📷 I couldn't judge {t} from this photo{f' ({say})' if say else ''}. Please send a clearer photo "
                "that shows the finished work (good light, whole item or screen in frame).")
