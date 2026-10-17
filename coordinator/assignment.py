"""Assignment Rules Engine for SyncSoul AI.

Evaluates candidates based on:
1. Skills and role match (skills matching task tags and keywords)
2. Role appropriateness (Lead, Senior, Developer, QA, DevOps, Designer)
3. Project membership and permissions
4. Current workload and availability (active tasks, estimated hours, capacity)
5. Task priority and complexity affinity
6. Explicit leader preferences
7. Dependencies and deadlines

Provides transparent score breakdowns and explanations.
Ensures leader approval is required for reassignments or automatic recommendations.
"""
import re
from typing import Dict, List, Optional, Tuple

ROLE_WEIGHTS = {
    "leader": {"architecture": 1.5, "lead": 1.5, "critical": 1.3},
    "admin": {"management": 1.4, "review": 1.3},
    "senior": {"architecture": 1.3, "complex": 1.3, "review": 1.2},
    "developer": {"code": 1.3, "feature": 1.3, "bug": 1.2, "implementation": 1.3},
    "backend engineer": {"code": 1.3, "backend": 1.5, "database": 1.4, "api": 1.4, "python": 1.4},
    "devops engineer": {"deploy": 1.5, "cloud": 1.5, "infra": 1.5, "docker": 1.5, "k8s": 1.5, "aws": 1.5},
    "frontend engineer": {"frontend": 1.5, "ui": 1.5, "react": 1.5, "css": 1.4, "javascript": 1.4},
    "devops": {"deploy": 1.5, "cloud": 1.5, "aws": 1.5, "docker": 1.5, "infra": 1.5, "credentials": 1.4, "access": 1.4},
    "qa": {"test": 1.5, "qa": 1.5, "verification": 1.4, "bug": 1.3, "testing": 1.4},
    "qa engineer": {"test": 1.5, "qa": 1.5, "automation": 1.4, "testing": 1.4},
    "designer": {"ui": 1.5, "ux": 1.5, "design": 1.5, "figma": 1.5, "frontend": 1.2},
    "member": {"general": 1.0}
}

SKILL_KEYWORDS = {
    "python": [r"\bpython\b", r"\bdjango\b", r"\bflask\b", r"\bfastapi\b", r"\bbackend\b"],
    "backend": [r"\bbackend\b", r"\bapi\b", r"\bmodels\b", r"\bserver\b"],
    "javascript": [r"\bjavascript\b", r"\bjs\b", r"\breact\b", r"\bnode\b", r"\bfrontend\b", r"\bvue\b"],
    "frontend": [r"\bfrontend\b", r"\bui\b", r"\bux\b", r"\breact\b", r"\bcss\b"],
    "database": [r"\bsql\b", r"\bdatabase\b", r"\bdb\b", r"\bpostgres\b", r"\bpostgresql\b", r"\bmongodb\b", r"\bmigration\b", r"\bindexes\b", r"\bquery\b"],
    "api": [r"\bapi\b", r"\brest\b", r"\bendpoint\b", r"\bwebhook\b", r"\bintegration\b"],
    "devops": [r"\baws\b", r"\bdocker\b", r"\bk8s\b", r"\bkubernetes\b", r"\bci/cd\b", r"\bdeploy\b", r"\bcloud\b", r"\bserver\b", r"\bcredentials\b", r"\baccess\b", r"\bterraform\b", r"\beks\b", r"\binfra\b", r"\binfrastructure\b"],
    "cloud": [r"\bcloud\b", r"\baws\b", r"\bgcp\b", r"\bazure\b", r"\beks\b", r"\bterraform\b"],
    "infra": [r"\binfra\b", r"\binfrastructure\b", r"\bterraform\b", r"\bcluster\b"],
    "kubernetes": [r"\bkubernetes\b", r"\bk8s\b", r"\beks\b"],
    "docker": [r"\bdocker\b", r"\bcontainer\b"],
    "qa": [r"\btest\b", r"\bqa\b", r"\btesting\b", r"\bverify\b", r"\be2e\b", r"\bunit test\b", r"\bautomation\b"],
    "testing": [r"\btest\b", r"\btesting\b", r"\be2e\b"],
    "design": [r"\bdesign\b", r"\bfigma\b", r"\bui\b", r"\bux\b", r"\bmockup\b", r"\bcss\b", r"\bstyle\b"],
    "ui": [r"\bui\b", r"\bux\b", r"\bfrontend\b", r"\bcss\b"],
    "security": [r"\bauth\b", r"\bsecurity\b", r"\btoken\b", r"\bpermission\b", r"\biam\b", r"\bkey\b", r"\bcertificate\b"],
    "blockchain": [r"\bblockchain\b", r"\bsolidity\b", r"\bethereum\b", r"\bsmart contract\b", r"\bweb3\b"],
    "solidity": [r"\bsolidity\b", r"\bsmart contract\b"]
}


def extract_task_skills(task_title: str, task_desc: str = "", tags: Optional[List[str]] = None) -> List[str]:
    """Identify required skills from task title, description, and tags."""
    found = set()
    if tags:
        for t in tags:
            found.add(str(t).lower().strip())
    
    text = f"{task_title} {task_desc}".lower()
    for skill, patterns in SKILL_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, text, re.I):
                found.add(skill)
                break
    return sorted(found)


def evaluate_candidate(member: dict, task: dict, active_tasks_for_member: List[dict],
                       project_config: Optional[dict] = None) -> Tuple[float, str, dict]:
    """
    Score a member for a given task (0 - 100).
    Returns (score, explanation, breakdown).
    """
    project_config = project_config or {}
    breakdown = {}
    notes = []

    # 1. Availability check
    m_status = member.get("status", "Active")
    if m_status == "On Leave":
        return 0.0, "Member is currently on leave", {"availability": 0}
    
    availability_score = 25.0
    if m_status == "Busy":
        availability_score = 10.0
        notes.append("Status is Busy (-15)")
    breakdown["availability"] = availability_score

    # 2. Workload and Capacity
    capacity = float(member.get("workload_capacity") or 40.0)
    current_open_tasks = len(active_tasks_for_member)
    current_hours = sum(float(t.get("estimated_effort") or 4.0) for t in active_tasks_for_member)
    
    # Capacity percentage
    load_ratio = current_hours / capacity if capacity > 0 else 1.0
    workload_score = max(0.0, 30.0 * (1.0 - min(load_ratio, 1.0)))
    
    if current_open_tasks == 0:
        notes.append("No active tasks (full availability)")
    elif load_ratio > 0.8:
        notes.append(f"High workload ({current_open_tasks} tasks, {round(current_hours, 1)}h / {capacity}h)")
    else:
        notes.append(f"Manageable load ({current_open_tasks} tasks, {round(current_hours, 1)}h / {capacity}h)")
    breakdown["workload"] = round(workload_score, 1)

    # 3. Skills Match
    task_skills = extract_task_skills(
        task.get("title", ""),
        task.get("description", ""),
        task.get("tags") if isinstance(task.get("tags"), list) else None
    )
    
    member_skills = member.get("skills") or []
    if isinstance(member_skills, str):
        import json
        try:
            member_skills = json.loads(member_skills)
        except Exception:
            member_skills = [s.strip().lower() for s in member_skills.split(",") if s.strip()]
    member_skills_set = {str(s).lower().strip() for s in member_skills}

    skills_score = 15.0  # Base score if no specific skills required
    matched_skills = []
    if task_skills:
        matched = [s for s in task_skills if s in member_skills_set or any(s in ms or ms in s for ms in member_skills_set)]
        matched_skills = matched
        if matched:
            match_ratio = len(matched) / len(task_skills)
            skills_score = 30.0 * match_ratio
            notes.append(f"Skills matched: {', '.join(matched)}")
        else:
            skills_score = 0.0
            notes.append(f"Missing required skills ({', '.join(task_skills)})")
    else:
        if member_skills_set:
            notes.append("General task (no specialized skill required)")
        else:
            skills_score = 5.0
            notes.append("No recorded skills for candidate")
    breakdown["skills"] = round(skills_score, 1)

    # 4. Role Fit
    role = (member.get("role") or "Member").lower()
    priority = (task.get("priority") or "Medium").lower()
    role_score = 10.0
    
    if role in ("leader", "admin") and priority in ("urgent", "high"):
        role_score = 15.0
        notes.append("Senior/Leader priority handling")
    elif ("devops" in role or "infra" in role) and any(s in ("devops", "cloud", "infra", "kubernetes", "docker", "aws", "security", "api") for s in task_skills):
        role_score = 15.0
        notes.append("DevOps role alignment")
    elif ("backend" in role or "developer" in role) and any(s in ("backend", "python", "database", "api") for s in task_skills):
        role_score = 15.0
        notes.append("Backend role alignment")
    elif ("qa" in role or "test" in role) and any(s in ("qa", "testing", "api") for s in task_skills):
        role_score = 15.0
        notes.append("QA role alignment")
    elif ("design" in role or "frontend" in role) and any(s in ("design", "ui", "frontend", "javascript") for s in task_skills):
        role_score = 15.0
        notes.append("Design/Frontend role alignment")
    breakdown["role"] = round(role_score, 1)

    # 5. Leader Preference
    pref_score = 0.0
    preferred_members = project_config.get("preferred_members", [])
    if member.get("id") in preferred_members or member.get("name") in preferred_members:
        pref_score = 5.0
        notes.append("Leader preference bonus (+5)")
    breakdown["preference"] = pref_score

    total_score = round(availability_score + workload_score + skills_score + role_score + pref_score, 1)
    explanation = f"{member.get('name')} (Score {total_score}/100): " + "; ".join(notes)
    return total_score, explanation, breakdown


def recommend_assignee(task: Optional[dict] = None,
                       members: Optional[List[dict]] = None,
                       all_tasks: Optional[List[dict]] = None,
                       project_config: Optional[dict] = None,
                       title: Optional[str] = None,
                       description: Optional[str] = None,
                       **kwargs) -> dict:
    """
    Select the best assignee for a task based on the assignment rules.
    Accepts task dictionary, positional args, or keyword arguments (title, description, members).
    """
    task = dict(task or {})
    if title:
        task["title"] = title
    if description:
        task["description"] = description
    for k, v in kwargs.items():
        task[k] = v

    members = members or []
    all_tasks = all_tasks or []
    project_config = project_config or {}

    if not members:
        return {
            "recommended_member": None,
            "recommended_id": None,
            "recommended_name": None,
            "score": 0.0,
            "explanation": "No members exist in this project to assign.",
            "rankings": [],
            "requires_leader_approval": True,
            "is_uncertain": True,
            "uncertain_reason": "No registered project members. Leader must invite or add team members."
        }

    # Group active tasks by member
    tasks_by_member = {}
    for t in all_tasks:
        if t.get("status") not in ("Complete", "Completed", "Cancelled") and t.get("member_id"):
            tasks_by_member.setdefault(t["member_id"], []).append(t)

    rankings = []
    for m in members:
        m_tasks = tasks_by_member.get(m["id"], [])
        score, explanation, breakdown = evaluate_candidate(m, task, m_tasks, project_config)
        rankings.append({
            "member_id": m["id"],
            "name": m["name"],
            "role": m.get("role", "Member"),
            "score": score,
            "explanation": explanation,
            "breakdown": breakdown,
            "open_tasks": len(m_tasks)
        })

    rankings.sort(key=lambda r: r["score"], reverse=True)
    best = rankings[0] if rankings else None

    # Check if task has specialized skill requirement and whether candidates match
    task_skills = extract_task_skills(
        task.get("title", ""),
        task.get("description", ""),
        task.get("tags") if isinstance(task.get("tags"), list) else None
    )

    is_uncertain = False
    uncertain_reason = None

    # Check if ANY member has the required skills
    any_member_has_skills = False
    if task_skills:
        for m in members:
            m_s = m.get("skills") or []
            if isinstance(m_s, str):
                import json
                try:
                    m_s = json.loads(m_s)
                except Exception:
                    m_s = [s.strip().lower() for s in m_s.split(",") if s.strip()]
            m_set = {str(s).lower().strip() for s in m_s}
            if any(ts in m_set or any(ts in ms or ms in ts for ms in m_set) for ts in task_skills):
                any_member_has_skills = True
                break

    if task_skills and not any_member_has_skills:
        is_uncertain = True
        uncertain_reason = f"No member has required skills ({', '.join(task_skills)}) for this task. Leader assignment required."
        recommended = None
        score = best["score"] if best else 0.0
        explanation = uncertain_reason
    elif not best or best["score"] < 35.0:
        is_uncertain = True
        uncertain_reason = "No suitable candidate found meeting qualification threshold (minimum score 35). Leader assignment required."
        recommended = None
        score = best["score"] if best else 0.0
        explanation = uncertain_reason
    else:
        # Check if candidate has empty skills
        best_member_obj = next(m for m in members if m["id"] == best["member_id"])
        cand_skills = best_member_obj.get("skills") or []
        if isinstance(cand_skills, str):
            import json
            try:
                cand_skills = json.loads(cand_skills)
            except Exception:
                cand_skills = [s.strip() for s in cand_skills.split(",") if s.strip()]

        if not cand_skills:
            is_uncertain = True
            uncertain_reason = f"Candidate '{best['name']}' has no recorded skills. Leader assignment required."
        elif task_skills and best["breakdown"].get("skills", 0) <= 5.0:
            is_uncertain = True
            uncertain_reason = f"Candidate '{best['name']}' lacks recorded skills: {', '.join(task_skills)}. Confirmation needed."

        recommended = best_member_obj
        score = best["score"]
        explanation = best["explanation"]

    return {
        "recommended_member": recommended,
        "recommended_id": recommended["id"] if recommended else None,
        "recommended_name": recommended["name"] if recommended else None,
        "score": score,
        "explanation": explanation,
        "rankings": rankings,
        "requires_leader_approval": True,
        "is_uncertain": is_uncertain,
        "uncertain_reason": uncertain_reason
    }

