"""Planning Agent for SyncSoul Agentic Project Orchestration System.

Converts high-level project goals into structured, phased task plans:
- Titles, descriptions, priority, deadlines, estimated effort
- Required deliverables (e.g. PR link, Swagger doc, test report)
- Explicit acceptance criteria checklist
- Dependency graphs with parallel execution stages and multi-dependency joins
- Assignee recommendations using recorded skills, roles, and workload
- Uncertainty flagging without inventing missing member information
- Circular dependency verification
- LLM integration when configured with deterministic fallback logic
"""
import datetime as dt
import json
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from . import assignment, config, dependencies


PLANNER_SYSTEM_PROMPT = """You are the SyncSoul AI Planning Agent.
You convert high-level project goals into realistic, structured task plans for human team members.
Important constraints:
1. Human team members perform the work; the agent plans, coordinates, monitors, and unlocks dependencies.
2. Return a valid JSON array of 4 to 6 tasks.
3. Every task must have:
   - "title": short descriptive string
   - "description": clear technical objective
   - "role_needed": Developer, DevOps, QA, Designer, or Leader
   - "skills_needed": list of string skill tags (e.g. ["python", "api"], ["javascript", "design"])
   - "priority": Urgent, High, Medium, or Low
   - "estimated_effort": float estimated hours (e.g. 6.0)
   - "required_deliverable": concrete deliverable required from assignee (e.g. "PR link with API endpoints")
   - "acceptance_criteria": list of 2 to 4 verifiable acceptance strings
   - "dependencies": list of 0-based integer task indices that must be verified complete first
4. Ensure tasks have a parallel structure: foundational tasks first (index 0), then backend and frontend tasks in parallel (both depending only on index 0), followed by QA/Integration depending on both, and final deployment.
Return JSON ONLY, no surrounding markdown or explanation.
"""


def _query_llm_plan(goal: str) -> Optional[List[dict]]:
    """Attempt to generate structured plan using Anthropic Claude API if configured."""
    if not config.ANTHROPIC_API_KEY:
        return None

    url = "https://api.anthropic.com/v1/messages"
    prompt = f"Project Goal:\n{goal}\n\nGenerate structured tasks following the system specifications."
    payload = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": 2048,
        "system": PLANNER_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}]
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            res = json.loads(response.read().decode("utf-8"))
            content = res["content"][0]["text"].strip()
            # Extract JSON block
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            raw_tasks = json.loads(content.strip())
            if isinstance(raw_tasks, list) and len(raw_tasks) >= 2:
                return raw_tasks
    except Exception as e:
        print(f"[planning agent] LLM fallback ({e}); using deterministic rules")
    return None


def _deterministic_decompose(goal: str, target_deadline: Optional[str] = None) -> List[dict]:
    """
    Deterministic rule-based planning engine that decomposes goals into structured, phased tasks.
    Analyzes goal taxonomy (Web, API, Database, Mobile, AI/ML, DevOps, Security) to tailor deliverables.
    """
    text = (goal or "").lower()
    is_mobile = any(w in text for w in ("mobile", "android", "ios", "react native", "flutter"))
    is_ai = any(w in text for w in ("ai", "llm", "agent", "classifier", "nlp", "model", "vision"))
    is_security = any(w in text for w in ("auth", "security", "jwt", "permission", "rbac", "encrypt"))
    is_ecommerce = any(w in text for w in ("ecommerce", "shop", "cart", "checkout", "payment", "stripe"))

    base_tasks = []

    # Phase 1: Foundation & Data Architecture
    if is_ai:
        t0_title = "Data Architecture & Pipeline Specification"
        t0_desc = f"Define data schemas, model evaluation metrics, and API contracts for: {goal[:80]}"
        t0_deliv = "Architecture specification document & schema definitions"
        t0_ac = ["Data input/output schemas documented", "Model evaluation criteria established"]
    else:
        t0_title = "Architecture & Data Schema Modeling"
        t0_desc = f"Design relational schemas, table constraints, and migration scripts for: {goal[:80]}"
        t0_deliv = "Database migration scripts & entity-relationship specification"
        t0_ac = ["All required entities and foreign keys modeled", "Schema migrations execute with zero errors"]

    base_tasks.append({
        "title": t0_title,
        "description": t0_desc,
        "role_needed": "Developer",
        "skills_needed": ["database", "python", "api"],
        "priority": "High",
        "estimated_effort": 6.0,
        "required_deliverable": t0_deliv,
        "acceptance_criteria": t0_ac,
        "dependencies": []
    })

    # Phase 2A: Core Backend API & Services (Runs in parallel with Phase 2B)
    if is_ai:
        t1_title = "Model Integration & Inference API Service"
        t1_desc = "Implement inference pipeline, prompt templates, and RESTful prediction endpoints."
        t1_deliv = "Inference API endpoints with unit tests"
        t1_ac = ["Inference latency meets benchmarks", "Input validation prevents malformed requests"]
    elif is_ecommerce:
        t1_title = "Catalog & Transaction Processing Service"
        t1_desc = "Implement catalog queries, cart calculations, and transaction payment endpoints."
        t1_deliv = "Transaction and order REST API endpoints"
        t1_ac = ["Cart operations handle concurrent updates correctly", "Payment gateway integration passes sandbox tests"]
    else:
        t1_title = "Core Backend API & Business Logic Endpoints"
        t1_desc = "Implement API endpoints, business logic validations, and database access queries."
        t1_deliv = "Git PR containing REST API endpoints and integration tests"
        t1_ac = ["All CRUD endpoints return expected status codes", "Endpoint integration tests achieve >80% coverage"]

    base_tasks.append({
        "title": t1_title,
        "description": t1_desc,
        "role_needed": "Developer",
        "skills_needed": ["python", "api", "database"],
        "priority": "High",
        "estimated_effort": 8.0,
        "required_deliverable": t1_deliv,
        "acceptance_criteria": t1_ac,
        "dependencies": [0]
    })

    # Phase 2B: Frontend UI / Client Views (Runs in PARALLEL with Task 1!)
    if is_mobile:
        t2_title = "Mobile App Views & Client Screens"
        t2_desc = "Develop mobile application screens, navigation stack, and local state handling."
        t2_deliv = "Mobile client build / preview with functional UI flow"
        t2_ac = ["App renders cleanly on test viewport sizes", "Client handles offline or network retry states"]
        t2_skills = ["javascript", "mobile", "ui"]
        t2_role = "Developer"
    else:
        t2_title = "Frontend Dashboard & Interactive UI Components"
        t2_desc = "Develop responsive user interface, design system tokens, and interactive views."
        t2_deliv = "Interactive dashboard views connected to mock/live data"
        t2_ac = ["UI passes responsive layout checks across desktop and mobile", "Components adhere to design system guidelines"]
        t2_skills = ["javascript", "design", "ui"]
        t2_role = "Designer"

    base_tasks.append({
        "title": t2_title,
        "description": t2_desc,
        "role_needed": t2_role,
        "skills_needed": t2_skills,
        "priority": "High",
        "estimated_effort": 8.0,
        "required_deliverable": t2_deliv,
        "acceptance_criteria": t2_ac,
        "dependencies": [0]  # Runs in parallel with Task 1!
    })

    # Phase 3: Integration & End-to-End QA Testing (Multi-dependency: depends on Task 1 and Task 2)
    base_tasks.append({
        "title": "Integration, End-to-End Testing & QA Verification",
        "description": "Execute comprehensive end-to-end integration tests between frontend and backend services.",
        "role_needed": "QA",
        "skills_needed": ["qa", "testing", "api"],
        "priority": "Medium",
        "estimated_effort": 6.0,
        "required_deliverable": "Automated test suite report with passing end-to-end test run",
        "acceptance_criteria": [
            "All end-to-end user workflows execute without errors",
            "Regression suite completes with zero failing tests"
        ],
        "dependencies": [1, 2]  # Multi-dependency join
    })

    # Phase 4: Deployment, DevOps Provisioning & Production Signoff
    base_tasks.append({
        "title": "CI/CD Deployment, Monitoring & Production Signoff",
        "description": "Configure deployment environments, automated build checks, logging, and leader verification.",
        "role_needed": "DevOps",
        "skills_needed": ["devops", "cloud", "security"],
        "priority": "Urgent",
        "estimated_effort": 4.0,
        "required_deliverable": "Live deployment URL & verified configuration sign-off",
        "acceptance_criteria": [
            "Application successfully running on target host",
            "Health check endpoint responds with 200 OK",
            "Team leader signs off on completion checklist"
        ],
        "dependencies": [3]
    })

    return base_tasks


def generate_plan(project: dict, members: List[dict], goal: Optional[str] = None,
                  target_deadline: Optional[str] = None, all_tasks: Optional[List[dict]] = None) -> dict:
    """
    Planning Agent main entrypoint:
    1. Takes high-level project goal
    2. Decomposes into structured tasks (via LLM or deterministic rules)
    3. Analyzes dependencies (detects circular loops, assigns parallel waves)
    4. Runs Assignment Rules Engine to recommend assignees without hallucinating
    5. Flags uncertain assignments for leader confirmation
    6. Returns structured proposed plan awaiting leader approval
    """
    effective_goal = (goal or project.get("goal") or project.get("description") or "Complete project deliverables").strip()
    deadline = target_deadline or project.get("deadline")
    all_tasks = all_tasks or []

    # 1. Decompose goal
    raw_tasks = _query_llm_plan(effective_goal)
    source = "llm" if raw_tasks else "deterministic_rules"
    if not raw_tasks:
        raw_tasks = _deterministic_decompose(effective_goal, deadline)

    # 2. Cycle detection on proposed plan
    has_cycle, cycle_str = dependencies.detect_cycles_in_plan(raw_tasks)
    if has_cycle:
        # Resolve cycles safely by stripping back-edges
        for t in raw_tasks:
            t["dependencies"] = [d for d in t.get("dependencies", []) if d < raw_tasks.index(t)]

    # 3. Calculate target deadlines per task
    today = dt.date.today()
    total_days = 14
    if deadline:
        try:
            d_obj = dt.date.fromisoformat(str(deadline).split("T")[0])
            diff = (d_obj - today).days
            if diff > 0:
                total_days = diff
        except Exception:
            pass

    # 4. Map and evaluate each planned task
    structured_tasks = []
    num_tasks = len(raw_tasks)

    for idx, t in enumerate(raw_tasks):
        title = t.get("title", f"Task {idx+1}")
        desc = t.get("description", "")
        prio = t.get("priority", "Medium")
        effort = float(t.get("estimated_effort") or 4.0)
        skills = t.get("skills_needed") or assignment.extract_task_skills(title, desc)
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]
        deliv = t.get("required_deliverable") or "Completed deliverable submission"
        ac = t.get("acceptance_criteria") or ["All acceptance criteria verified"]
        if isinstance(ac, str):
            ac = [ac]

        raw_deps = t.get("dependencies") or []
        parsed_deps = []
        for d in raw_deps:
            if isinstance(d, int) and 0 <= d < idx:
                parsed_deps.append(d)
            elif str(d).isdigit() and 0 <= int(d) < idx:
                parsed_deps.append(int(d))

        # Staggered deadline calculation
        task_day_offset = max(1, int(total_days * ((idx + 1) / max(num_tasks, 1))))
        task_due = (today + dt.timedelta(days=task_day_offset)).isoformat()

        # Run Assignment Rules Engine
        task_spec = {
            "title": title,
            "description": desc,
            "priority": prio,
            "tags": skills,
            "estimated_effort": effort
        }
        rec = assignment.recommend_assignee(task_spec, members, all_tasks)

        rec_member = rec.get("recommended_member")
        is_uncertain = rec.get("is_uncertain", False)
        uncertain_reason = rec.get("uncertain_reason")

        structured_tasks.append({
            "plan_index": idx,
            "title": title,
            "description": desc,
            "priority": prio,
            "estimated_effort": effort,
            "tags": skills,
            "due_date": task_due,
            "dependencies": parsed_deps,
            "required_deliverable": deliv,
            "acceptance_criteria": ac,
            "recommended_member_id": rec_member["id"] if rec_member else None,
            "recommended_member_name": rec_member["name"] if rec_member else None,
            "assignment_score": rec.get("score", 0.0),
            "assignment_explanation": rec.get("explanation", ""),
            "assignment_uncertain": is_uncertain,
            "uncertain_reason": uncertain_reason,
            "can_run_parallel": len(parsed_deps) == 0 or (idx > 0 and len(parsed_deps) == 1 and parsed_deps == raw_tasks[idx-1].get("dependencies"))
        })

    # 5. Dependency wave calculation for display
    parallel_waves = []
    placed = set()
    for _ in range(len(structured_tasks) + 1):
        wave = [t for t in structured_tasks if t["plan_index"] not in placed and set(t["dependencies"]).issubset(placed)]
        if not wave:
            break
        parallel_waves.append(wave)
        for t in wave:
            placed.add(t["plan_index"])

    return {
        "project_id": project["id"],
        "project_name": project["name"],
        "goal": effective_goal,
        "source": source,
        "tasks": structured_tasks,
        "total_tasks": len(structured_tasks),
        "parallel_waves": len(parallel_waves),
        "status": "proposed",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    }
