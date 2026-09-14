# IntervYou — AI Interviewer Backend

## 1. Project Overview

IntervYou is the backend for an AI-powered mock-interview platform. A
candidate registers, uploads a CV, and adds a target job description; the
backend then starts a **live voice interview** in a LiveKit room, where an
AI agent ("Aria") listens via speech-to-text, reasons with an LLM, and
replies with synthesized speech in real time — the same round trip as a
real interviewer, end to end. When the interview ends, a report is
generated and stored against the session.

**Core stack**

| Layer | Technology |
|---|---|
| API framework | FastAPI (async) |
| Database | PostgreSQL via SQLAlchemy 2.0 (async) + Alembic migrations |
| Cache / queues | Redis (rate limiting, token revocation, agent health) |
| Background jobs | Celery (report generation, CV parsing, scheduled reconciliation) |
| Real-time voice | LiveKit (WebRTC) + `livekit-agents` (STT → LLM → TTS pipeline) |
| Auth | JWT (access/refresh/reset) + Google Sign-In (`id_token` or `access_token`) |

**What the backend owns:**
- Account creation and authentication (email/password and Google).
- CV upload, parsing, and storage.
- Job description and interview-preference management.
- Interview session lifecycle: start → live voice interview → end → report.
- Dispatching and supervising the AI interviewer agent for each session.

---

## 2. Prerequisites & Installation

### Prerequisites

- Python 3.10+
- PostgreSQL 15+ and Redis 7+ (or Docker, see below)
- A LiveKit server (self-hosted via Docker, or a [LiveKit Cloud](https://cloud.livekit.io) project)
- Git

The fastest way to get PostgreSQL, Redis, and a local LiveKit server running is:

```bash
docker-compose up -d
```

### Clone the repository

```bash
git clone <repository-url>
cd "Ai_interviewer backend"
```

### Create and activate a virtual environment

Pick the block that matches your shell.

<details open>
<summary><strong>CMD (Windows)</strong></summary>

```bat
python -m venv venv
venv\Scripts\activate.bat
```

</details>

<details>
<summary><strong>PowerShell (Windows)</strong></summary>

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

> If script execution is blocked, run once per session:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

</details>

<details>
<summary><strong>Git Bash (Windows)</strong></summary>

```bash
python -m venv venv
source venv/Scripts/activate
```

</details>

<details>
<summary><strong>macOS / Linux</strong></summary>

```bash
python3 -m venv venv
source venv/bin/activate
```

</details>

Your prompt should now be prefixed with `(venv)` in every case above.

### Install dependencies

```bash
pip install -r requirements.txt
```

### Configure environment variables

```bash
cp .env.example .env      # macOS/Linux/Git Bash
copy .env.example .env    # Windows CMD/PowerShell
```

Then fill in `.env`. At minimum for local development:

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | **Yes** | No default on purpose — the app refuses to start without it. Generate with `python -c "import secrets; print(secrets.token_hex(48))"` |
| `POSTGRES_URL` | Yes | Defaults to a local Postgres instance |
| `REDIS_URL` | Yes | Defaults to a local Redis instance |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | Yes | From your LiveKit server or Cloud project |
| `DEEPGRAM_API_KEY` / `OPENAI_API_KEY` / `ELEVEN_API_KEY` | Only for the AI agent | STT / LLM / TTS providers — the API server runs fine without them, but the agent worker refuses to start until all three are set |
| `GOOGLE_CLIENT_ID` | Optional | Enables `POST /api/v1/auth/google` |
| `SMTP_*` | Optional | Enables real password-reset emails; unset falls back to printing the link to the console |

See `.env.example` for the full list with descriptions.

### Apply database migrations

```bash
alembic upgrade head
```

---

## 3. Running the Application

Three independent processes make up a full local environment. Run each in
its own terminal (with the virtual environment activated).

**1 — Infrastructure** (PostgreSQL, Redis, local LiveKit server):

```bash
docker-compose up -d
```

**2 — API server:**

```bash
python -m app.main
```

This binds to `0.0.0.0:8000`, so it's reachable from a phone, an emulator,
or through a tunnel like ngrok — not just from this machine. (Running
`uvicorn app.main:app --reload` directly binds to `127.0.0.1` only; add
`--host 0.0.0.0` explicitly if you use that form instead.)

- Interactive API docs: `http://localhost:8000/docs`

**3 — Background workers** (report generation, CV parsing — required for
those features, independent of the voice agent below):

```bash
celery -A app.workers.celery_app worker --loglevel=info
celery -A app.workers.celery_app beat --loglevel=info
```

**4 — AI interviewer agent** (required for the live voice interview itself
— without this running, a candidate joins an empty room):

```bash
python -m app.interviews.workers.livekit_agent start   # production
python -m app.interviews.workers.livekit_agent dev     # local dev, verbose logs
```

This process reads `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, and
`ELEVEN_API_KEY` directly from `.env` and refuses to start if any is
missing.

---

## 4. Database ERD Diagram

```mermaid
erDiagram
    USERS ||--o{ CANDIDATE_PROFILES : uploads
    USERS ||--o{ JOB_DESCRIPTIONS : adds
    USERS ||--o{ INTERVIEW_PREFERENCES : sets
    USERS ||--o{ INTERVIEW_SESSIONS : starts
    INTERVIEW_SESSIONS ||--o| INTERVIEW_REPORTS : generates

    USERS {
        uuid id PK
        string email UK
        string hashed_password "nullable — Google-only accounts"
        string full_name
        string role
        string google_id UK "nullable"
        string plan
        string initials
        datetime created_at
    }

    CANDIDATE_PROFILES {
        uuid id PK
        uuid user_id FK
        string full_name
        text raw_cv_text
        string cv_file_path
        string original_filename
        text skills
        bool is_active
        bool processing_failed
        text failure_reason
        datetime created_at
    }

    JOB_DESCRIPTIONS {
        uuid id PK
        uuid user_id FK
        string job_title
        text description_text
        bool is_active
        datetime created_at
    }

    INTERVIEW_PREFERENCES {
        uuid id PK
        uuid user_id FK
        string company_name
        string job_title
        string language
        datetime interview_date
        bool is_active
        datetime created_at
    }

    INTERVIEW_SESSIONS {
        uuid id PK
        uuid user_id FK
        string room_name UK
        enum status "IN_PROGRESS / COMPLETED / FAILED"
        text failure_reason
        datetime created_at
        datetime updated_at
    }

    INTERVIEW_REPORTS {
        uuid id PK
        uuid session_id FK "unique — one report per session"
        float overall_score
        float eye_contact_score
        float posture_score
        float speech_clarity_score
        text feedback_summary
        json detailed_metrics
        bool is_placeholder
    }
```

**Notes:**
- `CANDIDATE_PROFILES`, `JOB_DESCRIPTIONS`, and `INTERVIEW_PREFERENCES` each
  use an `is_active` flag rather than deleting old rows — a new upload
  deactivates the previous one, keeping full history.
- A partial unique index on `INTERVIEW_SESSIONS(user_id)` (where
  `status = 'IN_PROGRESS'`) enforces **at most one active session per
  user** at the database level, not just in application code.
- All foreign keys cascade on delete.

---

## 5. Backend Workflow Cycle Diagram

End-to-end flow for one interview, from login through the live voice
session to the final report.

```mermaid
sequenceDiagram
    autonumber
    participant FE as Frontend
    participant API as FastAPI Backend
    participant DB as PostgreSQL
    participant LK as LiveKit Server
    participant AG as AI Agent Worker (Aria)
    participant CW as Celery Worker

    FE->>API: POST /auth/login (email, password)
    API->>DB: Verify credentials
    API-->>FE: 200 { accessToken, refreshToken }

    Note over FE,API: CV upload + job description already on file

    FE->>API: POST /sessions/start (Bearer accessToken)
    API->>API: Validate JWT (signature, iss, aud, exp)
    API->>DB: Check active CV + job description + no other active session
    API->>DB: Create InterviewSession (IN_PROGRESS)
    API->>LK: Generate room access token
    API-->>FE: 201 { roomName, livekitToken }

    API-)LK: dispatch_agent(roomName)  [background task]
    LK-)AG: Job assigned (agent_name="aria-interviewer")
    AG->>LK: Connect and join room
    AG->>AG: wait_for_participant()

    FE->>LK: Connect to room (livekitToken)
    LK-->>AG: Candidate joined

    AG->>FE: Voice greeting (TTS)
    loop Live interview
        FE->>AG: Candidate speaks (audio track)
        AG->>AG: STT → LLM → TTS
        AG->>FE: Aria responds (audio track)
    end

    FE->>API: POST /sessions/end/{id}
    API->>DB: status = COMPLETED
    API-)LK: close_room(roomName)  [background task]
    API-)CW: generate_final_interview_report.delay(sessionId)
    API-->>FE: 200 { message, taskId }

    CW->>DB: Save InterviewReport
    Note over CW,DB: reconcile_missing_reports (Celery beat, every 5 min)<br/>re-dispatches any session left without a report

    FE->>API: GET /sessions/{id}/report
    API->>DB: Fetch report
    API-->>FE: 200 { overallScore, feedbackSummary, ... }
```

---

## 6. Git & PR Workflow

1. **Pull latest `main`:**
   ```bash
   git checkout main && git pull origin main
   ```
2. **Branch per task:** `git checkout -b feat/task-name` or `fix/issue-name`
3. **Commit and push:**
   ```bash
   git add .
   git commit -m "feat: description of the change"
   git push origin feat/task-name
   ```
4. **Open a Pull Request** into `main`.

> ⚠️ Direct pushes to `main` are not allowed. Every change goes through a
> reviewed and approved Pull Request.
