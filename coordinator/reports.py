"""Progress summary, evidence-based member reports, and risk report logic.

Features:
- Exact deadline and overdue duration tracking
- Evidence-based member summaries:
  1. Verified Records (system verified, photo proof, leader confirmed)
  2. Member Claims (self-reported percentages, promises, notes)
  3. AI Inferences (detected blockers, delay reasons, proposed deadlines)
- Dependency chain and blocker analysis
- Workload and capacity distribution
"""
import datetime as dt
from typing import Dict, List, Optional, Tuple

from .db import BLOCKED, COMPLETE, COMPLETED, IN_PROGRESS, NOT_STARTED, PENDING
from .reminders import calculate_overdue_duration, parse_deadline


def days_left(task, today=None):
    today = today or dt.date.today()
    due = task.get("due_date") or task.get("due_datetime")
    if not due:
        return None
    try:
        d_str = str(due).split("T")[0]
        return (dt.date.fromisoformat(d_str) - today).days
    except Exception:
        return None


def risk(task, today=None):
    """Return (level, reason) for an unfinished task, or None if it's on track."""
    if task.get("status") in (COMPLETE, COMPLETED):
        return None
    d = days_left(task, today)
    if d is not None and d < 0:
        return "high", f"Overdue by {-d} day{'s' if d != -1 else ''}"
    if task.get("status") == BLOCKED:
        if d is not None and d <= 3:
            return "high", f"Blocked, due in {d} day{'s' if d != 1 else ''}"
        return "medium", f"Blocked: {task.get('blocker') or 'reason not recorded'}"
    if task.get("priority") == "Urgent":
        return "high", "Urgent priority task open"
    if d is not None and d <= 2:
        if task.get("status") in (PENDING, NOT_STARTED):
            return "high", f"Not started, due in {d} day{'s' if d != 1 else ''}"
        return "medium", f"Due in {d} day{'s' if d != 1 else ''}"
    return None


def summary(tasks, today=None):
    counts = {
        PENDING: 0,
        IN_PROGRESS: 0,
        BLOCKED: 0,
        COMPLETE: 0,
        "In Review": 0
    }
    for t in tasks:
        st = t.get("status")
        if st in (COMPLETE, COMPLETED):
            counts[COMPLETE] += 1
        elif st in (PENDING, NOT_STARTED):
            counts[PENDING] += 1
        elif st == "In Review":
            counts["In Review"] += 1
        elif st == BLOCKED:
            counts[BLOCKED] += 1
        else:
            counts[IN_PROGRESS] += 1

    total = len(tasks)
    at_risk = sum(1 for t in tasks if risk(t, today))
    pct = round(100 * counts[COMPLETE] / total) if total else 0
    return {"total": total, "counts": counts, "percent_complete": pct, "at_risk": at_risk}


def member_summaries(members: List[dict], tasks: List[dict], events: Optional[List[dict]] = None) -> List[dict]:
    """
    Generate evidence-based summaries distinguishing:
    1. Verified Records
    2. Member Claims
    3. AI Inferences
    """
    events = events or []
    summaries = []

    tasks_by_member = {}
    for t in tasks:
        mid = t.get("member_id")
        if mid:
            tasks_by_member.setdefault(mid, []).append(t)

    for m in members:
        mid = m["id"]
        m_tasks = tasks_by_member.get(mid, [])
        total_m_tasks = len(m_tasks)
        completed = [t for t in m_tasks if t.get("status") in (COMPLETE, COMPLETED)]
        open_tasks = [t for t in m_tasks if t.get("status") not in (COMPLETE, COMPLETED)]
        blocked_tasks = [t for t in m_tasks if t.get("status") == BLOCKED]

        # 1. Verified Records
        verified_items = []
        for t in completed:
            if t.get("verified_at") or t.get("verified_by"):
                verified_items.append(f"Task #{t['id']} \"{t['title']}\" verified complete by {t.get('verified_by', 'system')}")
            else:
                verified_items.append(f"Task #{t['id']} \"{t['title']}\" marked complete")

        # 2. Member Claims
        claimed_items = []
        for t in open_tasks:
            if t.get("progress_note"):
                claimed_items.append(f"#{t['id']}: Claimed progress note \"{t['progress_note']}\"")
            if t.get("progress", 0) > 0:
                claimed_items.append(f"#{t['id']}: Self-reported {t['progress']}% complete")

        # 3. AI Inferences
        ai_inferred = []
        for t in blocked_tasks:
            if t.get("blocker"):
                ai_inferred.append(f"#{t['id']}: AI parsed blocker \"{t['blocker']}\"")
        for t in open_tasks:
            if t.get("delay_reason"):
                ai_inferred.append(f"#{t['id']}: AI extracted delay reason \"{t['delay_reason']}\"")
            if t.get("proposed_deadline"):
                ai_inferred.append(f"#{t['id']}: AI noted proposed deadline \"{t['proposed_deadline']}\"")

        summaries.append({
            "member_id": mid,
            "name": m["name"],
            "role": m.get("role", "Member"),
            "connected": bool(m.get("chat_id") or m.get("connected")),
            "total_tasks": total_m_tasks,
            "completed_count": len(completed),
            "open_count": len(open_tasks),
            "blocked_count": len(blocked_tasks),
            "verified_records": verified_items,
            "member_claims": claimed_items,
            "ai_inferences": ai_inferred
        })

    return summaries


def blocker_chains(tasks: List[dict]) -> List[dict]:
    """Extract dependency and blocker chains."""
    chains = []
    tasks_by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        deps = t.get("dependencies") or []
        if isinstance(deps, list) and deps:
            blocking = [tasks_by_id.get(dep_id) for dep_id in deps if tasks_by_id.get(dep_id)]
            unresolved = [b for b in blocking if b.get("status") not in (COMPLETE, COMPLETED)]
            if unresolved:
                chains.append({
                    "task": t,
                    "waiting_on": unresolved,
                    "is_stalled": any(u.get("status") == BLOCKED for u in unresolved)
                })
    return chains


def status_text(project, tasks):
    s = summary(tasks)
    c = s["counts"]
    lines = [f"📊 {project}: {s['percent_complete']}% complete "
             f"({c[COMPLETE]}/{s['total']} done)",
             f"In progress: {c[IN_PROGRESS]} | Blocked: {c[BLOCKED]} | Pending: {c[PENDING]}", ""]
    icon = {PENDING: "⚪", NOT_STARTED: "⚪", IN_PROGRESS: "🔵", BLOCKED: "🔴", COMPLETE: "✅", COMPLETED: "✅", "In Review": "🟡"}
    for t in tasks:
        st = t.get("status", PENDING)
        ic = icon.get(st, "⚪")
        line = f"{ic} #{t['id']} {t['title']} — {t.get('owner_name', 'Unassigned')} [{st}]"
        if st == BLOCKED and t.get("blocker"):
            line += f"\n     ↳ blocker: {t['blocker']}"
        lines.append(line)
    return "\n".join(lines)


def risk_report_text(project, tasks):
    rows = [(t, risk(t)) for t in tasks]
    rows = [(t, r) for t, r in rows if r]
    s = summary(tasks)
    head = f"⚠️ Risk report — {project}\n{s['percent_complete']}% complete, {len(rows)} task(s) at risk"
    if not rows:
        return head + "\n\nNothing at risk right now. 🎉"
    rows.sort(key=lambda x: (x[1][0] != "high", x[0].get("due_date") or ""))
    lines = [head, ""]
    for t, (level, why) in rows:
        mark = "🔴" if level == "high" else "🟠"
        line = f"{mark} #{t['id']} {t['title']} ({t.get('owner_name', 'Unassigned')}) — {why}"
        if t.get("blocker"):
            line += f"\n     ↳ blocker: {t['blocker']}"
        lines.append(line)
    return "\n".join(lines)
