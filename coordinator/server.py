"""HTTP server (stdlib): serves the dashboard and a JSON API for leaders.

Extended for SyncSoul AI:
- Comprehensive Task Management (priority, subtasks, dependencies, recurrence, verification)
- Assignment Rules Engine recommendations
- Blocker-to-subtask & Leader Approval workflows
- Evidence-based member summaries
- Role-based permissions and member profile updates
"""
import datetime as dt
import json
import re
import sqlite3
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import assignment, blockers, db, dependencies, planning, reminders, reports

HTML = Path(__file__).parent / "static" / "index.html"


class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def _clean(v, n=200):
    return (v or "").strip()[:n] if isinstance(v, str) else ""


def _date(v):
    v = _clean(v, 25)
    if not v:
        return None
    # Check if date or datetime
    try:
        if "T" in v or " " in v:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
        return dt.date.fromisoformat(v).isoformat()
    except ValueError:
        raise ApiError(400, "Dates must look like 2026-12-31 or 2026-12-31T17:00:00")


def make_handler(coord):
    # ---------- ownership helpers ----------
    def own_project(h, pid):
        p = db.get_project(int(pid))
        if not p or p["leader_id"] != h.leader["id"]:
            raise ApiError(404, "Project not found")
        return p

    def own_task(h, tid):
        t = db.get_task(int(tid))
        if not t:
            raise ApiError(404, "Task not found")
        own_project(h, t["project_id"])
        return t

    def own_member(h, mid):
        m = db.get_member(int(mid))
        if not m:
            raise ApiError(404, "Member not found")
        own_project(h, m["project_id"])
        return m

    def member_in(project, mid):
        if mid in (None, ""):
            return None
        m = db.get_member(int(mid))
        if not m or m["project_id"] != project["id"]:
            raise ApiError(400, "That member is not in this project")
        return m["id"]

    def project_state(p):
        tasks = db.tasks_of(p["id"])
        for t in tasks:
            r = reports.risk(t)
            t["risk"] = {"level": r[0], "reason": r[1]} if r else None
            # Subtasks count
            subtasks = [st for st in tasks if st.get("parent_id") == t["id"]]
            t["subtasks_count"] = len(subtasks)
            t["subtasks_completed"] = sum(1 for st in subtasks if st.get("status") in (db.COMPLETE, db.COMPLETED))

        members = db.members_of(p["id"])
        for m in members:
            m["connected"] = bool(m.pop("chat_id", None))
            m["open_tasks"] = sum(1 for t in tasks if t["member_id"] == m["id"] and t["status"] not in (db.COMPLETE, db.COMPLETED))

        for t in tasks:
            t.pop("owner_chat_id", None)
            t["owner_connected"] = any(m["id"] == t["member_id"] and m["connected"] for m in members)

        events = db.recent_events(p["id"])
        approvals = db.get_pending_approvals(p["id"])
        member_sums = reports.member_summaries(members, tasks, events)
        chains = reports.blocker_chains(tasks)
        recent_rems = db.recent_reminders(limit=25)
        plan_analysis = dependencies.analyze_parallel_and_waiting(tasks)
        audit_logs = db.audit_logs_of(p["id"], limit=60)
        proposed_plan = None
        if p.get("proposed_plan"):
            try:
                proposed_plan = json.loads(p["proposed_plan"])
            except Exception:
                proposed_plan = None

        return {
            "project": p,
            "members": members,
            "tasks": tasks,
            "summary": reports.summary(tasks),
            "events": events,
            "pending_approvals": approvals,
            "member_summaries": member_sums,
            "blocker_chains": chains,
            "recent_reminders": recent_rems,
            "plan_analysis": plan_analysis,
            "audit_logs": audit_logs,
            "proposed_plan": proposed_plan
        }


    # ---------- routes ----------
    def register(h):
        b = h.body()
        name, email, pw = _clean(b.get("name"), 80), _clean(b.get("email"), 120).lower(), b.get("password") or ""
        if not name or "@" not in email or len(pw) < 6:
            raise ApiError(400, "Enter your name, a valid email and a password of at least 6 characters")
        try:
            lid = db.create_leader(name, email, pw)
        except sqlite3.IntegrityError:
            raise ApiError(409, "That email is already registered – log in instead")
        h.login_as(lid)

    def login(h):
        b = h.body()
        l = db.verify_leader(_clean(b.get("email"), 120), b.get("password") or "")
        if not l:
            raise ApiError(401, "Wrong email or password")
        h.login_as(l["id"])

    def logout(h):
        db.delete_session(h.session_token())
        h.send_json(200, {"ok": True}, {"Set-Cookie": "sid=; Path=/; Max-Age=0"})

    def me(h):
        l = h.leader
        h.send_json(200, {
            "leader": {
                "name": l["name"],
                "email": l["email"],
                "link_code": l["link_code"],
                "telegram_linked": bool(l["chat_id"])
            },
            "bot_username": coord.bot_username
        })

    def list_projects(h):
        out = []
        for p in db.projects_of(h.leader["id"]):
            tasks = db.tasks_of(p["id"])
            out.append({**p, "summary": reports.summary(tasks), "members": len(db.members_of(p["id"]))})
        h.send_json(200, {"projects": out})

    def create_project(h):
        b = h.body()
        name = _clean(b.get("name"), 100)
        if not name:
            raise ApiError(400, "Project name is required")
        pid = db.create_project(
            h.leader["id"],
            name,
            _clean(b.get("description"), 500),
            _date(b.get("deadline")),
            _clean(b.get("timezone"), 40) or "UTC",
            _clean(b.get("goal"), 500)
        )
        h.send_json(200, {"id": pid})

    def get_project(h, pid):
        h.send_json(200, project_state(own_project(h, pid)))

    def delete_project(h, pid):
        db.delete_project(own_project(h, pid)["id"])
        h.send_json(200, {"ok": True})

    def add_member(h, pid):
        p, b = own_project(h, pid), h.body()
        name = _clean(b.get("name"), 80)
        if not name:
            raise ApiError(400, "Member name is required")
        role = _clean(b.get("role"), 30) or "Member"
        skills = b.get("skills") or []
        cap = float(b.get("workload_capacity") or 40.0)
        mid = db.add_member(p["id"], name, _clean(b.get("telegram_username"), 64), role=role, skills=skills, workload_capacity=cap)
        h.send_json(200, {"id": mid})

    def patch_member(h, mid):
        m, b = own_member(h, mid), h.body()
        fields = {}
        if "name" in b and _clean(b["name"], 80):
            fields["name"] = _clean(b["name"], 80)
        if "role" in b:
            fields["role"] = _clean(b["role"], 30)
        if "skills" in b:
            fields["skills"] = b["skills"]
        if "workload_capacity" in b:
            fields["workload_capacity"] = float(b["workload_capacity"])
        if "status" in b:
            fields["status"] = _clean(b["status"], 20)
        if fields:
            db.update_member(m["id"], **fields)
        h.send_json(200, {"ok": True})

    def delete_member(h, mid):
        db.delete_member(own_member(h, mid)["id"])
        h.send_json(200, {"ok": True})

    def add_task(h, pid):
        p, b = own_project(h, pid), h.body()
        title = _clean(b.get("title"), 150)
        if not title:
            raise ApiError(400, "Task title is required")
        mid = member_in(p, b.get("member_id"))
        priority = _clean(b.get("priority"), 20) or "Medium"
        if priority not in db.PRIORITIES:
            priority = "Medium"
        parent_id = int(b["parent_id"]) if b.get("parent_id") else None
        effort = float(b.get("estimated_effort") or 0.0)
        progress = int(b.get("progress") or 0)
        tags = b.get("tags") or []
        dependencies_val = dependencies.parse_dependencies(b.get("dependencies"))

        # Check circular dependencies if any
        if dependencies_val:
            all_p_tasks = db.tasks_of(p["id"])
            has_cycle, cycle_str = dependencies.check_circular_dependency(all_p_tasks, -1, dependencies_val)
            if has_cycle:
                raise ApiError(400, f"Circular dependency detected: {cycle_str}")

        initial_status = _clean(b.get("status"), 30)
        if not initial_status:
            initial_status = "Waiting" if dependencies_val else "In Progress"

        tid = db.add_task(
            pid=p["id"],
            title=title,
            description=_clean(b.get("description"), 500),
            member_id=mid,
            due_date=_date(b.get("due_date")),
            priority=priority,
            start_date=_date(b.get("start_date")),
            due_datetime=_date(b.get("due_datetime")) or _date(b.get("due_date")),
            timezone=_clean(b.get("timezone"), 40) or p.get("timezone", "UTC"),
            estimated_effort=effort,
            progress=progress,
            tags=tags,
            dependencies=dependencies_val,
            attachments=b.get("attachments") or [],
            parent_id=parent_id,
            is_recurring=1 if b.get("is_recurring") else 0,
            recurrence_rule=_clean(b.get("recurrence_rule"), 40) or None,
            verification_required=1 if b.get("verification_required") else 0,
            creator_id=h.leader["id"],
            status=initial_status,
            required_deliverable=_clean(b.get("required_deliverable"), 200),
            acceptance_criteria=b.get("acceptance_criteria") or []
        )
        db.add_event(p["id"], tid, "task_created", {"title": title, "priority": priority})
        # If task is eligible and assigned, notify assignee
        notified = False
        if mid and initial_status in ("In Progress", "Pending", "Active"):
            notified = coord.notify_assigned(tid)
        h.send_json(200, {"id": tid, "notified": notified})


    def patch_task(h, tid):
        t, b = own_task(h, tid), h.body()
        p, fields, reassigned = db.get_project(t["project_id"]), {}, False

        if "title" in b and _clean(b["title"], 150):
            fields["title"] = _clean(b["title"], 150)
        if "description" in b:
            fields["description"] = _clean(b["description"], 500)
        if "due_date" in b:
            fields["due_date"] = _date(b["due_date"])
        if "due_datetime" in b:
            fields["due_datetime"] = _date(b["due_datetime"])
        if "start_date" in b:
            fields["start_date"] = _date(b["start_date"])
        if "priority" in b and b["priority"] in db.PRIORITIES:
            fields["priority"] = b["priority"]
        if "estimated_effort" in b:
            fields["estimated_effort"] = float(b["estimated_effort"] or 0.0)
        if "progress" in b:
            fields["progress"] = max(0, min(100, int(b["progress"] or 0)))
        if "tags" in b:
            fields["tags"] = b["tags"]
        if "attachments" in b:
            fields["attachments"] = b["attachments"]
        if "is_recurring" in b:
            fields["is_recurring"] = 1 if b["is_recurring"] else 0
        if "recurrence_rule" in b:
            fields["recurrence_rule"] = _clean(b.get("recurrence_rule"), 40) or None
        if "verification_required" in b:
            fields["verification_required"] = 1 if b["verification_required"] else 0
        if "verified_by" in b:
            fields["verified_by"] = _clean(b["verified_by"], 80)
            fields["verified_at"] = db.now()

        # Dependencies with cycle check
        if "dependencies" in b:
            new_deps = dependencies.parse_dependencies(b["dependencies"])
            all_tasks = db.tasks_of(p["id"])
            has_cycle, cycle_str = dependencies.check_circular_dependency(all_tasks, t["id"], new_deps)
            if has_cycle:
                raise ApiError(400, f"Circular dependency detected: {cycle_str}")
            fields["dependencies"] = new_deps

        if "member_id" in b:
            fields["member_id"] = member_in(p, b["member_id"])
            reassigned = fields["member_id"] != t["member_id"]

        if "status" in b:
            if b["status"] not in db.STATUSES:
                raise ApiError(400, "Unknown status")
            # Check subtasks before completing parent task
            if b["status"] in (db.COMPLETE, db.COMPLETED):
                subtasks = db.subtasks_of(t["id"])
                incomplete = [st for st in subtasks if st["status"] not in (db.COMPLETE, db.COMPLETED)]
                if incomplete:
                    sub_ids = ", ".join(f"#{st['id']}" for st in incomplete)
                    raise ApiError(400, f"Cannot complete task while subtasks ({sub_ids}) remain incomplete")
            fields["status"] = b["status"]
            fields["blocker"] = _clean(b.get("blocker"), 140) or None if b["status"] == db.BLOCKED else None

        if fields:
            db.update_task(t["id"], **fields)
            db.add_event(p["id"], t["id"], "manual_update", {"changes": {k: v for k, v in fields.items()}})

        # If completed, unblock dependent tasks
        if fields.get("status") in (db.COMPLETE, db.COMPLETED):
            coord._resolve_dependencies(p["id"], t["id"])

        if reassigned and fields.get("member_id"):
            coord.notify_assigned(t["id"])

        h.send_json(200, {"ok": True})

    def delete_task(h, tid):
        db.delete_task(own_task(h, tid)["id"])
        h.send_json(200, {"ok": True})

    def task_comments(h, tid):
        t = own_task(h, tid)
        if h.command == "GET":
            h.send_json(200, {"comments": db.comments_of(t["id"])})
        else:
            b = h.body()
            text = _clean(b.get("text"), 1000)
            if not text:
                raise ApiError(400, "Comment cannot be empty")
            cid = db.add_comment(t["id"], "leader", h.leader["name"], text)
            h.send_json(200, {"id": cid, "created_at": db.now()})

    def recommend_assignee_endpoint(h, pid):
        p = own_project(h, pid)
        members = db.members_of(p["id"])
        all_tasks = db.tasks_of(p["id"])
        b = h.body() if h.command == "POST" else {}
        task_data = {
            "title": _clean(b.get("title", ""), 150),
            "description": _clean(b.get("description", ""), 500),
            "priority": _clean(b.get("priority", "Medium"), 20),
            "tags": b.get("tags", [])
        }
        res = assignment.recommend_assignee(task_data, members, all_tasks)
        h.send_json(200, res)

    def list_approvals(h, pid):
        p = own_project(h, pid)
        h.send_json(200, {"approvals": db.get_pending_approvals(p["id"])})

    def action_approval(h, aid):
        b = h.body()
        action = _clean(b.get("action"), 20).lower()
        if action not in ("approved", "rejected"):
            raise ApiError(400, "Action must be approved or rejected")
        appr = db.q("SELECT * FROM leader_approvals WHERE id=?", (int(aid),), one=True)
        if not appr:
            raise ApiError(404, "Approval not found")
        own_project(h, appr["project_id"])
        db.action_leader_approval(appr["id"], action)

        if action == "approved" and appr.get("request_type") == "subtask_creation":
            subtasks = json.loads(appr.get("proposed_data", "[]"))
            parent_id = appr.get("task_id")
            pid = appr["project_id"]
            created = []
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
                created.append(sub_tid)
                if st.get("recommended_member_id"):
                    coord.notify_assigned(sub_tid)
            db.add_event(pid, parent_id, "subtasks_approved", {"count": len(created), "task_ids": created})

        h.send_json(200, {"ok": True, "action": action})

    def followup_project(h, pid):
        h.send_json(200, {"sent": coord.followup_project(own_project(h, pid)["id"], force=True)})

    def nudge_task(h, tid):
        t = own_task(h, tid)
        h.send_json(200, {"sent": 1 if coord.send_followup(t, force=True) else 0})

    def project_report(h, pid):
        p = own_project(h, pid)
        tasks = db.tasks_of(p["id"])
        members = db.members_of(p["id"])
        events = db.recent_events(p["id"])
        h.send_json(200, {
            "project": p,
            "summary": reports.summary(tasks),
            "member_summaries": reports.member_summaries(members, tasks, events),
            "blocker_chains": reports.blocker_chains(tasks),
            "status_text": reports.status_text(p["name"], tasks),
            "risk_report": reports.risk_report_text(p["name"], tasks)
        })

    def generate_plan_endpoint(h, pid):
        p = own_project(h, pid)
        b = h.body() if h.command == "POST" else {}
        goal = _clean(b.get("goal"), 500) or p.get("goal") or p.get("description") or ""
        deadline = _date(b.get("deadline")) or p.get("deadline")
        members = db.members_of(p["id"])
        all_tasks = db.tasks_of(p["id"])
        plan = planning.generate_plan(p, members, goal, deadline, all_tasks)
        db.update_project(p["id"], goal=goal, proposed_plan=json.dumps(plan), plan_status="proposed")
        h.send_json(200, plan)

    def get_plan_endpoint(h, pid):
        p = own_project(h, pid)
        if p.get("proposed_plan"):
            try:
                plan = json.loads(p["proposed_plan"])
                return h.send_json(200, plan)
            except Exception:
                pass
        members = db.members_of(p["id"])
        all_tasks = db.tasks_of(p["id"])
        goal = p.get("goal") or p.get("description") or "Complete project goal"
        plan = planning.generate_plan(p, members, goal, p.get("deadline"), all_tasks)
        h.send_json(200, plan)

    def approve_plan_endpoint(h, pid):
        p = own_project(h, pid)
        b = h.body() if h.command == "POST" else {}
        plan_data = b.get("plan")
        if not plan_data and p.get("proposed_plan"):
            try:
                plan_data = json.loads(p["proposed_plan"])
            except Exception:
                pass
        if not plan_data:
            members = db.members_of(p["id"])
            all_tasks = db.tasks_of(p["id"])
            goal = _clean(b.get("goal"), 500) or p.get("goal") or p.get("description") or "Complete project goal"
            plan_data = planning.generate_plan(p, members, goal, p.get("deadline"), all_tasks)

        tasks = coord.orchestrator.approve_plan(p["id"], h.leader["id"], plan_data, h.leader["name"])
        h.send_json(200, {"ok": True, "tasks_created": len(tasks)})

    def reject_plan_endpoint(h, pid):
        p = own_project(h, pid)
        db.update_project(p["id"], plan_status="rejected", proposed_plan=None)
        h.send_json(200, {"ok": True})

    def get_graph_endpoint(h, pid):
        p = own_project(h, pid)
        tasks = db.tasks_of(p["id"])
        analysis = dependencies.analyze_parallel_and_waiting(tasks)
        h.send_json(200, analysis)

    def verify_task_endpoint(h, tid):
        t = own_task(h, tid)
        b = h.body() if h.command == "POST" else {}
        notes = _clean(b.get("notes"), 300)
        res = coord.orchestrator.verify_completion(t["id"], h.leader["name"], notes)
        h.send_json(200, {"ok": True, "unlocked": len(res.get("unlocked_downstream", []))})

    def reject_proof_endpoint(h, tid):
        t = own_task(h, tid)
        b = h.body() if h.command == "POST" else {}
        feedback = _clean(b.get("feedback"), 500) or "Deliverable needs rework"
        coord.orchestrator.reject_deliverable(t["id"], feedback, h.leader["name"])
        h.send_json(200, {"ok": True})

    def deliver_task_endpoint(h, tid):
        t = own_task(h, tid)
        b = h.body() if h.command == "POST" else {}
        submission = _clean(b.get("submission"), 1000) or "Deliverable submission"
        member_name = _clean(b.get("member_name"), 80) or t.get("owner_name") or h.leader["name"]
        coord.orchestrator.report_deliverable(t["id"], submission, member_name)
        h.send_json(200, {"ok": True})

    def override_dependencies_endpoint(h, tid):
        t = own_task(h, tid)
        b = h.body() if h.command == "POST" else {}
        new_deps = dependencies.parse_dependencies(b.get("dependencies"))
        coord.orchestrator.override_dependencies(t["id"], new_deps, h.leader["id"], h.leader["name"])
        h.send_json(200, {"ok": True, "dependencies": new_deps})

    def cancel_task_endpoint(h, tid):
        t = own_task(h, tid)
        b = h.body() if h.command == "POST" else {}
        reason = _clean(b.get("reason"), 300) or "Cancelled by leader"
        coord.orchestrator.cancel_task(t["id"], reason, h.leader["id"], h.leader["name"])
        h.send_json(200, {"ok": True})

    def reschedule_task_endpoint(h, tid):
        t = own_task(h, tid)
        b = h.body() if h.command == "POST" else {}
        new_due = _date(b.get("due_date")) or _date(b.get("due_datetime"))
        if not new_due:
            raise ApiError(400, "Valid due date required")
        coord.orchestrator.reschedule_deadline(t["id"], new_due, h.leader["id"], h.leader["name"])
        h.send_json(200, {"ok": True, "due_date": new_due})

    def audit_log_endpoint(h, pid):
        p = own_project(h, pid)
        logs = db.audit_logs_of(p["id"], limit=100)
        h.send_json(200, {"audit_logs": logs})

    ROUTES = [
        ("POST", r"/api/register", register, False),
        ("POST", r"/api/login", login, False),
        ("POST", r"/api/logout", logout, False),
        ("GET", r"/api/me", me, True),
        ("GET", r"/api/projects", list_projects, True),
        ("POST", r"/api/projects", create_project, True),
        ("GET", r"/api/projects/(\d+)", get_project, True),
        ("DELETE", r"/api/projects/(\d+)", delete_project, True),
        ("POST", r"/api/projects/(\d+)/members", add_member, True),
        ("PATCH", r"/api/members/(\d+)", patch_member, True),
        ("DELETE", r"/api/members/(\d+)", delete_member, True),
        ("POST", r"/api/projects/(\d+)/tasks", add_task, True),
        ("PATCH", r"/api/tasks/(\d+)", patch_task, True),
        ("DELETE", r"/api/tasks/(\d+)", delete_task, True),
        ("GET", r"/api/tasks/(\d+)/comments", task_comments, True),
        ("POST", r"/api/tasks/(\d+)/comments", task_comments, True),
        ("POST", r"/api/projects/(\d+)/recommend-assignee", recommend_assignee_endpoint, True),
        ("GET", r"/api/projects/(\d+)/approvals", list_approvals, True),
        ("POST", r"/api/approvals/(\d+)/action", action_approval, True),
        ("POST", r"/api/projects/(\d+)/followup", followup_project, True),
        ("POST", r"/api/tasks/(\d+)/nudge", nudge_task, True),
        ("GET", r"/api/projects/(\d+)/report", project_report, True),
        ("POST", r"/api/projects/(\d+)/plan", generate_plan_endpoint, True),
        ("GET", r"/api/projects/(\d+)/plan", get_plan_endpoint, True),
        ("POST", r"/api/projects/(\d+)/approve-plan", approve_plan_endpoint, True),
        ("POST", r"/api/projects/(\d+)/reject-plan", reject_plan_endpoint, True),
        ("GET", r"/api/projects/(\d+)/graph", get_graph_endpoint, True),
        ("POST", r"/api/tasks/(\d+)/verify", verify_task_endpoint, True),
        ("POST", r"/api/tasks/(\d+)/reject-proof", reject_proof_endpoint, True),
        ("POST", r"/api/tasks/(\d+)/deliver", deliver_task_endpoint, True),
        ("POST", r"/api/tasks/(\d+)/override-dependencies", override_dependencies_endpoint, True),
        ("POST", r"/api/tasks/(\d+)/cancel", cancel_task_endpoint, True),
        ("POST", r"/api/tasks/(\d+)/reschedule", reschedule_task_endpoint, True),
        ("GET", r"/api/projects/(\d+)/audit-log", audit_log_endpoint, True),
    ]
    ROUTES = [(m, re.compile(rx), fn, auth) for m, rx, fn, auth in ROUTES]


    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # -- helpers used by route functions
        def session_token(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c["sid"].value if "sid" in c else None

        @property
        def leader(self):
            return self._leader

        def body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}") if n else {}
                return data if isinstance(data, dict) else {}
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "Invalid JSON")

        def send_json(self, code, obj, headers=None):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def login_as(self, leader_id):
            tok = db.new_session(leader_id)
            self.send_json(200, {"ok": True}, {"Set-Cookie": f"sid={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"})

        # -- dispatch
        def _dispatch(self, method):
            path = urlparse(self.path).path
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    raw = HTML.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    return self.wfile.write(raw)
                if not path.startswith("/api/"):
                    raise ApiError(404, "Not found")
                if method != "GET" and self.headers.get("X-Requested-With") != "fetch":
                    raise ApiError(403, "Bad request")
                for m, rx, fn, auth in ROUTES:
                    mo = rx.fullmatch(path)
                    if m == method and mo:
                        if auth:
                            self._leader = db.leader_by_session(self.session_token())
                            if not self._leader:
                                raise ApiError(401, "Please log in")
                        return fn(self, *mo.groups())
                raise ApiError(404, "Not found")
            except ApiError as e:
                self.send_json(e.code, {"error": e.msg})
            except Exception as e:
                print(f"[server] {method} {path}: {e!r}")
                self.send_json(500, {"error": "Server error"})

        def do_GET(self): self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")
        def do_PATCH(self): self._dispatch("PATCH")
        def do_DELETE(self): self._dispatch("DELETE")

    return Handler


def start(coord, host, port):
    server = ThreadingHTTPServer((host, port), make_handler(coord))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
