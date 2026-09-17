#!/usr/bin/env bash
# Everything you do to the study VM after it is deployed, without remembering gcloud flags.
#
#   bash deploy/gcp/vm.sh status            what is running, and is the model up
#   bash deploy/gcp/vm.sh logs [service]    follow the logs (api, llama, caddy, db)
#   bash deploy/gcp/vm.sh restart           restart the containers, keeping the data
#   bash deploy/gcp/vm.sh stop              STOP THE VM - billing stops, the disk is kept
#   bash deploy/gcp/vm.sh start             start it again; the containers come back by themselves
#   bash deploy/gcp/vm.sh data              who is using the app, and when they were last seen
#   bash deploy/gcp/vm.sh feedback          what participants typed into the feedback screen
#   bash deploy/gcp/vm.sh export [csv]      run a research export (csv, json or xlsx)
#   bash deploy/gcp/vm.sh backup            pg_dump, downloaded to ./backups here
#   bash deploy/gcp/vm.sh key               the API key the phones need
#   bash deploy/gcp/vm.sh ssh               a shell on the VM
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
ZONE="${ZONE:-asia-south1-c}"
NAME="${NAME:-quitsmoke-study}"
REMOTE=/opt/quitsmoke
APP=$REMOTE/app
DC="cd $APP && sudo docker compose"

g()     { gcloud compute "$@" --zone "$ZONE" --project "$PROJECT" --quiet; }
on_vm() { g ssh "$NAME" --command "$1"; }

case "${1:-status}" in
status)
    # Every probe is guarded. A status command that aborts on its first failure tells you least
    # exactly when something is wrong, which is the only time anyone runs it.
    vm="$(g instances describe "$NAME" --format='value(status)' 2>/dev/null || echo 'NOT FOUND')"
    echo "VM: $vm"
    [ "$vm" = RUNNING ] || { echo "Nothing else to report while the VM is $vm."; exit 0; }
    on_vm "$DC ps" || true
    echo
    # The database being up says nothing about the model, and a missing model is invisible to a
    # participant - every reply just becomes the fallback. So print what /health actually says.
    on_vm "curl -fsS http://127.0.0.1:8000/health" || echo "the API did not answer"
    echo
    ;;
logs)
    on_vm "$DC logs -f --tail=200 ${2:-}"
    ;;
restart)
    on_vm "$DC restart"
    ;;
stop)
    # The VM, not the containers. A stopped VM costs only its disk, which is the lever that
    # matters overnight or between study rounds.
    g instances stop "$NAME"
    echo "Stopped. The disk and all study data are kept. Start it again with: vm.sh start"
    ;;
start)
    g instances start "$NAME"
    echo "Starting. The containers restart themselves; give the model a few minutes."
    echo "Note: the external IP changes unless it is reserved, which would change the app's URL."
    ;;
data)
    on_vm "$DC exec -T api uv run python scripts/show_data.py ${2:-}"
    ;;
feedback)
    on_vm "$DC exec -T api uv run python scripts/show_feedback.py"
    ;;
export)
    # There is no export CLI - it is POST /api/v1/exports behind require_api_key, so this asks
    # the running service rather than reaching into the database and reimplementing the scope
    # rules. services/export.py names its tables explicitly so a new table cannot join an export
    # by accident, and going round it would throw that away.
    fmt="${2:-csv}"
    on_vm "key=\$(sudo grep -oP '(?<=^DEVELOPMENT_API_KEY=).*' $REMOTE/.env)
curl -fsS -X POST http://127.0.0.1:8000/api/v1/exports \
     -H \"X-API-Key: \$key\" -H 'Content-Type: application/json' \
     -d '{\"format\":\"$fmt\"}'"
    echo
    echo "Written into /opt/quitsmoke/exports on the VM. Bring it down with:"
    echo "  gcloud compute scp --zone $ZONE --recurse $NAME:/opt/quitsmoke/exports ./exports"
    ;;
backup)
    stamp="$(date +%Y%m%d-%H%M)"
    mkdir -p backups
    # Through the db container's own pg_dump, so the version always matches the server's.
    on_vm "$DC exec -T db sh -c 'pg_dump -U \"\$POSTGRES_USER\" \"\$POSTGRES_DB\"' | gzip" \
        > "backups/quitsmoke-$stamp.sql.gz"
    echo "backups/quitsmoke-$stamp.sql.gz  ($(du -h "backups/quitsmoke-$stamp.sql.gz" | cut -f1))"
    ;;
key)
    on_vm "sudo grep DEVELOPMENT_API_KEY $REMOTE/.env"
    ;;
ssh)
    g ssh "$NAME"
    ;;
*)
    sed -n '2,14p' "$0"
    exit 1
    ;;
esac
