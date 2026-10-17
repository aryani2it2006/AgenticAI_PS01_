"""Dependency Management and Escalation System for SyncSoul AI.

Features:
- Circular dependency detection
- Prerequisite status checks
- Automatic blocking of dependent tasks if prerequisites fail or are delayed
- Escalation to leader when a prerequisite is overdue
- Automatic notification & unblocking when a prerequisite completes
"""
import datetime as dt
import json
from typing import Dict, List, Optional, Set, Tuple


def parse_dependencies(dep_val) -> List[int]:
    """Safely parse dependencies field to a list of integer task IDs."""
    if not dep_val:
        return []
    if isinstance(dep_val, list):
        return [int(x) for x in dep_val if str(x).isdigit()]
    if isinstance(dep_val, str):
        try:
            parsed = json.loads(dep_val)
            if isinstance(parsed, list):
                return [int(x) for x in parsed if str(x).isdigit()]
        except Exception:
            return [int(x.strip()) for x in dep_val.split(",") if x.strip().isdigit()]
    return []


def check_circular_dependency(all_tasks: List[dict], target_task_id: int, proposed_dependencies: List[int]) -> Tuple[bool, Optional[str]]:
    """
    Check if adding proposed_dependencies to target_task_id introduces a cycle.
    Returns (has_cycle, cycle_path_str).
    """
    # Build graph: task_id -> set of dependency task_ids
    graph: Dict[int, Set[int]] = {}
    for t in all_tasks:
        tid = t["id"]
        deps = set(parse_dependencies(t.get("dependencies")))
        graph[tid] = deps

    # Update with proposed dependencies
    graph[target_task_id] = set(proposed_dependencies)

    # DFS cycle detection
    visited: Set[int] = set()
    rec_stack: Set[int] = set()
    path: List[int] = []

    def dfs(curr: int) -> bool:
        visited.add(curr)
        rec_stack.add(curr)
        path.append(curr)

        for neighbor in graph.get(curr, set()):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in rec_stack:
                path.append(neighbor)
                return True

        rec_stack.remove(curr)
        path.pop()
        return False

    for tid in graph:
        if tid not in visited:
            if dfs(tid):
                cycle_str = " -> ".join(f"#{x}" for x in path)
                return True, cycle_str

    return False, None


def check_prerequisites(task: dict, all_tasks: List[dict]) -> dict:
    """
    Inspect the prerequisite tasks for a given task.
    Returns:
    {
        "all_completed": bool,
        "missing_dependencies": List[dict],
        "overdue_dependencies": List[dict]
    }
    """
    deps = parse_dependencies(task.get("dependencies"))
    if not deps:
        return {"all_completed": True, "missing_dependencies": [], "overdue_dependencies": []}

    tasks_by_id = {t["id"]: t for t in all_tasks}
    missing = []
    overdue = []
    today = dt.date.today()

    for dep_id in deps:
        prereq = tasks_by_id.get(dep_id)
        if not prereq:
            continue
        if prereq.get("status") not in ("Complete", "Completed"):
            missing.append(prereq)
            # Check if overdue
            due = prereq.get("due_date")
            if due:
                try:
                    d_obj = dt.date.fromisoformat(str(due).split("T")[0])
                    if d_obj < today:
                        overdue.append(prereq)
                except Exception:
                    pass

    return {
        "all_completed": len(missing) == 0,
        "missing_dependencies": missing,
        "overdue_dependencies": overdue
    }


def evaluate_all_dependencies(all_tasks: List[dict]) -> List[dict]:
    """
    Identify tasks that should be marked Blocked due to overdue or uncompleted prerequisites.
    Returns list of escalation actions to take.
    """
    escalations = []
    for t in all_tasks:
        if t.get("status") in ("Complete", "Completed", "Cancelled"):
            continue
        res = check_prerequisites(t, all_tasks)
        if res["overdue_dependencies"]:
            overdue_titles = [f"#{p['id']} ({p['title']})" for p in res["overdue_dependencies"]]
            reason = f"Blocked by overdue prerequisite(s): {', '.join(overdue_titles)}"
            escalations.append({
                "task": t,
                "reason": reason,
                "overdue_prereqs": res["overdue_dependencies"]
            })
    return escalations


def detect_cycles_in_plan(planned_tasks: List[dict]) -> Tuple[bool, Optional[str]]:
    """
    Validate a proposed plan where tasks reference dependencies by index, temporary ID, or title.
    Returns (has_cycle, cycle_path_str).
    """
    title_to_idx = {}
    for idx, t in enumerate(planned_tasks):
        key = t.get("id") if t.get("id") is not None else idx
        title = t.get("title")
        if title:
            title_to_idx[str(title).strip().lower()] = key

    graph: Dict[int, Set[int]] = {}
    title_by_idx: Dict[int, str] = {}

    for idx, t in enumerate(planned_tasks):
        key = t.get("id") if t.get("id") is not None else idx
        title_by_idx[key] = t.get("title", f"Task {key}")
        raw_deps = t.get("dependencies") or []
        deps = set()
        for d in raw_deps:
            if isinstance(d, int):
                deps.add(d)
            elif str(d).isdigit():
                deps.add(int(d))
            elif isinstance(d, str):
                d_key = d.strip().lower()
                if d_key in title_to_idx:
                    deps.add(title_to_idx[d_key])
        graph[key] = deps

    visited: Set[int] = set()
    rec_stack: Set[int] = set()
    path: List[int] = []

    def dfs(curr: int) -> bool:
        visited.add(curr)
        rec_stack.add(curr)
        path.append(curr)

        for neighbor in graph.get(curr, set()):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in rec_stack:
                path.append(neighbor)
                return True

        rec_stack.remove(curr)
        path.pop()
        return False

    for k in graph:
        if k not in visited:
            if dfs(k):
                cycle_str = " -> ".join(f"'{title_by_idx.get(x, f'Task {x}')}'" for x in path)
                return True, cycle_str

    return False, None


def find_affected_downstream_tasks(task_id: int, all_tasks: List[dict]) -> List[dict]:
    """
    Compute all tasks that directly or transitively depend on task_id.
    Useful when a task is blocked, overdue, or delayed to notify affected downstream assignees.
    """
    # Build forward dependency graph: prereq -> list of tasks that depend on it
    downstream_map: Dict[int, List[dict]] = {}
    tasks_by_id = {t["id"]: t for t in all_tasks}

    for t in all_tasks:
        deps = parse_dependencies(t.get("dependencies"))
        for d in deps:
            downstream_map.setdefault(d, []).append(t)

    affected: List[dict] = []
    visited_ids: Set[int] = set()
    queue = [task_id]

    while queue:
        curr = queue.pop(0)
        for dep_task in downstream_map.get(curr, []):
            dt_id = dep_task["id"]
            if dt_id not in visited_ids and dt_id != task_id:
                visited_ids.add(dt_id)
                # fetch fresh copy from tasks_by_id
                fresh_t = tasks_by_id.get(dt_id, dep_task)
                if fresh_t.get("status") not in ("Complete", "Completed", "Cancelled"):
                    affected.append(fresh_t)
                queue.append(dt_id)

    return affected


def analyze_parallel_and_waiting(tasks: List[dict]) -> dict:
    """
    Examine all tasks in a project and partition them into:
    - parallel_eligible: ready to run in parallel right now
    - waiting: must wait for prerequisites to complete
    - in_progress: actively running
    - in_review: reported complete, awaiting leader verification
    - completed: verified complete
    - blocked: blocked by reported obstacle or overdue prerequisite
    - waves: topological execution stages/phases
    - graph: nodes and directed edges for UI rendering
    """
    tasks_by_id = {t["id"]: t for t in tasks}
    parallel_eligible = []
    waiting = []
    active = []
    in_review = []
    blocked = []
    completed = []

    for t in tasks:
        st = t.get("status")
        if st in ("Complete", "Completed"):
            completed.append(t)
        elif st in ("In Review", "Reported"):
            in_review.append(t)
        elif st == "Blocked":
            blocked.append(t)
        elif st in ("In Progress", "Active"):
            active.append(t)
            # Active tasks with all prereqs satisfied are running in parallel
            prereqs = check_prerequisites(t, tasks)
            if prereqs["all_completed"]:
                parallel_eligible.append(t)
        else:
            # Waiting, Not Started, Pending
            prereqs = check_prerequisites(t, tasks)
            if prereqs["all_completed"]:
                parallel_eligible.append(t)
            else:
                waiting.append(t)

    # Build graph nodes and edges
    nodes = []
    edges = []
    for t in tasks:
        nodes.append({
            "id": t["id"],
            "title": t["title"],
            "status": t.get("status", "Waiting"),
            "priority": t.get("priority", "Medium"),
            "owner_name": t.get("owner_name") or "Unassigned",
            "due_date": t.get("due_datetime") or t.get("due_date"),
            "required_deliverable": t.get("required_deliverable") or "",
            "estimated_effort": t.get("estimated_effort") or 0.0
        })
        deps = parse_dependencies(t.get("dependencies"))
        for d in deps:
            if d in tasks_by_id:
                edges.append({"from": d, "to": t["id"]})

    # Compute topological waves/stages
    # Wave 0: tasks with no prerequisites in this set
    # Wave N: tasks whose prerequisites are all in Wave < N
    waves = []
    placed: Set[int] = set()
    remaining = list(tasks)

    for _ in range(len(tasks) + 1):
        if not remaining:
            break
        current_wave = []
        next_remaining = []
        for t in remaining:
            deps = set(parse_dependencies(t.get("dependencies")))
            # Only consider deps that exist within the project
            valid_deps = {d for d in deps if d in tasks_by_id}
            if valid_deps.issubset(placed):
                current_wave.append(t)
                placed.add(t["id"])
            else:
                next_remaining.append(t)
        if not current_wave:
            # Cycle or unresolved external reference: dump remainder
            if next_remaining:
                waves.append(next_remaining)
            break
        waves.append(current_wave)
        remaining = next_remaining

    wave_summaries = []
    for idx, w in enumerate(waves):
        wave_summaries.append({
            "wave_number": idx + 1,
            "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"], "owner_name": t.get("owner_name") or "Unassigned"} for t in w]
        })

    return {
        "parallel_eligible": parallel_eligible,
        "waiting": waiting,
        "active": active,
        "in_review": in_review,
        "blocked": blocked,
        "completed": completed,
        "nodes": nodes,
        "edges": edges,
        "waves": wave_summaries
    }

