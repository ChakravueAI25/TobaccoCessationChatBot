# Quit Smoke V1 Central Backend

This FastAPI service receives pseudonymous research events from the Android app for central storage, validation, synchronization, backup, analytics preparation, and export. It does not run an LLM or control chatbot, behavioral, intervention, coping, or safety decisions. The Android app remains functional when this service is offline.

## Android contract mapping

The central schema preserves the Android names semantically while using PostgreSQL snake_case identifiers: `eventId` -> `event_id`, `participantId` -> `participant_id`, `sessionId` -> `session_id`, `eventType` -> `event_type`, `timestamp` -> `event_timestamp`, `payload` -> `payload`, `schemaVersion` -> `schema_version`, and `createdAt` -> `created_at`. The incoming API uses the Android contract's snake_case wire representation shown in the schemas. `payload` remains JSON text and is validated as a JSON object before storage. Events are immutable and `event_id` is the primary key/uniqueness constraint.

## Setup

1. Install Python 3.12+ and PostgreSQL 17. `uv` is recommended.
2. Copy `.env.example` to `.env` and set a local database URL, development API key, and directories.
3. Create the development role and database with a PostgreSQL administrator:

```sql
CREATE USER quit_smoke_app WITH PASSWORD 'use-a-local-secret';
CREATE DATABASE quit_smoke_research OWNER quit_smoke_app;
```

4. Set `DATABASE_URL` in `.env`, then apply the versioned schema:

```powershell
uv run alembic upgrade head
```

## Run and test

```powershell
./scripts/start.ps1
uv run pytest
```

Development API documentation is available at `/docs`.

### Two credentials, for two kinds of caller

| Caller | Credential | Why |
|---|---|---|
| The Android app | `X-API-Key: <DEVELOPMENT_API_KEY>` | Must be non-interactive. Spec 04 freeze item 2 requires the app to work with this server off, so a phone can never be asked to complete a login. |
| A researcher | Session cookie or `Authorization: Bearer`, from `POST /api/v1/auth/login` | The dashboard, exports and research reads. Identifies a person, which a shared key cannot. |

The research read/export endpoints accept **either**. `/api/v1/auth/me` and
`/api/v1/auth/change-password` accept **only** a session.

Create the first operator account — there is no default account, and an install with none simply
cannot be signed into:

```powershell
uv run python scripts/manage_users.py create <username> --display-name "Your Name"
```

It prompts for the password. There is deliberately no `--password` flag: a password on a command
line lands in shell history and in the process list. Policy is 8-128 characters with an
upper-case letter, a lower-case letter, a digit and a symbol.

Passwords are hashed with `hashlib.scrypt` at the OWASP minimum (n=2^17, r=8, p=1) — stdlib, so
no dependency, and ~540 ms per hash by design. Login is throttled at 5 failures per username and
20 per client address in a rolling 15-minute window, checked **before** the hash so the throttle
cannot itself be used as a denial of service. Sessions are opaque server-stored tokens; the
database holds only their SHA-256, so a dump cannot be replayed as a signed-in browser, and
logout revokes immediately.

## Operations

Back up PostgreSQL separately from exports by setting `DATABASE_URL` and running:

```powershell
./scripts/backup.ps1
```

Generate a controlled synthetic/test export from PostgreSQL:

```powershell
uv run python scripts/export_test.py --database-url $env:DATABASE_URL --output exports/working/events.json --format json
```

The `scripts/export_test.py` utility supports JSON and CSV. The service's own export endpoints (`POST /api/v1/exports`) additionally support XLSX, written directly as minimal OOXML with no spreadsheet dependency. See `CLAUDE.md` for the full endpoint list and the operator dashboard at `/`. Restore a custom-format backup with `pg_restore --clean --if-exists --dbname=$env:DATABASE_URL path/to/backup.dump`; never copy PostgreSQL data directories as backups. Analytics consume controlled exports and must not write to the source database.

## Current state (for the next agent)

Everything below has been run, not just written.

**Working and verified against a live server** (uvicorn on SQLite, 2026-08-28): `/health`,
`POST /api/v1/sync` with `X-API-Key`, batch and event deduplication, `/api/v1/stats`,
`/participants`, `/events`, `/events.csv`, exports in CSV/JSON/XLSX (the hand-rolled XLSX opens
as a valid workbook), the export history and download, operator login, the rate limit and
lockout, immediate session revocation on logout, the dashboard's login form, and participant
registration and sign-in (including that an enrolled participant's events sync and that their
username never appears in the research listing).

`uv run pytest` — **90 tests**. The auth tests take ~40 s because scrypt is deliberately slow;
that is the cost of the parameters, not a hang.

### PostgreSQL has never actually been connected

`.env`'s `DATABASE_URL` password does not match the `quit_smoke_app` role, so
`/health` reports `database: unavailable` and every query fails. All 73 tests pass because
`tests/conftest.py` overrides `get_db` with SQLite — which means anything PostgreSQL-specific
(JSONB operators, `ON CONFLICT` syntax) would pass tests and fail in production.

Fix it with a PostgreSQL superuser, then put that password in `.env`:

```sql
ALTER USER quit_smoke_app WITH PASSWORD 'your-local-secret';
```

Then `uv run alembic upgrade head` and confirm `/health` says `database: ok`. **Do this before
trusting any of the above against Postgres.**

### Not done

- **No device is registered as a participant ahead of time.** `POST /api/v1/sync` creates a
  `Participant` row on first contact, so any holder of the API key can invent participants. Fine
  for development; the study may want devices enrolled deliberately.
- **`analytics/scripts` holds only a README.** Spec 11 §66 lists the aggregations the research
  team wants; none are written.
- **No questionnaire ingestion.** The `questionnaires` table exists and nothing writes to it.
- **Multi-device sync has not been tested** (spec 23 §14).
- **A backup has never been restored** (spec 23 §14). `scripts/backup.ps1` runs `pg_dump`; the
  restore side is documented but unexercised.
- **`AUTH_ENABLED=false` disables the device key *and* the session check.** It is a development
  switch; make sure it is true for any real collection.

## How the app talks to this

The Android side is complete **and now actually runs**: research events are written locally,
queued in `sync_queue`, and drained by `ResearchSyncManager` from a foreground coroutine in
`AppShell` every five minutes. Until 2026-08-28 the manager and its HTTP transport existed but
nothing called them, so events queued forever — if you are reading an older note claiming sync
was verified on device, it was not.

The app needs two values in the chatbot repo's `local.properties`:

```properties
research.baseUrl=http://192.168.1.x:8000
research.apiKey=<the same DEVELOPMENT_API_KEY>
```

An empty `research.baseUrl` leaves the uploader switched off entirely, which is a supported
state — the app must work with no server at all.

Wire format is the Android contract's snake_case form. `payload` is a JSON **string** that must
decode to an object; the app builds it with a hand-rolled writer (`encodeJsonObject`) that
escapes control characters, because a participant message can contain quotes and newlines.
`schema_version` is the string `"1"`, not the number.

`tests/test_android_wire_contract.py` pins the exact body the Android transport sends, and
`shared/src/androidHostTest/.../HttpResearchSyncTransportTest.kt` asserts the same values from
the Kotlin side against a real socket. Change one and both fail — which is the point.

Consent gates collection on the device — see `CLAUDE.md` in the app repo — so if a device shows
zero pending records, check consent before suspecting the transport.
