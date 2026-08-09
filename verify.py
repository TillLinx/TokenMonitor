"""Lightweight smoke test: imports, icon render, generic provider over a
local mock HTTP server, and config validity. Run with ``python verify.py``."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

# 1. imports
import providers  # noqa: F401
import monitor  # noqa: F401
from providers import Snapshot, fetch

# 2. icon render
img = monitor.render_tray_icon([
    ("OpenAI", Snapshot(primary=42.5, unit="percent", ok=True, detail="used $5")),
    ("DeepSeek", Snapshot(primary=12.34, unit="CNY", ok=True, detail="\u4f59\u989d \u00a512.34")),
    ("Broken", Snapshot(ok=False, error="api_key missing")),
])
print(f"[1/12] icon rendered ({img.size[0]}x{img.size[1]})")

# 3. tooltip + menu build
app = monitor.App.__new__(monitor.App)
# __new__ skips __init__; supply the attrs the tooltip/menu need.
app._last_reload_error = None
app._last_refresh_at = None
app._fail_state = {}
app.snapshots = [
    ("OpenAI", Snapshot(primary=85.0, unit="percent", ok=True, detail="used $5")),
    ("DeepSeek", Snapshot(primary=12.34, unit="CNY", ok=True, detail="\u4f59\u989d \u00a512.34")),
    ("Broken", Snapshot(ok=False, error="api_key missing")),
]
tooltip = app._build_tooltip()
menu = app._build_menu()
assert "Token Monitor" in tooltip
assert "OpenAI" in tooltip and "DeepSeek" in tooltip
assert "OpenAI: 85% | used $5" in tooltip
print(f"[2/12] tooltip built ({len(tooltip.splitlines())} lines)")
print(f"       menu items: {len(menu.items)}")

# 3. status thresholds + safe_err redaction (no network, no tray)
from providers import safe_err
from monitor import _status_of, _format_icon_label, _config_dir, _menu_label

def _expect(cond, msg):
    if not cond:
        raise AssertionError(msg)

snap_pct_low = Snapshot(primary=30.0, unit="percent", ok=True)
snap_pct_mid = Snapshot(primary=70.0, unit="percent", ok=True)
snap_pct_hi  = Snapshot(primary=92.0, unit="percent", ok=True)
_expect(_status_of(snap_pct_low) == "ok",    "30% should be ok")
_expect(_status_of(snap_pct_mid) == "warn",  "70% should be warn")
_expect(_status_of(snap_pct_hi)  == "alert", "92% should be alert")
snap_cur_low  = Snapshot(primary=0.5,  unit="USD", ok=True)
snap_cur_hi   = Snapshot(primary=200.0, unit="USD", ok=True)
_expect(_status_of(snap_cur_low) == "alert", "$0.50 should be alert (<=1.0)")
_expect(_status_of(snap_cur_hi)  == "ok",    "$200 should be ok")
snap_cur_above = Snapshot(primary=50.0, unit="USD", ok=True, alert_below=20.0)
snap_cur_below = Snapshot(primary=10.0, unit="USD", ok=True, alert_below=20.0)
_expect(_status_of(snap_cur_above) == "ok",    "alert_below=20: $50 should be ok")
_expect(_status_of(snap_cur_below) == "alert", "alert_below=20: $10 should be alert")

# Budget-driven status: color follows "used" percent instead of absolute balance.
_budget_ok   = Snapshot(primary=30.0,  unit="USD", ok=True, budget=50)   # 40% used
_budget_warn = Snapshot(primary=15.0,  unit="USD", ok=True, budget=50)   # 70% used
_budget_alrt = Snapshot(primary=5.38,  unit="USD", ok=True, budget=50)   # 89% used
_budget_full = Snapshot(primary=0.0,   unit="USD", ok=True, budget=50)   # 100% used
_budget_over = Snapshot(primary=-10.0, unit="USD", ok=True, budget=50)   # > 100%, clamp
_expect(_status_of(_budget_ok)   == "ok",    "budget 40% used should be ok")
_expect(_status_of(_budget_warn) == "warn",  "budget 70% used should be warn")
_expect(_status_of(_budget_alrt) == "alert", "budget 89% used should be alert")
_expect(_status_of(_budget_full) == "alert", "budget 100% used should be alert")
_expect(_status_of(_budget_over) == "alert", "budget over 100% should be alert")
# Without budget: fall back to absolute threshold (default <= 1.0 alert).
_expect(_status_of(Snapshot(primary=5.38, unit="USD", ok=True)) == "ok",
        "currency $5.38 without budget should stay ok")
_expect(_status_of(Snapshot(primary=0.5,  unit="USD", ok=True)) == "alert",
        "currency $0.5 without budget should fall back to alert")

_expect(_format_icon_label(Snapshot(primary=85.0, unit="percent", ok=True)) == "85",
        "percent label should be rounded integer (no % suffix in icon)")
_expect(_format_icon_label(Snapshot(primary=12.34, unit="USD", ok=True)) == "12",
        "currency label should round to integer (no $ prefix in icon)")
_expect(_format_icon_label(Snapshot(primary=1500.0, unit="USD", ok=True)) == "1.5k",
        "large currency should compact to k (no $ prefix in icon)")
_expect(_format_icon_label(Snapshot(primary=1_500_000.0, unit="USD", ok=True)) == "1.5M",
        "huge currency should compact to M (no $ prefix in icon)")
_expect(_menu_label("Popcorn", Snapshot(primary=6.74, unit="USD", ok=True, detail="$6.74")) == "Popcorn: $6.74",
        "menu label should not duplicate the primary value as detail")
_expect(_menu_label("Popcorn", Snapshot(primary=6.74, unit="USD", ok=True, detail="today $1.23")) == "Popcorn: $6.74  |  today $1.23",
        "menu label should include non-duplicate detail such as today cost")
usage_fraction = getattr(monitor, "_usage_fraction", None)
_expect(callable(usage_fraction), "monitor._usage_fraction must exist for native tray gauge fill")
_expect(abs(usage_fraction(Snapshot(primary=70.0, unit="percent", ok=True)) - 0.70) < 1e-6,
        "percent snapshots should map directly to 0..1 usage fraction")
_expect(abs(usage_fraction(Snapshot(primary=9.0, unit="USD", ok=True, budget=30.0)) - 0.70) < 1e-6,
        "currency snapshots with monthly_budget should map remaining balance to used fraction")
_expect(usage_fraction(Snapshot(primary=35.0, unit="USD", ok=True, budget=30.0)) == 0.0,
        "currency usage fraction should clamp to 0 when balance exceeds budget")
_expect(usage_fraction(Snapshot(primary=-1.0, unit="USD", ok=True, budget=30.0)) == 1.0,
        "currency usage fraction should clamp to 1 when balance is below zero")
_expect(usage_fraction(Snapshot(primary=9.0, unit="USD", ok=True)) is None,
        "currency snapshots without monthly_budget should not imply a gauge fill")

# safe_err must strip query strings, redacts auth params, and stay on one line.
class _BoomErr(Exception):
    def __str__(self):
        return ("HTTPSConnectionPool(host='api.example.com', port=443): "
                "Max retries exceeded with url: /v1/usage?api_key=sk-secret\n"
                "Caused by SSLError: certificate verify failed")
msg = safe_err(_BoomErr())
_expect("\n" not in msg,                       f"safe_err must strip newlines, got: {msg!r}")
_expect("sk-secret" not in msg,                 f"safe_err must redact api_key, got: {msg!r}")
_expect("url: (redacted)" in msg,               f"safe_err must redact urllib3 url: fragment, got: {msg!r}")
_expect("api.example.com" in msg,               f"safe_err should keep host, got: {msg!r}")
_expect(len(msg) <= 160,                         f"safe_err should cap at 160 chars, got {len(msg)}")
print(f"[3/12] status + safe_err OK")

# Multi-state icon render: ok / warn / alert / currency / empty / paused.
def _state(label, snap_list):
    img = monitor.render_tray_icon(snap_list)
    _expect(img.size == (64, 64), f"{label}: icon should be 64x64, got {img.size}")
    _expect(any(p[3] > 0 for p in _pixels(img)), f"{label}: icon should have visible pixels")
    return img

def _pixels(img):
    getter = getattr(img, "get_flattened_data", None)
    return list(getter() if callable(getter) else img.getdata())

_state("64_alert", [("X", Snapshot(primary=92.0, unit="percent", ok=True))])
_state("64_warn",  [("X", Snapshot(primary=70.0, unit="percent", ok=True))])
_state("64_ok",    [("X", Snapshot(primary=30.0, unit="percent", ok=True))])
_state("64_currency", [("Popcorn", Snapshot(primary=9.71, unit="USD", ok=True, detail="$9.71"))])
_state("64_empty", [])
print(f"[4/12] multi-state icon render OK")

# 4b. Tray icon is a 64x64 native tray glyph.
_dim_img = monitor.render_tray_icon([("X", Snapshot(primary=30.0, unit="percent", ok=True))])
_expect(_dim_img.size == (64, 64), f"icon should be 64x64, got {_dim_img.size}")
_dim_img2 = monitor.render_tray_icon([("X", Snapshot(primary=5.0, unit="USD", ok=True))])
_expect(_dim_img2.size == (64, 64), f"currency icon should also be 64x64, got {_dim_img2.size}")

# 4c. Native tray look: transparent background, white/gray ok glyph,
# and only a small colored accent for warn/alert states.
_corner = _dim_img.getpixel((1, 1))
_expect(_corner[3] == 0,
        f"icon corner should be transparent for native tray style, got {_corner}")
_corner2 = _dim_img2.getpixel((62, 62))
_expect(_corner2[3] == 0,
        f"currency icon corner should be transparent for native tray style, got {_corner2}")
_ok_pixels = [p for p in _pixels(_dim_img) if p[3] > 0]
_expect(_ok_pixels, "native tray icon should draw visible glyph pixels")
_expect(all(abs(p[0] - p[1]) <= 16 and abs(p[1] - p[2]) <= 16 for p in _ok_pixels),
        f"ok icon should be neutral white/gray only, got colored pixels: {_ok_pixels[:8]}")
_warn_img = monitor.render_tray_icon([("X", Snapshot(primary=70.0, unit="percent", ok=True))])
_warn_accent = [
    p for p in _pixels(_warn_img)
    if p[3] > 0 and p[0] > p[1] > p[2] and p[0] - p[2] > 80
]
_expect(1 <= len(_warn_accent) <= 80,
        f"warn icon should use a small amber accent, got {len(_warn_accent)} accent pixels")
_alert_img = monitor.render_tray_icon([("X", Snapshot(primary=92.0, unit="percent", ok=True))])
_alert_accent = [
    p for p in _pixels(_alert_img)
    if p[3] > 0 and p[0] > 180 and p[1] < 120 and p[2] < 120
]
_expect(1 <= len(_alert_accent) <= 80,
        f"alert icon should use a small red accent, got {len(_alert_accent)} accent pixels")

# 5. open_config_dir dispatch: under source, _config_dir() == APP_DIR,
# so the call must not raise. We don't actually invoke Explorer.
cdir = _config_dir()
_expect(cdir.exists(), f"_config_dir() must exist, got {cdir}")
print(f"[5/12] _config_dir() -> {cdir}")

# 6. generic provider end-to-end via local server
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"used": 70, "limit": 100, "balance": 12.5}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

cfg = {
    "name": "Mock",
    "type": "generic",
    "url": f"http://127.0.0.1:{port}/usage",
    "headers": {"Authorization": "Bearer test-token-for-verify-1234567890"},
    "used_path": "used",
    "limit_path": "limit",
}
snap = fetch(cfg, timeout=5)
server.shutdown()

assert snap.ok, f"expected ok, got error={snap.error}"
assert abs(snap.primary - 70.0) < 1e-6, f"expected 70%, got {snap.primary}"
print(f"[6/12] generic provider OK -> primary={snap.primary:.1f}% detail='{snap.detail}'")

# 5. config example is valid JSON
example = json.loads((HERE / "config.example.json").read_text(encoding="utf-8"))
assert "providers" in example and isinstance(example["providers"], list)
print(f"[7/12] config.example.json OK -> {len(example['providers'])} providers")

# 8. Popcorn-shaped config end-to-end + minimax empty body graceful.
from providers import fetch

class _CurrencyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"remaining": 9.70621048, "unit": "USD",
                            "quota": {"limit": 20, "used": 10.29,
                                      "remaining": 9.71, "unit": "USD"},
                            "usage": {"today": {"cost": 1.2345}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

srv = HTTPServer(("127.0.0.1", 0), _CurrencyHandler)
p2 = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
pop = {"name": "MockPop", "type": "generic",
       "url": f"http://127.0.0.1:{p2}/v1/usage",
       "api_key": "test-token-for-verify-1234567890",
       "value_path": "remaining", "format": "currency", "currency": "USD"}
s_pop = fetch(pop, timeout=5)
pop_today = dict(pop)
pop_today["today_cost_path"] = "usage.today.cost"
s_today = fetch(pop_today, timeout=5)
pop_today_rate = dict(pop_today)
pop_today_rate["today_cost_rate"] = 0.125
s_today_rate = fetch(pop_today_rate, timeout=5)
srv.shutdown()
srv.server_close()
_expect(s_pop.ok and abs(s_pop.primary - 9.70621048) < 1e-6,
        f"popcorn-shape fetch should return 9.71, got {s_pop.primary!r} err={s_pop.error!r}")
_expect(s_pop.unit == "USD", f"unit should be USD, got {s_pop.unit!r}")
_expect(s_pop.detail == "$9.71", f"detail should be $9.71, got {s_pop.detail!r}")
_expect(s_today.ok and s_today.detail == "today $1.23",
        f"today_cost_path should add today cost detail, got {s_today.detail!r}")
_expect(s_today_rate.ok and s_today_rate.detail == "today $0.15",
        f"today_cost_rate should multiply standard today cost, got {s_today_rate.detail!r}")
print(f"[8/12] Popcorn-shape OK -> {s_pop.detail}")

# 9. minimax_coding_plan must not produce an empty Snapshot when the
# API returns a successful base_resp with an empty model_remains;
import providers as _providers
_orig_http = _providers._http_get
_providers._http_get = lambda url, headers, timeout: {
    "base_resp": {"status_code": 0, "status_msg": "ok"},
    "model_remains": [],
}
try:
    s_mm = fetch({"type": "minimax_coding_plan",
                  "api_key": "test-minimax-1234567890"}, timeout=5)
finally:
    _providers._http_get = _orig_http
_expect(not s_mm.ok, "minimax empty body should not be ok")
_expect(s_mm.detail == "no usage data",
        f"minimax empty body detail should be informative, got {s_mm.detail!r}")
print(f"[9/12] minimax empty body OK -> detail={s_mm.detail!r}")

# 10. minimax reset countdown: detail must contain a "reset in Xh Ym"
# string instead of the old "(rem X%)" leftover, derived from the
# remains_time field. Use a synthetic 4h30m window so we can assert
# the exact "reset in 4h 30m" substring.
_fake_body = {
    "base_resp": {"status_code": 0, "status_msg": "ok"},
    "model_remains": [{
        "model_name": "general",
        "current_interval_remaining_percent": 87,
        "current_weekly_remaining_percent": 95,
        "remains_time": 4 * 3600 * 1000 + 30 * 60 * 1000,
    }],
}
_providers._http_get = lambda url, headers, timeout: _fake_body
try:
    s_reset = fetch({"type": "minimax_coding_plan",
                     "api_key": "test-minimax-1234567890"}, timeout=5)
finally:
    _providers._http_get = _orig_http
_expect(s_reset.ok, "minimax with data should be ok")
_expect("reset in 4h 30m" in s_reset.detail,
        f"detail should contain 'reset in 4h 30m', got {s_reset.detail!r}")
_expect("(rem" not in s_reset.detail,
        f"detail should no longer contain '(rem X%)', got {s_reset.detail!r}")
_expect(abs(s_reset.primary - 13.0) < 1e-6,
        f"primary should be 13.0 (100-87), got {s_reset.primary!r}")
print(f"[10/12] minimax reset countdown OK -> {s_reset.detail!r}")

# 11. tooltip age format: wall-clock based, includes 'never' before first refresh.
app_age = monitor.App.__new__(monitor.App)
app_age._last_reload_error = None
app_age._fail_state = {}
app_age.snapshots = []
import time as _time
_now = _time.time()
for delta_s, expected in [
    (0,    "0s ago"),
    (45,   "45s ago"),
    (75,   "1m ago"),
    (3700, "1h ago"),
]:
    app_age._last_refresh_at = _now - delta_s
    got = app_age._format_age()
    _expect(got == expected,
            f"age delta={delta_s}s: expected {expected!r}, got {got!r}")
app_age._last_refresh_at = None
_expect(app_age._format_age() == "never",
        "age before first refresh should be 'never'")
print(f"[11/12] tooltip age format OK")

# 12. generic provider offset: cfg.offset must be added to the API value
# only in the value branch (currency / percent / number), never in the
# used/limit percent path, and the detail must show the raw value so
# the user can audit the math.
class _OffsetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"remaining": 9.65, "usage": {"today": {"cost": 1.2345}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

def _fetch_with_offset(handler_cls, cfg_extra):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = {
        "name": "MockPop", "type": "generic",
        "url": f"http://127.0.0.1:{port}/v1/usage",
        "api_key": "test-token-for-verify-1234567890",
        "value_path": "remaining", "format": "currency", "currency": "USD",
    }
    cfg.update(cfg_extra)
    s = fetch(cfg, timeout=5)
    srv.shutdown()
    srv.server_close()
    return s

# 12a. offset = -3.72 should turn 9.65 into 5.93 and the detail should
# show both the adjusted and raw value.
s_off = _fetch_with_offset(_OffsetHandler, {"offset": -3.72})
_expect(s_off.ok and abs(s_off.primary - 5.93) < 1e-6,
        f"offset=-3.72: expected 5.93, got {s_off.primary!r} err={s_off.error!r}")
_expect(s_off.unit == "USD", f"unit should be USD, got {s_off.unit!r}")
_expect("$5.93" in s_off.detail,
        f"detail should contain adjusted $5.93, got {s_off.detail!r}")
_expect("offset" not in s_off.detail and "API" not in s_off.detail,
        f"detail should not expose offset audit text, got {s_off.detail!r}")

s_off_today = _fetch_with_offset(_OffsetHandler, {"offset": -3.72, "today_cost_path": "usage.today.cost"})
_expect(s_off_today.detail == "today $1.23",
        f"today cost detail should omit offset audit text, got {s_off_today.detail!r}")

# 12b. without offset, behavior must be unchanged.
s_raw = _fetch_with_offset(_OffsetHandler, {})
_expect(s_raw.ok and abs(s_raw.primary - 9.65) < 1e-6,
        f"no offset: expected 9.65, got {s_raw.primary!r}")
_expect(s_raw.detail == "$9.65",
        f'no offset: detail should be exactly "$9.65", got {s_raw.detail!r}')

# 12c. offset must NOT touch the used/limit percent path.
class _PercentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"used": 7, "limit": 100}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

s_pct = _fetch_with_offset(_PercentHandler, {"offset": -3.72, "used_path": "used", "limit_path": "limit"})
_expect(s_pct.ok and abs(s_pct.primary - 7.0) < 1e-6,
        f"used/limit with offset: offset must be ignored, got {s_pct.primary!r}")
_expect("(raw" not in s_pct.detail,
        f"used/limit detail must not contain (raw ...) suffix, got {s_pct.detail!r}")
print(f"[12/12] generic offset OK -> {s_off.detail}")

print("ALL VERIFY CHECKS PASSED")
