"""SQLite store: leaders, sessions, projects, members, tasks, events, messages,
comments, reminders, blocker resolutions, leader approvals, and telegram updates.
"""
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

from . import config

_path = config.DB_PATH

# Canonical Statuses (Requirement 1)
NOT_STARTED = "Not Started"
WAITING = "Waiting"
IN_PROGRESS = "In Progress"
BLOCKED = "Blocked"
IN_REVIEW = "In Review"
COMPLETED = "Completed"
CANCELLED = "Cancelled"

# Backwards compatibility aliases
PENDING = "Pending"
COMPLETE = "Complete"
ACTIVE = "Active"

STATUSES = [NOT_STARTED, WAITING, IN_PROGRESS, BLOCKED, IN_REVIEW, COMPLETED, CANCELLED, PENDING, COMPLETE, ACTIVE]
CANONICAL_STATUSES = [NOT_STARTED, WAITING, IN_PROGRESS, BLOCKED, IN_REVIEW, COMPLETED, CANCELLED]
PRIORITIES = ["Low", "Medium", "High", "Urgent"]
ROLES = ["Leader", "Admin", "Member"]
SESSION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS leaders (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE, salt TEXT, pw_hash TEXT, link_code TEXT UNIQUE, chat_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, leader_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, leader_id INTEGER NOT NULL,
  name TEXT NOT NULL, description TEXT, deadline TEXT, timezone TEXT DEFAULT 'UTC', assignment_rules_config TEXT,
  goal TEXT, status TEXT DEFAULT 'active', plan_status TEXT DEFAULT 'none', plan_approved_at TEXT, proposed_plan TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS members (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
  name TEXT NOT NULL, telegram_username TEXT, chat_id TEXT, invite_token TEXT UNIQUE,
  active_task_id INTEGER, role TEXT DEFAULT 'Member', skills TEXT DEFAULT '[]', workload_capacity REAL DEFAULT 40.0,
  status TEXT DEFAULT 'Active', created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
  member_id INTEGER, title TEXT NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'Pending',
  priority TEXT NOT NULL DEFAULT 'Medium', blocker TEXT, progress_note TEXT, start_date TEXT, due_date TEXT,
  due_datetime TEXT, timezone TEXT DEFAULT 'UTC', estimated_effort REAL DEFAULT 0.0, progress INTEGER DEFAULT 0,
  tags TEXT DEFAULT '[]', dependencies TEXT DEFAULT '[]', attachments TEXT DEFAULT '[]', parent_id INTEGER,
  is_recurring INTEGER DEFAULT 0, recurrence_rule TEXT, verification_required INTEGER DEFAULT 0,
  verified_at TEXT, verified_by TEXT, creator_id INTEGER, assignees TEXT DEFAULT '[]',
  proposed_deadline TEXT, delay_reason TEXT, last_followup_at TEXT,
  required_deliverable TEXT, acceptance_criteria TEXT DEFAULT '[]', deliverable_submission TEXT,
  completion_reported_at TEXT, is_eligible INTEGER DEFAULT 0, activated_at TEXT, cancellation_reason TEXT,
  assignment_uncertain INTEGER DEFAULT 0, assignment_rationale TEXT,
  updated_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, project_id INTEGER,
  task_id INTEGER, kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, direction TEXT,
  chat_id TEXT, project_id INTEGER, task_id INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
  author_type TEXT NOT NULL, author_name TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS reminders_log (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
  member_id INTEGER, reminder_type TEXT NOT NULL, scheduled_for TEXT, sent_at TEXT,
  status TEXT NOT NULL DEFAULT 'sent', response_text TEXT, error_message TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS blocker_resolutions (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
  blocker_text TEXT NOT NULL, responsible_type TEXT, responsible_entity TEXT, subtask_id INTEGER,
  proposed_subtasks TEXT, status TEXT NOT NULL DEFAULT 'detected', created_at TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS leader_approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
  request_type TEXT NOT NULL, task_id INTEGER, proposed_data TEXT NOT NULL, reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending', created_at TEXT, actioned_at TEXT);
CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, processed_at TEXT);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
  task_id INTEGER, member_id INTEGER, chat_id TEXT, notification_type TEXT NOT NULL,
  deduplication_key TEXT UNIQUE, sent_at TEXT, status TEXT NOT NULL DEFAULT 'sent', error_message TEXT);
CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  project_id INTEGER, task_id INTEGER, actor_id INTEGER, actor_name TEXT, action TEXT NOT NULL, detail TEXT);
"""


def configure(path):
    global _path
    _path = str(path)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextlib.contextmanager
def conn():
    Path(_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def backup_db():
    """Create a backup of the current database before running migrations."""
    db_file = Path(_path)
    if db_file.exists() and db_file.stat().st_size > 0:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = db_file.parent / f"{db_file.stem}_backup_{ts}.db"
        try:
            shutil.copy2(db_file, backup_file)
            return str(backup_file)
        except Exception as e:
            print(f"[db] backup warning: {e}")
    return None


def migrate():
    """Safe, incremental migration: add missing columns and new tables without data loss."""
    with conn() as c:
        # Check tasks table columns
        cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        add_cols = {
            "priority": "TEXT NOT NULL DEFAULT 'Medium'",
            "start_date": "TEXT",
            "due_datetime": "TEXT",
            "timezone": "TEXT DEFAULT 'UTC'",
            "estimated_effort": "REAL DEFAULT 0.0",
            "progress": "INTEGER DEFAULT 0",
            "tags": "TEXT DEFAULT '[]'",
            "dependencies": "TEXT DEFAULT '[]'",
            "attachments": "TEXT DEFAULT '[]'",
            "parent_id": "INTEGER",
            "is_recurring": "INTEGER DEFAULT 0",
            "recurrence_rule": "TEXT",
            "verification_required": "INTEGER DEFAULT 0",
            "verified_at": "TEXT",
            "verified_by": "TEXT",
            "creator_id": "INTEGER",
            "assignees": "TEXT DEFAULT '[]'",
            "proposed_deadline": "TEXT",
            "delay_reason": "TEXT",
            "required_deliverable": "TEXT",
            "acceptance_criteria": "TEXT DEFAULT '[]'",
            "deliverable_submission": "TEXT",
            "completion_reported_at": "TEXT",
            "is_eligible": "INTEGER DEFAULT 0",
            "activated_at": "TEXT",
            "cancellation_reason": "TEXT",
            "assignment_uncertain": "INTEGER DEFAULT 0",
            "assignment_rationale": "TEXT"
        }
        for col_name, col_def in add_cols.items():
            if col_name not in cols:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")

        # Check members table columns
        m_cols = {r["name"] for r in c.execute("PRAGMA table_info(members)")}
        m_add_cols = {
            "role": "TEXT DEFAULT 'Member'",
            "skills": "TEXT DEFAULT '[]'",
            "workload_capacity": "REAL DEFAULT 40.0",
            "status": "TEXT DEFAULT 'Active'"
        }
        for col_name, col_def in m_add_cols.items():
            if col_name not in m_cols:
                c.execute(f"ALTER TABLE members ADD COLUMN {col_name} {col_def}")

        # Check projects table columns
        p_cols = {r["name"] for r in c.execute("PRAGMA table_info(projects)")}
        p_add_cols = {
            "timezone": "TEXT DEFAULT 'UTC'",
            "assignment_rules_config": "TEXT",
            "goal": "TEXT",
            "status": "TEXT DEFAULT 'active'",
            "plan_status": "TEXT DEFAULT 'none'",
            "plan_approved_at": "TEXT",
            "proposed_plan": "TEXT"
        }
        for col_name, col_def in p_add_cols.items():
            if col_name not in p_cols:
                c.execute(f"ALTER TABLE projects ADD COLUMN {col_name} {col_def}")

        # Create new tables if they don't exist
        c.execute("""
        CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
          task_id INTEGER, member_id INTEGER, chat_id TEXT, notification_type TEXT NOT NULL,
          deduplication_key TEXT UNIQUE, sent_at TEXT, status TEXT NOT NULL DEFAULT 'sent', error_message TEXT)
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
          project_id INTEGER, task_id INTEGER, actor_id INTEGER, actor_name TEXT, action TEXT NOT NULL, detail TEXT)
        """)


def init():
    backup_db()
    with conn() as c:
        c.executescript(SCHEMA)
    migrate()


def q(sql, args=(), one=False):
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).lastrowid


# ---------------- leaders & sessions ----------------
def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def create_leader(name, email, password):
    """Raises sqlite3.IntegrityError if the email already exists."""
    salt = secrets.token_hex(16)
    return ex("INSERT INTO leaders (name,email,salt,pw_hash,link_code,created_at) VALUES (?,?,?,?,?,?)",
              (name, email.lower(), salt, _hash(password, salt), secrets.token_urlsafe(6), now()))


def verify_leader(email, password):
    l = q("SELECT * FROM leaders WHERE email=?", (email.lower(),), one=True)
    if l and hmac.compare_digest(l["pw_hash"], _hash(password, l["salt"])):
        return l
    return None


def new_session(leader_id):
    tok = secrets.token_urlsafe(32)
    ex("INSERT INTO sessions VALUES (?,?,?)", (tok, leader_id, now()))
    return tok


def leader_by_session(tok):
    if not tok:
        return None
    s = q("SELECT * FROM sessions WHERE token=?", (tok,), one=True)
    if not s:
        return None
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
    if age.days > SESSION_DAYS:
        return None
    return q("SELECT id,name,email,link_code,chat_id FROM leaders WHERE id=?", (s["leader_id"],), one=True)


def delete_session(tok):
    ex("DELETE FROM sessions WHERE token=?", (tok,))


def leader_by_code(code):
    return q("SELECT * FROM leaders WHERE link_code=?", (code,), one=True)


def link_leader_chat(leader_id, chat_id):
    ex("UPDATE leaders SET chat_id=? WHERE id=?", (str(chat_id), leader_id))


def leader_of_project(pid):
    return q("SELECT l.* FROM leaders l JOIN projects p ON p.leader_id=l.id WHERE p.id=?", (pid,), one=True)


def leader_by_id(lid):
    return q("SELECT * FROM leaders WHERE id=?", (lid,), one=True)


def save_proposed_plan(pid, plan):
    plan_str = json.dumps(plan) if isinstance(plan, (dict, list)) else (plan or "")
    ex("UPDATE projects SET proposed_plan=?, plan_status='proposed' WHERE id=?", (plan_str, pid))


# ---------------- projects ----------------
def create_project(leader_id, name, description="", deadline=None, timezone="UTC", goal=""):
    return ex("INSERT INTO projects (leader_id,name,description,deadline,timezone,goal,created_at) VALUES (?,?,?,?,?,?,?)",
              (leader_id, name, description, deadline, timezone, goal, now()))


def get_project(pid):
    return q("SELECT * FROM projects WHERE id=?", (pid,), one=True)


def projects_of(leader_id):
    return q("SELECT * FROM projects WHERE leader_id=? ORDER BY id DESC", (leader_id,))


def all_projects():
    return q("SELECT * FROM projects")


def update_project(pid, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), pid))


def delete_project(pid):
    with conn() as c:
        for t in ("events", "messages", "tasks", "members", "leader_approvals"):
            c.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


# ---------------- members ----------------
def add_member(pid, name, username=None, role="Member", skills="[]", workload_capacity=40.0):
    if isinstance(skills, list):
        skills = json.dumps(skills)
    return ex("INSERT INTO members (project_id,name,telegram_username,invite_token,role,skills,workload_capacity,created_at) VALUES (?,?,?,?,?,?,?,?)",
              (pid, name, (username or "").lstrip("@") or None, secrets.token_urlsafe(8), role, skills, workload_capacity, now()))


def get_member(mid):
    m = q("SELECT * FROM members WHERE id=?", (mid,), one=True)
    if m and isinstance(m.get("skills"), str):
        try:
            m["skills"] = json.loads(m["skills"])
        except Exception:
            m["skills"] = [s.strip() for s in m["skills"].split(",") if s.strip()]
    return m


def members_of(pid):
    rows = q("SELECT * FROM members WHERE project_id=? ORDER BY id", (pid,))
    for m in rows:
        if isinstance(m.get("skills"), str):
            try:
                m["skills"] = json.loads(m["skills"])
            except Exception:
                m["skills"] = [s.strip() for s in m["skills"].split(",") if s.strip()]
    return rows


def update_member(mid, **fields):
    if "skills" in fields and isinstance(fields["skills"], list):
        fields["skills"] = json.dumps(fields["skills"])
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE members SET {cols} WHERE id=?", (*fields.values(), mid))


def delete_member(mid):
    ex("UPDATE tasks SET member_id=NULL WHERE member_id=?", (mid,))
    ex("DELETE FROM members WHERE id=?", (mid,))


def member_by_token(tok):
    return q("SELECT * FROM members WHERE invite_token=?", (tok,), one=True)


def members_by_chat(chat_id):
    return q("SELECT m.*, p.name AS project_name FROM members m JOIN projects p ON p.id=m.project_id "
             "WHERE m.chat_id=?", (str(chat_id),))


def members_by_username(username):
    if not username:
        return []
    return q("SELECT * FROM members WHERE lower(telegram_username)=lower(?)", (username.lstrip("@"),))


def link_member(mid, chat_id, username=None):
    ex("UPDATE members SET chat_id=?, telegram_username=COALESCE(telegram_username, ?) WHERE id=?",
       (str(chat_id), username, mid))


def set_active_task(mid, task_id):
    ex("UPDATE members SET active_task_id=? WHERE id=?", (task_id, mid))


# ---------------- tasks ----------------
_TASK_SQL = ("SELECT t.*, m.name AS owner_name, m.chat_id AS owner_chat_id, m.active_task_id AS member_active, "
             "p.name AS project_name, p.timezone AS project_timezone FROM tasks t JOIN projects p ON p.id=t.project_id "
             "LEFT JOIN members m ON m.id=t.member_id")


def _format_task_row(t):
    if not t:
        return None
    for field in ("tags", "dependencies", "attachments", "assignees", "acceptance_criteria"):
        if isinstance(t.get(field), str):
            try:
                t[field] = json.loads(t[field])
            except Exception:
                t[field] = []
        elif t.get(field) is None:
            t[field] = []
    # Ensure sensible defaults for orchestration fields
    if t.get("required_deliverable") is None:
        t["required_deliverable"] = ""
    if t.get("is_eligible") is None:
        t["is_eligible"] = 1 if t.get("status") in ("In Progress", "Complete", "Completed") else 0
    if t.get("assignment_uncertain") is None:
        t["assignment_uncertain"] = 0
    return t


def add_task(pid, title, description="", member_id=None, due_date=None, priority="Medium",
             start_date=None, due_datetime=None, timezone="UTC", estimated_effort=0.0,
             progress=0, tags=None, dependencies=None, attachments=None, parent_id=None,
             is_recurring=0, recurrence_rule=None, verification_required=0, creator_id=None,
             assignees=None, status="Pending", required_deliverable=None, acceptance_criteria=None,
             assignment_uncertain=0, assignment_rationale=""):
    tags_str = json.dumps(tags or []) if isinstance(tags, list) else (tags or "[]")
    deps_str = json.dumps(dependencies or []) if isinstance(dependencies, list) else (dependencies or "[]")
    atts_str = json.dumps(attachments or []) if isinstance(attachments, list) else (attachments or "[]")
    assignees_str = json.dumps(assignees or []) if isinstance(assignees, list) else (assignees or "[]")
    ac_str = json.dumps(acceptance_criteria or []) if isinstance(acceptance_criteria, list) else (acceptance_criteria or "[]")

    is_eligible = 1 if status in ("In Progress", "Complete", "Completed") else 0
    activated_at = now() if status in ("In Progress", "Complete", "Completed") else None

    return ex("INSERT INTO tasks (project_id,member_id,title,description,due_date,priority,start_date,"
              "due_datetime,timezone,estimated_effort,progress,tags,dependencies,attachments,parent_id,"
              "is_recurring,recurrence_rule,verification_required,creator_id,assignees,status,"
              "required_deliverable,acceptance_criteria,assignment_uncertain,assignment_rationale,"
              "is_eligible,activated_at,updated_at,created_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (pid, member_id, title, description, due_date, priority, start_date, due_datetime or due_date,
               timezone, estimated_effort, progress, tags_str, deps_str, atts_str, parent_id,
               is_recurring, recurrence_rule, verification_required, creator_id, assignees_str, status,
               required_deliverable, ac_str, 1 if assignment_uncertain else 0, assignment_rationale or "",
               is_eligible, activated_at, now(), now()))


def get_task(tid):
    row = q(_TASK_SQL + " WHERE t.id=?", (tid,), one=True)
    return _format_task_row(row)


def tasks_of(pid):
    rows = q(_TASK_SQL + " WHERE t.project_id=? ORDER BY t.id", (pid,))
    return [_format_task_row(r) for r in rows]


def subtasks_of(parent_id):
    rows = q(_TASK_SQL + " WHERE t.parent_id=? ORDER BY t.id", (parent_id,))
    return [_format_task_row(r) for r in rows]


def open_tasks_for_chat(chat_id):
    rows = q(_TASK_SQL + " WHERE m.chat_id=? AND t.status NOT IN ('Complete', 'Completed') ORDER BY t.due_date, t.id",
             (str(chat_id),))
    return [_format_task_row(r) for r in rows]


def update_task(tid, **fields):
    fields["updated_at"] = now()
    for json_col in ("tags", "dependencies", "attachments", "assignees"):
        if json_col in fields and isinstance(fields[json_col], (list, dict)):
            fields[json_col] = json.dumps(fields[json_col])
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid))


def touch_followup(tid):
    ex("UPDATE tasks SET last_followup_at=? WHERE id=?", (now(), tid))


def delete_task(tid):
    with conn() as c:
        # Also clean up subtasks or orphan references
        c.execute("DELETE FROM task_comments WHERE task_id=?", (tid,))
        c.execute("DELETE FROM reminders_log WHERE task_id=?", (tid,))
        c.execute("DELETE FROM blocker_resolutions WHERE task_id=?", (tid,))
        c.execute("UPDATE tasks SET parent_id=NULL WHERE parent_id=?", (tid,))
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))


# ---------------- comments ----------------
def add_comment(task_id, author_type, author_name, text):
    return ex("INSERT INTO task_comments (task_id,author_type,author_name,text,created_at) VALUES (?,?,?,?,?)",
              (task_id, author_type, author_name, text, now()))


def comments_of(task_id):
    return q("SELECT * FROM task_comments WHERE task_id=? ORDER BY id ASC", (task_id,))


# ---------------- reminders log ----------------
def add_reminder_log(task_id, member_id, reminder_type, status='sent', scheduled_for=None,
                     sent_at=None, response_text=None, error_message=None):
    return ex("INSERT INTO reminders_log (task_id,member_id,reminder_type,scheduled_for,sent_at,status,response_text,error_message,created_at) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (task_id, member_id, reminder_type, scheduled_for, sent_at or now(), status, response_text, error_message, now()))


def recent_reminders(task_id=None, limit=50):
    if task_id:
        return q("SELECT * FROM reminders_log WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit))
    return q("SELECT r.*, t.title AS task_title, m.name AS member_name FROM reminders_log r "
             "LEFT JOIN tasks t ON t.id=r.task_id LEFT JOIN members m ON m.id=r.member_id "
             "ORDER BY r.id DESC LIMIT ?", (limit,))


# ---------------- blocker resolutions ----------------
def add_blocker_resolution(task_id, blocker_text, responsible_type=None, responsible_entity=None,
                           proposed_subtasks=None):
    p_str = json.dumps(proposed_subtasks or []) if isinstance(proposed_subtasks, list) else proposed_subtasks
    return ex("INSERT INTO blocker_resolutions (task_id,blocker_text,responsible_type,responsible_entity,proposed_subtasks,created_at) "
              "VALUES (?,?,?,?,?,?)", (task_id, blocker_text, responsible_type, responsible_entity, p_str, now()))


def resolve_blocker(task_id):
    ex("UPDATE blocker_resolutions SET status='resolved', resolved_at=? WHERE task_id=? AND status != 'resolved'",
       (now(), task_id))


def get_active_blocker(task_id):
    row = q("SELECT * FROM blocker_resolutions WHERE task_id=? AND status != 'resolved' ORDER BY id DESC",
            (task_id,), one=True)
    if row and isinstance(row.get("proposed_subtasks"), str):
        try:
            row["proposed_subtasks"] = json.loads(row["proposed_subtasks"])
        except Exception:
            row["proposed_subtasks"] = []
    return row


# ---------------- leader approvals ----------------
def create_leader_approval(project_id, request_type, task_id, proposed_data, reason=""):
    p_str = json.dumps(proposed_data) if isinstance(proposed_data, (dict, list)) else str(proposed_data)
    return ex("INSERT INTO leader_approvals (project_id,request_type,task_id,proposed_data,reason,status,created_at) "
              "VALUES (?,?,?,?,?,?,?)", (project_id, request_type, task_id, p_str, reason, "pending", now()))


def get_pending_approvals(project_id):
    rows = q("SELECT a.*, t.title AS task_title FROM leader_approvals a "
             "LEFT JOIN tasks t ON t.id=a.task_id WHERE a.project_id=? AND a.status='pending' ORDER BY a.id DESC",
             (project_id,))
    for r in rows:
        if isinstance(r.get("proposed_data"), str):
            try:
                r["proposed_data"] = json.loads(r["proposed_data"])
            except Exception:
                pass
    return rows


def action_leader_approval(approval_id, status="approved"):
    ex("UPDATE leader_approvals SET status=?, actioned_at=? WHERE id=?", (status, now(), approval_id))


# ---------------- telegram updates deduplication ----------------
def record_telegram_update(update_id):
    """Returns True if the update is new and was recorded, False if already seen."""
    if not update_id or int(update_id) <= 1:
        return True
    try:
        ex("INSERT INTO telegram_updates (update_id, processed_at) VALUES (?,?)", (int(update_id), now()))
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------- logs ----------------
def add_event(project_id, task_id, kind, detail=None):
    ex("INSERT INTO events (ts,project_id,task_id,kind,detail) VALUES (?,?,?,?,?)",
       (now(), project_id, task_id, kind, json.dumps(detail or {}, ensure_ascii=False)))


def add_message(direction, chat_id, text, project_id=None, task_id=None):
    ex("INSERT INTO messages (ts,direction,chat_id,project_id,task_id,text) VALUES (?,?,?,?,?,?)",
       (now(), direction, str(chat_id), project_id, task_id, text))


def recent_events(pid, limit=60):
    rows = q("SELECT e.*, t.title AS task_title, m.name AS owner_name FROM events e "
             "LEFT JOIN tasks t ON t.id=e.task_id LEFT JOIN members m ON m.id=t.member_id "
             "WHERE e.project_id=? ORDER BY e.id DESC LIMIT ?", (pid, limit))
    for r in rows:
        r["detail"] = json.loads(r["detail"] or "{}")
    return rows


# ---------------- notifications & deduplication ----------------
def record_notification(project_id, task_id, member_id=None, chat_id=None, notification_type='alert', dedup_key=None,
                        status='sent', error_message=None, deduplication_key=None) -> bool:
    """Safely record a notification attempt. Returns True if recorded (new), False if duplicate."""
    actual_key = deduplication_key or dedup_key
    if not actual_key:
        return True
    try:
        ex("INSERT INTO notifications (project_id, task_id, member_id, chat_id, notification_type, deduplication_key, sent_at, status, error_message) "
           "VALUES (?,?,?,?,?,?,?,?,?)",
           (project_id, task_id, member_id, str(chat_id) if chat_id else None, notification_type, actual_key, now(), status, error_message))
        return True
    except sqlite3.IntegrityError:
        return False


def has_notification(dedup_key: str) -> bool:
    """Check if a notification with this deduplication key has already been sent."""
    if not dedup_key:
        return False
    row = q("SELECT id FROM notifications WHERE deduplication_key=?", (dedup_key,), one=True)
    return bool(row)


# ---------------- atomic orchestration operations ----------------
def activate_task_atomic(task_id: int) -> bool:
    """Atomically activate a task if it is waiting or not started.

    Returns True if this call transitioned the task to In Progress,
    False if it was already activated or in another status.
    """
    with conn() as c:
        cursor = c.execute(
            "UPDATE tasks SET status='In Progress', is_eligible=1, activated_at=?, updated_at=? "
            "WHERE id=? AND status IN ('Waiting', 'Not Started')",
            (now(), now(), task_id)
        )
        return cursor.rowcount > 0


# ---------------- audit logs ----------------
def add_audit_log(project_id, task_id, action, detail, actor_id=None, actor_name=None):
    detail_str = json.dumps(detail, ensure_ascii=False) if isinstance(detail, (dict, list)) else str(detail or "")
    ex("INSERT INTO audit_logs (ts, project_id, task_id, actor_id, actor_name, action, detail) "
       "VALUES (?,?,?,?,?,?,?)",
       (now(), project_id, task_id, actor_id, actor_name, action, detail_str))


def audit_logs_of(project_id, limit=100):
    rows = q("SELECT a.*, t.title AS task_title FROM audit_logs a "
             "LEFT JOIN tasks t ON t.id=a.task_id "
             "WHERE a.project_id=? ORDER BY a.id DESC LIMIT ?", (project_id, limit))
    for r in rows:
        if isinstance(r.get("detail"), str):
            try:
                r["detail"] = json.loads(r["detail"])
            except Exception:
                pass
    return rows

