#!/usr/bin/env bash
#
# Everything, in the order that fails fastest.
#
#   tests/run.sh          logic only if there is no browser here
#   tests/run.sh -q       quiet: only failures and the summary
#
# The logic tests need nothing but Python. The geometry tests need a headless
# Chromium; where there is none they are skipped rather than failed, so this
# is still worth running on a machine that only has Python.

set -uo pipefail
cd "$(dirname "$0")/.."

# No .pyc files. git checkout can restore a source file with an older
# timestamp than its cached bytecode, and Python will then keep running
# the stale version — which looks exactly like a test lying to you.
export PYTHONDONTWRITEBYTECODE=1

# Where Playwright lives: the cloud image keeps it under /opt/node22; on the
# owner's Mac it is a global npm install. An explicit PLAYWRIGHT wins.
if [ -z "${PLAYWRIGHT:-}" ]; then
  PLAYWRIGHT=/opt/node22/lib/node_modules/playwright
  if [ ! -d "$PLAYWRIGHT" ] && command -v npm >/dev/null; then
    PLAYWRIGHT="$(npm root -g)/playwright"
  fi
fi
export PLAYWRIGHT
PORT="${PORT:-8451}"          # not 8444: do not fight a dashboard already up
quiet=false; [ "${1:-}" = "-q" ] && quiet=true

if [ -t 1 ]; then B=$'\033[1m'; R=$'\033[31m'; G=$'\033[32m'; D=$'\033[2m'; Z=$'\033[0m'
else B=""; R=""; G=""; D=""; Z=""; fi

fails=0

printf "\n  ${B}logic${Z}\n"
if $quiet; then
  python3 -m unittest discover -s tests -q 2>&1 | tail -4 | sed 's/^/  /'
else
  python3 -m unittest discover -s tests 2>&1 | tail -4 | sed 's/^/  /'
fi
[ "${PIPESTATUS[0]}" -eq 0 ] || fails=$((fails + 1))

printf "\n  ${B}geometry${Z}\n"
if [ ! -d "$PLAYWRIGHT" ]; then
  printf "  ${D}skipped — no headless browser at %s${Z}\n" "$PLAYWRIGHT"
else
  # A demo server of our own, on its own port, with every card on screen so
  # none goes unmeasured, and its own data directory so a real history and
  # the card order you actually use are left alone.
  tmp=$(mktemp -d)
  TEMPEST_DATA_DIR="$tmp" \
  TEMPEST_HTTP_PORT="$PORT" \
  TEMPEST_UDP_PORT=0 \
  TEMPEST_SLOTS="temperature,pollen,air,internet,astronomy,forecast,wind,pressure,rainfall,radar,lightning,records,hardware" \
  TEMPEST_RAIN_MONTHLY="1.9,2.0,2.4,3.6,4.2,4.0,3.7,3.9,3.0,3.2,2.9,2.3" \
  TEMPEST_TEMP_NORMAL_HIGH="31,35,46,59,70,80,84,82,75,62,48,36" \
  TEMPEST_TEMP_NORMAL_LOW="16,19,28,38,49,59,64,62,54,42,32,22" \
    python3 tempest_server.py --demo >"$tmp/server.log" 2>&1 &
  pid=$!
  trap 'kill '"$pid"' 2>/dev/null; rm -rf '"$tmp" EXIT

  for _ in $(seq 40); do
    curl -fs "http://localhost:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 0.25
  done
  if ! curl -fs "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    printf "  ${R}the demo server did not come up${Z}\n"
    sed 's/^/    /' "$tmp/server.log" | tail -20
    fails=$((fails + 1))
  else
    node tests/viewports.js "http://localhost:$PORT" || fails=$((fails + 1))
  fi
fi

if [ "$fails" -eq 0 ]; then
  printf "\n  ${G}✓${Z} everything passed\n\n"
else
  printf "\n  ${R}✗${Z} %d suite(s) failed\n\n" "$fails"
fi
exit $((fails > 0))
