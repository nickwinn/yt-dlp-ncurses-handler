#!/usr/bin/env python3
"""
ytdlp-gui  —  A curses-based interactive frontend for yt-dlp
"""

import curses
import curses.textpad
import subprocess
import threading
import time
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

# ─────────────────────────── CONSTANTS ────────────────────────────────────────

FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0"
)

COLOR_HEADER   = 1
COLOR_MENU     = 2
COLOR_SELECT   = 3
COLOR_INPUT    = 4
COLOR_STATUS   = 5
COLOR_SPEED    = 6
COLOR_ERROR    = 7
COLOR_SUCCESS  = 8
COLOR_BORDER   = 9
COLOR_DIM      = 10
COLOR_TITLE    = 11

QUALITIES = [
    ("Best available",        "bestvideo+bestaudio/best"),
    ("4K  (2160p)",           "bestvideo[height<=2160]+bestaudio/best[height<=2160]"),
    ("1440p",                 "bestvideo[height<=1440]+bestaudio/best[height<=1440]"),
    ("1080p",                 "bestvideo[height<=1080]+bestaudio/best[height<=1080]"),
    ("720p",                  "bestvideo[height<=720]+bestaudio/best[height<=720]"),
    ("480p",                  "bestvideo[height<=480]+bestaudio/best[height<=480]"),
    ("360p",                  "bestvideo[height<=360]+bestaudio/best[height<=360]"),
    ("Audio only (best)",     "bestaudio/best"),
    ("Audio only (mp3)",      "bestaudio[ext=mp3]/bestaudio"),
]

SPEED_LIMITS = [
    ("Unlimited",   None),
    ("50 MB/s",     "50M"),
    ("20 MB/s",     "20M"),
    ("10 MB/s",     "10M"),
    ("5 MB/s",      "5M"),
    ("2 MB/s",      "2M"),
    ("1 MB/s",      "1M"),
    ("500 KB/s",    "500K"),
    ("200 KB/s",    "200K"),
    ("100 KB/s",    "100K"),
]

PREFS_FILE   = Path.home() / ".config" / "ytdlp-tui" / "prefs.json"
URL_LOG_FILE = Path.home() / ".config" / "ytdlp-tui" / "download_history.log"

# ─────────────────────────── STATE ─────────────────────────────────────────────

class State:
    def __init__(self):
        self.spoof_ua       = True
        self.quality_idx    = 0
        self.speed_idx      = 0
        self.delay_secs     = 0           # free-form seconds between batch downloads
        self.output_name    = ""
        self.affix_source   = True
        self.url_input      = ""
        self.list_path      = ""
        self.download_log   = []          # list of (timestamp, text, color_key)
        self.status_msg     = "Ready"
        self.status_color   = COLOR_STATUS
        self.is_downloading = False
        self.dl_thread      = None
        self.current_speed  = ""
        self.current_pct    = ""
        self.queue          = []          # URLs pending
        self.queue_done     = 0
        self.countdown_secs = 0           # seconds remaining in inter-download delay
        self.cancel_after_current = False # graceful batch stop after current file
        self.lock           = threading.Lock()
        self.output_dir     = str(Path.home() / "Downloads")

state = State()

# ─────────────────────────── PREFS ────────────────────────────────────────────

PREF_KEYS = [
    "spoof_ua", "quality_idx", "speed_idx", "delay_secs",
    "output_name", "affix_source", "list_path", "output_dir",
]

def save_prefs():
    try:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {k: getattr(state, k) for k in PREF_KEYS}
        PREFS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log(f"  Could not save prefs: {e}", COLOR_ERROR)

def load_prefs():
    try:
        if not PREFS_FILE.exists():
            return
        data = json.loads(PREFS_FILE.read_text())
        for k in PREF_KEYS:
            if k in data:
                setattr(state, k, data[k])
        # clamp index values in case lists shrank
        state.quality_idx = max(0, min(state.quality_idx, len(QUALITIES) - 1))
        state.speed_idx   = max(0, min(state.speed_idx,   len(SPEED_LIMITS) - 1))
        state.delay_secs  = max(0, int(state.delay_secs))
    except Exception:
        pass  # silently ignore corrupt prefs



def log_url_to_file(url: str, status: str):
    """Append a URL entry to the persistent download history log."""
    try:
        URL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with URL_LOG_FILE.open("a") as f:
            f.write(f"{ts}  [{status}]  |  {url}\n")
    except Exception as e:
        log(f"  Could not write URL log: {e}", COLOR_ERROR)


def was_downloaded(url: str) -> bool:
    """Return True if url appears in the history log with status OK."""
    try:
        if not URL_LOG_FILE.exists():
            return False
        with URL_LOG_FILE.open("r") as f:
            for line in f:
                if "  [OK]  |  " in line and line.strip().endswith(url):
                    return True
    except Exception:
        pass
    return False


def domain_tag(url: str) -> str:
    try:
        host = urlparse(url).netloc
        parts = host.split(".")
        if len(parts) >= 2:
            return parts[-2]
    except Exception:
        pass
    return "web"


def build_cmd(url: str, output_name: str = "") -> list[str]:
    cmd = ["yt-dlp"]

    if state.spoof_ua:
        cmd += ["--user-agent", FIREFOX_UA]

    fmt = QUALITIES[state.quality_idx][1]
    cmd += ["-f", fmt]

    speed = SPEED_LIMITS[state.speed_idx][1]
    if speed:
        cmd += ["--limit-rate", speed]

    name = output_name or state.output_name or "%(title)s"
    if state.affix_source:
        tag = domain_tag(url)
        name = f"{name}_[{tag}]" if output_name or state.output_name else f"%(title)s_[{tag}]"

    out_template = os.path.join(state.output_dir, f"{name}.%(ext)s")
    cmd += ["-o", out_template]

    # progress in machine-readable form
    cmd += ["--newline", "--progress-template",
            "PROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s"
            "|%(progress.speed)s|%(progress.eta)s|%(progress._percent_str)s"]

    cmd.append(url)
    return cmd


def log(text: str, color_key: int = COLOR_STATUS):
    ts = datetime.now().strftime("%H:%M:%S")
    with state.lock:
        state.download_log.append((ts, text, color_key))
        if len(state.download_log) > 500:
            state.download_log.pop(0)


def fmt_bytes(b):
    try:
        b = float(b)
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} TB"
    except Exception:
        return str(b)


def fmt_speed(raw):
    try:
        raw = float(raw)
        if raw < 1024:
            return f"{raw:.0f} B/s"
        elif raw < 1024**2:
            return f"{raw/1024:.1f} KB/s"
        else:
            return f"{raw/1024**2:.2f} MB/s"
    except Exception:
        return ""


# ─────────────────────────── DOWNLOAD THREAD ──────────────────────────────────

def run_download(urls: list[str]):
    state.is_downloading = True
    state.cancel_after_current = False
    state.queue = list(urls)
    state.queue_done = 0
    total = len(urls)

    for i, url in enumerate(urls):
        with state.lock:
            state.status_msg   = f"Downloading {i+1}/{total}  ({total - i - 1} remaining): {url[:50]}"
            state.status_color = COLOR_STATUS

        log(f"▶ Starting: {url}", COLOR_STATUS)
        if was_downloaded(url):
            log(f"  ⚠ Previously Downloaded", COLOR_SPEED)
        cmd = build_cmd(url)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            for line in proc.stdout:
                line = line.rstrip()
                if line.startswith("PROG|"):
                    parts = line.split("|")
                    if len(parts) >= 6:
                        speed_raw = parts[3]
                        pct_str   = parts[5].strip()
                        with state.lock:
                            state.current_speed = fmt_speed(speed_raw)
                            state.current_pct   = pct_str
                else:
                    if line:
                        color = COLOR_ERROR if "ERROR" in line else COLOR_DIM
                        log(line, color)

            proc.wait()
            state.queue_done += 1

            if proc.returncode == 0:
                log(f"✓ Done: {url}", COLOR_SUCCESS)
                log_url_to_file(url, "OK")
                with state.lock:
                    state.current_speed = ""
                    state.current_pct   = ""
                    if state.url_input == url:
                        state.url_input = ""
            else:
                log(f"✗ Failed (exit {proc.returncode}): {url}", COLOR_ERROR)
                log_url_to_file(url, f"FAILED exit={proc.returncode}")

        except FileNotFoundError:
            log("✗ yt-dlp not found — install it first: pip install yt-dlp", COLOR_ERROR)
            break
        except Exception as e:
            log(f"✗ Error: {e}", COLOR_ERROR)

        # Graceful cancel — stop after this file
        if state.cancel_after_current:
            skipped_urls = state.queue[state.queue_done:]
            skipped = len(skipped_urls)
            for skipped_url in skipped_urls:
                log_url_to_file(skipped_url, "SKIPPED")
            log(f"⏹ Batch cancelled — {skipped} item(s) skipped", COLOR_ERROR)
            with state.lock:
                state.cancel_after_current = False
            break

        # Hard stop (X key)
        if not state.is_downloading:
            for skipped_url in state.queue[state.queue_done:]:
                log_url_to_file(skipped_url, "SKIPPED")
            break

        # inter-download delay
        delay = state.delay_secs
        if delay and i < total - 1:
            log(f"  Waiting {delay}s before next download…", COLOR_DIM)
            for remaining in range(delay, 0, -1):
                with state.lock:
                    state.countdown_secs = remaining
                    state.status_msg   = f"Next download in {remaining}s  ({state.queue_done}/{total} done)"
                    state.status_color = COLOR_SPEED
                if not state.is_downloading or state.cancel_after_current:
                    break
                time.sleep(1)
            with state.lock:
                state.countdown_secs = 0

    with state.lock:
        state.is_downloading = False
        state.status_msg     = f"Finished — {state.queue_done}/{total} downloaded"
        state.status_color   = COLOR_SUCCESS
        state.current_speed  = ""
        state.current_pct    = ""


def start_download(urls):
    if state.is_downloading:
        return
    t = threading.Thread(target=run_download, args=(urls,), daemon=True)
    state.dl_thread = t
    t.start()


# ─────────────────────────── TUI DRAWING ──────────────────────────────────────

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    bg = -1
    curses.init_pair(COLOR_HEADER,  curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(COLOR_MENU,    curses.COLOR_WHITE,  bg)
    curses.init_pair(COLOR_SELECT,  curses.COLOR_BLACK,  curses.COLOR_GREEN)
    curses.init_pair(COLOR_INPUT,   curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(COLOR_STATUS,  curses.COLOR_CYAN,   bg)
    curses.init_pair(COLOR_SPEED,   curses.COLOR_YELLOW, bg)
    curses.init_pair(COLOR_ERROR,   curses.COLOR_RED,    bg)
    curses.init_pair(COLOR_SUCCESS, curses.COLOR_GREEN,  bg)
    curses.init_pair(COLOR_BORDER,  curses.COLOR_CYAN,   bg)
    curses.init_pair(COLOR_DIM,     curses.COLOR_WHITE,  bg)
    curses.init_pair(COLOR_TITLE,   curses.COLOR_YELLOW, bg)


def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    max_len = w - x - 1
    if max_len <= 0:
        return
    try:
        win.addstr(y, x, text[:max_len], attr)
    except curses.error:
        pass


def draw_box(win, y, x, h, w, title="", color=COLOR_BORDER):
    attr = curses.color_pair(color)
    try:
        win.attron(attr)
        win.addch(y,     x,     curses.ACS_ULCORNER)
        win.addch(y,     x+w-1, curses.ACS_URCORNER)
        win.addch(y+h-1, x,     curses.ACS_LLCORNER)
        win.addch(y+h-1, x+w-1, curses.ACS_LRCORNER)
        for i in range(1, w-1):
            win.addch(y,     x+i, curses.ACS_HLINE)
            win.addch(y+h-1, x+i, curses.ACS_HLINE)
        for i in range(1, h-1):
            win.addch(y+i, x,     curses.ACS_VLINE)
            win.addch(y+i, x+w-1, curses.ACS_VLINE)
        win.attroff(attr)
    except curses.error:
        pass
    if title:
        safe_addstr(win, y, x+2, f" {title} ",
                    curses.color_pair(COLOR_TITLE) | curses.A_BOLD)


def draw_header(win, w):
    title = " yt-dlp TUI  —  Interactive Download Manager "
    safe_addstr(win, 0, 0, " " * w, curses.color_pair(COLOR_HEADER))
    safe_addstr(win, 0, max(0, (w - len(title)) // 2), title,
                curses.color_pair(COLOR_HEADER) | curses.A_BOLD)


def draw_settings(win, start_y, w):
    """Left column: toggles + dropdowns"""
    draw_box(win, start_y, 0, 18, w // 2, "Settings")

    col = 2
    cw  = w // 2 - 3

    def row(y, label, value, active_color=COLOR_STATUS):
        safe_addstr(win, start_y + y, col,
                    f"{label:<22}", curses.color_pair(COLOR_DIM))
        safe_addstr(win, start_y + y, col + 22,
                    str(value)[:cw - 22], curses.color_pair(active_color) | curses.A_BOLD)

    ua_val  = "Firefox UA  ✓" if state.spoof_ua else "Disabled"
    ua_col  = COLOR_SUCCESS if state.spoof_ua else COLOR_ERROR
    aff_val = "Enabled  ✓"   if state.affix_source else "Disabled"
    aff_col = COLOR_SUCCESS if state.affix_source else COLOR_ERROR

    delay_val = f"{state.delay_secs}s" if state.delay_secs else "None"
    row(2,  "[U] Spoof User-Agent:", ua_val,  ua_col)
    row(4,  "[Q] Quality:",          QUALITIES[state.quality_idx][0])
    row(6,  "[S] Speed Limit:",      SPEED_LIMITS[state.speed_idx][0])
    row(8,  "[D] Delay Between:",    delay_val)
    row(10, "[A] Affix Source:",     aff_val, aff_col)
    row(12, "[O] Output Name:",      state.output_name or "(auto from title)")
    row(14, "[P] Output Dir:",       state.output_dir[:cw - 22])

    hint = "  Press key in [ ] to change"
    safe_addstr(win, start_y + 16, col, hint, curses.color_pair(COLOR_DIM))


def draw_download(win, start_y, col_x, w):
    """Right column: URL input + queue + list"""
    panel_w = w - col_x
    draw_box(win, start_y, col_x, 18, panel_w, "Download")

    c = col_x + 2
    pw = panel_w - 4

    safe_addstr(win, start_y + 2,  c, "[I] URL / Paste:", curses.color_pair(COLOR_DIM))
    url_disp = (state.url_input or "")[:pw]
    safe_addstr(win, start_y + 3,  c, f" {url_disp:<{pw-1}}", curses.color_pair(COLOR_INPUT))

    safe_addstr(win, start_y + 5,  c, "[L] Batch list file:", curses.color_pair(COLOR_DIM))
    list_disp = (state.list_path or "")[:pw]
    safe_addstr(win, start_y + 6,  c, f" {list_disp:<{pw-1}}", curses.color_pair(COLOR_INPUT))

    safe_addstr(win, start_y + 8,  c, "[N] Create / Edit list interactively",
                curses.color_pair(COLOR_DIM))

    # status / speed bar
    with state.lock:
        spd       = state.current_speed
        pct       = state.current_pct
        dl        = state.is_downloading
        qd        = state.queue_done
        qt        = len(state.queue)
        countdown = state.countdown_secs
        cancelling = state.cancel_after_current

    if dl:
        if countdown:
            bar = f"  ⏳ Next in {countdown}s …"
            safe_addstr(win, start_y + 10, c, f"{' ' * pw}", curses.color_pair(COLOR_DIM))
            safe_addstr(win, start_y + 10, c, bar[:pw],
                        curses.color_pair(COLOR_SPEED) | curses.A_BOLD)
        else:
            bar = f"  ↓ {spd or '…'}   {pct or ''}"
            safe_addstr(win, start_y + 10, c, f"{' ' * pw}", curses.color_pair(COLOR_DIM))
            safe_addstr(win, start_y + 10, c, bar[:pw],
                        curses.color_pair(COLOR_SPEED) | curses.A_BOLD)

        remaining = qt - qd
        if qt > 1:
            if cancelling:
                queue_str = f"  ⏹ Finishing current — {remaining} item(s) will be skipped"
                q_color   = COLOR_ERROR
            else:
                queue_str = f"  ✓ {qd} done   ↻ {remaining} remaining   ({qt} total)"
                q_color   = COLOR_STATUS
        else:
            queue_str = f"  Downloading 1 of 1"
            q_color   = COLOR_STATUS
        safe_addstr(win, start_y + 11, c, f"{' ' * pw}", curses.color_pair(COLOR_DIM))
        safe_addstr(win, start_y + 11, c, queue_str[:pw],
                    curses.color_pair(q_color))
    else:
        safe_addstr(win, start_y + 10, c, " " * pw)
        safe_addstr(win, start_y + 11, c, " " * pw)

    # action hints — swap C in when a batch is active
    if dl and len(state.queue) > 1:
        actions  = "[C] Cancel after current   [X] Stop now"
    else:
        actions  = "[ENTER] Download URL   [B] Batch Download   [X] Stop"
    safe_addstr(win, start_y + 13, c, " " * pw, curses.color_pair(COLOR_DIM))
    safe_addstr(win, start_y + 13, c, actions[:pw], curses.color_pair(COLOR_DIM))

    key_hint = "[R] Reset log   [?] Help   [ESC] Quit"
    safe_addstr(win, start_y + 15, c, key_hint[:pw], curses.color_pair(COLOR_DIM))


def draw_log(win, log_y, w, log_h):
    draw_box(win, log_y, 0, log_h, w, "Download Log")
    with state.lock:
        logs = list(state.download_log)

    inner_h = log_h - 2
    visible = logs[-(inner_h):]
    for i, (ts, text, ck) in enumerate(visible):
        line = f" {ts}  {text}"
        safe_addstr(win, log_y + 1 + i, 1, line[:w - 2],
                    curses.color_pair(ck))


def draw_statusbar(win, h, w):
    with state.lock:
        msg = state.status_msg
        col = state.status_color
    bar = f"  {msg}"
    safe_addstr(win, h - 1, 0, " " * (w - 1), curses.color_pair(COLOR_HEADER))
    safe_addstr(win, h - 1, 0, bar[:w - 1],
                curses.color_pair(COLOR_HEADER) | curses.A_BOLD)


# ─────────────────────────── INPUT HELPERS ─────────────────────────────────────

def readline_popup(stdscr, prompt: str, prefill: str = "") -> str:
    """Simple single-line input popup"""
    h, w = stdscr.getmaxyx()
    pw = min(80, w - 4)
    py = h // 2 - 2
    px = (w - pw) // 2

    popup = curses.newwin(5, pw, py, px)
    popup.keypad(True)
    curses.curs_set(1)
    draw_box(popup, 0, 0, 5, pw, prompt)

    buf = list(prefill)
    cursor = len(buf)

    while True:
        inner = pw - 4
        disp = "".join(buf)[-inner:]
        safe_addstr(popup, 2, 2, " " * inner, curses.color_pair(COLOR_INPUT))
        safe_addstr(popup, 2, 2, disp[:inner], curses.color_pair(COLOR_INPUT))
        popup.move(2, min(2 + cursor, 2 + inner - 1))
        popup.refresh()

        ch = popup.get_wch()
        if ch in ("\n", "\r", curses.KEY_ENTER):
            break
        elif ch in (27,):          # ESC
            buf = list(prefill)
            break
        elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if cursor > 0:
                buf.pop(cursor - 1)
                cursor -= 1
        elif ch == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif ch == curses.KEY_RIGHT:
            cursor = min(len(buf), cursor + 1)
        elif ch == curses.KEY_HOME:
            cursor = 0
        elif ch == curses.KEY_END:
            cursor = len(buf)
        elif isinstance(ch, str) and ch.isprintable():
            buf.insert(cursor, ch)
            cursor += 1

    curses.curs_set(0)
    del popup
    stdscr.touchwin()
    stdscr.refresh()
    return "".join(buf).strip()


def pick_from_list(stdscr, title: str, items: list, current: int) -> int:
    """Arrow-key selection popup; returns chosen index."""
    n   = len(items)
    pw  = min(60, stdscr.getmaxyx()[1] - 4)
    ph  = min(n + 4, stdscr.getmaxyx()[0] - 4)
    py  = (stdscr.getmaxyx()[0] - ph) // 2
    px  = (stdscr.getmaxyx()[1] - pw) // 2

    popup = curses.newwin(ph, pw, py, px)
    popup.keypad(True)
    sel = current
    offset = max(0, sel - (ph - 5))

    while True:
        popup.erase()
        draw_box(popup, 0, 0, ph, pw, title)
        inner_h = ph - 4
        for i in range(inner_h):
            idx = offset + i
            if idx >= n:
                break
            label = items[idx][:pw - 4]
            if idx == sel:
                safe_addstr(popup, i + 2, 2, f" {label:<{pw-5}} ",
                            curses.color_pair(COLOR_SELECT) | curses.A_BOLD)
            else:
                safe_addstr(popup, i + 2, 2, f" {label:<{pw-5}} ",
                            curses.color_pair(COLOR_MENU))
        safe_addstr(popup, ph - 2, 2, "↑↓ navigate   ENTER select   ESC cancel",
                    curses.color_pair(COLOR_DIM))
        popup.refresh()

        ch = popup.getch()
        if ch == curses.KEY_UP:
            sel = max(0, sel - 1)
            if sel < offset:
                offset = sel
        elif ch == curses.KEY_DOWN:
            sel = min(n - 1, sel + 1)
            if sel >= offset + inner_h:
                offset = sel - inner_h + 1
        elif ch in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            break
        elif ch == 27:
            sel = current
            break

    del popup
    stdscr.touchwin()
    stdscr.refresh()
    return sel


def edit_list_popup(stdscr) -> list[str]:
    """Multi-line URL list editor. Returns list of URLs."""
    h, w = stdscr.getmaxyx()
    ph, pw = max(15, h - 6), max(50, w - 8)
    py, px = 3, 4

    popup = curses.newwin(ph, pw, py, px)
    popup.keypad(True)
    curses.curs_set(1)
    draw_box(popup, 0, 0, ph, pw, "Batch URL List Editor — one URL per line")

    instructions = " ESC save+close  |  Type URLs, one per line"
    safe_addstr(popup, ph - 2, 2, instructions[:pw - 4], curses.color_pair(COLOR_DIM))

    # Load existing list if set
    lines = [""]
    if state.list_path and os.path.isfile(state.list_path):
        with open(state.list_path) as f:
            lines = [l.rstrip() for l in f.readlines()] or [""]

    cur_line = len(lines) - 1
    cur_col  = len(lines[cur_line])
    inner_h  = ph - 4
    offset   = 0

    while True:
        for i in range(inner_h):
            li = offset + i
            row_y = i + 2
            safe_addstr(popup, row_y, 1, " " * (pw - 2))
            if li < len(lines):
                prefix = f"{li+1:>3} "
                text   = lines[li][:pw - 7]
                attr   = curses.color_pair(COLOR_SELECT) if li == cur_line else curses.color_pair(COLOR_DIM)
                safe_addstr(popup, row_y, 2, prefix + text, attr)

        # cursor position
        screen_row = cur_line - offset + 2
        screen_col = 6 + cur_col
        try:
            popup.move(screen_row, min(screen_col, pw - 2))
        except curses.error:
            pass
        popup.refresh()

        ch = popup.get_wch()

        if ch == 27:  # ESC — save and close
            break
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            lines.insert(cur_line + 1, "")
            cur_line += 1
            cur_col = 0
        elif ch == curses.KEY_UP:
            if cur_line > 0:
                cur_line -= 1
                cur_col = min(cur_col, len(lines[cur_line]))
                if cur_line < offset:
                    offset -= 1
        elif ch == curses.KEY_DOWN:
            if cur_line < len(lines) - 1:
                cur_line += 1
                cur_col = min(cur_col, len(lines[cur_line]))
                if cur_line >= offset + inner_h:
                    offset += 1
        elif ch == curses.KEY_LEFT:
            cur_col = max(0, cur_col - 1)
        elif ch == curses.KEY_RIGHT:
            cur_col = min(len(lines[cur_line]), cur_col + 1)
        elif ch == curses.KEY_HOME:
            cur_col = 0
        elif ch == curses.KEY_END:
            cur_col = len(lines[cur_line])
        elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if cur_col > 0:
                lines[cur_line] = lines[cur_line][:cur_col-1] + lines[cur_line][cur_col:]
                cur_col -= 1
            elif cur_line > 0:
                prev = lines[cur_line - 1]
                cur_col = len(prev)
                lines[cur_line - 1] = prev + lines[cur_line]
                lines.pop(cur_line)
                cur_line -= 1
                if cur_line < offset:
                    offset -= 1
        elif ch == curses.KEY_DC:
            if cur_col < len(lines[cur_line]):
                lines[cur_line] = lines[cur_line][:cur_col] + lines[cur_line][cur_col+1:]
            elif cur_line < len(lines) - 1:
                lines[cur_line] += lines.pop(cur_line + 1)
        elif isinstance(ch, str) and ch.isprintable():
            lines[cur_line] = lines[cur_line][:cur_col] + ch + lines[cur_line][cur_col:]
            cur_col += 1

    curses.curs_set(0)
    del popup
    stdscr.touchwin()
    stdscr.refresh()
    return [l for l in lines if l.strip()]


def show_help(stdscr):
    lines = [
        "  yt-dlp TUI — Key Bindings",
        "",
        "  [U]        Toggle Firefox user-agent spoofing",
        "  [Q]        Choose download quality",
        "  [S]        Set speed limit",
        "  [D]        Set delay between batch downloads (type any number of seconds)",
        "  [A]        Toggle auto-affix site name to filename",
        "  [O]        Set output filename (blank = auto)",
        "  [P]        Set output directory",
        "",
        "  [I]        Enter a URL to download",
        "  [ENTER]    Start downloading entered URL",
        "  [L]        Point to an existing URL list file",
        "  [N]        Open interactive list editor",
        "  [B]        Start batch download from list",
        "  [X]        Stop download immediately (kills current file)",
        "  [C]        Cancel after current file finishes (batch only; press again to undo)",
        "  [R]        Clear download log",
        "",
        "  [?]        This help screen",
        "  [ESC / Ctrl-Q]  Quit",
        "",
        "  Press any key to close…",
    ]
    h, w = stdscr.getmaxyx()
    ph = len(lines) + 2
    pw = max(len(l) for l in lines) + 4
    ph = min(ph, h - 2)
    pw = min(pw, w - 4)
    py = (h - ph) // 2
    px = (w - pw) // 2

    popup = curses.newwin(ph, pw, py, px)
    draw_box(popup, 0, 0, ph, pw, "Help")
    for i, l in enumerate(lines[:ph - 2]):
        safe_addstr(popup, i + 1, 2, l[:pw - 4], curses.color_pair(COLOR_DIM))
    popup.refresh()
    popup.getch()
    del popup
    stdscr.touchwin()
    stdscr.refresh()


# ─────────────────────────── MAIN LOOP ─────────────────────────────────────────

def main(stdscr):
    init_colors()
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    load_prefs()
    log("yt-dlp TUI started — press [?] for help", COLOR_SUCCESS)
    log(f"  URL history: {URL_LOG_FILE}", COLOR_DIM)

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        if h < 24 or w < 80:
            stdscr.addstr(0, 0, f"Terminal too small ({w}x{h}). Need at least 80x24.")
            stdscr.refresh()
            time.sleep(0.2)
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                break
            continue

        draw_header(stdscr, w)
        half = w // 2

        draw_settings(stdscr, 1, w)
        draw_download(stdscr, 1, half, w)

        log_h = max(6, h - 20)
        draw_log(stdscr, 19, w, log_h)
        draw_statusbar(stdscr, h, w)

        stdscr.refresh()
        time.sleep(0.08)

        ch = stdscr.getch()
        if ch == -1:
            continue

        # ── Quit  (ESC or Ctrl-Q) ──
        if ch in (27, 17):           # ESC | Ctrl-Q
            if not state.is_downloading:
                break
            state.is_downloading = False

        # ── Toggle UA ──
        elif ch in (ord("u"), ord("U")):
            state.spoof_ua = not state.spoof_ua
            log(f"User-agent spoofing: {'ON' if state.spoof_ua else 'OFF'}", COLOR_STATUS)
            save_prefs()

        # ── Toggle affix ──
        elif ch in (ord("a"), ord("A")):
            state.affix_source = not state.affix_source
            log(f"Auto-affix source: {'ON' if state.affix_source else 'OFF'}", COLOR_STATUS)
            save_prefs()

        # ── Quality picker ──
        elif ch in (ord("q"), ord("Q")):
            labels = [q[0] for q in QUALITIES]
            state.quality_idx = pick_from_list(stdscr, "Select Quality", labels, state.quality_idx)
            log(f"Quality set to: {QUALITIES[state.quality_idx][0]}", COLOR_STATUS)
            save_prefs()
        elif ch in (ord("s"), ord("S")):
            labels = [s[0] for s in SPEED_LIMITS]
            state.speed_idx = pick_from_list(stdscr, "Select Speed Limit", labels, state.speed_idx)
            log(f"Speed limit: {SPEED_LIMITS[state.speed_idx][0]}", COLOR_STATUS)
            save_prefs()
        elif ch in (ord("d"), ord("D")):
            val = readline_popup(stdscr, "Delay between downloads (seconds, 0 = none)", str(state.delay_secs))
            try:
                state.delay_secs = max(0, int(val))
            except ValueError:
                state.delay_secs = 0
            label = f"{state.delay_secs}s" if state.delay_secs else "None"
            log(f"Delay: {label}", COLOR_STATUS)
            save_prefs()

        # ── Output name ──
        elif ch in (ord("o"), ord("O")):
            val = readline_popup(stdscr, "Output Filename (no extension)", state.output_name)
            state.output_name = val
            log(f"Output name: {val or '(auto)'}", COLOR_STATUS)
            save_prefs()

        # ── Output directory ──
        elif ch in (ord("p"), ord("P")):
            val = readline_popup(stdscr, "Output Directory", state.output_dir)
            if val:
                state.output_dir = val
                log(f"Output dir: {val}", COLOR_STATUS)
                save_prefs()

        # ── URL input ──
        elif ch in (ord("i"), ord("I")):
            val = readline_popup(stdscr, "Enter URL", state.url_input)
            state.url_input = val
            if val:
                if was_downloaded(val):
                    log(f"⚠ Previously Downloaded: {val}", COLOR_SPEED)
                else:
                    log(f"URL set: {val}", COLOR_STATUS)

        # ── Start single URL download ──
        elif ch in (curses.KEY_ENTER, ord("\n"), ord("\r"), 10, 13):
            if state.url_input and not state.is_downloading:
                if was_downloaded(state.url_input):
                    log(f"⚠ Previously Downloaded — starting anyway", COLOR_SPEED)
                start_download([state.url_input])
            elif state.is_downloading:
                log("Already downloading — wait or press X to stop", COLOR_ERROR)
            else:
                log("No URL entered — press I to set one", COLOR_ERROR)

        # ── List file path ──
        elif ch in (ord("l"), ord("L")):
            val = readline_popup(stdscr, "Path to URL list file", state.list_path)
            if val:
                state.list_path = val
                log(f"List file: {val}", COLOR_STATUS)
                save_prefs()

        # ── Interactive list editor ──
        elif ch in (ord("n"), ord("N")):
            if not state.list_path:
                state.list_path = os.path.join(state.output_dir, "ytdlp_list.txt")
            urls = edit_list_popup(stdscr)
            if urls:
                os.makedirs(os.path.dirname(state.list_path), exist_ok=True)
                with open(state.list_path, "w") as f:
                    f.write("\n".join(urls) + "\n")
                log(f"Saved {len(urls)} URL(s) to {state.list_path}", COLOR_SUCCESS)
                save_prefs()

        # ── Batch download ──
        elif ch in (ord("b"), ord("B")):
            if state.is_downloading:
                log("Already downloading", COLOR_ERROR)
            elif state.list_path and os.path.isfile(state.list_path):
                with open(state.list_path) as f:
                    urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                if urls:
                    prev = [u for u in urls if was_downloaded(u)]
                    if prev:
                        log(f"⚠ {len(prev)} URL(s) previously downloaded:", COLOR_SPEED)
                        for u in prev:
                            log(f"  ⚠ Previously Downloaded: {u}", COLOR_SPEED)
                    log(f"Starting batch: {len(urls)} URL(s)", COLOR_STATUS)
                    start_download(urls)
                else:
                    log("List file is empty", COLOR_ERROR)
            else:
                log("No list file set — press L or N first", COLOR_ERROR)

        # ── Cancel after current file (batch only) ──
        elif ch in (ord("c"), ord("C")):
            if state.is_downloading and len(state.queue) > 1:
                if state.cancel_after_current:
                    state.cancel_after_current = False
                    log("Cancel-after-current cleared — resuming batch", COLOR_STATUS)
                else:
                    state.cancel_after_current = True
                    log("⏹ Will stop after current file completes (press C again to undo)", COLOR_ERROR)
            elif state.is_downloading:
                log("Single download — use X to stop", COLOR_DIM)

        # ── Stop now ──
        elif ch in (ord("x"), ord("X")):
            if state.is_downloading:
                state.is_downloading = False
                log("Stop requested — will halt after current item", COLOR_ERROR)

        # ── Clear log ──
        elif ch in (ord("r"), ord("R")):
            with state.lock:
                state.download_log.clear()

        # ── Help ──
        elif ch == ord("?"):
            show_help(stdscr)


def run():
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    print("\nyt-dlp TUI exited.")


if __name__ == "__main__":
    run()
