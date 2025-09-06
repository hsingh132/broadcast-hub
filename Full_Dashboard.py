#!/usr/bin/env python3
import os, psutil, datetime, signal, subprocess, time, telnetlib
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
)

# --- Paths & constants ---
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SCRIPT_RADIO = os.path.join(BASE_DIR, "stream_obs.liq")
KEYS_PATH  = os.path.join(BASE_DIR, "keys.env")
LOG_DIR    = os.path.join(BASE_DIR, "logs")
RUN_DIR    = os.path.join(BASE_DIR, "run")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(RUN_DIR, exist_ok=True)

INPUT_URL  = "rtmp://localhost:1935/live/test"  # single VM input

# Targets
YTA = "yta"
YTB = "ytb"
RAD = "radio"

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
        # kill any ffmpeg pulling from our INPUT_URL to rtmp youtube
        try: subprocess.run(["pkill","-f",f"ffmpeg -i {INPUT_URL}"], check=False)
        except Exception: pass
    elif target == RAD:
        try: subprocess.run(["pkill","-f","liquidsoap .*stream_obs.liq"], check=False)
        except Exception: pass
    # remove pidfile
    try: os.remove(pidfile(target))
    except Exception: pass

def _clear_log(target: str):
    try:
        with open(logfile(target), "w") as f:
            f.write("")  # truncate to zero
    except Exception:
        pass

#telnet server helper function
def update_radio_title(new_title: str) -> str:
    try:
        tn = telnetlib.Telnet("localhost", 1234, timeout=3)
        cmd = f'insert_metadata_0.insert title="{new_title}"\n'.encode("utf-8")
        tn.write(cmd)
        tn.write(b"\n")
        tn.close()
        return f"Radio title updated to: {new_title}"
    except Exception as e:
        return f"Error updating title: {e}"

# --- Commands for each target ---
def cmd_yt(endpoint_letter:str, key:str):
    # stream copy path (OBS already 30fps). Endpoint A uses "a.rtmp", B uses "b.rtmp".
    endpoint = f"{endpoint_letter}.rtmp.youtube.com"
    url = f"rtmp://{endpoint}/live2/{key}"
    return ["bash","-lc", f'exec ffmpeg -re -i "{INPUT_URL}" -c copy -f flv "{url}"']

def cmd_radio():
    # Run liquidsoap with our file, writing to stream_obs.log already via our redirection
    return ["bash","-lc", f'exec liquidsoap "{SCRIPT_RADIO}"']

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

    if stopped:
        flash("Stopped: " + ", ".join(stopped), "success")
    else:
        flash("Nothing selected to stop.", "error")
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
        "time": datetime.datetime.utcnow().isoformat()+"Z"
    })

@app.route("/update_radio_title", methods=["POST"])
@login_required
def update_radio_title_route():
    title = request.form.get("radio_title","").strip()
    if not title:
        flash("Title cannot be empty.", "error")
        return redirect(url_for("index"))
    msg = update_radio_title(title)
    flash(msg, "success" if msg.startswith("Radio title updated") else "error")
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
    if target not in (YTA, YTB, RAD):
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
    app.run(host="0.0.0.0", port=5000, debug=False)
