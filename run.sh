#!/bin/bash
# usage: run.sh old|new shimport calport
set -u
S=$TMPDIR/wg17; V=$1; SP=$2; CP=$3
D=$(mktemp -d); echo '{"outgoing":{"wizard-google":"grant-google-0123456789"}}' > $D/grants.json
WG_ROOT=$S/$V SHIM_PORT=$SP python3 $S/shim.py > $S/shim-$V.log 2>&1 &
SH=$!
cd ~/work/cal-17/app
PORT=$CP DATA_DIR=$D ICAL_SYNC_INTERVAL=0 CALENDAR_TIMEZONE=Asia/Bangkok CALENDAR_SOURCES=google=http://127.0.0.1:$SP \
  CALENDAR_DEV_MODE=loopback-single-owner WIZARD_GRANTS_FILE=$D/grants.json WIZARD_CORE_DIR=$HOME/work/cal-17/.runtime/core \
  python3 server.py > $S/cal-$V.log 2>&1 &
CA=$!
for i in $(seq 50); do curl -sf http://127.0.0.1:$CP/healthz >/dev/null && break; sleep .2; done
sleep 1
BROWSER_PATH=$(ls -d ~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome | tail -1) node $S/shot.cjs http://127.0.0.1:$CP $S/$V.png
kill $SH $CA
cat $S/shim-$V.log
