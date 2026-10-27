from __future__ import annotations

import asyncio
import shlex
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # type: ignore
    from fastapi.middleware.cors import CORSMiddleware  # type: ignore
    from fastapi.responses import FileResponse, JSONResponse  # type: ignore
    from fastapi.staticfiles import StaticFiles  # type: ignore
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int = 400, detail: str = ""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class WebSocket:  # type: ignore
        pass

    class WebSocketDisconnect(Exception):  # type: ignore
        pass

    class CORSMiddleware:  # type: ignore
        pass

    class StaticFiles:  # type: ignore
        def __init__(self, **_: Any):
            pass

    def FileResponse(path: str) -> Any:  # type: ignore
        return Path(path).read_text(encoding="utf-8")

    class JSONResponse(dict):  # type: ignore
        def __init__(self, content: Any, status_code: int = 200):
            super().__init__(content if isinstance(content, dict) else {"data": content})
            self.status_code = status_code

    class FastAPI:  # type: ignore
        def __init__(self, **_: Any):
            self.routes: List[Any] = []

        def add_middleware(self, *_: Any, **__: Any) -> None:
            pass

        def on_event(self, *_: Any) -> Callable[[ Any], Any]:
            return lambda fn: fn

        def get(self, path: str, **_: Any) -> Callable[[Any], Any]:
            def _dec(fn: Any) -> Any:
                self.routes.append(("GET", path, fn))
                return fn
            return _dec

        def post(self, path: str, **_: Any) -> Callable[[Any], Any]:
            def _dec(fn: Any) -> Any:
                self.routes.append(("POST", path, fn))
                return fn
            return _dec

        def websocket(self, *_: Any) -> Callable[[Any], Any]:
            return lambda fn: fn

        def mount(self, *_: Any, **__: Any) -> None:
            pass


_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from pac_neon.adapters import AdapterRegistry, GenericPluginAdapter, register_default_adapters
from pac_neon.adapters.antigravity_adapter import AntigravityAdapter
from pac_neon.adapters.claude_adapter import ClaudeAdapter
from pac_neon.models import (
    AgentGameState,
    AgentUnit,
    ArchiveAgentRequest,
    ContextFruit,
    DirectCommandRequest,
    HookPermissionPayload,
    NeonCliActionRequest,
    PermissionRequestItem,
    PermissionResolutionAction,
    ProviderMetadata,
    RenameWorkspaceRequest,
    ResolvePermissionRequest,
    SelfHandoffRequest,
    SendWarpFruitRequest,
    SpawnAgentRequest,
    utc_now_iso,
)
from pac_neon.neon_cli_bridge import NeonCliBridge, install_pac_neon_agent_hooks
from pac_neon.pty_manager import PTYSessionManager
from pac_neon.superset_bridge import SupersetBridge

register_default_adapters()

import re as _re_origin

_LOCAL_ORIGIN_RE = _re_origin.compile(
    r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/.*)?$",
    _re_origin.IGNORECASE,
)


def _is_trusted_local_origin(origin_or_referer: Optional[str]) -> bool:
    """Allows CLI/Hook requests (no Origin) and browser requests strictly from localhost / 127.0.0.1."""
    val = (origin_or_referer or "").strip()
    if not val or val == "null":
        return True
    return bool(_LOCAL_ORIGIN_RE.match(val))


app = FastAPI(
    title="PAC-NEON // Superset + Antigravity Arcade Cockpit",
    version="0.4.0",
    description="Cockpit gamificado estilo Pac-Man com tokens Neon acoplado ao Superset CLI (worktrees/terminals) + Google Antigravity.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

if HAS_FASTAPI and hasattr(app, "middleware"):
    @app.middleware("http")
    async def _localhost_csrf_guard(request: Any, call_next: Any) -> Any:
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin and not _is_trusted_local_origin(origin):
                return JSONResponse(
                    {"ok": False, "detail": "Origem externa bloqueada por segurança local (CSRF guard)."},
                    status_code=403,
                )
        return await call_next(request)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE_FILE = Path.home() / ".pac_neon" / "workspace_state.json"

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser

import getpass
import shutil
import sqlite3
import ssl
import urllib.request
from datetime import datetime, timezone

_LAST_BROWSER_POPUP_TS: float = 0.0


def trigger_cockpit_popup(
    reason_title: str = "PAC-NEON // Power Pellet Alert",
    reason_body: str = "Agente aguardando aprovação [Y/N] no Cockpit!",
    open_browser: bool = True,
    url: str = "http://127.0.0.1:8765",
    silent: bool = False,
) -> None:
    """Sends a macOS native banner notification (with or without sound) and pops open/focuses the Pac-Neon browser tab."""
    global _LAST_BROWSER_POPUP_TS
    if os.environ.get("PAC_NEON_NO_OPEN") == "1":
        return

    hub_inst = globals().get("hub")
    is_silent = silent
    if not is_silent and hub_inst is not None and hasattr(hub_inst, "meeting_guard"):
        try:
            m_st = hub_inst.meeting_guard.get_status()
            if m_st.get("should_mute_macos_sound"):
                is_silent = True
        except Exception:
            pass

    # 1. Native macOS notification (silenced when user is in a meeting and mute_macos_sound is enabled)
    if sys.platform == "darwin":
        safe_title = reason_title.replace('"', "'")
        safe_body = reason_body.replace('"', "'")[:110]
        sound_clause = "" if is_silent else ' sound name "Glass"'
        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'display notification "{safe_body}" with title "{safe_title}"{sound_clause}',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # 2. Auto-open / focus browser (throttled to at most once every 12 seconds)
    now = time.time()
    if open_browser and (now - _LAST_BROWSER_POPUP_TS > 12.0):
        _LAST_BROWSER_POPUP_TS = now
        try:
            webbrowser.open(url, new=0, autoraise=True)
        except Exception:
            pass


class MeetingCalendarGuard:
    """Per-user Calendar & Active Call Meeting Detector for Neon Collaborators.

    Detects whether the local user is currently in a meeting via 4 configurable layers:
      1. Manual 'In Meeting' quick toggle per user
      2. Active audio/video call detection on macOS (Google Meet / WebRTC, Zoom, MS Teams, Slack Huddle)
      3. Private/Corporate .ics Calendar feed URL (Google Calendar / Outlook / Apple Calendar)
      4. macOS native Calendar (icalBuddy or ~/Library/Calendars/Calendar.sqlitedb)
    """

    DEFAULT_SETTINGS: Dict[str, Any] = {
        "meeting_mute_enabled": True,
        "detect_active_call": True,
        "detect_macos_calendar": True,
        "calendar_ics_url": "",
        "mute_web_sfx": True,
        "mute_macos_sound": True,
        "manual_in_meeting": False,
    }

    def __init__(self) -> None:
        self.current_user: str = self._detect_current_user()
        self.settings: Dict[str, Any] = dict(self.DEFAULT_SETTINGS)
        self._ics_cache_ts: float = 0.0
        self._ics_cached_url: str = ""
        self._ics_events_today: List[Dict[str, Any]] = []
        self._ics_status: str = "Não configurado"
        self._macos_cal_cache_ts: float = 0.0
        self._macos_cal_events_today: List[Dict[str, Any]] = []
        self._call_cache_ts: float = 0.0
        self._active_call_label: Optional[str] = None
        self._status_cache_ts: float = 0.0
        self._cached_status: Optional[Dict[str, Any]] = None

    @staticmethod
    def _detect_current_user() -> str:
        try:
            return getpass.getuser() or os.environ.get("USER") or "colaborador"
        except Exception:
            return os.environ.get("USER") or "colaborador"

    def load_from_dict(self, data: Dict[str, Any]) -> None:
        user_map = data.get("user_meeting_settings")
        if isinstance(user_map, dict) and isinstance(user_map.get(self.current_user), dict):
            raw = user_map[self.current_user]
        elif isinstance(data.get("meeting_settings"), dict):
            raw = data["meeting_settings"]
        else:
            raw = {}
        merged = dict(self.DEFAULT_SETTINGS)
        for k in self.DEFAULT_SETTINGS:
            if k in raw and raw[k] is not None:
                if isinstance(self.DEFAULT_SETTINGS[k], bool):
                    merged[k] = bool(raw[k])
                else:
                    merged[k] = str(raw[k]).strip()
        self.settings = merged
        self._status_cache_ts = 0.0

    def dump_for_storage(self, existing_map: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        out = dict(existing_map) if isinstance(existing_map, dict) else {}
        out[self.current_user] = dict(self.settings)
        return out

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        old_url = self.settings.get("calendar_ics_url", "")
        for k, default_val in self.DEFAULT_SETTINGS.items():
            if k in patch and patch[k] is not None:
                if isinstance(default_val, bool):
                    self.settings[k] = bool(patch[k])
                else:
                    self.settings[k] = str(patch[k]).strip()
        if self.settings.get("calendar_ics_url", "") != old_url:
            self._ics_cache_ts = 0.0
        self._status_cache_ts = 0.0
        return dict(self.settings)

    def _detect_active_macos_call(self, now: float) -> Optional[str]:
        """Detects active audio/video calls on macOS via pmset assertions (Zoom, Meet/WebRTC, MS Teams, Slack Huddle)."""
        if sys.platform != "darwin":
            return None
        if (now - self._call_cache_ts) < 6.0:
            return self._active_call_label
        self._call_cache_ts = now
        detected: Optional[str] = None
        try:
            res = subprocess.run(
                ["pmset", "-g", "assertions"],
                capture_output=True,
                text=True,
                timeout=1.2,
            )
            out = res.stdout or ""
            for line in out.splitlines():
                ln = line.strip()
                if "PreventUserIdleDisplaySleep" not in ln and "NoDisplaySleepAssertion" not in ln:
                    continue
                low = ln.lower()
                if "cpthost" in low or "zoom.us" in low:
                    detected = "Chamada ativa no Zoom"
                    break
                if "msteams" in low or "microsoft teams" in low or "teams call" in low:
                    detected = "Reunião ativa no Microsoft Teams"
                    break
                if "webrtc" in low or "google meet" in low or "meet.google.com" in low:
                    detected = "Reunião ativa no Google Meet / WebRTC"
                    break
                if "slack" in low and ("huddle" in low or "call" in low or "webrtc" in low or "preventuseridledisplaysleep" in low):
                    detected = "Huddle / Chamada ativa no Slack"
                    break
                if "webex" in low:
                    detected = "Reunião ativa no Webex"
                    break
        except Exception:
            pass
        self._active_call_label = detected
        return detected

    @staticmethod
    def _parse_ics_datetime(raw_val: str, is_utc: bool) -> Optional[datetime]:
        val = raw_val.strip()
        if val.endswith("Z"):
            is_utc = True
            val = val[:-1]
        if len(val) == 8 and val.isdigit():
            # All-day date YYYYMMDD -> skip for meeting mute
            return None
        if len(val) >= 15 and "T" in val:
            core = val[:15]
            try:
                dt = datetime.strptime(core, "%Y%m%dT%H%M%S")
                if is_utc:
                    dt_utc = dt.replace(tzinfo=timezone.utc)
                    return dt_utc.astimezone().replace(tzinfo=None)
                return dt
            except Exception:
                return None
        return None

    def _fetch_and_parse_ics(self, now: float, force: bool = False) -> List[Dict[str, Any]]:
        raw_urls = str(self.settings.get("calendar_ics_url") or "").strip()
        if not raw_urls:
            self._ics_status = "Nenhum link .ics configurado"
            self._ics_events_today = []
            return []

        if not force and raw_urls == self._ics_cached_url and (now - self._ics_cache_ts) < 60.0:
            return self._ics_events_today

        self._ics_cache_ts = now
        self._ics_cached_url = raw_urls
        urls = [u.strip() for u in raw_urls.replace("\n", ",").split(",") if u.strip()]
        today_local = datetime.now()
        today_date = today_local.date()
        weekday_codes = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
        today_wk = weekday_codes[today_local.weekday()]

        events_today: List[Dict[str, Any]] = []
        errors: List[str] = []

        for u in urls:
            fetch_url = "https://" + u[len("webcal://"):] if u.startswith("webcal://") else u
            if not (fetch_url.startswith("https://") or fetch_url.startswith("http://")):
                errors.append("URL inválida (use https:// ou webcal://)")
                continue
            raw_text = ""
            try:
                req = urllib.request.Request(fetch_url, headers={"User-Agent": "PacNeon-CalendarGuard/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=4.5) as resp:
                        raw_text = resp.read().decode("utf-8", errors="replace")
                except Exception:
                    # Fallback for corporate Zscaler SSL interception on Neon Macs
                    ctx = ssl._create_unverified_context()
                    with urllib.request.urlopen(req, timeout=4.5, context=ctx) as resp:
                        raw_text = resp.read().decode("utf-8", errors="replace")
            except Exception as exc:
                errors.append(str(exc)[:80])
                continue

            # Unfold RFC 5545 folded lines
            unfolded: List[str] = []
            for line in raw_text.splitlines():
                if line.startswith((" ", "\t")) and unfolded:
                    unfolded[-1] += line[1:]
                else:
                    unfolded.append(line.rstrip("\r"))

            in_event = False
            ev: Dict[str, Any] = {}
            for line in unfolded:
                if line == "BEGIN:VEVENT":
                    in_event = True
                    ev = {"declined": False}
                    continue
                if line == "END:VEVENT":
                    in_event = False
                    if ev.get("status") == "CANCELLED" or ev.get("transp") == "TRANSPARENT" or ev.get("declined"):
                        continue
                    dt_start: Optional[datetime] = ev.get("dtstart")
                    dt_end: Optional[datetime] = ev.get("dtend")
                    summary = str(ev.get("summary") or "Reunião na Agenda").strip()
                    rrule = str(ev.get("rrule") or "").upper()
                    if dt_start and dt_end and dt_end > dt_start:
                        duration = dt_end - dt_start
                        # Ignore events >= 20 hours (all-day / OOO banners)
                        if duration.total_seconds() >= 20 * 3600:
                            continue
                        matched_start: Optional[datetime] = None
                        matched_end: Optional[datetime] = None
                        if dt_start.date() <= today_date <= dt_end.date():
                            matched_start = dt_start
                            matched_end = dt_end
                        elif rrule and dt_start.date() <= today_date:
                            # Check UNTIL in RRULE if present
                            until_ok = True
                            if "UNTIL=" in rrule:
                                until_part = rrule.split("UNTIL=", 1)[1].split(";", 1)[0].strip()
                                until_dt = self._parse_ics_datetime(until_part, until_part.endswith("Z"))
                                if until_dt and until_dt.date() < today_date:
                                    until_ok = False
                            if until_ok:
                                is_daily = "FREQ=DAILY" in rrule
                                is_weekly = "FREQ=WEEKLY" in rrule
                                byday_match = False
                                if "BYDAY=" in rrule:
                                    byday_str = rrule.split("BYDAY=", 1)[1].split(";", 1)[0]
                                    byday_match = today_wk in byday_str.split(",")
                                elif is_weekly:
                                    byday_match = (dt_start.weekday() == today_local.weekday())
                                if is_daily or (is_weekly and byday_match):
                                    matched_start = datetime.combine(today_date, dt_start.time())
                                    matched_end = matched_start + duration
                        if matched_start and matched_end:
                            events_today.append({
                                "title": summary,
                                "start_ts": matched_start.timestamp(),
                                "end_ts": matched_end.timestamp(),
                                "start_str": matched_start.strftime("%H:%M"),
                                "end_str": matched_end.strftime("%H:%M"),
                                "source": "ics",
                            })
                    continue

                if not in_event or ":" not in line:
                    continue
                prop, val = line.split(":", 1)
                prop_upper = prop.upper()
                if prop_upper.startswith("SUMMARY"):
                    ev["summary"] = val.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ")
                elif prop_upper.startswith("STATUS"):
                    ev["status"] = val.strip().upper()
                elif prop_upper.startswith("TRANSP"):
                    ev["transp"] = val.strip().upper()
                elif prop_upper.startswith("RRULE"):
                    ev["rrule"] = val.strip()
                elif prop_upper.startswith("ATTENDEE") and "PARTSTAT=DECLINED" in prop_upper:
                    if self.current_user.lower() in val.lower():
                        ev["declined"] = True
                elif prop_upper.startswith("DTSTART"):
                    if "VALUE=DATE" in prop_upper and "DATE-TIME" not in prop_upper:
                        continue
                    ev["dtstart"] = self._parse_ics_datetime(val, "TZID=" not in prop_upper and val.endswith("Z"))
                elif prop_upper.startswith("DTEND"):
                    if "VALUE=DATE" in prop_upper and "DATE-TIME" not in prop_upper:
                        continue
                    ev["dtend"] = self._parse_ics_datetime(val, "TZID=" not in prop_upper and val.endswith("Z"))

        events_today.sort(key=lambda x: x["start_ts"])
        self._ics_events_today = events_today
        if errors and not events_today:
            self._ics_status = f"Erro ao ler .ics: {errors[0]}"
        else:
            self._ics_status = f"Sincronizado ({len(events_today)} reunião(ões) hoje)"
        return events_today

    def _detect_macos_calendar_events(self, now: float) -> List[Dict[str, Any]]:
        """Reads today's meetings from macOS Calendar SQLite cache or icalBuddy without prompting dialogs."""
        if sys.platform != "darwin":
            return []
        if (now - self._macos_cal_cache_ts) < 30.0:
            return self._macos_cal_events_today
        self._macos_cal_cache_ts = now
        events: List[Dict[str, Any]] = []

        # Check macOS Calendar SQLite DB (read-only)
        cal_dbs = [
            Path.home() / "Library" / "Group Containers" / "group.com.apple.calendar" / "Calendar.sqlitedb",
            Path.home() / "Library" / "Calendars" / "Calendar.sqlitedb",
        ]
        # CoreData epoch is 2001-01-01 00:00:00 UTC (978307200 unix timestamp)
        APPLE_EPOCH_OFFSET = 978307200.0
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - APPLE_EPOCH_OFFSET
        today_end = today_start + 86400.0

        for db_path in cal_dbs:
            if not db_path.exists():
                continue
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.8)
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT ci.summary, o.occurrence_date, o.occurrence_end_date, ci.all_day
                    FROM OccurrenceCache o
                    JOIN CalendarItem ci ON o.event_id = ci.ROWID
                    WHERE o.occurrence_date <= ? AND o.occurrence_end_date >= ?
                    """,
                    (today_end, today_start),
                )
                for summary, occ_start, occ_end, all_day in cur.fetchall():
                    if all_day:
                        continue
                    s_unix = float(occ_start) + APPLE_EPOCH_OFFSET
                    e_unix = float(occ_end) + APPLE_EPOCH_OFFSET
                    if (e_unix - s_unix) >= 20 * 3600 or e_unix <= s_unix:
                        continue
                    s_dt = datetime.fromtimestamp(s_unix)
                    e_dt = datetime.fromtimestamp(e_unix)
                    events.append({
                        "title": str(summary or "Reunião (Calendário Mac)"),
                        "start_ts": s_unix,
                        "end_ts": e_unix,
                        "start_str": s_dt.strftime("%H:%M"),
                        "end_str": e_dt.strftime("%H:%M"),
                        "source": "macos_calendar",
                    })
                conn.close()
                if events:
                    break
            except Exception:
                continue

        events.sort(key=lambda x: x["start_ts"])
        self._macos_cal_events_today = events
        return events

    def get_status(self, force_refresh: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force_refresh and self._cached_status is not None and (now - self._status_cache_ts) < 5.0:
            return self._cached_status

        enabled = bool(self.settings.get("meeting_mute_enabled", True))
        manual = bool(self.settings.get("manual_in_meeting", False))
        active_title = ""
        active_source = "none"
        all_events: List[Dict[str, Any]] = []

        # 1. ICS feed events
        if self.settings.get("calendar_ics_url"):
            ics_ev = self._fetch_and_parse_ics(now, force=force_refresh)
            all_events.extend(ics_ev)
        else:
            self._ics_status = "Não configurado"

        # 2. macOS Calendar events
        if self.settings.get("detect_macos_calendar", True):
            mac_ev = self._detect_macos_calendar_events(now)
            all_events.extend(mac_ev)

        # Deduplicate events by (title, start_str)
        dedup_events: List[Dict[str, Any]] = []
        seen_ev = set()
        for ev in sorted(all_events, key=lambda x: x["start_ts"]):
            key = (ev["title"].lower(), ev["start_str"], ev["end_str"])
            if key not in seen_ev:
                seen_ev.add(key)
                ev_copy = dict(ev)
                ev_copy["is_now"] = bool(ev["start_ts"] <= now <= ev["end_ts"])
                dedup_events.append(ev_copy)

        # Determine if currently in a meeting
        if manual:
            active_title = "Modo Reunião Manual (Ativado pelo Usuário)"
            active_source = "manual"
        else:
            for ev in dedup_events:
                if ev.get("is_now"):
                    active_title = f"{ev['title']} ({ev['start_str']}–{ev['end_str']})"
                    active_source = ev["source"]
                    break
            if not active_title and self.settings.get("detect_active_call", True):
                call_lbl = self._detect_active_macos_call(now)
                if call_lbl:
                    active_title = call_lbl
                    active_source = "active_call"

        in_meeting = bool(active_title)
        should_mute_sound = bool(enabled and in_meeting)

        status = {
            "in_meeting": in_meeting,
            "should_mute_sound": should_mute_sound,
            "should_mute_web_sfx": bool(should_mute_sound and self.settings.get("mute_web_sfx", True)),
            "should_mute_macos_sound": bool(should_mute_sound and self.settings.get("mute_macos_sound", True)),
            "active_meeting_title": active_title,
            "source": active_source,
            "current_user": self.current_user,
            "ics_status": self._ics_status,
            "events_today": dedup_events[:12],
            "settings": dict(self.settings),
        }
        self._cached_status = status
        self._status_cache_ts = now
        return status



_TTY_SCREEN_CACHE: Dict[str, tuple[float, List[str]]] = {}


def _capture_macos_terminal_screen(tty_name: str) -> List[str]:
    """Reads the live visible text buffer of a Terminal.app tab or iTerm2 session matching tty_name."""
    if sys.platform != "darwin" or not tty_name:
        return []
    short_tty = tty_name.replace("/dev/", "").strip()
    if not short_tty or short_tty == "??":
        return []

    now = time.time()
    cached = _TTY_SCREEN_CACHE.get(short_tty)
    if cached and (now - cached[0]) < 2.0:
        return cached[1]

    lines_out: List[str] = []
    has_iterm_app = Path("/Applications/iTerm.app").exists() or (Path.home() / "Applications" / "iTerm.app").exists()
    script = f'''
    if application "Terminal" is running then
        tell application "Terminal"
            repeat with w in windows
                repeat with t in tabs of w
                    try
                        if (tty of t) contains "{short_tty}" then
                            return history of t
                        end if
                    end try
                end repeat
            end repeat
        end tell
    end if
    '''
    if has_iterm_app:
        script += f'''
        if application "iTerm" is running then
            tell application "iTerm"
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            try
                                if (tty of s) contains "{short_tty}" then
                                    return contents of s
                                end if
                            end try
                        end repeat
                    end repeat
                end repeat
            end tell
        end if
        '''
    script += '\nreturn ""\n'
    try:
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=1.5)
        raw = res.stdout or ""
        if raw.strip():
            lines_out = [ln.rstrip() for ln in raw.splitlines() if ln.strip()][-800:]
    except Exception:
        pass

    _TTY_SCREEN_CACHE[short_tty] = (now, lines_out)
    return lines_out


def _is_macos_accessibility_trusted(prompt_user: bool = False) -> bool:
    """Checks (and optionally prompts for) macOS Accessibility permission required by System Events keystroke."""
    if sys.platform != "darwin":
        return True
    try:
        import ctypes
        import ctypes.util

        app_services_path = ctypes.util.find_library("ApplicationServices")
        core_foundation_path = ctypes.util.find_library("CoreFoundation")
        if not app_services_path:
            return False
        app_services = ctypes.cdll.LoadLibrary(app_services_path)
        if prompt_user and core_foundation_path:
            cf = ctypes.cdll.LoadLibrary(core_foundation_path)
            kAXTrustedCheckOptionPrompt = ctypes.c_void_p.in_dll(
                app_services, "kAXTrustedCheckOptionPrompt"
            )
            kCFBooleanTrue = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
            cf.CFDictionaryCreate.restype = ctypes.c_void_p
            cf.CFDictionaryCreate.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            keys = (ctypes.c_void_p * 1)(kAXTrustedCheckOptionPrompt)
            vals = (ctypes.c_void_p * 1)(kCFBooleanTrue)
            opts = cf.CFDictionaryCreate(None, keys, vals, 1, None, None)
            app_services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
            app_services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
            return bool(app_services.AXIsProcessTrustedWithOptions(opts))
        app_services.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(app_services.AXIsProcessTrusted())
    except Exception:
        return False


class ArcadeOrchestratorHub:
    """Central state hub managing ONLY the developer's real agents, history/archive drawer, and synchronized workspace titles."""

    def __init__(self) -> None:
        self.agents: Dict[str, AgentUnit] = {}
        self.warp_fruits: List[ContextFruit] = []
        self.pty_manager = PTYSessionManager()
        self.superset_bridge = SupersetBridge()
        self.neon_cli = NeonCliBridge()
        self.hook_waiters: Dict[str, asyncio.Future] = {}
        self.hook_events: Dict[str, threading.Event] = {}
        self.agent_hook_events: Dict[str, str] = {}
        self.hook_suggestions: Dict[str, list] = {}
        self.hook_results: Dict[str, dict] = {}
        self.hook_state_overrides: Dict[str, Dict[str, Any]] = {}
        self.active_panel_commands: Dict[str, float] = {}
        self.active_panel_procs: Dict[str, subprocess.Popen] = {}
        self.recent_panel_lines: Dict[str, List[tuple[float, str]]] = {}
        self.bridge_input_queues: Dict[str, List[str]] = {}
        self.ws_clients: Set[WebSocket] = set()
        self.high_score: int = 0
        self.archived_overrides: Dict[str, bool] = {}
        self.archived_snapshots: Dict[str, Dict[str, Any]] = {}
        self.custom_titles: Dict[str, str] = {}
        self.dismissed_perm_mtime: Dict[str, float] = {}
        self.notified_perm_timestamps: Dict[str, float] = {}
        self.self_handoff_overrides: Dict[str, float] = {}
        self.notification_timeout_sec: float = 60.0
        self.meeting_guard = MeetingCalendarGuard()
        self.user_meeting_settings_map: Dict[str, Any] = {}
        self._sync_lock = threading.Lock()
        self._load_workspace_state()
        install_pac_neon_agent_hooks(8765)
        if "--daemon" not in sys.argv:
            threading.Thread(target=self._background_sync_loop, daemon=True).start()

    def _background_sync_loop(self) -> None:
        """Continuously synchronizes live agent statuses every 1.0s in the background."""
        while True:
            try:
                self.sync_live_agents(force=True)
            except Exception:
                pass
            time.sleep(1.0)

    def record_panel_line(self, agent_id: str, line: str) -> None:
        """Records a terminal line generated from the Pac-Neon panel so transcript polling doesn't overwrite it."""
        now = time.time()
        bucket = [item for item in self.recent_panel_lines.get(agent_id, []) if (now - item[0]) < 180.0]
        bucket.append((now, line))
        self.recent_panel_lines[agent_id] = bucket[-80:]
        ag = self.agents.get(agent_id)
        if ag is not None:
            ag.terminal_tail.append(line)
            ag.terminal_tail = ag.terminal_tail[-800:]

    def _load_workspace_state(self) -> None:
        """Loads persistent archive/history state and custom workspace titles from ~/.pac_neon/workspace_state.json."""
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8", errors="replace"))
            self.archived_overrides = dict(data.get("archived_overrides") or {})
            self.archived_snapshots = dict(data.get("archived_snapshots") or {})
            self.custom_titles = dict(data.get("custom_titles") or {})
            self.dismissed_perm_mtime = {
                k: float(v) for k, v in (data.get("dismissed_perm_mtime") or {}).items()
            }
            if data.get("notification_timeout_sec") is not None:
                try:
                    self.notification_timeout_sec = max(5.0, min(3600.0, float(data["notification_timeout_sec"])))
                except Exception:
                    self.notification_timeout_sec = 60.0
            if isinstance(data.get("user_meeting_settings"), dict):
                self.user_meeting_settings_map = dict(data["user_meeting_settings"])
            self.meeting_guard.load_from_dict(data)
            for aid, snap in self.archived_snapshots.items():
                if isinstance(snap, dict) and aid not in self.agents:
                    try:
                        snap_copy = dict(snap)
                        if isinstance(snap_copy.get("pending_permission"), dict):
                            snap_copy["pending_permission"] = PermissionRequestItem(**snap_copy["pending_permission"])
                        unit = AgentUnit(**snap_copy)
                        unit.archived = bool(self.archived_overrides.get(aid, True))
                        if aid in self.custom_titles:
                            unit.workspace_title = self.custom_titles[aid]
                            unit.name = self.custom_titles[aid]
                        unit.neon_meta = self.neon_cli.enrich_agent_metadata(
                            unit.repo_path,
                            unit.repo_name,
                            unit.branch,
                            unit.workspace_title,
                            current_action=unit.current_action,
                            last_thought=unit.last_thought,
                            terminal_tail=unit.terminal_tail,
                            provider=unit.provider,
                            session_id=unit.session_id,
                        )
                        self.agents[aid] = unit
                    except Exception:
                        continue
        except Exception:
            pass

    def _save_workspace_state(self) -> None:
        """Persists archive/history state and custom workspace titles to ~/.pac_neon/workspace_state.json."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            for aid, ag in self.agents.items():
                if ag.archived:
                    self.archived_snapshots[aid] = ag.model_dump()
            self.user_meeting_settings_map = self.meeting_guard.dump_for_storage(self.user_meeting_settings_map)
            payload = {
                "archived_overrides": self.archived_overrides,
                "archived_snapshots": self.archived_snapshots,
                "custom_titles": self.custom_titles,
                "dismissed_perm_mtime": self.dismissed_perm_mtime,
                "notification_timeout_sec": self.notification_timeout_sec,
                "user_meeting_settings": self.user_meeting_settings_map,
                "meeting_settings": dict(self.meeting_guard.settings),
                "updated_at": utc_now_iso(),
            }
            STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def set_agent_archived(self, agent_id: str, archived: bool) -> Optional[AgentUnit]:
        ag = self.agents.get(agent_id)
        if not ag:
            return None
        ag.archived = archived
        ag.archived_at = utc_now_iso() if archived else None
        ag.updated_at = utc_now_iso()
        self.archived_overrides[agent_id] = archived
        if archived:
            self.archived_snapshots[agent_id] = ag.model_dump()
        self._save_workspace_state()
        return ag

    def archive_agent(self, agent_id: str, archived: bool = True) -> Optional[AgentUnit]:
        return self.set_agent_archived(agent_id, archived)

    def rename_agent_workspace(self, agent_id: str, new_title: str, sync_external: bool = True) -> Optional[dict]:
        ag = self.agents.get(agent_id)
        if not ag:
            return None
        clean_title = new_title.strip()
        if not clean_title:
            return None

        ag.workspace_title = clean_title
        ag.name = clean_title
        ag.updated_at = utc_now_iso()
        self.custom_titles[agent_id] = clean_title

        synced_targets: List[str] = ["Pac-Neon Painel"]

        if sync_external:
            # 1. Synchronize with Antigravity conversation annotation (.pbtxt)
            if ag.provider.lower() == "antigravity":
                conv_id = ag.session_id or agent_id.replace("agy-", "")
                if AntigravityAdapter.update_conversation_title(conv_id, clean_title):
                    synced_targets.append("Janela Antigravity (.pbtxt)")

            # 2. Synchronize with live Terminal TTY / Tab title (Terminal.app / iTerm2 / PTY)
            if ag.tty:
                tty_path = f"/dev/{ag.tty}" if not ag.tty.startswith("/dev/") else ag.tty
                try:
                    with open(tty_path, "wb") as tf:
                        tf.write(f"\033]0;{clean_title}\007".encode("utf-8"))
                    synced_targets.append(f"Terminal ({ag.tty})")
                except Exception:
                    pass
                _sync_macos_terminal_tab_title(ag.tty, clean_title)

            if ag.control_mode == "pty":
                self.pty_manager.write_input(ag.id, f"\033]0;{clean_title}\007")

            # 3. Synchronize with local Git / Worktree metadata (.git/pac_neon_title)
            try:
                repo_dir = Path(ag.repo_path).expanduser()
                git_dir = repo_dir / ".git"
                if git_dir.is_dir():
                    (git_dir / "pac_neon_title").write_text(clean_title, encoding="utf-8")
                    synced_targets.append(f"Worktree ({ag.repo_name})")
            except Exception:
                pass

        ag.terminal_tail.append(f"✏️ [TÍTULO SINCRONIZADO] '{clean_title}' -> {', '.join(synced_targets)}")
        self._save_workspace_state()
        return {"ok": True, "agent_id": agent_id, "title": clean_title, "synced_targets": synced_targets}

    def refresh_discovered_agents(self) -> None:
        # Remove any legacy fake/demo IDs if they somehow existed in state
        for fake_id in ("agy-invoice", "claude-arch", "gemini-iac", "codex-bff", "claude-gateway", "gemini-sre", "codex-ui"):
            self.agents.pop(fake_id, None)
        for k in list(self.agents.keys()):
            if k.startswith("antigravity-"):
                self.agents.pop(k, None)

        # 1. Discover ONLY the current user's local Antigravity / Claude / Gemini / Codex sessions
        discovered = AdapterRegistry.discover_all(max_age_minutes=43200)
        # 2. Discover live Superset workspaces & terminals via `superset snapshot --json`
        superset_units = self.superset_bridge.discover_from_snapshot()
        discovered.extend(superset_units)

        for disc in discovered:
            # Apply user's explicit archive/unarchive preference if set
            if disc.id in self.archived_overrides:
                disc.archived = self.archived_overrides[disc.id]

            # Apply user's custom title if set
            if disc.id in self.custom_titles:
                disc.workspace_title = self.custom_titles[disc.id]
                disc.name = self.custom_titles[disc.id]
            elif not disc.workspace_title:
                disc.workspace_title = disc.name

            # Enrich with Neon CLI metadata (K8s namespace, GitHub repo, Jira issue key, PR URL, Glowworm)
            disc.neon_meta = self.neon_cli.enrich_agent_metadata(
                disc.repo_path,
                disc.repo_name,
                disc.branch,
                disc.workspace_title,
                current_action=disc.current_action,
                last_thought=disc.last_thought,
                terminal_tail=disc.terminal_tail,
                provider=disc.provider,
                session_id=disc.session_id,
            )

            # Enrich with live macOS Terminal/iTerm2 screen capture if TTY is active and process is alive
            if disc.pid and disc.tty and disc.host_app in ("Terminal", "iTerm"):
                live_lines = _capture_macos_terminal_screen(disc.tty)
                if live_lines:
                    if not disc.terminal_tail or len(live_lines) >= len(disc.terminal_tail):
                        disc.terminal_tail = live_lines[-800:]
                    else:
                        # Keep the richer structured transcript and append any live interactive prompt lines at the bottom
                        for lln in live_lines[-6:]:
                            if lln not in disc.terminal_tail[-15:]:
                                disc.terminal_tail.append(lln)
                        disc.terminal_tail = disc.terminal_tail[-800:]
                    # Check if the bottom of the live terminal screen shows an interactive permission prompt
                    bottom_blob = "\n".join(live_lines[-6:])
                    if (
                        disc.pending_permission is None
                        and disc.game_state != AgentGameState.IDLE
                        and any(
                            pat in bottom_blob
                            for pat in ("Do you want to", "1. Yes", "2. Yes, allow", "Allow ", "[y/n]", "(y/n)")
                        )
                    ):
                        disc.game_state = AgentGameState.POWER_PELLET_WAIT
                        disc.pending_permission = PermissionRequestItem(
                            id=f"perm-tty-{disc.id}",
                            agent_id=disc.id,
                            agent_name=disc.workspace_title,
                            provider=disc.provider,
                            repo_name=disc.repo_name,
                            tool_name="Terminal Prompt",
                            command_preview="\n".join(live_lines[-10:]),
                            reason=f"Prompt interativo detectado ao vivo na tela do terminal • Diretório: {disc.repo_path}",
                            full_details={
                                "cwd": disc.repo_path,
                                "agent_context": disc.last_thought or "",
                            },
                            channel="terminal_prompt",
                        )

            # Apply real-time push Hook state overrides (from ~/.pac-neon/hooks/notify.sh) only while fresh
            now_ts = time.time()
            hook_ov = self.hook_state_overrides.get(disc.id)
            if hook_ov:
                ov_ts = float(hook_ov.get("ts", 0.0))
                ov_state = hook_ov.get("game_state")
                has_waiting_hook_ev = disc.id in self.agent_hook_events
                if ov_state == AgentGameState.POWER_PELLET_WAIT:
                    is_fresh_ov = has_waiting_hook_ev or ((now_ts - ov_ts) < 12.0 and ov_ts >= disc.last_active_ts)
                else:
                    is_fresh_ov = (now_ts - ov_ts) < 8.0 and ov_ts >= disc.last_active_ts
                if is_fresh_ov:
                    if isinstance(ov_state, AgentGameState):
                        disc.game_state = ov_state
                    if hook_ov.get("current_action"):
                        disc.current_action = str(hook_ov["current_action"])
                    if "pending_permission" in hook_ov:
                        disc.pending_permission = hook_ov["pending_permission"]
                else:
                    self.hook_state_overrides.pop(disc.id, None)

            # Respect user's dismissal/resolution if transcript hasn't changed since dismissal
            dismissed_ts = self.dismissed_perm_mtime.get(disc.id, 0.0)
            if disc.pending_permission is not None and dismissed_ts > 0 and disc.last_active_ts <= dismissed_ts + 0.5:
                disc.pending_permission = None
                if disc.game_state == AgentGameState.POWER_PELLET_WAIT:
                    disc.game_state = AgentGameState.IDLE

            # Match against existing agents by id, session_id, pid, tty, or recently spawned same-(provider, repo_path)
            matched_existing_id: Optional[str] = None
            if disc.id in self.agents:
                matched_existing_id = disc.id
            else:
                disc_short_tty = (disc.tty or "").replace("/dev/", "").strip()
                for cand_id, cand in list(self.agents.items()):
                    if cand.session_id and disc.session_id and cand.session_id == disc.session_id:
                        matched_existing_id = cand_id
                        break
                    if cand.pid and disc.pid and cand.pid == disc.pid:
                        matched_existing_id = cand_id
                        break
                    cand_short_tty = (cand.tty or "").replace("/dev/", "").strip()
                    if (
                        cand_short_tty
                        and disc_short_tty
                        and cand_short_tty == disc_short_tty
                        and cand.provider == disc.provider
                    ):
                        matched_existing_id = cand_id
                        break
                    if (
                        cand.provider == disc.provider
                        and not cand.is_live_discovered
                        and (now_ts - (cand.last_active_ts or 0.0)) < 300.0
                    ):
                        try:
                            if Path(cand.repo_path).resolve() == Path(disc.repo_path).resolve():
                                matched_existing_id = cand_id
                                break
                        except Exception:
                            pass

            if matched_existing_id and matched_existing_id != disc.id:
                existing_cand = self.agents[matched_existing_id]
                if existing_cand.control_mode == "pty" and matched_existing_id in self.pty_manager.sessions:
                    disc.id = matched_existing_id
                else:
                    self.agents[disc.id] = self.agents.pop(matched_existing_id)
                    if matched_existing_id in self.custom_titles and disc.id not in self.custom_titles:
                        self.custom_titles[disc.id] = self.custom_titles.pop(matched_existing_id)
                    if matched_existing_id in self.archived_overrides and disc.id not in self.archived_overrides:
                        self.archived_overrides[disc.id] = self.archived_overrides.pop(matched_existing_id)
                    if matched_existing_id in self.active_panel_commands:
                        self.active_panel_commands[disc.id] = self.active_panel_commands.pop(matched_existing_id)
                    if matched_existing_id in self.recent_panel_lines:
                        self.recent_panel_lines[disc.id] = self.recent_panel_lines.pop(matched_existing_id)

            if disc.id not in self.agents:
                self.agents[disc.id] = disc
                target_ag = disc
            else:
                existing = self.agents[disc.id]
                if isinstance(existing.pending_permission, dict):
                    try:
                        existing.pending_permission = PermissionRequestItem(**existing.pending_permission)
                    except Exception:
                        existing.pending_permission = None
                if disc.id in self.custom_titles:
                    existing.workspace_title = self.custom_titles[disc.id]
                    existing.name = self.custom_titles[disc.id]
                elif disc.workspace_title:
                    existing.workspace_title = disc.workspace_title
                    existing.name = disc.workspace_title

                existing.archived = disc.archived
                existing.repo_name = disc.repo_name or existing.repo_name
                existing.repo_path = disc.repo_path or existing.repo_path
                existing.branch = disc.branch or existing.branch
                existing.session_id = disc.session_id or existing.session_id
                existing.pid = disc.pid or existing.pid
                existing.tty = disc.tty or existing.tty
                existing.host_app = disc.host_app or existing.host_app
                existing.neon_color = disc.neon_color or existing.neon_color
                existing.sprite_type = disc.sprite_type or existing.sprite_type
                prev_meta = existing.neon_meta or {}
                new_meta = dict(disc.neon_meta or prev_meta)
                if (
                    not new_meta.get("pr_url")
                    and prev_meta.get("pr_url")
                    and prev_meta.get("pr_source") == "branch"
                ):
                    new_meta["pr_url"] = prev_meta.get("pr_url")
                    new_meta["pr_number"] = prev_meta.get("pr_number")
                    new_meta["pr_repo"] = prev_meta.get("pr_repo")
                    new_meta["pr_source"] = "branch"
                    new_meta["prs"] = prev_meta.get("prs") or [
                        {
                            "pr_url": prev_meta.get("pr_url"),
                            "pr_number": prev_meta.get("pr_number"),
                            "pr_repo": prev_meta.get("pr_repo"),
                            "pr_source": "branch",
                        }
                    ]
                existing.neon_meta = new_meta
                existing.last_active_ts = disc.last_active_ts or existing.last_active_ts
                existing.score_xp = max(existing.score_xp, disc.score_xp)
                existing.pellets_eaten = max(existing.score_xp // 150, existing.pellets_eaten, disc.pellets_eaten)
                handoff_reset_ts = self.self_handoff_overrides.get(disc.id, 0.0)
                if handoff_reset_ts > 0 and (now_ts - handoff_reset_ts) < 45.0:
                    existing.context_health_pct = 100
                    existing.hallucination_risk_pct = 0
                    existing.health_status = "HEALTHY"
                    existing.context_tokens_used = min(disc.context_tokens_used or 1500, 2500)
                    existing.context_tokens_limit = disc.context_tokens_limit or 200000
                    existing.active_elapsed_sec = 0
                    existing.is_stalled = False
                    existing.health_signals = []
                else:
                    if handoff_reset_ts > 0:
                        self.self_handoff_overrides.pop(disc.id, None)
                    existing.context_health_pct = disc.context_health_pct
                    existing.hallucination_risk_pct = disc.hallucination_risk_pct
                    existing.health_status = disc.health_status
                    existing.context_tokens_used = disc.context_tokens_used
                    existing.context_tokens_limit = disc.context_tokens_limit
                    existing.active_elapsed_sec = disc.active_elapsed_sec
                    existing.is_stalled = disc.is_stalled
                    existing.health_signals = list(disc.health_signals)


                panel_cmd_ts = self.active_panel_commands.get(disc.id, 0.0)
                if panel_cmd_ts > 0 and (disc.last_active_ts >= panel_cmd_ts or (now_ts - panel_cmd_ts) >= 12.0):
                    self.active_panel_commands.pop(disc.id, None)
                    panel_cmd_ts = 0.0
                is_running_panel_cmd = panel_cmd_ts > 0 and (now_ts - panel_cmd_ts) < 12.0
                has_waiting_hook = disc.id in self.agent_hook_events

                # Always update live action, thought, tool, artifacts, and terminal tail in real time
                if disc.current_action:
                    existing.current_action = disc.current_action
                if disc.last_thought:
                    existing.last_thought = disc.last_thought
                existing.active_tool = None if disc.game_state == AgentGameState.IDLE else (disc.active_tool or existing.active_tool)
                if disc.artifacts:
                    existing.artifacts = disc.artifacts
                if disc.terminal_tail and existing.control_mode != "pty":
                    merged_tail = list(disc.terminal_tail)
                    recent_bucket = [
                        item
                        for item in self.recent_panel_lines.get(disc.id, [])
                        if (now_ts - item[0]) < 90.0
                    ]
                    self.recent_panel_lines[disc.id] = recent_bucket
                    for _, pline in recent_bucket:
                        if pline not in merged_tail[-40:]:
                            merged_tail.append(pline)
                    existing.terminal_tail = merged_tail[-800:]

                if has_waiting_hook and existing.pending_permission is not None:
                    existing.game_state = AgentGameState.POWER_PELLET_WAIT
                elif disc.pending_permission is not None:
                    disc.pending_permission.agent_name = existing.workspace_title
                    existing.pending_permission = disc.pending_permission
                    existing.game_state = AgentGameState.POWER_PELLET_WAIT
                elif is_running_panel_cmd and disc.game_state == AgentGameState.IDLE:
                    existing.game_state = AgentGameState.CHOMPING
                else:
                    # Clear auto-detected permissions once resolved in transcript
                    if existing.pending_permission and existing.pending_permission.id.startswith(("perm-claude-", "perm-agy-", "perm-tty-", "perm-hook-")):
                        existing.pending_permission = None
                    if existing.pending_permission is None:
                        existing.game_state = disc.game_state
                        if existing.game_state == AgentGameState.IDLE:
                            existing.active_tool = None
                target_ag = existing

            # Fire native macOS notification once per configured notification_timeout_sec (default 60s = 1 minute)
            if target_ag.pending_permission and not target_ag.archived:
                try:
                    sig = f"{target_ag.pending_permission.id}:{target_ag.pending_permission.tool_name}:{(target_ag.pending_permission.command_preview or '')[:120]}"
                    last_notif = self.notified_perm_timestamps.get(sig, 0.0)
                    interval = max(5.0, float(getattr(self, "notification_timeout_sec", 60.0)))
                    if (now_ts - last_notif) >= interval:
                        self.notified_perm_timestamps[sig] = now_ts
                        trigger_cockpit_popup(
                            reason_title=f"⚡ PAC-NEON // {target_ag.workspace_title}",
                            reason_body=f"{target_ag.pending_permission.tool_name}: {(target_ag.pending_permission.command_preview or '')[:200]}",
                            open_browser=False,
                        )
                except Exception:
                    pass

        # Ensure any older restored agent not returned in this discovery cycle is marked IDLE
        discovered_ids = {d.id for d in discovered}
        for ag_id, ag_unit in list(self.agents.items()):
            if ag_unit.provider == "antigravity":
                ag_unit.neon_color = "#0078D9"
            if ag_id not in discovered_ids and ag_unit.control_mode != "pty" and ag_id not in self.agent_hook_events:
                # Preserve CHOMPING state briefly if just spawned within the last 12 seconds
                recently_spawned = (time.time() - (ag_unit.last_active_ts or 0.0)) < 12.0 and not ag_unit.is_live_discovered
                if not recently_spawned:
                    ag_unit.game_state = AgentGameState.IDLE
                    ag_unit.active_tool = None
                    ag_unit.pending_permission = None
                if ag_unit.pid is not None:
                    try:
                        os.kill(ag_unit.pid, 0)
                    except OSError:
                        ag_unit.pid = None

    def sync_live_agents(self, force: bool = False) -> None:
        now = time.time()
        if not force and getattr(self, "_last_sync_ts", 0.0) > 0 and (now - self._last_sync_ts) < 0.8:
            return
        if not self._sync_lock.acquire(blocking=force):
            return
        try:
            self._last_sync_ts = time.time()
            self.refresh_discovered_agents()
        finally:
            self._sync_lock.release()

    def get_pending_permissions(self) -> List[PermissionRequestItem]:
        perms: List[PermissionRequestItem] = []
        for ag in self.agents.values():
            if not ag.archived and ag.pending_permission and ag.pending_permission.status == "PENDING":
                perms.append(ag.pending_permission)
        return perms

    def _get_cached_superset_status(self) -> dict:
        now = time.time()
        cached = getattr(self, "_cached_superset_status", None)
        last_ts = getattr(self, "_last_superset_status_ts", 0.0)
        if cached is not None and (now - last_ts) < 15.0:
            return cached
        st = self.superset_bridge.get_host_status()
        self._cached_superset_status = st
        self._last_superset_status_ts = now
        return st

    def _get_cached_toolchain_status(self) -> dict:
        now = time.time()
        cached = getattr(self, "_cached_toolchain_status", None)
        last_ts = getattr(self, "_last_toolchain_status_ts", 0.0)
        if cached is not None and (now - last_ts) < 30.0:
            return cached
        st = self.neon_cli.get_toolchain_status()
        self._cached_toolchain_status = st
        self._last_toolchain_status_ts = now
        return st

    def _get_cached_hosts_and_clis(self) -> Tuple[dict, dict]:
        now = time.time()
        cached_hosts = getattr(self, "_cached_available_hosts", None)
        cached_clis = getattr(self, "_cached_available_clis", None)
        last_ts = getattr(self, "_last_hosts_discovery_ts", 0.0)
        if cached_hosts is not None and cached_clis is not None and (now - last_ts) < 30.0:
            return cached_hosts, cached_clis
        sup_st = self._get_cached_superset_status()
        sup_installed = bool(sup_st.get("installed") or sup_st.get("cli_available") or sup_st.get("app_installed"))
        hosts = _discover_available_hosts(superset_installed=sup_installed)
        clis = _discover_available_cli_binaries()
        self._cached_available_hosts = hosts
        self._cached_available_clis = clis
        self._last_hosts_discovery_ts = now
        return hosts, clis

    def snapshot(self) -> dict:
        # Sort agents: active non-archived first (by last_active_ts desc), then archived
        sorted_agents = sorted(
            self.agents.values(),
            key=lambda a: (
                0 if not a.archived else 1,
                0 if a.game_state == AgentGameState.POWER_PELLET_WAIT else (1 if a.game_state != AgentGameState.IDLE else 2),
                -(a.last_active_ts or 0.0),
            ),
        )
        active_agents = [a for a in sorted_agents if not a.archived]
        archived_agents = [a for a in sorted_agents if a.archived]
        total_score = sum(a.score_xp for a in active_agents)
        self.high_score = max(self.high_score, total_score)
        available_hosts, available_cli_binaries = self._get_cached_hosts_and_clis()
        user_repos, default_repo = _discover_user_repositories(active_agents)
        return {
            "high_score": self.high_score,
            "total_score": total_score,
            "active_count": len(active_agents),
            "archived_count": len(archived_agents),
            "notification_timeout_sec": self.notification_timeout_sec,
            "meeting": self.meeting_guard.get_status(),
            "accessibility_trusted": _is_macos_accessibility_trusted(False),
            "superset": self._get_cached_superset_status(),
            "toolchain": self._get_cached_toolchain_status(),
            "available_hosts": available_hosts,
            "available_cli_binaries": available_cli_binaries,
            "user_repositories": user_repos,
            "default_repo_path": default_repo,
            "agents": [a.model_dump() for a in sorted_agents],
            "pending_permissions": [p.model_dump() for p in self.get_pending_permissions()],
            "warp_fruits": [f.model_dump() for f in self.warp_fruits[:12]],
            "providers": [p.model_dump() for p in AdapterRegistry.all_metadata()],
        }

    async def broadcast(self) -> None:
        if not self.ws_clients:
            return
        payload = self.snapshot()
        dead: List[WebSocket] = []
        for ws in self.ws_clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)


hub = ArcadeOrchestratorHub()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/state")
async def get_state() -> dict:
    hub.sync_live_agents()
    return hub.snapshot()


api_state = get_state


@app.get("/api/superset/status")
async def get_superset_status() -> dict:
    return hub.superset_bridge.get_host_status()


@app.get("/api/toolchain/status")
async def get_toolchain_status() -> dict:
    return hub.neon_cli.get_toolchain_status()


@app.get("/api/hosts")
async def get_available_hosts_endpoint() -> dict:
    available_hosts, available_cli_binaries = hub._get_cached_hosts_and_clis()
    user_repos, default_repo = _discover_user_repositories(list(hub.agents.values()))
    return {
        "available_hosts": available_hosts,
        "available_cli_binaries": available_cli_binaries,
        "repositories": user_repos,
        "default_repo_path": default_repo,
    }


@app.get("/api/doctor")
async def get_doctor_status_endpoint() -> dict:
    return run_environment_doctor()


def _resolve_valid_repo_dir(ag: AgentUnit) -> str:
    """Finds the exact existing local folder on macOS for this agent's repository."""
    candidates = [
        Path(ag.repo_path).expanduser(),
        Path.home() / "Projects" / ag.repo_name,
        Path.home() / "Projects",
    ]
    for c in candidates:
        try:
            if c.exists() and c.is_dir():
                return str(c.resolve())
        except Exception:
            continue
    return str(Path.home())


_KNOWN_TERMINAL_APPS = (
    "Terminal",
    "iTerm",
    "Warp",
    "Ghostty",
    "Alacritty",
    "WezTerm",
)

_KNOWN_IDE_APPS = (
    "Antigravity",
    "IntelliJ IDEA",
    "Android Studio",
    "Visual Studio Code",
    "Cursor",
    "Windsurf",
    "PyCharm",
    "WebStorm",
    "GoLand",
    "Xcode",
)

_APP_BUNDLE_ALIASES: Dict[str, List[str]] = {
    "Antigravity": ["Antigravity", "Antigravity IDE"],
    "IntelliJ IDEA": ["IntelliJ IDEA", "IntelliJ IDEA Ultimate", "IntelliJ IDEA CE"],
    "PyCharm": ["PyCharm", "PyCharm Professional Edition", "PyCharm CE"],
    "iTerm": ["iTerm", "iTerm2"],
}


def _find_installed_mac_app(app_names: List[str]) -> str | None:
    """Returns the first installed macOS .app name or bundle path from the list."""
    search_dirs = [
        Path("/Applications"),
        Path("/System/Applications/Utilities"),
        Path.home() / "Applications",
        Path("/Volumes/Antigravity"),
    ]
    expanded_names: List[str] = []
    for name in app_names:
        clean = name.replace(".app", "")
        for alias in _APP_BUNDLE_ALIASES.get(clean, [clean]):
            if alias not in expanded_names:
                expanded_names.append(alias)

    for name in expanded_names:
        app_bundle = f"{name}.app" if not name.endswith(".app") else name
        for sdir in search_dirs:
            candidate = sdir / app_bundle
            if candidate.exists():
                if str(sdir).startswith("/Volumes/"):
                    return str(candidate)
                return name.replace(".app", "")
    return None


def _get_augmented_search_path() -> str:
    """Returns PATH augmented with standard macOS Homebrew, local, npm, nvm, and cargo binary directories."""
    extra_dirs = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".npm-global" / "bin"),
        str(Path.home() / ".cargo" / "bin"),
    ]
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.exists():
        try:
            for node_ver in sorted(nvm_dir.iterdir(), reverse=True)[:3]:
                bin_dir = node_ver / "bin"
                if bin_dir.exists():
                    extra_dirs.append(str(bin_dir))
        except Exception:
            pass
    current_path = os.environ.get("PATH", "")
    parts = [p for p in extra_dirs if p not in current_path.split(":")]
    if current_path:
        parts.append(current_path)
    return ":".join(parts)


def _resolve_cli_binary(cmd_name: str) -> Optional[str]:
    """Resolves a CLI binary name on macOS using augmented PATH."""
    if not cmd_name:
        return None
    base = cmd_name.strip().split()[0]
    return shutil.which(base, path=_get_augmented_search_path())


def _discover_available_cli_binaries() -> Dict[str, Any]:
    """Detects installed AI CLI binaries on the device."""
    search_path = _get_augmented_search_path()
    candidates = {
        "antigravity": ["agy", "antigravity"],
        "claude": ["claude"],
        "gemini": ["gemini"],
        "codex": ["codex"],
        "aider": ["aider"],
        "cursor": ["cursor"],
        "opencode": ["opencode"],
        "superset": ["superset"],
    }
    result: Dict[str, Any] = {}
    for key, bins in candidates.items():
        found_path: Optional[str] = None
        found_bin: Optional[str] = None
        for b in bins:
            p = shutil.which(b, path=search_path)
            if p:
                found_path = p
                found_bin = b
                break
        result[key] = {
            "installed": bool(found_path),
            "binary": found_bin or bins[0],
            "path": found_path,
        }
    return result


def _discover_available_hosts(superset_installed: bool = False) -> Dict[str, Any]:
    """Discovers all IDEs and Terminal Emulators available on this Mac."""
    terminal_specs = [
        ("Terminal", "Terminal.app (macOS Nativo)"),
        ("iTerm", "iTerm2"),
        ("Warp", "Warp Terminal"),
        ("Ghostty", "Ghostty"),
        ("Alacritty", "Alacritty"),
        ("WezTerm", "WezTerm"),
    ]
    ide_specs = [
        ("Antigravity", "Google Antigravity IDE"),
        ("IntelliJ IDEA", "IntelliJ IDEA"),
        ("Android Studio", "Android Studio"),
        ("Visual Studio Code", "Visual Studio Code"),
        ("Cursor", "Cursor IDE"),
        ("Windsurf", "Windsurf IDE"),
        ("PyCharm", "PyCharm"),
        ("WebStorm", "WebStorm"),
        ("GoLand", "GoLand"),
        ("Xcode", "Xcode"),
    ]

    terminals: List[Dict[str, Any]] = []
    for host_id, label in terminal_specs:
        resolved = _find_installed_mac_app([host_id])
        terminals.append(
            {
                "id": host_id,
                "label": label,
                "category": "terminal",
                "installed": bool(resolved),
                "resolved_app": resolved,
            }
        )

    ides: List[Dict[str, Any]] = []
    for host_id, label in ide_specs:
        resolved = _find_installed_mac_app([host_id])
        if host_id == "Antigravity" and not resolved:
            # Also check if Antigravity language server is currently running
            try:
                if AntigravityAdapter._discover_servers():
                    resolved = "Antigravity"
            except Exception:
                pass
        ides.append(
            {
                "id": host_id,
                "label": label,
                "category": "ide",
                "installed": bool(resolved),
                "resolved_app": resolved,
            }
        )

    superset_app = _find_installed_mac_app(["Superset"])
    is_superset_ready = bool(superset_installed or superset_app)
    superset_entry = {
        "id": "Superset",
        "label": "Superset Worktree (Git Isolado)",
        "category": "superset",
        "installed": is_superset_ready,
        "resolved_app": superset_app or ("superset" if is_superset_ready else None),
    }

    installed_all = (
        [h for h in ides if h["installed"]]
        + [t for t in terminals if t["installed"]]
        + ([superset_entry] if is_superset_ready else [])
    )
    return {
        "terminals": terminals,
        "ides": ides,
        "superset": superset_entry,
        "installed": installed_all,
    }


def _discover_user_repositories(active_agents: Optional[List[AgentUnit]] = None) -> Tuple[List[Dict[str, str]], str]:
    """Dynamically discovers the logged-in Neon collaborator's local repositories (zero hardcoded paths)."""
    home = Path.home()
    seen: Dict[str, str] = {}

    def _add_repo(p: Path, label: Optional[str] = None) -> None:
        try:
            rp = p.expanduser().resolve()
            if not rp.exists() or not rp.is_dir():
                return
            rp_str = str(rp)
            if rp_str not in seen:
                seen[rp_str] = label or rp.name
        except Exception:
            pass

    if active_agents:
        for ag in active_agents:
            if ag.repo_path and not ag.repo_path.startswith("superset://"):
                _add_repo(Path(ag.repo_path), ag.repo_name or Path(ag.repo_path).name)

    _add_repo(Path(_PROJECT_ROOT), Path(_PROJECT_ROOT).name)

    search_roots = [
        home / "Projects",
        home / "Git",
        home / "Developer",
        home / "workspace",
        home / "repos",
    ]
    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            children = sorted(
                (c for c in root.iterdir() if c.is_dir() and not c.name.startswith(".")),
                key=lambda d: (0 if (d / ".git").exists() else 1, d.name.lower()),
            )
            for child in children[:40]:
                _add_repo(child, child.name)
                if len(seen) >= 50:
                    break
        except Exception:
            continue
        if len(seen) >= 50:
            break

    repositories = [{"path": k, "name": v} for k, v in seen.items()]
    default_repo = (
        repositories[0]["path"]
        if repositories
        else (str(home / "Projects") if (home / "Projects").exists() else str(home))
    )
    return repositories, default_repo


def run_environment_doctor() -> Dict[str, Any]:
    """Runs a comprehensive self-service environment diagnostic for any Neon collaborator."""
    import socket

    port_active = False
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.5):
            port_active = True
    except OSError:
        port_active = False

    launch_agent_plist = Path.home() / "Library" / "LaunchAgents" / "com.neon.pac-neon.plist"
    app_bundle = Path.home() / "Applications" / "Pac-Neon.app"
    state_dir = STATE_FILE.parent
    state_dir.mkdir(parents=True, exist_ok=True)

    clis = _discover_available_cli_binaries()
    hosts = _discover_available_hosts()
    toolchain = hub.neon_cli.get_toolchain_status() if "hub" in globals() else NeonCliBridge().get_toolchain_status()

    # Check Claude Code hooks in ~/.claude/settings.json
    claude_settings_path = Path.home() / ".claude" / "settings.json"
    claude_hook_ok = False
    claude_hook_detail = "Não configurado"
    if claude_settings_path.exists():
        try:
            raw_cfg = claude_settings_path.read_text(encoding="utf-8")
            if "claude_pre_tool_use.py" in raw_cfg or "notify.sh" in raw_cfg:
                claude_hook_ok = True
                claude_hook_detail = "Hooks do Claude Code registrados em ~/.claude/settings.json"
        except Exception:
            pass

    # Check GitHub CLI auth status
    gh_bin = shutil.which("gh") or ("/opt/homebrew/bin/gh" if Path("/opt/homebrew/bin/gh").exists() else None)
    gh_authenticated = False
    if gh_bin:
        try:
            proc = subprocess.run([gh_bin, "auth", "status"], capture_output=True, text=True, timeout=2.5, check=False)
            gh_authenticated = proc.returncode == 0
        except Exception:
            gh_authenticated = False

    ax_trusted = _is_macos_accessibility_trusted(False)
    agy_servers = []
    try:
        agy_servers = AntigravityAdapter._discover_servers()
    except Exception:
        pass

    checks = [
        {
            "id": "server_daemon",
            "title": "Servidor Pac-Neon (127.0.0.1:8765)",
            "ok": port_active,
            "detail": "Ativo em http://127.0.0.1:8765" if port_active else "Servidor parado (rode `pac-neon start`)",
            "fix_cmd": "pac-neon start",
        },
        {
            "id": "autostart_service",
            "title": "Inicialização Automática (LaunchAgent & Pac-Neon.app)",
            "ok": launch_agent_plist.exists(),
            "detail": (
                f"LaunchAgent ativo ({launch_agent_plist.name})"
                if launch_agent_plist.exists()
                else "Serviço automático não instalado"
            ),
            "fix_cmd": "pac-neon install",
        },
        {
            "id": "ai_engines",
            "title": "Motores de IA Detectados",
            "ok": bool(
                agy_servers
                or (Path.home() / ".gemini" / "antigravity" / "brain").exists()
                or clis.get("claude", {}).get("installed")
                or clis.get("gemini", {}).get("installed")
                or clis.get("codex", {}).get("installed")
            ),
            "detail": ", ".join(
                name
                for name, present in [
                    ("Antigravity IDE", bool(agy_servers or (Path.home() / ".gemini" / "antigravity" / "brain").exists())),
                    ("Claude Code", bool(clis.get("claude", {}).get("installed"))),
                    ("Gemini CLI", bool(clis.get("gemini", {}).get("installed"))),
                    ("Codex CLI", bool(clis.get("codex", {}).get("installed"))),
                ]
                if present
            )
            or "Nenhum motor detectado ainda",
            "fix_cmd": None,
        },
        {
            "id": "claude_hooks",
            "title": "Hooks de Permissão & Eventos (Claude Code)",
            "ok": claude_hook_ok,
            "detail": claude_hook_detail,
            "fix_cmd": "pac-neon install",
        },
        {
            "id": "github_cli",
            "title": "GitHub CLI (`gh`) & Resolução de PRs Neon",
            "ok": bool(gh_bin and gh_authenticated),
            "detail": (
                "Autenticado e pronto para resolver PRs da Neon"
                if (gh_bin and gh_authenticated)
                else ("Instalado, mas requer `gh auth login`" if gh_bin else "GitHub CLI (`gh`) não instalado (`brew install gh`)")
            ),
            "fix_cmd": "gh auth login" if gh_bin else "brew install gh && gh auth login",
        },
        {
            "id": "neon_cli",
            "title": "Neon CLI (`neon` / `agy` / `glowworm`)",
            "ok": bool(toolchain.get("neon_cli", {}).get("installed") or toolchain.get("agy_cli", {}).get("installed")),
            "detail": (
                f"neon-cli: {'OK' if toolchain.get('neon_cli', {}).get('installed') else 'ausente'} | "
                f"agy: {'OK' if toolchain.get('agy_cli', {}).get('installed') else 'ausente'} | "
                f"glowworm: {'OK' if toolchain.get('glowworm_cli', {}).get('installed') else 'ausente'}"
            ),
            "fix_cmd": None,
        },
        {
            "id": "macos_accessibility",
            "title": "Permissão de Acessibilidade macOS (System Events)",
            "ok": ax_trusted,
            "detail": (
                "Concedida (injeção direta em Terminal/iTerm habilitada)"
                if ax_trusted
                else "Opcional: conceda em Ajustes do Sistema > Privacidade > Acessibilidade para injetar teclas em terminais externos"
            ),
            "fix_cmd": "pac-neon doctor --prompt-accessibility",
        },
    ]

    passed = sum(1 for c in checks if c["ok"])
    score_pct = int(round((passed / len(checks)) * 100))
    recommendations = [c["fix_cmd"] for c in checks if not c["ok"] and c.get("fix_cmd")]
    repos, default_repo = _discover_user_repositories(
        list(hub.agents.values()) if "hub" in globals() else None
    )

    return {
        "ok": score_pct >= 70,
        "ready_score_pct": score_pct,
        "passed_checks": passed,
        "total_checks": len(checks),
        "user": getpass.getuser(),
        "home": str(Path.home()),
        "default_repo_path": default_repo,
        "discovered_repos_count": len(repos),
        "app_bundle_installed": app_bundle.exists(),
        "checks": checks,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def _focus_antigravity_app(repo_dir: Optional[str] = None) -> str:
    """Focuses the running Antigravity IDE window using Electron deep-link (steal: true) and AppleScript."""
    if sys.platform == "darwin":
        try:
            # Triggers Electron handleDeepLink -> wins[0].restore(); wins[0].show(); wins[0].focus(); app.focus({ steal: true })
            subprocess.run(
                ["open", "antigravity://focus"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
        except Exception:
            pass
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application id "com.google.antigravity" to activate'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
        except Exception:
            pass
    return "Antigravity"


def _sync_macos_terminal_tab_title(tty_name: str, new_title: str) -> None:
    """Sets the custom title of a Terminal.app or iTerm2 tab matching tty_name without requiring System Events."""
    if sys.platform != "darwin":
        return
    short_tty = tty_name.replace("/dev/", "").strip()
    safe_title = new_title.replace('"', "'")
    script = f'''
    if application "Terminal" is running then
        tell application "Terminal"
            repeat with w in windows
                repeat with t in tabs of w
                    try
                        if (tty of t contains "{short_tty}") then
                            set custom title of t to "{safe_title}"
                            set title displays custom title of t to true
                        end if
                    end try
                end repeat
            end repeat
        end tell
    end if
    '''
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _focus_macos_terminal_by_tty_or_title(tty_name: Optional[str], search_tokens: List[str], repo_dir: str) -> Optional[str]:
    """Tries to bring the exact existing Terminal.app or iTerm2 tab to the foreground via AppleScript (without System Events)."""
    if sys.platform != "darwin":
        return None
    short_tty = (tty_name or "").replace("/dev/", "").strip()
    if short_tty == "??":
        short_tty = ""
    tokens = [t.replace('"', "'").strip() for t in search_tokens if t and len(t.strip()) >= 3]

    # 1. Try Terminal.app exact tab match (by TTY first, then by custom/window title)
    cond_parts = []
    if short_tty:
        cond_parts.append(f'(tty of t contains "{short_tty}")')
    for tok in tokens[:3]:
        cond_parts.append(f'(custom title of t contains "{tok}")')
        cond_parts.append(f'(name of w contains "{tok}")')

    if cond_parts:
        cond_expr = " or ".join(cond_parts)
        term_script = f'''
        if application "Terminal" is running then
            tell application "Terminal"
                repeat with w in windows
                    repeat with t in tabs of w
                        try
                            if {cond_expr} then
                                set selected of t to true
                                set index of w to 1
                                activate
                                return "Terminal.app (Aba: " & (name of w) & ")"
                            end if
                        end try
                    end repeat
                end repeat
            end tell
        end if
        return ""
        '''
        try:
            res = subprocess.run(["osascript", "-e", term_script], capture_output=True, text=True, timeout=2.5)
            out = res.stdout.strip()
            if out:
                return out
        except Exception:
            pass

        # 2. Try iTerm2 exact session match (only if iTerm.app is installed)
        if Path("/Applications/iTerm.app").exists() or (Path.home() / "Applications" / "iTerm.app").exists():
            iterm_conds = []
            if short_tty:
                iterm_conds.append(f'(tty of s contains "{short_tty}")')
            for tok in tokens[:3]:
                iterm_conds.append(f'(name of s contains "{tok}")')
            iterm_expr = " or ".join(iterm_conds)
            iterm_script = f'''
            if application "iTerm" is running then
                tell application "iTerm"
                    repeat with w in windows
                        repeat with t in tabs of w
                            repeat with s in sessions of t
                                try
                                    if {iterm_expr} then
                                        select t
                                        select s
                                        set index of w to 1
                                        activate
                                        return "iTerm2 (Sessão: " & (name of s) & ")"
                                    end if
                                end try
                            end repeat
                        end repeat
                    end repeat
                end tell
            end if
            return ""
            '''
            try:
                res = subprocess.run(["osascript", "-e", iterm_script], capture_output=True, text=True, timeout=2.5)
                out = res.stdout.strip()
                if out:
                    return out
            except Exception:
                pass

    return None


def _focus_macos_ide_window_by_title(search_tokens: List[str], preferred_app: Optional[str] = None) -> Optional[str]:
    """Focuses the preferred IDE application without requiring Accessibility permissions."""
    if sys.platform != "darwin" or not preferred_app:
        return None
    if "antigravity" in preferred_app.lower():
        return _focus_antigravity_app()
    installed = _find_installed_mac_app([preferred_app])
    if not installed:
        return None
    try:
        safe_app = installed.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'tell application "{safe_app}" to activate'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        return Path(installed).stem if installed.endswith(".app") else installed
    except Exception:
        return None


def _open_and_activate_mac_app(app_name: str, folder_path: Optional[str] = None) -> None:
    """Activates a macOS app and focuses its window. Never passes folder_path to Terminal/iTerm to avoid spawning new windows."""
    if "antigravity" in app_name.lower():
        _focus_antigravity_app(folder_path)
        return

    resolved_app = _find_installed_mac_app([app_name]) or app_name
    is_terminal_emulator = any(
        t in resolved_app.lower() for t in ("terminal", "iterm", "warp", "ghostty", "alacritty", "wezterm")
    )

    try:
        cmd = ["open", "-a", resolved_app]
        # IMPORTANT: Never pass folder_path to Terminal.app or iTerm2, as `open -a Terminal <dir>` ALWAYS spawns a new window!
        if folder_path and not is_terminal_emulator:
            cmd.append(folder_path)
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0)
    except Exception:
        pass
    if sys.platform == "darwin":
        safe_app = resolved_app.replace('"', "'")
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{safe_app}" to activate'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
        except Exception:
            pass


def focus_agent_environment(ag: AgentUnit, target: str = "auto") -> dict:
    """
    Redirects and brings to the foreground the exact IDE window, existing Terminal tab, or Finder folder
    where the agent is working on macOS — NEVER spawning a new duplicate Terminal window.
    """
    repo_dir = _resolve_valid_repo_dir(ag)
    repo_folder_name = Path(repo_dir).name
    target = (target or "auto").lower().strip()

    # Re-inspect live PID if present to ensure fresh TTY and host_app (unless running inside our managed PTY)
    if ag.pid and ag.control_mode != "pty":
        live_tty, live_host = ClaudeAdapter._inspect_pid_tree(ag.pid)
        if live_tty:
            ag.tty = live_tty
        if live_host:
            ag.host_app = live_host

    search_tokens = [
        ag.workspace_title,
        ag.name,
        repo_folder_name,
        ag.repo_name.split("/")[-1] if ag.repo_name else "",
        ag.repo_name,
    ]

    # 1. Explicit Finder folder open
    if target == "finder":
        subprocess.run(["open", repo_dir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.5)
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", 'tell application "Finder" to activate'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return {"ok": True, "opened_with": f"Finder ({repo_folder_name})", "path": repo_dir}

    # 2. If host_app is explicitly a standalone Terminal (Terminal.app, iTerm2, Warp, Ghostty, etc.), focus that terminal tab first!
    if ag.host_app in _KNOWN_TERMINAL_APPS:
        matched_tab = _focus_macos_terminal_by_tty_or_title(ag.tty, search_tokens, repo_dir)
        if matched_tab:
            return {"ok": True, "opened_with": matched_tab, "path": repo_dir}
        _open_and_activate_mac_app(ag.host_app, None)
        return {"ok": True, "opened_with": f"{ag.host_app} ({ag.workspace_title or ag.repo_name})", "path": repo_dir}

    # 3. If managed by Superset, jump straight to Superset workspace/terminal
    if ag.host_app == "Superset" or (ag.superset_workspace_id and hub.superset_bridge.is_installed()):
        if ag.superset_workspace_id:
            hub.superset_bridge.open_workspace_in_superset_ui(ag.superset_workspace_id, ag.superset_terminal_id)
        return {"ok": True, "opened_with": f"Superset ({ag.superset_workspace_id or ag.workspace_title})", "path": repo_dir}

    # 4. Antigravity conversations (or agents hosted inside Antigravity) focus the Antigravity IDE window directly
    if (ag.host_app and "antigravity" in ag.host_app.lower()) or (ag.provider.lower() == "antigravity" and not ag.host_app):
        _focus_antigravity_app(repo_dir)
        return {
            "ok": True,
            "opened_with": f"Antigravity ({ag.workspace_title or ag.repo_name})",
            "path": repo_dir,
        }

    # 5. CLI agent running inside an IDE's integrated environment (IntelliJ IDEA, Android Studio, VS Code, Cursor, etc.)
    if ag.host_app in _KNOWN_IDE_APPS:
        _open_and_activate_mac_app(ag.host_app, repo_dir)
        return {
            "ok": True,
            "opened_with": f"IDE ({ag.host_app} — {ag.workspace_title or ag.repo_name})",
            "path": repo_dir,
        }

    # 6. Try focusing an existing Terminal.app or iTerm2 tab by TTY or Workspace Title
    matched_tab = _focus_macos_terminal_by_tty_or_title(ag.tty, search_tokens, repo_dir)
    if matched_tab:
        return {"ok": True, "opened_with": matched_tab, "path": repo_dir}

    # 7. Fallback for stopped sessions without a live PID: activate the project's IDE window (IntelliJ IDEA / Android Studio / Antigravity / VS Code)
    preferred_candidates = ["IntelliJ IDEA", "Android Studio", "Antigravity", "Cursor", "Visual Studio Code"]
    if "android" in ag.repo_name.lower() or (Path(repo_dir) / "build.gradle").exists() or (Path(repo_dir) / "build.gradle.kts").exists():
        preferred_candidates = ["Android Studio", "IntelliJ IDEA", "Antigravity", "Visual Studio Code", "Cursor"]

    ide_app = _find_installed_mac_app(preferred_candidates) or "Antigravity"
    _open_and_activate_mac_app(ide_app, repo_dir)
    display_ide = Path(ide_app).stem if ide_app.endswith(".app") else ide_app
    return {"ok": True, "opened_with": f"{display_ide} ({ag.workspace_title or ag.repo_name})", "path": repo_dir}


@app.post("/api/agents/{agent_id}/open-superset")
async def open_agent_in_superset(agent_id: str) -> dict:
    ag = hub.agents.get(agent_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")
    res = focus_agent_environment(ag, target="auto")
    ag.terminal_tail.append(f"↗ [FOCUS] Abrindo ambiente: {res.get('opened_with')} -> {res.get('path')}")
    await hub.broadcast()
    return res


@app.post("/api/agents/{agent_id}/focus")
async def focus_agent_endpoint(agent_id: str, body: dict | None = None) -> dict:
    ag = hub.agents.get(agent_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")
    target = (body or {}).get("target", "auto")
    res = focus_agent_environment(ag, target=target)
    ag.terminal_tail.append(f"🎯 [IR PARA AGENTE] Focado em {res.get('opened_with')} ({res.get('path')})")
    await hub.broadcast()
    return res


@app.post("/api/agents/{agent_id}/archive")
async def archive_agent_endpoint(agent_id: str, body: dict | None = None) -> dict:
    archived = bool((body or {}).get("archived", True))
    ag = hub.set_agent_archived(agent_id, archived)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")
    state_label = "guardado no Histórico" if archived else "restaurado para a Área de Trabalho Ativa"
    ag.terminal_tail.append(f"📦 [HISTÓRICO] Agente {state_label}.")
    await hub.broadcast()
    return {"ok": True, "agent_id": agent_id, "archived": ag.archived, "agent": ag.model_dump()}


@app.post("/api/agents/archive-inactive")
async def archive_inactive_agents_endpoint() -> dict:
    count = 0
    for ag in list(hub.agents.values()):
        # Preserve sessions that still have an active open PID or are the live Antigravity cockpit
        has_live_process = (ag.pid is not None) or (ag.provider == "antigravity")
        if (
            not ag.archived
            and not has_live_process
            and ag.game_state in (AgentGameState.IDLE, AgentGameState.STAGE_CLEAR, AgentGameState.GAME_OVER)
        ):
            hub.set_agent_archived(ag.id, True)
            count += 1
    await hub.broadcast()
    return {"ok": True, "archived_count": count}


@app.post("/api/agents/{agent_id}/neon-action")
async def neon_agent_action_endpoint(agent_id: str, body: dict | None = None) -> dict:
    """Executes a `neon-cli` action (`gh_open`, `gh_prs`, `dd_service`, `dd_logs`, `flow_open`) for the agent's workspace."""
    ag = hub.agents.get(agent_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")
    payload = body or {}
    action = str(payload.get("action") or "gh_open")
    env = str(payload.get("env") or "prod")
    repo_dir = _resolve_valid_repo_dir(ag)
    ag.neon_meta = hub.neon_cli.enrich_agent_metadata(
        repo_dir,
        ag.repo_name,
        ag.branch,
        ag.workspace_title,
        current_action=ag.current_action,
        last_thought=ag.last_thought,
        terminal_tail=ag.terminal_tail,
        provider=ag.provider,
        session_id=ag.session_id,
        check_gh_branch=(action == "gh_prs"),
    )
    meta = ag.neon_meta or {}
    jira_key = payload.get("jira_key") or meta.get("jira_key")
    pr_url = payload.get("pr_url") or meta.get("pr_url")
    pr_number = payload.get("pr_number") or meta.get("pr_number")
    pr_repo = payload.get("pr_repo") or meta.get("pr_repo")
    res = hub.neon_cli.execute_neon_action(
        action=action,
        repo_path=repo_dir,
        repo_name=ag.repo_name,
        jira_key=jira_key,
        env=env,
        pr_url=pr_url,
        pr_number=pr_number,
        pr_repo=pr_repo,
        branch=ag.branch,
    )
    if res.get("opened_specific_pr") and res.get("url") and not ag.neon_meta.get("prs"):
        ag.neon_meta["pr_url"] = res.get("url")
        if res.get("pr_number"):
            ag.neon_meta["pr_number"] = res.get("pr_number")
        ag.neon_meta["pr_source"] = ag.neon_meta.get("pr_source") or "branch"
        ag.neon_meta["prs"] = [
            {
                "pr_url": res.get("url"),
                "pr_number": res.get("pr_number"),
                "pr_repo": pr_repo or ag.neon_meta.get("pr_repo"),
                "pr_source": ag.neon_meta["pr_source"],
            }
        ]
    ag.terminal_tail.append(f"⚡ [NEON-CLI] {res.get('message')} ({res.get('command', action)})")
    await hub.broadcast()
    return res


@app.post("/api/agents/{agent_id}/rename")
async def rename_agent_endpoint(agent_id: str, body: dict | None = None) -> dict:
    new_title = str((body or {}).get("title") or "").strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Informe um título válido.")
    res = hub.rename_agent_workspace(agent_id, new_title, sync_external=True)
    if not res:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")
    await hub.broadcast()
    return res


def _echo_to_agent_tty(ag: AgentUnit, text_line: str) -> None:
    """Mirrors a panel action/output line onto the agent's /dev/ttys* screen so the IDE terminal displays it live."""
    short_tty = (ag.tty or "").replace("/dev/", "").strip()
    if not short_tty or short_tty == "??":
        return
    tty_path = f"/dev/{short_tty}"
    try:
        if os.access(tty_path, os.W_OK):
            with open(tty_path, "wb", buffering=0) as f:
                f.write((text_line + "\r\n").encode("utf-8", errors="replace"))
    except Exception:
        pass


def inject_input_into_live_terminal(
    ag: AgentUnit,
    text: str,
    auto_enter: bool = True,
    is_permission_key: bool = False,
) -> dict:
    """
    Injects a command or permission key ('1', '2', '3', 'y', 'n', or full prompt) directly into
    the live terminal where the agent is running (iTerm2, Terminal.app, or IDE Integrated Terminal).
    """
    clean_text = text.rstrip("\r\n")
    if sys.platform != "darwin":
        return {"injected": False, "method": "non-darwin"}

    if ag.pid:
        live_tty, live_host = ClaudeAdapter._inspect_pid_tree(ag.pid)
        if live_tty:
            ag.tty = live_tty
        if live_host:
            ag.host_app = live_host

    # Always copy to clipboard via pbcopy as an instant fallback
    try:
        subprocess.run(["pbcopy"], input=clean_text.encode("utf-8"), timeout=1.5)
    except Exception:
        pass

    short_tty = (ag.tty or "").replace("/dev/", "").strip()
    repo_dir = _resolve_valid_repo_dir(ag)
    repo_folder = Path(repo_dir).name
    tokens = [
        t.replace('"', "'").strip()
        for t in [ag.workspace_title, ag.name, repo_folder, ag.repo_name.split("/")[-1] if ag.repo_name else ""]
        if t and len(t.strip()) >= 3
    ]

    # 1. Try iTerm2 direct session write (`write text` writes straight to PTY stdin in background without Accessibility!)
    has_iterm_app = Path("/Applications/iTerm.app").exists() or (Path.home() / "Applications" / "iTerm.app").exists()
    iterm_conds = []
    if short_tty and short_tty != "??":
        iterm_conds.append(f'(tty of s contains "{short_tty}")')
    for tok in tokens[:3]:
        iterm_conds.append(f'(name of s contains "{tok}")')
    if has_iterm_app and iterm_conds:
        safe_iterm_text = clean_text.replace("\\", "\\\\").replace('"', '\\"')
        newline_flag = "" if auto_enter else " newline NO"
        iterm_script = f'''
        if application "iTerm" is running then
            tell application "iTerm"
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            try
                                if {" or ".join(iterm_conds)} then
                                    tell s to write text "{safe_iterm_text}"{newline_flag}
                                    return "INJECTED:iTerm2 (" & (name of s) & ")"
                                end if
                            end try
                        end repeat
                    end repeat
                end repeat
            end tell
        end if
        return ""
        '''
        try:
            res = subprocess.run(["osascript", "-e", iterm_script], capture_output=True, text=True, timeout=2.5)
            out = (res.stdout or "").strip()
            if out.startswith("INJECTED:"):
                if short_tty:
                    _TTY_SCREEN_CACHE.pop(short_tty, None)
                return {"injected": True, "method": out.replace("INJECTED:", "")}
        except Exception:
            pass

    # Check if macOS Accessibility is enabled for System Events keystroke injection
    ax_trusted = _is_macos_accessibility_trusted(prompt_user=False)
    if not ax_trusted:
        # For Claude full prompts, do NOT steal window focus — let _run_claude_session_command_async execute cleanly!
        if ag.provider.lower() == "claude" and not is_permission_key:
            return {
                "injected": False,
                "needs_accessibility": True,
                "clipboard_ready": True,
                "method": "Claude Session CLI",
            }
        # For permission keys or Antigravity GUI actions, prompt macOS Accessibility once and focus window cleanly
        _is_macos_accessibility_trusted(prompt_user=True)
        target_ide = "Antigravity" if ag.provider == "antigravity" else (ag.host_app or "Terminal")
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{target_ide}" to activate'],
                capture_output=True,
                timeout=1.5,
            )
        except Exception:
            pass
        return {
            "injected": False,
            "needs_accessibility": True,
            "focused_window": target_ide,
            "clipboard_ready": True,
            "method": f"{target_ide} (Habilite 'Acessibilidade' no macOS p/ envio automático ou use Cmd+V)",
        }

    # 2. Try Terminal.app tab selection + keystroke/paste injection (only if not running inside an IDE terminal)
    term_conds = []
    if short_tty and short_tty != "??" and ag.host_app not in _KNOWN_IDE_APPS:
        term_conds.append(f'(tty of t contains "{short_tty}")')
    if ag.host_app in ("Terminal", "iTerm"):
        for tok in tokens[:3]:
            term_conds.append(f'(custom title of t contains "{tok}")')
            term_conds.append(f'(name of w contains "{tok}")')
    if term_conds:
        is_single_key = len(clean_text) == 1 and clean_text.isalnum()
        if clean_text.upper() == "ESC":
            send_stmt = "key code 53"
        elif clean_text.upper() == "ENTER" or clean_text == "":
            send_stmt = "key code 36"
        elif is_single_key:
            enter_part = "\ndelay 0.05\nkey code 36" if auto_enter else ""
            send_stmt = f'keystroke "{clean_text}"{enter_part}'
        else:
            enter_part = "\ndelay 0.08\nkey code 36" if auto_enter else ""
            send_stmt = f'keystroke "v" using command down{enter_part}'

        term_script = f'''
        if application "Terminal" is running then
            tell application "Terminal"
                repeat with w in windows
                    repeat with t in tabs of w
                        try
                            if {" or ".join(term_conds)} then
                                set selected of t to true
                                set index of w to 1
                                activate
                                delay 0.12
                                try
                                    tell application "System Events"
                                        tell process "Terminal"
                                            {send_stmt}
                                        end tell
                                    end tell
                                    return "INJECTED:Terminal.app (" & (name of w) & ")"
                                on error
                                    return "FOCUSED:Terminal.app (" & (name of w) & ")"
                                end try
                            end if
                        end try
                    end repeat
                end repeat
            end tell
        end if
        return ""
        '''
        try:
            res = subprocess.run(["osascript", "-e", term_script], capture_output=True, text=True, timeout=3.0)
            out = (res.stdout or "").strip()
            if out.startswith("INJECTED:"):
                if short_tty:
                    _TTY_SCREEN_CACHE.pop(short_tty, None)
                return {"injected": True, "method": out.replace("INJECTED:", "")}
            if out.startswith("FOCUSED:"):
                return {
                    "injected": False,
                    "focused_window": out.replace("FOCUSED:", ""),
                    "clipboard_ready": True,
                    "method": f"{out.replace('FOCUSED:', '')} (Focado + Copiado p/ Cmd+V)",
                }
        except Exception:
            pass

    # 3. Try IDE Integrated Terminal (IntelliJ IDEA / Android Studio / VS Code / Cursor / Antigravity)
    if ag.host_app in _KNOWN_IDE_APPS or ag.provider == "antigravity":
        target_ide = "Antigravity" if ag.provider == "antigravity" else (ag.host_app or "Antigravity")
        # Focus the exact IDE window WITHOUT calling `open -a <IDE> <repo_dir>` so terminal focus inside the IDE is preserved!
        focused_win = _focus_window_by_title_in_process([target_ide], tokens)
        if not focused_win:
            try:
                subprocess.run(["osascript", "-e", f'tell application "{target_ide}" to activate'], capture_output=True, timeout=1.5)
            except Exception:
                pass
        is_single_key = len(clean_text) == 1 and clean_text.isalnum()
        if clean_text.upper() == "ESC":
            send_stmt = "key code 53"
        elif clean_text.upper() == "ENTER" or clean_text == "":
            send_stmt = "key code 36"
        elif ag.provider == "antigravity" and is_permission_key and clean_text.lower() in ("y", "1"):
            send_stmt = "key code 36 using command down\ndelay 0.08\nkey code 36"
        elif is_single_key:
            enter_part = "\ndelay 0.05\nkey code 36" if auto_enter else ""
            send_stmt = f'keystroke "{clean_text}"{enter_part}'
        else:
            enter_part = "\ndelay 0.1\nkey code 36" if auto_enter else ""
            send_stmt = f'keystroke "v" using command down{enter_part}'

        key_script = f'''
        delay 0.15
        tell application "System Events"
            {send_stmt}
        end tell
        return "INJECTED:{target_ide}"
        '''
        try:
            res = subprocess.run(["osascript", "-e", key_script], capture_output=True, text=True, timeout=2.5)
            out = (res.stdout or "").strip()
            if res.returncode == 0 and out.startswith("INJECTED:"):
                return {"injected": True, "method": out.replace("INJECTED:", "")}
        except Exception:
            pass
        return {
            "injected": False,
            "focused_window": target_ide,
            "clipboard_ready": True,
            "method": f"{target_ide} (Janela focada + Comando copiado p/ Cmd+V)",
        }

    return {"injected": False, "clipboard_ready": True, "method": "Clipboard (Cmd+V)"}


def _run_claude_session_command_async(ag: AgentUnit, prompt_text: str) -> bool:
    """
    Executes a prompt directly in the agent's Claude Code session (`claude --resume <session_id> -p <prompt>`)
    with `--output-format stream-json --verbose` in a background thread, streaming every thought, tool call,
    and assistant response live into `ag.terminal_tail` and mirroring onto the agent's `/dev/ttys*` screen.
    """
    if ag.provider.lower() != "claude" or not ag.session_id:
        return False

    claude_bin = "claude"
    for cand in [
        Path.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]:
        if cand.exists():
            claude_bin = str(cand)
            break

    repo_dir = _resolve_valid_repo_dir(ag)
    session_id = ag.session_id
    hub.active_panel_commands[ag.id] = time.time()

    def _append_live_line(line: str, action_summary: Optional[str] = None) -> None:
        hub.record_panel_line(ag.id, line)
        if action_summary:
            ag.current_action = action_summary[:140]
        ag.last_active_ts = time.time()
        ag.updated_at = utc_now_iso()
        _echo_to_agent_tty(ag, line)

    def _worker() -> None:
        ag.game_state = AgentGameState.CHOMPING
        ag.current_action = f"Executando: {prompt_text[:100]}"
        _echo_to_agent_tty(ag, f"\r\n⚡ [PAC-NEON PAINEL] ❯ {prompt_text}")
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
        }
        try:
            proc = subprocess.Popen(
                [
                    claude_bin,
                    "--resume",
                    session_id,
                    "-p",
                    prompt_text,
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ],
                cwd=repo_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=clean_env,
            )
            hub.active_panel_procs[ag.id] = proc
            if proc.stdout:
                for raw_line in proc.stdout:
                    ln = raw_line.strip()
                    if not ln:
                        continue
                    hub.active_panel_commands[ag.id] = time.time()
                    if ln.startswith("{") and ln.endswith("}"):
                        try:
                            evt = json.loads(ln)
                        except Exception:
                            _append_live_line(f"● {ln}", ln)
                            continue
                        etype = evt.get("type")
                        if etype == "assistant":
                            msg = evt.get("message") or {}
                            for blk in msg.get("content") or []:
                                if not isinstance(blk, dict):
                                    continue
                                btype = blk.get("type")
                                if btype == "thinking":
                                    th = str(blk.get("thinking") or "").strip().splitlines()
                                    if th:
                                        ag.last_thought = th[-1][:180]
                                        _append_live_line(f"✻ Thinking: {th[-1][:160]}", f"Pensando: {th[-1][:100]}")
                                elif btype == "tool_use":
                                    tname = str(blk.get("name") or "Tool")
                                    tinp = blk.get("input") if isinstance(blk.get("input"), dict) else {}
                                    tprev = (
                                        tinp.get("command")
                                        or tinp.get("file_path")
                                        or tinp.get("pattern")
                                        or json.dumps(tinp, ensure_ascii=False)[:100]
                                    )
                                    ag.active_tool = tname
                                    ag.score_xp += 45
                                    _append_live_line(f"● {tname}({str(tprev)[:140]})", f"Executando {tname}: {str(tprev)[:90]}")
                                elif btype == "text":
                                    txt = str(blk.get("text") or "").strip()
                                    for tline in txt.splitlines():
                                        if tline.strip():
                                            _append_live_line(f"● {tline.strip()}", tline.strip())
                        elif etype == "user":
                            msg = evt.get("message") or {}
                            for blk in msg.get("content") or []:
                                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                    rc = blk.get("content")
                                    if isinstance(rc, str) and rc.strip():
                                        first_ln = rc.strip().splitlines()[0][:150]
                                        _append_live_line(f"  ⎿  {first_ln}")
                        elif etype == "result":
                            res_txt = str(evt.get("result") or "").strip()
                            if evt.get("is_error"):
                                errs = evt.get("errors") or [res_txt or "Erro na execução"]
                                _append_live_line(f"  ⎿  ⚠️ {errs[0]}", f"Erro: {errs[0]}")
                            elif res_txt and not any(res_txt[:40] in x for x in ag.terminal_tail[-5:]):
                                for rline in res_txt.splitlines()[:12]:
                                    if rline.strip():
                                        _append_live_line(f"● {rline.strip()}", rline.strip())
                    else:
                        _append_live_line(f"● {ln}", ln)
            proc.wait(timeout=300)
            ag.game_state = AgentGameState.IDLE
            ag.current_action = "Parado — comando do painel concluído (aguardando próximo comando)"
            _append_live_line("❯ [Comando concluído — aguardando sua próxima ação]")
        except Exception as exc:
            ag.game_state = AgentGameState.IDLE
            _append_live_line(f"  ⎿  ℹ️ Execução concluída ({exc})")
        finally:
            hub.active_panel_procs.pop(ag.id, None)
            hub.active_panel_commands.pop(ag.id, None)

    threading.Thread(target=_worker, daemon=True).start()
    return True


def _send_antigravity_command_async(ag: AgentUnit, prompt_text: str) -> dict:
    """
    Sends a user prompt directly to an Antigravity agent via the live Antigravity.app IDE
    LanguageServerService ConnectRPC (`SendUserCascadeMessage`) and streams the live trajectory
    updates into `ag.terminal_tail`.
    """
    if ag.provider.lower() != "antigravity" or not ag.session_id:
        return {"injected": False, "error": "Sessão Antigravity inválida"}

    session_id = ag.session_id
    pre_traj = AntigravityAdapter.get_live_trajectory(session_id) or {}
    pre_steps = ((pre_traj.get("trajectory") or {}).get("steps") or [])
    start_step_count = len(pre_steps)

    rpc_res = AntigravityAdapter.send_cascade_message(session_id, prompt_text)
    if rpc_res.get("ok"):
        hub.active_panel_commands[ag.id] = time.time()

        def _watch_rpc_turn() -> None:
            seen_idx = start_step_count
            idle_polls = 0
            try:
                for _ in range(75):
                    time.sleep(0.8)
                    t_resp = AntigravityAdapter.get_live_trajectory(session_id)
                    if not isinstance(t_resp, dict):
                        break
                    status = str(t_resp.get("status") or "")
                    steps = ((t_resp.get("trajectory") or {}).get("steps") or [])
                    if len(steps) > seen_idx:
                        # Live trajectory has picked up the turn — let refresh_discovered_agents drive state directly
                        hub.active_panel_commands.pop(ag.id, None)
                        for st in steps[seen_idx:]:
                            if not isinstance(st, dict):
                                continue
                            pr = st.get("plannerResponse") or {}
                            thinking = str(pr.get("thinking") or "").strip()
                            if thinking:
                                th_ln = thinking.splitlines()[-1][:150]
                                ag.last_thought = th_ln
                                hub.record_panel_line(ag.id, f"✻ Thinking: {th_ln}")
                            reply = str(pr.get("response") or pr.get("text") or "").strip()
                            if reply:
                                for rln in reply.splitlines()[:6]:
                                    if rln.strip():
                                        hub.record_panel_line(ag.id, f"● {rln.strip()[:160]}")
                                ag.current_action = reply.splitlines()[-1][:140]
                            rc = st.get("runCommand") or {}
                            if rc:
                                cmd_s = rc.get("commandLine") or rc.get("proposedCommandLine") or "command"
                                ag.active_tool = "run_command"
                                hub.record_panel_line(ag.id, f"● Bash({str(cmd_s)[:140]})")
                        seen_idx = len(steps)
                    if status == "CASCADE_RUN_STATUS_WAITING_FOR_USER":
                        ag.game_state = AgentGameState.POWER_PELLET_WAIT
                        break
                    if status == "CASCADE_RUN_STATUS_IDLE" and seen_idx > start_step_count:
                        idle_polls += 1
                        if idle_polls >= 2:
                            ag.game_state = AgentGameState.IDLE
                            ag.active_tool = None
                            break
                    elif status == "CASCADE_RUN_STATUS_IDLE":
                        idle_polls += 1
                        if idle_polls >= 6:
                            break
            except Exception:
                pass
            finally:
                hub.active_panel_commands.pop(ag.id, None)

        threading.Thread(target=_watch_rpc_turn, daemon=True).start()
        return {
            "injected": True,
            "method": rpc_res.get("method", f"Antigravity ConnectRPC ({session_id[:8]})"),
        }

    return {"injected": False, "error": rpc_res.get("error", "Falha ao contatar Antigravity ConnectRPC")}


@app.post("/api/permissions/{perm_id}/resolve")
async def resolve_permission(perm_id: str, req: ResolvePermissionRequest) -> dict:
    target_agent: AgentUnit | None = None
    for ag in hub.agents.values():
        if ag.pending_permission and ag.pending_permission.id == perm_id:
            target_agent = ag
            break

    # Fallback lookup by agent_id suffix if permission ID changed between hook & transcript poll
    if not target_agent:
        for ag in hub.agents.values():
            if ag.pending_permission and (perm_id.endswith(ag.id) or ag.id in perm_id):
                target_agent = ag
                break

    if not target_agent or not target_agent.pending_permission:
        raise HTTPException(status_code=404, detail="Permissão não encontrada ou já resolvida.")

    perm = target_agent.pending_permission
    adapter = AdapterRegistry.get(target_agent.provider)
    seq = (
        adapter.format_approval_Sequence(req.action.value, req.custom_text)
        if adapter
        else ("y\r" if req.action != PermissionResolutionAction.DENY else "n\r")
    )

    # Record dismissal mtime so auto-discovery doesn't re-pop the same permission before transcript updates
    hub.dismissed_perm_mtime[target_agent.id] = max(time.time(), target_agent.last_active_ts or 0.0)
    hub._save_workspace_state()

    # 1. If managed by Superset CLI (worktree + terminal)
    superset_dispatched = False
    if target_agent.superset_workspace_id and target_agent.superset_terminal_id:
        superset_dispatched = hub.superset_bridge.resolve_permission_in_superset(
            workspace_id=target_agent.superset_workspace_id,
            terminal_id=target_agent.superset_terminal_id,
            action=req.action,
            custom_text=req.custom_text,
        )

    # 2. If managed by local PTYSessionManager
    live_injected_info: Dict[str, Any] = {}
    hook_dispatched = False
    if target_agent.id in hub.pty_manager.sessions:
        hub.pty_manager.sessions[target_agent.id].resolve_permission(req.action, req.custom_text)
        live_injected_info = {"injected": True, "method": "PTY Local"}
    else:
        # 3. If connected via CLI wrapper bridge queue
        hub.bridge_input_queues.setdefault(target_agent.id, []).append(seq)

        # 4. If waiting on a synchronous Hook (Future or Threading Event) — resolves natively in Claude Code!
        decision_payload = {
            "decision": "deny" if req.action == PermissionResolutionAction.DENY else "allow",
            "action": req.action.value,
            "custom_text": req.custom_text,
        }
        candidate_perm_ids = {perm.id, perm_id, f"perm-hook-{target_agent.id}"}
        mapped_hook_id = hub.agent_hook_events.pop(target_agent.id, None)
        if mapped_hook_id:
            candidate_perm_ids.add(mapped_hook_id)

        for cid in candidate_perm_ids:
            waiter = hub.hook_waiters.pop(cid, None)
            if waiter and not waiter.done():
                waiter.set_result(decision_payload)
                hook_dispatched = True
            ev = hub.hook_events.pop(cid, None)
            if ev:
                hub.hook_results[cid] = decision_payload
                ev.set()
                hook_dispatched = True

        if hook_dispatched:
            live_injected_info = {
                "injected": True,
                "method": "Hook Nativo Claude Code (Tempo Real)",
            }
            _echo_to_agent_tty(
                target_agent,
                f"\r\n🟢 [PAC-NEON PAINEL] Permissão decidida ({req.action.value.upper()})"
                + (f": {req.custom_text}" if req.custom_text else ""),
            )
        elif target_agent.provider.lower() == "antigravity" and target_agent.session_id:
            # 5a. Native Antigravity LanguageServerService ConnectRPC resolution!
            agy_rpc = AntigravityAdapter.resolve_cascade_interaction(
                target_agent.session_id,
                req.action.value,
                req.custom_text,
            )
            if agy_rpc.get("ok"):
                live_injected_info = {
                    "injected": True,
                    "method": agy_rpc.get("method", "Antigravity ConnectRPC"),
                }
            else:
                live_injected_info = inject_input_into_live_terminal(
                    target_agent,
                    req.custom_text or ("n" if req.action == PermissionResolutionAction.DENY else "y"),
                    auto_enter=True,
                    is_permission_key=(not bool(req.custom_text)),
                )
        else:
            # 5b. For any permission not waiting on a blocking hook (terminal_prompt, sdk, etc.), inject into live terminal/IDE
            if req.custom_text:
                key_to_send = req.custom_text
            elif target_agent.provider.lower() == "claude":
                key_to_send = (
                    "3"
                    if req.action == PermissionResolutionAction.DENY
                    else ("2" if req.action == PermissionResolutionAction.ALWAYS else "1")
                )
            else:
                key_to_send = "n" if req.action == PermissionResolutionAction.DENY else "y"

            live_injected_info = inject_input_into_live_terminal(
                target_agent,
                key_to_send,
                auto_enter=True,
                is_permission_key=(not bool(req.custom_text)),
            )
            if (
                not live_injected_info.get("injected")
                and req.custom_text
                and target_agent.provider.lower() == "claude"
                and target_agent.session_id
            ):
                if _run_claude_session_command_async(target_agent, req.custom_text):
                    live_injected_info = {
                        "injected": True,
                        "method": f"Claude CLI (--resume {target_agent.session_id[:8]})",
                    }

        perm.status = "DENIED" if req.action == PermissionResolutionAction.DENY else "APPROVED"
        perm.resolved_at = utc_now_iso()
        target_agent.pending_permission = None
        hub.hook_state_overrides[target_agent.id] = {
            "ts": time.time(),
            "game_state": AgentGameState.THINKING if req.action == PermissionResolutionAction.DENY else AgentGameState.CHOMPING,
            "current_action": f"Decisão ({req.action.value}) enviada pelo painel Pac-Neon",
            "pending_permission": None,
        }

        via_label = (
            live_injected_info.get("method")
            or (
                f"superset terminals send --workspace {target_agent.superset_workspace_id}"
                if target_agent.superset_workspace_id
                else "Pac-Neon Terminal Bridge"
            )
        )
        if req.action == PermissionResolutionAction.DENY:
            target_agent.game_state = AgentGameState.THINKING
            target_agent.current_action = f"Permissão negada via {via_label}."
            hub.record_panel_line(target_agent.id, f"🔴 [PAC-NEON -> {via_label}] > Permissão NEGADA")
        else:
            target_agent.game_state = AgentGameState.CHOMPING
            target_agent.score_xp += 500
            target_agent.pellets_eaten = min(target_agent.pellets_total, target_agent.pellets_eaten + 3)
            label = "SEMPRE PERMITIR (2)" if req.action == PermissionResolutionAction.ALWAYS else "PERMITIR (1/Y)"
            target_agent.current_action = f"Resposta [{label}] enviada via {via_label}!"
            hub.record_panel_line(
                target_agent.id,
                f"🟢 [PAC-NEON +500 PTS -> {via_label}] > {label}: {perm.command_preview}",
            )

        target_agent.updated_at = utc_now_iso()

    delivered = bool(
        hook_dispatched
        or superset_dispatched
        or live_injected_info.get("injected")
    )
    await hub.broadcast()
    return {
        "ok": True,
        "delivered": delivered,
        "hook_dispatched": hook_dispatched,
        "superset_dispatched": superset_dispatched,
        "live_terminal": live_injected_info,
        "agent": target_agent.model_dump(),
        "state": hub.snapshot(),
    }


def interrupt_live_terminal_execution(ag: AgentUnit) -> dict:
    """
    Sends an interrupt signal (ESC + SIGINT / Ctrl+C) to stop a running CLI/terminal agent task
    without terminating the interactive session itself or stealing focus unnecessarily.
    """
    import signal as _signal

    methods: List[str] = []
    interrupted = False

    if ag.pid:
        try:
            live_tty, live_host = ClaudeAdapter._inspect_pid_tree(ag.pid)
            if live_tty:
                ag.tty = live_tty
            if live_host:
                ag.host_app = live_host
        except Exception:
            pass

    # 1. If the agent has a panel-spawned subprocess (`claude --resume`), stop it immediately
    panel_proc = hub.active_panel_procs.pop(ag.id, None)
    if panel_proc is not None:
        try:
            if panel_proc.poll() is None:
                try:
                    panel_proc.send_signal(_signal.SIGINT)
                except Exception:
                    pass
                try:
                    panel_proc.terminate()
                except Exception:
                    pass
                interrupted = True
                methods.append(f"Subprocesso Painel (PID {panel_proc.pid} SIGINT)")
        except Exception:
            pass

    # 2. If managed by local PTYSessionManager, write ESC + Ctrl+C (\x1b\x03)
    if ag.id in hub.pty_manager.sessions:
        try:
            hub.pty_manager.send_command(ag, "\x1b\x03", auto_enter=False)
            interrupted = True
            methods.append("PTY Local (ESC + Ctrl+C)")
        except Exception:
            pass

    # 3. If managed by Superset CLI
    if ag.superset_workspace_id and ag.superset_terminal_id:
        try:
            if hub.superset_bridge.send_to_terminal(
                workspace_id=ag.superset_workspace_id,
                terminal_id=ag.superset_terminal_id,
                text="\x03",
            ):
                interrupted = True
                methods.append(f"Superset ({ag.superset_workspace_id} Ctrl+C)")
        except Exception:
            pass

    # 4. Interrupt any running child tool processes spawned by ag.pid (e.g. bash command, test suite)
    if ag.pid:
        try:
            pg_res = subprocess.run(
                ["pgrep", "-P", str(ag.pid)],
                capture_output=True,
                text=True,
                timeout=1.2,
            )
            child_pids = [int(p.strip()) for p in (pg_res.stdout or "").splitlines() if p.strip().isdigit()]
            killed_children = 0
            for cpid in child_pids:
                try:
                    os.kill(cpid, _signal.SIGINT)
                    killed_children += 1
                except Exception:
                    pass
            if killed_children > 0:
                interrupted = True
                methods.append(f"SIGINT em {killed_children} subprocesso(s) filho(s)")
        except Exception:
            pass

    # 5. If running in iTerm2, inject ESC (ASCII 27) + Ctrl+C (ASCII 3) directly into the PTY in background!
    short_tty = (ag.tty or "").replace("/dev/", "").strip()
    repo_dir = _resolve_valid_repo_dir(ag)
    repo_folder = Path(repo_dir).name
    tokens = [
        t.replace('"', "'").strip()
        for t in [ag.workspace_title, ag.name, repo_folder, ag.repo_name.split("/")[-1] if ag.repo_name else ""]
        if t and len(t.strip()) >= 3
    ]
    has_iterm_app = Path("/Applications/iTerm.app").exists() or (Path.home() / "Applications" / "iTerm.app").exists()
    iterm_conds = []
    if short_tty and short_tty != "??":
        iterm_conds.append(f'(tty of s contains "{short_tty}")')
    for tok in tokens[:3]:
        iterm_conds.append(f'(name of s contains "{tok}")')
    if has_iterm_app and iterm_conds and sys.platform == "darwin":
        iterm_stop_script = f'''
        if application "iTerm" is running then
            tell application "iTerm"
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            try
                                if {" or ".join(iterm_conds)} then
                                    tell s to write text (ASCII character 27) newline NO
                                    delay 0.05
                                    tell s to write text (ASCII character 3) newline NO
                                    return "STOPPED:iTerm2 (" & (name of s) & ")"
                                end if
                            end try
                        end repeat
                    end repeat
                end repeat
            end tell
        end if
        return ""
        '''
        try:
            res = subprocess.run(["osascript", "-e", iterm_stop_script], capture_output=True, text=True, timeout=2.5)
            out = (res.stdout or "").strip()
            if out.startswith("STOPPED:"):
                interrupted = True
                methods.append(out.replace("STOPPED:", "") + " [ESC + Ctrl+C]")
        except Exception:
            pass

    # 6. Send SIGINT directly to ag.pid if it's a live CLI process (Claude / Gemini / Codex) and wasn't already stopped via iTerm
    if ag.pid and not any("iTerm2" in m for m in methods):
        try:
            os.kill(int(ag.pid), _signal.SIGINT)
            interrupted = True
            methods.append(f"SIGINT direto no PID {ag.pid}")
        except Exception:
            pass

    # 7. Fallback to Esc key injection if still not interrupted
    if not interrupted:
        esc_res = inject_input_into_live_terminal(ag, "Esc", auto_enter=False, is_permission_key=True)
        if esc_res.get("injected"):
            interrupted = True
            methods.append(f"{esc_res.get('method')} [Esc]")

    return {
        "ok": interrupted,
        "injected": interrupted,
        "method": " + ".join(methods) if methods else "Sinal de parada enviado",
    }


@app.post("/api/agents/{agent_id}/stop")
async def stop_agent_execution(agent_id: str) -> dict:
    """Immediately interrupts/stops the current execution of an Antigravity or CLI/Terminal agent."""
    ag = hub.agents.get(agent_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")

    hub.active_panel_commands.pop(ag.id, None)

    # 1. Unblock any waiting synchronous PermissionRequest hook with deny/abort
    hook_aborted = False
    candidate_ids = {f"perm-hook-{ag.id}"}
    if ag.pending_permission and ag.pending_permission.id:
        candidate_ids.add(ag.pending_permission.id)
    mapped_hook_id = hub.agent_hook_events.pop(ag.id, None)
    if mapped_hook_id:
        candidate_ids.add(mapped_hook_id)

    abort_payload = {
        "decision": "deny",
        "action": "deny",
        "custom_text": "Execução interrompida pelo usuário no painel PAC-NEON.",
    }
    for cid in candidate_ids:
        waiter = hub.hook_waiters.pop(cid, None)
        if waiter and not waiter.done():
            waiter.set_result(abort_payload)
            hook_aborted = True
        ev = hub.hook_events.pop(cid, None)
        if ev:
            hub.hook_results[cid] = abort_payload
            ev.set()
            hook_aborted = True

    hub.bridge_input_queues.setdefault(ag.id, []).append("\x1b\x03")

    # 2. Dispatch provider-specific stop/cancel
    stop_info: Dict[str, Any] = {}
    if ag.provider.lower() == "antigravity" and ag.session_id:
        rpc_stop = AntigravityAdapter.cancel_cascade_invocation(ag.session_id)
        if rpc_stop.get("ok"):
            stop_info = {
                "ok": True,
                "injected": True,
                "method": rpc_stop.get("method", f"Antigravity ConnectRPC (CancelCascadeInvocation -> {ag.session_id[:8]})"),
                "cancelled_steps": rpc_stop.get("cancelled_steps") or [],
            }
        else:
            stop_info = interrupt_live_terminal_execution(ag)
    else:
        stop_info = interrupt_live_terminal_execution(ag)

    if hook_aborted and not stop_info.get("ok"):
        stop_info = {"ok": True, "injected": True, "method": "Hook Nativo Cancelado"}

    via_method = stop_info.get("method") or "Sinal de interrupção (Stop)"
    ag.pending_permission = None
    ag.active_tool = None
    ag.game_state = AgentGameState.IDLE
    ag.current_action = f"⏹ Execução interrompida pelo usuário ({via_method})"
    ag.updated_at = utc_now_iso()
    hub.dismissed_perm_mtime[ag.id] = max(time.time(), ag.last_active_ts or 0.0)
    hub.hook_state_overrides[ag.id] = {
        "ts": time.time(),
        "game_state": AgentGameState.IDLE,
        "current_action": f"⏹ Execução interrompida ({via_method})",
        "pending_permission": None,
    }
    hub.record_panel_line(ag.id, f"⏹ [PAC-NEON STOP] Execução interrompida via {via_method}")
    _echo_to_agent_tty(ag, f"\r\n⏹ [PAC-NEON PAINEL] Execução interrompida ({via_method})")
    hub._save_workspace_state()

    await hub.broadcast()
    return {
        "ok": True,
        "stopped": bool(stop_info.get("ok") or hook_aborted),
        "method": via_method,
        "details": stop_info,
        "agent": ag.model_dump(),
        "state": hub.snapshot(),
    }


@app.post("/api/agents/{agent_id}/command")
async def send_agent_command(agent_id: str, req: DirectCommandRequest) -> dict:
    ag = hub.agents.get(agent_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")

    prompt_clean = req.prompt.strip()
    if not prompt_clean:
        raise HTTPException(status_code=400, detail="Comando vazio.")

    # Support /stop, /parar, /cancel, /interrupt, and ctrl+c directly from the prompt input bar!
    if prompt_clean.lower() in ("/stop", "/parar", "/cancel", "/interrupt", "ctrl+c", "^c"):
        stop_res = await stop_agent_execution(agent_id)
        return {
            "ok": True,
            "delivered": True,
            "stopped": True,
            "superset_sent": False,
            "live_terminal": {"injected": True, "method": stop_res.get("method", "Stop")},
            "agent": stop_res.get("agent"),
            "state": stop_res.get("state"),
        }

    # If the agent has a waiting synchronous PermissionRequest hook and the user sends 1/2/3/y/n or custom text,
    # resolve the waiting hook directly!
    mapped_hook_id = hub.agent_hook_events.pop(ag.id, None)
    if mapped_hook_id and mapped_hook_id in hub.hook_events:
        ev = hub.hook_events.pop(mapped_hook_id, None)
        if ev:
            low = prompt_clean.lower()
            if low in ("1", "y", "yes", "sim"):
                hub.hook_results[mapped_hook_id] = {"decision": "allow", "action": "allow", "custom_text": None}
            elif low in ("2", "always", "sempre"):
                hub.hook_results[mapped_hook_id] = {"decision": "allow", "action": "always", "custom_text": None}
            elif low in ("3", "n", "no", "nao", "não", "esc"):
                hub.hook_results[mapped_hook_id] = {"decision": "deny", "action": "deny", "custom_text": None}
            else:
                hub.hook_results[mapped_hook_id] = {"decision": "deny", "action": "deny", "custom_text": prompt_clean}
            ev.set()
            ag.pending_permission = None
            ag.game_state = AgentGameState.CHOMPING
            ag.current_action = f"Respondido via Hook Nativo: {prompt_clean[:80]}"
            hub.record_panel_line(ag.id, f"🟢 [HOOK NATIVO] ❯ {prompt_clean}")
            _echo_to_agent_tty(ag, f"\r\n🟢 [PAC-NEON PAINEL] ❯ {prompt_clean}")
            ag.updated_at = utc_now_iso()
            await hub.broadcast()
            return {
                "ok": True,
                "delivered": True,
                "superset_sent": False,
                "live_terminal": {"injected": True, "method": "Hook Nativo Claude Code"},
                "agent": ag.model_dump(),
                "state": hub.snapshot(),
            }

    had_pending_perm = ag.pending_permission is not None
    if ag.pending_permission:
        ag.pending_permission = None
    hub.dismissed_perm_mtime[ag.id] = max(time.time(), ag.last_active_ts or 0.0)
    hub._save_workspace_state()

    ag.game_state = AgentGameState.CHOMPING
    ag.current_action = f"Comando enviado: {prompt_clean[:100]}"
    hub.record_panel_line(ag.id, f"❯ {prompt_clean}")

    superset_sent = False
    if ag.superset_workspace_id and ag.superset_terminal_id:
        superset_sent = hub.superset_bridge.send_to_terminal(
            workspace_id=ag.superset_workspace_id,
            terminal_id=ag.superset_terminal_id,
            text=prompt_clean,
        )

    pty_sent = False
    if ag.id in hub.pty_manager.sessions:
        pty_sent = hub.pty_manager.send_command(ag, prompt_clean, auto_enter=req.auto_enter)

    hub.bridge_input_queues.setdefault(agent_id, []).append(prompt_clean + "\r")

    live_result: Dict[str, Any] = {"injected": False, "method": "fila local"}
    if not superset_sent and not pty_sent:
        is_quick_key = prompt_clean in ("1", "2", "3", "y", "n", "Y", "N", "Enter", "Esc")
        if ag.provider.lower() == "antigravity" and ag.session_id:
            if is_quick_key or had_pending_perm:
                low_k = prompt_clean.lower()
                if low_k in ("1", "y", "enter"):
                    act = "allow"
                    custom = None
                elif low_k == "2":
                    act = "always"
                    custom = None
                elif low_k in ("3", "n", "esc"):
                    act = "deny"
                    custom = None
                else:
                    act = "allow"
                    custom = prompt_clean
                rpc_out = AntigravityAdapter.resolve_cascade_interaction(ag.session_id, act, custom)
                if rpc_out.get("ok"):
                    live_result = {
                        "injected": True,
                        "method": rpc_out.get("method", f"Antigravity ConnectRPC ({ag.session_id[:8]})"),
                    }
                    hub.record_panel_line(
                        ag.id,
                        f"  ⎿  ⚡ Entregue direto ao Antigravity via {live_result['method']}",
                    )
            if not live_result.get("injected"):
                live_result = _send_antigravity_command_async(ag, prompt_clean)
                if live_result.get("injected"):
                    hub.record_panel_line(
                        ag.id,
                        f"  ⎿  ⚡ Entregue direto ao console Antigravity via {live_result.get('method')}",
                    )
        else:
            live_result = inject_input_into_live_terminal(
                ag,
                prompt_clean,
                auto_enter=req.auto_enter,
                is_permission_key=is_quick_key,
            )
            if live_result.get("injected"):
                hub.record_panel_line(ag.id, f"  ⎿  ⚡ Enviado direto ao terminal ativo via {live_result.get('method')}")
            elif ag.provider.lower() == "claude" and ag.session_id and not is_quick_key:
                # Execute directly via `claude --resume <session_id> -p --output-format stream-json --verbose`
                started = _run_claude_session_command_async(ag, prompt_clean)
                if started:
                    live_result = {"injected": True, "method": f"Claude CLI (--resume {ag.session_id[:8]})"}
                    hub.record_panel_line(
                        ag.id,
                        f"  ⎿  ⚡ Executando em tempo real na sessão Claude ({ag.session_id[:8]}) em {ag.repo_name}...",
                    )
            elif live_result.get("focused_window"):
                hub.record_panel_line(
                    ag.id,
                    f"  ⎿  📋 {live_result.get('method')}",
                )

    ag.terminal_tail = ag.terminal_tail[-800:]
    ag.updated_at = utc_now_iso()
    await hub.broadcast()
    return {
        "ok": True,
        "delivered": bool(superset_sent or pty_sent or live_result.get("injected")),
        "superset_sent": superset_sent,
        "live_terminal": live_result,
        "agent": ag.model_dump(),
        "state": hub.snapshot(),
    }


@app.post("/api/agents/{agent_id}/trigger-permission")
async def trigger_test_permission(agent_id: str) -> dict:
    """Simulates or triggers a permission request on an agent to test 1-click remote approval."""
    ag = hub.agents.get(agent_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")

    sample_commands = {
        "antigravity": ("run_command", "dotnet test tests/InvoiceStatement.UnitTests --filter Category=Contract"),
        "claude": ("Bash", "git push -u origin feat/invoice-installment-schema"),
        "gemini": ("Bash", "terraform apply -var-file=envs/stg.tfvars"),
        "codex": ("Bash", "npm run test:e2e:update-snapshots"),
    }
    tool_name, cmd = sample_commands.get(ag.provider.lower(), ("Bash", f" Execute task in {ag.repo_name}? [y/n]"))

    channel = "superset" if ag.superset_workspace_id else "pty"
    perm = PermissionRequestItem(
        agent_id=ag.id,
        agent_name=ag.name,
        provider=ag.provider,
        repo_name=ag.repo_name,
        tool_name=f"Superset ({tool_name})" if ag.superset_workspace_id else tool_name,
        command_preview=cmd,
        reason=(
            f"O agente {ag.name} entrou em estado 'blocked' na worktree {ag.superset_workspace_id}. Aprovar via `superset terminals send`?"
            if ag.superset_workspace_id
            else f"O agente {ag.name} solicita permissão para rodar {tool_name} em {ag.repo_name}."
        ),
        channel=channel,
    )
    ag.pending_permission = perm
    ag.game_state = AgentGameState.POWER_PELLET_WAIT
    ag.current_action = f"AGUARDANDO PERMISSÃO: {cmd}"
    ag.terminal_tail.append(f"⚠️ [PROMPT] Allow {tool_name}: '{cmd}'? [y/n]: ")
    ag.updated_at = utc_now_iso()

    await hub.broadcast()
    return {"ok": True, "permission": perm.model_dump()}


@app.post("/api/warp/send")
async def send_warp_fruit(req: SendWarpFruitRequest) -> dict:
    if req.target_agent_id in ("__SELF__", req.source_agent_id):
        return await self_handoff_agent(
            req.source_agent_id,
            SelfHandoffRequest(custom_handoff=req.content, mode="clear_and_continue"),
        )

    src = hub.agents.get(req.source_agent_id)
    dst = hub.agents.get(req.target_agent_id)
    if not src or not dst:
        raise HTTPException(status_code=404, detail="Agente de origem ou destino não encontrado.")

    fruit = ContextFruit(
        fruit_type=req.fruit_type,
        title=req.title,
        source_agent_id=src.id,
        source_agent_name=src.name,
        source_repo=src.repo_name,
        target_agent_id=dst.id,
        target_repo=dst.repo_name,
        content=req.content,
    )
    hub.warp_fruits.insert(0, fruit)

    src.score_xp += 300
    dst.score_xp += 300
    dst.game_state = AgentGameState.WARPING
    dst.pellets_eaten = min(dst.pellets_total, dst.pellets_eaten + 2)
    if req.title not in dst.artifacts:
        dst.artifacts.append(req.title)
    dst.last_thought = f"🌀 [WARP TUNNEL] Recebi '{req.title}' de {src.name} ({src.repo_name})! Aplicando contexto..."
    dst.current_action = f"Consumindo cápsula '{req.title}' vinda de {src.repo_name}"
    dst.terminal_tail.append(f"🍒 [WARP FRUIT +300 PTS] Contexto '{req.title}' injetado de {src.repo_name}!")

    context_prompt = f"[Contexto recebido de {src.repo_name} ({req.title})]: {req.content}"
    superset_sent = False
    if dst.superset_workspace_id and dst.superset_terminal_id:
        superset_sent = hub.superset_bridge.send_to_terminal(
            workspace_id=dst.superset_workspace_id,
            terminal_id=dst.superset_terminal_id,
            text=context_prompt,
        )
    pty_sent = False
    if dst.id in hub.pty_manager.sessions:
        pty_sent = hub.pty_manager.send_command(dst, context_prompt, auto_enter=True)
    if not superset_sent and not pty_sent:
        if dst.provider.lower() == "antigravity" and dst.session_id:
            _send_antigravity_command_async(dst, context_prompt)
        else:
            inject_input_into_live_terminal(dst, context_prompt, auto_enter=True, is_permission_key=False)
    hub.bridge_input_queues.setdefault(dst.id, []).append(context_prompt + "\r")

    await hub.broadcast()
    return {"ok": True, "fruit": fruit.model_dump()}


def _extract_transcript_handoff_details(ag: AgentUnit) -> Tuple[str, str, List[str]]:
    """
    Reads the tail of the agent's real transcript (Antigravity or Claude) to recover:
    - full untruncated last progress/response
    - full untruncated last thought
    - recently edited file paths (even across multiple repositories)
    """
    full_progress = ""
    full_thought = ""
    edited_files: List[str] = []

    try:
        if ag.provider.lower() == "antigravity" and ag.session_id:
            brain_root = Path.home() / ".gemini" / "antigravity" / "brain"
            candidates = [brain_root / ag.session_id]
            if len(ag.session_id) < 36 and brain_root.exists():
                candidates.extend(p for p in brain_root.glob(f"{ag.session_id}*") if p.is_dir())
            for conv_dir in candidates:
                logs_dir = conv_dir / ".system_generated" / "logs"
                t_file = logs_dir / "transcript_full.jsonl"
                if not t_file.exists():
                    t_file = logs_dir / "transcript.jsonl"
                if not t_file.exists():
                    continue
                lines = t_file.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
                for raw in reversed(lines):
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if obj.get("type") == "PLANNER_RESPONSE":
                        content = (obj.get("content") or "").strip()
                        thinking = (obj.get("thinking") or "").strip()
                        if content and not full_progress:
                            full_progress = content if len(content) <= 4000 else (content[:1500] + "\n...\n" + content[-2200:])
                        if thinking and not full_thought:
                            full_thought = thinking if len(thinking) <= 2000 else thinking[-2000:]
                        for tc in obj.get("tool_calls") or []:
                            if not isinstance(tc, dict):
                                continue
                            tname = tc.get("name") or tc.get("tool_name") or ""
                            targs = tc.get("arguments") or tc.get("args") or {}
                            if isinstance(targs, str):
                                try:
                                    targs = json.loads(targs)
                                except Exception:
                                    targs = {}
                            if isinstance(targs, dict) and tname in (
                                "replace_file_content",
                                "multi_replace_file_content",
                                "write_to_file",
                            ):
                                tf = str(targs.get("TargetFile") or "").strip('"')
                                if tf and tf not in edited_files:
                                    edited_files.append(tf)
                break
        elif ag.provider.lower() == "claude" and ag.session_id:
            claude_root = Path.home() / ".claude" / "projects"
            if claude_root.exists():
                matches = list(claude_root.rglob(f"{ag.session_id}*.jsonl"))
                for t_file in matches[:1]:
                    lines = t_file.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
                    for raw in reversed(lines):
                        if not raw.strip():
                            continue
                        try:
                            obj = json.loads(raw)
                        except Exception:
                            continue
                        if obj.get("type") == "assistant":
                            msg = obj.get("message") or {}
                            content_list = msg.get("content")
                            if isinstance(content_list, list):
                                for blk in reversed(content_list):
                                    if not isinstance(blk, dict):
                                        continue
                                    btype = blk.get("type")
                                    if btype == "text" and not full_progress:
                                        txt = str(blk.get("text") or "").strip()
                                        if txt:
                                            full_progress = txt if len(txt) <= 4000 else (txt[:1500] + "\n...\n" + txt[-2200:])
                                    elif btype == "thinking" and not full_thought:
                                        th = str(blk.get("thinking") or "").strip()
                                        if th:
                                            full_thought = th if len(th) <= 2000 else th[-2000:]
                                    elif btype == "tool_use":
                                        tname = str(blk.get("name") or "")
                                        tinp = blk.get("input") or {}
                                        if isinstance(tinp, dict) and tname in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                                            fp = str(tinp.get("file_path") or "").strip()
                                            if fp and fp not in edited_files:
                                                edited_files.append(fp)
    except Exception:
        pass

    return full_progress, full_thought, edited_files[:15]


def _build_self_handoff_prompt(ag: AgentUnit, custom_handoff: Optional[str] = None) -> str:
    """
    Generates a clean, structured Continuity Handoff prompt from the current AgentUnit (`ag`),
    preserving mission title, repo, branch, real git modified files, artifacts, last progress,
    and last useful reasoning while discarding past tool errors.
    """
    repo_dir = _resolve_valid_repo_dir(ag) or ag.repo_path or str(Path.home() / "Projects")
    branch_name = (ag.branch or "main").strip()
    ws_title = (ag.workspace_title or ag.name or ag.repo_name).strip()

    full_progress, full_thought, transcript_edited_files = _extract_transcript_handoff_details(ag)

    git_status_sections: List[str] = []
    checked_repos: Set[str] = set()
    candidate_repos: List[str] = [repo_dir]
    for ef in transcript_edited_files:
        try:
            p = Path(ef)
            for parent in [p.parent, *p.parents]:
                if (parent / ".git").exists():
                    candidate_repos.append(str(parent))
                    break
        except Exception:
            continue

    for rdir in candidate_repos:
        if not rdir or rdir in checked_repos or not Path(rdir).is_dir():
            continue
        checked_repos.add(rdir)
        try:
            cp = subprocess.run(
                ["git", "status", "--short"],
                cwd=rdir,
                capture_output=True,
                text=True,
                timeout=2.5,
            )
            if cp.returncode == 0 and (cp.stdout or "").strip():
                status_lines = [ln.rstrip() for ln in cp.stdout.strip().splitlines() if ln.strip()]
                if len(candidate_repos) > 1 and rdir != repo_dir:
                    git_status_sections.append(f"[{rdir}]\n" + "\n".join(status_lines[:15]))
                else:
                    git_status_sections.append("\n".join(status_lines[:20]))
        except Exception:
            pass

    git_status_txt = "\n".join(git_status_sections).strip()
    if transcript_edited_files:
        extra_files = "\n".join(f"• {fp}" for fp in transcript_edited_files[:10])
        git_status_txt = f"{git_status_txt}\nArquivos tocados recentemente na sessão:\n{extra_files}".strip()

    if not git_status_txt:
        if ag.artifacts:
            git_status_txt = "\n".join(f"M {art}" for art in ag.artifacts[:15])
        else:
            git_status_txt = "(Nenhuma alteração pendente detectada no git status)"

    raw_action = full_progress or (ag.current_action or "").strip()
    for prefix in (
        "Parado / Pronto • ",
        "Parado no prompt (❯ Pronto) • ",
        "Parado (Sessão encerrada) • Última: ",
        "Trabalhando na missão inicial: ",
    ):
        if raw_action.startswith(prefix):
            raw_action = raw_action[len(prefix):].strip()
    if not raw_action or "falha" in raw_action.lower() or "error" in raw_action.lower():
        raw_action = f"Evolução na tarefa '{ws_title}' em {ag.repo_name}"

    raw_thought = full_thought or (ag.last_thought or "").strip()
    if not raw_thought or "error" in raw_thought.lower() or "traceback" in raw_thought.lower():
        raw_thought = f"Prosseguir com a implementação de '{ws_title}' no repositório {ag.repo_name}."

    custom_clean = (custom_handoff or "").strip()
    if custom_clean.startswith("[♻️ HANDOFF DE CONTINUIDADE"):
        return custom_clean

    prompt_lines = [
        "[♻️ HANDOFF DE CONTINUIDADE — CONTEXTO LIMPO]",
        f"Missão: {ws_title}",
        f"Repositório: {repo_dir} (branch: {branch_name})",
        "Arquivos alterados até agora:",
        git_status_txt,
        f"Último progresso: {raw_action}",
        f"Contexto/Raciocínio anterior: {raw_thought}",
    ]
    if ag.artifacts:
        prompt_lines.append(f"Artefatos relevantes: {', '.join(ag.artifacts[:12])}")
    if custom_clean:
        prompt_lines.append(f"Notas adicionais do Handoff:\n{custom_clean}")
    prompt_lines.append("")
    prompt_lines.append(
        "Continue a implementação a partir deste ponto com foco total, verificando os arquivos reais no disco antes de editar."
    )
    return "\n".join(prompt_lines)


def _dispatch_to_cli_agent_terminal(ag: AgentUnit, text_cmd: str) -> Dict[str, Any]:
    """Dispatches a command/prompt to a CLI agent's live terminal (Superset, PTY, or macOS TTY/Window)."""
    superset_sent = False
    if ag.superset_workspace_id and ag.superset_terminal_id:
        superset_sent = hub.superset_bridge.send_to_terminal(
            workspace_id=ag.superset_workspace_id,
            terminal_id=ag.superset_terminal_id,
            text=text_cmd,
        )
    pty_sent = False
    if ag.id in hub.pty_manager.sessions:
        pty_sent = hub.pty_manager.send_command(ag, text_cmd, auto_enter=True)

    hub.bridge_input_queues.setdefault(ag.id, []).append(text_cmd + "\r")

    live_res: Dict[str, Any] = {"injected": bool(superset_sent or pty_sent), "method": "Superset/PTY" if (superset_sent or pty_sent) else "fila local"}
    if not superset_sent and not pty_sent:
        live_res = inject_input_into_live_terminal(
            ag,
            text_cmd,
            auto_enter=True,
            is_permission_key=False,
        )
    return live_res


@app.get("/api/agents/{agent_id}/handoff-preview")
async def handoff_preview_agent(agent_id: str) -> dict:
    """Returns the complete, untruncated Continuity Handoff package built from real transcript and git status."""
    ag = hub.agents.get(agent_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")
    prompt_txt = await asyncio.to_thread(_build_self_handoff_prompt, ag, None)
    return {
        "ok": True,
        "agent_id": ag.id,
        "title": f"Handoff: {ag.workspace_title or ag.name} ({ag.repo_name})",
        "handoff_prompt": prompt_txt,
    }


@app.post("/api/agents/{agent_id}/self-handoff")
async def self_handoff_agent(agent_id: str, req: Optional[SelfHandoffRequest] = None) -> dict:
    """
    Clears or compacts the current conversation and immediately injects a clean Continuity Handoff
    package into the agent itself (or spawns a clean replacement cascade in Antigravity and archives the saturated one).
    """
    ag = hub.agents.get(agent_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")

    mode = ((req.mode if req else None) or "clear_and_continue").strip().lower()
    if mode not in ("clear_and_continue", "compact"):
        mode = "clear_and_continue"
    custom_handoff = req.custom_handoff if req else None

    handoff_prompt = await asyncio.to_thread(_build_self_handoff_prompt, ag, custom_handoff)
    repo_dir = _resolve_valid_repo_dir(ag) or ag.repo_path or str(Path.home() / "Projects")
    ws_title = (ag.workspace_title or ag.name or ag.repo_name).strip()
    now_ts = time.time()

    # CASE 1: Antigravity agent -> Create a clean Cascade with the same title + handoff_prompt and archive old one
    if ag.provider.lower() == "antigravity":
        agy_res = await asyncio.to_thread(
            AntigravityAdapter.create_cascade,
            repo_dir,
            ws_title,
            handoff_prompt,
        )
        if not agy_res.get("ok"):
            raise HTTPException(
                status_code=500,
                detail=agy_res.get("error") or "Falha ao criar conversa limpa de Handoff no Antigravity.",
            )

        cascade_id = agy_res["cascade_id"]
        short_id = agy_res["short_id"]
        new_agent_id = f"agy-{short_id}"
        repo_path_str = agy_res.get("repo_path") or repo_dir
        repo_name = agy_res.get("repo_name") or ag.repo_name
        clean_title = agy_res.get("title") or ws_title

        # Carry over top-level artifacts (walkthrough.md, implementation_plan.md, *.html) to the new brain folder
        try:
            brain_root = Path.home() / ".gemini" / "antigravity" / "brain"
            old_conv_dir = brain_root / (ag.session_id or "")
            if not old_conv_dir.is_dir() and ag.session_id and brain_root.exists():
                matches = [p for p in brain_root.glob(f"{ag.session_id}*") if p.is_dir()]
                if matches:
                    old_conv_dir = matches[0]
            new_conv_dir = brain_root / cascade_id
            if old_conv_dir.is_dir() and cascade_id:
                new_conv_dir.mkdir(parents=True, exist_ok=True)
                for item in old_conv_dir.iterdir():
                    if item.is_file() and not item.name.startswith("."):
                        dst_item = new_conv_dir / item.name
                        if not dst_item.exists():
                            shutil.copy2(item, dst_item)
        except Exception:
            pass

        # Archive the old saturated conversation
        ag.terminal_tail.append(
            f"♻️ [SELF-HANDOFF] Conversa arquivada — continuidade transferida para {new_agent_id} (100% Saúde)."
        )
        hub.archive_agent(ag.id, True)

        hub.custom_titles[new_agent_id] = clean_title
        hub._save_workspace_state()

        tail_lines = [
            f"♻️ [SELF-HANDOFF — CONTEXTO LIMPO] Nova conversa Antigravity ({short_id}) iniciada a partir de {ag.id}",
            f"🛡️ Saúde do Contexto restaurada para 100% | 🧠 Risco de Alucinação zerado (0%)",
            f"❯ {handoff_prompt}",
            "  ⎿  ⚡ Pacote de Handoff enviado via ConnectRPC — agente continuando com contexto limpo!",
        ]

        new_unit = AgentUnit(
            id=new_agent_id,
            name=clean_title,
            workspace_title=clean_title,
            provider="antigravity",
            repo_name=repo_name,
            repo_path=repo_path_str,
            branch=ag.branch or "main",
            game_state=AgentGameState.CHOMPING,
            score_xp=max(650, ag.score_xp + 500),
            pellets_eaten=min(30, max(4, ag.pellets_eaten + 3)),
            pellets_total=30,
            context_health_pct=100,
            hallucination_risk_pct=0,
            health_status="HEALTHY",
            context_tokens_used=1200,
            context_tokens_limit=200000,
            active_elapsed_sec=0,
            is_stalled=False,
            health_signals=[],
            current_action=f"♻️ Continuando após Self-Handoff limpo: {clean_title}",
            last_thought=f"Conversa renovada via Self-Handoff a partir de {ag.id} com contexto 100% limpo.",
            active_tool="thinking",
            artifacts=list(ag.artifacts),
            terminal_tail=tail_lines,
            control_mode="sdk",
            session_id=cascade_id,
            host_app=ag.host_app or "Antigravity",
            last_active_ts=now_ts,
            neon_color=ag.neon_color or "#0078D9",
            sprite_type=ag.sprite_type or "pac_neon",
        )
        hub.agents[new_agent_id] = new_unit
        hub.active_panel_commands[new_agent_id] = now_ts
        hub.self_handoff_overrides[new_agent_id] = now_ts
        hub.recent_panel_lines.setdefault(new_agent_id, []).append((now_ts, f"❯ {handoff_prompt}"))

        fruit = ContextFruit(
            fruit_type="self_handoff_clean",
            title=f"♻️ Self-Handoff: {clean_title}",
            source_agent_id=ag.id,
            source_agent_name=ag.name,
            source_repo=ag.repo_name,
            target_agent_id=new_agent_id,
            target_repo=repo_name,
            content=handoff_prompt,
        )
        hub.warp_fruits.insert(0, fruit)

        await asyncio.to_thread(_focus_antigravity_app, repo_path_str)
        await hub.broadcast()
        return {
            "ok": True,
            "mode": mode,
            "provider": "antigravity",
            "old_agent_id": ag.id,
            "new_agent_id": new_agent_id,
            "agent": new_unit.model_dump(),
            "handoff_prompt": handoff_prompt,
            "fruit": fruit.model_dump(),
            "state": hub.snapshot(),
        }

    # CASE 2: CLI Agent (claude, gemini, codex, aider, cursor, etc.)
    if mode == "compact":
        compact_cmd = (
            f"/compact Preserve apenas o objetivo ({ws_title}), "
            "os arquivos modificados e o próximo passo, descartando erros antigos."
        )
        live_res = await asyncio.to_thread(_dispatch_to_cli_agent_terminal, ag, compact_cmd)
        hub.record_panel_line(ag.id, f"♻️ [SELF-HANDOFF COMPACT] ❯ {compact_cmd}")
        ag.context_health_pct = 95
        ag.hallucination_risk_pct = 5
        ag.health_status = "HEALTHY"
        ag.context_tokens_used = min(ag.context_tokens_used or 12000, 18000)
        ag.active_elapsed_sec = 0
        ag.is_stalled = False
        ag.health_signals = []
        ag.pending_permission = None
        ag.game_state = AgentGameState.CHOMPING
        ag.score_xp += 400
        ag.current_action = f"♻️ Contexto compactado (/compact) — continuando '{ws_title}'"
        ag.last_thought = "Janela de contexto compactada com sucesso, preservando arquivos alterados e objetivo atual."
    else:
        # mode == "clear_and_continue":
        # 1. Send /clear to wipe the CLI agent's context window
        clear_res = await asyncio.to_thread(_dispatch_to_cli_agent_terminal, ag, "/clear")
        hub.record_panel_line(ag.id, "♻️ [SELF-HANDOFF] ❯ /clear (Zerando janela de contexto...)")
        # 2. Wait 1.2s and inject the Clean Handoff prompt into the same terminal
        await asyncio.sleep(1.2)
        live_res = await asyncio.to_thread(_dispatch_to_cli_agent_terminal, ag, handoff_prompt)
        if not live_res.get("injected") and ag.provider.lower() == "claude" and ag.session_id:
            if _run_claude_session_command_async(ag, handoff_prompt):
                live_res = {"injected": True, "method": f"Claude CLI (--resume {ag.session_id[:8]})"}
        hub.record_panel_line(ag.id, f"♻️ [HANDOFF LIMPO INJETADO] ❯ {handoff_prompt}")

        # 3. Reset context & hallucination telemetry immediately on AgentUnit
        ag.context_health_pct = 100
        ag.hallucination_risk_pct = 0
        ag.health_status = "HEALTHY"
        ag.context_tokens_used = 1500
        ag.active_elapsed_sec = 0
        ag.is_stalled = False
        ag.health_signals = []
        ag.pending_permission = None
        ag.game_state = AgentGameState.CHOMPING
        ag.score_xp += 500
        ag.pellets_eaten = min(ag.pellets_total, ag.pellets_eaten + 3)
        ag.current_action = f"♻️ Continuando com contexto limpo (100%): {ws_title}"
        ag.last_thought = "Contexto limpo via /clear + pacote de Handoff de Continuidade injetado no mesmo agente."

    ag.last_active_ts = now_ts
    ag.updated_at = utc_now_iso()
    hub.self_handoff_overrides[ag.id] = now_ts
    hub.active_panel_commands[ag.id] = now_ts

    fruit = ContextFruit(
        fruit_type="self_handoff_clean",
        title=f"♻️ Self-Handoff ({mode}): {ws_title}",
        source_agent_id=ag.id,
        source_agent_name=ag.name,
        source_repo=ag.repo_name,
        target_agent_id=ag.id,
        target_repo=ag.repo_name,
        content=handoff_prompt,
    )
    hub.warp_fruits.insert(0, fruit)

    await hub.broadcast()
    return {
        "ok": True,
        "mode": mode,
        "provider": ag.provider,
        "old_agent_id": ag.id,
        "new_agent_id": ag.id,
        "live_terminal": live_res,
        "agent": ag.model_dump(),
        "handoff_prompt": handoff_prompt,
        "fruit": fruit.model_dump(),
        "state": hub.snapshot(),
    }


def _find_pid_on_tty(short_tty: str) -> Optional[int]:
    """Finds the newest foreground process PID attached to a given tty (e.g. ttys012)."""
    clean_tty = (short_tty or "").replace("/dev/", "").strip()
    if not clean_tty:
        return None
    try:
        res = subprocess.run(
            ["ps", "-t", clean_tty, "-o", "pid=,comm="],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        pids: List[int] = []
        preferred_pids: List[int] = []
        for raw_line in (res.stdout or "").splitlines():
            parts = raw_line.strip().split(None, 1)
            if not parts:
                continue
            try:
                pid_val = int(parts[0])
            except ValueError:
                continue
            comm = (parts[1] if len(parts) > 1 else "").lower()
            pids.append(pid_val)
            if any(k in comm for k in ("claude", "gemini", "codex", "aider", "cursor", "node", "python", "agy")):
                preferred_pids.append(pid_val)
        if preferred_pids:
            return preferred_pids[-1]
        if pids:
            return pids[-1]
    except Exception:
        pass
    return None


def _build_physical_cli_invocation(
    provider_id: str,
    raw_cmd: str,
    session_uuid: str,
    initial_prompt: Optional[str],
) -> Tuple[str, str, str, Optional[str]]:
    """
    Constructs the physical CLI command string for launching an AI agent in a Terminal or PTY.
    Returns (agent_id, base_bin_name, full_cli_command, resolved_bin_path).
    """
    base_cmd = (raw_cmd or provider_id).strip()
    cmd_parts = shlex.split(base_cmd) if base_cmd else [provider_id]
    base_bin_name = cmd_parts[0] if cmd_parts else provider_id
    extra_args = cmd_parts[1:] if len(cmd_parts) > 1 else []
    resolved_bin = _resolve_cli_binary(base_bin_name)
    exec_bin = resolved_bin or base_bin_name
    prompt_clean = (initial_prompt or "").strip()

    if provider_id == "claude":
        agent_id = f"claude-{session_uuid[:8]}"
        args = [shlex.quote(exec_bin)] + [shlex.quote(a) for a in extra_args]
        if "--session-id" not in extra_args and "-r" not in extra_args and "--resume" not in extra_args:
            args.extend(["--session-id", session_uuid])
        if prompt_clean:
            args.append(shlex.quote(prompt_clean))
        return agent_id, base_bin_name, " ".join(args), resolved_bin

    agent_id = f"{provider_id}-{session_uuid[:8]}"
    args = [shlex.quote(exec_bin)] + [shlex.quote(a) for a in extra_args]
    if prompt_clean:
        if provider_id == "aider":
            args.extend(["--message", shlex.quote(prompt_clean)])
        else:
            args.append(shlex.quote(prompt_clean))
    return agent_id, base_bin_name, " ".join(args), resolved_bin


def _spawn_in_macos_terminal_app(
    terminal_host: str,
    repo_path_str: str,
    cli_cmd: str,
    tab_title: str,
    base_bin_name: str,
) -> Dict[str, Any]:
    """
    Opens a real window/tab in Terminal.app or iTerm2 at repo_path_str, sets its title to tab_title,
    executes cli_cmd (with augmented PATH), and returns {'ok': True, 'tty': short_tty, 'pid': pid}.
    """
    if sys.platform != "darwin":
        return {"ok": False, "error": "Disponível apenas no macOS."}

    safe_title = (tab_title or repo_path_str).replace('"', "'").replace("\\", "/")
    augmented_path = _get_augmented_search_path()
    # Build shell command that sets terminal tab title via ANSI OSC 0, exports PATH, cd's into repo, and runs CLI
    shell_line = (
        f"export PATH={shlex.quote(augmented_path)}:$PATH; "
        f"cd {shlex.quote(repo_path_str)} && "
        f"printf '\\033]0;%s\\007' {shlex.quote(safe_title)}; "
        f"if command -v {shlex.quote(base_bin_name)} >/dev/null 2>&1; then {cli_cmd}; "
        f"else echo '⚠️ [PAC-NEON] Binário CLI \"{base_bin_name}\" não encontrado no PATH deste Mac.'; "
        f"echo '📂 Diretório pronto em: {repo_path_str}'; fi"
    )
    apple_cmd = shell_line.replace("\\", "\\\\").replace('"', '\\"')
    apple_title = safe_title.replace("\\", "\\\\").replace('"', '\\"')

    if terminal_host.lower() in ("iterm", "iterm2"):
        iterm_script = f'''
        tell application "iTerm"
            activate
            if (count of windows) = 0 then
                set newWin to (create window with default profile)
                set curSession to current session of newWin
            else
                tell current window
                    set newTab to (create tab with default profile)
                    set curSession to current session of newTab
                end tell
            end if
            tell curSession
                set name to "{apple_title}"
                write text "{apple_cmd}"
                return (tty of curSession)
            end tell
        end tell
        '''
        try:
            res = subprocess.run(["osascript", "-e", iterm_script], capture_output=True, text=True, timeout=6.0)
            raw_tty = (res.stdout or "").strip()
            short_tty = raw_tty.replace("/dev/", "").strip()
            if res.returncode == 0 and short_tty:
                time.sleep(0.35)
                pid = _find_pid_on_tty(short_tty)
                return {"ok": True, "tty": short_tty, "pid": pid, "host_app": "iTerm"}
            return {"ok": False, "error": (res.stderr or "Falha ao abrir aba no iTerm2.").strip()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # Default: macOS Terminal.app
    term_script = f'''
    tell application "Terminal"
        activate
        set newTab to do script "{apple_cmd}"
        try
            set custom title of newTab to "{apple_title}"
            set title displays custom title of newTab to true
        end try
        return (tty of newTab)
    end tell
    '''
    try:
        res = subprocess.run(["osascript", "-e", term_script], capture_output=True, text=True, timeout=6.0)
        raw_tty = (res.stdout or "").strip()
        short_tty = raw_tty.replace("/dev/", "").strip()
        if res.returncode == 0 and short_tty:
            time.sleep(0.35)
            pid = _find_pid_on_tty(short_tty)
            return {"ok": True, "tty": short_tty, "pid": pid, "host_app": "Terminal"}
        return {"ok": False, "error": (res.stderr or "Falha ao abrir aba no Terminal.app.").strip()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/agents/spawn")
async def spawn_new_agent(req: SpawnAgentRequest) -> dict:
    raw_prov = (req.provider or "antigravity").lower().strip()
    if "antigravity" in raw_prov or raw_prov == "agy":
        provider_id = "antigravity"
    elif "claude" in raw_prov:
        provider_id = "claude"
    elif "gemini" in raw_prov:
        provider_id = "gemini"
    elif "codex" in raw_prov:
        provider_id = "codex"
    elif "cursor" in raw_prov:
        provider_id = "cursor"
    elif "aider" in raw_prov:
        provider_id = "aider"
    else:
        provider_id = raw_prov

    adapter = AdapterRegistry.get(provider_id)

    # If it's a brand new future provider not yet registered, register it dynamically!
    if not adapter:
        new_meta = ProviderMetadata(
            id=provider_id,
            display_name=req.provider.capitalize(),
            arcade_title=f"NEON-{req.provider.upper()} // PLUGIN",
            neon_color="#FF2E93",
            secondary_color="#00E5FF",
            sprite_type="ghost_pinky",
            cli_command=provider_id,
        )
        adapter = GenericPluginAdapter(new_meta)
        AdapterRegistry.register(adapter)

    meta = adapter.get_metadata()

    # Resolve repository path cleanly
    raw_repo = (req.repo_path or "").strip()
    resolved_repo = Path(raw_repo).expanduser() if raw_repo else (Path.home() / "Projects")
    if not resolved_repo.is_absolute() or not resolved_repo.exists():
        cand = Path.home() / "Projects" / (resolved_repo.name or raw_repo.split("/")[-1])
        if cand.exists():
            resolved_repo = cand
    repo_path_str = str(resolved_repo)
    repo_name = resolved_repo.name or req.repo_path or "workspace"

    # Determine target host environment (IDE, Terminal, or Superset)
    requested_host = (req.target_host or "").strip()
    if req.use_superset or requested_host.lower() == "superset":
        effective_host = "Superset"
    elif requested_host and requested_host.lower() != "auto":
        effective_host = requested_host
    else:
        if provider_id == "antigravity":
            effective_host = "Antigravity"
        elif provider_id == "cursor" and _find_installed_mac_app(["Cursor"]):
            effective_host = "Cursor"
        elif _find_installed_mac_app(["iTerm"]):
            effective_host = "iTerm"
        else:
            effective_host = "Terminal"

    # CASE A: Provider is Antigravity and target host is Antigravity IDE (or another IDE alongside Antigravity cascade)
    if provider_id == "antigravity" and effective_host not in _KNOWN_TERMINAL_APPS and effective_host != "Superset":
        agy_res = await asyncio.to_thread(
            AntigravityAdapter.create_cascade,
            repo_path_str,
            req.name,
            req.initial_prompt,
        )
        if not agy_res.get("ok"):
            return {
                "ok": False,
                "error": agy_res.get("error") or "Falha ao criar conversa no Antigravity.",
            }

        cascade_id = agy_res["cascade_id"]
        short_id = agy_res["short_id"]
        agent_id = f"agy-{short_id}"
        repo_path_str = agy_res["repo_path"]
        repo_name = agy_res["repo_name"]
        clean_title = agy_res["title"]
        has_prompt = bool((req.initial_prompt or "").strip())

        if (req.name or "").strip():
            hub.custom_titles[agent_id] = req.name.strip()
            hub._save_workspace_state()

        now_ts = time.time()
        tail_lines = [
            f"🕹️ [INSERT COIN] Nova conversa criada no Antigravity ({cascade_id[:8]}) em {repo_path_str}",
            f"⚡ ConnectRPC: StartCascade + UpdateConversationAnnotations ({clean_title})",
        ]
        if has_prompt:
            prompt_clean = (req.initial_prompt or "").strip()
            tail_lines.append(f"❯ {prompt_clean}")
            tail_lines.append("  ⎿  ⚡ Missão enviada via SendUserCascadeMessage — agente trabalhando no Antigravity!")
            hub.active_panel_commands[agent_id] = now_ts
            hub.recent_panel_lines.setdefault(agent_id, []).append((now_ts, f"❯ {prompt_clean}"))

        chosen_ide = effective_host if effective_host in _KNOWN_IDE_APPS else "Antigravity"
        if chosen_ide == "Antigravity":
            await asyncio.to_thread(_focus_antigravity_app, repo_path_str)
        else:
            await asyncio.to_thread(_open_and_activate_mac_app, chosen_ide, repo_path_str)

        unit = AgentUnit(
            id=agent_id,
            name=clean_title,
            workspace_title=clean_title,
            provider="antigravity",
            repo_name=repo_name,
            repo_path=repo_path_str,
            branch="main",
            game_state=AgentGameState.CHOMPING if has_prompt else AgentGameState.IDLE,
            score_xp=650 if has_prompt else 150,
            pellets_eaten=4 if has_prompt else 1,
            pellets_total=30,
            context_health_pct=100,
            current_action=(
                f"Trabalhando na missão inicial: {(req.initial_prompt or '').strip()[:100]}"
                if has_prompt
                else f"Nova conversa Antigravity pronta em {repo_name}"
            ),
            last_thought=f"Conversa Antigravity #{short_id} criada em {repo_path_str} ({chosen_ide}).",
            active_tool="thinking" if has_prompt else None,
            terminal_tail=tail_lines,
            control_mode="sdk",
            session_id=cascade_id,
            host_app=chosen_ide,
            last_active_ts=now_ts,
            neon_color=meta.neon_color,
            sprite_type=meta.sprite_type,
        )
        hub.agents[unit.id] = unit
        await hub.broadcast()
        return {"ok": True, "agent": unit.model_dump(), "cascade_id": cascade_id, "target_host": chosen_ide}

    # Generate UUID and CLI command for non-Antigravity-SDK launches
    session_uuid = str(uuid.uuid4())
    short_num = len(hub.agents) + 1
    raw_cli = req.command_override or meta.cli_command or provider_id
    agent_id, base_bin_name, full_cli_cmd, resolved_bin_path = _build_physical_cli_invocation(
        provider_id=provider_id,
        raw_cmd=raw_cli,
        session_uuid=session_uuid,
        initial_prompt=req.initial_prompt,
    )
    clean_title = (req.name or "").strip() or f"{meta.display_name} — {repo_name}"
    if (req.name or "").strip():
        hub.custom_titles[agent_id] = clean_title
        hub._save_workspace_state()

    now_ts = time.time()
    has_prompt = bool((req.initial_prompt or "").strip())

    # CASE B: Target host is Superset Worktree
    if effective_host == "Superset":
        ws_id = req.superset_workspace_id or f"ws_{repo_name[:10]}_{short_num:02d}"
        created = await asyncio.to_thread(
            hub.superset_bridge.spawn_agent_in_workspace,
            ws_id,
            provider_id,
            req.initial_prompt or f"Work on {repo_name}",
        )
        term_id = str(created["sessionId"]) if (created and created.get("sessionId")) else f"term_{provider_id}_{short_num:02d}"
        deep_link = f"superset://v2-workspace/{ws_id}?terminalId={term_id}"
        await asyncio.to_thread(hub.superset_bridge.open_workspace_in_superset_ui, ws_id, term_id)

        unit = AgentUnit(
            id=agent_id,
            name=clean_title,
            workspace_title=clean_title,
            provider=meta.id,
            repo_name=repo_name,
            repo_path=repo_path_str,
            branch=f"worktree/{agent_id}",
            game_state=AgentGameState.CHOMPING,
            score_xp=500,
            pellets_eaten=3,
            pellets_total=24,
            context_health_pct=100,
            current_action=req.initial_prompt or f"Iniciando {meta.display_name} na worktree Superset {ws_id}",
            last_thought=f"Sessão criada via `superset agents create --workspace {ws_id} --agent {provider_id}`.",
            terminal_tail=[
                f"🕹️ [INSERT COIN] Invocando {meta.display_name} em {repo_path_str}...",
                f"⚡ Engine: Superset Worktree ({ws_id})",
                f"⚡ Missão inicial: {req.initial_prompt or 'Aguardando instruções'}",
            ],
            control_mode="superset",
            session_id=session_uuid,
            host_app="Superset",
            superset_workspace_id=ws_id,
            superset_terminal_id=term_id,
            superset_deep_link=deep_link,
            last_active_ts=now_ts,
            neon_color=meta.neon_color,
            sprite_type=meta.sprite_type,
        )
        hub.agents[unit.id] = unit
        await hub.broadcast()
        return {"ok": True, "agent": unit.model_dump(), "target_host": "Superset"}

    # CASE C: Target host is a Terminal Emulator (Terminal.app, iTerm2, Warp, Ghostty, Alacritty, WezTerm)
    if effective_host in _KNOWN_TERMINAL_APPS:
        tail_lines = [
            f"🕹️ [INSERT COIN] Abrindo {meta.display_name} fisicamente em {effective_host} ({repo_path_str})...",
            f"⚡ Comando CLI: {full_cli_cmd}",
        ]
        spawned_tty: Optional[str] = None
        spawned_pid: Optional[int] = None
        control_mode = "hook"

        if effective_host in ("Terminal", "iTerm"):
            term_res = await asyncio.to_thread(
                _spawn_in_macos_terminal_app,
                effective_host,
                repo_path_str,
                full_cli_cmd,
                clean_title,
                base_bin_name,
            )
            if term_res.get("ok"):
                spawned_tty = term_res.get("tty")
                spawned_pid = term_res.get("pid")
                tail_lines.append(
                    f"✅ Aba física aberta em {effective_host} (TTY: {spawned_tty or 'ativo'}"
                    + (f", PID: {spawned_pid}" if spawned_pid else "")
                    + ")"
                )
            else:
                tail_lines.append(f"⚠️ Aviso ao abrir {effective_host}: {term_res.get('error')}")
        else:
            # Warp, Ghostty, Alacritty, WezTerm: open the terminal app and run managed PTY session
            await asyncio.to_thread(_open_and_activate_mac_app, effective_host, None)
            control_mode = "pty"

        if not resolved_bin_path:
            tail_lines.append(
                f"ℹ️ Binário '{base_bin_name}' não detectado em PATH; aba do terminal foi aberta pronta em {repo_path_str}."
            )

        unit = AgentUnit(
            id=agent_id,
            name=clean_title,
            workspace_title=clean_title,
            provider=meta.id,
            repo_name=repo_name,
            repo_path=repo_path_str,
            branch="main",
            game_state=AgentGameState.CHOMPING if has_prompt else AgentGameState.IDLE,
            score_xp=550 if has_prompt else 200,
            pellets_eaten=3 if has_prompt else 1,
            pellets_total=24,
            context_health_pct=100,
            current_action=(
                f"Executando em {effective_host}: {(req.initial_prompt or '').strip()[:100]}"
                if has_prompt
                else f"Sessão {meta.display_name} aberta em {effective_host} ({repo_name})"
            ),
            last_thought=f"Agente físico iniciado em {effective_host} no repositório {repo_name}.",
            active_tool="Bash" if has_prompt else None,
            terminal_tail=tail_lines,
            control_mode=control_mode,
            session_id=session_uuid,
            pid=spawned_pid,
            tty=spawned_tty,
            host_app=effective_host,
            last_active_ts=now_ts,
            neon_color=meta.neon_color,
            sprite_type=meta.sprite_type,
        )
        hub.agents[unit.id] = unit
        if has_prompt:
            hub.active_panel_commands[unit.id] = now_ts

        if control_mode == "pty" and resolved_bin_path:
            try:
                await hub.pty_manager.spawn_pty_agent(
                    unit=unit,
                    command_str=full_cli_cmd,
                    on_state_change=lambda _: asyncio.create_task(hub.broadcast()),
                )
            except Exception as exc:
                unit.terminal_tail.append(f"ℹ️ PTY fallback ({exc})")

        await hub.broadcast()
        return {"ok": True, "agent": unit.model_dump(), "target_host": effective_host}

    # CASE D: Target host is an IDE (IntelliJ IDEA, Android Studio, Visual Studio Code, Cursor, Windsurf, Xcode, etc.)
    await asyncio.to_thread(_open_and_activate_mac_app, effective_host, repo_path_str)
    tail_lines = [
        f"🕹️ [INSERT COIN] Abrindo projeto {repo_name} na IDE {effective_host} ({repo_path_str})...",
        f"⚡ Comando CLI vinculado: {full_cli_cmd}",
    ]

    unit = AgentUnit(
        id=agent_id,
        name=clean_title,
        workspace_title=clean_title,
        provider=meta.id,
        repo_name=repo_name,
        repo_path=repo_path_str,
        branch="main",
        game_state=AgentGameState.CHOMPING if has_prompt else AgentGameState.IDLE,
        score_xp=550 if has_prompt else 200,
        pellets_eaten=3 if has_prompt else 1,
        pellets_total=24,
        context_health_pct=100,
        current_action=(
            f"Trabalhando em {effective_host}: {(req.initial_prompt or '').strip()[:100]}"
            if has_prompt
            else f"Projeto aberto em {effective_host} com {meta.display_name} ({repo_name})"
        ),
        last_thought=f"Projeto {repo_name} aberto em {effective_host} com sessão {meta.display_name}.",
        active_tool="Bash" if has_prompt else None,
        terminal_tail=tail_lines,
        control_mode="pty" if resolved_bin_path else "hook",
        session_id=session_uuid,
        host_app=effective_host,
        last_active_ts=now_ts,
        neon_color=meta.neon_color,
        sprite_type=meta.sprite_type,
    )
    hub.agents[unit.id] = unit
    if has_prompt:
        hub.active_panel_commands[unit.id] = now_ts

    if resolved_bin_path:
        try:
            await hub.pty_manager.spawn_pty_agent(
                unit=unit,
                command_str=full_cli_cmd,
                on_state_change=lambda _: asyncio.create_task(hub.broadcast()),
            )
            unit.terminal_tail.append(
                f"✅ Processo real {base_bin_name} iniciado em {repo_path_str} (PID: {unit.pid}, TTY: {unit.tty})"
            )
        except Exception as exc:
            unit.terminal_tail.append(f"ℹ️ Aviso ao iniciar PTY local ({exc})")
    else:
        unit.terminal_tail.append(
            f"ℹ️ IDE {effective_host} aberta em {repo_path_str}. (Binário '{base_bin_name}' não encontrado no PATH global)."
        )

    await hub.broadcast()
    return {"ok": True, "agent": unit.model_dump(), "target_host": effective_host}


@app.post("/api/hooks/request-permission")
async def hook_request_permission(payload: HookPermissionPayload) -> dict:
    """Long-polling endpoint called by pac_neon_hook.py from any external terminal."""
    agent_id = payload.agent_id or f"{payload.provider}-hook"
    repo_name = Path(payload.repo_path).name or "external-repo"
    adapter = AdapterRegistry.get(payload.provider)
    color = adapter.get_metadata().neon_color if adapter else "#FF7A00"
    sprite = adapter.get_metadata().sprite_type if adapter else "ghost_clyde"

    if agent_id not in hub.agents:
        hub.agents[agent_id] = AgentUnit(
            id=agent_id,
            name=payload.agent_name or f"{payload.provider.upper()} ({repo_name})",
            provider=payload.provider,
            repo_name=repo_name,
            repo_path=payload.repo_path,
            neon_color=color,
            sprite_type=sprite,
        )

    ag = hub.agents[agent_id]
    perm = PermissionRequestItem(
        agent_id=ag.id,
        agent_name=ag.name,
        provider=ag.provider,
        repo_name=repo_name,
        tool_name=payload.tool_name,
        command_preview=payload.command_preview,
        reason=payload.reason or "Aprovação remota interceptada via Hook.",
        channel="hook",
    )
    ag.pending_permission = perm
    ag.game_state = AgentGameState.POWER_PELLET_WAIT
    ag.current_action = f"AGUARDANDO PERMISSÃO (HOOK): {payload.command_preview}"
    ag.terminal_tail.append(f"🚨 [HOOK INTERCEPT] {payload.tool_name}: {payload.command_preview}")
    ag.updated_at = utc_now_iso()

    ev = threading.Event()
    hub.hook_events[perm.id] = ev
    await hub.broadcast()

    # Show macOS Notification when an agent blocks on a hook (without stealing focus via webbrowser.open!)
    trigger_cockpit_popup(
        reason_title=f"PAC-NEON // {ag.name} pede Permissão",
        reason_body=f"{payload.tool_name}: {payload.command_preview}",
        open_browser=False,
    )

    signaled = await asyncio.to_thread(ev.wait, float(payload.timeout_seconds))
    hub.hook_events.pop(perm.id, None)
    if signaled:
        return hub.hook_results.pop(perm.id, {"decision": "allow"})
    return {"decision": "deny", "message": "Timeout aguardando resposta no Pac-Neon."}


@app.post("/api/bridge/heartbeat")
async def bridge_heartbeat(data: dict) -> dict:
    agent_id = data.get("agent_id", "wrap-agent")
    provider = data.get("provider", "claude")
    repo_path = data.get("repo_path", ".")
    repo_name = Path(repo_path).name or "repo"
    adapter = AdapterRegistry.get(provider)

    if agent_id not in hub.agents:
        hub.agents[agent_id] = AgentUnit(
            id=agent_id,
            name=data.get("agent_name") or f"{provider.upper()} ({repo_name})",
            provider=provider,
            repo_name=repo_name,
            repo_path=repo_path,
            neon_color=adapter.get_metadata().neon_color if adapter else "#00E5FF",
            sprite_type=adapter.get_metadata().sprite_type if adapter else "pac_neon",
        )
    ag = hub.agents[agent_id]
    recent = data.get("recent_output", "").strip()
    if recent:
        lines = [ln.strip() for ln in recent.splitlines() if ln.strip()]
        if lines:
            ag.current_action = lines[-1][:140]
            ag.terminal_tail = lines[-15:]
    pending = hub.bridge_input_queues.pop(agent_id, [])
    return {"ok": True, "pending_inputs": pending}


@app.post("/api/bridge/permission-alert")
async def bridge_permission_alert(data: dict) -> dict:
    agent_id = data.get("agent_id", "wrap-agent")
    ag = hub.agents.get(agent_id)
    if ag and ag.pending_permission is None:
        cmd_prev = data.get("command_preview", "Confirmar execução? [y/n]")
        ag.pending_permission = PermissionRequestItem(
            agent_id=ag.id,
            agent_name=ag.name,
            provider=ag.provider,
            repo_name=ag.repo_name,
            tool_name="Terminal Prompt (y/n)",
            command_preview=cmd_prev,
            channel="pty",
        )
        ag.game_state = AgentGameState.POWER_PELLET_WAIT
        trigger_cockpit_popup(
            reason_title=f"PAC-NEON // {ag.name} bloqueado [Y/N]",
            reason_body=str(cmd_prev),
            open_browser=False,
        )
        await hub.broadcast()
    return {"ok": True}


@app.post("/api/system/accessibility-prompt")
async def prompt_macos_accessibility() -> dict:
    """Prompts macOS Accessibility dialog and opens Privacy & Security -> Accessibility settings."""
    trusted = _is_macos_accessibility_trusted(prompt_user=True)
    if not trusted and sys.platform == "darwin":
        try:
            subprocess.Popen(
                ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    return {"ok": True, "accessibility_trusted": trusted}


@app.post("/api/hooks/agent-event")
async def hook_agent_event(data: dict) -> dict:
    """
    Receives real-time lifecycle events pushed from ~/.pac-neon/hooks/notify.sh
    (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest, Notification, Stop, SessionEnd).
    For PermissionRequest, blocks until resolved in the Pac-Neon panel (or terminal) and returns Claude Code's
    native `hookSpecificOutput` JSON decision so approval happens directly inside the live session without keystrokes.
    """
    session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
    event_name = str(data.get("hook_event_name") or data.get("event") or "").strip()
    cwd = str(data.get("cwd") or "").strip()
    tool_name = str(data.get("tool_name") or data.get("tool") or "").strip()
    notif_type = str(data.get("notification_type") or "").strip()
    message = str(data.get("message") or "").strip()

    target_ag: Optional[AgentUnit] = None
    if session_id:
        candidate_id = f"claude-{session_id[:8]}"
        target_ag = hub.agents.get(candidate_id)
        if not target_ag:
            for ag in hub.agents.values():
                if ag.session_id == session_id or ag.id.endswith(session_id[:8]):
                    target_ag = ag
                    break
    if not target_ag and cwd:
        for ag in hub.agents.values():
            if ag.repo_path == cwd and not ag.archived:
                target_ag = ag
                break

    if not target_ag:
        return {"ok": True, "matched": False, "event": event_name}

    now = time.time()
    target_ag.last_active_ts = now

    def _release_stale_hook_if_terminal_resolved() -> None:
        stale_perm_id = hub.agent_hook_events.pop(target_ag.id, None)
        if stale_perm_id:
            ev_stale = hub.hook_events.pop(stale_perm_id, None)
            if ev_stale:
                hub.hook_results[stale_perm_id] = {"resolved_in_terminal": True}
                ev_stale.set()

    if event_name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionStart"):
        if event_name in ("UserPromptSubmit", "PostToolUse"):
            _release_stale_hook_if_terminal_resolved()
        target_ag.game_state = AgentGameState.CHOMPING
        target_ag.pending_permission = None
        if event_name == "UserPromptSubmit":
            prompt_preview = str(data.get("prompt") or "")[:90]
            target_ag.current_action = f"Processando comando: {prompt_preview}" if prompt_preview else "Processando novo comando..."
        elif tool_name:
            target_ag.active_tool = tool_name
            target_ag.current_action = f"Executando {tool_name}..."
        else:
            target_ag.current_action = "Trabalhando ativamente..."
        hub.hook_state_overrides[target_ag.id] = {
            "ts": now,
            "game_state": AgentGameState.CHOMPING,
            "current_action": target_ag.current_action,
            "pending_permission": None,
        }
    elif event_name == "PermissionRequest" or (
        event_name == "Notification"
        and (
            notif_type in ("permission_prompt", "elicitation_dialog")
            or "permission" in message.lower()
            or "waiting for your input" in message.lower()
        )
    ):
        tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        cmd_preview = (
            tool_input.get("command")
            or tool_input.get("file_path")
            or tool_input.get("pattern")
            or message
            or f"Aprovar {tool_name or 'ação'} no terminal"
        )
        desc = str(tool_input.get("description") or "").strip()
        fpath = str(tool_input.get("file_path") or "").strip()
        diff_old = str(tool_input.get("old_string") or "").strip()
        diff_new = str(tool_input.get("new_string") or tool_input.get("content") or tool_input.get("plan") or "").strip()
        opts_list: List[str] = []
        q_list = tool_input.get("questions") if isinstance(tool_input.get("questions"), list) else []
        if q_list:
            q_texts = []
            for qi in q_list:
                if isinstance(qi, dict):
                    if qi.get("question"):
                        q_texts.append(str(qi["question"]))
                    if isinstance(qi.get("options"), list):
                        for opt_item in qi["options"]:
                            if isinstance(opt_item, dict) and opt_item.get("label"):
                                opts_list.append(str(opt_item["label"]))
                            elif isinstance(opt_item, str):
                                opts_list.append(opt_item)
            if q_texts:
                cmd_preview = "\n".join(q_texts)

        reason_parts = []
        if desc:
            reason_parts.append(f"Descrição: {desc}")
        if fpath and fpath != str(cmd_preview):
            reason_parts.append(f"Arquivo: {fpath}")
        if cwd or target_ag.repo_path:
            reason_parts.append(f"Diretório: {cwd or target_ag.repo_path}")
        if message and message != str(cmd_preview):
            reason_parts.append(f"Mensagem: {message}")
        reason_str = (
            " • ".join(reason_parts)
            if reason_parts
            else "Interceptado em tempo real via Hook nativo — aprove ou responda direto pelo painel."
        )
        filtered_params = {
            k: v
            for k, v in tool_input.items()
            if k not in ("old_string", "new_string", "content") and v not in (None, "", [], {})
        }
        perm_id = f"perm-hook-{target_ag.id}"
        perm = PermissionRequestItem(
            id=perm_id,
            agent_id=target_ag.id,
            agent_name=target_ag.workspace_title or target_ag.name,
            provider=target_ag.provider,
            repo_name=target_ag.repo_name,
            tool_name=tool_name or "Permission Prompt",
            command_preview=str(cmd_preview),
            reason=reason_str,
            full_details={
                "tool_action": desc,
                "description": desc,
                "cwd": cwd or target_ag.repo_path,
                "target_path": fpath,
                "diff_old": diff_old[:1500] if diff_old else "",
                "diff_new": diff_new[:1500] if diff_new else "",
                "agent_context": target_ag.last_thought or "",
                "parameters": filtered_params,
            },
            options=opts_list,
            channel="native_hook",
        )
        suggestions = data.get("permission_suggestions")
        if isinstance(suggestions, list) and suggestions:
            hub.hook_suggestions[perm_id] = suggestions

        target_ag.game_state = AgentGameState.POWER_PELLET_WAIT
        target_ag.pending_permission = perm
        target_ag.current_action = f"Aguardando sua permissão: {tool_name or 'Terminal'} — {cmd_preview}"
        hub.hook_state_overrides[target_ag.id] = {
            "ts": now,
            "game_state": AgentGameState.POWER_PELLET_WAIT,
            "current_action": target_ag.current_action,
            "pending_permission": perm,
        }
        trigger_cockpit_popup(
            reason_title=f"⚡ PAC-NEON // {target_ag.workspace_title}",
            reason_body=f"{perm.tool_name}: {perm.command_preview}",
            open_browser=False,
        )

        # If this is a synchronous PermissionRequest hook from Claude Code, wait for the user's click in Pac-Neon
        # and return `hookSpecificOutput` JSON so Claude Code executes the decision natively!
        if event_name == "PermissionRequest":
            _release_stale_hook_if_terminal_resolved()
            ev = threading.Event()
            hub.hook_events[perm_id] = ev
            hub.agent_hook_events[target_ag.id] = perm_id
            await hub.broadcast()

            signaled = await asyncio.to_thread(ev.wait, 580.0)
            hub.hook_events.pop(perm_id, None)
            if hub.agent_hook_events.get(target_ag.id) == perm_id:
                hub.agent_hook_events.pop(target_ag.id, None)

            if not signaled:
                return {}
            res = hub.hook_results.pop(perm_id, None) or {"decision": "allow", "action": "allow"}
            if res.get("resolved_in_terminal"):
                return {}

            if res.get("decision") == "deny":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": {
                            "behavior": "deny",
                            "message": str(
                                res.get("custom_text")
                                or "Permissão negada pelo usuário no painel Pac-Neon."
                            ),
                            "interrupt": False,
                        },
                    }
                }

            decision_obj: Dict[str, Any] = {"behavior": "allow"}
            if res.get("action") == "always":
                suggs = hub.hook_suggestions.pop(perm_id, None)
                if suggs:
                    decision_obj["updatedPermissions"] = suggs
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision_obj,
                }
            }

    elif event_name in ("Stop", "SubagentStop", "SessionEnd") or (
        event_name == "Notification" and notif_type == "idle_prompt"
    ):
        _release_stale_hook_if_terminal_resolved()
        target_ag.game_state = AgentGameState.IDLE
        target_ag.pending_permission = None
        target_ag.current_action = "Parado — turno finalizado (aguardando próximo comando)"
        hub.hook_state_overrides[target_ag.id] = {
            "ts": now,
            "game_state": AgentGameState.IDLE,
            "current_action": target_ag.current_action,
            "pending_permission": None,
        }

    await hub.broadcast()
    return {"ok": True, "matched": True, "agent_id": target_ag.id, "state": target_ag.game_state.value}


_SLASH_COMMANDS_CACHE: Dict[str, Any] = {"ts": 0.0, "items": []}


def _parse_skill_md(md_path: Path, fallback_name: str, source_badge: str, category: str = "skill") -> Optional[Dict[str, Any]]:
    """Extract name and description from a SKILL.md or command .md file."""
    try:
        raw = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    name = fallback_name.strip().lstrip("/")
    description = ""

    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":
        in_desc_multiline = False
        desc_parts: List[str] = []
        for ln in lines[1:60]:
            s = ln.strip()
            if s == "---":
                break
            if ln.startswith("name:"):
                val = ln.split(":", 1)[1].strip().strip("'\"")
                if val:
                    name = val.lstrip("/").replace("_", "-") if category == "skill" else val.lstrip("/")
                in_desc_multiline = False
            elif ln.startswith("description:"):
                val = ln.split(":", 1)[1].strip().strip("'\"")
                if val and val not in ("|", ">", "|-", ">-"):
                    desc_parts.append(val)
                    in_desc_multiline = True
                else:
                    in_desc_multiline = True
            elif in_desc_multiline:
                if ln.startswith(("  ", "\t")) or (s and ":" not in s[:18]):
                    desc_parts.append(s)
                else:
                    in_desc_multiline = False
        if desc_parts:
            description = " ".join(desc_parts).strip()

    if not description:
        for ln in lines[:30]:
            s = ln.strip()
            if not s or s == "---" or s.startswith("#") or s.startswith("name:"):
                continue
            description = s
            break

    if not name:
        return None
    cmd_slug = name.strip().lstrip("/")
    return {
        "command": f"/{cmd_slug}",
        "name": cmd_slug,
        "description": description or f"Executar {source_badge}: /{cmd_slug}",
        "source": source_badge,
        "category": category,
        "path": str(md_path),
    }


def discover_slash_commands() -> List[Dict[str, Any]]:
    """Discover all installed Skills, Workflows, Custom Commands, and Built-in Slash Commands."""
    now = time.time()
    if _SLASH_COMMANDS_CACHE["items"] and (now - float(_SLASH_COMMANDS_CACHE["ts"])) < 25.0:
        return _SLASH_COMMANDS_CACHE["items"]

    home = Path.home()
    seen: Dict[str, Dict[str, Any]] = {}

    def _add(item: Optional[Dict[str, Any]]) -> None:
        if not item:
            return
        key = item["command"].lower()
        if key not in seen:
            seen[key] = item

    # 1. Neon Skills Hub & Claude Marketplaces (~/.claude/plugins/marketplaces/*/skills/*/SKILL.md)
    mp_root = home / ".claude" / "plugins" / "marketplaces"
    if mp_root.exists():
        for skill_md in sorted(mp_root.glob("*/skills/*/SKILL.md")):
            mp_name = skill_md.parent.parent.parent.name
            badge = "Neon Skills Hub" if "neon" in mp_name.lower() else f"Plugin ({mp_name})"
            _add(_parse_skill_md(skill_md, skill_md.parent.name, badge, "skill"))

    # 2. User Global Claude Skills & Commands (~/.claude/skills/*/SKILL.md, ~/.claude/commands/*.md)
    claude_skills = home / ".claude" / "skills"
    if claude_skills.exists():
        for skill_md in sorted(claude_skills.glob("*/SKILL.md")):
            _add(_parse_skill_md(skill_md, skill_md.parent.name, "Claude Skill", "skill"))
    claude_cmds = home / ".claude" / "commands"
    if claude_cmds.exists():
        for cmd_md in sorted(claude_cmds.glob("**/*.md")):
            _add(_parse_skill_md(cmd_md, cmd_md.stem, "Claude Command", "command"))

    # 3. Antigravity & Gemini Skills (~/.gemini/...)
    gemini_roots = [
        (home / ".gemini" / "antigravity" / "builtin" / "skills", "Antigravity Builtin"),
        (home / ".gemini" / "config" / "skills", "Gemini Skill"),
        (home / ".gemini" / "skills", "Gemini Skill"),
        (home / ".agent" / "skills", "Agent Skill"),
    ]
    for groot, badge in gemini_roots:
        if groot.exists():
            for skill_md in sorted(groot.glob("*/SKILL.md")):
                _add(_parse_skill_md(skill_md, skill_md.parent.name.replace("_", "-"), badge, "skill"))

    gemini_plugins = home / ".gemini" / "config" / "plugins"
    if gemini_plugins.exists():
        for skill_md in sorted(gemini_plugins.glob("*/skills/*/SKILL.md")):
            plug_name = skill_md.parent.parent.parent.name
            _add(_parse_skill_md(skill_md, skill_md.parent.name.replace("_", "-"), f"Plugin ({plug_name})", "skill"))

    agent_wf = home / ".agent" / "workflows"
    if agent_wf.exists():
        for wf_md in sorted(agent_wf.glob("*.md")):
            _add(_parse_skill_md(wf_md, wf_md.stem, "Workflow", "workflow"))

    # 4. Project-specific skills/commands/workflows across active agent repositories
    scanned_repos: Set[str] = set()
    for ag in list(hub.agents.values()):
        rpath = (ag.repo_path or "").strip()
        if not rpath or rpath in scanned_repos:
            continue
        scanned_repos.add(rpath)
        rp = Path(rpath)
        if not rp.exists():
            continue
        rname = ag.repo_name or rp.name
        for sub_glob, cat in [
            (".claude/skills/*/SKILL.md", "skill"),
            (".claude/commands/*.md", "command"),
            (".agent/skills/*/SKILL.md", "skill"),
            (".agent/workflows/*.md", "workflow"),
            (".gemini/skills/*/SKILL.md", "skill"),
        ]:
            try:
                for p_md in sorted(rp.glob(sub_glob)):
                    fb = p_md.parent.name if p_md.name == "SKILL.md" else p_md.stem
                    item = _parse_skill_md(p_md, fb, f"Repo ({rname})", cat)
                    if item:
                        item["repo_path"] = rpath
                        _add(item)
            except Exception:
                continue

    # 5. Built-in CLI / IDE Slash Commands (Terminal & Antigravity)
    builtins = [
        ("/stop", "⏹ Interrompe / para imediatamente a execução atual do agente (Stop / Ctrl+C / Esc)", "PAC-NEON Control", "builtin"),
        ("/parar", "⏹ Interrompe / para imediatamente a execução atual do agente (Stop / Ctrl+C / Esc)", "PAC-NEON Control", "builtin"),
        ("/compact", "Compacta o histórico da conversa mantendo o resumo essencial no contexto", "CLI Built-in", "builtin"),
        ("/clear", "Limpa o histórico da sessão atual e inicia um contexto limpo", "CLI Built-in", "builtin"),
        ("/review", "Solicita revisão completa de código / Pull Request da branch atual", "CLI Built-in", "builtin"),
        ("/init", "Analisa o repositório e gera/atualiza o arquivo de contexto (CLAUDE.md)", "Claude CLI", "builtin"),
        ("/cost", "Exibe o consumo de tokens e custo acumulado da sessão atual", "Claude CLI", "builtin"),
        ("/context", "Visualiza o uso detalhado da janela de contexto atual", "Claude CLI", "builtin"),
        ("/model", "Lista ou altera o modelo de IA ativo na sessão", "CLI Built-in", "builtin"),
        ("/mcp", "Inspeciona e gerencia servidores MCP conectados à sessão", "CLI Built-in", "builtin"),
        ("/memory", "Visualiza ou edita instruções persistentes de memória (CLAUDE.md)", "Claude CLI", "builtin"),
        ("/pr-comments", "Busca e lista os comentários do Pull Request aberto para a branch", "Claude CLI", "builtin"),
        ("/status", "Mostra status da sessão, modelo ativo, worktree e conectividade", "CLI Built-in", "builtin"),
        ("/diff", "Exibe o diff das alterações atuais no worktree", "CLI Built-in", "builtin"),
        ("/doctor", "Executa diagnóstico de saúde e configuração do agente", "Claude CLI", "builtin"),
        ("/goal", "Executa tarefa longa de forma autônoma e completa até atingir o objetivo", "Antigravity", "builtin"),
        ("/grill-me", "Inicia entrevista interativa para alinhar decisões de design do plano", "Antigravity", "builtin"),
        ("/learn", "Persiste um aprendizado ou correção de setup para próximas tarefas", "Antigravity", "builtin"),
        ("/browser", "Ativa navegação e interação visual com aplicações web", "Antigravity", "builtin"),
        ("/schedule", "Agenda execução recorrente (cron) ou timer para o agente", "Antigravity", "builtin"),
        ("/help", "Exibe a lista de comandos e ajuda interativa do agente", "CLI Built-in", "builtin"),
    ]
    for cmd, desc, src, cat in builtins:
        _add({
            "command": cmd,
            "name": cmd.lstrip("/"),
            "description": desc,
            "source": src,
            "category": cat,
        })

    # Sort: Neon Skills Hub first, then repo skills, then other skills, then built-ins
    def _sort_key(it: Dict[str, Any]) -> Any:
        src = it.get("source", "")
        if it.get("command") in ("/stop", "/parar"):
            prio = -1
        elif src == "Neon Skills Hub":
            prio = 0
        elif src.startswith("Repo ("):
            prio = 1
        elif it.get("category") == "skill":
            prio = 2
        elif it.get("category") in ("command", "workflow"):
            prio = 3
        else:
            prio = 4
        return (prio, it["command"].lower())

    result = sorted(seen.values(), key=_sort_key)
    _SLASH_COMMANDS_CACHE["ts"] = now
    _SLASH_COMMANDS_CACHE["items"] = result
    return result


@app.get("/api/slash-commands")
async def get_slash_commands_endpoint() -> dict:
    items = discover_slash_commands()
    return {"count": len(items), "commands": items}


@app.post("/api/settings")
async def update_settings_endpoint(payload: dict) -> dict:
    changed = False
    if "notification_timeout_sec" in payload:
        try:
            val = float(payload["notification_timeout_sec"])
            hub.notification_timeout_sec = max(5.0, min(3600.0, val))
            changed = True
        except Exception:
            pass
    meeting_patch = payload.get("meeting_settings") if isinstance(payload.get("meeting_settings"), dict) else payload
    if any(k in meeting_patch for k in MeetingCalendarGuard.DEFAULT_SETTINGS):
        hub.meeting_guard.update_settings(meeting_patch)
        changed = True
    if changed:
        hub._save_workspace_state()
    force_refresh = bool(payload.get("force_refresh_calendar", False) or changed)
    meeting_status = hub.meeting_guard.get_status(force_refresh=force_refresh)
    await hub.broadcast()
    return {
        "ok": True,
        "notification_timeout_sec": hub.notification_timeout_sec,
        "meeting": meeting_status,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.ws_clients.add(ws)
    try:
        hub.sync_live_agents()
        await ws.send_json(hub.snapshot())
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                hub.sync_live_agents()
                await ws.send_json(hub.snapshot())
    except (WebSocketDisconnect, Exception):
        hub.ws_clients.discard(ws)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _run_stdlib_http_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Zero-dependency HTTP server fallback when FastAPI/Uvicorn is not installed."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    threading.Thread(target=hub.sync_live_agents, daemon=True).start()

    class _Handler(BaseHTTPRequestHandler):
        def _cors_origin(self) -> str:
            origin = (self.headers.get("Origin") or "").strip() if hasattr(self, "headers") and self.headers else ""
            if origin and _is_trusted_local_origin(origin):
                return origin
            return "http://127.0.0.1:8765"

        def _send_json(self, payload: Any, status: int = 200) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Access-Control-Allow-Origin", self._cors_origin())
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self._cors_origin())
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:
            req_path = self.path.split("?", 1)[0]
            if req_path in ("/", "/index.html"):
                html = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif req_path == "/api/state":
                hub.sync_live_agents()
                self._send_json(hub.snapshot())
            elif req_path == "/api/slash-commands":
                items = discover_slash_commands()
                self._send_json({"count": len(items), "commands": items})
            elif req_path == "/api/superset/status":
                self._send_json(hub.superset_bridge.get_host_status())
            elif req_path == "/api/toolchain/status":
                self._send_json(hub.neon_cli.get_toolchain_status())
            elif req_path == "/api/providers":
                self._send_json({"providers": [m.model_dump() for m in AdapterRegistry.all_metadata()]})
            elif req_path == "/api/hosts":
                user_repos, default_repo = _discover_user_repositories(list(hub.agents.values()))
                self._send_json({
                    "hosts": _discover_available_hosts(),
                    "available_hosts": _discover_available_hosts(),
                    "cli_binaries": _discover_available_cli_binaries(),
                    "available_cli_binaries": _discover_available_cli_binaries(),
                    "repositories": user_repos,
                    "default_repo_path": default_repo,
                    "providers": [m.model_dump() for m in AdapterRegistry.all_metadata()],
                })
            elif req_path == "/api/doctor":
                self._send_json(run_environment_doctor())
            elif req_path.startswith("/api/agents/") and req_path.endswith("/handoff-preview"):
                try:
                    agent_id = req_path.split("/")[3]
                    res = asyncio.run(handoff_preview_agent(agent_id))
                    self._send_json(res)
                except HTTPException as exc:
                    self._send_json({"detail": exc.detail}, status=exc.status_code)
            else:
                self._send_json({"error": "Not found"}, status=404)

        def do_POST(self) -> None:
            origin = (self.headers.get("Origin") or "").strip()
            if origin and not _is_trusted_local_origin(origin):
                self._send_json(
                    {"ok": False, "detail": "Origem externa bloqueada por segurança local (CSRF guard)."},
                    status=403,
                )
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            try:
                if self.path.startswith("/api/permissions/") and self.path.endswith("/resolve"):
                    perm_id = self.path.split("/")[3]
                    req = ResolvePermissionRequest(
                        action=PermissionResolutionAction(body.get("action", "allow")),
                        custom_text=body.get("custom_text"),
                    )
                    res = asyncio.run(resolve_permission(perm_id, req))
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/stop"):
                    agent_id = self.path.split("/")[3]
                    res = asyncio.run(stop_agent_execution(agent_id))
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/command"):
                    agent_id = self.path.split("/")[3]
                    req = DirectCommandRequest(
                        prompt=body.get("prompt", ""),
                        auto_enter=bool(body.get("auto_enter", True)),
                    )
                    res = asyncio.run(send_agent_command(agent_id, req))
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/trigger-permission"):
                    agent_id = self.path.split("/")[3]
                    res = asyncio.run(trigger_test_permission(agent_id))
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/open-superset"):
                    agent_id = self.path.split("/")[3]
                    res = asyncio.run(open_agent_in_superset(agent_id))
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/focus"):
                    agent_id = self.path.split("/")[3]
                    res = asyncio.run(focus_agent_endpoint(agent_id, body))
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/neon-action"):
                    agent_id = self.path.split("/")[3]
                    res = asyncio.run(neon_agent_action_endpoint(agent_id, body))
                    self._send_json(res)
                elif self.path == "/api/agents/archive-inactive":
                    res = asyncio.run(archive_inactive_agents_endpoint())
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/archive"):
                    agent_id = self.path.split("/")[3]
                    res = asyncio.run(archive_agent_endpoint(agent_id, body))
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/rename"):
                    agent_id = self.path.split("/")[3]
                    res = asyncio.run(rename_agent_endpoint(agent_id, body))
                    self._send_json(res)
                elif self.path.startswith("/api/agents/") and self.path.endswith("/self-handoff"):
                    agent_id = self.path.split("/")[3]
                    req = SelfHandoffRequest(**(body or {}))
                    res = asyncio.run(self_handoff_agent(agent_id, req))
                    self._send_json(res)
                elif self.path == "/api/warp/send":
                    req = SendWarpFruitRequest(**body)
                    res = asyncio.run(send_warp_fruit(req))
                    self._send_json(res)
                elif self.path == "/api/agents/spawn":
                    req = SpawnAgentRequest(**body)
                    res = asyncio.run(spawn_new_agent(req))
                    self._send_json(res)
                elif self.path == "/api/plugins/register":
                    req = RegisterPluginProviderRequest(**body)
                    res = asyncio.run(register_plugin_provider(req))
                    self._send_json(res)
                elif self.path == "/api/hooks/request-permission":
                    req = HookPermissionPayload(**body)
                    res = asyncio.run(hook_request_permission(req))
                    self._send_json(res)
                elif self.path == "/api/hooks/agent-event":
                    res = asyncio.run(hook_agent_event(body))
                    self._send_json(res)
                elif self.path == "/api/system/accessibility-prompt":
                    res = asyncio.run(prompt_macos_accessibility())
                    self._send_json(res)
                elif self.path == "/api/bridge/heartbeat":
                    res = asyncio.run(bridge_heartbeat(body))
                    self._send_json(res)
                elif self.path == "/api/bridge/permission-alert":
                    res = asyncio.run(bridge_permission_alert(body))
                    self._send_json(res)
                elif self.path == "/api/settings":
                    res = asyncio.run(update_settings_endpoint(body))
                    self._send_json(res)
                else:
                    self._send_json({"error": "Not found"}, status=404)
            except HTTPException as exc:
                self._send_json({"detail": exc.detail}, status=exc.status_code)

        def log_message(self, format: str, *args: Any) -> None:
            return

    global _ACTIVE_STDLIB_SERVER
    ThreadingHTTPServer.allow_reuse_address = True
    srv = None
    for attempt in range(12):
        try:
            srv = ThreadingHTTPServer((host, port), _Handler)
            break
        except OSError:
            _free_stale_port(port)
            time.sleep(0.3)
    if srv is None:
        srv = ThreadingHTTPServer((host, port), _Handler)
    _ACTIVE_STDLIB_SERVER = srv
    print(f"🕹️  PAC-NEON ARCADE ORCHESTRATOR (Stdlib Server v0.3) rodando em: http://{host}:{port}")
    srv.serve_forever()


_ACTIVE_STDLIB_SERVER: Any = None


def _free_stale_port(port: int = 8765) -> None:
    """If an older pac_neon.server is still holding port 8765, terminate it cleanly so the updated server takes over."""
    my_pid = os.getpid()
    try:
        res = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True, timeout=2.0)
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != my_pid:
                    try:
                        os.kill(pid, 15)
                        time.sleep(0.25)
                    except OSError:
                        pass
    except Exception:
        pass


def _start_auto_reload_watcher() -> None:
    """Watches pac_neon/*.py files and hot-restarts the server process automatically when code changes."""
    pkg_dir = Path(__file__).resolve().parent
    project_root = pkg_dir.parent

    def _snapshot_mtimes() -> dict[str, float]:
        mtimes: dict[str, float] = {}
        for py_file in pkg_dir.rglob("*.py"):
            try:
                mtimes[str(py_file)] = py_file.stat().st_mtime
            except OSError:
                continue
        return mtimes

    initial = _snapshot_mtimes()

    def _watch() -> None:
        while True:
            time.sleep(2.0)
            current = _snapshot_mtimes()
            if current != initial:
                print("🔄 [HOT-RELOAD] Alteração detectada em pac_neon/*.py — reiniciando servidor automaticamente...")
                try:
                    if _ACTIVE_STDLIB_SERVER is not None:
                        _ACTIVE_STDLIB_SERVER.server_close()
                except Exception:
                    pass
                os.environ["PAC_NEON_NO_OPEN"] = "1"
                os.environ["PYTHONPATH"] = str(project_root)
                try:
                    os.chdir(str(project_root))
                except OSError:
                    pass
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "--no-open"])

    threading.Thread(target=_watch, daemon=True).start()


def main() -> None:
    project_root = str(Path(__file__).resolve().parent.parent)
    os.environ["PYTHONPATH"] = project_root
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    if "--daemon" in sys.argv:
        _free_stale_port(8765)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(STATE_FILE.parent / "server.log", "a", encoding="utf-8")
        child_args = [sys.executable, str(Path(__file__).resolve())] + [
            a for a in sys.argv[1:] if a != "--daemon"
        ]
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = project_root
        proc = subprocess.Popen(
            child_args,
            cwd=project_root,
            env=child_env,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        import socket

        for _ in range(25):
            time.sleep(0.1)
            try:
                with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
                    break
            except OSError:
                continue
        print(f"🕹️  PAC-NEON iniciado em segundo plano (PID {proc.pid}) -> http://127.0.0.1:8765")
        return

    _free_stale_port(8765)
    _start_auto_reload_watcher()

    auto_open = "--no-open" not in sys.argv and os.environ.get("PAC_NEON_NO_OPEN") != "1"
    if auto_open:
        def _delayed_open() -> None:
            import socket
            for _ in range(40):
                time.sleep(0.15)
                try:
                    with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
                        break
                except OSError:
                    continue
            print("🌐 Abrindo Pac-Neon automaticamente no navegador: http://127.0.0.1:8765")
            webbrowser.open("http://127.0.0.1:8765", new=0, autoraise=True)

        threading.Thread(target=_delayed_open, daemon=True).start()

    try:
        import uvicorn  # type: ignore

        if HAS_FASTAPI:
            print("🕹️  PAC-NEON ARCADE ORCHESTRATOR (FastAPI v0.3) rodando em: http://127.0.0.1:8765")
            uvicorn.run(app, host="127.0.0.1", port=8765, reload=False)
            return
    except ImportError:
        pass
    _run_stdlib_http_server("127.0.0.1", 8765)


if __name__ == "__main__":
    main()
