"""Two participants end to end against the live stack - the phone's actual path.

Not a unit test, and it is not trying to be. `pytest` runs against SQLite through a `get_db`
override, so it structurally cannot see the tunnel, PostgreSQL's foreign keys, or whether the
model is even running. Every one of those has produced a defect that the green suite missed.

    uv run python scripts/smoke_stack.py                      # through the tunnel
    uv run python scripts/smoke_stack.py http://127.0.0.1:8000  # straight at the API

It CREATES DATA - two accounts, eight events, two backups - and deliberately does not clean up,
so the results stay inspectable in the dashboard afterwards. Follow it with
`reset_participants.py` before a real test round.

What it pins that the unit suite cannot:

* the model answers through whatever hop the phone uses, and how fast
* a replayed sync batch adds no duplicate rows (asserted on stored counts, not the reply's
  counters - the server short-circuits on batch_id and echoes the first acknowledgement)
* one participant's restore never contains another's message text
* the device key alone cannot GET a backup, in three URL shapes
* message text never reaches an export, checked against the downloaded bytes
* deletion closes the account, blocks that login, and leaves the other participant intact
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from pathlib import Path

DEFAULT_BASE = "https://favorably-immature-fountain.ngrok-free.dev"
BASE = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")

ENV = Path(__file__).resolve().parent.parent / ".env"
KEY = [
    line.split("=", 1)[1].strip()
    for line in ENV.read_text(encoding="utf-8").splitlines()
    if line.startswith("DEVELOPMENT_API_KEY=")
][0]

# Usernames are reused every run, so a previous run's accounts have to be gone first. That is
# reset_participants.py's job, not this script's - clearing up after itself would also destroy the
# evidence somebody is about to want to look at.
print(f"Target: {BASE}")

# The server rejects a password containing the username, so these deliberately do not.
PASSWORDS = {"abhinay": "QuitStudy!7412", "kaushik": "StudyRound!9358"}

failures: list[str] = []
notes: list[str] = []


def call(method: str, path: str, body=None, expect=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", KEY)
    req.add_header("ngrok-skip-browser-warning", "1")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = raw[:400].decode(errors="replace")
    if expect is not None and status != expect:
        failures.append(f"{method} {path} -> {status}, expected {expect}: {parsed}")
    return status, parsed


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(f"{label}{(': ' + detail) if detail else ''}")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


now = datetime.now(timezone.utc)

# ---------------------------------------------------------------- A. sign up both
print("\n[A] Sign up (no logins - the app only offers signup on a clean install)")
ids: dict[str, str] = {}
for name, password in PASSWORDS.items():
    status, body = call("POST", "/api/v1/participants/register",
                        {"username": name, "password": password}, expect=201)
    if status == 201:
        ids[name] = body["participant_id"]
        check(f"{name} registered", True, body["participant_id"])
    else:
        check(f"{name} registered", False, str(body))

if len(ids) != 2:
    print("\nCannot continue without both accounts.")
    sys.exit(1)

check("the two participant ids differ", ids["abhinay"] != ids["kaushik"])

# ---------------------------------------------------------------- B. the model answers
print("\n[B] Chat - the model wording a reply, through the tunnel")
PROMPT = (
    "<|im_start|>system\nYou are a supportive tobacco cessation coach. Reply in one short "
    "sentence.<|im_end|>\n<|im_start|>user\n{msg}<|im_end|>\n<|im_start|>assistant\n"
    "<think>\n\n</think>\n\n"
)
for name, msg in (("abhinay", "I am craving a cigarette after lunch."),
                  ("kaushik", "I had a stressful day and want to smoke.")):
    status, body = call("POST", "/api/v1/generate",
                        {"prompt": PROMPT.format(msg=msg), "max_tokens": 48}, expect=200)
    if status == 200:
        text = body.get("text", "").strip()
        check(f"{name}: model replied", bool(text),
              f"{body.get('generation_ms')}ms, {body.get('token_count')} tok")
        notes.append(f"{name} reply: {text[:90]}")
    else:
        check(f"{name}: model replied", False, str(body)[:200])

# ---------------------------------------------------------------- C. research sync
print("\n[C] Research sync - the study's data path")
EVENTS = [
    ("session_started", {"resumed": "false"}),
    ("craving_reported", {"intensity": "7", "trigger": "after_meal"}),
    ("coping_selected", {"coping_id": "COPE_WALK", "outcome": "helped"}),
    ("lapse_reported", {"count": "1"}),
]
replayed: list = []
for name, pid in ids.items():
    records = []
    for i, (etype, payload) in enumerate(EVENTS):
        stamp = iso(now - timedelta(minutes=10 - i))
        records.append({
            "event_id": f"evt-{name}-{i}",
            "participant_id": pid,
            "event_type": etype,
            "timestamp": stamp,
            "session_id": f"s-{name}-1",
            # payload is a JSON *string*, not a nested object - one of the three pinned traps.
            "payload": json.dumps(payload),
            "schema_version": "1",
            "created_at": stamp,
        })
    batch = {
        "batch_id": f"batch-{name}-1",
        "participant_id": pid,
        "app_version": "1.0",
        "schema_version": "1",
        "records": records,
    }
    status, body = call("POST", "/api/v1/sync", batch, expect=200)
    if status == 200:
        check(f"{name}: {len(records)} events accepted",
              body.get("accepted_count") == len(records) and body.get("rejected_count") == 0,
              str(body))
        # Replaying the same batch must not double-count. The server short-circuits on
        # batch_id and echoes the original acknowledgement, so the counters in the reply are
        # the FIRST call's - which is why this asserts the stored row count instead. The
        # counters are the implementation; "no duplicate rows" is the requirement.
        call("POST", "/api/v1/sync", batch, expect=200)
        _, st = call("GET", "/api/v1/stats", expect=200)
        replayed.append((name, st.get("events")))
    else:
        check(f"{name}: events accepted", False, str(body)[:200])

expected = len(ids) * len(EVENTS)
_, st = call("GET", "/api/v1/stats", expect=200)
check("replaying every batch stored no duplicate rows",
      st.get("events") == expected, f"{st.get('events')} events, expected {expected}")
check("and no duplicate batches", st.get("batches") == len(ids),
      f"{st.get('batches')} batches")

# ---------------------------------------------------------------- D. account backup
print("\n[D] Account backup - the participant's own copy, with message text in it")
SECRET = {"abhinay": "ABHINAY_PRIVATE_MESSAGE_TEXT", "kaushik": "KAUSHIK_PRIVATE_MESSAGE_TEXT"}
for name, pid in ids.items():
    payload = {
        "profile": {"participantId": pid, "tobaccoType": "cigarette"},
        "memories": [{"key": "interest:music", "value": "true"}],
        "sessions": [{"sessionId": f"s-{name}-1"}],
        "messages": [{"role": "user", "content": SECRET[name]}],
    }
    status, body = call("PUT", "/api/v1/backup", {
        "participant_id": pid,
        "schema_version": "1",
        "device_updated_at": iso(now),
        "payload": payload,
    }, expect=200)
    check(f"{name}: backup stored", status == 200 and body.get("stored") is True, str(body))

# ---------------------------------------------------------------- E. restore, and isolation
print("\n[E] Restore on login - and one participant must never get the other's")
for name, password in PASSWORDS.items():
    status, body = call("POST", "/api/v1/participants/login",
                        {"username": name, "password": password, "include_backup": True},
                        expect=200)
    if status != 200:
        check(f"{name}: login returns a backup", False, str(body)[:200])
        continue
    backup = body.get("backup")
    blob = json.dumps(backup)
    check(f"{name}: login returned their backup", backup is not None)
    check(f"{name}: it contains their own message text", SECRET[name] in blob)
    other = "kaushik" if name == "abhinay" else "abhinay"
    check(f"{name}: it does NOT contain {other}'s text", SECRET[other] not in blob)

print("\n  the device key alone must never read a backup:")
for shape in (f"/api/v1/backup?participant_id={ids['abhinay']}",
              f"/api/v1/backup/{ids['abhinay']}",
              f"/api/v1/participants/{ids['abhinay']}/backup"):
    status, _ = call("GET", shape)
    check(f"GET {shape} is refused", status in (404, 405, 403, 401, 422), f"got {status}")

# ---------------------------------------------------------------- F. operator view
print("\n[F] What you see in the backend")
status, stats = call("GET", "/api/v1/stats", expect=200)
if status == 200:
    print(f"      stats: {json.dumps(stats)[:300]}")
    check("stats counts both participants and their events",
          stats.get("participants") == len(ids) and stats.get("events") == expected,
          f"participants={stats.get('participants')} events={stats.get('events')}")

status, plist = call("GET", "/api/v1/participants", expect=200)
if status == 200:
    seen = {p.get("participant_id") for p in plist}
    check("both participants listed", set(ids.values()) <= seen,
          f"{len(plist)} listed")
    for p in plist:
        print(f"      {p.get('participant_id')}  events={p.get('event_count')}  "
              f"batches={p.get('batch_count')}")

status, page = call("GET", "/api/v1/events?limit=50", expect=200)
if status == 200:
    rows = page.get("items") or page.get("events") or []
    kinds = sorted({r.get("event_type") for r in rows})
    check("every event type arrived", len(kinds) >= 4, ", ".join(k for k in kinds if k))

# ---------------------------------------------------------------- G. export
print("\n[G] Export - and message text must not be in it")
status, run = call("POST", "/api/v1/exports", {"format": "csv"}, expect=201)
if status == 201:
    print(f"      export: {run.get('row_count')} rows -> {run.get('file_path') or run.get('path')}")
    check("the export has rows", (run.get("row_count") or 0) >= 8)
    eid = run.get("export_id") or run.get("id")
    req = urllib.request.Request(f"{BASE}/api/v1/exports/{eid}/download")
    req.add_header("X-API-Key", KEY)
    req.add_header("ngrok-skip-browser-warning", "1")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            csv_text = r.read().decode(errors="replace")
        check("export downloads", True, f"{len(csv_text)} bytes")
        for name in PASSWORDS:
            check(f"{name}'s message text is ABSENT from the export",
                  SECRET[name] not in csv_text)
        check("both participant ids appear in the export",
              all(pid in csv_text for pid in ids.values()))
    except urllib.error.HTTPError as e:
        check("export downloads", False, f"{e.code}")
else:
    check("export created", False, str(run)[:200])

# ---------------------------------------------------------------- H. deletion
print("\n[H] Kaushik requests deletion - abhinay must be untouched")
status, body = call("DELETE", f"/api/v1/backup?participant_id={ids['kaushik']}", expect=200)
check("kaushik's backup deleted", status == 200 and body.get("deleted") is True, str(body))

status, body = call("DELETE", f"/api/v1/participants/account?participant_id={ids['kaushik']}",
                    expect=200)
check("kaushik's account closed", status == 200 and body.get("account_removed") is True, str(body))

status, body = call("POST", "/api/v1/participants/login",
                    {"username": "kaushik", "password": PASSWORDS["kaushik"]})
check("kaushik can no longer log in", status == 401, f"got {status}")

status, body = call("POST", "/api/v1/participants/register",
                    {"username": "kaushik", "password": "FreshStart!5521"})
check("the deleted username is BLOCKED, not freed", status == 409, f"got {status}")
if status == 409:
    check("and the message says why rather than just 'taken'",
          "deleted account" in str(body.get("detail", "")), str(body))

status, body = call("POST", "/api/v1/participants/register",
                    {"username": "kaushik_new", "password": "FreshStart!5521"})
check("but a NEW username still works - they must be able to sign up again",
      status == 201, f"got {status}")
if status == 201:
    check("and it is a different participant id", body["participant_id"] != ids["kaushik"])

status, body = call("POST", "/api/v1/participants/login",
                    {"username": "abhinay", "password": PASSWORDS["abhinay"],
                     "include_backup": True}, expect=200)
if status == 200:
    check("abhinay's login still works", True)
    check("abhinay's backup survived kaushik's deletion",
          body.get("backup") is not None and SECRET["abhinay"] in json.dumps(body.get("backup")))

status, plist = call("GET", "/api/v1/participants", expect=200)
if status == 200:
    still = {p.get("participant_id") for p in plist}
    check("kaushik's research events survive by design (study data, pseudonymous)",
          ids["kaushik"] in still, "as specified")

status, body = call("DELETE", f"/api/v1/participants/account?participant_id={ids['kaushik']}",
                    expect=200)
check("closing an already-closed account is idempotent",
      status == 200 and body.get("account_removed") is False, str(body))

# ---------------------------------------------------------------- summary
print("\n" + "=" * 74)
for n in notes:
    print("  " + n)
print("=" * 74)
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("\nAll checks passed.")
