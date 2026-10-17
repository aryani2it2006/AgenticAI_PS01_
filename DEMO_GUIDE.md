# SyncSoul AI — Complete Agentic Orchestration Demo Guide

This guide walks through every step of evaluating the **SyncSoul AI Agentic Project Orchestration System** for hackathon demonstration.

---

## 🚀 Quick Launch (Windows)

1. Open PowerShell or Command Prompt in the project folder:
   ```powershell
   cd syncsoul_orchestrator
   ```
2. Create your `.env` file from the example:
   ```powershell
   Copy-Item .env.example .env
   ```
3. Set your Telegram Bot Token in `.env`:
   ```ini
   TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
   # Optional: LLM Planning & Interpreter (deterministic rule engine works out-of-the-box):
   ANTHROPIC_API_KEY=your_claude_api_key
   ```
4. Run the application:
   ```powershell
   python run.py
   ```
5. Open the web dashboard in your browser:
   👉 **http://localhost:8080**

---

## 🧪 Automated Test Verification

Run all 67 automated tests:
```powershell
python -m unittest discover tests
```

To run only the agentic orchestration test suite:
```powershell
python -m unittest tests/test_orchestration.py
```

To run legacy compatibility tests:
```powershell
python -m unittest tests/test_legacy_compat.py
```

---

## 🎯 Complete End-to-End Workflow Walkthrough

### 1. Leader Registration & Project Setup
1. Open `http://localhost:8080`.
2. Register a leader account:
   - Name: `Dr. Sarah Chen`
   - Email: `sarah@mesh.ai`
   - Password: `password123`
3. Click **Link Telegram** in top bar to link your leader Telegram account for alerts.
4. Click **+ New Project**:
   - Name: `Autonomous Agent Mesh`
   - Goal: `Deploy production multi-agent system with verified security and API gateways`
   - Deadline: Choose a target deadline (e.g. `2030-06-30`)
   - Timezone: `UTC` or `Asia/Kolkata`

### 2. Team Member Setup with Roles & Skills
1. Click the **Team & Workload** tab on the dashboard.
2. Add members with distinct roles, skills, and weekly capacity:
   - **Kabir Dev**: Role: `Backend Engineer`, Skills: `python, api, database, backend`, Capacity: `40h`
   - **Priya Ops**: Role: `DevOps Engineer`, Skills: `docker, kubernetes, cloud, security, infra`, Capacity: `40h`
   - **Aarav UI**: Role: `Frontend Engineer`, Skills: `react, javascript, css, ui`, Capacity: `30h`
   - **Meera QA**: Role: `QA Engineer`, Skills: `testing, qa, automation`, Capacity: `25h`
3. Have team members link their Telegram by opening their unique invite links (`/start <token>`).

### 3. Agentic Goal Decomposition & Planning
1. Navigate to the **🎯 Agentic Planning** tab.
2. Enter a project goal or use the default:
   `"Deploy production multi-agent system with verified security and API gateways"`
3. Click **✨ Generate Agentic Plan**.
4. The Planning Agent analyzes the goal and decomposes it into phased tasks:
   - Foundational database & architecture schema (Wave 1)
   - Core Backend API & Inference Service (Wave 2 - runs in parallel)
   - Frontend UI & Interactive Views (Wave 2 - runs in parallel)
   - Integration & End-to-End QA Testing (Wave 3 - multi-dependency join)
   - DevOps Deployment & Signoff (Wave 4)
5. Review the proposed deliverables, acceptance criteria, recommended assignees, and transparent match scores.

### 4. Plan Approval & Atomic Activation
1. Click **✅ 1-Click Approve & Activate Plan** (or type `/approve_plan <project_id>` in Telegram).
2. The orchestrator:
   - Maps planned task indices to real database IDs.
   - Verifies no circular dependencies exist.
   - Atomically activates Wave 1 eligible tasks to `In Progress`.
   - Sends Telegram notifications to assigned members with required deliverables.
   - Keeps downstream tasks safely held in `Waiting` state.

### 5. Dependency Graph Visualizer & Parallel Execution
1. Navigate to the **📊 Dependency Graph** tab.
2. View the visual layout:
   - **Execution Waves**: Topological stages clearly separated.
   - **Parallel Tasks**: Tasks running simultaneously with zero prerequisite blockers.
   - **Waiting Tasks**: Tasks gating on upstream prerequisites.

### 6. Deliverable Reporting (Member Claim vs. Verified State)
1. When a member finishes work, they submit their deliverable:
   - Via Telegram: `/deliver 1 https://github.com/org/repo/pull/42`
   - Or on the dashboard: Click task → **Submit Deliverable**
2. Task transitions to `In Review` (Reported completion).
3. Notice that downstream tasks **remain held in Waiting state**! Self-reported claims never bypass verification gates.

### 7. Leader Deliverable Verification & Cascading Unlock
1. The leader reviews the submitted deliverable:
   - On the dashboard: Click **Verify Deliverable**
   - Or via Telegram: `/verify 1`
2. Task transitions to `Completed` (Verified complete).
3. The Orchestration Engine detects that all prerequisites for Wave 2 tasks are now complete:
   - Automatically activates eligible downstream tasks to `In Progress`.
   - Sends Telegram notifications to Wave 2 assignees.
   - Logs the verification in the `audit_logs` table.

### 8. Deliverable Rejection & Feedback Loop
1. If a deliverable fails acceptance criteria:
   - Leader clicks **Reject Deliverable** or sends `/reject_proof <id> CI lint tests failing`.
2. Task returns to `In Progress`.
3. Assignee receives immediate Telegram notification with the leader's specific rework feedback.

### 9. Blocker Reporting & Cascading Downstream Impact
1. If a member encounters a blocker:
   - Via Telegram: `"#2 stuck, waiting for compliance credentials"` or `/block 2 waiting for credentials`
2. Task status updates to `Blocked`.
3. The Orchestration Engine immediately computes all affected downstream tasks:
   - Downstream assignees are alerted via Telegram that an upstream prerequisite is blocked.
   - Leader receives an urgent Cascading Impact Alert.
4. When the blocker is resolved (`#2 credentials received, unblocked`):
   - Task returns to `In Progress` — **never** prematurely marked Complete!
   - Downstream tasks remain held in Waiting until the task is verified complete.

### 10. Audit History & Decision Log
1. Navigate to the **📜 Decision Log** tab.
2. Review the immutable chronological audit log tracking:
   - Plan approval and task instantiation
   - Atomic task activations and notifications
   - Deliverable submissions, verifications, and rejections
   - Dependency overrides and reassignments
   - Task cancellations with recorded rationale
