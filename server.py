"""Factory Console v2 - a live control panel for a Windows workstation that renders, trains and runs agents,
with a Game Mode that freezes that work while you play and resumes it exactly where it stopped.

Python standard library only (the optional desktop drop-down, shell.py, needs pywebview). Windows 10/11.
First run writes config.json from the defaults below; edit it to choose what Game Mode pauses.

What it shows, live (Server-Sent Events every 2 s):
  - machine: GPU use / VRAM / temperature / power (NVIDIA), CPU, RAM, network, every fixed disk
  - the newest frame of whatever is rendering, plus a strip of the last frames (from the running renders' output folders)
  - jobs: the Task Scheduler folder you name in config (optional), or the watched processes
  - an optional ledger (SQLite: tasks + events) for "needs you" items and recent events
What it does:
  - GAME MODE: when a listed game starts, matching work processes are SUSPENDED (frozen in place: no CPU, no GPU work,
    no network) and RESUMED where they were when the game closes. Also a manual switch. A frozen Blender keeps its
    graphics memory; "Free graphics memory" stops those renders and restarts their scheduled tasks after the game.
Run:  python server.py [--port 8787]   then open http://127.0.0.1:8787   (desktop drop-down: pythonw shell.py)
History: v1 2026-10-21 (PC Claude, Tool Forge T1). v2 2026-10-22: config defaults + first-run config, auto disks, background
sampler (a page request never waits on a process scan: v1 took 28 s when the task query held WMI), Task Scheduler via COM
(0.6 s instead of ~20 s), frame strip, /api/info, optional ledger / flag file / Ollama stop, serve() for shell.py.
"""
import ctypes, ctypes.wintypes as wt, json, os, re, shutil, socket, sqlite3, subprocess, sys, threading, time, glob
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from urllib.request import urlopen

VERSION = "2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
DEFAULTS = {
    "title": "Factory",
    "host": "127.0.0.1",
    "port": 8787,
    "disks": "auto",                     # "auto" = every fixed drive, or a list like ["C", "D"]
    "games": ["VALORANT-Win64-Shipping.exe", "cs2.exe", "FortniteClient-Win64-Shipping.exe", "r5apex.exe", "r5apex_dx12.exe",
              "LeagueofLegends.exe", "RocketLeague.exe", "Overwatch.exe", "dota2.exe", "GTA5.exe", "eldenring.exe", "bf2042.exe"],
    "resume_after_s": 30,                # how long a game must be closed before work resumes
    "pausable_names": ["blender.exe", "ffmpeg.exe"],   # process names Game Mode may freeze ...
    "factory_paths": [],                 # ... and only when the command line contains one of these (empty = any)
    "never_pause": ["factory_console"],  # command-line text that is never frozen
    "extra_frame_globs": [],             # extra folders to watch for rendered frames (glob patterns)
    "task_folder": "",                   # Task Scheduler folder to list as jobs, e.g. "\\renders\\" (empty = show processes)
    "ledger": "",                        # optional SQLite ledger (tables tasks + events)
    "ledger_needs_status": "needs-you",  # the task status that means "waits on me"
    "game_mode_flag": "",                # optional JSON file other scripts read before heavy work
    "stop_ollama_in_game": False,        # stop the Ollama server during a game (a loaded model holds GPU memory)
    "ollama_app": "",                    # Ollama app path for the restart after the game (empty = default install)
    "feeds_dir": "",                     # where factory_feed.py / POST /api/feed reports land (empty = ./feeds)
    "shell": {"width": 1240, "height": 740, "hotkey": "ctrl+alt+f", "dwell_ms": 220, "hide_after_ms": 450, "pill_width": 180,
              "server_task": ""}    # "\\folder\\task" = start the console as that scheduled task; empty = inside the shell
}


def load_config():
    if not os.path.exists(CONFIG_FILE):
        json.dump(DEFAULTS, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2)
    user = json.load(open(CONFIG_FILE, encoding="utf-8-sig"))     # -sig: Windows PowerShell 5.1 writes a BOM
    cfg = json.loads(json.dumps(DEFAULTS)); cfg.update(user)
    cfg["shell"] = {**DEFAULTS["shell"], **(user.get("shell") or {})}
    return cfg


CFG = load_config()
STATE_FILE = os.path.join(HERE, "state.json")
FLAG_FILE = CFG.get("game_mode_flag") or ""      # other scripts read this before starting heavy work
NO_WINDOW = 0x08000000                           # CREATE_NO_WINDOW for helper processes


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW, encoding="utf-8", errors="replace").stdout
    except Exception as e:
        return ""


PS_EXE = "pwsh" if shutil.which("pwsh") else "powershell"     # PowerShell 7 when installed, else Windows PowerShell 5.1


def ps(script, timeout=30):
    return run([PS_EXE, "-NoProfile", "-NonInteractive", "-Command", script], timeout)


# ---------------------------------------------------------------- machine stats (ctypes + nvidia-smi + netstat)
class MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
_last_cpu = None


def cpu_ram():
    global _last_cpu
    idle, kern, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
    cur = (f(idle), f(kern), f(user))
    cpu = None
    if _last_cpu:
        di, dk, du = (cur[i] - _last_cpu[i] for i in range(3))
        tot = dk + du
        cpu = round(100.0 * (tot - di) / tot, 1) if tot else 0.0
    _last_cpu = cur
    m = MEMSTAT(); m.dwLength = ctypes.sizeof(MEMSTAT)
    k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return cpu, round(m.ullAvailPhys / 2**30, 2), round(m.ullTotalPhys / 2**30, 1)


def gpu():
    out = run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"], 10)
    try:
        name, u, mu, mt, t, p = [x.strip() for x in out.strip().splitlines()[0].split(",")]
        return {"name": name.replace("NVIDIA ", "").replace("GeForce ", ""), "util": int(u), "vram_used": int(mu), "vram_total": int(mt),
                "temp": int(t), "power": float(p) if p.replace(".", "").isdigit() else None}
    except Exception:
        return None


def gpu_apps():
    out = run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"], 10)
    res = {}
    for line in out.strip().splitlines():
        try:
            pid, mem = [int(x) for x in line.split(",")]
            res[pid] = mem
        except Exception:
            pass
    return res


_last_net = None


def net():
    global _last_net
    out = run(["netstat", "-e"], 10)
    m = re.search(r"^\s*(?:Bytes|\S+)\s+(\d+)\s+(\d+)\s*$", out, re.M)    # first numeric row = bytes received / sent
    if not m:
        return None
    now = (time.time(), int(m.group(1)), int(m.group(2)))
    rate = None
    if _last_net:
        dt = now[0] - _last_net[0]
        if dt > 0:
            rate = {"down_kbs": round(max(0, now[1] - _last_net[1]) / dt / 1024, 1), "up_kbs": round(max(0, now[2] - _last_net[2]) / dt / 1024, 1)}
    _last_net = now
    return rate


def disk_letters():
    if CFG["disks"] != "auto":
        return list(CFG["disks"])
    mask = k32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            d = chr(65 + i)
            if k32.GetDriveTypeW(ctypes.c_wchar_p(d + ":\\")) == 3:      # DRIVE_FIXED
                out.append(d)
    return out


def disks():
    res = {}
    for d in disk_letters():
        free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        if k32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(d + ":\\"), ctypes.byref(free), ctypes.byref(total), None):
            res[d] = {"free": round(free.value / 2**30, 1), "total": round(total.value / 2**30, 1)}
    return res


# ---------------------------------------------------------------- processes (one PowerShell call, refreshed in the background)
PROC_CACHE = {"t": 0, "rows": []}
PROC_LOCK = threading.Lock()


def processes(max_age=4):
    if time.time() - PROC_CACHE["t"] < max_age:
        return PROC_CACHE["rows"]
    with PROC_LOCK:
        if time.time() - PROC_CACHE["t"] < max_age:
            return PROC_CACHE["rows"]
        out = ps("Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine | ConvertTo-Json -Compress", 30)
        try:
            rows = json.loads(out) if out.strip() else []
        except Exception:
            rows = PROC_CACHE["rows"]
        PROC_CACHE.update(t=time.time(), rows=rows)
        return rows


def factory_procs(rows):
    """processes that belong to work jobs: matched by NAME first, then by a work path in the command line (empty list = any).
    (Matching command lines alone also catches shells whose text merely mentions a path; lesson of 2026-10-21.)"""
    names = set(n.lower() for n in CFG["pausable_names"])
    pats = [p.lower() for p in CFG["factory_paths"]]
    never = [p.lower() for p in CFG["never_pause"]]
    out = []
    for r in rows:
        n = (r.get("Name") or "").lower(); cl = (r.get("CommandLine") or "").lower()
        if n not in names or not cl:
            continue
        if any(x in cl for x in never):
            continue
        if not pats or any(p in cl for p in pats):
            out.append(r)
    return out


def games_running(rows):
    g = set(x.lower() for x in CFG["games"])
    return sorted({r["Name"] for r in rows if (r.get("Name") or "").lower() in g})


def games_fast():
    """tasklist is ~20x cheaper than a full Win32_Process query: used every 3 s while a game may be running"""
    g = set(x.lower() for x in CFG["games"])
    out = run(["tasklist", "/fo", "csv", "/nh"], 10)
    return sorted({line.split('","')[0].strip('"') for line in out.splitlines() if line.split('","')[0].strip('"').lower() in g})


# ---------------------------------------------------------------- jobs, frames, ledger
JOBS_CACHE = {"t": 0, "rows": []}
TASK_STATES = "@{0='Unknown';1='Disabled';2='Queued';3='Ready';4='Running'}"


def jobs(max_age=15):
    folder = (CFG.get("task_folder") or "").strip()
    if not folder:
        return []
    if time.time() - JOBS_CACHE["t"] < max_age:
        return JOBS_CACHE["rows"]
    f = "\\" + folder.strip("\\")
    script = ("$s = New-Object -ComObject Schedule.Service; $s.Connect(); $m = %s; "
              "@($s.GetFolder('%s').GetTasks(1) | ForEach-Object { $t = $_.LastRunTime; [pscustomobject]@{ name = $_.Name; state = $m[[int]$_.State]; "
              "last = $(if ($t.Year -gt 2000) { $t.ToString('yyyy-MM-dd HH:mm') } else { '' }); result = $_.LastTaskResult; "
              "action = (($_.Definition.Actions | Select-Object -First 1).Arguments) } }) | ConvertTo-Json -Compress") % (TASK_STATES, f.replace("'", "''"))
    try:
        rows = json.loads(ps(script, 30) or "[]")
        rows = rows if isinstance(rows, list) else [rows]
    except Exception:
        rows = JOBS_CACHE["rows"]
    for r in rows:
        r["log"] = last_log_line(r.get("action") or "")
    rank = {"Running": 0, "Queued": 1, "Ready": 2, "Disabled": 3}
    rows.sort(key=lambda r: (rank.get(str(r.get("state")), 2), "".join(chr(255 - ord(ch)) for ch in (r.get("last") or ""))))   # running first, then newest run first
    JOBS_CACHE.update(t=time.time(), rows=rows)
    return rows


# ---------------------------------------------------------------- feeds: any script reports its own job (factory_feed.py)
FEEDS_DIR = CFG.get("feeds_dir") or os.path.join(HERE, "feeds")
FEED_STATES = {"running": "Running", "queued": "Queued", "done": "Ready", "ok": "Ready", "failed": "Failed", "error": "Failed", "idle": "Ready"}


def _slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("._")[:80] or "job"


def write_feed(d):
    """store one feed update: {name, state, progress 0-1, log, frames_dir} -> feeds/<name>.json"""
    if not isinstance(d, dict) or not d.get("name"):
        raise ValueError("a feed needs a name")
    os.makedirs(FEEDS_DIR, exist_ok=True)
    row = {"name": str(d["name"])[:120], "state": str(d.get("state") or "running").lower(), "log": str(d.get("log") or "")[:300],
           "progress": None, "frames_dir": str(d.get("frames_dir") or "")[:400], "updated": time.time()}
    try:
        p = d.get("progress")
        row["progress"] = None if p is None else max(0.0, min(1.0, float(p)))
    except (TypeError, ValueError):
        pass
    tmp = os.path.join(FEEDS_DIR, _slug(row["name"]) + ".json.tmp")
    json.dump(row, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, os.path.join(FEEDS_DIR, _slug(row["name"]) + ".json"))
    return row


def read_feeds(max_age_h=24):
    """feed rows for the job list; a 'running' feed silent for 10 min shows as stalled; finished ones fade after a day"""
    rows = []
    for p in glob.glob(os.path.join(FEEDS_DIR, "*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        age = time.time() - float(d.get("updated") or os.path.getmtime(p))
        st = FEED_STATES.get(str(d.get("state")).lower(), "Ready")
        if st != "Running" and age > max_age_h * 3600:
            continue
        log = d.get("log") or ""
        if st == "Running" and age > 600:
            log = "no update for %d min · %s" % (age // 60, log)
        rows.append({"name": d.get("name") or os.path.basename(p)[:-5], "state": st, "progress": d.get("progress"), "log": log,
                     "last": time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - age)), "result": 1 if st == "Failed" else 0,
                     "source": "feed", "frames_dir": d.get("frames_dir") or "", "age_s": int(age)})
    rows.sort(key=lambda r: r["age_s"])
    return rows


def last_log_line(action):
    """find the script a task runs and the log it writes ('$log = "...\\x.log"' lines in the .ps1), return its last line"""
    m = re.search(r"-File\s+(\S+\.ps1)", action)
    if not m or not os.path.exists(m.group(1)):
        return ""
    try:
        src = open(m.group(1), encoding="utf-8", errors="replace").read()
    except Exception:
        return ""
    logs = re.findall(r"\$log\s*=\s*['\"]([^'\"]+\.log)['\"]", src)
    w = re.search(r"\$w\s*=\s*['\"]([^'\"]+)['\"]", src)
    dvar = re.search(r"\$d\s*=\s*['\"]([^'\"]+)['\"]", src)
    for lg in logs:
        if "$w" in lg and w:
            lg = lg.replace("$w", w.group(1))
        if "$d" in lg and dvar:
            lg = lg.replace("$d", dvar.group(1))
        if os.path.exists(lg):
            try:
                with open(lg, "rb") as f:
                    f.seek(max(0, os.path.getsize(lg) - 4000))
                    tail = f.read().decode("utf-8", "replace").replace("\x00", "").strip().splitlines()
                return tail[-1][:220] if tail else ""
            except Exception:
                return ""
    return ""


def render_dirs(rows, any_age=False):
    """output folders of running renders, read from the command lines (--out / --render / -o), plus the watched globs
    (any_age=True: also folders that stopped changing, so the monitor can show the last frame after a restart)"""
    dirs = set()
    for r in rows:
        cl = r.get("CommandLine") or ""
        if (r.get("Name") or "").lower() not in ("blender.exe", "python.exe", "pwsh.exe", "ffmpeg.exe"):
            continue
        for m in re.finditer(r"(?:--out|--render|-o)\s+\"?([A-Za-z]:\\[^\"]+?)(?:\\c_#+|\\f_#+|\\#+|\")?(?:\s|$)", cl):
            p = m.group(1).rstrip("\\")
            if os.path.isdir(p):
                dirs.add(p)
            elif os.path.isdir(os.path.dirname(p)):
                dirs.add(os.path.dirname(p))
    for g in CFG["extra_frame_globs"]:
        for p in glob.glob(g):
            if os.path.isdir(p) and (any_age or time.time() - os.path.getmtime(p) < 900):
                dirs.add(p)
    for f in read_feeds():                                     # a feed can point at the folder it renders into
        if f["frames_dir"] and os.path.isdir(f["frames_dir"]) and (any_age or f["state"] == "Running"):
            dirs.add(f["frames_dir"])
    return sorted(dirs)


FRAME = {"path": None, "mtime": 0, "dir": None, "count": 0}
IMG = (".png", ".jpg", ".jpeg")


def newest_frame(dirs):
    best = None
    for d in dirs:
        try:
            with os.scandir(d) as it:
                files = [e for e in it if e.is_file() and e.name.lower().endswith(IMG + (".exr",))]
        except Exception:
            continue
        if not files:
            continue
        f = max(files, key=lambda e: e.stat().st_mtime)
        if not best or f.stat().st_mtime > best[1]:
            best = (f.path, f.stat().st_mtime, d, len(files))
    if best and best[0].lower().endswith(IMG):
        FRAME.update(path=best[0], mtime=best[1], dir=best[2], count=best[3])
    return FRAME if FRAME["path"] else None


def recent_frames(n=12):
    d = FRAME.get("dir")
    if not d or not os.path.isdir(d):
        return []
    try:
        with os.scandir(d) as it:
            files = [(e.name, e.stat().st_mtime) for e in it if e.is_file() and e.name.lower().endswith(IMG)]
    except Exception:
        return []
    files.sort(key=lambda x: x[1], reverse=True)
    return [{"name": nm, "mtime": int(mt)} for nm, mt in files[:n]]


def ledger():
    path = CFG.get("ledger") or ""
    if not path or not os.path.exists(path):
        return {"off": True}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=5)
        counts = dict(c.execute("select status, count(*) from tasks group by status").fetchall())
        you = [{"id": i, "t": t[:160]} for i, t in c.execute(
            "select id, title from tasks where status=? order by updated desc limit 20", (CFG["ledger_needs_status"],)).fetchall()]
        ev = [{"ts": ts[5:], "who": a, "task": tk, "msg": (m or "")[:200]} for ts, a, tk, m in c.execute(
            "select ts, actor, task, msg from events where actor != 'factory_watch' order by ts desc limit 20").fetchall()]
        c.close()
        return {"counts": counts, "needs": CFG["ledger_needs_status"], "you": you, "events": ev}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------- game mode: suspend / resume
PROCESS_SUSPEND_RESUME = 0x0800
LOCK = threading.Lock()


def _state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {"game_mode": False, "suspended": [], "restart_tasks": [], "since": None, "reason": None, "auto": True, "log": []}


def _save(st):
    st["log"] = st.get("log", [])[-40:]
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), indent=1)
    if FLAG_FILE:
        json.dump({"game_mode": st["game_mode"], "since": st["since"], "reason": st["reason"]}, open(FLAG_FILE, "w", encoding="utf-8"))


def _note(st, msg):
    st.setdefault("log", []).append(time.strftime("%H:%M:%S ") + msg)


def _sr(pid, fn):
    h = k32.OpenProcess(PROCESS_SUSPEND_RESUME, False, int(pid))
    if not h:
        return False
    try:
        return fn(h) == 0
    finally:
        k32.CloseHandle(h)


def game_on(reason):
    with LOCK:
        st = _state()
        if st["game_mode"]:
            return st
        PROC_CACHE["t"] = 0
        procs = factory_procs(processes(0))
        done = []
        for r in procs:
            if _sr(r["ProcessId"], ntdll.NtSuspendProcess):
                done.append({"pid": r["ProcessId"], "name": r["Name"], "cmd": (r.get("CommandLine") or "")[:160]})
        st.update(game_mode=True, suspended=done, since=time.strftime("%Y-%m-%d %H:%M:%S"), reason=reason)
        _note(st, "GAME MODE ON (%s): froze %d work processes" % (reason, len(done)))
        # Ollama is never frozen (other tools share it), but a loaded model holds ~4-7 GB of graphics memory. 10-21: unloading the
        # model or ending its runner was not enough: the frozen job's open request made Ollama load qwen3.5:9b again mid-match
        # (19:31, 7.97 GB + 100% GPU, low fps). So stop the whole Ollama server; game_off starts it again BEFORE resuming the jobs.
        if CFG.get("stop_ollama_in_game"):
            try:
                names = run(["tasklist", "/fo", "csv", "/nh"], 10).lower()
                if "ollama" in names or "llama-server" in names:
                    for im in ("ollama app.exe", "ollama.exe", "llama-server.exe"):
                        subprocess.run(["taskkill", "/IM", im, "/F"], capture_output=True, creationflags=NO_WINDOW)
                    st["ollama_stopped"] = True
                    _note(st, "stopped Ollama (app, server, model runner): no model can load onto the GPU during the game")
            except Exception as e:
                _note(st, "Ollama stop skipped: %r" % e)
        _save(st)
        return st


def game_off(reason):
    with LOCK:
        st = _state()
        if not st["game_mode"]:
            return st
        if st.pop("ollama_stopped", False):
            # start it through explorer so Ollama is not a child of this console's scheduled task; wait until it answers,
            # so the resumed jobs find the server up (their one in-flight request fails once, then they carry on)
            subprocess.run(["explorer.exe", CFG.get("ollama_app") or os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama app.exe")],
                           creationflags=NO_WINDOW)
            up = False
            for _ in range(40):
                try:
                    up = bool(urlopen("http://127.0.0.1:11434/api/version", timeout=2).read()); break
                except Exception:
                    time.sleep(1)
            _note(st, "started Ollama again (%s)" % ("answers on 11434" if up else "NOT answering after 40 s"))
        n = sum(1 for p in st.get("suspended", []) if _sr(p["pid"], ntdll.NtResumeProcess))
        tf = (CFG.get("task_folder") or "").strip("\\ ")
        folder = "\\%s\\" % tf if tf else "\\"
        for t in st.get("restart_tasks", []):
            ps("Start-ScheduledTask -TaskPath '%s' -TaskName '%s'" % (folder, t.replace("'", "")), 30)
        _note(st, "GAME MODE OFF (%s): resumed %d processes, restarted %d tasks" % (reason, n, len(st.get("restart_tasks", []))))
        st.update(game_mode=False, suspended=[], restart_tasks=[], since=None, reason=None)
        _save(st)
        return st


def free_gpu():
    """stop the suspended Blender renders that hold graphics memory; their tasks restart when game mode ends"""
    with LOCK:
        st = _state()
        held = gpu_apps()
        rows = processes(0)
        by_pid = {r["ProcessId"]: r for r in rows}
        killed = []
        for p in list(st.get("suspended", [])):
            if p["name"].lower() == "blender.exe" and held.get(p["pid"], 0) > 200:
                task = task_of(p["pid"], by_pid)
                subprocess.run(["taskkill", "/PID", str(p["pid"]), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
                st["suspended"].remove(p)
                killed.append(p["pid"])
                if task and task not in st["restart_tasks"]:
                    st["restart_tasks"].append(task)
        _note(st, "freed GPU memory: stopped %s; will restart %s" % (killed, st["restart_tasks"]))
        _save(st)
        return st


def task_of(pid, by_pid):
    """walk up the parent chain to the shell that runs a scheduled task's .ps1, and match it to a task name"""
    seen = 0
    while pid in by_pid and seen < 8:
        cl = by_pid[pid].get("CommandLine") or ""
        m = re.search(r"-File\s+(\S+\.ps1)", cl)
        if m:
            for j in JOBS_CACHE["rows"]:
                if m.group(1).lower() in (j.get("action") or "").lower():
                    return j["name"]
        pid = by_pid[pid].get("ParentProcessId"); seen += 1
    return None


def auto_loop():
    """watch for games: on start -> game mode on; gone for resume_after_s -> off (only if game mode was switched on automatically)"""
    gone_since = None
    while True:
        try:
            st = _state()
            g = games_fast()
            if g and not st["game_mode"] and st.get("auto", True):
                game_on("auto: " + ", ".join(g)); gone_since = None
            elif not g and st["game_mode"] and (st.get("reason") or "").startswith("auto"):
                gone_since = gone_since or time.time()
                if time.time() - gone_since > CFG["resume_after_s"]:
                    game_off("auto: game closed"); gone_since = None
            else:
                gone_since = None
        except Exception as e:
            pass
        time.sleep(3)


# ---------------------------------------------------------------- background sampler: requests only read caches
HIST = {"gpu": [], "cpu": [], "down": [], "up": []}
CLIENTS = [0]          # open live pages; scans run faster while someone is watching
SNAP = {"data": None}


def sample():
    cpu, ram_free, ram_total = cpu_ram()
    g = gpu(); n = net(); rows = processes(0)
    st = _state()
    for k, v in (("gpu", g and g["util"]), ("cpu", cpu), ("down", n and n["down_kbs"]), ("up", n and n["up_kbs"])):
        if v is not None:
            HIST[k] = (HIST[k] + [v])[-90:]
    fp = factory_procs(rows)
    held = gpu_apps()
    fr = newest_frame(render_dirs(rows))
    if not fr and not SNAP.get("scanned_old"):
        SNAP["scanned_old"] = True                      # once: the newest frame of any watched folder, however old
        fr = newest_frame(render_dirs(rows, any_age=True))
    SNAP["data"] = {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gpu": g, "cpu": cpu, "ram_free": ram_free, "ram_total": ram_total, "net": n, "disks": disks(), "hist": HIST,
        "games": games_running(rows),
        "factory_procs": [{"pid": r["ProcessId"], "name": r["Name"], "vram": held.get(r["ProcessId"], 0),
                           "cmd": re.sub(r"\s+", " ", (r.get("CommandLine") or ""))[:150]} for r in fp],
        "game": {k: st.get(k) for k in ("game_mode", "since", "reason", "auto", "restart_tasks")} | {"suspended": len(st.get("suspended", [])), "log": st.get("log", [])[-8:]},
        "frame": fr and {"url": "/frame?t=%d" % int(fr["mtime"]), "name": os.path.basename(fr["path"]), "dir": fr["dir"], "count": fr["count"],
                         "age_s": int(time.time() - fr["mtime"])},
    }
    return SNAP["data"]


def sampler_loop():
    while True:
        try:
            sample()
        except Exception:
            pass
        time.sleep(2 if CLIENTS[0] > 0 else 10)


def job_loop():
    while True:
        if CLIENTS[0] > 0:
            jobs(0)
        time.sleep(15)


def snapshot():
    if SNAP["data"] is None:
        return sample()
    d = dict(SNAP["data"])
    if d.get("frame"):
        d["frame"] = dict(d["frame"], age_s=int(time.time() - FRAME["mtime"]))
    return d


def info():
    return {"title": CFG["title"], "version": VERSION, "host": socket.gethostname(),
            "features": {"jobs": bool((CFG.get("task_folder") or "").strip()) or bool(glob.glob(os.path.join(FEEDS_DIR, "*.json"))), "ledger": bool(CFG.get("ledger") and os.path.exists(CFG["ledger"])),
                         "gpu": SNAP["data"] is None or bool(SNAP["data"].get("gpu"))},
            "games": CFG["games"], "resume_after_s": CFG["resume_after_s"]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _img(self, path):
        return self._send(200, open(path, "rb").read(), "image/png" if path.lower().endswith(".png") else "image/jpeg")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, open(os.path.join(HERE, "static", "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path.startswith("/static/"):
            p = os.path.normpath(os.path.join(HERE, u.path.lstrip("/")))
            if not p.startswith(os.path.join(HERE, "static")) or not os.path.isfile(p):
                return self._send(404, "{}")
            ct = {".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2"}.get(os.path.splitext(p)[1], "application/octet-stream")
            return self._send(200, open(p, "rb").read(), ct + ("; charset=utf-8" if ct.startswith(("text", "application/j")) else ""))
        if u.path == "/api/info":
            return self._send(200, json.dumps(info()))
        if u.path == "/api/snapshot":
            return self._send(200, json.dumps(snapshot()))
        if u.path == "/api/jobs":
            feeds = read_feeds()
            if (CFG.get("task_folder") or "").strip():
                rows = jobs()
            else:
                rows = [{"name": "%s %s" % (p["name"], p["pid"]), "state": "Running", "last": "", "log": p["cmd"], "source": "process"}
                        for p in (snapshot().get("factory_procs") or [])]
            return self._send(200, json.dumps([f for f in feeds if f["state"] == "Running"] + rows + [f for f in feeds if f["state"] != "Running"]))
        if u.path == "/api/ledger":
            return self._send(200, json.dumps(ledger()))
        if u.path == "/api/frames":
            return self._send(200, json.dumps({"dir": FRAME.get("dir"), "frames": recent_frames(int(parse_qs(u.query).get("n", ["12"])[0]))}))
        if u.path == "/frame":
            name = unquote(parse_qs(u.query).get("name", [""])[0])
            if name:                                   # a frame from the current render folder only, by bare file name
                p = os.path.join(FRAME.get("dir") or "", name)
                if os.path.basename(name) == name and name.lower().endswith(IMG) and FRAME.get("dir") and os.path.isfile(p):
                    return self._img(p)
                return self._send(404, "{}")
            if FRAME["path"] and os.path.exists(FRAME["path"]):
                return self._img(FRAME["path"])
            return self._send(404, "{}")
        if u.path == "/api/stream":
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store"); self.end_headers()
            CLIENTS[0] += 1
            try:
                while True:
                    self.wfile.write(("data: " + json.dumps(snapshot()) + "\n\n").encode("utf-8")); self.wfile.flush()
                    time.sleep(2)
            except Exception:
                return
            finally:
                CLIENTS[0] = max(0, CLIENTS[0] - 1)
        return self._send(404, "{}")

    def do_POST(self):
        u = urlparse(self.path)
        if self.headers.get("Origin") not in (None, "http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT):
            return self._send(403, '{"error":"wrong origin"}')
        if u.path == "/api/feed":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 65536:
                    return self._send(413, '{"error":"too big"}')
                return self._send(200, json.dumps(write_feed(json.loads(self.rfile.read(n) or b"{}"))))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        if u.path == "/api/game/on":
            return self._send(200, json.dumps(game_on("manual")))
        if u.path == "/api/game/off":
            return self._send(200, json.dumps(game_off("manual")))
        if u.path == "/api/game/free-gpu":
            return self._send(200, json.dumps(free_gpu()))
        if u.path == "/api/game/auto":
            q = parse_qs(u.query); st = _state(); st["auto"] = q.get("on", ["1"])[0] == "1"; _note(st, "auto game mode %s" % ("on" if st["auto"] else "off")); _save(st)
            return self._send(200, json.dumps(st))
        return self._send(404, "{}")


PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else CFG["port"]


def serve(port=None, host=None):
    """run the console (blocks). Used by `python server.py` and, when no console is running yet, by shell.py in a thread."""
    global PORT
    PORT = port or PORT

    class Srv(ThreadingHTTPServer):
        allow_reuse_address = False     # on Windows SO_REUSEADDR lets a 2nd console bind the same port: two game watchers would
        daemon_threads = True           # suspend each job twice and resume it once, leaving it frozen. One console only.
    try:
        httpd = Srv((host or CFG["host"], PORT), H)
    except OSError as e:
        print("port %d is taken (another console already runs?): %s" % (PORT, e), flush=True)
        return False
    _save(_state())
    for target in (auto_loop, sampler_loop, job_loop):
        threading.Thread(target=target, daemon=True).start()
    print("%s Console %s on http://127.0.0.1:%d" % (CFG["title"], VERSION, PORT), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
