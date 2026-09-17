# Running the study

Everything needed to keep participants able to use the app, and to see what they are producing.
Written for the person on duty rather than for whoever wrote the code.

`GCP_DEPLOYMENT.md` covers the study's Google Cloud VM. This covers running the stack
wherever it is, including the development laptop.

## The three processes

There is no single "the backend". Three things run, they fail independently, and **the app fails
differently for each** — which is why it matters to know which one is down.

| process | port | if it stops |
|---|---|---|
| API (`uvicorn`) | 8000 | Sign-in fails and nothing syncs. The app still works offline. |
| Model (`llama-server`) | 8081 | **Every reply becomes the fallback, and nothing on screen says so.** |
| Tunnel (`ngrok`) | 4040 | Phones outside this network cannot reach any of it. |

The middle row is the one that bites. On 29 August the app appeared to hold conversations for a
whole day while the model had served **zero completions** — the fallback is deliberately good
enough that nobody noticed. **Check the model separately; do not infer it from replies arriving.**

## Running it from VS Code

The way to do it if you want to watch it. Open this folder in VS Code, then:

**Ctrl+Shift+P** -> `Tasks: Run Task` -> **Run study stack**

Three terminals open, one per service, and you can read the log and press Ctrl+C in any of them.
Other tasks in the same menu: **Health check**, **Show study data**, and **Clear all participant
data**.

The API task runs with `--reload`, so editing anything under `api/` restarts it automatically -
which is the difference that matters from the scripted version below, where a stale process
served pre-edit code and a route answered 405 while its tests passed.

**The tunnel is only needed for a phone that is not on this network.** Testing on the same wifi
does not need it - point the app's `research.baseUrl` at this machine's LAN address instead.

## Starting and stopping

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_stack.ps1
```

Starts all three, waits, then probes each and prints what is actually true rather than "started".
It ends with the participant-facing URL when they can reach it, and a red line when they cannot.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop_stack.ps1
```

And to ask whether participants can actually use it right now:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\health.ps1
```

Stops the three by listening port, not by process name — killing every `python.exe` to stop the
study backend is how a different day's work ends by accident. PostgreSQL is left running: it is a
service, and it holds the only data that cannot be rebuilt.

Logs are in `logs/`, one `.log` and one `.err` per process.

### Restart the API after changing anything under `api/`

`uvicorn` runs without `--reload`, so a process started before an edit **is still serving the old
code**. This is not hypothetical: it made a `DELETE` route answer `405` through the tunnel while
its own tests passed, and only an end-to-end call found it. `run_stack.ps1` prints a warning when
it finds port 8000 already occupied, for exactly this reason.

### Keeping it alive

Nothing restarts these automatically. On this laptop that is fine — the server is expected to be
off outside office hours, the app is built for it, and the phone's queue is durable.

If it needs to survive a reboot or a crash, that is the point at which it should move to a real
host. `docker-compose.yml` sets `restart: unless-stopped`, which is the right answer and
does not exist on a laptop running three loose processes.

## Is it healthy?

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/v1/generate/health
```

`/health` reports the **database**. `/generate/health` reports the **model**. They are separate
endpoints on purpose: the database stays up while the model is down, and conflating them means an
operator sees green while every chat turn is falling back.

| answer | meaning |
|---|---|
| `{"status":"ok","database":"ok"}` | API and database fine |
| `{"model":"ok"}` | inference reachable |
| `{"model":"unreachable"}` | `llama-server` is not running |
| `{"model":"not_configured"}` | `LLAMA_SERVER_URL` is empty — supported, everything falls back |

## Looking at the data

```bash
uv run python scripts/show_data.py
uv run python scripts/show_data.py --events
```

Reads PostgreSQL **directly, not through the API**, so it answers the question with the backend
switched off — which for an office-hours server is most of the time.

The per-participant columns are the ones that matter during a study. `events` says whether someone
is using the app at all; `last seen` says whether they have stopped. **A participant whose last
event is four days old is the thing to notice early, not at the end.**

### Ad-hoc queries

```bash
uv run python -c "from api.app.database import engine; from sqlalchemy import text; print([dict(r._mapping) for r in engine.connect().execute(text('SELECT event_type, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC'))])"
```

Or `psql` if it is on the path:

```bash
psql "postgresql://quit_smoke_app@127.0.0.1:5432/quit_smoke_research"
```

Useful starting points:

| question | query |
|---|---|
| Who is enrolled and when did they last do anything? | `SELECT participant_id, first_seen_at, last_seen_at FROM participants ORDER BY last_seen_at DESC;` |
| What are people actually doing? | `SELECT event_type, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC;` |
| Is anything stuck in the upload queue? | `SELECT status, count(*) FROM sync_batches GROUP BY 1;` |
| Did a craving record its trigger? | `SELECT payload->>'trigger', count(*) FROM events WHERE event_type='craving_reported' GROUP BY 1;` |

That last one is worth knowing as a shape: `payload` is a JSON **object**, so `->>` works. It was
briefly stored as a JSON string inside a JSON string, which made every such query silently return
nothing — one of the three defects connecting PostgreSQL exposed.

### Exports

`GET /api/v1/events.csv` with the API key for a quick look, or `POST /api/v1/exports` for a full
run written to `exports/`. Both cover research data only.

**`participant_backups` is never in an export.** It holds the participant's own copy of their
account — profile, answers and conversation — and is theirs rather than the study's. It is read
back only by that participant signing in with their own password. Keep it that way.

## Clearing the data

```bash
uv run python scripts/reset_participants.py
```

Truncates every participant table, including `participant_backups`, and leaves the schema and
migration history intact — no `alembic upgrade` needed afterwards. Operator accounts survive; they
are not participant data.

**There is no undo.** Take a dump first if the data might matter.

## Backups

```bash
pg_dump -U quit_smoke_app quit_smoke_research | gzip > backup-$(date +%F).sql.gz
```

Nothing schedules this. Spec 23 §14 wants a restore **proven**, not just a dump taken — and the
time to discover a backup does not restore is not the end of the study.

## When something is wrong

| symptom | first thing to check |
|---|---|
| Testers cannot sign in | Is the tunnel up? `curl https://<domain>/health`. It drops silently. |
| Replies feel generic | `/api/v1/generate/health`. The fallback is good enough to hide a dead model. |
| A route 404s or 405s that should work | The API is serving pre-edit code. Restart it. |
| No events arriving from a device | Check the consent record first — withdrawal stops collection, not just upload. |
| `alembic` cannot connect | The password must be percent-encoded in `DATABASE_URL` (`@` → `%40`). |
| A phone reaches nothing at all | `research.baseUrl` is compiled in at **build** time. One APK reaches one server. |

## The study runs on Google Cloud

See `GCP_DEPLOYMENT.md`. One VM, `bash deploy/gcp/deploy.sh`, and `deploy/gcp/vm.sh` for
everything after that. Nothing in the code changes: every setting is already environment-driven.

Three things that are different there and are easy to carry the wrong assumption into:

- **ngrok does not go with it.** The tunnel exists because this laptop has no public address. The
  VM has one, and keeping the tunnel is one more thing to fail at 2 a.m. It stays here, for
  development against a phone off this network.
- **`llama-server` IS in the compose file there**, behind a `cpu` / `gpu` profile. That reversed
  when the host stopped being "whatever the university provides" and became one VM we create.
  `LLAMA_SERVER_URL` is still the seam, and leaving it empty is still supported - the app falls
  back and still works.
- **`ENVIRONMENT=production` is load-bearing.** The service refuses to start with the shipped
  default API key or with authentication disabled unless the environment is `development`,
  because neither of those announces itself at runtime.

Then point the app at it in `local.properties` and rebuild — the address is baked in, so moving
the server means redistributing the APK.

### What the hardware needs to be

Measured on the development laptop (RTX 3050, 4 GB): about 23 tokens/sec, **2,827 of 4,096 MiB**
video memory, 1.3 s for a warm reply. Roughly 180 model calls a day across 15 participants.

This is not a machine that needs to be large. **It needs to be on.**
