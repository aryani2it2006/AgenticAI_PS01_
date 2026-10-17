"""Blocker Detection and Automatic Subtasks Engine for SyncSoul AI.

When a blocker is reported:
1. Parses the obstacle and determines the responsible category:
   - Person: specific teammate mentioned (e.g. "waiting for Aman")
   - Resource: credentials, API keys, Figma design, server, repo access
   - Approval: client approval, PR review, lead signoff
   - Dependency / Bug: blocking bug or prerequisite task
2. Proposes actionable subtasks to eliminate the blocker.
3. Uses the Assignment Rules Engine to recommend assignees with explanations.
4. Creates a Leader Approval request so the group leader can review and authorize subtasks.
5. Tracks subtasks to completion and automatically unblocks the parent task once resolved.
"""
import re
from typing import Dict, List, Optional, Tuple

from . import assignment


PERSON_PATTERNS = [
    r"(?:waiting\s+(?:for|on)|from|contact|ask|ping)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
    r"([A-Z][a-z]+)\s+(?:ne|ko|se|didn'?t|hasn'?t|is\s+delaying|has\s+not)",
    r"(?:ask|check\s+with|talk\s+to|blocked\s+by)\s+([A-Z][a-z]+)",
]

RESOURCE_PATTERNS = [
    (r"\b(?:credentials?|password|token|api\s*key|access\s*key|auth)\b", "access/credentials"),
    (r"\b(?:aws|azure|gcp|cloud|server|database|db|vpn|ssh)\b", "infrastructure/environment"),
    (r"\b(?:design|figma|mockup|wireframe|assets?|images?)\b", "design/assets"),
    (r"\b(?:api|endpoint|backend|service|webhook)\b", "api/integration"),
    (r"\b(?:repo|github|gitlab|branch|repository)\b", "repository/code_access")
]

APPROVAL_PATTERNS = [
    r"\b(?:approval|signoff|sign-off|permission|review|pr\s*review|client\s*approval)\b",
    r"\b(?:approved|sanctioned|cleared|authorized)\b"
]


def detect_blocker_details(blocker_text: str, project_members: Optional[List[dict]] = None) -> dict:
    """
    Parse blocker text to extract obstacle type, responsible party, and root cause.
    """
    text = blocker_text.strip()
    project_members = project_members or []
    responsible_person = None
    responsible_type = "technical"
    root_cause = text

    # Check for person mentions
    for m in project_members:
        # Match by name or telegram username
        m_name = m.get("name", "")
        m_user = (m.get("telegram_username") or "").lstrip("@")
        if (m_name and re.search(rf"\b{re.escape(m_name)}\b", text, re.I)) or \
           (m_user and re.search(rf"\b{re.escape(m_user)}\b", text, re.I)):
            responsible_person = m
            responsible_type = "person"
            break

    # If no member matched, try regex person patterns
    if not responsible_person:
        for pat in PERSON_PATTERNS:
            match = re.search(pat, text)
            if match:
                candidate_name = match.group(1).strip()
                # Check if matches any member loosely
                for m in project_members:
                    if m.get("name", "").lower().startswith(candidate_name.lower()):
                        responsible_person = m
                        responsible_type = "person"
                        break
                if responsible_person:
                    break

    # Check for approval
    if responsible_type != "person":
        for pat in APPROVAL_PATTERNS:
            if re.search(pat, text, re.I):
                responsible_type = "approval"
                break

    # Check for resources
    if responsible_type not in ("person", "approval"):
        for pat, r_type in RESOURCE_PATTERNS:
            if re.search(pat, text, re.I):
                responsible_type = f"resource:{r_type}"
                break

    return {
        "responsible_type": responsible_type,
        "responsible_member": responsible_person,
        "root_cause": root_cause
    }


def propose_blocker_subtasks(task: dict, blocker_text: str, project_members: List[dict],
                             all_tasks: List[dict]) -> List[dict]:
    """
    Formulate actionable subtasks to resolve the blocker.
    Recommends assignees using the Assignment Rules Engine.
    """
    details = detect_blocker_details(blocker_text, project_members)
    r_type = details["responsible_type"]
    resp_member = details["responsible_member"]
    task_title = task.get("title", "")
    tid = task["id"]

    proposed_subtasks = []

    if r_type == "person" and resp_member:
        # Specific person identified
        proposed_subtasks.append({
            "title": f"Coordinate with {resp_member['name']} to resolve blocker for #{tid}",
            "description": f"Provide requested deliverables/access to unblock #{tid} ({task_title}): {blocker_text}",
            "recommended_member_id": resp_member["id"],
            "recommended_member_name": resp_member["name"],
            "reason": f"Explicitly identified as responsible for: {blocker_text}",
            "priority": "High",
            "estimated_effort": 2.0
        })
    elif "access/credentials" in r_type or "infrastructure" in r_type:
        # DevOps / Admin subtask
        subtask_data = {
            "title": f"Provision credentials/access for #{tid} ({task_title})",
            "description": f"Grant required environment credentials or access: {blocker_text}",
            "priority": "High",
            "estimated_effort": 1.5,
            "tags": ["devops", "security", "credentials"]
        }
        # Run assignment engine to find best DevOps / Lead member
        rec = assignment.recommend_assignee(subtask_data, project_members, all_tasks)
        m = rec.get("recommended_member")
        proposed_subtasks.append({
            "title": subtask_data["title"],
            "description": subtask_data["description"],
            "recommended_member_id": m["id"] if m else None,
            "recommended_member_name": m["name"] if m else "Leader Assignment Required",
            "reason": rec.get("explanation", "DevOps/infrastructure task matching"),
            "priority": "High",
            "estimated_effort": 1.5
        })
    elif "design/assets" in r_type:
        subtask_data = {
            "title": f"Provide design assets/specs for #{tid}",
            "description": f"Deliver design specifications/mockups: {blocker_text}",
            "priority": "High",
            "estimated_effort": 3.0,
            "tags": ["design", "ui", "ux"]
        }
        rec = assignment.recommend_assignee(subtask_data, project_members, all_tasks)
        m = rec.get("recommended_member")
        proposed_subtasks.append({
            "title": subtask_data["title"],
            "description": subtask_data["description"],
            "recommended_member_id": m["id"] if m else None,
            "recommended_member_name": m["name"] if m else "Leader Assignment Required",
            "reason": rec.get("explanation", "Design/UI task matching"),
            "priority": "High",
            "estimated_effort": 3.0
        })
    elif r_type == "approval":
        # Leader or admin approval task
        subtask_data = {
            "title": f"Review and approve deliverables for #{tid}",
            "description": f"Provide review and sign-off: {blocker_text}",
            "priority": "Urgent",
            "estimated_effort": 1.0,
            "tags": ["review", "approval", "lead"]
        }
        rec = assignment.recommend_assignee(subtask_data, project_members, all_tasks)
        m = rec.get("recommended_member")
        proposed_subtasks.append({
            "title": subtask_data["title"],
            "description": subtask_data["description"],
            "recommended_member_id": m["id"] if m else None,
            "recommended_member_name": m["name"] if m else "Leader Assignment Required",
            "reason": rec.get("explanation", "Review/approval workflow"),
            "priority": "Urgent",
            "estimated_effort": 1.0
        })
    else:
        # General technical blocker
        subtask_data = {
            "title": f"Investigate and resolve blocker on #{tid}: {blocker_text[:40]}",
            "description": f"Technical troubleshooting for: {blocker_text}",
            "priority": "High",
            "estimated_effort": 3.0,
            "tags": assignment.extract_task_skills(task_title, blocker_text)
        }
        rec = assignment.recommend_assignee(subtask_data, project_members, all_tasks)
        m = rec.get("recommended_member")
        proposed_subtasks.append({
            "title": subtask_data["title"],
            "description": subtask_data["description"],
            "recommended_member_id": m["id"] if m else None,
            "recommended_member_name": m["name"] if m else "Leader Assignment Required",
            "reason": rec.get("explanation", "Technical assistance matching"),
            "priority": "High",
            "estimated_effort": 3.0
        })

    return proposed_subtasks
