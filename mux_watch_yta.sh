#!/usr/bin/env bash
# Supervisor for the YouTube A pipeline.
# - Ensures OBS producer and Fallback producer are running.
# - Runs the mux consumer in a restart loop **in the foreground** so the
#   dashboard captures live logs on stdout.
# The dashboard starts/stops only THIS script.

set -euo pipefail

# --- External inputs provided by the dashboard (with sane defaults) ---
: "${INPUT_URL:=rtmp://localhost:1935/live/test}"                  # OBS -> VM RTMP
: "${FALLBACK_FILE:=/home/hdsingh132/static/black_silent.mp4}"     # Local MP4 for silent failover
: "${YT_INGEST:=rtmp://a.rtmp.youtube.com/live2}"                  # YouTube ingest base
: "${YT_KEY:=}"                                                    # Live key, required by consumer

# --- Local RTMP endpoints (Nginx-RTMP apps) ---
: "${OBS_OUT:=rtmp://localhost:1935/yt_producer_a}"                # where producer_obs_yta publishes
: "${FALLBACK_OUT:=rtmp://localhost:1935/yt_fallback_a}"           # where producer_fallback_yta publishes

OBS_PRODUCER="$HOME/producer_obs_yta.sh"
FALLBACK_PRODUCER="$HOME/producer_fallback_yta.sh"
CONSUMER="$HOME/mux_consumer_yta.sh"

# Ensure scripts are executable
chmod +x "$OBS_PRODUCER" "$FALLBACK_PRODUCER" "$CONSUMER" || true

printf '[%(%a %b %d %T %Z %Y)T] Supervisor starting. INPUT_URL=%s YT_INGEST=%s KEY=%s\n' -1 "$INPUT_URL" "$YT_INGEST" "${YT_KEY:+(set)}"

start_if_missing () {
  local name="$1"; shift
  local pattern="$1"; shift
  if ! pgrep -f "$pattern" >/dev/null 2>&1; then
    printf '[%(%a %b %d %T %Z %Y)T] Launching %s...\n' -1 "$name"
    # Launch producers detached; they log to their own stdout which goes to journald when run via dashboard
    nohup env INPUT_URL="$INPUT_URL" \
              OUTPUT_URL="$OBS_OUT" \
              FALLBACK_FILE="$FALLBACK_FILE" \
              PUBLISH_URL="$FALLBACK_OUT" \
              "$@" >/dev/null 2>&1 &
    disown || true
  else
    printf '[%(%a %b %d %T %Z %Y)T] %s already running.\n' -1 "$name"
  fi
}

# Clean up stale ffmpeg publishers to our target apps (best-effort)
pkill -f "ffmpeg.*$OBS_OUT" >/dev/null 2>&1 || true
pkill -f "ffmpeg.*$FALLBACK_OUT" >/dev/null 2>&1 || true

# Kick off the producers once; the loop will keep the consumer alive.
start_if_missing "producer_obs_yta" "producer_obs_yta.sh" "$OBS_PRODUCER"
start_if_missing "producer_fallback_yta" "producer_fallback_yta.sh" "$FALLBACK_PRODUCER"

# Consumer loop: pushes to YouTube using the current YT_KEY (foreground)
while true; do
  printf '[%(%a %b %d %T %Z %Y)T] Starting mux_consumer_yta...\n' -1
  # Use line-buffering so ffmpeg logs appear immediately in the dashboard
  if ! stdbuf -oL -eL env YT_INGEST="$YT_INGEST" \
          YT_KEY="$YT_KEY" \
          OBS_IN="$OBS_OUT" \
          FALLBACK_IN="$FALLBACK_OUT" \
          "$CONSUMER"; then
    rc=$?
    printf '[%(%a %b %d %T %Z %Y)T] mux_consumer_yta exited (rc=%s). Restarting in 3s...\n' -1 "$rc"
    sleep 3
  fi
done