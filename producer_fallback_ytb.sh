#!/bin/bash
# NOTE: Avoid 'pipefail' for compatibility when some launchers use /bin/sh.
set -eu
# producer_fallback_ytb.sh
# --------------------------------------------------------------
# Pushes a local fallback file (black video + silent audio) into
# the local RTMP mux fallback input for the YouTube B pipeline.
#
# This DOES NOT upload to YouTube directly. The mux/consumer is
# responsible for selecting between OBS and this fallback, and
# for sending the chosen program to YouTube.
#
# Env / config precedence: explicit env var > keys.env default.
# --------------------------------------------------------------
shopt -s inherit_errexit || true

HERE="$(cd "$(dirname "$0")" && pwd)"
KEYS_FILE="$HERE/full_dashboard/keys.env"

# Load config if present
if [[ -f "$KEYS_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$KEYS_FILE"
fi

# Defaults (may be overridden by environment or keys.env)
FALLBACK_FILE="${FALLBACK_FILE:-/home/hdsingh132/static/black_silent.mp4}"
# Target is the local mux fallback inlet (do NOT point to YouTube here)
MUX_FALLBACK_URL="${MUX_FALLBACK_URL:-rtmp://127.0.0.1:1935/fallback/ytb}"

# ffmpeg binary
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"

if [[ ! -f "$FALLBACK_FILE" ]]; then
  echo "[fallback] ERROR: Fallback file not found: $FALLBACK_FILE" >&2
  exit 2
fi

echo "[fallback] Using file: $FALLBACK_FILE"
echo "[fallback] Output URL: $MUX_FALLBACK_URL"

echo "[fallback] Starting infinite loop. Press Ctrl-C to stop."

#
# We use -re (read input in real-time) and -stream_loop -1 to loop forever.
# We copy A/V when possible (H.264 + AAC inside MP4) to avoid CPU cost.
# If your file codecs are incompatible with FLV/RTMP, drop `-c copy` or
# set codecs explicitly (e.g., -c:v libx264 -c:a aac).
#
while true; do
  "$FFMPEG_BIN" -hide_banner -loglevel info \
    -re -stream_loop -1 -i "$FALLBACK_FILE" \
    -c copy \
    -f flv "$MUX_FALLBACK_URL"

  rc=$?
  echo "[fallback] ffmpeg exited with code $rc. Restarting in 3s..." >&2
  sleep 3
done
