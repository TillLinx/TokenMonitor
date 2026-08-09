"""Token Monitor - Windows tray widget for coding-plan usage and API balance.

Reads ``config.json`` next to this script, polls each provider on an interval,
and renders a dynamic native-style tray icon plus a live context menu showing
every provider's current status. Hover the tray icon for a
multiline tooltip with all values.

Dependencies: pystray, Pillow, requests. No GUI framework required.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pystray
from PIL import Image, ImageDraw, ImageFont
from pystray import MenuItem as Item

from providers import Snapshot, fetch, safe_err


APP_DIR = Path(__file__).parent.resolve()

def _config_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA") or Path.home()) / "TokenMonitor"
        base.mkdir(parents=True, exist_ok=True)
        return base
    return APP_DIR


def _example_source() -> Path | None:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "config.example.json"
        return None
    return APP_DIR / "config.example.json"


CONFIG_PATH = _config_dir() / "config.json"
EXAMPLE_PATH = _example_source()

STATUS_COLORS = {
    "ok": "#10b981",
    "warn": "#f59e0b",
    "alert": "#ef4444",
    "unknown": "#6b7280",
}
CURRENCY_SYMBOLS = {"USD": "$", "CNY": "\u00a5", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5"}
STATUS_DOTS = {"ok": "\u25cf", "warn": "\u25cf", "alert": "\u25cf", "unknown": "\u25cb"}


# ---------- config ----------

def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        if EXAMPLE_PATH is not None and EXAMPLE_PATH.exists():
            CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            raise FileNotFoundError("no config.json or config.example.json found")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# ---------- formatting ----------

def _status_of(snap: Snapshot) -> str:
    """Classify a snapshot as one of: 'ok' | 'warn' | 'alert' | 'unknown'.

    Single source of truth for status thresholds, shared by the tray icon,
    tooltip dot, and worst-status picker so the three stay in sync.
    """
    if not snap.ok:
        return "unknown"
    if snap.unit == "percent" and snap.primary is not None and snap.primary >= 85:
        return "alert"
    if snap.unit == "percent" and snap.primary is not None and snap.primary >= 60:
        return "warn"
    if snap.unit in CURRENCY_SYMBOLS and snap.primary is not None:
        if snap.budget is not None and snap.budget > 0:
            # Monthly-budget mode: color tracks 'how much of the budget
            # is gone', using the same percent thresholds as the
            # percent unit so percent and currency icons stay in sync.
            used_pct = (snap.budget - snap.primary) / snap.budget * 100.0
            if used_pct >= 85:
                return "alert"
            if used_pct >= 60:
                return "warn"
        else:
            threshold = snap.alert_below if snap.alert_below is not None else 1.0
            if snap.primary <= threshold:
                return "alert"
    return "ok"


_STATUS_RANK = {"alert": 2, "warn": 1, "ok": 0, "unknown": -1}


def status_color(snap: Snapshot) -> str:
    return STATUS_COLORS[_status_of(snap)]


def status_rank(snap: Snapshot) -> int:
    return _STATUS_RANK[_status_of(snap)]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _usage_fraction(snap: Snapshot) -> float | None:
    """Return the 0..1 used fraction that the tiny tray gauge should show."""
    if not snap.ok or snap.primary is None:
        return None
    if snap.unit == "percent":
        return _clamp01(snap.primary / 100.0)
    if snap.unit in CURRENCY_SYMBOLS and snap.budget is not None and snap.budget > 0:
        return _clamp01((snap.budget - snap.primary) / snap.budget)
    return None


def format_primary(snap: Snapshot) -> str:
    if not snap.ok:
        return snap.error or "ERR"
    if snap.primary is None:
        return "-"
    if snap.unit == "percent":
        return f"{snap.primary:.0f}%"
    if snap.unit in CURRENCY_SYMBOLS:
        sym = CURRENCY_SYMBOLS[snap.unit]
        return f"{sym}{snap.primary:,.2f}"
    return f"{snap.primary:g}"


def _try_font(size: int) -> ImageFont.ImageFont:
    for path in (
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


# ---------- tray icon ----------

def _format_icon_label(snap: Snapshot) -> str:
    """Pick a short label that fits a 64x64 icon.

    Currency is rounded to the integer (e.g. 5.38 -> "5") because two
    decimals crowd the 64x64 glyph and become unreadable on high-DPI
    taskbars. The full precision still lives in the tooltip / menu.
    Values >= 1k are compacted to "1.5k" to keep the label inside the
    pie. No unit suffix is added: the tray icon shows the number only,
    the tooltip/menu carry the $, %, and other unit context.
    """
    if snap.primary is None:
        return "-"
    if snap.unit == "percent":
        return f"{max(0.0, min(100.0, snap.primary)):.0f}"
    v = snap.primary
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.1f}k"
    if snap.unit in CURRENCY_SYMBOLS:
        return f"{int(round(v))}"
    return f"{v:g}"


# 4x6 pixel font used by the tray icon. "1" = on pixel, "0"/"." = off.
# Hand-tuned for chunky 16x16 pixel art; digits 0-9 plus "k", "M", ".".
PIXEL_FONT_4X6 = {
    "0": ["0110", "1001", "1001", "1001", "1001", "0110"],
    "1": ["0010", "0110", "0010", "0010", "0010", "0111"],
    "2": ["0110", "1001", "0001", "0010", "0100", "1111"],
    "3": ["0110", "1001", "0010", "0001", "1001", "0110"],
    "4": ["0010", "0110", "1010", "1111", "0010", "0010"],
    "5": ["1111", "1000", "1110", "0001", "1001", "0110"],
    "6": ["0010", "0100", "1110", "1001", "1001", "0110"],
    "7": ["1111", "0001", "0010", "0100", "0100", "0100"],
    "8": ["0110", "1001", "0110", "1001", "1001", "0110"],
    "9": ["0110", "1001", "1001", "0111", "0010", "0100"],
    "k": ["1001", "1010", "1100", "1010", "1001", "1001"],
    "M": ["1001", "1111", "1001", "1001", "1001", "1001"],
    ".": ["0000", "0000", "0000", "0000", "0000", "0010"],
    # S-shape with a small vertical bar through col 2: reads as $ in context.
    "$": [".11.", "1..1", ".11.", "..1.", "1..1", ".11."],
    # Diagonal slash with two small dots at the corners.
    "%": ["11.1", "..11", ".1..", "11..", "1.11"],
}

# 3x5 fallback for long labels (e.g. "1.5k") that would overflow at 4x6.
PIXEL_FONT_3X5 = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["010", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "010"],
    "k": ["101", "110", "100", "110", "101"],
    "M": ["101", "111", "101", "101", "101"],
    ".": ["000", "000", "000", "000", "010"],
    # Vertical bar with two horizontal stubs: reads as $ in context.
    "$": [".1.", "111", ".1.", "111", ".1."],
    # Diagonal slash with two corner dots.
    "%": ["11.", "..1", ".1.", "1..", ".11"],
}

# Default fallback for chars not in the pixel font. The "?" glyph
# warns the user that the label contains something we cannot draw.
PIXEL_FONT_MISS = {
    4: ["1111", "0001", "0010", "0000", "0010", "0000"],
    3: ["111", "001", "010", "000", "010", "000"],
}

def render_tray_icon(snapshots: list[tuple[str, Snapshot]]) -> Image.Image:
    """Render the worst-status provider as a native Windows tray glyph.

    The source art is drawn on a 16x16 transparent grid and nearest-scaled
    to 64x64. That keeps strokes aligned to tray pixels after Windows scales
    it down, avoiding the old black LED block and high-saturation green.
    """
    GLYPH = (232, 234, 237, 255)
    SOFT = (160, 166, 174, 235)
    DIM = (104, 112, 123, 215)
    WARN = (245, 158, 11, 255)
    ALERT = (239, 68, 68, 255)

    valid = [s for _, s in snapshots
             if s.ok and s.primary is not None and not s.informational]
    worst = max(valid, key=status_rank) if valid else None
    state = _status_of(worst) if worst is not None else "unknown"
    fraction = _usage_fraction(worst) if worst is not None else None

    base = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    outline = DIM if state == "unknown" else GLYPH

    # One-pixel outline, manually rounded so the 16px source remains sharp.
    draw.line([(4, 2), (12, 2)], fill=outline)
    draw.line([(4, 14), (12, 14)], fill=outline)
    draw.line([(2, 4), (2, 12)], fill=outline)
    draw.line([(14, 4), (14, 12)], fill=outline)
    draw.point([(3, 3), (13, 3), (3, 13), (13, 13)], fill=outline)

    if state == "unknown":
        draw.line([(6, 11), (11, 6)], fill=DIM)
    else:
        bar_bottom = 12
        bar_specs = [(5, 4), (8, 6), (11, 8)]
        if fraction is None:
            for x, height in [(5, 2), (8, 4), (11, 6)]:
                draw.line([(x, bar_bottom - height + 1), (x, bar_bottom)], fill=SOFT)
        else:
            for x, max_height in bar_specs:
                height = max(1, int(round(max_height * fraction))) if fraction > 0 else 0
                if height:
                    draw.line(
                        [(x, bar_bottom - height + 1), (x, bar_bottom)],
                        fill=GLYPH,
                    )
                if height < max_height:
                    draw.line(
                        [(x, bar_bottom - max_height + 1), (x, bar_bottom - height)],
                        fill=DIM,
                    )

        if state in ("warn", "alert"):
            accent = WARN if state == "warn" else ALERT
            draw.rectangle([13, 13, 14, 14], fill=accent)

    return base.resize((64, 64), Image.Resampling.NEAREST)



# ---------- menu ----------

def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _display_detail(value: str, detail: str) -> str:
    detail = (detail or "").strip()
    if not detail or detail == value:
        return ""
    return detail


def _menu_label(name: str, snap: Snapshot) -> str:
    if snap.paused:
        # Failure-throttled: surface the pause counter and the last error.
        head = f"paused ({snap.fail_count}/{snap.paused_total} fails)"
        detail = snap.error or "fetch failed"
        return _short(f"{name}: {head}  |  {detail}", 60)
    value = format_primary(snap)
    if snap.ok:
        detail = _display_detail(value, snap.detail)
        label = f"{name}: {value}"
        if detail:
            label += f"  |  {detail}"
        return _short(label, 60)
    detail = snap.error or "fetch failed"
    return _short(f"{name}: {value}  |  {detail}", 60)


# ---------- application ----------

class App:
    def __init__(self):
        self.config: dict[str, Any] = {}
        self.timeout: float = 10.0
        self.interval: int = 300
        self.provider_cfgs: list[dict[str, Any]] = []
        self.snapshots: list[tuple[str, Snapshot]] = []
        self._stop = threading.Event()
        self.icon: pystray.Icon | None = None
        self._refresh_lock = threading.Lock()
        self._last_reload_error: str | None = None
        self._last_refresh_at: float | None = None  # wall-clock seconds of last refresh
        # Per-provider failure-throttling state, keyed by display name.
        # Each entry: {"fail_count": int, "paused_remaining": int, "last_error": str}
        self._fail_state: dict[str, dict[str, Any]] = {}
        self.reload_config()

    def _apply_config(self, cfg: dict[str, Any]) -> None:
        """Update the in-memory config snapshot. Used by both __init__ and
        Reload config menu item so behaviour stays identical."""
        self.config = cfg
        self.timeout = float(cfg.get("request_timeout_seconds", 10))
        self.interval = int(cfg.get("refresh_interval_seconds", 300))
        self.provider_cfgs = list(cfg.get("providers") or [])

    def reload_config(self) -> None:
        """Re-read config.json from disk. On parse error, keep the previous
        in-memory config and surface the error in the menu/tooltip."""
        try:
            cfg = load_config()
        except Exception as exc:
            self._last_reload_error = f"reload failed: {exc}"
            print(self._last_reload_error, file=sys.stderr)
            self._update_tray()
            return
        self._last_reload_error = None
        self._apply_config(cfg)
        # Per-provider state from the previous config is no longer valid
        # if providers were added/removed/reordered. Reset snapshots and
        # the failure-throttle state so the next refresh starts clean.
        self.snapshots = []
        self._fail_state = {}
        self._update_tray()
        self.refresh_async()

    # ----- refresh -----
    def refresh(self):
        with self._refresh_lock:
            results: list[tuple[str, Snapshot]] = []
            new_fail_state: dict[str, dict[str, Any]] = {}
            for cfg in self.provider_cfgs:
                name = cfg.get("name") or cfg.get("type") or "provider"
                k = max(0, int(cfg.get("failure_pause_cycles", 5)))
                state = self._fail_state.get(
                    name,
                    {"fail_count": 0, "paused_remaining": 0, "last_error": "fetch failed"},
                )
                if state["paused_remaining"] > 0:
                    # Throttled: skip fetch, decrement, surface a paused
                    # placeholder that still carries the last error.
                    state["paused_remaining"] -= 1
                    snap = Snapshot(
                        ok=False,
                        error=state["last_error"],
                        paused=True,
                        paused_remaining=state["paused_remaining"],
                        paused_total=k,
                        fail_count=state["fail_count"],
                    )
                else:
                    try:
                        snap = fetch(cfg, timeout=self.timeout)
                    except Exception as exc:
                        snap = Snapshot(ok=False, error=safe_err(exc))
                    # Hidden (unconfigured) snapshots: pass through without
                    # throttling, so fixing the config takes effect on the
                    # very next refresh without a K-cycle wait.
                    if snap.hidden:
                        pass
                    elif snap.ok:
                        state["fail_count"] = 0
                        state["paused_remaining"] = 0
                        state["last_error"] = "fetch failed"
                    else:
                        state["fail_count"] += 1
                        state["paused_remaining"] = k
                        state["last_error"] = snap.error or "fetch failed"
                        snap = Snapshot(
                            ok=False,
                            error=state["last_error"],
                            paused=True,
                            paused_remaining=state["paused_remaining"],
                            paused_total=k,
                            fail_count=state["fail_count"],
                        )
                new_fail_state[name] = state
                results.append((name, snap))
            self._fail_state = new_fail_state
            self.snapshots = results
            self._last_refresh_at = time.time()
            self._update_tray()

    def refresh_async(self):
        threading.Thread(target=self.refresh, daemon=True).start()

    def _update_tray(self):
        if self.icon is None:
            return
        try:
            self.icon.icon = render_tray_icon(self.snapshots)
        except Exception:
            pass
        try:
            self.icon.title = self._build_tooltip()
        except Exception:
            pass
        try:
            self.icon.menu = self._build_menu()
        except Exception:
            pass

    def _format_age(self) -> str:
        """Render the time since the last successful refresh for the
        tooltip footer. Returns 'never' before the first refresh.
        `_last_refresh_at` is a wall-clock timestamp (seconds since
        the epoch), so subtracting `time.time()` gives the same
        elapsed-seconds value and reads naturally if you log it."""
        ts = getattr(self, "_last_refresh_at", None)
        if ts is None:
            return "never"
        secs = time.time() - ts
        if secs < 0:
            return "never"
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
        return f"{int(secs // 86400)}d ago"

    def _build_tooltip(self) -> str:
        # If a Reload config just failed, the per-provider data on
        # disk is stale but we kept the previous in-memory state,
        # so the only place to surface the failure is the tray
        # tooltip itself.
        if self._last_reload_error:
            return f"Token Monitor\n{self._last_reload_error}"
        visible = [(n, s) for n, s in self.snapshots if not s.hidden]
        if not visible:
            return "Token Monitor\n(no providers configured)"
        lines = ["Token Monitor"]
        for name, snap in visible:
            dot = STATUS_DOTS[_status_of(snap)]
            if snap.paused:
                head = f"paused ({snap.fail_count}/{snap.paused_total} fails)"
                err = _short(snap.error or "", 50)
                lines.append(f"{dot} {name}: {head} ERR {err}")
            elif snap.ok:
                value = format_primary(snap)
                detail = _display_detail(value, snap.detail)
                if detail:
                    lines.append(f"{dot} {name}: {value} | {_short(detail, 45)}")
                else:
                    lines.append(f"{dot} {name}: {value}")
            else:
                err = _short(snap.error or "", 50)
                lines.append(f"{dot} {name}: ERR {err}")
        lines.append(f"last update: {self._format_age()}")
        return "\n".join(lines)

    def _build_menu(self) -> pystray.Menu:
        items: list[Any] = []
        visible = [(n, s) for n, s in self.snapshots if not s.hidden]
        if not visible:
            items.append(Item("(no providers configured)", None, enabled=False))
        else:
            for name, snap in visible:
                items.append(Item(_menu_label(name, snap), None, enabled=False))
        items.extend([
            pystray.Menu.SEPARATOR,
            Item("Refresh now", lambda icon, item: self.refresh_async()),
            Item("Reload config", lambda icon, item: self.reload_config()),
            Item("Open config folder", lambda icon, item: self.open_config_dir()),
            pystray.Menu.SEPARATOR,
            Item("Quit", lambda icon, item: self.quit()),
        ])
        return pystray.Menu(*items)

    # ----- tray lifecycle -----
    def _start_tray(self):
        img = render_tray_icon(self.snapshots)
        self.icon = pystray.Icon(
            "token_monitor", img, "Token Monitor", self._build_menu(),
        )
        run_detached = getattr(self.icon, "run_detached", None)
        if callable(run_detached):
            try:
                run_detached()
                return
            except Exception:
                pass
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _shutdown(self):
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass

    # ----- main loop -----
    # Two independent timers:
    #  - data refresh every `interval` seconds (default 5m);
    #  - tooltip rebuild every TOOLTIP_TICK_S seconds (default 180s / 3min),
    #    so the "last update: Xs ago" footer keeps moving even
    #    between data refreshes. Without the tick, Windows caches
    #    the tooltip title and the age string stays "0s ago" until
    #    the next data fetch.
    TOOLTIP_TICK_S = 180  # tooltip rebuild cadence; 3 min is enough

    def run(self):
        self.refresh()
        self._start_tray()
        try:
            next_refresh = time.monotonic() + self.interval
            while not self._stop.is_set():
                tick = min(self.TOOLTIP_TICK_S,
                           max(1.0, next_refresh - time.monotonic()))
                if self._stop.wait(tick):
                    break
                # Always refresh the tooltip so "last update" ages.
                self._update_tray()
                if time.monotonic() >= next_refresh:
                    self.refresh()
                    next_refresh = time.monotonic() + self.interval
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self._shutdown()

    def quit(self):
        self._stop.set()
        self._shutdown()

    def open_config_dir(self):
        # When frozen, CONFIG_PATH lives under %APPDATA%/TokenMonitor,
        # not next to the EXE. Open the actual config dir so the user
        # lands on the file they need to edit.
        try:
            os.startfile(str(_config_dir()))
        except Exception:
            pass


def main():
    try:
        load_config()
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)
    app = App()
    app.run()


if __name__ == "__main__":
    main()



