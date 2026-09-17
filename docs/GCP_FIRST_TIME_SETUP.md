# Google Cloud, from nothing to live — first-timer's walkthrough

For the person doing the deployment who has never used a cloud before. It stops at the moment the
backend is running on the internet; from there `GCP_DEPLOYMENT.md` is the reference for everything
ongoing (logs, data, backups, stopping the VM).

**What actually happens here:** you make a Google Cloud account, claim the free trial, install one
tool, and run one script that this repo already contains. The script builds the whole server for
you — the API, the database, the model and HTTPS. You are not configuring any of that by hand.

Set aside about **45 minutes**, most of it waiting.

---

## Before you begin — two honest things

**1. A card is required to claim the free trial, even though nothing is charged.** Google asks for
a credit or debit card to prove you are a real person. The $300 credit is free and lasts 90 days;
you are **not** charged unless you deliberately click "Activate full account" / "Upgrade" later.
When the trial ends the servers simply pause. A 4-week study finishes long inside the 90 days.

- Indian cards sometimes get rejected at this step (an international-transactions or
  recurring-payment block on the card). If one card is refused, a different card usually works.
  This is Google's verification, not something in this project.

**2. Whoever creates the account owns the billing.** Decide now whether this account belongs to
you or to the doctor, because moving the project to a different owner later is fiddly. For a study
you are running, the simplest thing is that **you** own it and hand over access if needed.

**I cannot do this part for you** — creating the account and entering the card are yours to do, and
should be. Everything after that is one script.

---

## Part 1 · Make the account and claim the credit (browser, ~15 min)

1. Go to **<https://cloud.google.com>** and click **Get started for free** (or **Start free**).
2. Sign in with a Google account, or create one.
3. Choose your **country**, accept the terms.
4. Enter the **card** (see the note above). You will see the **$300 / 90-day** credit confirmed.
5. You land in the **Google Cloud Console**. There will be a banner showing your free credit and
   how many days are left. That banner is how you keep an eye on it during the study.

---

## Part 2 · Make a project (browser, ~5 min)

A "project" is just the box that holds this study's server and its bill.

6. At the very top of the Console there is a **project dropdown** (it may say "My First Project").
   Click it → **New Project**.
7. Name it something like **quitsmoke**. Create it.
8. **Write down the Project ID.** This is the important part, and it is *not* the same as the name:
   the ID is lower-case, often with a number on the end, like `quitsmoke-431915`. You can see it on
   the Console home page under **Project info**. You will paste this into one command later.

That is all you do in the browser. The rest is one tool and one script.

---

## Part 3 · Install the two tools (Windows, ~10 min)

The deploy script is a `bash` script that drives Google's `gcloud` command. On Windows you need
both. Install them once.

9. **Git for Windows** — this gives you "Git Bash", the terminal the script runs in.
   <https://git-scm.com/download/win> · run the installer, accept the defaults.
10. **Google Cloud CLI** — this is `gcloud`.
    <https://cloud.google.com/sdk/docs/install> → the Windows installer. Accept the defaults; when
    it offers to run `gcloud init` at the end, let it.
11. Open **Git Bash** (Start menu → "Git Bash"). Use this, **not** PowerShell — the scripts are
    bash. Check gcloud is visible:
    ```bash
    gcloud version
    ```
    If it says "command not found", close Git Bash and open it again (it needs to pick up the new
    PATH). If it still cannot find it after a reboot, run the two `gcloud` commands in Step 12–14
    from PowerShell once — but run the `deploy.sh` script itself from Git Bash.

---

## Part 4 · Connect gcloud to your account and project (~3 min)

In Git Bash:

12. Sign in — this opens a browser, sign in and click **Allow**:
    ```bash
    gcloud auth login
    ```
13. Point it at the project you made (paste your **Project ID** from Step 8):
    ```bash
    gcloud config set project quitsmoke-431915
    ```
14. Turn on the one service the script needs:
    ```bash
    gcloud services enable compute.googleapis.com
    ```
    This one can take a minute. If it complains that billing is not enabled, open the Console →
    **Billing** and link the project to your trial billing account, then run it again.

---

## Part 5 · Deploy (~15 min, mostly waiting)

15. Go to the backend folder. In Git Bash the `D:` drive is `/d`:
    ```bash
    cd "/d/ChakraVue AI/TobaccoCessationBackend"
    ```
16. Run it:
    ```bash
    bash deploy/gcp/deploy.sh
    ```

The **first time** `gcloud` connects to the new machine it creates an SSH key and may ask you to
**set a passphrase** — press Enter twice to leave it blank. It may also ask to trust the machine
("Are you sure you want to continue connecting") — type **yes**.

Then it runs on its own for about fifteen minutes. Nearly all of that is the machine downloading
the 2.4 GB model. It finishes by printing:

- the **study URL** (looks like `https://34-93-1-2.sslip.io`)
- a `/health` check
- how to get the **API key**

Leave that terminal output on screen — you need the URL and the key for the next step.

---

## Part 6 · Check it is actually live

17. Ask the machine how it is doing:
    ```bash
    bash deploy/gcp/vm.sh status
    ```
    You want the VM `RUNNING`, the containers up, and `/health` reporting the model. **If `model`
    is not `ok`, wait a couple of minutes and run it again** — on a cold start the model reads
    2.4 GB before it can answer, and the machine says the app is fine before the model is ready.
18. Open the study URL with `/health` on the end in a browser, e.g.
    `https://34-93-1-2.sslip.io/health`. You should see `{"status":"ok","database":"ok"}`. The
    certificate can take a minute the very first time; if the browser warns, wait and refresh.

That is the backend live on the internet, with HTTPS, reachable by any phone anywhere.

---

## Part 7 · One thing to do before handing out any phone

The server's address is **compiled into the app**, and by default that address can change if the
machine is ever stopped and started. Lock it so it never moves:

```bash
gcloud compute addresses create quitsmoke-ip --region asia-south1
```

Then reserve it to the machine (the Console → **VPC network → IP addresses** → find
`quitsmoke-ip` → **Change** → attach to `quitsmoke-study`). `GCP_DEPLOYMENT.md` §6 has the detail.

Do this **before** the app is built with the URL, or a later stop/start silently breaks every
installed phone.

---

## Part 8 · Point the app at it, and build

In the **app** repo, edit `local.properties`:

```
research.baseUrl=https://<your URL from Step 16>
research.apiKey=<run: bash deploy/gcp/vm.sh key>
```

Then rebuild the APK. Now the app talks to the cloud, not the laptop.

---

## About the GPU — read before you try it

`GCP_DEPLOYMENT.md` mentions `deploy.sh --gpu` for faster replies. **On a free trial it will
almost certainly fail** with a quota error: new accounts get zero GPU quota, and raising it
usually requires upgrading to a paid account first. That is Google's policy, not a bug here.

**Start on CPU — the plain `deploy.sh` with no flag.** Replies take a few seconds instead of one,
and the app is built to tolerate exactly that: while the model is slow or unavailable it uses its
built-in responses, which is never an outage. A 15-person study runs fine this way. Only chase the
GPU if replies feel too slow with real participants, and by then you will know whether to upgrade.

---

## If something goes wrong

| what you see | what to do |
|---|---|
| `deploy.sh` hangs on "waiting for the first boot" | Normal for ~10 min (model download). If much longer: `bash deploy/gcp/vm.sh ssh` then `sudo journalctl -u google-startup-scripts -f` |
| `gcloud: command not found` in Git Bash | Close and reopen Git Bash; if still missing, reboot after installing the Cloud CLI |
| "billing is not enabled" | Console → Billing → link the project to your trial account, then re-run Step 14 |
| the card is refused | Try another card — Indian cards sometimes block Google's verification |
| every reply in the app sounds generic | The model is not up: `bash deploy/gcp/vm.sh status`, then `bash deploy/gcp/vm.sh logs llama` |
| anything after deployment | It is all in `GCP_DEPLOYMENT.md` — logs, data, backups, stopping the VM to save credit |

---

## What this costs against the free credit

The CPU machine (`e2-standard-4`) left running 24/7 is roughly the cheaper half of the $300 over a
month, so a 4-week study fits inside the trial with room to spare. You do **not** need to stop it
overnight to afford it — and if you reserved the IP in Part 7 you safely can, with
`bash deploy/gcp/vm.sh stop` / `start`. Prices change and depend on your account; the live figure
is in the Console's billing view and the calculator at
<https://cloud.google.com/products/calculator>.
