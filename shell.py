"""Factory Console - desktop drop-down (v2, 2026-10-22).

A thin pill sits at the top centre of the screen. Push the mouse against the top edge on the pill and the console slides
down; move the mouse away and it tucks back up. Ctrl+Alt+F (config: shell.hotkey) opens or closes it from anywhere.
Inside the panel: P pins it open, Esc tucks it, Shift+Esc quits the shell.
It hides completely while a full-screen app has focus or Game Mode is on, so it never sits over a game.

Needs pywebview (WebView2, built into Windows 11). Starts the console server in-process when none is running yet.
Run: pythonw shell.py   (install.ps1 puts a shortcut in the Startup folder)
"""
import ctypes, ctypes.wintypes as wt, json, os, socket, sys, threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LOG = os.path.join(HERE, "shell.log")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))      # per-monitor v2: every coordinate below is a real pixel
except Exception:
    pass

# one shell per user session
_mutex = k32.CreateMutexW(None, False, "Local\\FactoryConsoleShell")
if ctypes.get_last_error() == 183:                                  # ERROR_ALREADY_EXISTS
    sys.exit(0)

import server                                                       # config + the console itself (stdlib only)
import webview

CFG = server.CFG
SH = CFG["shell"]
PORT = CFG["port"]
URL = "http://127.0.0.1:%d/?panel=1" % PORT
TITLE = "%s Console Panel" % CFG["title"]
PEEK = 10                                                           # px of the panel left on screen when tucked (the pill)

HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]
gdi32.CreateRoundRectRgn.restype = wt.HRGN


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


S = {"hwnd": None, "mode": "start", "pinned": False, "hot_since": None, "away_since": None, "entered": True, "quit": False,
     "x": 0, "w": 0, "h": 0, "busy": False}


def server_up():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_server():
    """the console server must run: start the scheduled task named in shell.server_task, else run it inside this process"""
    if server_up():
        return True
    task = (SH.get("server_task") or "").strip()
    if task:
        folder, name = task.rsplit("\\", 1)
        server.ps("Start-ScheduledTask -TaskPath '%s\\' -TaskName '%s'" % (folder.rstrip("\\") or "", name), 30)
    else:
        threading.Thread(target=server.serve, daemon=True).start()
    for _ in range(60):
        if server_up():
            log("console server started (%s)" % (task or "in this process")); return True
        time.sleep(0.25)
    log("console server did not come up"); return False


def primary_monitor():
    hmon = user32.MonitorFromPoint(wt.POINT(0, 0), 1)
    mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    return mi.rcMonitor, hmon


def scale_for(hwnd):
    try:
        return user32.GetDpiForWindow(hwnd) / 96.0 or 1.0
    except Exception:
        return 1.0


def fullscreen_foreground():
    fg = user32.GetForegroundWindow()
    if not fg or fg == S["hwnd"]:
        return False
    cls = ctypes.create_unicode_buffer(64); user32.GetClassNameW(fg, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return False
    if user32.IsZoomed(fg):        # a maximized window also covers the screen when the taskbar auto-hides; games are not "zoomed"
        return False
    r = wt.RECT(); user32.GetWindowRect(fg, ctypes.byref(r))
    mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(fg, 2), ctypes.byref(mi))
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


def game_mode_on():
    try:
        return bool(json.load(open(os.path.join(HERE, "state.json"), encoding="utf-8")).get("game_mode"))
    except Exception:
        return False


def place(y, region):
    h = S["hwnd"]
    user32.SetWindowPos(h, HWND_TOPMOST, S["x"], int(round(y)), 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
    if region == "pill":
        pw = int(SH["pill_width"] * S["scale"]) + int(28 * S["scale"])
        x0 = (S["w"] - pw) // 2
        user32.SetWindowRgn(h, gdi32.CreateRoundRectRgn(x0, S["h"] - int(PEEK * S["scale"]), x0 + pw, S["h"] + 1, int(10 * S["scale"]), int(10 * S["scale"])), True)
    elif region == "full":
        user32.SetWindowRgn(h, None, True)


def animate(y0, y1, ms, ease, region_end):
    S["busy"] = True
    try:
        t0 = time.perf_counter()
        while True:
            k = min(1.0, (time.perf_counter() - t0) * 1000.0 / ms)
            place(y0 + (y1 - y0) * ease(k), None)
            if k >= 1.0:
                break
            time.sleep(0.007)
        place(y1, region_end)
    finally:
        S["busy"] = False


ease_out = lambda k: 1 - (1 - k) ** 3          # arrives fast, settles soft (opening follows the hand)
ease_in = lambda k: k * k                      # leaves gently, then gets out of the way


def tucked_y():
    return S["top"] - S["h"] + int(PEEK * S["scale"])


def reveal(by_hotkey=False):
    if S["mode"] == "open":
        return
    place(tucked_y(), "full")
    animate(tucked_y(), S["top"], 170, ease_out, "full")
    S.update(mode="open", away_since=None, entered=not by_hotkey)


def tuck():
    if S["mode"] != "open":
        return
    animate(S["top"], tucked_y(), 150, ease_in, "pill")
    S.update(mode="tucked", hot_since=None, pinned=False)
    try:
        S["window"].evaluate_js("document.getElementById('pin') && document.getElementById('pin').setAttribute('aria-pressed','false')")
    except Exception:
        pass


def hide_all():
    if S["mode"] != "hidden":
        user32.ShowWindow(S["hwnd"], SW_HIDE)
        S["mode"] = "hidden"


def unhide():
    if S["mode"] == "hidden":
        place(tucked_y(), "pill")
        user32.ShowWindow(S["hwnd"], SW_SHOWNOACTIVATE)
        S["mode"] = "tucked"


def watch():
    """30 ms loop: the hot edge, the leave timer, and the full-screen / Game Mode guard"""
    pt = wt.POINT()
    last_guard = 0; guard = False; last_srv = time.time()
    while not S["quit"]:
        try:
            now = time.time()
            if now - last_srv > 30:
                last_srv = now
                if not server_up():
                    threading.Thread(target=ensure_server, daemon=True).start()
            if now - last_guard > 0.5:
                guard = fullscreen_foreground() or game_mode_on()
                last_guard = now
            if guard and not S["pinned"]:
                if S["mode"] == "open":
                    tuck()
                hide_all()
            elif S["mode"] == "hidden":
                unhide()
            user32.GetCursorPos(ctypes.byref(pt))
            if S["mode"] == "tucked" and not S["busy"]:
                cx = S["x"] + S["w"] // 2; half = int((SH["pill_width"] / 2 + 60) * S["scale"])
                if pt.y <= S["top"] + 1 and abs(pt.x - cx) <= half:
                    S["hot_since"] = S["hot_since"] or now
                    if (now - S["hot_since"]) * 1000 >= SH["dwell_ms"]:
                        reveal()
                else:
                    S["hot_since"] = None
            elif S["mode"] == "open" and not S["busy"] and not S["pinned"]:
                m = int(10 * S["scale"])
                inside = S["x"] - m <= pt.x <= S["x"] + S["w"] + m and pt.y <= S["top"] + S["h"] + m
                if inside:
                    S["entered"] = True; S["away_since"] = None
                elif S["entered"]:
                    S["away_since"] = S["away_since"] or now
                    if (now - S["away_since"]) * 1000 >= SH["hide_after_ms"]:
                        tuck()
        except Exception:
            log("watch: " + traceback.format_exc())
            time.sleep(1)
        time.sleep(0.03)


def parse_hotkey(s):
    mods, vk = MOD_NOREPEAT, 0
    for part in (s or "").lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"): mods |= MOD_CONTROL
        elif part == "alt": mods |= MOD_ALT
        elif part == "shift": mods |= MOD_SHIFT
        elif part in ("win", "super"): mods |= MOD_WIN
        elif len(part) == 1: vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit(): vk = 0x6F + int(part[1:])
    return mods, vk


def hotkey_loop():
    mods, vk = parse_hotkey(SH.get("hotkey"))
    if not vk or not user32.RegisterHotKey(None, 1, mods, vk):
        log("hotkey %r not registered (taken by another app?)" % SH.get("hotkey")); return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            try:
                if S["mode"] == "open":
                    tuck()
                else:
                    if S["mode"] == "hidden":
                        S["pinned"] = True; unhide()
                    reveal(by_hotkey=True)
            except Exception:
                log("hotkey: " + traceback.format_exc())


class Api:
    def pin(self, on):
        S["pinned"] = bool(on); S["away_since"] = None
        return S["pinned"]

    def tuck(self):
        threading.Thread(target=tuck, daemon=True).start()

    def quit(self):
        S["quit"] = True
        threading.Thread(target=lambda: (time.sleep(0.05), S["window"].destroy()), daemon=True).start()

    def state(self, cls):
        return True

    def config(self):
        return {"pill_width": SH["pill_width"]}


def start(window):
    """runs once the GUI loop is up: find the window, make it a borderless tool window on top, tuck it to the pill"""
    for _ in range(200):
        h = user32.FindWindowW(None, TITLE)
        if h:
            break
        time.sleep(0.05)
    if not h:
        log("panel window not found"); return
    S["hwnd"] = h; S["window"] = window
    S["scale"] = scale_for(h)
    mon, _ = primary_monitor()
    S["w"] = int(SH["width"] * S["scale"]); S["h"] = int(SH["height"] * S["scale"])
    S["x"] = mon.left + (mon.right - mon.left - S["w"]) // 2; S["top"] = mon.top
    user32.ShowWindow(h, SW_HIDE)
    ex = user32.GetWindowLongPtrW(h, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(h, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)     # no taskbar button, not in Alt+Tab
    try:
        c = ctypes.c_int(2); ctypes.windll.dwmapi.DwmSetWindowAttribute(h, 33, ctypes.byref(c), 4)   # rounded corners (Win 11)
    except Exception:
        pass
    user32.SetWindowPos(h, HWND_TOPMOST, S["x"], tucked_y(), S["w"], S["h"], SWP_NOACTIVATE)
    place(tucked_y(), "pill")
    user32.ShowWindow(h, SW_SHOWNOACTIVATE)
    S["mode"] = "tucked"
    if "--open" in sys.argv:
        reveal(by_hotkey=True); S["pinned"] = True
    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=hotkey_loop, daemon=True).start()
    log("shell up: %dx%d at x=%d, scale %.2f, hotkey %s" % (S["w"], S["h"], S["x"], S["scale"], SH.get("hotkey")))


if __name__ == "__main__":
    try:
        ensure_server()
        w = webview.create_window(TITLE, URL, width=SH["width"], height=SH["height"], x=-20000, y=-20000, frameless=True, on_top=True,
                                  resizable=False, background_color="#0A0B0D", js_api=Api(), focus=False)
        webview.start(start, w, private_mode=False, storage_path=os.path.join(HERE, ".webview"))
    except Exception:
        log("fatal: " + traceback.format_exc())
        raise
