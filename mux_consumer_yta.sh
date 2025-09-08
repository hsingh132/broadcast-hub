#!/usr/bin/env bash
# mux_consumer_yta.sh — OBS-first to YouTube A with automatic file fallback
# This script prefers OBS RTMP; if unavailable, it streams a local fallback file
# to YouTube. When OBS returns, it switches back automatically.
#
# Environment expected (set by the dashboard):
#   INPUT_URL      : RTMP published by OBS on this VM (e.g. rtmp://localhost:1935/live/test)
#   FALLBACK_FILE  : local mp4 (black/silent), e.g. /home/hdsingh132/static/black_silent.mp4
#   YT_INGEST      : rtmp://a.rtmp.youtube.com/live2     (or rtmp://b.rtmp.youtube.com/live2)
#   YT_KEY         : YouTube stream key (from KEY1/KEY2)
#
# NOTE: Keep this script executed with bash (dashboard uses `bash -lc ...`).

set -eu

FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
FFPROBE_BIN="${FFPROBE_BIN:-ffprobe}"

# Validate required env
: "${INPUT_URL:?INPUT_URL not set}"
: "${FALLBACK_FILE:?FALLBACK_FILE not set}"
: "${YT_INGEST:?YT_INGEST not set}"
: "${YT_KEY:?YT_KEY not set}"

YT_URL="${YT_INGEST%/}/${YT_KEY}"

log() { echo "$(date '+[%a %b %d %T %Z %Y]') $*"; }

probe_obs() {
  # Return 0 if OBS RTMP is readable; small timeout for snappy checks
  "${FFPROBE_BIN}" -v error -rw_timeout 5000000 -i "$INPUT_URL" -show_streams >/dev/null 2>&1
}

fallback_pid=""

start_fallback() {
  if [[ -n "${fallback_pid}" ]] && kill -0 "${fallback_pid}" 2>/dev/null; then
    return
  fi
  log "Starting FALLBACK -> YouTube"
  "${FFMPEG_BIN}" -hide_banner -loglevel info -re \
    -stream_loop -1 -i "$FALLBACK_FILE" \
    -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 60 -keyint_min 60 \
    -c:a aac -b:a 128k -ar 44100 -ac 2 \
    -f flv "$YT_URL" &
  fallback_pid=$!
}

stop_fallback() {
  if [[ -n "${fallback_pid}" ]] && kill -0 "${fallback_pid}" 2>/dev/null; then
    log "Stopping FALLBACK (pid ${fallback_pid})"
    # Ask ffmpeg to finish nicely
    kill -INT "${fallback_pid}" || true

    # Wait up to ~8s for graceful shutdown
    for i in {1..8}; do
      if ! kill -0 "${fallback_pid}" 2>/dev/null; then
        break
      fi
      sleep 1
    done

    # If still alive, force kill
    if kill -0 "${fallback_pid}" 2>/dev/null; then
      log "FALLBACK still alive after grace period, forcing kill -9"
      kill -9 "${fallback_pid}" || true
      # Allow the OS to reap sockets
      sleep 1
    fi

    # Reap the process to avoid zombies
    wait "${fallback_pid}" 2>/dev/null || true

    # Give YouTube ingest a moment to release the old session
    log "FALLBACK fully stopped; waiting 3s for ingest drain"
    sleep 3
  fi
  fallback_pid=""
}

log "Consumer starting."
log "INPUT_URL=$INPUT_URL"
log "FALLBACK_FILE=$FALLBACK_FILE"
log "YT_INGEST=$YT_INGEST"
log "YT_KEY=$( [[ -n "${YT_KEY:-}" ]] && echo set || echo missing )"

# Main loop: prefer OBS; when it drops, run fallback; when it returns, switch back.
while true; do
  if probe_obs; then
    # OBS is available: ensure fallback off and let ingest drain, then push OBS -> YouTube
    stop_fallback
    log "OBS detected. Pushing OBS -> YouTube"
    if ! "${FFMPEG_BIN}" -hide_banner -loglevel info -re \
        -i "$INPUT_URL" \
        -c copy -f flv "$YT_URL"; then
      log "OBS push ended with non-zero status. Waiting 2s and retrying once..."
      sleep 2
      "${FFMPEG_BIN}" -hide_banner -loglevel info -re \
        -i "$INPUT_URL" \
        -c copy -f flv "$YT_URL" || true
    fi
    log "OBS path ended; re-evaluating in 2s."
    sleep 2
  else
    # OBS not available: ensure fallback is running in background
    log "OBS not detected. Ensuring FALLBACK -> YouTube is running."
    start_fallback
    # Poll OBS while fallback continues
    sleep 3
  fi
done
