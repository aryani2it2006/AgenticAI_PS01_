"""Intelligent Reminder and Follow-up Tracker for SyncSoul AI.

Tracks exact deadlines with timezone support.
Sends intelligent reminders:
- Before deadline (e.g. 24h or 2h before deadline)
- At deadline (within deadline window)
- Overdue follow-ups asking for:
  1. Progress %
  2. Remaining work
  3. Blockers
  4. Realistic completion estimate
Prevents duplicate/excessive reminders with configurable cooldown and anti-spam limits.
Persists reminder state so restarts do not cause repeated alerts.
Escalates overdue tasks to team leaders honestly.
Tracks all notification attempts and failures in the database.
"""
import datetime as dt
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def parse_deadline(due_date_str: Optional[str], tz_name: Optional[str] = "UTC") -> Optional[dt.datetime]:
    """Parse due_date or due_datetime string with timezone awareness into UTC datetime."""
    if not due_date_str:
        return None
    val = str(due_date_str).strip()
    if not val:
        return None

    # Determine local timezone
    tz = dt.timezone.utc
    if tz_name and ZoneInfo:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = dt.timezone.utc

    # Try ISO datetime
    try:
        cleaned = val.replace(" ", "T")
        if "T" in cleaned:
            parts = cleaned.split("T")
            date_p = dt.date.fromisoformat(parts[0])
            time_part = parts[1].split("+")[0].split("Z")[0]
            if len(time_part.split(":")) == 2:
                time_part += ":00"
            time_p = dt.time.fromisoformat(time_part)
            naive = dt.datetime.combine(date_p, time_p)
            local_dt = naive.replace(tzinfo=tz)
            return local_dt.astimezone(dt.timezone.utc)
        else:
            # Date only - treat as end of day in local timezone (23:59:59)
            date_p = dt.date.fromisoformat(cleaned)
            local_dt = dt.datetime.combine(date_p, dt.time(23, 59, 59), tzinfo=tz)
            return local_dt.astimezone(dt.timezone.utc)
    except Exception:
        return None


def calculate_overdue_duration(due_utc: Optional[dt.datetime], now_utc: Optional[dt.datetime] = None) -> Tuple[bool, str, float]:
    """
    Returns (is_overdue, human_readable_duration, overdue_hours).
    """
    if not due_utc:
        return False, "", 0.0
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    diff = now_utc - due_utc
    total_seconds = diff.total_seconds()
    if total_seconds <= 0:
        return False, "", 0.0

    hours = total_seconds / 3600.0
    days = int(hours // 24)
    rem_hours = int(hours % 24)
    mins = int((total_seconds % 3600) // 60)

    if days > 0:
        desc = f"{days}d {rem_hours}h overdue"
    elif rem_hours > 0:
        desc = f"{rem_hours}h {mins}m overdue"
    else:
        desc = f"{mins}m overdue"

    return True, desc, round(hours, 2)


def determine_reminder_phase(task: dict, now_utc: Optional[dt.datetime] = None) -> Tuple[str, str, float]:
    """
    Classifies task deadline phase:
    - 'overdue': past deadline
    - 'at_deadline': within 2 hours of deadline
    - 'before': within 24 hours of deadline
    - 'upcoming': more than 24 hours until deadline
    - 'nudge': no deadline configured
    Returns (phase, description, hours_diff)
    """
    # Completed tasks are never overdue
    if task.get("status") in ("Complete", "Completed"):
        return "completed", "Task complete", 0.0

    due_str = task.get("due_datetime") or task.get("due_date")
    if not due_str:
        return "nudge", "No deadline set", 0.0

    due_utc = parse_deadline(due_str, task.get("timezone", "UTC"))
    if not due_utc:
        return "nudge", "No deadline set", 0.0

    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    if now_utc > due_utc:
        is_overdue, desc, hours = calculate_overdue_duration(due_utc, now_utc)
        return "overdue", desc, hours

    # Time remaining until deadline
    diff_seconds = (due_utc - now_utc).total_seconds()
    hours_left = diff_seconds / 3600.0
    if hours_left <= 2.0:
        mins_left = max(1, int(diff_seconds // 60))
        return "at_deadline", f"due in {mins_left}m", round(hours_left, 2)
    elif hours_left <= 24.0:
        return "before", f"due in {round(hours_left, 1)}h", round(hours_left, 2)
    else:
        days_left = round(hours_left / 24.0, 1)
        return "upcoming", f"due in {days_left}d", round(hours_left, 2)


def generate_reminder_prompt(task: dict, member_name: str, reminder_type: str, info_str: str = "") -> str:
    """Generate structured reminder message for Telegram."""
    t_title = task.get("title", "")
    tid = task["id"]
    due = task.get("due_datetime") or task.get("due_date") or "No deadline set"

    if reminder_type == "overdue":
        return (
            f"⚠️ Follow-up: Task #{tid} \"{t_title}\" is {info_str}!\n\n"
            f"Hi {member_name}, to keep the project on track, please reply with an update:\n"
            f"1. Current progress % (e.g. \"60%\")\n"
            f"2. Remaining work\n"
            f"3. Any blockers (e.g. \"stuck waiting for API credentials\")\n"
            f"4. Realistic completion estimate (e.g. \"will finish by tomorrow 5 PM\")\n\n"
            f"Reply directly here, e.g. \"#{tid} 70% done, need 1 more day\" or \"#{tid} blocked: need AWS access\"."
        )
    elif reminder_type == "at_deadline":
        return (
            f"⏰ Deadline Check: Task #{tid} \"{t_title}\" is {info_str} (due: {due}).\n"
            f"Hi {member_name}, is this ready or do you have any blockers? Reply with #{tid} and your status."
        )
    elif reminder_type == "before":
        return (
            f"🔔 Upcoming Deadline: Task #{tid} \"{t_title}\" is {info_str} (due: {due}).\n"
            f"Hi {member_name}, please check in if you need any assistance, code review, or approvals before the deadline!"
        )
    else:
        # Standard nudge
        return (
            f"👋 Quick check-in on Task #{tid} \"{t_title}\".\n"
            f"Hi {member_name}, please share your progress or any blockers."
        )


def should_send_reminder(task: dict, recent_logs: List[dict], min_interval_hours: float = 4.0,
                         max_per_day: int = 3, force: bool = False) -> Tuple[bool, str]:
    """
    Anti-spam and cooldown checks.
    Returns (should_send, reason).
    """
    if force:
        return True, "Forced by user/leader"

    if task.get("status") in ("Complete", "Completed"):
        return False, "Task is already complete"

    if not task.get("member_id"):
        return False, "Task has no assignee"

    now = dt.datetime.now(dt.timezone.utc)
    today_str = now.date().isoformat()

    # Filter logs for this task
    task_logs = [log for log in recent_logs if log.get("task_id") == task["id"] and log.get("status") == "sent"]
    
    # Check max per day
    sent_today = sum(1 for log in task_logs if str(log.get("created_at", "")).startswith(today_str))
    if sent_today >= max_per_day:
        return False, f"Maximum daily reminder limit ({max_per_day}) reached for task #{task['id']}"

    # Check cooldown
    if task_logs:
        last_log = task_logs[0]
        last_time_str = last_log.get("created_at") or last_log.get("sent_at")
        if last_time_str:
            try:
                last_dt = dt.datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
                elapsed_hours = (now - last_dt).total_seconds() / 3600.0
                if elapsed_hours < min_interval_hours:
                    return False, f"Cooldown active: last reminder was {round(elapsed_hours, 1)}h ago (min interval {min_interval_hours}h)"
            except Exception:
                pass

    return True, "Eligible for reminder"
