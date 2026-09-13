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

set -euo pipefail

NAS="${NAS:-synologyadmin@10.0.0.101}"
APP="${APP:-/volume1/docker/tempest}"
PORT="${PORT:-8444}"

cd "$(dirname "$0")"

# The server reports sha256(web/index.html)[:12] as `ui`. Computing it here
# is how we tell "the rebuild worked" from "the old container is still up".
sha256() { command -v shasum >/dev/null && shasum -a 256 "$1" || sha256sum "$1"; }
want=$(sha256 web/index.html | cut -c1-12)

echo "==> shipping to $NAS:$APP (ui $want)"
# DSM restricts SFTP, so stream a tar over ssh. COPYFILE_DISABLE keeps macOS
# tar from sprinkling ._ resource-fork files through the build context.
COPYFILE_DISABLE=1 tar czf - \
    tempest_core.py tempest_server.py web Dockerfile .dockerignore \
  | ssh "$NAS" "mkdir -p '$APP' && tar xzf - -C '$APP'"

# The image chowns /app to uid 10001, but compose runs the container as your
# DSM user. Anything not world-readable becomes a "Missing index.html" at
# runtime, which reads like a much more interesting bug than it is.
ssh "$NAS" "cd '$APP' && chmod -R a+rX tempest_core.py tempest_server.py web Dockerfile"

echo "==> rebuilding"
# docker is not on the PATH of a non-login shell on DSM.
ssh "$NAS" "cd '$APP' && /usr/local/bin/docker compose up -d --build"

echo "==> waiting for the new page"
for _ in $(seq 30); do
  got=$(ssh "$NAS" "curl -fs localhost:$PORT/api/state" 2>/dev/null \
        | sed -n 's/.*"ui": *"\([0-9a-f]*\)".*/\1/p') || true
  if [ "$got" = "$want" ]; then
    echo "==> live: $got"
    exit 0
  fi
  sleep 2
done

echo "==> the NAS is serving ui '${got:-nothing}', expected '$want'" >&2
exit 1
