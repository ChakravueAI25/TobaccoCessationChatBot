# Security and robustness review — 2026-08-28

A read of the backend for things that could leak data, break the study dataset, or degrade the
phone experience. Findings keep their original numbers as they move to **Fixed**, so references
to them elsewhere stay valid.

**1–7 and 9 are fixed. 8 is open and needs a product decision, not code.**

Threat model worth stating up front: **the device API key is not a secret.** It is compiled into
the APK and anyone who can install the app can extract it with `apktool` in about a minute. So
"authenticated as a device" should be treated as "any participant, or anyone they gave the APK
to". Everything below assumes that.

---

## Fixed

### 1. Stored XSS in the operator dashboard — HIGH

`cell()` interpolated values straight into `innerHTML`. Every field it renders —
`participant_id`, `app_version`, `event_type`, `session_id`, `event_id` — arrives from a device
over `/sync`. An `event_type` of `<img src=x onerror=fetch(...)>` was script execution in a
signed-in researcher's browser.

The session cookie is `HttpOnly`, so the token itself was not readable, but the script could
still drive `/api/v1/exports` and `/api/v1/events` as the researcher and exfiltrate the whole
study dataset. The `payload` column was already escaped; these columns were missed.

Fixed by routing `cell()` through `escapeHtml`. Pinned by
`test_dashboard_escapes_every_device_supplied_field`.

---

### 2. One bad record permanently blocked a participant's uploads — HIGH · fixed

`routes/sync.py` ended with `except (IntegrityError, Exception)` → **503**. Any server-side bug,
on any record, became a 503.

The Android side reads 5xx as retryable, and `syncOnce` always takes the *head* of the pending
queue (`ORDER BY createdAt ASC`). So **one unstorable record and that participant never uploaded
again** — silently, with the app showing nothing wrong and the research team seeing a device that
"stopped syncing".

Closed from both ends, because either half alone leaves the hole open:

- **Server**: the except is now `(OperationalError, IntegrityError)` — the database being
  unreachable, locked, or racing another batch on the same `event_id`, all of which clear on
  their own and are worth retrying. Anything else is a bug and now surfaces as a 500 with a
  traceback in the log instead of hiding behind the status the app is told to keep retrying.
- **App**: `ResearchSyncManager` retires a batch after `MAX_ANSWERED_ATTEMPTS = 20` attempts
  when the server *answered* with an error. `SyncOutcome.Unreachable` is deliberately exempt —
  a server that is off for a week is the expected state (spec 04 freeze item 7), not a failure.

Pinned by `test_a_server_bug_is_not_disguised_as_a_retryable_503`,
`test_an_unavailable_database_returns_a_retryable_503`,
`aServerThatKeepsAnsweringWithAnErrorEventuallyStopsBlockingTheQueue` and
`anOfflineServerIsNeverRetiredHoweverLongItStaysOff`.

**Still open in this area:** a 4xx retires the *whole* batch, so one unstorable record discards
up to 99 good ones alongside it. Closing that needs per-record rejection in the response and is
a wire-contract change; the app's own records cannot currently trigger it.

---

### 3. `/sync` did 2N queries per batch — MEDIUM · fixed

`duplicate_count` ran one `db.get` per record and the insert loop ran another — 2000 round trips
at the schema's own 1000-record maximum, slow enough on a laptop Postgres to trip the app's 20 s
read timeout, which the app reads as *unreachable* and retries, producing the same 2000 queries
again.

Now one `select(Event.event_id).where(Event.event_id.in_(event_ids))` up front and set membership
in memory. Pinned by `test_a_batch_is_deduplicated_without_a_query_per_record`, which overlaps two
batches so accepted and duplicate both have to be non-zero.

---

### 4. `payload` had no length limit — MEDIUM · fixed

Every other field was bounded; `payload: str` was not, so 1000 records × a few MB each was a
multi-hundred-MB body the server buffered and parsed, reachable by anyone holding the extractable
API key. Now `Field(min_length=2, max_length=8192)` — roughly 40× what the app actually writes,
which is a flat map of short strings. A proxy-level body cap is still wanted if this is ever
exposed beyond a LAN.

---

### 5. Unbounded reads on `GET /events.csv` — MEDIUM · fixed

`event_rows` streamed the whole `events` table into a Python list and then into one response
body. `/events` was paginated (`le=1000`); this was not.

Capped at `QUICK_LOOK_ROW_LIMIT = 50_000`, and it **refuses with 413 rather than truncating** —
a short CSV that looks complete is the failure that ends up in an analysis with nobody able to
tell. The message points at `POST /exports`.

**`POST /exports` is deliberately left uncapped.** It is the one that has to be complete: an
export that silently stopped at N rows is a study dataset with a hole in it. `event_rows`'
`limit` parameter documents that, and `run_export` never passes one.

Fixing this surfaced a second bug in the dashboard: the quick-CSV `fetch` never checked
`response.ok`, so a 401 — or the new 413 — was saved to disk as `events.csv` with an error body
inside it. Its own comment claimed that was handled; now it is.

Pinned by `test_the_quick_csv_refuses_past_its_row_cap_rather_than_truncating`, which also
asserts the full export still comes back complete.

---

### 6. No rate limit outside the auth routes — MEDIUM · fixed

`/auth/login` and `/participants/login` were throttled; `/sync`, `/events`, `/exports` and
`/events.csv` were not.

`dependencies.rate_limit` is an in-process fixed window, 120 requests per minute per socket
peer, applied to the sync and research routers. `/health` is deliberately outside it — it is
how the operator learns the server is up and must answer when everything else is refusing.

It is separate from the login throttle on purpose: that one counts *failures*, to make password
guessing expensive, which is the wrong shape where every request is legitimate and the cost is
volume.

**Sized to be unreachable in normal use**, which is the point rather than a compromise: the
device syncs once every five minutes and the dashboard has no auto-refresh, so this is several
hundred times either one's rate. Throttling a device draining a backlog would slow the study's
data collection to stop an attack nobody is mounting.
`test_the_rate_limit_cannot_be_reached_by_the_app_or_the_dashboard` exists to stop it being
tuned down to something the app can hit.

**Ceiling to know about:** in-memory and per-process, so it resets on restart and does not span
workers. Run this with `--workers` or behind a load balancer and it stops being a limit and
wants replacing rather than tuning.

---

### 7 and 9. `AUTH_ENABLED=false`, and the shipped default API key — MEDIUM / LOW · fixed

One mistake in two settings, and what made both dangerous is that neither announced itself: the
server started, `/health` said ok, the dashboard worked, and the whole dataset was public.
`AUTH_ENABLED=false` was the worse of the two — `require_api_key` returns early on it, so it
disabled the device key **and** the operator session check together.

`config.verify_deployment_is_safe` runs at import in `main.py`, so a misconfigured deployment
fails before uvicorn binds a port. It keys on `ENVIRONMENT == "development"`, which is exactly
where both settings are legitimate. Pinned by
`test_development_shortcuts_are_refused_outside_development`.

---

## Open — needs a decision, not code

### 8. `/participants/register` is open to any key holder — LOW/MEDIUM

Anyone with the APK can enrol unlimited participants and inject events under them, polluting
the dataset. The rate limit in (6) bounds the speed but not the principle.

**This is a study-design question rather than a bug**, which is why it is the one finding left:
if enrolment is meant to be deliberate it needs a study-issued code, and somebody has to decide
what issues those codes and how a participant receives one. Building a code system before that
is decided would be guessing at the study's enrolment process.

---

## Checked and found sound

- **SQL injection** — every query goes through SQLAlchemy expressions with bound parameters.
  Nothing interpolates a user string into SQL.
- **Path traversal on `/exports/{id}/download`** — resolves the path and checks the configured
  export root is among its parents before serving. A doctored `file_path` row is refused.
- **Password storage** — scrypt at OWASP parameters, self-describing record, `compare_digest`
  for verification. No fallback to a fast hash.
- **Login enumeration** — wrong password, unknown user and disabled account all return one
  identical 401. Registration deliberately differs (409), which is the right trade.
- **Session tokens** — opaque, server-stored, only the SHA-256 persisted, revoked immediately on
  logout and on password change.
- **Batch replay** — dedup on both `event_id` and `batch_id`, so a retried batch reports the
  original counts rather than double-inserting.
- **Identity separation** — `participant_accounts` is a separate table; the research listing
  never exposes a username.

---

## Standing caveat on all of the above

Every green test in this repo runs against **SQLite** via the `get_db` override in
`tests/conftest.py`. PostgreSQL has never been connected. Anything Postgres-specific — JSONB
behaviour, `ON CONFLICT`, transaction isolation, connection-pool exhaustion under the N+1 in (3)
— is currently untested and could behave differently in production.
