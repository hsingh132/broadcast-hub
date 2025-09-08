
#!/bin/bash
# Note: using "set -eu" (no pipefail) for portability under /bin/sh
set -eu
# producer_obs_yta.sh
# Pull OBS RTMP from the local nginx-rtmp input and push it to the local
# "obs/yta" app so the muxer can switch between OBS and fallback.
#
# This script is intentionally simple: if the input dies, ffmpeg exits and we
# immediately retry after a short sleep. The muxer script is responsible for
# switching to the fallback feed when OBS is down.
#
# IMPORTANT FOR FUTURE ME:
# Do NOT change this script to talk to YouTube directly.
# It must ONLY push to the local RTMP endpoint rtmp://127.0.0.1:1935/obs/yta.
# The mux_watch_yta.sh script will do the A/B switching and the consumer
# will push to YouTube.
#
# Env vars (optional):
#   INPUT_URL   - RTMP URL where OBS is publishing (default: rtmp://localhost:1935/live/test)
#   OBS_APP_URL - Local RTMP endpoint that receives OBS for YT-A (default: rtmp://127.0.0.1:1935/obs/yta)

# Load optional overrides from keys.env if present (harmless if missing)
KEYS_FILE="$HOME/full_dashboard/keys.env"
if [[ -f "$KEYS_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$KEYS_FILE"
fi

INPUT_URL="${INPUT_URL:-rtmp://localhost:1935/live/test}"
OBS_APP_URL="${OBS_APP_URL:-rtmp://127.0.0.1:1935/obs/yta}"

echo "==> producer_obs_yta starting"
echo "    INPUT_URL   = $INPUT_URL"
echo "    OBS_APP_URL = $OBS_APP_URL"

# Small helper to print timestamps
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Clean shutdown handler
terminate() {
  echo "$(ts) [producer_obs_yta] Caught signal, exiting..."
  exit 0
}
trap terminate INT TERM

# Main retry loop
while true; do
  echo "$(ts) [producer_obs_yta] Launching ffmpeg to pull OBS and push to $OBS_APP_URL"
  # Notes:
  # - We "copy" both audio and video to avoid re-encoding inside this hop.
  # - For RTMP inputs, the -rtmp_* flags can help a little with buffering.
  ffmpeg \
    -loglevel info -hide_banner \
    -rtmp_live live -rtmp_buffer 1000 \
    -i "$INPUT_URL" \
    -c:v copy -c:a copy \
    -f flv "$OBS_APP_URL"

  status=$?
  echo "$(ts) [producer_obs_yta] ffmpeg exited with status $status"
  # If clean exit, stop looping; if failure, retry shortly.
  if [[ $status -eq 0 ]]; then
    echo "$(ts) [producer_obs_yta] Clean exit. Stopping."
    break
  fi
  echo "$(ts) [producer_obs_yta] Will retry in 2s..."
  sleep 2
done