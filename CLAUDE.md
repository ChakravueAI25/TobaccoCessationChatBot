# Quit Smoke — research backend

FastAPI + PostgreSQL. Receives pseudonymous research events from the Android app, stores them,
and exports the study dataset.

## Where the app is, and how the repos are laid out

`../TobaccoCessationChatbot` on disk. Both codebases push to **one GitHub repo**,
`github.com/ChakravueAI25/TobaccoCessationBot` — the Android app on `main`, this service on
`backend`, with **unrelated histories**. GitHub will offer to open a PR from `backend`; never
take it, or the Python tree lands inside the Android tree. They are parallel branches and stay
that way.

Governed by `../TobaccoCessationChatbot/Resources for development/` specs 17, 18, 21, 22, 23.
The app repo's `docs/HANDOFF.md` is the shared status document for both.

**Read `docs/SECURITY_REVIEW.md` before changing a route.** Eight of its nine findings are now
fixed and the ninth needs a study decision rather than code. It also states the threat model
they all assume: the device API key is compiled into the APK and
extractable from it, so "authenticated as a device" means "anyone who has the app".

## What this service must never do (spec 23 §7)

Run LLM inference. Choose behavioural mechanisms, interventions or coping. Override the app's
safety pathway. Create clinical knowledge. It is a data sink and an export tool — nothing else.

It is also **expected to be offline most of the time** (spec 04 freeze item 7: online during
office hours or when the team is collecting). The app queues locally and retries; server
downtime must never be treated as an app failure.

## Layout

```
api/app/
  main.py           # router registration
  config.py         # pydantic-settings, reads .env
  database.py       # engine + get_db
  dependencies.py   # require_api_key (X-API-Key or operator session) + rate_limit
  models/           # SQLAlchemy: Participant, SyncBatch, Event, ExportRun, ...
  schemas/          # Pydantic wire contracts
  security.py       # scrypt hashing, password policy, session tokens
  routes/
    health.py       # GET /health
    auth.py         # operators:    POST /api/v1/auth/login|logout, /me, /change-password
    participants.py # participants: POST /api/v1/participants/register|login,
                    #               DELETE /api/v1/participants/account
    sync.py         # POST /api/v1/sync   <- the app posts here
    research.py     # read + export endpoints
    dashboard.py    # GET / — self-contained operator page, session login
  services/export.py  # CSV/JSON/XLSX writers, scope queries
db/migrations/      # alembic: 0001 research schema, 0002 operator accounts + indexes,
                    #          0003 participant accounts
scripts/manage_users.py  # create/disable operator accounts (prompts for the password)
scripts/verify_postgres.py  # PostgreSQL smoke test - `pytest` does NOT touch PostgreSQL
scripts/reset_participants.py # clears every participant-facing table (TRUNCATE)
scripts/smoke_stack.py      # two participants end to end against the LIVE stack
scripts/start_model.ps1     # llama-server for the study stack; the VS Code task runs this
docs/SECURITY_REVIEW.md  # open findings, severity-ranked — read before changing a route
deploy/gcp/         # the ONLY deployment target: one Compute Engine VM
  startup.sh        #   machine prep, re-run on every boot, knows nothing about this repo
  deploy.sh         #   create the VM if missing, push this working copy, bring the stack up
  vm.sh             #   everything after: status, logs, data, feedback, backup, stop/start
tests/              # 123 tests, run against SQLite via conftest override
```

## Deployment: Google Cloud, and only Google Cloud

There were three deployment stories — a USB bundle for an Ubuntu box with no git, a fully
portable Windows bundle for a clinician's laptop, and Docker for "whatever server arrives".
All three are deleted. The study runs on one Compute Engine VM, `docs/GCP_DEPLOYMENT.md`
describes it, and `deploy/gcp/` is three shell scripts.

Two consequences worth knowing before changing something:

- **`llama-server` is in `docker-compose.yml` now**, behind a `cpu` / `gpu` profile. It used to
  be deliberately excluded because "the container runtime for a GPU differs on every host" —
  true when the host could be anybody's server, and meaningless now there is one VM we create.
  `llama-gpu` carries a network alias of `llama`, so `LLAMA_SERVER_URL=http://llama:8080` is
  constant across both profiles and switching is a flag rather than an edit.
- **The VM's source tree is replaced wholesale on every deploy**, and everything the study
  accumulates (`.env`, the model, exports, backups) lives outside it and is symlinked in. A
  merge-style deploy leaves a module that no longer exists in the repo still importable on the
  server, which is how a deployment quietly stops being the thing that was tested.

## Authentication — three callers, deliberately separated

| caller | credential | why |
|---|---|---|
| a device syncing | `X-API-Key` | must be non-interactive — spec 04 freeze item 2 |
| a researcher | session cookie / bearer, from `/auth/login` | the dashboard and exports need to know who acted |
| a participant | username + password, from `/participants/login` | **once**, then the device caches the id and never authenticates again |

`require_api_key` accepts a device key **or** an operator session, and every research
read/export route depends on it. `current_user` accepts **only** a session.

### Participants never hold a session

`POST /api/v1/participants/login` returns a `participant_id` and nothing else — no token, no
cookie, no expiry. Spec 04 freeze item 2 requires the app to work with this server switched off,
so anything that could expire would be a way to strand someone mid-study. The device caches the
id and keeps syncing with the device key.
`test_syncing_never_requires_a_participant_login` and
`test_login_issues_no_session_cookie` are what stop that being "improved" into a session later.

`participant_accounts` is a **separate table** from `participants`. The latter is joined to every
event, so a password hash on it would be dragged into every research query and every export.
Dropping `participant_accounts` leaves a valid pseudonymous dataset with no route back to a
person — which is what spec 18 §68's de-identification step needs to be possible. The
participant id is `secrets.token_urlsafe`, not a counter: a sequential id would leak enrolment
order and cohort size.

Registration is the one endpoint that *does* reveal whether a username exists (409). Someone
choosing a username has to be told it is taken; login stays deliberately vague.

- **Hashing is `hashlib.scrypt`**, n=2^17/r=8/p=1 (the OWASP minimum), ~540 ms per hash.
  Stdlib, so no argon2/bcrypt dependency on a laptop install. The stored record is
  self-describing (`scrypt$n$r$p$salt$key`) so the parameters can be raised without
  invalidating existing passwords — `needs_rehash` upgrades an account on its next login.
  **Never replace this with a single-pass hash.** `test_a_password_is_never_stored_in_recoverable_form`
  fails if a bare sha256/sha1/md5 of the password appears in the record.
- **The rate limit runs before the password is hashed.** 5 failures per username and 20 per
  client address in a rolling 15-minute window. Hashing first would make the throttle itself the
  denial of service it exists to prevent — `test_the_throttle_runs_before_the_password_is_hashed`
  pins that ordering.
- **Every login failure returns one identical 401.** Wrong username, wrong password and
  disabled account are indistinguishable, or the endpoint becomes a username oracle.
- **Sessions are opaque and server-stored**, not JWTs, so logout revokes immediately. The
  database holds only `sha256(token)` — a dump must not be replayable as a signed-in browser.
  A single hash pass is correct *here* and wrong for passwords: the input is 256 bits of
  `secrets` output, so there is no guessable space.
- The session cookie is `HttpOnly` + `SameSite=Strict` but **not** `Secure`, because the
  development deployment is plain HTTP on a LAN (spec 23 §2) and a Secure cookie would silently
  never be stored. Set it when the study runs over HTTPS.
- `AUTH_ENABLED=false` disables the device key *and* the session check. Development only.

There is **no default account**. Create one with
`uv run python scripts/manage_users.py create <username>`; it prompts, and there is no
`--password` flag on purpose.

## The wire contract

`POST /api/v1/sync` with header `X-API-Key`. Body is `SyncRequest`:

- `batch_id`, `participant_id`, `app_version`, `schema_version` (**the string `"1"`, not a
  number** — validated by string comparison), `records[]`
- each record: `event_id`, `participant_id`, `event_type`, `timestamp`, `session_id`,
  `payload` (**a JSON *string*** that must parse to an object), `schema_version`, `created_at`

Dedup is on `event_id` (primary key) and on `batch_id` — a retried batch returns the original
counts instead of double-inserting (spec 04 freeze item 9). Android side:
`shared/src/androidMain/.../data/sync/HttpResearchSyncTransport.kt`.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | Operator dashboard. Unauthenticated page, authenticated fetches — signs in with a username and password, and holds no credential in the page at all (the session is an HttpOnly cookie). |
| `GET /health` | `status` + `database`. `degraded` means Postgres is unreachable. |
| `POST /api/v1/auth/login` | Operator sign-in. Sets an HttpOnly cookie and returns a bearer token. |
| `POST /api/v1/auth/logout` | Revokes the presented session. Always 204. |
| `GET /api/v1/auth/me` | The signed-in operator. Session only — the device key is refused. |
| `POST /api/v1/auth/change-password` | Requires the current password; signs every session out. |
| `POST /api/v1/participants/register` | Creates an account, returns a `participant_id`. Device key. |
| `POST /api/v1/participants/login` | Proves who is asking. The **only** route that returns a backup. Device key. |
| `DELETE /api/v1/participants/account` | Closes the account so the same login cannot come back. `participant_id` as a **query parameter**. Device key, idempotent. |
| `PUT /api/v1/backup` | The participant's own copy up. Device key. |
| `DELETE /api/v1/backup` | The participant's own copy gone. Device key. |
| `POST /api/v1/generate` | Proxies a finished prompt to `llama-server`. Device key. 503 means the model is not running. |
| `POST /api/v1/sync` | Ingest. Device key. |
| `GET /api/v1/stats` | Counts, events-by-type, last event/sync time. |
| `GET /api/v1/participants` | One row per device with event/batch counts and last-seen. |
| `GET /api/v1/events` | Paged, newest first, filter by participant/type. |
| `POST /api/v1/exports` | Writes CSV/JSON/XLSX to `EXPORT_DIRECTORY`, records an `ExportRun`. |
| `GET /api/v1/exports` | Export history. |
| `GET /api/v1/exports/{id}/download` | Download; path-confined to `EXPORT_DIRECTORY`. |
| `GET /api/v1/events.csv` | Quick CSV, no run recorded. |

## Closing an account blocks the username — a tombstone, not a delete

`DELETE /api/v1/participants/account` exists because clearing the phone could not do the job the
participant asked for. Deletion is meant to sign them out and make them start again on a **new
username**; the credential lives here, so the username stayed valid and a login handed the old
account straight back.

The first version deleted the row, which **freed the username**. The live smoke test caught it: a
closed `kaushik` re-registered as `kaushik` and got a working account back. A unit test had
asserted that behaviour as correct and passed, which is why the live test matters.

So a closure is a tombstone (migration `0005_closed_accounts`):

| column | after closing |
|---|---|
| `username` | **kept** — the primary key, and the thing that blocks re-use |
| `participant_id` | NULL — severs the only join between a person and the dataset |
| `display_name` | NULL |
| `password_hash` | `CLOSED_ACCOUNT_SENTINEL`, a value no password produces |
| `closed_at` | set, and `login` refuses on it before any comparison happens |

Keeping the row does **not** weaken spec 18 §68's de-identification step. That step *is* the
removal of the username-to-`participant_id` join, and nulling `participant_id` removes exactly
that. What survives is a username with nothing attached: no id, no profile detail, no usable
credential. It cannot be signed into and it cannot be linked to an event.

- `register` returns **409** with wording naming a deleted account, because a plain "already
  taken" reads as a mistake the participant can retry their way out of.
- `login` refuses with the **same** 401 and the same wording as a username that never existed.
  "This account was deleted" would confirm to a stranger that the name was once real, and the
  person who requested the deletion already knows.
- `participant_id` is nullable now. PostgreSQL treats each NULL as distinct, so the UNIQUE
  constraint tolerates any number of closed accounts.
- Idempotent: a second call finds no account for that id — `participant_id` is NULL once closed —
  and reports `account_removed = false` rather than 404, because the phone retries dropped
  connections and a completed deletion must not surface as an error.

Left alone deliberately, the same three as `DELETE /backup`:

* `events` — pseudonymous, the study's data rather than the participant's own copy. Removing rows
  mid-study would change results other participants are part of.
* `participants` — joined to every event. Dropping it orphans the dataset.
* `login_attempts` — spec 21 §67 rate-limit evidence. Deleting it on request would let somebody
  clear their own security trail.

**`participant_id` goes as a query parameter, not a body.** The Android side is
`AndroidParticipantAuth.closeAccount`; a body there is a 422 the app reads as failure, which is
exactly the shape of "delete my data looks broken". Change the two together.

The block lasts **until the table is cleared** by `scripts/reset_participants.py`. Within a study
round a name is gone for good; a fresh round starts from nothing.

## Passwords: already correct, do not "simplify" this

Asked about directly, so it is recorded here to stop it being replaced with something weaker.

`hash_password` uses **scrypt at n=2^17, r=8, p=1** with a fresh random salt per row, stored as
`scrypt$n$r$p$salt$key`. `verify_password` splits that, re-derives the key using the salt **from
the row**, and compares with `hmac.compare_digest`.

The intuitive design — hash the incoming password and compare the two hashes as strings — needs a
*fixed* salt to work, and a fixed salt means one precomputed table attacks every account at once.
Per-row salting is why two participants with the same password have different stored values, and
it is why the comparison has to re-derive rather than just compare.

`verify_password` returns False rather than raising on a malformed record. That is what makes the
closed-account sentinel safe as a second lock behind `closed_at`, and it means a corrupted row
fails the login instead of crashing the endpoint and revealing that the account exists.

Scrypt at these parameters costs roughly half a second per attempt, which is deliberate and is why
the rate-limit check runs **before** hashing — otherwise the throttle would be the denial of
service it exists to prevent.

## The model is a prerequisite, and /health says so

`tasks.json` used to start all three services in parallel, with a comment calling a still-loading
model "a supported state rather than an outage". In practice, when the model task failed the API
came up perfectly healthy while **every chat turn fell back to deterministic text that reads like a
working conversation**. Invisible from outside for a full day of testing.

- **`Run study stack` is sequential now**: `2. Model (llama-server)` first, then a
  `Backend and tunnel` task running those two in parallel. The model task carries a
  `problemMatcher` whose `background.endsPattern` matches `listening on http` — a background task
  never exits, so without an endsPattern a sequential `dependsOn` waits forever. `beginsPattern`
  also matches the banner, so `start_model.ps1`'s "already up, nothing to do" exit does not hang
  the chain.
- **`/health` reports `model` alongside `database`**, and `status` is `degraded` when either is
  unavailable. `not_configured` counts as healthy: spec 04 freeze item 2 requires this service to
  be useful with no inference server at all.
- **`lifespan` in `main.py` probes the model on boot** and logs at ERROR when it is not there. It
  deliberately does **not** refuse to start — sync, backup and export need no model, and turning
  one missing GPU process into a collection outage would be the worse failure.
- **`generate.py` retries once**, 1.5 s apart, on a connection error or a 503. `llama-server` binds
  its port before it finishes reading 2.5 GB of weights, so a turn arriving during startup used to
  be spent on a single 503. **Not** retried on a 504: that means the model already consumed the
  whole budget, and a retry doubles a wait the participant is sitting through when the fallback is
  instant.

The app's fallback stays, and an unreachable model still routes to it — spec 10 §24 requires a real
answer rather than an error. What changed is that it is no longer the *normal* path, and a failure
is now visible rather than silent.

**Health tests must pin the model state.** `.env` sets `LLAMA_SERVER_URL`, so a `/health` test
without a monkeypatch makes a real network call to 127.0.0.1:8081 and its result depends on whether
a GPU process happens to be running. Use `_fixed_model_state` in `tests/test_backend.py`.

## Resetting between test rounds

```bash
uv run python scripts/reset_participants.py
```

One `TRUNCATE ... RESTART IDENTITY CASCADE`, child tables named explicitly so the script can
be audited by reading it. It is a **development** reset: once real
participants are enrolled their events are the study, and the narrower supported operations are
`DELETE /api/v1/backup` for one participant's own copy and
`DELETE /api/v1/participants/account` for one credential — neither touches anybody else.

## First-time setup

Needs a Postgres superuser — the app role is not self-provisioning:

```sql
CREATE USER quit_smoke_app WITH PASSWORD 'your-local-secret';
CREATE DATABASE quit_smoke_research OWNER quit_smoke_app;
```

Then put that password in `.env` as `DATABASE_URL`. **Percent-encode it** — a password
containing `@` (or `/`, `:`, `%`) breaks URL parsing, and SQLAlchemy reports the result as an
authentication failure rather than a malformed URL:

```
DATABASE_URL=postgresql+psycopg://quit_smoke_app:secret%40with%40ats@127.0.0.1:5432/quit_smoke_research
```

If the `postgres` superuser password is unknown, it can be recovered by switching the two
localhost rules in `pg_hba.conf` to `trust`, resetting the role, and putting the file back —
**put it back in a `finally`, or a failure halfway leaves the database open to every local
process.** Then:

```powershell
uv sync
uv run alembic upgrade head
uv run uvicorn api.app.main:app --host 0.0.0.0 --port 8000
```

Bind `0.0.0.0` (not `127.0.0.1`) for phones to reach it, and allow port 8000 through the
Windows firewall. `/health` reporting `database: unavailable` means the role/password/database
above is not right yet.

```powershell
uv run pytest                              # 123 tests - all against SQLite
uv run python scripts/verify_postgres.py   # the only thing that touches PostgreSQL
```

**`pytest` does not test PostgreSQL.** `tests/conftest.py` overrides `get_db` with SQLite, so
run the smoke test too after changing a route, a model or a migration.

The suite now sets `PRAGMA foreign_keys=ON` on its SQLite connections, so the insert-ordering
class of bug that PostgreSQL caught is visible in the fast suite: removing the `db.flush()`
calls from `routes/sync.py` fails 14 tests in about four seconds. SQLite still differs from
PostgreSQL on JSON semantics and type affinity, which is what the smoke test is for.

The auth tests take ~40 s. That is scrypt at n=2^17 doing its job, not a hang.

## Gotchas

- **`%LOCALAPPDATA%` is not the same folder for every process on this machine.** An agent
  session running inside the Claude desktop app's MSIX container has its LocalAppData writes
  redirected into that package's private cache:
  `...\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\...`. Inside the
  container the path resolves and files run; from a VS Code task outside it, the same path is
  unreachable. This cost **three rounds** of "the llama-server binary is not there" about a file
  that was on disk, unchanged, and runnable seconds earlier from the shell that built it.
  `Get-Item -Force` and its `.Target` property is what finally showed it, and the directory ACL
  carried an `S-1-15-3-...` AppContainer SID. **It was not antivirus and not Controlled Folder
  Access** - Defender reported `EnableControlledFolderAccess = 0` throughout, and an earlier
  version of this note blaming them was wrong. Build tool output belongs on **D:, in the repo**
  (`../TobaccoCessationBackend/bin/llamahost`, gitignored), which nothing redirects.
- **A file-existence check cannot decide whether to abort.** Same episode, separate lesson. From
  the affected context `Test-Path -LiteralPath` returned `$false`, `[System.IO.File]::Exists`
  returned `$false`, and `[System.IO.Directory]::GetFiles` threw - so every probe agreed with
  itself and disagreed with reality, and each "improved" version stated the wrong verdict more
  confidently than the last. `start_model.ps1` now only **warns**; the launch decides, because
  the operating system is the one authority that is never wrong about whether an exe runs.
- **XLSX is written without openpyxl** — `services/export.py` emits the four minimal
  OOXML parts into a zip. Every cell is an inline string on purpose, so Excel cannot retype an
  id like `0001` into a number. If styling is ever needed, that trade flips to openpyxl.
- Tests run on **SQLite** via a `get_db` override, so anything Postgres-specific will pass
  tests and fail in production. **PostgreSQL was first connected on 2026-08-28** and
  immediately produced three defects that 98 green tests had never touched — see "What
  connecting PostgreSQL found" below. Assume more are waiting: run
  `scripts/verify_postgres.py`-style round trips after touching a route, not just `pytest`.
- **SQLite does not enforce foreign keys** unless `PRAGMA foreign_keys=ON`, which the suite
  never sets. Any insert ordering bug is therefore invisible in tests and fatal in production.
- **SQLite drops the timezone** on a `DateTime(timezone=True)` column, so a timestamp read back
  is naive there and aware on PostgreSQL. `dependencies._as_aware` exists because comparing the
  two raises, which turned every session check into a 500 on the test database.
- `get_settings` is `@lru_cache`d — a test that changes `EXPORT_DIRECTORY` must call
  `get_settings.cache_clear()` on both sides.
- `routes/sync.py` catches **only** `(OperationalError, IntegrityError)` → 503. Everything else
  is a bug and must stay a 500: the app reads 5xx as retryable and retries the *head* of its
  queue, so a bug disguised as a 503 used to stop a participant uploading anything ever again.
  `test_a_server_bug_is_not_disguised_as_a_retryable_503` fails if a broad `except` comes back.
- A 4xx retires the **whole** batch on the app side, not the offending record. The app's own
  payloads cannot trigger one, but a future validation rule that can would discard up to 99 good
  records with the bad one. Per-record rejection is a wire-contract change, not a local fix.
- Migration `0001` creates tables but **not** the indexes the models declare via `index=True`.
  Migration `0002` adds them (`ix_events_*`, `ix_sync_batches_participant_id`) along with the
  auth tables, so export and participant queries no longer sequentially scan.
- Never copy Postgres data files as a backup (spec 23 §11) — use `scripts/backup.ps1` (pg_dump)
  or the export endpoints.

## The wire contract is pinned from both sides

`tests/test_android_wire_contract.py` holds the exact JSON body the Android transport produces,
as a literal. `shared/src/androidHostTest/.../HttpResearchSyncTransportTest.kt` asserts the same
field values against a real socket on the Kotlin side. Change either side and both fail.

That exists because both halves were written by reading the other's source and, until
2026-08-28, nothing had ever sent a byte across the boundary. The three ways it breaks are all
invisible from the phone — a 422 looks exactly like a server fault:

- `payload` sent as a nested object instead of a JSON string,
- `schema_version` sent as the number `1` instead of the string `"1"`,
- a timestamp the hand-rolled Android `Iso8601` formatter produces that Pydantic will not parse.

## Rate limiting

`dependencies.rate_limit` — 120 requests per minute per socket peer, on the sync and research
routers. `/health` is outside it on purpose: it is how the operator learns the server is up and
must answer when everything else is refusing.

Separate from the login throttle in `routes/auth.py`, which counts *failures* to make password
guessing expensive — the wrong shape where every request is legitimate and the cost is volume.

**Sized to be unreachable in normal use, and that is the requirement, not a compromise.** The
device syncs once every five minutes; throttling one that is draining a backlog would slow the
study's data collection to stop an attack nobody is mounting.
`test_the_rate_limit_cannot_be_reached_by_the_app_or_the_dashboard` fails if it is tuned down to
something the app can hit. It is in-memory and per-process, so it resets on restart and does not
span workers — with `--workers` or a load balancer it stops being a limit.

**A third, tighter throttle on `/participants/register`** (`MAX_REGISTRATIONS_PER_DEVICE`, 3 per
hour **per device**). The general limiter above is a coarse *volume* ceiling; this caps successful
account *creations*, so the same phone cannot enrol account after account. The app sends its stable
per-install id (the pseudonymous, non-hardware id it already puts on research events — no new
identifier) in `X-Device-Id`, and each device gets its own small quota.

**Keyed on the device, not the address, and that is the whole point.** On shared wifi or the ngrok
tunnel every phone collapses to one IP, so an address-keyed cap either blocks a room full of
participants enrolling on their own phones or is too loose to mean anything. Per device, each phone
is its own bucket, so the cap is tight (one account, or two if they delete and sign up again)
without ever catching legitimate enrolment. A caller with no header — not the app — falls back to
the client address so it is still bounded. Only successful creations count, and the check runs
before the ~540 ms hash, same as the login throttle. Same in-memory, single-process ceiling as
`rate_limit`; if this ever runs with `--workers`, move the count to a `registration_attempts` table
keyed on the device id. `test_bulk_account_creation_from_one_device_is_throttled` and
`test_each_device_has_its_own_quota` pin it. Still not a hard one-person-one-account guarantee — a
reinstall mints a fresh device id; an enrolment code is the hard version (see Known-bad below).

## What connecting PostgreSQL found

Three defects, all invisible to the 98-test suite, all fixed on 2026-08-28. They are recorded
because each one is a *class* of bug this setup will keep producing.

**1. Alembic never read `.env`.** `db/migrations/env.py` took `DATABASE_URL` from `os.environ`
only and otherwise fell back to `alembic.ini`, which shipped
`postgresql+psycopg://unused:unused@localhost/unused`. The documented setup — put the password
in `.env`, run `alembic upgrade head` — failed with
`password authentication failed for user "unused"`, naming a user nobody configured. `env.py`
now takes the URL from `api.app.config.Settings` and uses the app's own engine, so migrations
and the service cannot target different databases. `alembic.ini`'s `sqlalchemy.url` is
deliberately empty: a real-looking value there is a *silent fallback*, which is the whole bug.

Related trap, also fixed by not going through the ini file at all: `config.set_main_option`
writes through ConfigParser, which treats `%` as interpolation. A percent-encoded password —
and **any password containing `@` must be percent-encoded** or SQLAlchemy parses the host out of
it — raised `invalid interpolation syntax` even when correct.

**2. Every event insert violated a foreign key.** The models declare `ForeignKey` columns but no
`relationship()`, so SQLAlchemy's unit of work has no mapper-level dependency and flushes in
**alphabetical mapper order** — `Event` before `Participant`. PostgreSQL rejected the child row;
SQLite had never enforced the constraint. `routes/sync.py` now `db.flush()`es the participant
and the batch before the events. **Adding a new table with a foreign key needs the same care** —
the ORM will not order it for you.

**3. `payload` was double-encoded.** The column is `JSON` and was handed the raw JSON *string*,
so `json_typeof(payload)` returned `string` and the value stored was
`"{\"intensity\": \"6\"}"`. Every research query anyone would actually write —
`payload->>'trigger'` — returned NULL. It is now stored decoded, and `services/export.py`'s
`_isoformat` serialises dicts back to JSON text so `csv.DictWriter` cannot emit a Python repr.

The reason this one survived is worth remembering: `test_android_wire_contract` *looked* like it
checked the stored form and did not — it parsed the input constant and compared it to itself.
It now asserts on `stored["payload"]`.

## Known-bad, not yet fixed

Findings 1–7 and 9 in `docs/SECURITY_REVIEW.md` are fixed. **Finding 8 is the only one left and
it is a study-design decision, not a bug:** `/participants/register` is open to any API-key
holder, and the key is extractable from the APK. It is no longer *unlimited* — the per-device
registration throttle (see Rate limiting) caps one phone to a few accounts an hour — but that is
not a hard one-person-one-account guarantee: a reinstall mints a fresh device id, so a determined
person can still enrol again. The real hard fix is a study-issued enrolment code (phone-number
registration was considered and rejected — a phone number is directly identifying, which spec 18
rules out, the same reason email was rejected). Which the study needs depends on how it enrols
people, which nobody has decided.

The largest untested surface is not in that list: **PostgreSQL has never been connected.** Every
green test here is a SQLite result via the `get_db` override.
