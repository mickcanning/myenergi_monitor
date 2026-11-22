#!/usr/bin/env bash
# Build the Docker image and check it the same way CI does, before committing/pushing.
#
#   scripts/docker-test.sh          build, run the tests inside the image, smoke-test a container
#   scripts/docker-test.sh run      ...then run it with your .env on http://localhost:5001 (Ctrl+C stops it)
#   PLATFORM=linux/amd64 scripts/docker-test.sh   build exactly what Unraid runs (slower on Apple silicon)
#
# Test data goes in a Docker volume (myenergi-test-data), never in your local instance/ folder.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  ""|run) ;;
  *) printf 'Unknown option: %q\nUsage: scripts/docker-test.sh [run]\n' "$1" >&2; exit 2 ;;
esac

IMAGE=myenergi_monitor:local
NAME=myenergi-smoke
PORT=${PORT:-5001}
PLATFORM_ARGS=()
[ -n "${PLATFORM:-}" ] && PLATFORM_ARGS=(--platform "$PLATFORM")

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$*"; docker logs "$NAME" 2>&1 | tail -20 || true; exit 1; }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

step "Building $IMAGE ${PLATFORM:-(native)}"
docker build ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} -t "$IMAGE" .

step "Running the test suite inside the image"
docker run --rm ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} --entrypoint python "$IMAGE" tests/test_app.py

step "Smoke test: start a container with dummy credentials"
cleanup
docker run -d --name "$NAME" ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} -p "127.0.0.1:$PORT:5001" \
  -e MYENERGI_HUB_SERIAL=1 -e MYENERGI_API_KEY=dummy "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null           || fail "/healthz did not answer"
echo "ok  /healthz answers"
for path in / /report /upload; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$path")
  [ "$code" = 200 ] || [ "$path" = / ] || fail "$path returned $code"   # / is 502 with dummy credentials
  echo "ok  $path -> $code"
done
[ "$(docker exec "$NAME" awk '/^Uid:/{print $2}' /proc/1/status)" = 99 ] || fail "app is not running as uid 99"
echo "ok  app runs as uid 99 (not root)"
[ "$(docker exec "$NAME" stat -c %u /app/instance/uploads)" = 99 ]       || fail "data folder not owned by uid 99"
echo "ok  data folder is writable by the app"
# (capture the logs first: "docker logs | grep -q" trips pipefail when grep stops reading early)
polled() { local logs; logs=$(docker logs "$NAME" 2>&1); [[ $logs == *"recorder: poll failed"* ]]; }
for _ in $(seq 1 20); do
  polled && break
  sleep 1
done
polled                                                                    || fail "background recorder did not poll"
echo "ok  background recorder is polling (it fails here only because the credentials are dummies)"
docker exec "$NAME" test -s /app/instance/readings.db                     || fail "readings.db was not created"
echo "ok  readings.db created"
cleanup

printf '\n\033[32mAll container checks passed.\033[0m\n'

if [ "${1:-}" = run ]; then
  [ -f .env ] || { echo "No .env file: copy .env.example and fill it in first."; exit 1; }
  step "Running with your .env on http://localhost:$PORT (data in volume myenergi-test-data; Ctrl+C to stop)"
  trap - EXIT
  exec docker run --rm --name myenergi-local ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} -p "127.0.0.1:$PORT:5001" \
    --env-file .env -e PORT=5001 -e BEHIND_PROXY=0 -v myenergi-test-data:/app/instance "$IMAGE"
fi
