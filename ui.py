"""Frameless usage card for the tray widget.

The card is a WebView2 window drawn with HTML/CSS. It is created hidden at
startup; the tray's default action shows it near the cursor. The page asks
the small JS API for state and polls it while visible, so refreshes and a
login in progress show up without extra cross-thread calls.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
import webbrowser
from datetime import datetime
from typing import Any

import webview

CARD_WIDTH = 380
CARD_HEIGHT = 300
# Measured by dragging the real window on the user's display: the window
# left/top sits 363px left of the work-area right and 259px above its
# bottom. The window rect is 364x261, so the visible card ends flush with
# the screen/taskbar edge (1-2px overhang hidden by the edge).
ANCHOR_RIGHT_GAP = 363
ANCHOR_BOTTOM_GAP = 259
DEFAULT_DETAILS_URL = (
    "https://bailian.console.aliyun.com/cn-beijing/subscription/token-plan/personal?commonbuy=1"
)
_CURRENCY_SYMBOLS = {"USD": "$", "CNY": "\u00a5", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5"}


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


def status_color(pct: float) -> str:
    if pct >= 85:
        return "#ef4444"
    if pct >= 60:
        return "#f59e0b"
    return "#5eead4"


def bar_fill(pct: float, total: int = 40) -> int:
    return max(0, min(total, int(round(pct / 100.0 * total))))


def reset_in(reset_ms: float | None) -> str:
    if not reset_ms:
        return ""
    secs = int(reset_ms / 1000.0 - time.time())
    if secs <= 0:
        return "soon"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    if mins:
        return f"{mins}m"
    return "soon"


def reset_at(reset_ms: float | None) -> str:
    if not reset_ms:
        return ""
    try:
        return datetime.fromtimestamp(reset_ms / 1000.0).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def generic_value(snap: Any) -> str:
    if snap is None or not snap.ok or snap.primary is None:
        return "-"
    if snap.unit == "percent":
        return f"{snap.primary:.0f}%"
    if snap.unit in _CURRENCY_SYMBOLS:
        return f"{_CURRENCY_SYMBOLS[snap.unit]}{snap.primary:,.2f}"
    return f"{snap.primary:g}"


def _pick_snapshot(app: Any) -> tuple[str | None, Any]:
    visible = [(name, snap) for name, snap in getattr(app, "snapshots", []) if not snap.hidden]
    bailian = [(name, snap) for name, snap in visible if snap.windows]
    if bailian:
        return bailian[0]
    if visible:
        return visible[0]
    return None, None


class _JsApi:
    """Public surface exposed to the page as ``window.pywebview.api``."""

    def __init__(self, window: "UsageWindow"):
        self._window = window

    def state(self) -> dict[str, Any]:
        return self._window.state()

    def refresh(self) -> None:
        self._window.refresh()

    def login(self) -> None:
        self._window.login()

    def details(self) -> None:
        self._window.open_details()

    def hide(self) -> None:
        self._window.hide()


class UsageWindow:
    def __init__(self, app: Any):
        self.app = app
        self._ready = False
        self._visible = False
        self._hwnd = 0
        self._ignore_until = 0.0
        self._watch_stop = threading.Event()
        window = webview.create_window(
            "Token Monitor",
            html=_HTML,
            js_api=_JsApi(self),
            width=CARD_WIDTH,
            height=CARD_HEIGHT,
            frameless=True,
            easy_drag=False,
            resizable=False,
            hidden=True,
            on_top=True,
            shadow=False,
            focus=True,
            background_color="#202020",
        )
        if window is None:
            raise RuntimeError("could not create the usage window")
        self._window: webview.Window = window

    # ----- lifecycle -----
    def start(self, on_ready) -> None:
        webview.start(lambda: self._on_start(on_ready), gui="edgechromium")

    def _on_start(self, on_ready) -> None:
        self._ready = True
        self._apply_style()
        threading.Thread(target=self._focus_watch, daemon=True).start()
        try:
            on_ready()
        except Exception as exc:
            print(f"GUI ready hook failed: {exc}", file=sys.stderr)

    def show(self) -> None:
        if not self._ready:
            return
        try:
            self._position()
            self._window.show()
            self._visible = True
            self._apply_style()
            self._position()
            self._ignore_until = time.time() + 0.5
            if self._hwnd:
                try:
                    ctypes.windll.user32.SetForegroundWindow(self._hwnd)
                except Exception:
                    pass
            self.notify()
        except Exception:
            self._visible = False

    def hide(self) -> None:
        self._visible = False
        try:
            self._window.hide()
        except Exception:
            pass

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def destroy(self) -> None:
        self._visible = False
        self._watch_stop.set()
        try:
            self._window.destroy()
        except Exception:
            pass

    def _focus_watch(self) -> None:
        """Hide when another window becomes foreground, mirroring a Windows
        tray flyout's click-outside behavior. The short grace period lets
        show() take focus before the first check."""
        user32 = ctypes.windll.user32
        while not self._watch_stop.wait(0.15):
            if not (self._visible and self._hwnd):
                continue
            if time.time() < self._ignore_until:
                continue
            try:
                foreground = user32.GetForegroundWindow()
                root = user32.GetAncestor(foreground, 2) if foreground else 0
            except Exception:
                continue
            if root != self._hwnd:
                self.hide()

    def notify(self) -> None:
        if not (self._visible and self._ready):
            return
        try:
            self._window.evaluate_js("window.loadUsage && window.loadUsage()")
        except Exception:
            pass

    # ----- positioning / native styling -----
    def _position(self) -> None:
        """Anchor to the bottom-right of the monitor containing the cursor,
        like a Windows tray flyout. The work area already excludes the
        taskbar; 8px keeps the card off the screen and taskbar edges."""
        user32 = ctypes.windll.user32
        point = _POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return
        work = _RECT()
        placed = False
        try:
            monitor = user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                work = info.rcWork
                placed = True
        except Exception:
            placed = False
        if not placed and not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
            work.left = 0
            work.top = 0
            work.right = user32.GetSystemMetrics(0)
            work.bottom = user32.GetSystemMetrics(1)
        x = max(work.left, work.right - ANCHOR_RIGHT_GAP)
        y = max(work.top, work.bottom - ANCHOR_BOTTOM_GAP)
        try:
            self._window.move(x, y)
        except Exception:
            pass

    def _native_handle(self) -> int:
        try:
            native = self._window.native
        except Exception:
            return 0
        for _ in range(6):
            raw = getattr(native, "Handle", None) or getattr(native, "hwnd", None)
            try:
                value = int(str(raw)) if raw is not None else 0
            except (TypeError, ValueError):
                value = 0
            if value:
                return value
            time.sleep(0.03)
            try:
                native = self._window.native
            except Exception:
                return 0
        return 0

    def _apply_style(self) -> None:
        self._hwnd = self._native_handle()
        if not self._hwnd:
            return
        user32 = ctypes.windll.user32
        try:
            ex_style = user32.GetWindowLongW(self._hwnd, -20)
            user32.SetWindowLongW(self._hwnd, -20, ex_style | 0x80)  # WS_EX_TOOLWINDOW
            user32.SetWindowPos(
                self._hwnd, 0, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0020,  # NOSIZE | NOMOVE | FRAMECHANGED
            )
        except Exception:
            pass

    # ----- data / actions -----
    def _details_url(self) -> str:
        cfg = None
        if hasattr(self.app, "_bailian_cfg"):
            try:
                cfg = self.app._bailian_cfg()
            except Exception:
                cfg = None
        if cfg is None:
            return ""
        return str(cfg.get("details_url") or DEFAULT_DETAILS_URL)

    def state(self) -> dict[str, Any]:
        app = self.app
        name, snap = _pick_snapshot(app)
        if name is None and hasattr(app, "_bailian_cfg"):
            try:
                cfg = app._bailian_cfg()
            except Exception:
                cfg = None
            if cfg is not None:
                name = cfg.get("name") or "Bailian Token Plan"
        windows: list[dict[str, Any]] = []
        if snap is not None:
            for item in getattr(snap, "windows", []) or []:
                reset_ms = item.get("reset_ms")
                windows.append({
                    "label": str(item.get("label") or "Quota"),
                    "pct": round(float(item.get("pct") or 0.0), 2),
                    "primary": bool(item.get("primary")),
                    "resetIn": reset_in(reset_ms),
                    "resetAt": reset_at(reset_ms),
                })
        error = None
        if snap is not None and not snap.ok:
            error = snap.error or "Unavailable"
        if error is None and getattr(app, "_last_reload_error", None):
            error = app._last_reload_error
        message = ""
        if snap is None:
            message = "No provider configured" if not error else ""
        return {
            "ok": bool(snap is not None and snap.ok),
            "name": name or "Token Monitor",
            "error": error,
            "message": message,
            "windows": windows,
            "loginRunning": bool(getattr(app, "_bailian_login_running", False)),
            "loginError": getattr(app, "_bailian_login_error", None),
            "hasDetails": bool(self._details_url()),
            "value": generic_value(snap),
            "detail": getattr(snap, "detail", "") if snap is not None else "",
            "unit": getattr(snap, "unit", "") if snap is not None else "",
        }

    def refresh(self) -> None:
        try:
            self.app.refresh_async()
        except Exception:
            pass

    def login(self) -> None:
        self.hide()
        try:
            self.app.start_bailian_login()
        except Exception:
            pass

    def open_details(self) -> None:
        url = self._details_url()
        if not url:
            return
        self.hide()
        try:
            webbrowser.open(url)
        except Exception:
            pass


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="dark">
<style>
  :root {
    --bg: #202020;
    --text: #f5f5f5;
    --muted: #a6a6a6;
    --line: #3a3a3a;
    --track: #3a3a3a;
    --mint: #66e3b4;
  }
  html, body {
    margin: 0; width: 100%; height: 100%; overflow: hidden;
    background: var(--bg); color: var(--text);
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    -webkit-user-select: none; user-select: none;
  }
  body { display: flex; }
  .card {
    width: 100%; height: 100%; box-sizing: border-box;
    padding: 16px 20px 14px; display: flex; flex-direction: column;
    background: #202020;
  }
  .header { display: flex; justify-content: space-between; align-items: center; height: 24px; }
  .title { font-size: 15px; font-weight: 600; letter-spacing: .1px; }
  .close {
    border: 0; background: transparent; color: var(--muted); font-size: 18px;
    line-height: 1; cursor: pointer; padding: 0 2px; font-family: inherit;
  }
  .close:hover { color: #fff; }
  .provider { display: flex; align-items: center; gap: 7px; margin-top: 5px; color: #aab1bd; font-size: 12px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: #6b7280; }
  .content { flex: 1; min-height: 0; overflow-y: auto; margin-top: 14px; scrollbar-width: none; }
  .content::-webkit-scrollbar { display: none; }
  .section-head { display: flex; justify-content: space-between; align-items: baseline; font-size: 12px; font-weight: 600; color: #dfe4ec; }
  .muted { color: var(--muted); font-size: 11px; font-weight: 400; }
  .big { font-size: 32px; font-weight: 600; letter-spacing: -.6px; margin-top: 4px; color: #fff; }
  .big .unit { font-size: 13px; font-weight: 400; color: var(--muted); margin-left: 3px; letter-spacing: 0; }
  .bar { display: flex; gap: 3px; margin: 11px 0 9px; height: 12px; align-items: stretch; }
  .bar i { flex: 1; border-radius: 2px; background: var(--track); }
  .reset { font-size: 11.5px; color: #c8c8c8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .others { margin-top: 12px; padding-top: 10px; border-top: 1px solid #333333; display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
  .others > div { display: flex; justify-content: space-between; }
  .error { font-size: 12px; color: #f1a3a3; line-height: 1.45; margin: 8px 0 12px; word-break: break-word; }
  .login-error { margin-top: 8px; word-break: break-word; }
  .primary {
    border: 0; border-radius: 10px; background: var(--mint); color: #0b1512;
    font-family: inherit; font-size: 13px; font-weight: 600; height: 38px;
    width: 100%; cursor: pointer;
  }
  .primary:hover { background: #7bf0c2; }
  .footer { display: flex; align-items: center; gap: 10px; margin-top: 12px; }
  .icon {
    width: 40px; height: 40px; flex: 0 0 40px; border-radius: 50%;
    border: 1px solid var(--line); background: transparent; color: #c9d0da;
    cursor: pointer; display: flex; align-items: center;
    justify-content: center; padding: 0; font-family: inherit;
  }
  .icon svg { width: 18px; height: 18px; display: block; }
  .icon path {
    fill: none; stroke: currentColor; stroke-width: 2;
    stroke-linecap: round; stroke-linejoin: round;
  }
  .icon:hover { background: #2c2c2c; color: #fff; }
  .details { margin-top: 0; height: 40px; width: auto; flex: 1; }
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <span class="title">My usage</span>
    <button class="close" onclick="hideCard()" title="Close">&times;</button>
  </div>
  <div class="provider"><span class="dot" id="dot"></span><span id="provider">Bailian Token Plan</span></div>
  <div class="content" id="content"></div>
  <div class="footer">
    <button class="icon" onclick="refresh()" title="Refresh" aria-label="Refresh">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M20.5 12a8.5 8.5 0 1 1-2.49-6.01" />
        <path d="M20.5 4.5v6h-6" />
      </svg>
    </button>
    <button class="primary details" id="details" onclick="details()">View details &nearr;</button>
  </div>
</div>
<script>
  const SEGMENTS = 40;
  const state = { current: null };

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }
  function colorFor(pct) {
    if (pct >= 85) return '#ef4444';
    if (pct >= 60) return '#f59e0b';
    return '#5eead4';
  }
  function barHtml(pct) {
    const filled = Math.max(0, Math.min(SEGMENTS, Math.round(pct / 100 * SEGMENTS)));
    const color = colorFor(pct);
    let out = '';
    for (let i = 0; i < SEGMENTS; i++) {
      out += '<i style="background:' + (i < filled ? color : 'var(--track)') + '"></i>';
    }
    return '<div class="bar">' + out + '</div>';
  }
  function render(s) {
    state.current = s;
    document.getElementById('provider').textContent = s.name || 'Token Monitor';
    const windows = s.windows || [];
    const primary = windows.find(w => w.primary) || windows[0];
    const pct = primary ? primary.pct : 0;
    const dot = document.getElementById('dot');
    dot.style.background = s.ok ? colorFor(pct) : '#ef4444';
    dot.style.boxShadow = '0 0 8px ' + (s.ok ? colorFor(pct) : '#ef4444') + '44';
    const content = document.getElementById('content');
    if (s.ok && primary) {
      const remaining = Math.max(0, 100 - pct);
      const others = windows.filter(w => w !== primary);
      const resetText = primary.resetIn
        ? 'Resets in ' + esc(primary.resetIn) +
          (primary.resetAt ? ' &middot; ' + esc(primary.resetAt) : '')
        : (primary.resetAt ? 'Resets ' + esc(primary.resetAt) : 'Reset time unavailable');
      content.innerHTML =
        '<div class="section-head"><span>Plan Credits</span><span class="muted">Remaining ' +
          remaining.toFixed(2) + '%</span></div>' +
        '<div class="big">' + Number(pct).toFixed(2) + '<span class="unit">% used</span></div>' +
        barHtml(pct) +
        '<div class="reset">' + resetText + '</div>' +
        (others.length ? '<div class="others">' + others.map(w =>
          '<div><span>' + esc(w.label) + '</span><span class="muted">' +
          Number(w.pct).toFixed(2) + '%</span></div>').join('') + '</div>' : '');
    } else if (s.ok) {
      content.innerHTML =
        '<div class="section-head"><span>Current usage</span></div>' +
        '<div class="big">' + esc(s.value) + '</div>' +
        '<div class="muted">' + esc(s.detail || '') + '</div>';
    } else {
      const loginHint = /session expired|bl auth login/i.test(s.error || '');
      let action = '';
      if (s.loginRunning) {
        action = '<div class="muted">Waiting for browser login...</div>';
      } else if (loginHint) {
        action = '<button class="primary" onclick="login()">Login</button>';
      } else {
        action = '<button class="primary" onclick="refresh()">Retry</button>';
      }
      content.innerHTML =
        '<div class="section-head"><span>Connection</span></div>' +
        '<div class="error">' + esc(s.error || s.message || 'Waiting for data...') + '</div>' +
        action +
        (s.loginError ? '<div class="muted login-error">' + esc(s.loginError) + '</div>' : '');
    }
    document.getElementById('details').style.display = s.hasDetails ? '' : 'none';
  }

  async function loadUsage() {
    if (!window.pywebview || !window.pywebview.api) return;
    try {
      const snapshot = await window.pywebview.api.state();
      render(snapshot);
    } catch (e) {}
  }
  window.loadUsage = loadUsage;

  function refresh() {
    if (window.pywebview) window.pywebview.api.refresh();
    setTimeout(loadUsage, 800);
    setTimeout(loadUsage, 2500);
  }
  function login() {
    if (window.pywebview) window.pywebview.api.login();
  }
  function details() {
    if (window.pywebview) window.pywebview.api.details();
  }
  function hideCard() {
    if (window.pywebview) window.pywebview.api.hide();
  }

  window.addEventListener('pywebviewready', loadUsage);
  document.addEventListener('DOMContentLoaded', () => {
    loadUsage();
    setInterval(loadUsage, 2000);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') hideCard();
  });
</script>
</body>
</html>
"""