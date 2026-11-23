"""Provider fetchers for coding-plan usage and API account balance.

Each provider returns a normalized Snapshot:
    primary: float | None  # headline number (percent 0-100, or balance)
    used:    float | None
    limit:   float | None
    unit:    str           # "percent" | "USD" | "CNY" | "tokens" | ...
    detail:  str           # short extra line for the popup
    ok:      bool
    error:   str | None
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests


# ---------- data model ----------

@dataclass
class Snapshot:
    primary: float | None = None
    used: float | None = None
    limit: float | None = None
    unit: str = "number"
    detail: str = ""
    ok: bool = False
    error: str | None = None
    hidden: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    # Structured quota windows for the usage card: label, pct (0-100),
    # reset_ms (epoch ms), primary. Empty for providers without windows.
    windows: list[dict[str, Any]] = field(default_factory=list)
    # Optional per-provider override: for currency units, treat primary
    # <= alert_below as 'alert'. None means use the built-in default.
    alert_below: float | None = None
    # Optional monthly budget for currency units. When set, the icon
    # progress bar is filled by (budget - primary) / budget so a low
    # remaining balance can be visualised as almost out, not just as
    # the absolute primary <= 1.0 threshold.
    budget: float | None = None
    # When True, the provider is still listed in the menu/tooltip but is
    # excluded from the worst-status pick that drives the tray color, so
    # providers the user wants to keep an eye on without letting them
    # dominate the icon (e.g. a known-low-balance account) can opt out.
    informational: bool = False
    # Failure-throttling state (set by App.refresh, not by providers):
    # when paused is True the snapshot is a placeholder produced because
    # the previous K cycles already failed; paused_remaining counts how
    # many more cycles to skip before the next real fetch; paused_total
    # is the K so the UI can show "X/K fails"; fail_count is the running
    # consecutive failure count for this provider.
    paused: bool = False
    paused_remaining: int = 0
    paused_total: int = 0
    fail_count: int = 0


# ---------- helpers ----------

def _path(obj: Any, path: str) -> Any:
    """Resolve a dotted path against nested dicts/lists. Supports integer
    indices, e.g. ``data.items.0.value``. Returns None on any miss."""
    if not path:
        return obj
    cur: Any = obj
    for seg in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(\.\d+)?", v)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _http_get(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"non-JSON response from {url}: {exc}") from exc


_PLACEHOLDER_TOKENS = ("REPLACE_ME", "REPLACE", "YOUR_TOKEN", "YOUR_KEY")


_PLACEHOLDER_BOUNDARY = re.compile(
    r"(?:^|[\s/=:,;])(" + "|".join(re.escape(t) for t in _PLACEHOLDER_TOKENS) + r")(?:$|[\s,;])"
)
_CURRENCY_SYMBOLS = {"USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£", "JPY": "¥"}


def _looks_like_placeholder(s: object) -> bool:
    """True if s is a known placeholder (or contains one as a separate token).

    Matches either the bare token (``"YOUR_TOKEN"``), the common
    example-config suffix pattern (``"sk-cp-REPLACE_ME"``), or an
    embedded token like ``"Bearer YOUR_TOKEN"``. Embedded substrings
    inside a real key (e.g. ``"sk-abcYOUR_TOKENdef"``) are ignored.
    """
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s:
        return True
    if s in _PLACEHOLDER_TOKENS:
        return True
    for tok in _PLACEHOLDER_TOKENS:
        if s.endswith("-" + tok) or s.endswith("_" + tok):
            return True
    if _PLACEHOLDER_BOUNDARY.search(s):
        return True
    return False


def _is_unconfigured(cfg: dict[str, Any]) -> bool:
    if cfg.get("disabled"):
        return True
    key = cfg.get("api_key") or cfg.get("admin_key") or ""
    if not _looks_like_placeholder(key):
        return False
    # Generic providers may keep auth inside headers instead of api_key.
    headers = cfg.get("headers") or {}
    for hv in headers.values():
        if not _looks_like_placeholder(hv):
            return False
    auth = headers.get("Authorization", "")
    if isinstance(auth, str) and "Bearer" in auth and len(auth) > 20:
        return False
    return True


def _unconfigured_snapshot(why: str) -> Snapshot:
    return Snapshot(ok=False, hidden=True, error=why)


def safe_err(exc: BaseException) -> str:
    """Render an exception as a short, safe string for the tray UI.

    Strips query strings and fragments from any URLs in the message, and
    redacts common auth-style ``key=value`` pairs, so API keys and signed
    URLs never appear in tooltips or context menus.
    """
    raw = str(exc) or exc.__class__.__name__
    # Collapse newlines so the redacted string stays a single line;
    # the tooltip and menu both render this on one row.
    raw = raw.replace("\r", " ").replace("\n", " ")
    # Drop query string and fragment from full URLs.
    raw = re.sub(r"(https?://[^\s?#]+)\?[^)\s]*", r"\1", raw)
    raw = re.sub(r"(https?://[^\s?#]+)#[^\s)]*", r"\1", raw)
    # Drop path/query from "url: /path?query" style fragments.
    raw = re.sub(r"(url:\s*)(?!https?://)[^\s)]+", r"\1(redacted)", raw, flags=re.IGNORECASE)
    # Redact common auth-style query parameters.
    raw = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret|signature|sig|password|passwd|pwd|token)\b\s*=\s*([^&\s]*)",
        r"\1=***",
        raw,
    )
    if len(raw) > 160:
        # Keep both ends so the host (head) and errno/cause (tail)
        # survive long urllib3 messages. Middle repeats (object
        # reprs, frame dumps) are noise on a single tray line.
        head_n = 90
        tail_n = 69  # 90 + 1 ellipsis + 69 = 160
        raw = raw[:head_n] + "\u2026" + raw[-tail_n:]
    return raw


def _format_currency(unit: str, value: float) -> str:
    unit = unit.upper()
    sym = _CURRENCY_SYMBOLS.get(unit, unit + " ")
    return f"{sym}{value:,.2f}"

# ---------- built-in providers ----------

def _snap_from(cfg: dict[str, Any], **kwargs) -> Snapshot:
    """Build a Snapshot, carrying over per-provider overrides for
    alert_below (currency threshold), monthly_budget (currency icon
    progress fill), and informational (tray-color opt-out)."""
    try:
        budget = cfg.get("monthly_budget")
        budget = float(budget) if budget is not None else None
    except (TypeError, ValueError):
        budget = None
    return Snapshot(
        alert_below=cfg.get("alert_below"),
        budget=budget,
        informational=bool(cfg.get("informational", False)),
        **kwargs,
    )




def fetch_openai(cfg: dict[str, Any], timeout: float) -> Snapshot:
    api_key = cfg.get("api_key") or ""
    if _is_unconfigured(cfg) or not api_key:
        return _unconfigured_snapshot("api_key missing")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        sub = _http_get(
            "https://api.openai.com/v1/dashboard/billing/subscription",
            headers, timeout,
        )
        grants = _http_get(
            "https://api.openai.com/v1/dashboard/billing/credit_grants",
            headers, timeout,
        )
    except Exception as exc:
        return _snap_from(cfg, ok=False, error=safe_err(exc))

    used_total = _to_float(_path(grants, "total_used"))
    granted = _to_float(_path(grants, "total_granted"))
    remaining = _to_float(_path(grants, "total_available"))
    soft_limit = _to_float(_path(sub, "hard_limit_usd"))

    primary = remaining
    if primary is None and granted is not None and used_total is not None:
        primary = granted - used_total

    detail_parts = []
    if used_total is not None:
        detail_parts.append(f"used ${used_total:.2f}")
    if soft_limit is not None:
        detail_parts.append(f"limit ${soft_limit:.2f}")
    detail = " / ".join(detail_parts)

    return _snap_from(cfg, 
        primary=primary,
        used=used_total,
        limit=soft_limit,
        unit="USD",
        detail=detail,
        ok=primary is not None,
        raw={"grants": grants, "sub": sub},
    )


def fetch_deepseek(cfg: dict[str, Any], timeout: float) -> Snapshot:
    api_key = cfg.get("api_key") or ""
    if _is_unconfigured(cfg) or not api_key:
        return _unconfigured_snapshot("api_key missing")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        data = _http_get("https://api.deepseek.com/user/balance", headers, timeout)
    except Exception as exc:
        return _snap_from(cfg, ok=False, error=safe_err(exc))

    avail = _to_float(_path(data, "balance_available"))
    total = _to_float(_path(data, "balance_total"))
    used = (total - avail) if (total is not None and avail is not None) else None
    detail = f"\u4f59\u989d \u00a5{avail:.2f}" if avail is not None else ""
    return _snap_from(cfg, 
        primary=avail,
        used=used,
        limit=total,
        unit="CNY",
        detail=detail,
        ok=avail is not None,
        raw=data,
    )


def fetch_openrouter(cfg: dict[str, Any], timeout: float) -> Snapshot:
    api_key = cfg.get("api_key") or ""
    if _is_unconfigured(cfg) or not api_key:
        return _unconfigured_snapshot("api_key missing")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        data = _http_get("https://openrouter.ai/api/v1/credits", headers, timeout)
    except Exception as exc:
        return _snap_from(cfg, ok=False, error=safe_err(exc))

    total = _to_float(_path(data, "data.total_credits"))
    used = _to_float(_path(data, "data.total_usage"))
    remaining = (total - used) if (total is not None and used is not None) else None
    detail = (
        f"used ${used:.4f} / total ${total:.2f}"
        if used is not None and total is not None
        else ""
    )
    return _snap_from(cfg, 
        primary=remaining,
        used=used,
        limit=total,
        unit="USD",
        detail=detail,
        ok=remaining is not None,
        raw=data,
    )


def fetch_anthropic_admin(cfg: dict[str, Any], timeout: float) -> Snapshot:
    """Anthropic has no public balance API for normal keys. With an admin key
    the usage is queryable per workspace. We fetch this month-to-date spend."""
    admin_key = cfg.get("admin_key") or ""
    if _is_unconfigured(cfg) or not admin_key:
        return _unconfigured_snapshot("admin_key missing")
    headers = {
        "x-api-key": admin_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        data = _http_get(
            "https://api.anthropic.com/v1/organizations/usage?limit=1",
            headers, timeout,
        )
    except Exception as exc:
        return _snap_from(cfg, ok=False, error=safe_err(exc))

    used = None
    for candidate in ("data.0.amount", "data.0.spend", "total_spend", "amount"):
        used = _to_float(_path(data, candidate))
        if used is not None:
            break
    return _snap_from(cfg, 
        primary=used,
        used=used,
        limit=None,
        unit="USD",
        detail="MTD spend (admin key)",
        ok=used is not None,
        raw=data,
    )


def fetch_generic(cfg: dict[str, Any], timeout: float) -> Snapshot:
    if _is_unconfigured(cfg):
        return _unconfigured_snapshot("not configured")
    url = cfg.get("url")
    if not url:
        return _unconfigured_snapshot("url missing")
    method = (cfg.get("method") or "GET").upper()
    headers = dict(cfg.get("headers") or {})
    api_key = cfg.get("api_key")
    if api_key and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.request(method, url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return _snap_from(cfg, ok=False, error=safe_err(exc))

    used = _to_float(_path(data, cfg.get("used_path", ""))) if cfg.get("used_path") else None
    limit = _to_float(_path(data, cfg.get("limit_path", ""))) if cfg.get("limit_path") else None
    value = _to_float(_path(data, cfg.get("value_path", ""))) if cfg.get("value_path") else None

    fmt = (cfg.get("format") or "number").lower()
    currency = cfg.get("currency", "USD")
    # Optional manual correction applied in the value branch only
    # (currency, percent, or number — not the used/limit percent path).
    # Lets you reconcile an API-returned value with your real-world
    # balance. Use a negative number to subtract from the API value.
    try:
        offset = float(cfg.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0.0

    primary: float | None
    unit: str
    detail: str
    detail_is_plain_primary = False
    if used is not None and limit:
        primary = (used / limit) * 100.0
        unit = "percent"
        detail = f"{used:g} / {limit:g}"
    elif value is not None:
        adjusted = value + offset if offset else value
        primary = adjusted
        if fmt == "currency":
            unit = currency.upper()
            detail = _format_currency(unit, adjusted)
            detail_is_plain_primary = True
        elif fmt == "percent":
            unit = "percent"
            detail = f"{adjusted:g}%"
            detail_is_plain_primary = True
        else:
            unit = cfg.get("unit", "number")
            detail = f"{adjusted:g} {unit}"
            detail_is_plain_primary = True
    else:
        primary = None
        unit = fmt
        detail = "no data extracted"

    extra_parts: list[str] = []
    today_cost_path = cfg.get("today_cost_path")
    if today_cost_path:
        today_cost = _to_float(_path(data, today_cost_path))
        if today_cost is not None:
            try:
                today_rate = float(cfg.get("today_cost_rate", 1.0))
            except (TypeError, ValueError):
                today_rate = 1.0
            today_label = str(cfg.get("today_cost_label") or "today")
            today_unit = str(cfg.get("today_cost_currency") or currency).upper()
            extra_parts.append(f"{today_label} {_format_currency(today_unit, today_cost * today_rate)}")

    if extra_parts:
        extra = " / ".join(extra_parts)
        detail = extra if detail_is_plain_primary else f"{detail} / {extra}"

    return _snap_from(cfg, 
        primary=primary,
        used=used,
        limit=limit,
        unit=unit,
        detail=detail,
        ok=primary is not None,
        raw=data,
    )


def fetch_minimax_coding_plan(cfg: dict[str, Any], timeout: float) -> Snapshot:
    """MiniMax / MiniMax Coding Plan (Token Plan).
    Endpoint: GET https://api.minimaxi.com/v1/coding_plan/remains
    Auth:     Authorization: Bearer <cp_key>
    The API returns remaining% for a 5h window and a weekly window.
    We invert both so the displayed value is "used%", which lets the
    standard color thresholds (>=85% red, >=60% yellow) work correctly.
    """
    api_key = cfg.get("api_key") or ""
    if _is_unconfigured(cfg) or not api_key:
        return _unconfigured_snapshot("api_key missing")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        data = _http_get(
            "https://api.minimaxi.com/v1/coding_plan/remains",
            headers, timeout,
        )
    except Exception as exc:
        return _snap_from(cfg, ok=False, error=safe_err(exc))

    base = _path(data, "base_resp") or {}
    if _to_float(_path(base, "status_code")) not in (0, None):
        return _snap_from(cfg, 
            ok=False,
            error=str(_path(base, "status_msg") or "api error"),
            raw=data,
        )

    intervals = _path(data, "model_remains") or []
    primary_used: float | None = None
    weekly_used: float | None = None
    model_label = ""
    reset_in_s: float | None = None  # seconds until 5h window resets
    if isinstance(intervals, list) and intervals:
        first = intervals[0]
        model_label = str(_path(first, "model_name") or "")
        rem5 = _to_float(_path(first, "current_interval_remaining_percent"))
        if rem5 is not None:
            primary_used = max(0.0, min(100.0, 100.0 - rem5))
        remw = _to_float(_path(first, "current_weekly_remaining_percent"))
        if remw is not None:
            weekly_used = max(0.0, min(100.0, 100.0 - remw))
        # remains_time is in milliseconds: ms until the current 5h
        # quota window ends and resets.
        rt = _to_float(_path(first, "remains_time"))
        if rt is not None:
            reset_in_s = max(0.0, rt / 1000.0)

    detail_parts: list[str] = []
    if primary_used is not None:
        detail_parts.append(f"5h {primary_used:.0f}% used")
        if reset_in_s is not None:
            mins = int(reset_in_s // 60)
            if mins >= 60:
                hrs = mins // 60
                rm = mins % 60
                reset_str = f"reset in {hrs}h {rm}m"
            elif mins >= 1:
                reset_str = f"reset in {mins}m"
            else:
                reset_str = "reset soon"
            detail_parts.append(reset_str)
    else:
        # Either model_remains is empty or none of the entries had
        # the expected percent fields. Tell the user we saw nothing
        # so an empty tray line is not interpreted as ok.
        detail_parts.append("no usage data")
    if weekly_used is not None:
        detail_parts.append(f"week {weekly_used:.0f}% used")
    if model_label:
        detail_parts.append(f"model={model_label}")

    return _snap_from(cfg, 
        primary=primary_used,
        used=primary_used,
        limit=100.0,
        unit="percent",
        detail=" | ".join(detail_parts),
        ok=primary_used is not None,
        raw=data,
    )


# ---------- Alibaba Cloud Bailian Token Plan ----------

# Token Plan quota has no public API-key endpoint: sk-sp keys only serve
# inference. The usage figure lives behind the same Console gateway that
# ``bl usage token-plan`` calls. These constants mirror bailian-cli 2.10.x.
_BAILIAN_USAGE_API = "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage"
_BAILIAN_GATEWAY = {
    ("cn-beijing", "domestic"): ("bailian-cs.console.aliyun.com", "BroadScopeAspnGateway"),
    ("cn-beijing", "international"): ("bailian-cs.console.alibabacloud.com", "BroadScopeAspnGateway"),
    ("ap-southeast-1", "domestic"): ("modelstudio-cs.console.aliyun.com", "IntlBroadScopeAspnGateway"),
    ("ap-southeast-1", "international"): ("bailian-singapore-cs.alibabacloud.com", "IntlBroadScopeAspnGateway"),
}
_BAILIAN_WINDOW_RE = re.compile(r"^per(\d+)(Hour|Day|Week|Month)(Percentage|ResetTime)$")
_BAILIAN_WINDOW_HOURS = {"Hour": 1, "Day": 24, "Week": 168, "Month": 720}
_BAILIAN_UNIT_LABEL = {"Hour": "hour", "Day": "day", "Week": "week", "Month": "month"}
_BAILIAN_ACS_ACTION = "GenerateCLIAccessToken"
_BAILIAN_ACS_VERSION = "2026-02-10"
_BAILIAN_ACS_PATH = "/modelstudio/cli/generateAccessToken"
_BAILIAN_MINT_HOST = {
    "cn-beijing": "modelstudio.cn-beijing.aliyuncs.com",
    "ap-southeast-1": "modelstudio.ap-southeast-1.aliyuncs.com",
}
# Minted console tokens live about 20 minutes; refresh before that so a
# refresh interval never races the expiry.
_BAILIAN_TOKEN_TTL_S = 15 * 60
_BAILIAN_TOKEN_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def _bailian_stored_config(cfg: dict[str, Any]) -> dict[str, Any]:
    path = cfg.get("bailian_config") or (Path.home() / ".bailian" / "config.json")
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _bailian_console_call(
    api: str,
    data: dict[str, Any],
    token: str,
    region: str,
    site: str,
    switch_agent: Any,
    timeout: float,
) -> dict[str, Any]:
    host, action = _BAILIAN_GATEWAY.get(
        (region, site), _BAILIAN_GATEWAY[("cn-beijing", "domestic")],
    )
    cornerstone: dict[str, Any] = {
        "protocol": "V2",
        "console": "ONE_CONSOLE",
        "productCode": "p_efm",
        "switchUserType": 3,
        "consoleSite": "BAILIAN_ALIYUN",
    }
    if switch_agent is not None:
        cornerstone["switchAgent"] = switch_agent
    params = json.dumps(
        {"Api": api, "V": "1.0", "Data": {**data, "cornerstoneParam": cornerstone}},
        separators=(",", ":"),
    )
    resp = requests.post(
        f"https://{host}/cli/api.json",
        params={"action": action, "product": "sfm_bailian", "api": api},
        data={"params": params, "region": region},
        headers={"Accept": "*/*", "Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _bailian_acs_headers(
    ak: str,
    sk: str,
    host: str,
    action: str,
    version: str,
    security_token: str | None = None,
) -> dict[str, str]:
    """ACS3-HMAC-SHA256 headers for the control-plane RPC action.

    Matches Alibaba's own client: POST, no query, no body, signed headers
    sorted, content-type included. The canonical headers block ends with a
    newline and the canonical request adds one more, as the ACS3 spec and
    the official SDKs do.
    """
    payload_hash = hashlib.sha256(b"").hexdigest()
    headers = {
        "host": host,
        "x-acs-action": action,
        "x-acs-version": version,
        "x-acs-date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "x-acs-signature-nonce": secrets.token_hex(16),
        "x-acs-content-sha256": payload_hash,
        "content-type": "application/json",
    }
    if security_token:
        headers["x-acs-security-token"] = security_token
    keys = sorted(headers)
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in keys)
    signed = ";".join(keys)
    canonical = "\n".join(
        ["POST", _BAILIAN_ACS_PATH, "", canonical_headers, signed, payload_hash],
    )
    to_sign = "ACS3-HMAC-SHA256\n" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    signature = hmac.new(
        sk.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    headers["authorization"] = (
        f"ACS3-HMAC-SHA256 Credential={ak},SignedHeaders={signed},Signature={signature}"
    )
    return headers


def _bailian_mint_token(
    ak: str,
    sk: str,
    region: str,
    security_token: str | None,
    timeout: float,
) -> str:
    host = _BAILIAN_MINT_HOST.get(region, _BAILIAN_MINT_HOST["cn-beijing"])
    headers = _bailian_acs_headers(
        ak, sk, host, _BAILIAN_ACS_ACTION, _BAILIAN_ACS_VERSION, security_token,
    )
    headers.pop("host", None)  # requests sets Host from the URL
    resp = requests.post(
        f"https://{host}{_BAILIAN_ACS_PATH}", headers=headers, timeout=timeout,
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    token = str(data.get("cliAccessToken") or "").strip() if isinstance(data, dict) else ""
    if resp.status_code < 300 and token:
        return token

    code = str(data.get("Code") or data.get("code") or f"HTTP {resp.status_code}")
    message = str(data.get("Message") or data.get("message") or "")
    message = message.replace(ak, "***")
    if "NotAuthorised" in code or "NotAuthorized" in code or "Forbidden" in code:
        message += (
            "; the AccessKey needs Token Plan read permission"
            " (main account, or a RAM user with AliyunTokenPlanReadOnlyAccess,"
            " AliyunBSSReadOnlyAccess, a custom policy for"
            " modelstudio:GenerateCLIAccessToken, and Model Studio subscription access)"
        )
    detail = f"{code} {message}".strip()
    raise RuntimeError(f"Alibaba Cloud {_BAILIAN_ACS_ACTION} failed: {detail}")


def _bailian_minted_token(
    ak: str,
    sk: str,
    region: str,
    security_token: str | None,
    timeout: float,
    force: bool = False,
) -> str:
    key = (ak, region)
    now = time.time()
    if not force:
        cached = _BAILIAN_TOKEN_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
    token = _bailian_mint_token(ak, sk, region, security_token, timeout)
    _BAILIAN_TOKEN_CACHE[key] = (now + _BAILIAN_TOKEN_TTL_S, token)
    return token


def _bailian_unwrap(payload: Any) -> Any:
    """Unwrap the Console DataV2 envelope, matching bailian-cli's
    unwrapResponse: {data:{DataV2:{data:{data:...}}}} -> innermost data."""
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    dv2 = data.get("DataV2")
    if isinstance(dv2, dict):
        inner = dv2.get("data")
        if isinstance(inner, dict) and "data" in inner:
            return inner.get("data")
        return inner if inner is not None else dv2
    inner = data.get("data")
    return inner if inner is not None else data


def _bailian_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "unexpected response"
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    code = data.get("errorCode")
    if data.get("success") is False and code:
        text = str(code)
        if "NotLogined" in text:
            return "console session expired; run: bl auth login --console"
        msg = data.get("errorMsg") or data.get("message") or text
        if "NotAuthorised" in text or "NotAuthorized" in text:
            msg = f"{msg}; check the AccessKey's Token Plan read permission"
        return f"{msg} (code={text})"
    return None


def _bailian_window_label(count: int, unit: str) -> str:
    return _BAILIAN_UNIT_LABEL[unit] if count == 1 else f"{count} {unit.lower()}"


def _bailian_window_title(count: int, unit: str) -> str:
    if unit == "Month":
        return "Monthly"
    if unit == "Week":
        return "Weekly"
    if unit == "Hour":
        return "5-hour" if count == 5 else f"{count}-hour"
    if unit == "Day":
        return "Daily" if count == 1 else f"{count}-day"
    return f"{count} {unit.lower()}"


def _bailian_reset_in(reset_ms: float | None) -> str:
    if reset_ms is None:
        return ""
    secs = int(reset_ms / 1000.0 - time.time())
    if secs <= 0:
        return "resetting now"
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


def fetch_bailian_token_plan(cfg: dict[str, Any], timeout: float) -> Snapshot:
    """Alibaba Cloud Bailian Token Plan (monthly Credits).

    There is no API-key quota endpoint. With an AccessKey this mints a
    fresh console token through GenerateCLIAccessToken and never needs a
    browser login; otherwise it falls back to the Console session written
    by ``bl auth login --console``, which expires. The gateway reports each
    quota window as per<N><Unit>Percentage (0-1 fraction) and ...ResetTime
    (epoch ms); the monthly window drives the tray value and the others are
    shown in the detail line.
    """
    if cfg.get("disabled"):
        return _unconfigured_snapshot("disabled")
    stored = _bailian_stored_config(cfg)
    region = str(cfg.get("console_region") or stored.get("console_region") or "cn-beijing")
    site = str(cfg.get("console_site") or stored.get("console_site") or "domestic")
    switch_agent = stored.get("console_switch_agent")

    ak = str(cfg.get("access_key_id") or stored.get("access_key_id") or "")
    sk = str(cfg.get("access_key_secret") or stored.get("access_key_secret") or "")
    if _looks_like_placeholder(ak) or _looks_like_placeholder(sk):
        ak = sk = ""
    security_token = cfg.get("security_token") or stored.get("security_token")
    token = str(cfg.get("access_token") or stored.get("access_token") or "")
    if _looks_like_placeholder(token):
        token = ""

    mint_error: str | None = None
    if ak and sk:
        try:
            token = _bailian_minted_token(ak, sk, region, security_token, timeout)
        except Exception as exc:
            mint_error = safe_err(exc)
            if not token:
                return _snap_from(cfg, ok=False, error=mint_error)
    if not token:
        return _unconfigured_snapshot("no Bailian console session; run bl auth login --console")

    try:
        payload = _bailian_console_call(
            _BAILIAN_USAGE_API, {}, token, region, site, switch_agent, timeout,
        )
    except Exception as exc:
        return _snap_from(cfg, ok=False, error=safe_err(exc))

    err = _bailian_error(payload)
    if err and "session expired" in err and ak and sk and not mint_error:
        try:
            token = _bailian_minted_token(
                ak, sk, region, security_token, timeout, force=True,
            )
            payload = _bailian_console_call(
                _BAILIAN_USAGE_API, {}, token, region, site, switch_agent, timeout,
            )
            err = _bailian_error(payload)
        except Exception as exc:
            mint_error = safe_err(exc)
    if err:
        if mint_error:
            err = f"{err} (AccessKey refresh: {mint_error})"
        return _snap_from(cfg, ok=False, error=err, raw=payload)

    body = _bailian_unwrap(payload)
    windows: dict[tuple[int, str], dict[str, float | None]] = {}
    if isinstance(body, dict):
        for key, value in body.items():
            match = _BAILIAN_WINDOW_RE.match(str(key))
            if not match:
                continue
            count, unit, kind = int(match.group(1)), match.group(2), match.group(3)
            windows.setdefault((count, unit), {})[kind] = _to_float(value)

    parsed: list[dict[str, Any]] = []
    for (count, unit), entry in windows.items():
        pct = entry.get("Percentage")
        reset = entry.get("ResetTime")
        if pct is None and reset is None:
            continue
        pct_used = None if pct is None else max(0.0, min(100.0, pct * 100.0))
        parsed.append({"count": count, "unit": unit, "pct": pct_used, "reset": reset})
    parsed.sort(key=lambda w: w["count"] * _BAILIAN_WINDOW_HOURS[w["unit"]])

    primary_window = next(
        (w for w in parsed if w["unit"] == "Month" and w["pct"] is not None),
        None,
    )
    if primary_window is None:
        primary_window = next(
            (w for w in reversed(parsed) if w["pct"] is not None), None,
        )
    primary = primary_window["pct"] if primary_window else None

    if primary is None:
        detail = "no quota data" if not parsed else "no usage percentage"
        return _snap_from(
            cfg, primary=None, unit="percent", detail=detail,
            ok=False, error=detail, raw=payload,
        )

    primary_label = _bailian_window_label(primary_window["count"], primary_window["unit"])
    parts = [f"{primary_label} {primary:.0f}% used"]
    others = [
        f"{_bailian_window_label(w['count'], w['unit'])} {w['pct']:.0f}%"
        for w in parsed if w is not primary_window and w["pct"] is not None
    ]
    if others:
        parts.append(" ".join(others))
    reset_text = _bailian_reset_in(primary_window.get("reset"))
    if reset_text == "resetting now":
        parts.append(reset_text)
    elif reset_text:
        parts.append(f"resets in {reset_text}")

    windows = [
        {
            "label": _bailian_window_title(w["count"], w["unit"]),
            "pct": w["pct"],
            "reset_ms": w.get("reset"),
            "primary": w is primary_window,
        }
        for w in parsed
        if w["pct"] is not None
    ]
    return _snap_from(
        cfg,
        primary=primary,
        used=primary,
        limit=100.0,
        unit="percent",
        detail=" | ".join(parts),
        ok=True,
        raw=payload,
        windows=windows,
    )


REGISTRY: dict[str, Callable[[dict[str, Any], float], Snapshot]] = {
    "openai": fetch_openai,
    "deepseek": fetch_deepseek,
    "openrouter": fetch_openrouter,
    "anthropic_admin": fetch_anthropic_admin,
    "anthropic": fetch_anthropic_admin,
    "generic": fetch_generic,
    "http": fetch_generic,
    "minimax_coding_plan": fetch_minimax_coding_plan,
    "minimax": fetch_minimax_coding_plan,
    "bailian_token_plan": fetch_bailian_token_plan,
    "bailian": fetch_bailian_token_plan,
}


def fetch(cfg: dict[str, Any], timeout: float = 10.0) -> Snapshot:
    kind = (cfg.get("type") or "generic").lower()
    fn = REGISTRY.get(kind, fetch_generic)
    return fn(cfg, timeout)


