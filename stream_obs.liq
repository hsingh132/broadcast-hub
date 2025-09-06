#!/usr/bin/liquidsoap

# ---- Logging (print to stdout; level 3 = info) ----
set("log.stdout", true)
set("log.level", 3)
set("log.file", false)

# ---- Control & audio engine tweaks ----
set("server.telnet", true)
set("frame.size", 4096)

# ---- Input: pull audio from your OBS RTMP on this VM ----
# Decodes to raw PCM for Liquidsoap. If OBS stops, restart=true will reconnect.
obs_input =
  input.external(
    "bash -c 'ffmpeg -loglevel error -i rtmp://localhost:1935/live/test -f s16le -ac 2 -ar 44100 -'",
    channels = 2,
    samplerate = 44100,
    restart = true
  )

# ---- Safe stream pipeline & metadata passthrough ----
radio = mksafe(obs_input)
radio = amplify(1.0, radio)
radio = server.insert_metadata(radio)

# ---- Output: push MP3 to Radio Dodra Shoutcast ----
output.shoutcast(
  %mp3,
  host = "192.99.170.8",
  port = 5810,
  password = "Dodr@55",
  name = "Dodra Simran Stream",
  genre = "Simran",
  url = "https://dodrasimran.com",
  radio
)
