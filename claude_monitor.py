"""
Claude Code Monitor - a always-on-top desktop indicator that tracks every running
Claude Code conversation and shows, at a glance, which ones are working and which
ones are done and waiting for you.

Data sources (all local, read-only):
  ~/.claude/sessions/<pid>.json          live registry, one file per running process
  ~/.claude/projects/<slug>/<sid>.jsonl  the conversation transcript

State detection walks the tail of the transcript backwards to the first decisive
event:
  system/turn_duration  -> the turn ended        -> DONE  (green)
  assistant / user      -> mid-turn              -> WORKING (blue)
  working + no writes for a while                -> WAITING (amber, may need you)

Run:  pythonw claude_monitor.py       (no console)
      python  claude_monitor.py --probe   (one-shot text dump, for debugging)
"""

import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import sys
import time
import glob

HOME = os.path.expanduser("~")
SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
CONFIG_PATH = os.path.join(HOME, ".claude-monitor.json")

POLL_MS = 1000          # how often we rescan
DRAG_SLOP = 8           # px of movement before a click counts as a drag
WAITING_AFTER = 180     # seconds blocked on a tool before we suspect a prompt
TAIL_SMALL = 256 * 1024
TAIL_BIG = 4 * 1024 * 1024

# ---------------------------------------------------------------- palette
BG = "#1E1D1B"
BG_HOVER = "#2A2825"
BORDER = "#3B3835"
FG = "#EDEAE5"
FG_DIM = "#8E877F"
ORANGE = "#D97757"
GREEN = "#5FCB8B"
BLUE = "#6BA8E5"
VIOLET = "#A78BFA"
AMBER = "#E5B04B"
GREY = "#6A645E"
CHROMA = "#FF00FE"      # transparency key colour

STATE_COLOR = {
    "done": GREEN,
    "thinking": VIOLET,
    "working": BLUE,
    "waiting": AMBER,
    "starting": GREY,
    "seen": GREY,
}
STATE_LABEL = {
    "done": "ready for you",
    "thinking": "thinking",
    "working": "working",
    "waiting": "may need input",
    "starting": "starting",
    "seen": "done",
}
# short forms for the header tally, kept distinct so "done" and "seen" don't
# both print as the same word
SUMMARY_LABEL = {
    "done": "done", "waiting": "check", "working": "working",
    "thinking": "thinking", "seen": "seen", "starting": "starting",
}

# ---------------------------------------------------------------- win32
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def process_snapshot():
    """pid -> (parent pid, exe name) for every live process, in one pass.

    One snapshot per scan answers liveness, identity and parentage at once,
    which is far cheaper than querying each pid separately.
    """
    out = {}
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return out
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID,
                                    e.szExeFile.decode("mbcs", "replace"))
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def top_level_windows_by_pid():
    """Map pid -> [(hwnd, title), ...] for visible, un-owned, titled windows.

    A pid can own several windows - Windows Terminal is a single process
    hosting every terminal window you have open - so this has to be a list.
    Collapsing it to one hwnd makes every session in that terminal focus the
    same window.
    """
    out = {}
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        if u32.GetWindow(hwnd, 4):          # GW_OWNER -> it is a dialog/child
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.setdefault(pid.value, []).append((hwnd, buf.value))
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def resolve_windows(pid, procs, wins):
    """Candidate windows hosting this Claude process, nearest ancestor first.

    A CLI session owns no window - the terminal that spawned it does - so we
    walk up the process tree until we find one. Coming back empty is itself
    meaningful: nothing on screen is showing this session, i.e. it is a
    detached background job rather than something you have open.
    """
    cur = pid
    for _ in range(8):
        if cur in wins:
            return wins[cur]
        nxt = procs.get(cur, (0, ""))[0]
        if not nxt or nxt == cur:
            return []
        cur = nxt
    return []


def _norm(text):
    """Strip the leading status glyph Claude Code puts in the title (✳, or a
    braille spinner frame while busy) so titles compare cleanly."""
    text = (text or "").strip()
    while text and not text[0].isalnum():
        text = text[1:].lstrip()
    return text.casefold()


def window_score(item, title):
    """How strongly a window title identifies this session."""
    t = _norm(title)
    if not t:
        return 0
    for cand, pts in ((item["title"], 4), (item["name"], 2)):
        c = _norm(str(cand or ""))
        if not c:
            continue
        if t == c:
            return pts
        if c in t or t in c:
            return pts - 1
    return 0


def assign_windows(items):
    """Give each session its own window, best title match first.

    Terminal windows show the *active* tab's title, so a session sitting in a
    background tab won't match anything - it just falls back to a spare window
    of its host process.
    """
    pairs = []
    for i, item in enumerate(items):
        for hwnd, title in item["_cands"]:
            pairs.append((window_score(item, title), i, hwnd))
    pairs.sort(key=lambda p: -p[0])

    chosen, used = {}, set()
    for score, i, hwnd in pairs:
        if score <= 0 or i in chosen or hwnd in used:
            continue
        chosen[i] = hwnd
        used.add(hwnd)

    for i, item in enumerate(items):
        if i not in chosen:
            spare = [h for h, _ in item["_cands"] if h not in used]
            if spare:
                chosen[i] = spare[0]
                used.add(spare[0])
            elif item["_cands"]:
                chosen[i] = item["_cands"][0][0]
        item["hwnd"] = chosen.get(i)
        del item["_cands"]


_instance_lock = None


def claim_single_instance():
    """False if another copy is already running, so launching twice is safe."""
    global _instance_lock
    k32.CreateMutexW.restype = wt.HANDLE
    _instance_lock = k32.CreateMutexW(None, False, "Local\\ClaudeCodeMonitor")
    return ctypes.get_last_error() != 183       # ERROR_ALREADY_EXISTS


def foreground_window():
    return u32.GetForegroundWindow()


def window_title(hwnd):
    if not hwnd:
        return ""
    n = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def focus_window(hwnd):
    if not hwnd:
        return False
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)             # SW_RESTORE
    # Windows blocks SetForegroundWindow unless the caller "owns" the input;
    # a synthetic ALT tap releases that lock.
    u32.keybd_event(0x12, 0, 0, 0)
    u32.keybd_event(0x12, 0, 2, 0)
    u32.SetForegroundWindow(hwnd)
    return True


# ---------------------------------------------------------------- scanning
_transcript_cache = {}      # path -> (size, mtime, parsed)
_path_cache = {}            # session id -> transcript path


def transcript_path(session_id):
    p = _path_cache.get(session_id)
    if p and os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl"))
    if not hits:
        return None
    _path_cache[session_id] = hits[0]
    return hits[0]


def _read_tail(path, nbytes):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        start = max(0, size - nbytes)
        f.seek(start)
        raw = f.read()
    text = raw.decode("utf-8", "replace")
    lines = text.split("\n")
    if start > 0:
        lines = lines[1:]           # first line is almost certainly truncated
    return lines


# entry types that carry no state information
NOISE = {
    "file-history-snapshot", "file-history-delta", "queue-operation",
    "attachment", "mode", "summary",
}


def parse_transcript(path):
    """Walk the tail backwards and work out what this conversation is doing."""
    result = {
        "state": None, "title": None, "prompt": None,
        "permission_mode": None, "tool": None, "last_ts": None,
        "turn_ms": None,
    }
    for nbytes in (TAIL_SMALL, TAIL_BIG):
        objs = []
        for line in _read_tail(path, nbytes):
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                objs.append(json.loads(line))
            except ValueError:
                continue

        # metadata entries are rewritten near the end of the file, so a forward
        # pass picks up the freshest values
        for o in objs:
            t = o.get("type")
            if t == "ai-title" and o.get("aiTitle"):
                result["title"] = o["aiTitle"]
            elif t == "last-prompt" and o.get("lastPrompt"):
                result["prompt"] = o["lastPrompt"]
            elif t == "permission-mode":
                result["permission_mode"] = o.get("permissionMode")

        for o in reversed(objs):
            t = o.get("type")
            if t in NOISE or o.get("isSidechain") or o.get("isMeta"):
                continue

            if t == "system":
                sub = o.get("subtype")
                if sub == "turn_duration":
                    result["state"] = "done"
                    result["turn_ms"] = o.get("durationMs")
                    result["last_ts"] = o.get("timestamp")
                    return result
                if sub == "stop_hook_summary":
                    result["state"] = "done"
                    result["last_ts"] = o.get("timestamp")
                    return result
                continue        # other system notes are mid-turn chatter

            if t == "assistant":
                result["last_ts"] = o.get("timestamp")
                msg = o.get("message") or {}
                content = msg.get("content") or []
                if isinstance(content, list):
                    for b in reversed(content):
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            result["tool"] = b.get("name")
                            break

                # the API's own stop_reason is the authoritative end-of-turn
                # marker, and unlike turn_duration it is written by every
                # entrypoint. "tool_use" means more is coming; anything else
                # means the model handed control back to you.
                stop = msg.get("stop_reason")
                if stop and stop != "tool_use":
                    result["state"] = "done"
                elif result["tool"]:
                    result["state"] = "working"
                else:
                    # mid-turn prose or a thinking block - more is coming
                    result["state"] = "thinking"
                return result

            if t == "user":
                # either a fresh prompt or a returning tool result - either way
                # the ball is back in the model's court
                result["state"] = "thinking"
                result["last_ts"] = o.get("timestamp")
                return result

        # nothing decisive in this window - widen it once, then give up
        if os.path.getsize(path) <= nbytes:
            break
    return result


def read_transcript(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime)
    cached = _transcript_cache.get(path)
    if cached and cached[0] == key:
        return cached[1], st.st_mtime
    try:
        parsed = parse_transcript(path)
    except OSError:
        return None
    _transcript_cache[path] = (key, parsed)
    return parsed, st.st_mtime


def scan(include_detached=False):
    """Return one dict per Claude Code conversation you currently have open.

    "Open" means the process is alive *and* something on screen is showing it.
    Detached background jobs own no window and are left out unless asked for.
    """
    out = []
    try:
        files = os.listdir(SESSIONS_DIR)
    except OSError:
        return out

    now = time.time()
    procs = process_snapshot()
    wins = top_level_windows_by_pid()
    for name in files:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, name), "r", encoding="utf-8") as f:
                reg = json.load(f)
        except (OSError, ValueError):
            continue

        pid = reg.get("pid")
        sid = reg.get("sessionId")
        if not pid or not sid:
            continue
        # a dead pid leaves its file behind, and pids get recycled - so require
        # both "alive" and "still a claude process". An update renames the
        # running binary to claude.exe.old.<ts>, hence the prefix match.
        info = procs.get(pid)
        if not info or not info[1].lower().startswith("claude.exe"):
            continue

        cands = resolve_windows(pid, procs, wins)
        if not cands and not include_detached:
            continue

        cwd = reg.get("cwd") or ""
        item = {
            "pid": pid,
            "hwnd": None,
            "_cands": cands,
            "session_id": sid,
            "cwd": cwd,
            "folder": os.path.basename(cwd.rstrip("\\/")) or cwd,
            "name": reg.get("name") or sid[:8],
            "kind": reg.get("kind") or "interactive",
            "entrypoint": reg.get("entrypoint") or "",
            # CLI sessions publish busy/idle/shell here; desktop ones omit it
            "status": reg.get("status"),
            "title": None,
            "prompt": None,
            "tool": None,
            "turn_ms": None,
            "permission_mode": None,
            "state": "starting",
            "since": 0.0,
            "mtime": 0.0,
            "event": None,
        }

        path = transcript_path(sid)
        got = read_transcript(path) if path else None
        if got:
            parsed, mtime = got
            item.update({k: parsed[k] for k in
                         ("title", "prompt", "tool", "turn_ms", "permission_mode")})
            if parsed["state"]:
                item["state"] = parsed["state"]
            # age off the event's own timestamp, not the file's mtime - the
            # transcript gets rewritten with metadata (titles, queue entries)
            # long after the last real event, which made mtime read as "4s ago"
            # on a session that had been idle for minutes
            item["mtime"] = mtime
            event = ts_epoch(parsed["last_ts"])
            item["event"] = event
            item["since"] = max(0.0, now - (event if event else mtime))
            # the registry's own status beats anything inferred from the
            # transcript: "busy" means the session really is working, however
            # long it has been quiet
            if item["status"] == "idle" and item["state"] in ("thinking", "working"):
                item["state"] = "done"

            # a long silence *while blocked on a tool* can mean a permission
            # prompt is sitting there. Only guess that when nothing contradicts
            # it: not while the registry says busy, and never in a mode where
            # prompts cannot happen at all.
            elif (item["state"] == "working"
                    and item["status"] != "busy"
                    and item["permission_mode"] != "bypassPermissions"
                    and item["since"] > WAITING_AFTER):
                item["state"] = "waiting"
        out.append(item)

    assign_windows(out)
    out.sort(key=recency, reverse=True)
    return out


def recency(sess):
    """Most recent real activity, for ordering the list."""
    return sess["event"] or sess["mtime"] or 0.0


def looking_at(sessions, fg=None, title=None):
    """Which sessions the focused window means you are currently looking at.

    Several sessions can share one terminal window, so focus alone isn't
    enough - the window title says which tab is on top, and that's the one
    you're looking at. When the title names none of them, only a window
    hosting a single session can be attributed confidently.
    """
    fg = foreground_window() if fg is None else fg
    if not fg:
        return []
    here = [s for s in sessions if s["hwnd"] and s["hwnd"] == fg]
    if len(here) <= 1:
        return here

    title = window_title(fg) if title is None else title
    ranked = sorted(here, key=lambda s: window_score(s, title), reverse=True)
    return ranked[:1] if window_score(ranked[0], title) > 0 else []


def ack_key(sess):
    """Identifies *which* finished turn you've looked at. Uses the event stamp
    so unrelated rewrites of the transcript don't re-raise the alert."""
    return sess["event"] or sess["mtime"]


def ts_epoch(text):
    """Parse a transcript ISO-8601 UTC stamp into epoch seconds."""
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(
            text.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def fmt_age(sec):
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm" % (sec // 60)
    return "%dh%dm" % (sec // 3600, (sec % 3600) // 60)


# ---------------------------------------------------------------- config
def load_config():
    cfg = {"rx": None, "y": 0, "pinned": False, "sound": True,
           "show_detached": False}
    try:
        # utf-8-sig: tolerate a BOM, which several Windows editors (and
        # PowerShell's Set-Content) add. Without it the whole config is
        # silently discarded and settings appear not to stick.
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


# ---------------------------------------------------------------- probe mode
def probe():
    rows = scan(include_detached="--all" in sys.argv)
    if not rows:
        print("no open Claude Code sessions found")
    for s in rows:
        print("[%-8s] %-22s pid=%-6s %s" %
              (s["state"], s["name"], s["pid"], s["folder"]))
        print("           %s" % (s["title"] or "(untitled)"))
        print("           idle %s  tool=%s  window=%s  kind=%s" %
              (fmt_age(s["since"]), s["tool"],
               s["hwnd"] or "none (detached)", s["kind"]))
        if s["hwnd"]:
            print("           -> %r" % window_title(s["hwnd"]))
    return 0


# ---------------------------------------------------------------- UI
def run_gui():
    import tkinter as tk
    import winsound

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    SCALE = dpi / 96.0

    def s(v):
        return int(round(v * SCALE))

    F_TITLE = ("Segoe UI Semibold", -s(12))
    F_BODY = ("Segoe UI", -s(10))
    F_SMALL = ("Segoe UI", -s(9))

    PILL_H = s(34)
    ROW_H = s(46)
    HEAD_H = s(30)
    PANEL_W = s(330)
    PAD = s(10)

    cfg = load_config()

    root = tk.Tk()
    root.withdraw()
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-transparentcolor", CHROMA)
    win.configure(bg=CHROMA)

    canvas = tk.Canvas(win, bg=CHROMA, highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)

    state = {
        "sessions": [],
        "expanded": False,
        "pinned": bool(cfg.get("pinned")),
        "sound": bool(cfg.get("sound", True)),
        "show_detached": bool(cfg.get("show_detached", False)),
        "hover_row": -1,
        "hover_close": False,
        "close_hit": None,
        "prev": {},             # session id -> last seen state
        "ack": {},              # session id -> mtime of the turn you've looked at
        "first": True,
        "flash_until": 0.0,
        "drag": None,
        "moved": False,
        "collapse_job": None,
        "row_hits": [],
        "w": s(96),
        "h": PILL_H,
        "left": 0,
    }

    # ---- drawing helpers -------------------------------------------------
    def round_rect(x0, y0, x1, y1, r, **kw):
        pts = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1,
            x0 + r, y1, x0, y1, x0, y1 - r,
            x0, y0 + r, x0, y0,
        ]
        return canvas.create_polygon(pts, smooth=True, **kw)

    def draw_logo(cx, cy, r, color):
        """A ten-spoke sunburst, in the spirit of the Claude mark."""
        import math
        for i in range(10):
            a = math.pi * 2 * i / 10.0
            inner = r * 0.30
            canvas.create_line(
                cx + math.cos(a) * inner, cy + math.sin(a) * inner,
                cx + math.cos(a) * r, cy + math.sin(a) * r,
                fill=color, width=max(1, s(2)), capstyle="round")

    def place(w, h):
        # cfg["rx"] is the RIGHT edge, so the panel grows leftwards when it
        # expands instead of running off the side of the screen
        state["w"], state["h"] = w, h
        canvas.config(width=w, height=h)
        if cfg.get("rx") is None:
            # top right, but below the band where maximised windows put their
            # minimise/close buttons - nobody wants to mis-click that
            cfg["rx"] = win.winfo_screenwidth() - s(12)
            cfg["y"] = s(40)

        # keep it somewhere on the visible desktop, across all monitors, so a
        # stale saved position can never strand it off screen
        vx, vy = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)
        vw, vh = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
        cfg["rx"] = min(max(cfg["rx"], vx + w), vx + vw)
        cfg["y"] = min(max(cfg["y"], vy), vy + vh - h)

        left = cfg["rx"] - w
        state["left"] = left
        win.geometry("%dx%d+%d+%d" % (w, h, left, cfg["y"]))

    # ---- render ----------------------------------------------------------
    def render():
        canvas.delete("all")
        sessions = state["sessions"]
        counts = {}
        for sess in sessions:
            counts[sess["ui"]] = counts.get(sess["ui"], 0) + 1
        any_done = counts.get("done", 0) > 0

        if state["expanded"]:
            render_panel(sessions, counts, any_done)
        else:
            render_pill(sessions, counts, any_done)

    def render_pill(sessions, counts, any_done):
        state["close_hit"] = None       # no quit button while collapsed
        n = len(sessions)
        dots = min(n, 6)
        w = s(30) + (dots * s(13) if dots else s(18)) + s(10)
        h = PILL_H
        place(w, h)

        flashing = any_done and (int(time.time() * 2) % 2 == 0)
        edge = GREEN if any_done else BORDER
        round_rect(1, 1, w - 2, h - 2, s(9),
                   fill=BG, outline=edge, width=s(2) if any_done else 1)
        if flashing:
            round_rect(1, 1, w - 2, h - 2, s(9), fill="", outline=GREEN, width=s(2))

        draw_logo(s(17), h / 2, s(8), ORANGE if n else GREY)

        if not n:
            canvas.create_text(s(31), h / 2, anchor="w", text="idle",
                               fill=FG_DIM, font=F_SMALL)
            return

        x = s(32)
        for sess in sessions[:6]:
            c = STATE_COLOR.get(sess["ui"], GREY)
            r = s(4)
            canvas.create_oval(x, h / 2 - r, x + 2 * r, h / 2 + r,
                               fill=c, outline="")
            x += s(13)
        if n > 6:
            canvas.create_text(x, h / 2, anchor="w", text="+%d" % (n - 6),
                               fill=FG_DIM, font=F_SMALL)

    def render_panel(sessions, counts, any_done):
        n = max(len(sessions), 1)
        w = PANEL_W
        h = HEAD_H + n * ROW_H + s(8)
        place(w, h)

        round_rect(1, 1, w - 2, h - 2, s(11), fill=BG,
                   outline=GREEN if any_done else BORDER,
                   width=s(2) if any_done else 1)

        draw_logo(s(18), HEAD_H / 2 + s(2), s(8), ORANGE)
        canvas.create_text(s(32), HEAD_H / 2 + s(2), anchor="w",
                           text="Claude Code", fill=FG, font=F_TITLE)
        summary = "  ".join(
            "%d %s" % (counts[k], SUMMARY_LABEL[k])
            for k in ("done", "waiting", "working", "thinking", "seen",
                      "starting")
            if counts.get(k))
        canvas.create_text(w - s(30), HEAD_H / 2 + s(2), anchor="e",
                           text=summary or "no sessions",
                           fill=FG_DIM, font=F_SMALL)

        # quit button
        cx, cy, r = w - s(16), HEAD_H / 2 + s(2), s(4)
        hot = state["hover_close"]
        if hot:
            canvas.create_oval(cx - s(10), cy - s(10), cx + s(10), cy + s(10),
                               fill="#4A2420", outline="")
        for ax, ay in ((-r, -r), (-r, r)):
            canvas.create_line(cx + ax, cy + ay, cx - ax, cy - ay,
                               fill=ORANGE if hot else FG_DIM,
                               width=max(1, s(1.5)), capstyle="round")
        state["close_hit"] = (w - s(28), 0, w, HEAD_H)

        canvas.create_line(PAD, HEAD_H, w - PAD, HEAD_H, fill=BORDER)

        state["row_hits"] = []
        if not sessions:
            canvas.create_text(w / 2, HEAD_H + ROW_H / 2,
                               text="no Claude Code sessions running",
                               fill=FG_DIM, font=F_BODY)
            return

        y = HEAD_H + s(4)
        for i, sess in enumerate(sessions):
            ui = sess["ui"]
            colour = STATE_COLOR.get(ui, GREY)
            if state["hover_row"] == i:
                round_rect(s(5), y, w - s(5), y + ROW_H - s(2), s(7),
                           fill=BG_HOVER, outline="")

            # status dot, pulsing while the session is actively busy
            r = s(4.5)
            cy = y + ROW_H / 2 - s(1)
            if ui in ("working", "thinking") and int(time.time() * 2) % 2 == 0:
                canvas.create_oval(s(12) - r - s(3), cy - r - s(3),
                                   s(12) + r + s(3), cy + r + s(3),
                                   fill="", outline=colour)
            canvas.create_oval(s(12) - r, cy - r, s(12) + r, cy + r,
                               fill=colour, outline="")

            title = sess["title"] or sess["name"]
            canvas.create_text(s(26), y + s(13), anchor="w",
                               text=clip(title, 34), fill=FG, font=F_TITLE)

            label = STATE_LABEL.get(ui, ui)
            if ui == "working" and sess["tool"]:
                label = "running %s" % sess["tool"]
            line = "%s · %s ago" % (label, fmt_age(sess["since"]))
            canvas.create_text(s(26), y + s(30), anchor="w",
                               text=clip(line, 34),
                               fill=colour if ui in ("done", "waiting") else FG_DIM,
                               font=F_SMALL)

            tag = sess["folder"]
            if sess["kind"] == "bg":
                tag = "bg · " + tag
            canvas.create_text(w - s(12), y + s(30), anchor="e",
                               text=clip(tag, 20), fill=FG_DIM, font=F_SMALL)

            state["row_hits"].append((y, y + ROW_H, sess))
            y += ROW_H

    def clip(text, n):
        text = " ".join(str(text).split())
        return text if len(text) <= n else text[:n - 1] + "…"

    # ---- interaction -----------------------------------------------------
    def cancel_collapse():
        if state["collapse_job"]:
            win.after_cancel(state["collapse_job"])
            state["collapse_job"] = None

    def on_enter(_):
        cancel_collapse()
        if not state["expanded"]:
            state["expanded"] = True
            render()

    def on_leave(_):
        if state["pinned"]:
            return
        cancel_collapse()

        def collapse():
            state["collapse_job"] = None
            state["expanded"] = False
            state["hover_row"] = -1
            render()
        state["collapse_job"] = win.after(350, collapse)

    def in_close(e):
        box = state["close_hit"]
        return bool(box and box[0] <= e.x <= box[2] and box[1] <= e.y <= box[3])

    def on_motion(e):
        if not state["expanded"]:
            return
        hit = -1
        for i, (y0, y1, _) in enumerate(state["row_hits"]):
            if y0 <= e.y < y1:
                hit = i
                break
        close = in_close(e)
        if hit != state["hover_row"] or close != state["hover_close"]:
            state["hover_row"] = hit
            state["hover_close"] = close
            render()

    def on_press(e):
        state["drag"] = (e.x_root - state["left"], e.y_root - cfg["y"])
        state["moved"] = False

    def on_drag(e):
        if not state["drag"]:
            return
        dx, dy = state["drag"]
        nl, ny = e.x_root - dx, e.y_root - dy
        # generous threshold: a click on a row usually carries a few pixels of
        # jitter, and treating that as a drag silently relocates the widget
        if not state["moved"]:
            if abs(nl - state["left"]) < DRAG_SLOP and abs(ny - cfg["y"]) < DRAG_SLOP:
                return
            state["moved"] = True
        state["left"], cfg["y"] = nl, ny
        cfg["rx"] = nl + state["w"]
        win.geometry("%dx%d+%d+%d" % (state["w"], state["h"], nl, ny))

    def on_release(e):
        was_drag = state["moved"]
        state["drag"] = None
        state["moved"] = False
        if was_drag:
            save_config(cfg)
            return
        if not state["expanded"]:
            state["expanded"] = True
            render()
            return
        if in_close(e):
            root.destroy()
            return
        for y0, y1, sess in state["row_hits"]:
            if y0 <= e.y < y1:
                # you're going to look at it now, so stop nagging about it
                state["ack"][sess["session_id"]] = ack_key(sess)
                sess["ui"] = "seen" if sess["state"] == "done" else sess["state"]
                focus_window(sess["hwnd"])
                render()
                return

    menu = tk.Menu(win, tearoff=0, bg=BG, fg=FG,
                   activebackground=ORANGE, activeforeground="#FFFFFF",
                   borderwidth=0)

    def toggle_pin():
        state["pinned"] = not state["pinned"]
        cfg["pinned"] = state["pinned"]
        save_config(cfg)
        if state["pinned"]:
            state["expanded"] = True
        render()

    def toggle_sound():
        state["sound"] = not state["sound"]
        cfg["sound"] = state["sound"]
        save_config(cfg)

    def toggle_detached():
        state["show_detached"] = not state["show_detached"]
        cfg["show_detached"] = state["show_detached"]
        save_config(cfg)

    def reset_pos():
        cfg["rx"] = None
        render()
        save_config(cfg)

    def ack_all():
        for sess in state["sessions"]:
            state["ack"][sess["session_id"]] = ack_key(sess)
            if sess["state"] == "done":
                sess["ui"] = "seen"
        render()

    def on_menu(e):
        menu.delete(0, "end")
        menu.add_command(
            label=("✓ " if state["pinned"] else "   ") + "Keep expanded",
            command=toggle_pin)
        menu.add_command(
            label=("✓ " if state["sound"] else "   ") + "Chime when done",
            command=toggle_sound)
        menu.add_command(
            label=("✓ " if state["show_detached"] else "   ")
                  + "Include detached background jobs",
            command=toggle_detached)
        menu.add_separator()
        menu.add_command(label="   Mark all as seen", command=ack_all)
        menu.add_command(label="   Reset position", command=reset_pos)
        menu.add_command(label="   Quit", command=root.destroy)
        menu.tk_popup(e.x_root, e.y_root)

    for widget in (win, canvas):
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)
        widget.bind("<Motion>", on_motion)
        widget.bind("<ButtonPress-1>", on_press)
        widget.bind("<B1-Motion>", on_drag)
        widget.bind("<ButtonRelease-1>", on_release)
        widget.bind("<Button-3>", on_menu)

    # ---- poll ------------------------------------------------------------
    def tick():
        try:
            sessions = scan(include_detached=state["show_detached"])
        except Exception:
            sessions = state["sessions"]
        # looking at a session's window counts as looking at the session,
        # however you got there - so clear it without needing the widget
        for sess in looking_at(sessions):
            state["ack"][sess["session_id"]] = ack_key(sess)

        live = set()
        for sess in sessions:
            sid = sess["session_id"]
            live.add(sid)

            # anything that finished before the monitor started is already
            # water under the bridge - don't launch into a wall of green
            if state["first"] and sess["state"] == "done":
                state["ack"][sid] = ack_key(sess)

            acked = state["ack"].get(sid) == ack_key(sess)
            sess["ui"] = "seen" if (sess["state"] == "done" and acked) else sess["state"]

            was = state["prev"].get(sid)
            if (was and was != "done" and sess["state"] == "done"
                    and not acked and state["sound"]):
                # no chime if you were already watching it finish
                try:
                    winsound.MessageBeep(0x00000040)   # asterisk
                except Exception:
                    pass
            state["prev"][sid] = sess["state"]

        for d in (state["prev"], state["ack"]):
            for sid in list(d):
                if sid not in live:
                    del d[sid]

        sessions.sort(key=recency, reverse=True)
        state["sessions"] = sessions
        state["first"] = False

        render()
        win.after(POLL_MS, tick)

    if state["pinned"]:
        state["expanded"] = True
    place(s(96), PILL_H)
    tick()
    root.mainloop()


if __name__ == "__main__":
    if "--probe" in sys.argv:
        sys.exit(probe())
    if not claim_single_instance():
        sys.exit(0)         # already up; clicking the icon twice is harmless
    run_gui()
