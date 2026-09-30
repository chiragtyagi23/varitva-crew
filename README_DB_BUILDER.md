# Form DB Flow — Orchestrator DB Builder

1 Crew / 3 Agents:

- **Orchestrator** — coordinates; never runs SQL
- **DB Schema Designer** — returns structured JSON schema
- **DB Creation Agent** — applies schema via `postgres_safe` tool

## Setup

```bash
cp .env.example .env   # set OPENAI_API_KEY + DATABASE_URL
docker compose up -d   # local Postgres if needed
uv sync
```

## CLI

```bash
uv run kickoff
# or
uv run python -m form_db_flow.main "Create an employee management database with employees, departments, attendance and salaries."
```

## HTTP (for Nest Tools)

```bash
uv run serve
# POST http://localhost:8090/v1/db-builder/kickoff
# {"requirement":"..."}
```

Registry table: `tools_db` (auto-created). Domain tables are created separately per feature.
