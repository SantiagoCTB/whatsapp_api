# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Whapco is a multi-tenant WhatsApp/Messenger/Instagram chatbot platform for customer service automation. It combines rule-based flows, AI responses (OpenAI API), and real-time human advisor handoff.

## Commands

### Run locally
```bash
pip install -r requirements.txt
cp .env_example .env   # then fill in credentials
python app.py          # starts on PORT (default 5000)
```

### Run with Docker (production-like)
```bash
docker compose -f deploy/linux/docker-compose.yml up -d --build
```

### Tests
```bash
pytest tests/                      # all tests
pytest tests/test_auth_security.py # single file
```

### Tenant management
```bash
python scripts/create_tenant.py <key> <db_name> \
  --name "Company" --db-host 127.0.0.1 \
  --db-user root --db-password secret --init-schema

python scripts/backup_databases.py --env-file .env
```

### Deployment (CI/CD via GitHub Actions → SSH)
```bash
/opt/whapco/deploy/linux/deploy.sh   # backs up DB, pulls, rebuilds Docker
```

## Architecture

### Entry Points
- `app.py` — Flask app factory; binds tenant context and registers all blueprints
- `asgi.py` — ASGI wrapper for Uvicorn (production server)
- `config.py` — All configuration, reads from environment variables

### Key Directories
- `routes/` — Flask Blueprints (one file per feature area)
- `services/` — Business logic, external API clients, database layer
- `templates/` — Jinja2 HTML templates
- `static/` — CSS, JS, user-uploaded media (`static/uploads/`)
- `tests/` — Pytest suite
- `deploy/` — Docker Compose, Nginx config, deploy scripts
- `scripts/` — Admin CLI utilities

### Core Services
| File | Responsibility |
|---|---|
| `services/db.py` | All MySQL queries; connection pooling; per-tenant DB isolation |
| `services/tenants.py` | Tenant resolution (header/query/session), `TenantInfo` dataclass, per-tenant env override |
| `services/webhook.py` | **Largest file (138 KB).** Parses inbound messages from all platforms, evaluates rule engine, invokes AI, sends replies |
| `services/whatsapp_api.py` | Meta Cloud API wrapper — sends messages, media, buttons, templates |
| `services/ia_client.py` | OpenAI client — chat completions, multimodal, system prompt config |
| `services/chat_automation.py` | Per-chat AI enable/disable and advisor lock flags |
| `services/assignments.py` | Assigns chats to available advisors |
| `services/transcripcion.py` | Vosk-based Spanish audio-to-text (async thread pool) |
| `services/catalog.py` | OCR (Tesseract) and PDF (pymupdf) text extraction for product catalogs |
| `services/realtime.py` | Flask-SocketIO initialization |

### Request Flow

```
Incoming webhook (WhatsApp / Messenger / Instagram)
  → routes/webhook.py (verification + dispatch)
  → services/webhook.py _receive_message_safe()
      → global_commands check
      → message buffering (dedup)
      → save to DB
      → rule engine evaluation (DB-driven steps/conditions/actions)
      → if no rule match → AI response (ia_client)
      → services/whatsapp_api.py → Meta Cloud API (send reply)
      → SocketIO push to advisor dashboard
```

### Multi-Tenancy
Each tenant gets its own MySQL database. The central DB holds a `tenants` registry table. Tenant context is resolved per-request from `X-Tenant-ID` header, query parameter, or session, then stored in a Python context variable. All `db.py` calls use this context to connect to the correct database.

### Rule Engine
Rules are stored in the tenant DB (`reglas`, `botones` tables). Each rule has: trigger patterns, conditions (chat state, step, platform), actions (send text/media/buttons, advance step, assign advisor, enable AI). `services/webhook.py` evaluates rules in order on every inbound message.

### Real-time Updates
Flask-SocketIO broadcasts new messages and state changes to connected advisor browsers. The chat UI (`templates/chat.html`) updates via Socket.IO events without full page reloads.

## Environment Variables

Key variables (see `.env_example` for full list):

| Variable | Purpose |
|---|---|
| `DB_HOST/PORT/USER/PASSWORD/NAME` | MySQL connection |
| `META_TOKEN`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN` | WhatsApp Cloud API |
| `PAGE_ACCESS_TOKEN`, `INSTAGRAM_TOKEN` | Messenger / Instagram |
| `SECRET_KEY` | Flask session encryption |
| `PUBLIC_BASE_URL` | External URL for webhooks and media |
| `IA_API_TOKEN`, `IA_MODEL` | OpenAI key and model (default `o4-mini`) |
| `VOSK_MODEL_PATH` | Path to Spanish Vosk model for audio transcription |
| `DEFAULT_TENANT` | Fallback tenant key when none is specified |
| `INIT_DB_ON_START` | Set to `0` to skip schema init on startup |

## Notes

- The default superadmin credentials are set in the README (Spanish) — change them on first deploy.
- Audio transcription requires the Vosk Spanish model downloaded separately and `ffmpeg` installed.
- OCR requires `tesseract-ocr` and the Spanish language pack (`tesseract-ocr-spa`).
- SSL certificates are managed by Certbot inside Docker; auto-renewed every 12 hours.
- `configuracion.py` in `routes/` is the largest route file (163 KB) — it handles the entire rule/button configuration UI and API.
