import os

ROOT = "intervyou_backend"

FILES = {
    # --- Project Core & Main Entry ---
    "app/__init__.py": "",
    "app/main.py": (
        "from fastapi import FastAPI\n"
        "from fastapi.middleware.cors import CORSMiddleware\n\n"
        "app = FastAPI(title='IntervYou AI Backend', version='1.0')\n\n"
        "# CORS Settings for Flutter Client\n"
        "app.add_middleware(\n"
        "    CORSMiddleware,\n"
        "    allow_origins=['*'],\n"
        "    allow_credentials=True,\n"
        "    allow_methods=['*'],\n"
        "    allow_headers=['*'],\n"
        ")\n\n"
        "@app.get('/')\n"
        "async def root():\n"
        "    return {'status': 'healthy', 'message': 'IntervYou AI API is running'}\n"
    ),

    # --- Core Configuration & Security ---
    "app/core/__init__.py": "",
    "app/core/config.py": "# Pydantic Settings & Env Management\n",
    "app/core/database.py": "# Async SQLAlchemy Engine & Session\n",
    "app/core/security.py": "# JWT Token Generation & Passlib Security\n",

    # --- API Routers & Endpoints ---
    "app/api/__init__.py": "",
    "app/api/v1/__init__.py": "",
    "app/api/v1/router.py": "# API Router Aggregator\n",
    "app/api/v1/endpoints/__init__.py": "",
    "app/api/v1/endpoints/auth.py": "# POST /auth/login & POST /auth/register\n",
    "app/api/v1/endpoints/candidate.py": "# CV upload & Candidate Profile Management\n",
    "app/api/v1/endpoints/sessions.py": "# Session lifecycle & LiveKit Token Generation\n",
    "app/api/v1/endpoints/ws_analytics.py": "# Real-time Analytics WebSocket + AlertEngine\n",

    # --- Schemas (Configured for CamelCase Compatibility with Flutter) ---
    "app/schemas/__init__.py": "",
    "app/schemas/base.py": (
        "from pydantic import BaseModel, ConfigDict\n"
        "from pydantic.alias_generators import to_camel\n\n"
        "class CamelModel(BaseModel):\n"
        "    model_config = ConfigDict(\n"
        "        alias_generator=to_camel,\n"
        "        populate_by_name=True,\n"
        "        from_attributes=True\n"
        "    )\n"
    ),
    "app/schemas/auth.py": "# Auth Request & Response Schemas\n",
    "app/schemas/candidate.py": "# Candidate & CV Schemas\n",
    "app/schemas/session.py": "# Session & LiveKit Schemas\n",
    "app/schemas/metadata.py": "# Real-time Frame Metadata & Alert Schemas\n",
    "app/schemas/report.py": "# Interview Report Schemas\n",

    # --- Database Models ---
    "app/db/__init__.py": "",
    "app/db/base.py": "# Base Registry for Alembic Migrations\n",
    "app/db/models/__init__.py": "",
    "app/db/models/user.py": "# User & Candidate Profile Database Models\n",
    "app/db/models/session.py": "# Interview Session Database Model\n",
    "app/db/models/report.py": "# Interview Report Database Model\n",

    # --- External Services ---
    "app/services/__init__.py": "",
    "app/services/livekit_service.py": "# LiveKit Server Integration & Token Helper\n",
    "app/services/redis_service.py": "# Async Redis Cache Manager\n",
    "app/services/cv_parser.py": "# PDF/DOCX Text Extraction\n",

    # --- Celery Workers ---
    "app/workers/__init__.py": "",
    "app/workers/celery_app.py": "# Celery Application Instance\n",
    "app/workers/tasks.py": "# Celery Tasks (Report Generation, CV Analysis)\n",

    # --- Docker, LiveKit Config & Environment Variables ---
    "docker-compose.yml": (
        "version: '3.8'\n\n"
        "services:\n"
        "  postgres:\n"
        "    image: postgres:15-alpine\n"
        "    container_name: intervyou_postgres\n"
        "    restart: always\n"
        "    environment:\n"
        "      POSTGRES_USER: postgres\n"
        "      POSTGRES_PASSWORD: postgrespassword\n"
        "      POSTGRES_DB: intervyou_db\n"
        "    ports:\n"
        "      - \"5432:5432\"\n"
        "    volumes:\n"
        "      - postgres_data:/var/lib/postgresql/data\n\n"
        "  redis:\n"
        "    image: redis:7-alpine\n"
        "    container_name: intervyou_redis\n"
        "    restart: always\n"
        "    ports:\n"
        "      - \"6379:6379\"\n"
        "    volumes:\n"
        "      - redis_data:/data\n\n"
        "  livekit:\n"
        "    image: livekit/livekit-server:v1.6\n"
        "    container_name: intervyou_livekit\n"
        "    restart: always\n"
        "    command: --config /etc/livekit.yaml\n"
        "    ports:\n"
        "      - \"7880:7880\"\n"
        "      - \"7881:7881\"\n"
        "      - \"7882:7882/udp\"\n"
        "      - \"50000-50010:50000-50010/udp\"\n"
        "    environment:\n"
        "      LIVEKIT_KEYS: \"devkey: secretsecretsecretsecretsecretsecretsecret\"\n"
        "    volumes:\n"
        "      - ./livekit.yaml:/etc/livekit.yaml\n\n"
        "volumes:\n"
        "  postgres_data:\n"
        "  redis_data:\n"
    ),

    "livekit.yaml": (
        "port: 7880\n"
        "bind_addresses:\n"
        "  - \"\"\n"
        "rtc:\n"
        "  tcp_port: 7881\n"
        "  port_range_start: 50000\n"
        "  port_range_end: 50010\n"
        "  use_external_ip: false\n\n"
        "keys:\n"
        "  devkey: secretsecretsecretsecretsecretsecretsecret\n"
    ),

    ".env": (
        "PROJECT_NAME=IntervYou AI Backend\n"
        "DEBUG=True\n"
        "SECRET_KEY=change-me-in-production-secret-key-123\n"
        "REDIS_URL=redis://localhost:6379\n"
        "POSTGRES_URL=postgresql+asyncpg://postgres:postgrespassword@localhost:5432/intervyou_db\n"
        "LIVEKIT_API_KEY=devkey\n"
        "LIVEKIT_API_SECRET=secretsecretsecretsecretsecretsecretsecret\n"
        "LIVEKIT_URL=ws://localhost:7880\n"
    ),

    "requirements.txt": (
        "fastapi\n"
        "uvicorn[standard]\n"
        "pydantic\n"
        "pydantic-settings\n"
        "sqlalchemy[asyncio]\n"
        "asyncpg\n"
        "alembic\n"
        "redis\n"
        "celery\n"
        "python-jose[cryptography]\n"
        "passlib[bcrypt]\n"
        "aiofiles\n"
        "pypdf\n"
        "python-docx\n"
        "livekit-api\n"
        "python-multipart\n"
        "python-dotenv\n"
    ),
}

DIRS_ONLY = [
    "uploads/cvs",
]


def create_structure() -> None:
    print("--- Scaffolding IntervYou AI Backend ---")
    for rel_path, content in FILES.items():
        full_path = os.path.join(ROOT, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        if os.path.exists(full_path):
            print(f"Skip (exists): {full_path}")
            continue
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Created file: {full_path}")

    for rel_dir in DIRS_ONLY:
        full_dir = os.path.join(ROOT, rel_dir)
        os.makedirs(full_dir, exist_ok=True)
        gitkeep = os.path.join(full_dir, ".gitkeep")
        if not os.path.exists(gitkeep):
            open(gitkeep, "w").close()
        print(f"Created dir:  {full_dir}")


if __name__ == "__main__":
    create_structure()
    print(f"\nDone! Project scaffolded successfully under ./{ROOT}/")
    print("\nQuick Start Guide:")
    print("  1. Run Infrastructure (Docker): docker-compose up -d")
    print(f"  2. Navigate into directory:     cd {ROOT}")
    print("  3. Install dependencies:         pip install -r requirements.txt")
    print("  4. Start FastAPI server:         uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload")