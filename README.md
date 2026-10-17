# SyncSoul AI — Agentic Project Orchestration System

**SyncSoul AI** is an enterprise-grade Agentic Project Orchestration System featuring:
- Autonomous goal-to-task decomposition via LLM or deterministic fallback
- Explicit verification gates (reported completion vs. verified completion)
- Automated parallel wave activation & cascading dependency unlocking
- Skills & workload-based assignment rules with uncertainty detection
- Multi-dependency join gating and circular dependency rejection
- Downstream impact alerts for blocked or overdue prerequisites
- Native Telegram bot interface (multilingual: English, Hindi, Hinglish)
- Real-time HTML/CSS/JavaScript web dashboard with dependency graph visualizer
- Zero-dependency Python standard library & SQLite architecture

```
Leader registers → creates project & goal → sets team with skills & capacity
      ↓
Agentic Planning Engine decomposes goal into phased tasks, deliverables, and acceptance criteria
      ↓
Leader reviews & 1-click approves plan on Dashboard or Telegram (/approve_plan)
      ↓
Wave 1: Parallel Tasks activate concurrently → Assignees notified instantly via Telegram
      ↓                                                                          ↓
Leader verifies deliverable on Dashboard / Telegram  ←  Member submits deliverable via /deliver or dashboard
      ↓
Prerequisites satisfied → Downstream tasks unlock automatically & notify next wave assignees
```

---

## 🌟 Core System Capabilities

### 1. Goal Decomposition & Planning Agent
- Converts high-level project goals into phased, structured tasks containing:
  - Technical title & objective description
  - Required deliverable (PR link, Swagger doc, test run, schema migration)
  - Explicit acceptance criteria checklist
  - Topological dependency links
  - Assignee recommendations with scoring breakdown
- Uses Anthropic Claude LLM when configured (`ANTHROPIC_API_KEY`) and deterministic rule decomposition otherwise.
- Flags uncertain assignments when required skills are missing from team member profiles.
- Requires explicit Leader approval before any plan is instantiated or activated.

### 2. Assignment Rules & Uncertainty Detection
- Evaluates candidate members using recorded skills, roles, permissions, workload, and availability.
- Strictly adheres to ground-truth profiles — never invents skills or hallucinated member information.
- Automatically flags uncertainty (`is_uncertain=True`) and alerts the leader when no team member possesses required skills or when candidates lack qualification thresholds.

### 3. Dependency Engine & Parallel Waves
- **Cycle Detection**: Direct (`A -> B -> A`) and transitive (`A -> B -> C -> A`) cycles are detected and rejected.
- **Wave Execution**: Tasks with zero unsatisfied prerequisites activate in parallel in Wave 1.
- Downstream tasks remain held in `Waiting` state until all upstream prerequisites are verified complete.
- **Multi-Dependency Joins**: Tasks depending on multiple prerequisites unlock only when *all* prerequisites are satisfied.

### 4. Verification-Gated Completion vs. Self-Reported Progress
- Members submit deliverables via `/deliver <id> <link>` or dashboard.
- Tasks transition to `In Review` (Reported completion) awaiting leader verification.
- Downstream tasks are never unlocked by unverified member claims.
- Leaders verify (`/verify <id>` or 1-click on Dashboard) or reject (`/reject_proof <id> <feedback>`) deliverables.
- Rejection returns tasks to `In Progress` with constructive leader feedback and notifies the assignee.

### 5. Blocker & Overdue Cascading Impact Escalation
- When a task is blocked or overdue, the orchestrator immediately computes all affected downstream tasks.
- Downstream assignees are alerted via Telegram that upstream prerequisites are delayed.
- Project leader receives immediate cascading impact alerts.
- Resolving a blocker returns tasks to `In Progress` — never treating unblocking as completed.

### 6. Leader Controls & Full Audit Logging
- Leader controls for plan approval, assignment changes, dependency overrides, task cancellation, and deadline changes.
- Every important decision, status change, and notification attempt is permanently recorded in SQLite `audit_logs` and `notifications` tables.
- Deduplication keys prevent duplicate Telegram messages across polling loops and system restarts.

### 7. Interactive Dashboard Views
- **Agentic Planning**: Goal decomposition, structured execution plan table, 1-click approve/reject.
- **Dependency Graph**: Parallel execution waves, parallel active tasks, waiting tasks list.
- **Tasks Table**: Full search, filters, manual task creation, deliverable verification modal, dependency override modal.
- **Kanban Board**: Drag/status view across Waiting, In Progress, Blocked, In Review, and Completed.
- **Blockers & Cascading Impact**: Visual breakdown of active blockers and affected downstream dependencies.
- **Team & Workload**: Workload capacity distribution and active task loads per member.
- **Decision Log**: Complete chronological audit trail of all leader actions and system events.
- **Evidence Reports**: Verified records vs. self-reported claims vs. AI inferences.

---

## 🛠️ Setup & Windows Launch Instructions

### Prerequisites
- Python 3.9+ (Standard library only; zero external pip dependencies needed for production)
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### Quick Start
```powershell
# 1. Navigate to directory
cd syncsoul_orchestrator

# 2. Configure environment
Copy-Item .env.example .env

# 3. Edit .env with your bot token:
# TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
# ANTHROPIC_API_KEY=sk-ant-... (optional, deterministic rule engine active by default)

# 4. Start the application
python run.py
```

Open your browser to: **http://localhost:8080**

---

## 🧪 Testing

Run all 67 automated test cases:
```powershell
python -m unittest discover tests
```

Run specific test modules:
```powershell
# Agentic Orchestration Tests (Parallel, Unlocking, Leader Approvals, Cycles, Restarts)
python -m unittest tests/test_orchestration.py

# Legacy Compatibility Tests (Ingestion syntax, deduplication, rollback)
python -m unittest tests/test_legacy_compat.py

# End-to-End App & Server Integration Tests
python -m unittest tests/test_app.py

# Multilingual AI Interpreter Tests (English, Hindi, Hinglish)
python -m unittest tests/test_interpreter.py

# Enhanced Features & Blocker Tests
python -m unittest tests/test_enhanced_features.py

# Photo Proof & Vision Review Tests
python -m unittest tests/test_photo.py
```

---

## 📁 Architecture Overview
```
syncsoul_orchestrator/
├── run.py                     # Application entry point: starts bot polling + web server + scheduler
├── coordinator/
│   ├── db.py                  # SQLite store, transactions, safe incremental migrations
│   ├── config.py              # Environment variable loader
│   ├── orchestrator.py        # Orchestration Engine: activation, verification, unlocking, audit logging
│   ├── planning.py            # Planning Agent: goal decomposition, structured tasks, wave calculation
│   ├── assignment.py          # Assignment Rules Engine: skills matching, workload, uncertainty detection
│   ├── dependencies.py        # Dependency graph, cycle detection, parallel waves analysis
│   ├── server.py              # HTTP server, REST API endpoints, session authentication
│   ├── bot.py                 # Telegram bot handler, update deduplication, commands, alerts
│   ├── interpreter.py         # AI task understanding (English, Hindi, Hinglish, promises vs done)
│   ├── blockers.py            # Blocker detection, obstacle categorization, subtask formulation
│   ├── reminders.py           # Intelligent deadline tracker, anti-spam cooldown, prompt generator
│   ├── reports.py             # Evidence-based member summaries (verified vs claimed vs inferred)
│   ├── vision.py              # Optional photo proof review engine
│   ├── telegram_api.py        # Telegram Bot API client (stdlib urllib)
│   └── static/
│       └── index.html         # Interactive Dashboard (Plan, Graph, Kanban, Tables, Blockers, Audit)
├── tests/
│   ├── test_orchestration.py  # 10 comprehensive orchestration requirement tests
│   ├── test_legacy_compat.py  # Legacy command and task ingestion compatibility tests
│   ├── test_app.py            # End-to-end leader and bot flows
│   ├── test_interpreter.py    # Classifier test cases (English, Hindi, Hinglish)
│   ├── test_enhanced_features.py # Extended tasks, assignment, blockers, dependencies
│   └── test_photo.py          # Photo proof verification tests
├── DEMO_GUIDE.md              # Complete walkthrough guide for evaluating all features
├── README.md                  # Project documentation
├── .env.example               # Clean environment configuration template
└── .gitignore                 # Ignores databases, local .env, caches, virtual environments
```
