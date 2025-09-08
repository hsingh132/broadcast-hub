#!/usr/bin/liquidsoap

# --- Logging ---
set("log.stdout", true)
set("log.level", 3)

# === OBS-first with file fallback, output directly to YouTube A ===
# NOTE: YT key / input URL / fallback file are passed in via environment
#       variables from the dashboard. Changing the key requires restarting
#       this pipeline (no auto-restart is performed).

input_url    = getenv("INPUT_URL",    default="rtmp://localhost:1935/live/test")
fallback_mp4 = getenv("FALLBACK_FILE", default="/home/hdsingh132/static/black_silent.mp4")
yt_key       = getenv("YT_KEY",       default="")

# Primary: pull the RTMP from OBS (audio+video)
# Liquidsoap 2.x `input.rtmp` demuxes A/V; if OBS goes away it errors and
# fallback() below will select the next source.
obs = input.rtmp(input_url)

# Secondary: a local black/silent MP4 as continuous backup
file_back = single(fallback_mp4)

# Build the program: try OBS, otherwise play the file
program = fallback([obs, file_back], track_sensitive=false)

# Output directly to YouTube ingest for channel A
# Use Liquidsoap's native RTMP output (2.x). We encode A/V here.
yt_url = "rtmp://a.rtmp.youtube.com/live2/" ^ yt_key

output.rtmp(
  program,
  url = yt_url,
  video = video.lavc(x264, preset="veryfast", crf=28, fps=30),
  audio = audio.lavc(aac, bitrate=128)
)