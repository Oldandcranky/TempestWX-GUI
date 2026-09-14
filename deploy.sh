#!/usr/bin/env bash
#
# Ship this checkout to the NAS and rebuild the container.
#
# Run it from a machine that can reach the NAS over the LAN and has its SSH
# key authorised there — your laptop, not a cloud shell. See the README for
# the one-time key setup.
#
#   ./deploy.sh                          # defaults below
#   NAS=me@10.0.0.101 ./deploy.sh        # somewhere else
#
# What it will not touch: docker-compose.yml, .env and data/ on the NAS.
# Those hold your location, your units, your tokens and your history, and
# they are meant to differ from what is in git.
#
# Two streams, on purpose. The terminal gets one line per step, because a
# deploy you have to read carefully is a deploy you stop reading. The log
# file gets every command, its full output, its exit code and how long it
# took — that is the thing to paste at whoever is helping you debug.

set -euo pipefail

NAS="${NAS:-synologyadmin@10.0.0.101}"
APP="${APP:-/volume1/docker/tempest}"
PORT="${PORT:-8444}"
DOCKER="${DOCKER:-/usr/local/bin/docker}"   # not on a non-login shell's PATH
SHIP="tempest_core.py tempest_server.py web Dockerfile .dockerignore"
TRIES="${TRIES:-30}"                        # × 2s before giving up on the page

cd "$(dirname "$0")"

# ───────────────────────────────────────────────────────────────── output ───

if [ -t 1 ]; then
  B=$'\033[1m'; D=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'
  Y=$'\033[33m'; C=$'\033[36m'; Z=$'\033[0m'
else
  B=""; D=""; R=""; G=""; Y=""; C=""; Z=""
fi

mkdir -p deploy-logs
LOG="deploy-logs/deploy-$(date +%Y%m%d-%H%M%S).log"
# Keep the last twenty. Enough to compare a bad deploy against the last good
# one, few enough that the directory never needs thinking about.
ls -1t deploy-logs/*.log 2>/dev/null | tail -n +21 | xargs -r rm -f 2>/dev/null || true

ts(){ date "+%H:%M:%S"; }
note(){ printf '%s\n' "$*" >>"$LOG"; }

step=""
begin(){ step="$1"; printf "  ${C}▸${Z} %-26s" "$1"; }
ok(){    printf "${G}✓${Z} ${D}%5ss${Z}\n" "$1"; }
bad(){   printf "${R}✗${Z} ${D}exit %s${Z}\n" "$1"; }

# Every command goes through here: logged with its exit code and duration, and
# on failure the log lines it just wrote are echoed to the terminal, so the
# reason is on screen without anyone having to go and open the file.
run(){
  local label="$1" cmd="$2" from t0 rc dt
  begin "$label"
  from=$(( $(wc -l <"$LOG") + 1 ))
  note ""; note "=== $(ts)  $label"; note "\$ $cmd"
  t0=$SECONDS
  set +e; eval "$cmd" >>"$LOG" 2>&1; rc=$?; set -e
  dt=$(( SECONDS - t0 ))
  note "--- exit $rc after ${dt}s"
  if [ "$rc" -eq 0 ]; then ok "$dt"; else
    bad "$rc"
    printf "\n${R}%s failed.${Z} What it said:\n\n" "$label"
    sed -n "${from},\$p" "$LOG" | sed 's/^/    /'
    diagnose
    printf "\n  ${B}log${Z} %s  ${D}(paste this whole file)${Z}\n\n" "$LOG"
    exit "$rc"
  fi
}

die(){
  printf "\n  ${R}✗${Z} %s\n" "$1"
  note ""; note "=== $(ts)  FAILED: $1"
  diagnose
  printf "\n  ${B}log${Z} %s  ${D}(paste this whole file)${Z}\n\n" "$LOG"
  exit 1
}

# Only runs when something has already gone wrong, so it can afford to be
# thorough. Everything here answers a question someone will otherwise ask:
# is the container up, what did it say as it died, did the files arrive, are
# they readable by the uid compose runs as, and is the volume full.
diagnose(){
  note ""; note "======== $(ts)  DIAGNOSTICS"
  local c
  for c in \
    "uname -a" \
    "df -h '$APP'" \
    "ls -la '$APP'" \
    "ls -la '$APP/web' | head -40" \
    "find '$APP/tempest_core.py' '$APP/tempest_server.py' '$APP/web' '$APP/Dockerfile' \\( -type f ! -perm -o=r \\) -o \\( -type d ! -perm -o=x \\)" \
    "cd '$APP' && $DOCKER compose ps" \
    "cd '$APP' && $DOCKER compose logs --tail=80" \
    "curl -s -o /dev/null -w 'http %{http_code}\\n' localhost:$PORT/api/state" \
    "curl -s localhost:$PORT/api/state | head -c 2000"
  do
    note ""; note "\$ $c"
    ssh "$NAS" "$c" >>"$LOG" 2>&1 || note "(command failed)"
  done
}

# ─────────────────────────────────────────────────────────────── the work ───

# The server reports sha256(web/index.html)[:12] as `ui`. Computing it here is
# how we tell "the rebuild worked" from "the old container is still up".
sha256(){ command -v shasum >/dev/null && shasum -a 256 "$1" || sha256sum "$1"; }
want=$(sha256 web/index.html | cut -c1-12)

head=$(git rev-parse --short HEAD 2>/dev/null || echo "not a git checkout")
dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
state=$([ "$dirty" = "0" ] && echo "clean" || echo "${dirty} file(s) uncommitted")

{
  echo "Tempest deploy — $(date)"
  echo "target   $NAS:$APP  port $PORT"
  echo "commit   $head ($state)"
  echo "page     $want"
  echo
  echo "shipping $SHIP"
  echo
  echo "--- local environment"
  uname -a
  bash --version | head -1
  tar --version 2>&1 | head -1
  ssh -V 2>&1
  echo
  echo "--- files being shipped"
  ls -la $SHIP 2>&1
} >>"$LOG"

printf "\n  ${B}Tempest deploy${Z}\n"
printf "  ${D}────────────────────────────────────────────${Z}\n"
printf "  ${D}target${Z}  %s:%s\n" "$NAS" "$APP"
printf "  ${D}commit${Z}  %s ${D}(%s)${Z}\n" "$head" "$state"
printf "  ${D}page${Z}    %s\n" "$want"
printf "  ${D}log${Z}     %s\n\n" "$LOG"

[ "$dirty" = "0" ] || printf "  ${Y}!${Z} shipping uncommitted changes\n\n"

# DSM restricts SFTP, so stream a tar over ssh. The two macOS guards: without
# COPYFILE_DISABLE its tar sprinkles ._ resource-fork files through the build
# context, and without --no-xattrs it writes com.apple.provenance and friends
# as pax headers, which GNU tar on the NAS then complains about once per file.
run "ship files" \
  "COPYFILE_DISABLE=1 tar --no-xattrs -czvf - $SHIP \
     | ssh '$NAS' \"mkdir -p '$APP' && tar xzf - -C '$APP'\""

# The image chowns /app to uid 10001, but compose runs the container as your
# DSM user. Anything not world-readable becomes a "Missing index.html" at
# runtime, which reads like a much more interesting bug than it is.
run "fix permissions" \
  "ssh '$NAS' \"cd '$APP' && chmod -R a+rX $SHIP\""

# Hash the file where it landed. Without this a stale page has two possible
# causes — the files never arrived, or they arrived and the container did not
# restart — and the rest of the run cannot tell them apart. This can, before
# the build has even started.
begin "verify what landed"
landed=$(ssh "$NAS" "command -v sha256sum >/dev/null \
  && sha256sum '$APP/web/index.html' | cut -c1-12 || echo no-sha256sum" 2>>"$LOG")
note ""; note "=== $(ts)  verify what landed"
note "local  $want"
note "remote $landed"
if [ "$landed" = "no-sha256sum" ]; then
  printf "${Y}?${Z} ${D}no sha256sum on the NAS${Z}\n"
  note "(NAS has no sha256sum; skipped — a stale page after this cannot be"
  note " attributed to shipping or to the container without more digging)"
elif [ "$landed" != "$want" ]; then
  printf "${R}✗${Z}\n"
  die "the NAS has ui '$landed' on disk, but we sent '$want' — the files did not arrive"
else
  ok "0"
fi

run "rebuild container" \
  "ssh '$NAS' \"cd '$APP' && $DOCKER compose up -d --build\""

# The page is served from inside the image, so until this agrees the deploy
# has not happened, whatever docker exited with.
begin "wait for the new page"
t0=$SECONDS
got=""
for i in $(seq "$TRIES"); do
  got=$(ssh "$NAS" "curl -fs localhost:$PORT/api/state" 2>>"$LOG" \
        | sed -n 's/.*"ui": *"\([0-9a-f]*\)".*/\1/p') || true
  note "poll $i/$TRIES  $(ts)  ui='${got:-}'"
  [ "$got" = "$want" ] && break
  sleep 2
done
if [ "$got" = "$want" ]; then
  ok "$(( SECONDS - t0 ))"
else
  printf "${R}✗${Z}\n"
  die "after $(( TRIES * 2 ))s the NAS still serves ui '${got:-nothing}', wanted '$want'"
fi

printf "\n  ${G}✓ live${Z} — %s\n" "$want"
printf "  ${D}log${Z}   %s\n\n" "$LOG"
note ""; note "=== $(ts)  OK — live on $want"
