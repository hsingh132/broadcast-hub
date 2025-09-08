#!/usr/bin/env python3
import os, psutil, datetime, signal, subprocess, time, telnetlib, threading, re, html
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
)
from collections import deque
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# --- Paths & constants ---
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SCRIPT_RADIO = "/home/hdsingh132/full_dashboard/stream_obs.liq"
SCRIPT_RADIO2 = "/home/hdsingh132/stream_obs2.liq"
SCRIPT_MUX_WATCH = "/home/hdsingh132/mux_watch_yta.sh"
YT_FALLBACK_FILE = "/home/hdsingh132/static/black_silent.mp4"
KEYS_PATH  = os.path.join(BASE_DIR, "keys.env")
LOG_DIR    = os.path.join(BASE_DIR, "logs")
RUN_DIR    = os.path.join(BASE_DIR, "run")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(RUN_DIR, exist_ok=True)

INPUT_URL  = "rtmp://localhost:1935/live/test"  # OBS RTMP published on this VM

# RD (Radio Dodra) title monitor constants
RD_STATUS_URL = os.environ.get("RD_STATUS_URL", "http://192.99.41.102:5386/index.html?sid=1")
RD_POLL_SECONDS = int(os.environ.get("RD_POLL_SECONDS", "15"))
TITLE_LOG = os.path.join(LOG_DIR, "rd_title_changes.log")
TITLE_BUF_MAX = int(os.environ.get("RD_TITLE_BUF_MAX", "300"))  # in-memory entries
TITLE_FILE_CAP = int(os.environ.get("RD_TITLE_FILE_CAP", str(256 * 1024)))  # 256 KB cap
LOCAL_TZ = ZoneInfo("America/Los_Angeles") if ZoneInfo else None

# Targets
YTA = "yta"
YTB = "ytb"
RAD = "radio"
RAD2 = "radio2"

# --- Flask config & auth ---
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("DASHBOARD_SECRET", "change-me-please")
USERNAME = os.environ.get("DASHBOARD_USER", "rd")
PASSWORD = os.environ.get("DASHBOARD_PASS", "rd")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("username") == USERNAME and request.form.get("password") == PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        flash("Invalid credentials.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# --- Keys helpers ---
def read_keys():
    data = {"KEY1": "", "KEY2": ""}
    if os.path.exists(KEYS_PATH):
        with open(KEYS_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                if k in data: data[k] = v.strip()
    else:
        write_keys("", "")
    return data

def write_keys(k1, k2):
    with open(KEYS_PATH, "w") as f:
        f.write(f"KEY1={k1}\n")
        f.write(f"KEY2={k2}\n")

def mask(s):
    if not s: return ""
    return "*"*(max(0, len(s)-4)) + s[-4:] if len(s) > 4 else "*"*len(s)

# --- Proc helpers ---
def pidfile(target): return os.path.join(RUN_DIR, f"{target}.pid")
def logfile(target): return os.path.join(LOG_DIR, f"{target}.log")

def _pid_is_alive(pid:int)->bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def _read_pid(target):
    pf = pidfile(target)
    if os.path.exists(pf):
        try:
            with open(pf) as f:
                pid = int(f.read().strip())
            return pid if _pid_is_alive(pid) else None
        except Exception:
            return None
    return None

def _write_pid(target, pid:int):
    with open(pidfile(target), "w") as f:
        f.write(str(pid))

def _start_bg(cmd:list, target:str):
    lf = open(logfile(target), "ab", buffering=0)  # append, unbuffered
    p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, preexec_fn=os.setsid, cwd=BASE_DIR)
    _write_pid(target, p.pid)
    return p.pid

def _stop_target(target:str):
    pid = _read_pid(target)
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)  # terminate process group
        except Exception:
            pass
        # quick wait + hard kill if needed
        for _ in range(20):
            if not _pid_is_alive(pid): break
            time.sleep(0.1)
        if _pid_is_alive(pid):
            try: os.killpg(pid, signal.SIGKILL)
            except Exception: pass
    # best-effort cleanup of known commands if pid missing/stale
    if target in (YTA, YTB):
        # Stop any ffmpeg sender and our mux scripts if pid missing/stale
        try:
            subprocess.run(["pkill", "-f", f"ffmpeg .*{INPUT_URL}"], check=False)
        except Exception:
            pass
        for pat in [
            r"mux_watch_yta\.sh",
            r"mux_consumer_yta\.sh",
            r"producer_obs_yta\.sh",
            r"producer_fallback_yta\.sh",
        ]:
            try:
                subprocess.run(["pkill", "-f", pat], check=False)
            except Exception:
                pass
    elif target == RAD:
        try:
            subprocess.run(["pkill", "-f", "liquidsoap .*stream_obs.liq"], check=False)
        except Exception:
            pass
    elif target == RAD2:
        try:
            subprocess.run(["pkill", "-f", "liquidsoap .*stream_obs2.liq"], check=False)
        except Exception:
            pass
    # remove pidfile
    try: os.remove(pidfile(target))
    except Exception: pass

def _clear_log(target: str):
    try:
        with open(logfile(target), "w") as f:
            f.write("")  # truncate to zero
    except Exception:
        pass

# --- RD Title Monitor helpers ---
_rd_log_buf = deque(maxlen=TITLE_BUF_MAX)
_rd_last_state = {"online": None, "title": None}
_rd_thread_started = False

def _pst_now_str():
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    if LOCAL_TZ:
        now = now.astimezone(LOCAL_TZ)
        # Include zone abbreviation like PST/PDT
        return now.strftime("%Y-%m-%d %H:%M:%S %Z")
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _titlefile_cap_tail():
    try:
        if os.path.exists(TITLE_LOG) and os.path.getsize(TITLE_LOG) > TITLE_FILE_CAP:
            keep = max(4096, TITLE_FILE_CAP // 4)
            with open(TITLE_LOG, "rb") as f:
                f.seek(-keep, os.SEEK_END)
                tail = f.read()
            with open(TITLE_LOG, "wb") as f:
                f.write(b"...(truncated)\n")
                f.write(tail)
    except Exception:
        pass

def _append_title_log(line: str):
    ts = _pst_now_str()
    entry = f"[{ts}] {line}"
    _rd_log_buf.append(entry)
    try:
        _titlefile_cap_tail()
        with open(TITLE_LOG, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass

def _fetch_radio_status():
    """
    Scrape Shoutcast HTML status page and extract online/title.
    Returns dict: {"online": bool, "title": str}
    """
    try:
        req = Request(RD_STATUS_URL, headers={"User-Agent": "BroadcastHub/1.0"})
        with urlopen(req, timeout=5) as r:
            html_text = r.read().decode("utf-8", "ignore")
    except (URLError, HTTPError):
        return {"online": False, "title": ""}

    # Determine online from common phrase
    online = bool(re.search(r"Stream\s*Status:\s*Stream\s*is\s*up", html_text, re.I))

    # Extract "Current Song" cell: <td>Current Song:</td><td>Title</td>
    m = re.search(r"Current\s*Song\s*:</td>\s*<td[^>]*>(.*?)</td>", html_text, re.I | re.S)
    title = ""
    if m:
        # Remove any HTML tags & entities
        raw = m.group(1)
        # Strip tags
        raw = re.sub(r"<.*?>", "", raw)
        title = html.unescape(raw).strip()

    return {"online": online, "title": title}

def _rd_monitor_loop():
    global _rd_last_state
    while True:
        try:
            st = _fetch_radio_status()
            online, title = st["online"], st["title"]

            if _rd_last_state["online"] is None:
                # first observation, set baseline without logging noise
                _rd_last_state = {"online": online, "title": title}
            else:
                # online/offline transitions
                if online != _rd_last_state["online"]:
                    if online:
                        _append_title_log(f"Radio ONLINE, title='{title or '(none)'}'")
                    else:
                        _append_title_log("Radio OFFLINE")
                # title changes while online
                if online and title != _rd_last_state["title"]:
                    _append_title_log(f"Title changed: '{_rd_last_state['title'] or '(none)'}' -> '{title or '(none)'}'")

                _rd_last_state = {"online": online, "title": title}
        except Exception as e:
            _append_title_log(f"(monitor error: {e})")
        time.sleep(RD_POLL_SECONDS)

def _start_monitor_thread_once():
    global _rd_thread_started
    if _rd_thread_started:
        return
    t = threading.Thread(target=_rd_monitor_loop, daemon=True)
    t.start()
    _rd_thread_started = True

#
# --- Flask 3.x COMPAT: start monitor at import ---
# DO NOT CHANGE OR REVERT TO @app.before_first_request.
# Flask 3 removed that decorator. Starting the monitor at import ensures
# it runs under systemd/waitress as soon as the module is imported.
# DO NOT use @app.before_first_request — it no longer exists in Flask 3.
try:
    _start_monitor_thread_once()
except Exception as e:
    print(f"[init] RD monitor not started (ignored): {e}")


#telnet server helper function
def update_radio_title(new_title: str) -> str:
    results = []
    for port, label in ((1234, "RD1"), (1235, "RD2")):
        try:
            tn = telnetlib.Telnet("localhost", port, timeout=5)
            cmd = f'insert_metadata_0.insert title="{new_title}"\n'.encode("utf-8")
            tn.write(cmd)
            tn.write(b"\n")
            tn.close()
            results.append(f"{label}: OK")
        except Exception as e:
            results.append(f"{label}: {e}")
    return " / ".join(results)

# --- Commands for each target ---
def cmd_yt(endpoint_letter: str, key: str):
    """Start YouTube A/B via the ffmpeg mux pipeline.

We execute ~/mux_watch_yta.sh with environment variables:
  - YT_KEY        : YouTube stream key
  - INPUT_URL     : OBS RTMP on this VM
  - FALLBACK_FILE : path to local MP4 (black/silent)
  - YT_INGEST     : rtmp://a.rtmp.youtube.com/live2 or .../b.rtmp.youtube.com/live2

NOTE TO FUTURE SELF: DO NOT switch this back to Liquidsoap here.
The A/V mux + failover lives in these shell scripts now.
"""
    ingest = "rtmp://a.rtmp.youtube.com/live2" if endpoint_letter.lower() == "a" else "rtmp://b.rtmp.youtube.com/live2"
    env_export = (
        f'YT_KEY="{key}" '
        f'INPUT_URL="{INPUT_URL}" '
        f'FALLBACK_FILE="{YT_FALLBACK_FILE}" '
        f'YT_INGEST="{ingest}"'
    )
    # Force bash for the watch script so `set -euo pipefail` is honored.
    # Running via /bin/sh (dash) causes "Illegal option -o pipefail".
    # We run a single supervisor script; it spawns/respawns the producers/consumer.
    return [
        "bash", "-lc",
        f'echo "RUN: {SCRIPT_MUX_WATCH}" ; exec env {env_export} bash "{SCRIPT_MUX_WATCH}" </dev/null'
    ]

def cmd_radio():
    # Run exactly one Liquidsoap process, matching the manual success path
    return ["bash","-lc", f'echo "RUN: liquidsoap {SCRIPT_RADIO}" ; exec liquidsoap "{SCRIPT_RADIO}" </dev/null']

def cmd_radio2():
    # Same approach for RD2
    return ["bash","-lc", f'echo "RUN: liquidsoap {SCRIPT_RADIO2}" ; exec liquidsoap "{SCRIPT_RADIO2}" </dev/null']

# --- Views ---
@app.route("/")
@login_required
def index():
    keys = read_keys()
    masked = {"KEY1": mask(keys["KEY1"]), "KEY2": mask(keys["KEY2"])}
    return render_template("index.html", masked=masked, have_keys=bool(keys["KEY1"] or keys["KEY2"]))

@app.route("/save_keys", methods=["POST"])
@login_required
def save_keys():
    write_keys(request.form.get("key1","").strip(), request.form.get("key2","").strip())
    flash("Stream keys saved.", "success")
    return redirect(url_for("index"))

@app.route("/start", methods=["POST"])
@login_required
def start_targets():
    keys = read_keys()
    want_yta = request.form.get("yta") == "on"
    want_ytb = request.form.get("ytb") == "on"
    want_rad = request.form.get("radio") == "on"
    want_rad2 = request.form.get("radio2") == "on"

    started = []
    if want_yta:
        if not keys["KEY1"]:
            flash("YouTube A key is empty.", "error")
        elif not _read_pid(YTA):
            pid = _start_bg(cmd_yt("a", keys["KEY1"]), YTA)
            started.append(("YouTube A", pid))
    if want_ytb:
        if not keys["KEY2"]:
            flash("YouTube B key is empty.", "error")
        elif not _read_pid(YTB):
            pid = _start_bg(cmd_yt("b", keys["KEY2"]), YTB)
            started.append(("YouTube B", pid))
    if want_rad and not _read_pid(RAD):
        pid = _start_bg(cmd_radio(), RAD)
        started.append(("Radio Dodra", pid))
    if want_rad2 and not _read_pid(RAD2):
        pid = _start_bg(cmd_radio2(), RAD2)
        started.append(("Radio Dodra 2", pid))

    if started:
        flash("Started: " + ", ".join([f"{name} (pid {pid})" for name, pid in started]), "success")
    else:
        flash("Nothing started (already running or missing keys).", "error")
    return redirect(url_for("index"))

@app.route("/stop", methods=["POST"])
@login_required
def stop_targets():
    want_yta = request.form.get("yta") == "on"
    want_ytb = request.form.get("ytb") == "on"
    want_rad = request.form.get("radio") == "on"
    want_rad2 = request.form.get("radio2") == "on"

    stopped = []
    if want_yta:
        _stop_target(YTA)
        _clear_log(YTA)     # <-- NEW
        stopped.append("YouTube A")
    if want_ytb:
        _stop_target(YTB)
        _clear_log(YTB)     # <-- NEW
        stopped.append("YouTube B")
    if want_rad:
        _stop_target(RAD)
        _clear_log(RAD)     # <-- NEW
        stopped.append("Radio Dodra")
    if want_rad2:
        _stop_target(RAD2)
        _clear_log(RAD2)
        stopped.append("Radio Dodra 2")

    if stopped:
        flash("Stopped: " + ", ".join(stopped), "success")
    else:
        flash("Nothing selected to stop.", "error")
    return redirect(url_for("index"))

@app.route("/rd_status.json")
@login_required
def rd_status_json():
    st = _fetch_radio_status()
    st["time"] = _pst_now_str()
    return jsonify(st)

@app.route("/rd_title_log")
@login_required
def rd_title_log():
    # newest first for convenience
    lines = list(_rd_log_buf)[-200:]
    lines.reverse()
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")

@app.route("/rd_title_log/clear", methods=["POST"])
@login_required
def rd_title_log_clear():
    try:
        _rd_log_buf.clear()
        open(TITLE_LOG, "w").close()
        flash("RD title log cleared.", "success")
    except Exception as e:
        flash(f"Failed to clear RD title log: {e}", "error")
    return redirect(url_for("index"))

@app.route("/status.json")
@login_required
def status_json():
    def stat_for(t):
        pid = _read_pid(t)
        lg = logfile(t)
        mtime = os.path.getmtime(lg) if os.path.exists(lg) else 0
        age = time.time() - mtime if mtime else None
        return {"running": bool(pid), "pid": pid, "log_age_sec": age}

    return jsonify({
        "yta": stat_for(YTA),
        "ytb": stat_for(YTB),
        "radio": stat_for(RAD),
        "radio2": stat_for(RAD2),
        "time": datetime.datetime.utcnow().isoformat() + "Z"
    })

@app.route("/update_radio_title", methods=["POST"])
@login_required
def update_radio_title_route():
    title = request.form.get("radio_title","").strip()
    if not title:
        flash("Title cannot be empty.", "error")
        return redirect(url_for("index"))
    msg = update_radio_title(title)
    level = "success" if "OK" in msg else "error"
    flash(msg, level)
    return redirect(url_for("index"))

def _tail(path, lines):
    if not os.path.exists(path): return ""
    # fast tail without reading full file
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and lines > 0:
                step = min(block, size)
                size -= step
                f.seek(size)
                chunk = f.read(step)
                data = chunk + data
                lines_found = data.count(b"\n")
                if lines_found >= lines: break
            return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", "replace")
    except Exception as e:
        return f"(log read error: {e})"

@app.route("/logs/<target>")
@login_required
def logs_tail(target):
    target = target.lower()
    if target not in (YTA, YTB, RAD, RAD2):
        return Response("unknown target", status=404)
    n = int(request.args.get("lines", "200"))
    return Response(_tail(logfile(target), n), mimetype="text/plain; charset=utf-8")

# Health (same as before)
@app.route("/health.json")
@login_required
def health():
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    return jsonify({
        "time": datetime.datetime.utcnow().isoformat() + "Z",
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "ram_percent": vm.percent,
        "disk_percent": disk.percent,
        "net_bytes_sent": net.bytes_sent,
        "net_bytes_recv": net.bytes_recv,
    })

if __name__ == "__main__":
    # Flask 3 compat: keep this direct call; no decorators.
    _start_monitor_thread_once()
    app.run(host="0.0.0.0", port=5000, debug=False)
