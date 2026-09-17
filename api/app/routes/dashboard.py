"""A single-page operator view of the research dataset.

Spec 23 §14 wants the team to be able to confirm devices are syncing and pull exports. This
serves that with one self-contained HTML page rather than a separate frontend project.

The page itself is unauthenticated because it contains no data — every figure on it is fetched
by the browser from the authenticated `/api/v1/*` routes, and the operator pastes the API key
once per browser session. That keeps the key out of the served HTML and out of the server logs,
and means this route leaks nothing if the port is ever exposed.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quit Smoke Research</title>
<style>
  :root {
    --bg: #F6F4F0; --surface: #FFFFFF; --ink: #131B1A; --muted: #6C7C79;
    --faint: #8A938F; --primary: #0F7A6B; --hairline: rgba(19,27,26,.10);
    --danger: #A9483C;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
    padding: 28px 22px 60px; max-width: 1180px; margin-inline: auto;
  }
  h1 { font-size: 24px; letter-spacing: -.4px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13.5px; margin: 0 0 24px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 22px; }
  input, select, button {
    height: 40px; border-radius: 11px; border: 1.2px solid var(--hairline);
    background: var(--surface); color: var(--ink); padding: 0 12px; font-size: 14px;
  }
  button { background: var(--primary); color: #fff; border: 0; font-weight: 600; cursor: pointer; padding: 0 16px; }
  button.ghost { background: var(--surface); color: var(--primary); border: 1.2px solid var(--hairline); }
  button:disabled { opacity: .5; cursor: default; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 26px; }
  .tile { background: var(--surface); border-radius: 16px; padding: 16px 18px; border: 1px solid var(--hairline); }
  .tile b { display: block; font-size: 26px; font-weight: 600; letter-spacing: -.5px; }
  .tile span { color: var(--faint); font-size: 12.5px; }
  section { margin-bottom: 30px; }
  h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .9px; color: var(--faint); margin: 0 0 10px; }
  .scroll { overflow-x: auto; background: var(--surface); border: 1px solid var(--hairline); border-radius: 16px; }
  table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
  th, td { text-align: left; padding: 11px 14px; border-bottom: 1px solid var(--hairline); white-space: nowrap; }
  th { color: var(--faint); font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: .7px; }
  tr:last-child td { border-bottom: 0; }
  td.payload { max-width: 380px; overflow: hidden; text-overflow: ellipsis; font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--muted); }
  .note { color: var(--muted); font-size: 13px; }
  .err { color: var(--danger); font-size: 13.5px; }
  code { background: #EFECE7; padding: 1px 6px; border-radius: 5px; font-size: 12.5px; }
</style>
</head>
<body>
<h1>Quit Smoke research data</h1>
<p class="sub">Everything the phones have synchronised. Read-only; exports never modify the source.</p>

<div class="row" id="signin">
  <input id="username" placeholder="username" autocomplete="username" style="width:180px">
  <input id="password" type="password" placeholder="password" autocomplete="current-password" style="width:200px">
  <button id="connect">Sign in</button>
  <span id="status" class="note"></span>
</div>

<div class="row" id="signedin" hidden>
  <span class="note">Signed in as <strong id="who"></strong></span>
  <button id="refresh" class="ghost">Refresh</button>
  <button id="signout" class="ghost">Sign out</button>
  <span id="status2" class="note"></span>
</div>

<div id="body" hidden>
  <div class="tiles" id="tiles"></div>

  <section>
    <h2>Export</h2>
    <div class="row">
      <select id="format"><option value="csv">CSV</option><option value="json">JSON</option><option value="xlsx">XLSX</option></select>
      <input id="scopeParticipant" placeholder="participant id (optional)" style="width:220px">
      <button id="export">Create export</button>
      <a id="quick" href="#" class="note">or download all events as CSV now</a>
    </div>
    <div id="exportResult" class="note"></div>
  </section>

  <section>
    <h2>Participants</h2>
    <div class="scroll"><table id="participants"></table></div>
  </section>

  <section>
    <h2>Recent events</h2>
    <div class="row">
      <input id="filterParticipant" placeholder="filter by participant" style="width:220px">
      <input id="filterType" placeholder="filter by event type" style="width:220px">
      <button id="applyFilter" class="ghost">Apply</button>
    </div>
    <div class="scroll"><table id="events"></table></div>
  </section>

  <section>
    <h2>Export history</h2>
    <div class="scroll"><table id="exports"></table></div>
  </section>
</div>

<p id="hint" class="note">Sign in with your operator account. Create one with
<code>uv run python scripts/manage_users.py create &lt;username&gt;</code>.</p>

<script>
const $ = (id) => document.getElementById(id);

// No credential is held in this page at all. The session lives in an HttpOnly cookie the
// browser attaches automatically, so a script on this page cannot read it and a shared API key
// is no longer pasted into a form. `credentials: "same-origin"` is what sends it.
async function api(path) {
  const response = await fetch(path, { credentials: "same-origin" });
  if (response.status === 401) throw new Error("Sign in required");
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

function showSignedIn(user) {
  $("who").textContent = user.display_name ? user.display_name + " (" + user.username + ")" : user.username;
  $("signin").hidden = true;
  $("signedin").hidden = false;
  $("hint").hidden = true;
}

function showSignedOut(message) {
  $("signin").hidden = false;
  $("signedin").hidden = true;
  $("hint").hidden = false;
  $("body").hidden = true;
  $("status").innerHTML = message ? '<span class="err">' + escapeHtml(message) + "</span>" : "";
}

function table(node, columns, rows, render) {
  if (!rows.length) {
    node.innerHTML = '<tr><td class="note">Nothing yet.</td></tr>';
    return;
  }
  const head = "<tr>" + columns.map((c) => "<th>" + c + "</th>").join("") + "</tr>";
  node.innerHTML = head + rows.map(render).join("");
}

// escapeHtml, not raw interpolation. Every value passed to cell() — participant_id,
// app_version, event_type, session_id, event_id — arrives from a device over /sync, so it is
// attacker-controlled by anyone holding the API key (which is extractable from the APK). Without
// escaping, an event_type of "<img src=x onerror=...>" is stored XSS that executes in a
// signed-in researcher's browser and can drive the export endpoints as them.
const cell = (value) => "<td>" + (value === null || value === undefined ? "" : escapeHtml(value)) + "</td>";
const shortTime = (value) => (value ? String(value).replace("T", " ").slice(0, 19) : "");

async function load() {
  // status2 lives in the signed-in row; #status is in the sign-in row, which is hidden by then.
  const say = (text, isError) => {
    $("status2").textContent = text;
    $("status2").className = isError ? "err" : "note";
  };
  say("loading…", false);
  try {
    const [stats, participants, events, exports] = await Promise.all([
      api("/api/v1/stats"),
      api("/api/v1/participants"),
      api("/api/v1/events?limit=100" + filterQuery()),
      api("/api/v1/exports"),
    ]);

    $("tiles").innerHTML = [
      ["Participants", stats.participants],
      ["Events", stats.events],
      ["Sync batches", stats.batches],
      ["Last event", shortTime(stats.latest_event_at) || "—"],
      ["Last sync", shortTime(stats.latest_sync_at) || "—"],
    ].map(([label, value]) => `<div class="tile"><b>${value}</b><span>${label}</span></div>`).join("");

    table($("participants"),
      ["Participant", "App", "First seen", "Last seen", "Events", "Batches"],
      participants,
      (p) => "<tr>" + cell(p.participant_id) + cell(p.app_version) + cell(shortTime(p.first_seen_at))
           + cell(shortTime(p.last_seen_at)) + cell(p.event_count) + cell(p.batch_count) + "</tr>");

    table($("events"),
      ["Event id", "Participant", "Type", "When", "Session", "Payload"],
      events.items,
      (e) => "<tr>" + cell(e.event_id) + cell(e.participant_id) + cell(e.event_type)
           + cell(shortTime(e.event_timestamp)) + cell(e.session_id)
           + '<td class="payload">' + escapeHtml(typeof e.payload === "string" ? e.payload : JSON.stringify(e.payload)) + "</td></tr>");

    table($("exports"),
      ["Created", "Format", "Status", "Rows", "File"],
      exports,
      (x) => "<tr>" + cell(shortTime(x.created_at)) + cell(x.format) + cell(x.status) + cell(x.row_count)
           + '<td><a href="/api/v1/exports/' + x.export_id + '/download">download</a></td></tr>');

    $("body").hidden = false;
    $("hint").hidden = true;
    say("updated " + new Date().toLocaleTimeString(), false);
  } catch (error) {
    // A session that expired while the page was open lands here; showing the login row again is
    // more useful than an error next to stale data.
    if (error.message === "Sign in required") {
      showSignedOut("Session expired. Sign in again.");
      return;
    }
    say(error.message, true);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function filterQuery() {
  const participant = $("filterParticipant").value.trim();
  const type = $("filterType").value.trim();
  let query = "";
  if (participant) query += "&participant_id=" + encodeURIComponent(participant);
  if (type) query += "&event_type=" + encodeURIComponent(type);
  return query;
}

$("connect").onclick = async () => {
  $("status").textContent = "signing in…";
  const response = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ username: $("username").value.trim(), password: $("password").value }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    // The server sends one message for every failure so the page cannot become a username
    // oracle; 429 carries the lockout message instead.
    showSignedOut(body.detail || "Sign in failed");
    return;
  }
  // Cleared immediately: no reason for the password to sit in the DOM after this point.
  $("password").value = "";
  $("status").textContent = "";
  showSignedIn(body);
  load();
};

$("signout").onclick = async () => {
  await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" });
  showSignedOut("");
};

// Enter submits, which is what anyone typing a password expects.
$("password").onkeydown = (event) => { if (event.key === "Enter") $("connect").click(); };
$("username").onkeydown = (event) => { if (event.key === "Enter") $("password").focus(); };

$("refresh").onclick = load;
$("applyFilter").onclick = load;

$("export").onclick = async () => {
  const payload = { format: $("format").value };
  const participant = $("scopeParticipant").value.trim();
  if (participant) payload.participant_id = participant;

  $("exportResult").textContent = "exporting…";
  const response = await fetch("/api/v1/exports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) {
    $("exportResult").innerHTML = '<span class="err">' + escapeHtml(body.detail || "export failed") + "</span>";
    return;
  }
  $("exportResult").innerHTML = "Wrote <code>" + escapeHtml(body.file_path) + "</code> ("
    + body.row_count + " rows) &middot; <a href=\\"/api/v1/exports/" + body.export_id + "/download\\">download</a>";
  load();
};

$("quick").onclick = (event) => {
  event.preventDefault();
  // Fetched rather than linked so no credential appears in a URL, and so a 401 is visible
  // as an error instead of rendering the login page into a downloaded file.
  fetch("/api/v1/events.csv" + ("?" + filterQuery().slice(1)), { credentials: "same-origin" })
    .then((r) => {
      // The `ok` check is the point of fetching rather than linking. Without it a 401 or the
      // 413 the endpoint raises past its row cap is saved as `events.csv`, so an error body
      // reaches the researcher's disk looking exactly like data.
      if (!r.ok) return r.json().then((b) => { throw new Error(b.detail || ("HTTP " + r.status)); });
      return r.blob();
    })
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "events.csv";
      anchor.click();
      URL.revokeObjectURL(url);
    })
    .catch((error) => { $("exportResult").textContent = String(error.message || error); });
};

// Resume a session that is still valid, so a refresh does not ask for the password again.
fetch("/api/v1/auth/me", { credentials: "same-origin" })
  .then((response) => (response.ok ? response.json() : null))
  .then((user) => {
    if (!user) return;
    showSignedIn(user);
    load();
  })
  .catch(() => {});
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    return HTMLResponse(_PAGE)
