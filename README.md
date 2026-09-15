# IntervYou — Production AI Interviewer Backend

[![Architecture: FastAPI + LiveKit + Celery](https://img.shields.io/badge/Architecture-Async%20Microservices-blue.svg)](https://fastapi.tiangolo.com/)
[![Python Version](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://python.org)
[![Database: PostgreSQL + Redis](https://img.shields.io/badge/Database-PostgreSQL%20%7C%20Redis-blueviolet.svg)](https://www.postgresql.org/)
[![AI Engine: Gemini 3.6 Flash](https://img.shields.io/badge/AI%20Engine-Gemini%203.6%20Flash-orange.svg)](https://deepmind.google/technologies/gemini/)

---

## Executive Summary

**IntervYou** is an enterprise-grade, real-time AI technical interviewing platform engineered for high-concurrency voice interactions and automated candidate evaluations. 

The backend orchestrates a multi-agent system that ingests candidate CVs and target job descriptions, synthesizes tailored technical questions, conducts low-latency WebRTC voice interviews via LiveKit, and performs deep multi-dimensional evaluation reports asynchronously.

Built with **FastAPI**, **PostgreSQL (AsyncPG)**, **Redis**, **Celery**, and **Google Gemini 3.6 Flash**, the system guarantees strict relational integrity, zero event-loop blocking, and high resilience against transient infrastructure failures.

---

## 1. Core System Architecture & Technology Stack

### System Topography & Separation of Concerns

The architecture follows a decoupled, event-driven pattern designed to isolate non-blocking HTTP endpoints from CPU/IO-bound background evaluations and real-time WebRTC media streams.

```
+-----------------------------------------------------------------------------------+
|                                  CLIENT LAYER                                     |
|                       (Flutter Mobile / Web Frontend)                              |
+-----------------------------------------------------------------------------------+
                                   |                 |
                   REST HTTP / WS  |                 |  WebRTC Audio Stream
                                   v                 v
+--------------------------------------+   +----------------------------------------+
|          FASTAPI API SERVER          |   |          LIVEKIT WEBRTC SERVER         |
|   - Authentication & Auth Guards     |   |   - Real-time Audio Distribution       |
|   - Session & Candidate Management   |   |   - Room State & Data Channels         |
|   - Reconnect Token Generation       |   +----------------------------------------+
+--------------------------------------+                       ^
          |                 |                                  |
          |                 +------------------+               | Room Events
          v                                    v               v
+-------------------+                +----------------------------------------------+
| POSTGRESQL (DB)   |                |            LIVEKIT VOICE AGENT               |
| - Relational Schema|                |   - STT (Deepgram Nova-3)                    |
| - Users/Transcripts|                |   - LLM (Gemma / Gemini 3.6)                |
| - Session History |                |   - TTS (Rime / ElevenLabs)                  |
+-------------------+                |   - Turn Mirroring & Redis Buffer            |
          ^                          +----------------------------------------------+
          |                                            |
          | Write Report                              v
+--------------------------------------+     +--------------------------------------+
|       CELERY WORKER PROCESSES        |     |         REDIS IN-MEMORY STORE        |
| - Async Evaluation Engine            |     | - Real-time Transcript Turn Buffer   |
| - Fallback Reconciliation Beat       | <---| - Rate Limiting & Token Revocation    |
| - Gemini Quota Isolation             |     | - Live Agent Status Monitoring       |
+--------------------------------------+     +--------------------------------------+
```

### Technology Stack & Engineering Justifications

| Layer | Component | Choice | Engineering Rationale & Justification |
| :--- | :--- | :--- | :--- |
| **API Framework** | ASGI Gateway | **FastAPI (Async Python)** | Provides native ASGI asynchronous non-blocking I/O handling high concurrent connections, combined with automated Pydantic schema validation and OpenAPI specification generation. |
| **Relational Storage**| Primary DB | **PostgreSQL + AsyncPG (SQLAlchemy 2.0)** | Ensures strict ACID compliance, foreign key cascades, JSONB capabilities for dynamic transcript/metrics storage, and zero-blocking DB connections via `asyncpg` connection pools. |
| **In-Memory Store** | Cache / Buffer | **Redis 7 (aioredis)** | Acts as an ephemeral live-transcript buffer for crash recovery, handles rate-limiting counters, manages refresh token rotation grace-periods, and stores live LiveKit agent status. |
| **Task Queue** | Distributed Queue | **Celery + Redis Broker** | Offloads multi-prompt AI evaluation and report generation from the main ASGI web thread to worker processes, preventing HTTP request timeouts and server thread starvation. |
| **Real-time Media** | WebRTC Gateway | **LiveKit Server** | Enables low-latency bidirectional WebRTC voice streaming with native agent dispatch, participant tracking, and data-channel messaging. |
| **AI Intelligence** | Multi-Modal LLM | **Google Gemini 3.6 Flash** | Delivers state-of-the-art multi-turn reasoning, structured JSON output enforcement, fast inference latency, and high technical evaluation depth. |

---

## 2. Component Workflow & Sequence Diagram

The following sequence details the full lifecycle of an interview: from candidate preparation, WebRTC room initialization, and real-time voice interaction, through session termination, Redis-to-PostgreSQL fallback transcript sync, and Celery evaluation dispatch.

```mermaid
sequenceDiagram
    autonumber
    actor Candidate as Candidate (Flutter App)
    participant API as FastAPI Gateway
    participant Redis as Redis Store
    participant DB as PostgreSQL DB
    participant LK as LiveKit WebRTC
    participant Agent as LiveKit Voice Agent
    participant Worker as Celery Worker
    participant Gemini as Google Gemini API

    %% 1. Preparation Phase
    rect rgb(240, 245, 255)
    Note over Candidate, API: 1. Preparation & Context Fingerprinting
    Candidate->>API: POST /api/v1/interviews/prepare (CV + Job Description)
    API->>Gemini: DocumentAgent.analyze() + QuestionnaireAgent.generate()
    Gemini-->>API: Candidate Summary + 5 Technical Questions
    API->>Redis: Cache preparation context (key: pipeline-context:{id}, TTL: 6h)
    API-->>Candidate: 200 OK (Preparation Response)
    end

    %% 2. Start Session Phase
    rect rgb(245, 255, 240)
    Note over Candidate, LK: 2. Session Initialization & Dispatch
    Candidate->>API: POST /api/v1/sessions/start
    API->>DB: Insert InterviewSession (status: IN_PROGRESS)
    API->>LK: Issue WebRTC Access Token & Dispatch Agent
    LK->>Agent: Spawn Voice Agent Process (room_name)
    API-->>Candidate: 201 Created (livekit_token, room_name)
    end

    %% 3. Voice Interview Phase
    rect rgb(255, 250, 240)
    Note over Candidate, Agent: 3. Real-Time WebRTC Dialogue Loop
    Candidate->>LK: Connect WebRTC Audio Track
    Agent->>LK: Join Room & Subscribe Audio
    loop Dialogue Loop
        Candidate->>Agent: Candidate speaks (STT stream)
        Agent->>Gemini: Agent calls get_next_question() tool
        Gemini-->>Agent: Returns question text
        Agent->>Candidate: Aria responds (TTS audio stream)
        Agent->>Redis: Append turn to Redis buffer (transcript:{room_name})
    end
    end

    %% 4. Termination & Sync Phase
    rect rgb(255, 240, 240)
    Note over Candidate, Worker: 4. Session Termination & Evaluation Dispatch
    Candidate->>API: POST /api/v1/sessions/end/{session_id}
    API->>DB: Update InterviewSession (status: COMPLETED)
    API->>Redis: Get live transcript buffer (redis_service.get_transcript_turns)
    API->>DB: Explicit Transcript Sync -> Insert InterviewTranscript (PostgreSQL)
    API->>LK: Close Room (background task)
    API->>Worker: Dispatch generate_final_interview_report.delay(session_id)
    API-->>Candidate: 200 OK (Session Ended, Task ID)
    end

    %% 5. Background Evaluation Phase
    rect rgb(245, 240, 255)
    Note over Worker, DB: 5. Asynchronous Scoring & Persistence
    Worker->>DB: load_evaluation_inputs() (Session + Transcript + Job)
    Worker->>Gemini: EvaluationAgent.evaluate() (with retry & JSON enforcement)
    Gemini-->>Worker: Evaluation Scores & Detailed Feedback
    Worker->>DB: Save InterviewReport
    Candidate->>API: GET /api/v1/sessions/{session_id}/report
    API->>DB: Query InterviewReport
    API-->>Candidate: 200 OK (Evaluation Report Response)
    end
```

---

## 3. Database Architecture & Entity Relationship Diagram (ERD)

The database schema enforces strict relational normalization, single-active-session constraints, and dynamic JSON schema storage.

```mermaid
erDiagram
    users ||--o{ candidate_profiles : "uploads"
    users ||--o{ job_descriptions : "configures"
    users ||--o{ interview_sessions : "initiates"
    interview_sessions ||--o| interview_transcripts : "has durable"
    interview_sessions ||--o| interview_reports : "generates"

    users {
        uuid id PK "gen_random_uuid()"
        varchar email UK "Indexed, Not Null"
        varchar hashed_password "Nullable for OAuth"
        varchar full_name "Nullable"
        varchar role "Default: candidate"
        varchar plan "Default: free"
        timestamptz created_at "Server Default: NOW()"
    }

    candidate_profiles {
        uuid id PK
        uuid user_id FK "CASCADE"
        varchar full_name
        text raw_cv_text "Extracted plain text"
        varchar cv_file_path "Storage path"
        boolean is_active "Single active profile flag"
        timestamptz created_at
    }

    job_descriptions {
        uuid id PK
        uuid user_id FK "CASCADE"
        varchar job_title "Target position"
        text description_text "Full job requirements"
        boolean is_active "Single active job flag"
        timestamptz created_at
    }

    interview_sessions {
        uuid id PK "UUIDv4"
        uuid user_id FK "CASCADE"
        varchar room_name UK "LiveKit room identifier"
        varchar status "IN_PROGRESS | COMPLETED | FAILED"
        jsonb questions "Snapshotted 5 questions"
        jsonb candidate_summary "Snapshotted profile summary"
        text failure_reason "Populated if status = FAILED"
        timestamptz created_at
        timestamptz updated_at
    }

    interview_transcripts {
        uuid id PK
        uuid session_id FK,UK "CASCADE"
        jsonb turns "[{speaker, text, timestamp}]"
        timestamptz created_at
        timestamptz updated_at
    }

    interview_reports {
        uuid id PK
        uuid session_id FK,UK "CASCADE"
        float overall_score "0.0 to 10.0"
        float technical_score "0.0 to 10.0"
        float problem_solving_score "0.0 to 10.0"
        float communication_score "0.0 to 10.0"
        jsonb strengths "List of key strengths"
        jsonb weaknesses "List of identified gaps"
        text recommendation "Hire / Further Review / Reject"
        text summary "Detailed executive narrative"
        boolean is_placeholder "Default: false"
        timestamptz created_at
    }
```

### Relational Schema Specifications

#### 1. `users` Table
Stores user credentials, OAuth profiles, and platform roles.
- `id` (UUID, Primary Key)
- `email` (VARCHAR(255), Unique, Indexed)
- `hashed_password` (VARCHAR(255), Nullable for Google OAuth users)
- `role` / `plan` (VARCHAR(50), Default defaults for access control)

#### 2. `interview_sessions` Table
Tracks session lifecycle state machine (`IN_PROGRESS` -> `COMPLETED` / `FAILED`).
- `id` (UUID, Primary Key)
- `user_id` (UUID, Foreign Key -> `users.id` ON DELETE CASCADE)
- `room_name` (VARCHAR(255), Unique)
- `status` (Enum: `IN_PROGRESS`, `COMPLETED`, `FAILED`)
- **Constraint**: Partial unique index `idx_one_active_session` on `(user_id)` WHERE `status = 'IN_PROGRESS'` prevents candidates from opening concurrent active sessions.

#### 3. `interview_transcripts` Table
Stores full chronological dialogue turns between the agent and candidate.
- `id` (UUID, Primary Key)
- `session_id` (UUID, Foreign Key -> `interview_sessions.id` ON DELETE CASCADE, Unique)
- `turns` (JSONB): Structured array: `[{"speaker": "agent"|"candidate", "text": "...", "timestamp": "ISO8601"}]`

#### 4. `interview_reports` Table
Stores final multi-dimensional scores and evaluation narratives generated by Celery worker.
- `session_id` (UUID, Foreign Key -> `interview_sessions.id` ON DELETE CASCADE, Unique)
- Scores: `overall_score`, `technical_score`, `problem_solving_score`, `communication_score` (FLOAT)
- Structured Feedback: `strengths` (JSONB), `weaknesses` (JSONB), `recommendation` (TEXT), `summary` (TEXT)

---

## 4. Local Setup & Environment Deployment Guide

### Prerequisites
- **Python**: `v3.10` or higher
- **Docker Desktop**: `v24.0+` & Docker Compose
- **Git**: Installed locally

---

### Step 1: Clone Repository & Virtual Environment Setup

```bash
# 1. Clone repository
git clone https://github.com/BaselAhmed22/Ai_interviewer-backend.git
cd Ai_interviewer-backend

# 2. Create Python virtual environment
python -m venv .venv

# 3. Activate virtual environment
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# 4. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

---

### Step 2: Configure Environment Variables (`.env`)

Create a `.env` file in the root directory:

```env
# ==========================================
# APPLICATION & SECURITY CONFIGURATION
# ==========================================
PROJECT_NAME="IntervYou AI Backend"
API_V1_STR="/api/v1"
DEBUG=True
SECRET_KEY="generate-with-python-secrets-token-hex-48"
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=7

# ==========================================
# INFRASTRUCTURE DATABASES & BROKERS
# ==========================================
POSTGRES_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/intervyou_db"
REDIS_URL="redis://localhost:6379/0"

# ==========================================
# LIVEKIT WEBRTC CONFIGURATION
# ==========================================
LIVEKIT_URL="ws://localhost:7880"
LIVEKIT_API_KEY="devkey"
LIVEKIT_API_SECRET="secretsecretsecretsecretsecretsecretsecret"
LIVEKIT_AGENT_NAME="aria-interviewer"

# ==========================================
# AI ENGINES & API KEYS (QUOTA ISOLATION)
# ==========================================
# Primary API Key (Interactive pipeline & LiveKit Agent)
GEMINI_API_KEY="AIzaSyYourPrimaryGeminiAPIKey"

# Secondary API Key (Isolated Celery evaluation worker to prevent rate limits)
GEMINI_EVAL_API_KEY="AIzaSyYourSecondaryGeminiAPIKey"

# LiveKit Voice Pipeline Providers
DEEPGRAM_API_KEY="your-deepgram-api-key"
RIME_API_KEY="your-rime-api-key"

# ==========================================
# SMTP EMAIL CONFIGURATION (OPTIONAL)
# ==========================================
SMTP_HOST="smtp.gmail.com"
SMTP_PORT=587
SMTP_USER="your-email@gmail.com"
SMTP_PASSWORD="your-app-password"
EMAILS_FROM_EMAIL="your-email@gmail.com"
```

---

### Step 3: Infrastructure Containerization (Docker)

Spin up PostgreSQL, Redis, and LiveKit local instances via Docker Compose:

```bash
docker-compose up -d
```

Verify running services:
```bash
docker-compose ps
```

---

### Step 4: Database Migrations

Apply database migrations via Alembic to provision tables and indexes:

```bash
# Run latest database migrations
alembic upgrade head
```

---

### Step 5: Execution (Running the 3 Application Components)

A full execution setup requires 3 running processes in separate terminal sessions:

#### Terminal 1: FastAPI Main Application Gateway
```bash
python -m app.main
```
*Access interactive Swagger API documentation at: `http://localhost:8000/docs`*

#### Terminal 2: Celery Background Evaluation Worker
```bash
celery -A app.workers.celery_app worker --loglevel=info -P solo
celery -A app.workers.celery_app beat --loglevel=info
```

#### Terminal 3: LiveKit Voice Agent Worker Process
```bash
python -m app.interviews.workers.livekit_agent dev
```

---

## 5. Engineering Challenges & Production Resilient Solutions

During development and deployment testing, key production edge-cases were identified and architecturally mitigated:

### 1. Gemini API Rate Limiting & Quota Exhaustion (`429` & `503`)

#### Problem
Google's Gemini free-tier imposes strict RPM (Requests Per Minute) limits. During high-concurrency evaluation requests, downstream Gemini API calls raised `429 RESOURCE_EXHAUSTED` or transient `503 Service Unavailable` errors, causing evaluation tasks to crash prematurely.

#### Architectural Mitigation
- **API Key Quota Isolation**: Separated interactive preparation calls (`GEMINI_API_KEY`) from asynchronous Celery evaluation workers (`GEMINI_EVAL_API_KEY`), isolating rate-limit pools.
- **Exponential Backoff Wrapper (`gemini_retry.py`)**: Implemented a retry wrapper catching both `ServerError` (5xx) and `APIError` with code `429` / `503`, executing linear backoff retries before raising exceptions to Celery.

```python
# app/interviews/agents/gemini_retry.py
def call_with_retry(fn: Callable[[], T], *, max_retries: int = 2, base_delay: float = 1.0) -> T:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except (genai_errors.ServerError, genai_errors.APIError) as exc:
            code = getattr(exc, "code", None)
            is_transient = isinstance(exc, genai_errors.ServerError) or code in (429, 503)
            if not is_transient:
                raise
            last_exc = exc
            if attempt >= max_retries:
                break
            time.sleep(base_delay * (attempt + 1))
    if last_exc:
        raise last_exc
```

---

### 2. Transcript Persistence Race Conditions & `404 transcript_not_ready`

#### Problem
When a candidate or client ends an interview via `POST /api/v1/sessions/end/{session_id}`, the Celery evaluation task is enqueued immediately. If the LiveKit voice agent process experienced minor network lag during its `on_exit()` hook, `interview_transcripts` had 0 rows committed in PostgreSQL, triggering a `404 transcript_not_ready` error or evaluation task failure.

#### Architectural Mitigation
- **Turn-by-Turn Redis Mirroring**: The LiveKit agent mirrors every dialogue turn into Redis (`redis_service.append_transcript_turn`) in real-time as it occurs.
- **Explicit Sync on Session End**: In `POST /api/v1/sessions/end/{session_id}`, the endpoint verifies PostgreSQL presence; if missing, it fetches the live turn buffer from Redis and commits it to PostgreSQL synchronously before dispatching Celery.
- **Auto-Persisting Endpoint Fallback**: In `GET /api/v1/sessions/{session_id}/transcript`, if PostgreSQL returns 0 rows, it automatically recovers turns from Redis, inserts the `InterviewTranscript` row into PostgreSQL, commits, and returns `200 OK`.

---

### 3. Celery Worker Event Loop & Engine Disposal (`RuntimeError`)

#### Problem
Inside persistent Celery worker processes executing async SQLAlchemy coroutines via `asyncio.run()` or manual event loop management, `engine.dispose()` was being invoked at the end of every individual task. This destroyed the shared connection pool for subsequent tasks in the same worker process, resulting in `RuntimeError: Event loop is closed` and closed DB connection errors.

#### Architectural Mitigation
- Implemented `get_worker_loop()` in `app/workers/tasks.py` to maintain a single, persistent event loop per Celery worker process.
- Removed per-task `engine.dispose()` calls, reserving database engine cleanup strictly for worker process teardown (`@worker_process_shutdown.connect`).

---

## 6. Development & Quality Assurance Workflow

### Code Formatting & Quality Verification

```bash
# Verify Python AST syntax across all core modules
python -c "import ast, glob; [ast.parse(open(f, encoding='utf-8').read()) for f in glob.glob('app/**/*.py', recursive=True)]; print('All AST checks passed!')"

# Verify module import tree
python -c "import importlib; [importlib.import_module(m) for m in ['app.main', 'app.workers.tasks', 'app.ai_evaluator.evaluation_agent']]; print('All imports clean!')"
```

### Git Branching & PR Guidelines
- `main`: Protected production-ready code. Direct pushes are disabled.
- `feat/*`: Feature development branches.
- `fix/*`: Bug fix & patch branches.

---

## 7. License & Credits

Developed by the **IntervYou Engineering Team**.
- **Backend Architecture**: FastAPI, SQLAlchemy Async, Celery, Redis.
- **Real-Time AI & Audio**: LiveKit WebRTC, Google Gemini 3.6 Flash, Deepgram, Rime.