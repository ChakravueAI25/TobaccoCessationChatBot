#!/usr/bin/env bash
# Puts the study backend on Google Cloud, from this working copy. Run it from anywhere:
#
#   bash deploy/gcp/deploy.sh              # create the VM if missing, then deploy
#   bash deploy/gcp/deploy.sh --gpu        # same, but attach a T4 and run the gpu profile
#
# Safe to run again. A second run skips creation, replaces the source and restarts, which is
# also how a fix ships mid-study.
#
# It does NOT use git on the VM. The source goes over SSH, so the VM needs no deploy key and no
# access to a private repository.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
ZONE="${ZONE:-asia-south1-c}"          # Mumbai: the participants and the study are in India
NAME="${NAME:-quitsmoke-study}"
MACHINE="${MACHINE:-e2-standard-4}"    # 4 vCPU, 16 GB
DISK_GB="${DISK_GB:-50}"               # model 2.4 GB + images + database + exports
PROFILE=cpu
GPU_ARGS=()

# Everything the study keeps lives OUTSIDE the source tree, so a redeploy can replace the tree
# wholesale without thinking about what it might be standing on.
REMOTE=/opt/quitsmoke
APP=$REMOTE/app

[ -n "$PROJECT" ] || { echo "No project set. Run: gcloud config set project <project-id>"; exit 1; }

say()   { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
on_vm() { gcloud compute ssh "$NAME" --zone "$ZONE" --project "$PROJECT" --quiet --command "$1"; }

if [ "${1:-}" = "--gpu" ]; then
    PROFILE=gpu
    MACHINE="${MACHINE_GPU:-n1-standard-4}"    # a T4 does not attach to an e2
    GPU_ARGS=(--accelerator=type=nvidia-tesla-t4,count=1
              --maintenance-policy=TERMINATE)  # a GPU VM cannot live-migrate
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------- the VM
if gcloud compute instances describe "$NAME" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
    say "$NAME already exists - deploying to it"
else
    say "creating $NAME ($MACHINE, $ZONE)"
    gcloud compute instances create "$NAME" \
        --project "$PROJECT" --zone "$ZONE" --machine-type "$MACHINE" \
        --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
        --boot-disk-size="${DISK_GB}GB" --boot-disk-type=pd-balanced \
        --metadata-from-file=startup-script=deploy/gcp/startup.sh \
        --tags=quitsmoke \
        "${GPU_ARGS[@]}"

    # Exactly two ports. 8000 is deliberately absent: Caddy terminates TLS on 443 and the API
    # binds the VM's loopback, so there is no plaintext port to find from outside.
    gcloud compute firewall-rules create quitsmoke-web \
        --project "$PROJECT" --allow=tcp:80,tcp:443 --target-tags=quitsmoke \
        --description="Study API over HTTPS" 2>/dev/null || echo "  firewall rule already exists"
fi

IP="$(gcloud compute instances describe "$NAME" --zone "$ZONE" --project "$PROJECT" \
      --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
# No domain to hand, and no certificate authority issues for a bare IP. sslip.io resolves
# 34-1-2-3.sslip.io to 34.1.2.3, which is a name Let's Encrypt will sign.
PUBLIC_HOST="${PUBLIC_HOST:-${IP//./-}.sslip.io}"

say "waiting for the first boot (docker, and a 2.4 GB model download)"
# Polling the startup script's own last line is the only honest signal. The VM answers SSH long
# before the model has arrived, and deploying into a half-prepared machine fails in ways that
# look like a broken image rather than a race.
ready=false
for _ in $(seq 1 90); do
    if on_vm 'sudo journalctl -u google-startup-scripts --no-pager 2>/dev/null | grep -q "\[startup.*\] ready"' 2>/dev/null; then
        ready=true; break
    fi
    sleep 20
done
$ready || { echo "The VM did not finish preparing. Look at:"; \
            echo "  gcloud compute ssh $NAME --zone $ZONE --command 'sudo journalctl -u google-startup-scripts'"; exit 1; }

# ---------------------------------------------------------------- settings, written once
say "settings"
# Generated ON the VM, so no secret is ever typed here or sent over the wire. Written once and
# then kept: a redeploy that rotated the API key would lock out every phone already carrying it.
on_vm "sudo mkdir -p $REMOTE
if sudo test -f $REMOTE/.env; then
  echo '  .env already exists - keeping the key the phones already have'
else
  sudo tee $REMOTE/.env >/dev/null <<ENV
COMPOSE_PROJECT_NAME=quitsmoke
POSTGRES_USER=quit_smoke_app
POSTGRES_PASSWORD=\$(openssl rand -hex 24)
POSTGRES_DB=quit_smoke_research
ENVIRONMENT=production
LOG_LEVEL=INFO
AUTH_ENABLED=true
DEVELOPMENT_API_KEY=\$(openssl rand -hex 24)
LLAMA_SERVER_URL=http://llama:8080
LLAMA_TIMEOUT_SECONDS=120
PUBLIC_HOST=$PUBLIC_HOST
API_PUBLISH_PORT=127.0.0.1:8000
ENV
  sudo chmod 600 $REMOTE/.env
  echo '  .env written'
fi"

# ---------------------------------------------------------------- source
say "copying the source"
# Replaced wholesale rather than merged. A file deleted in the repo has to stop existing on the
# VM too, or a module that no longer exists here stays importable there and the deployment
# quietly stops being what was tested.
on_vm "sudo rm -rf $APP && sudo mkdir -p $APP $REMOTE/{models,exports,backups} && sudo chown -R \$USER $APP"
tar --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=exports --exclude=backups --exclude=models --exclude=dist --exclude=.env \
    -czf - . | on_vm "tar xzf - -C $APP"
on_vm "cd $APP && ln -sfn $REMOTE/.env .env && ln -sfn $REMOTE/models models \
        && ln -sfn $REMOTE/exports exports && ln -sfn $REMOTE/backups backups"

# ---------------------------------------------------------------- run it
say "building and starting (--profile $PROFILE)"
on_vm "cd $APP && sudo docker compose --profile $PROFILE --profile tls up -d --build"

say "migrations"
# `up` already started this service, but a container that exits takes its failure with it. Run
# it again in the foreground so a bad migration is a failed deploy rather than a healthy API
# that errors on every query.
on_vm "cd $APP && sudo docker compose run --rm migrate"

say "health"
on_vm "curl -fsS http://127.0.0.1:8000/health" || true
echo

cat <<DONE

  Study API   https://$PUBLIC_HOST
  Health      https://$PUBLIC_HOST/health
  VM          $NAME  ($MACHINE, $ZONE)

  For the app's local.properties:
    research.baseUrl=https://$PUBLIC_HOST
    research.apiKey=   ->  bash deploy/gcp/vm.sh key

  The certificate takes a minute the first time. Until Let's Encrypt answers, https fails and
  the reason is in:  bash deploy/gcp/vm.sh logs caddy

  Everything after this - status, logs, data, backups, stopping the VM - is vm.sh.
DONE
