# Running the study backend on Google Cloud

One VM, one command, three scripts in `deploy/gcp/`. This replaces the laptop for the study
round; the laptop stays the development machine.

Nothing about the code changes. Every setting the service reads is already an environment
variable (`api/app/config.py`) and the app reaches the backend by URL, so this is a deployment
job rather than a development one.

---

## 0. What you need before you start

> **First time using a cloud at all?** Follow `GCP_FIRST_TIME_SETUP.md` instead — it starts from
> making the account and claiming the free credit and ends where this doc's §1 begins. This
> section is the terse version for someone who has done cloud before.

| | |
|---|---|
| A Google Cloud project with billing enabled | The $300 / 90-day free trial credit covers this study comfortably. Billing still has to be *enabled* - the credit is spent against it. |
| `gcloud` on your machine | <https://cloud.google.com/sdk/docs/install>. Then `gcloud init` and `gcloud auth login`. |
| The Compute Engine API turned on | `gcloud services enable compute.googleapis.com` |
| A bash shell | Git Bash on Windows is fine. The scripts are POSIX sh + gcloud, nothing else. |

That is the whole prerequisite list. There is no Docker, Python or PostgreSQL to install here -
all three are installed on the VM by its own startup script, and the model is downloaded there
directly rather than uploaded from here.

---

## 1. Deploy

```bash
gcloud config set project <your-project-id>
bash deploy/gcp/deploy.sh
```

First run takes about **15 minutes**, nearly all of it the 2.4 GB model download on the VM.
It prints the study URL and finishes with a `/health` check.

What it does, in order:

1. Creates `quitsmoke-study` - `e2-standard-4` (4 vCPU, 16 GB), Ubuntu 24.04, 50 GB disk, in
   `asia-south1` (Mumbai).
2. Opens **only** 80 and 443.
3. Waits for the VM's startup script to install Docker and fetch the model.
4. Generates the database password and the device API key **on the VM** - no secret is typed
   here or sent over the wire.
5. Copies this working copy up over SSH and runs `docker compose up -d --build`.
6. Runs the migrations as their own container, so a bad migration is a failed deploy rather
   than an API that starts and then errors on every query.

Run it again any time to ship a change. It skips creation, replaces the source and restarts.

### With a GPU instead

```bash
bash deploy/gcp/deploy.sh --gpu
```

`n1-standard-4` plus one NVIDIA T4. The startup script installs the driver and the container
toolkit, and the `gpu` compose profile runs the same model with `-ngl 99`. See §5 for whether
this is worth it.

---

## 2. Point the app at it

`local.properties` in the **app** repo, then rebuild:

```
research.baseUrl=https://34-93-1-2.sslip.io
research.apiKey=<from: bash deploy/gcp/vm.sh key>
```

**The address is baked in at build time.** One APK reaches one server, so changing the backend
address means rebuilding and reinstalling on every device. That is why §6's reserved IP matters
more than it looks.

---

## 3. Running it day to day

Everything goes through one script.

```bash
bash deploy/gcp/vm.sh status      # VM state, containers, and what /health says about the model
bash deploy/gcp/vm.sh logs api    # follow one service: api, llama, caddy, db
bash deploy/gcp/vm.sh restart     # restart the containers, keeping the database
bash deploy/gcp/vm.sh data        # who is using the app, and when each was last seen
bash deploy/gcp/vm.sh feedback    # what participants typed into the feedback screen
bash deploy/gcp/vm.sh backup      # pg_dump, downloaded to ./backups here
bash deploy/gcp/vm.sh key         # the API key the phones need
bash deploy/gcp/vm.sh stop        # stop the VM - billing stops, the disk is kept
bash deploy/gcp/vm.sh start       # start it again
bash deploy/gcp/vm.sh ssh         # a shell on the VM
```

`status` prints `/health` rather than just "the containers are up", because **the database being
up says nothing about the model**. When `llama` is down every reply silently becomes the
deterministic fallback and nothing on the participant's screen says so - that is the failure
worth being able to see.

### Getting the research data out

```bash
bash deploy/gcp/vm.sh backup            # pg_dump of everything, downloaded here
bash deploy/gcp/vm.sh export csv        # a research export - csv, json or xlsx
```

`export` posts to `/api/v1/exports` rather than reading the database directly. That is
deliberate: `services/export.py` names its tables explicitly so a new table cannot join an
export by accident, and `test_backups_never_reach_an_export` asserts the outcome across every
research read. A script that went round the route would throw both away.

Exports land in `/opt/quitsmoke/exports` on the VM, which is outside the source tree and so
survives every redeploy. Copy them down with:

```bash
gcloud compute scp --zone asia-south1-c --recurse \
    quitsmoke-study:/opt/quitsmoke/exports ./exports
```

Spec 23 §14 wants a **restore proven, not just a dump taken**. Do one restore into a scratch
database early in the round; a backup nobody has restored is not a backup.

---

## 4. What is where on the VM

```
/opt/quitsmoke/
  .env          generated once, root-only. The API key is in here.
  models/       the GGUF
  exports/      research exports
  backups/
  app/          this repo, REPLACED WHOLESALE on every deploy
```

The split is deliberate: `app/` is thrown away and rewritten each time, so a file deleted in the
repo actually stops existing on the VM. Everything the study accumulates lives outside it and is
symlinked in, so a redeploy cannot stand on it.

---

## 5. CPU or GPU

The API and the database are trivial to host. **The model is the entire sizing question.**

| | CPU (`e2-standard-4`) | GPU (`n1-standard-4` + T4) |
|---|---|---|
| a reply | seconds - measured at ~7 tokens/sec on comparable hardware | ~1.3 s, measured on the RTX 3050 |
| driver setup | none | installed by the startup script, and it is the part that can fail |
| roughly | the cheaper half of the credit | several times that |

Start on CPU. The reason is not cost - it is that a CPU VM has no driver, no toolkit and no GPU
quota request between you and a working study, and the fallback path means a slow model is never
an outage. Switch with `deploy.sh --gpu` if replies feel too slow with real participants.

`docs/HARDWARE_AND_HOSTING.md` has the measurement this table is built on.

**Prices change and I cannot see your billing account.** Check the real figure in the console
before you commit: <https://cloud.google.com/products/calculator>. The lever that actually
matters is `vm.sh stop` - a stopped VM bills only for its disk.

---

## 6. Before real participants

Honest list. None of these block a pilot; all of them matter for a study round.

- **Reserve the external IP.** It is ephemeral by default, so stopping and starting the VM can
  change it - and the address is compiled into the APK. One command, and it should be done
  before any phone is handed out:
  ```bash
  gcloud compute addresses create quitsmoke-ip --region asia-south1
  ```
  then attach it to the instance. Without this, `vm.sh stop` overnight is a redistribution
  risk rather than a saving.
- **TLS is on and it is not decoration.** Caddy gets a Let's Encrypt certificate for
  `<ip>.sslip.io` automatically. Every sync, login and chat turn carries participant text; the
  release APK refuses cleartext anyway. If you get a real domain, set `PUBLIC_HOST` in
  `/opt/quitsmoke/.env` and restart Caddy.
- **`ENVIRONMENT=production` is load-bearing.** `verify_deployment_is_safe` refuses to start
  with the shipped default API key or with `AUTH_ENABLED=false` outside development, because
  neither announces itself at runtime - the server would start, `/health` would say ok, and the
  dataset would be served to anyone who asked. `deploy.sh` sets it.
- **No automated backup schedule.** `vm.sh backup` is manual. A cron on your own machine calling
  it is enough.
- **No log shipping.** Logs are the containers' stdout and stay on the VM.
- **SSH is open to the internet** on the default VPC. Restricting it to IAP
  (`--tunnel-through-iap` plus a firewall rule for `35.235.240.0/20`) is the hardening step if
  the study's ethics approval asks for one.

---

## 7. When something is wrong

| symptom | look at |
|---|---|
| `https://` does not resolve or the certificate is untrusted | `vm.sh logs caddy` - Let's Encrypt takes a minute on first boot and says why when it refuses |
| the app works but every reply sounds generic | `vm.sh status`. If `model` is not `ok`, `vm.sh logs llama`. A cold start reads 2.4 GB before it answers. |
| sign-in fails, nothing syncs | `vm.sh logs api`, then `vm.sh status` for the database |
| the deploy hung on "waiting for the first boot" | `vm.sh ssh`, then `sudo journalctl -u google-startup-scripts -f`. Almost always the model download. |
| nothing answers at all | `vm.sh status` - the VM may simply be stopped |
