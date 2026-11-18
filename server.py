#!/usr/bin/env python3
"""Local grid and inbox for your own Sniffies account.

Sign in from the page with the account email and password. The server keeps
that session in memory and calls the API directly. Replies can include one
photo. Listens on 127.0.0.1.

A background watcher can announce new messages on your own Echo through the
Voice Monkey API. The token stays in a mode-600 file under ~/.config. Message
text is spoken only when read-aloud is on, and it is never written to the log.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import uuid
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("SNIFFIES_PORT", "8765"))
import sys as _sys

APP_DIR = Path(getattr(_sys, "_MEIPASS", Path(__file__).resolve().parent))
STATIC = Path(os.environ.get("GRIFFIES_STATIC") or (APP_DIR / "static"))
# Local, untracked settings (map key etc). Never bundled into desktop builds.
_LOCAL_CONFIG_PATHS = [
    Path(__file__).resolve().parent / "local-config.json",
    Path.home() / ".config" / "sniffies-messages" / "app.json",
    APP_DIR / "bundled-config.json",  # written by CI from a repo secret; desktop builds only
]


def _local_config() -> dict:
    for path in _LOCAL_CONFIG_PATHS:
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


UPDATE_REPO = os.environ.get("GRIFFIES_UPDATE_REPO", "mitchellorion/Griffies-Mashapp")


def app_version() -> str:
    for path in (APP_DIR / "VERSION", Path(__file__).resolve().parent / "VERSION"):
        try:
            v = path.read_text().strip()
            if v:
                return v
        except Exception:
            continue
    return "0.0.0-dev"


def _version_tuple(v: str) -> tuple:
    nums = re.findall(r"\d+", str(v).split("-")[0])
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


_update_cache: dict = {"at": 0.0, "data": None}


def update_check() -> dict:
    """Compare this build to the newest GitHub release. Cached for an hour; never raises."""
    current = app_version()
    base = {"current": current, "latest": None, "available": False, "url": None, "notes": ""}
    now = time.time()
    if _update_cache["data"] is not None and now - _update_cache["at"] < 3600:
        data = dict(_update_cache["data"])
        data["current"] = current
        data["available"] = bool(data.get("latest")) and _version_tuple(data["latest"]) > _version_tuple(current)
        return data
    request = urllib.request.Request(
        f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "griffies-update-check"},
    )
    try:
        with _direct_opener.open(request, timeout=8) as response:
            rel = json.loads(response.read(200000).decode() or "{}")
    except Exception:
        return base
    tag = str(rel.get("tag_name") or "").lstrip("vV")
    if not tag:
        return base
    assets = rel.get("assets") or []
    want = ".exe" if _sys.platform.startswith("win") else ".dmg"
    machine = (os.uname().machine if hasattr(os, "uname") else "").lower()
    arch = "apple-silicon" if machine.startswith("arm") or machine == "aarch64" else ("intel" if want == ".dmg" else "x64")
    direct = next((a.get("browser_download_url") for a in assets if str(a.get("name", "")).endswith(want) and arch in str(a.get("name", ""))), None)
    direct = direct or next((a.get("browser_download_url") for a in assets if str(a.get("name", "")).endswith(want)), None)
    data = {
        "current": current,
        "latest": tag,
        "url": direct or rel.get("html_url"),
        "page": rel.get("html_url"),
        "notes": str(rel.get("body") or "")[:600],
    }
    _update_cache.update(at=now, data=data)
    data = dict(data)
    data["available"] = _version_tuple(tag) > _version_tuple(current)
    return data


def app_config_public() -> dict:
    cfg = _local_config()
    key = str(os.environ.get("GRIFFIES_AWS_MAP_KEY") or cfg.get("awsMapKey") or "").strip()
    region = str(os.environ.get("GRIFFIES_AWS_MAP_REGION") or cfg.get("awsMapRegion") or "us-east-1").strip()
    return {"awsMapKey": key, "awsMapRegion": region, "desktop": bool(getattr(_sys, "frozen", False)), "version": app_version()}
API_HOST_DEFAULT = "uswapi2.sniffies.com"
ALEXA_CONFIG = Path(
    os.environ.get(
        "SNIFFIES_ALEXA_CONFIG",
        str(Path.home() / ".config" / "sniffies-messages" / "alexa.json"),
    )
)
ALEXA_STATE = Path(
    os.environ.get(
        "SNIFFIES_ALEXA_STATE",
        str(Path.home() / ".config" / "sniffies-messages" / "alexa-state.json"),
    )
)
ALEXA_POLL_SECONDS = 10
VOICE_MONKEY = "https://api-v3.voicemonkey.io"
NOTIFY_CONFIG = Path(
    os.environ.get(
        "SNIFFIES_NOTIFY_CONFIG",
        str(Path.home() / ".config" / "sniffies-messages" / "notify.json"),
    )
)
NOTIFY_STATE = Path(
    os.environ.get(
        "SNIFFIES_NOTIFY_STATE",
        str(Path.home() / ".config" / "sniffies-messages" / "notify-state.json"),
    )
)
SESSION_FILE = Path(
    os.environ.get(
        "SNIFFIES_SESSION",
        str(Path.home() / ".config" / "sniffies-messages" / "session.json"),
    )
)

USER_ID_RE = re.compile(r"^[a-f0-9]{24}$")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")

_lock = threading.Lock()
_jar: dict[str, str] = {}
_api_host = API_HOST_DEFAULT
_user_agent = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0"
)
_known_partners: dict[str, str] = {}  # conversationId -> other user id
_partner_account: dict[str, str] = {}
_alexa_status_lock = threading.Lock()
_alexa_status = {"lastError": "", "lastOk": ""}
_alexa_wake = threading.Event()
_alexa_stop = threading.Event()
_auto_responder_status_lock = threading.Lock()
_auto_responder_status = {"lastError": "", "lastOk": "", "enabled": False}
_auto_responder_events: list = []
_auto_responder_wake = threading.Event()
_auto_responder_stop = threading.Event()
_auto_responder_timer_lock = threading.Lock()
_auto_responder_timer: threading.Timer | None = None
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _auto_responder_cancel_wake() -> None:
    global _auto_responder_timer
    with _auto_responder_timer_lock:
        if _auto_responder_timer is not None:
            try:
                _auto_responder_timer.cancel()
            except Exception:
                pass
            _auto_responder_timer = None


def _auto_responder_schedule_wake(seconds: float) -> None:
    global _auto_responder_timer
    with _auto_responder_timer_lock:
        if _auto_responder_timer is not None:
            try:
                _auto_responder_timer.cancel()
            except Exception:
                pass
        _auto_responder_timer = threading.Timer(max(0.1, seconds), _auto_responder_wake.set)
        _auto_responder_timer.daemon = True
        _auto_responder_timer.start()


def _auto_responder_disable() -> None:
    """Immediately disable and cancel any queued reply."""
    _set_auto_responder_status(enabled=False)
    _auto_responder_cancel_wake()
    state = load_state_auto_responder()
    if state.get("pending"):
        state["pending"] = {}
        _save_state_auto_responder(state)
    _auto_responder_wake.set()


_explicit = False
_unflagged: set[str] = set()
_own_loc: list[float] | None = None
_pin: dict | None = None
_account_type = ""
_units = "imperial"
_country = "US"
_chat_photos: list[str] = []
_grindr: dict | None = None
_grindr_http = None
_grindr_http_lock = threading.Lock()
_partner_read: dict[str, str] = {}
_profiles: dict[str, dict] = {}
_account_email = ""
_accounts: list[dict] = []
_active = 0
PHOTO_LIMIT = 8 * 1024 * 1024
CHAT_PHOTO_HOST = "sniffies-chat-photos.s3.us-west-2.amazonaws.com"
_MEMBER_TYPES = {"paid", "ambassador", "admin"}
_grid_cache: dict = {"at": 0.0, "payload": None}
_places_cache: dict = {"at": 0.0, "payload": None}
_feed_raw: dict | None = None
GRID_TTL = 300
PLACE_HOST = "place.sniffiesassets.com"
PHOTO_KEEP = 40
LOGIN_HOSTS = ("uswapi2.sniffies.com", "uswapi.sniffies.com")
GRINDR_ORIGIN = "https://grindr.mobi"
GRINDR_UA = "grindr3/26.20.0.187695;187695;Free;Android 13;Pixel 7;Google"
GRINDR_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
GRINDR_CONVO_RE = re.compile(r"^[0-9]+:[0-9]+$")
GRINDR_MESSAGE_RE = re.compile(r"^[0-9]{10,16}:[0-9a-fA-F-]{32,40}$")
SNIFFIES_REACTIONS = {"fire", "like", "sweat", "devil"}
_GRINDR_CDN = {"cdns.grindr.com", "cdn.grindr.com"}

_AUTO_RESPONDER_CONFIG = Path(
    os.environ.get(
        "SNIFFIES_AUTO_RESPONDER_CONFIG",
        str(Path.home() / ".config" / "sniffies-messages" / "auto-responder.json"),
    )
)
_AUTO_RESPONDER_STATE = Path(
    os.environ.get(
        "SNIFFIES_AUTO_RESPONDER_STATE",
        str(Path.home() / ".config" / "sniffies-messages" / "auto-responder-state.json"),
    )
)
AUTO_RESPONDER_POLL_SECONDS = int(
    os.environ.get("SNIFFIES_AUTO_RESPONDER_POLL_SECONDS", "30")
)
_DEFAULT_AUTO_RESPONDER_PROMPT = (
    "You are writing a short reply on behalf of a user in a casual gay "
    "cruising/hookup chat app. Messages marked assistant are ones the user "
    "has already sent; messages marked user are from the other person. "
    "Reply naturally, briefly, and conversationally. Keep it under 200 "
    "characters. Avoid explicit sexual content, contact details, promises "
    "to meet, or anything that sounds robotic. If the last incoming message "
    "is only a photo, react to it briefly."
)

_grindr_files: dict[str, str] = {}
_grindr_drawer: dict[int, str] = {}
_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"
# Grindr profile ids, shared with Sniffies string values after aliasing.
BODY_SLUGS = {1: "toned", 2: "average", 3: "large", 4: "muscular", 5: "slim", 6: "stocky"}
POSITION_SLUGS = {1: "top", 2: "bottom", 3: "versatile", 4: "vers-bottom", 5: "vers-top", 6: "side"}
TRIBE_SLUGS = {
    1: "bear", 2: "clean-cut", 3: "daddy", 4: "discreet", 5: "geek", 6: "jock",
    7: "leather", 8: "otter", 9: "poz", 10: "rugged", 11: "trans", 12: "twink", 13: "sober",
}
_BODY_ALIAS = {
    "slim": "slim", "skinny": "slim", "thin": "slim",
    "toned": "toned", "athletic": "toned", "fit": "toned",
    "average": "average",
    "muscular": "muscular", "muscle": "muscular",
    "stocky": "stocky",
    "large": "large",
}
_POSITION_ALIAS = {
    "top": "top", "bottom": "bottom", "side": "side",
    "vers": "versatile", "versatile": "versatile",
    "vers top": "vers-top", "vers-top": "vers-top", "versatile top": "vers-top",
    "vers bottom": "vers-bottom", "vers-bottom": "vers-bottom", "versatile bottom": "vers-bottom",
}
_TRIBE_ALIAS = {slug: slug for slug in TRIBE_SLUGS.values()}
_TRIBE_ALIAS["clean cut"] = "clean-cut"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def refresh_session() -> None:
    if _explicit and _jar.get("access_token"):
        return
    raise ApiError(401, "Log in.")


def _apply_set_cookie(header_text: str) -> None:
    # curl -D dumps one header per line. Set-Cookie values can contain '='.
    for line in header_text.splitlines():
        if not line.lower().startswith("set-cookie:"):
            continue
        pair = line.split(":", 1)[1].strip().split(";", 1)[0]
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        if name.strip():
            _jar[name.strip()] = value.strip()


def api(method: str, path: str, body: dict | None = None, query: dict | None = None) -> tuple[int, bytes, str]:
    with _lock:
        return _api_unlocked(method, path, body, query)


def _api_unlocked(method: str, path: str, body: dict | None, query: dict | None) -> tuple[int, bytes, str]:
    refresh_session()
    url = f"https://{_api_host}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    header_file = tempfile.NamedTemporaryFile("w", prefix="sniff-h-", delete=False)
    data_file = None
    hdr_out = tempfile.NamedTemporaryFile("w+", prefix="sniff-d-", delete=False)
    out = tempfile.NamedTemporaryFile(prefix="sniff-b-", delete=False)
    try:
        cookie = "; ".join(f"{k}={v}" for k, v in _jar.items())
        header_file.write(
            f"User-Agent: {_user_agent}\n"
            "Accept: application/json, text/plain, */*\n"
            "Accept-Language: en-US,en;q=0.5\n"
            "Referer: https://sniffies.com/\n"
            "Origin: https://sniffies.com\n"
            "ngsw-bypass: true\n"
            "Sec-Fetch-Dest: empty\n"
            "Sec-Fetch-Mode: cors\n"
            "Sec-Fetch-Site: same-site\n"
            f"Cookie: {cookie}\n"
        )
        if body is not None:
            header_file.write("Content-Type: application/json\n")
        header_file.close()
        os.chmod(header_file.name, 0o600)
        cmd = [
            "curl",
            "--http2",
            "--silent",
            "--show-error",
            "--max-time",
            "25",
        ]
        cmd.extend(
            [
                "-H",
                f"@{header_file.name}",
                "-D",
                hdr_out.name,
                "-o",
                out.name,
                "-w",
                "%{http_code}",
                "-X",
                method,
                url,
            ]
        )
        if body is not None:
            data_file = tempfile.NamedTemporaryFile("w", prefix="sniff-p-", delete=False)
            json.dump(body, data_file)
            data_file.close()
            os.chmod(data_file.name, 0o600)
            cmd[1:1] = ["--data-binary", f"@{data_file.name}"]
        proc = subprocess.run(cmd, capture_output=True)
        status_text = proc.stdout.decode().strip() or "0"
        try:
            status = int(status_text)
        except ValueError:
            status = 0
        payload = Path(out.name).read_bytes()
        hdr_out.seek(0)
        _apply_set_cookie(hdr_out.read())
        if proc.returncode != 0 and status == 0:
            err = proc.stderr.decode("utf-8", "replace")[-300]
            raise ApiError(502, f"Could not reach Sniffies ({err.strip() or 'curl failed'}).")
        ctype = "application/json"
        for line in Path(hdr_out.name).read_text("utf-8", "replace").splitlines():
            if line.lower().startswith("content-type:"):
                ctype = line.split(":", 1)[1].strip()
        return status, payload, ctype
    finally:
        for path_ in (header_file.name, hdr_out.name, out.name):
            try:
                os.unlink(path_)
            except OSError:
                pass
        if data_file is not None:
            try:
                os.unlink(data_file.name)
            except OSError:
                pass


def me() -> str:
    user_id = _jar.get("id") or ""
    if not USER_ID_RE.match(user_id):
        raise ApiError(401, "Session is missing your user id. Log in again.")
    return user_id


def _other_id(conv: dict, user_id: str) -> str | None:
    for key in ("author1", "author2", "participants"):
        value = conv.get(key)
        if isinstance(value, str) and USER_ID_RE.match(value) and value != user_id:
            return value
    return None


def _online(data: dict) -> bool:
    connected = data.get("connectUpdateTime") or ""
    disconnected = data.get("disconnectTime") or ""
    return bool(connected) and connected >= disconnected


def _people(payload: dict) -> dict[str, dict]:
    people = {}
    for entry in payload.get("partialVisitorData") or []:
        if isinstance(entry, dict) and entry.get("_id"):
            people[entry["_id"]] = entry.get("data") or {}
    return people


def normalize_inbox(payload: dict) -> dict:
    user_id = me()
    people = _people(payload)
    items = []
    for conv in (payload.get("conversationData") or {}).get("conversations") or []:
        if not isinstance(conv, dict):
            continue
        other = _other_id(conv, user_id)
        if not other:
            continue
        person = people.get(other) or {}
        profile = person.get("profile") or {}
        stats = ((profile.get("extended") or {}).get("stats") or {})
        last = conv.get("lastSentMessage") or {}
        conversation_id = conv.get("id") or conv.get("_id")
        if not conversation_id:
            continue
        _known_partners[conversation_id] = other
        if person.get("accountType"):
            _partner_account[other] = str(person.get("accountType"))
        _index_profile(other, person)
        their_read = _their_read_id(conv, other)
        if their_read:
            _partner_read[conversation_id] = their_read
        body = last.get("body")
        preview = body if isinstance(body, str) and body else ""
        if not preview and (last.get("filesCount") or 0):
            preview = "Photo"
        items.append(
            {
                "conversationId": conversation_id,
                "userId": other,
                "age": stats.get("age"),
                "photo": _sfw_photo(profile.get("images") or []),
                "online": _online(person),
                "unread": conv.get("unreadCount") or 0,
                "preview": preview,
                "lastAt": last.get("createdAt"),
                "mine": last.get("author") == user_id,
                "accountType": person.get("accountType"),
                "theirReadId": their_read,
                "name": "",
                "source": "sniffies",
            }
        )
    items.sort(key=lambda item: item.get("lastAt") or "", reverse=True)
    return {"me": user_id, "conversations": items}


def _their_read_id(conv: dict, other: str) -> str | None:
    ids = conv.get("lastReadMessageIds")
    if isinstance(ids, dict):
        value = ids.get(other)
        if isinstance(value, str) and USER_ID_RE.match(value):
            return value
        if isinstance(value, dict):
            mid = value.get("messageId") or value.get("id") or value.get("_id")
            if isinstance(mid, str) and USER_ID_RE.match(mid):
                return mid
    last = conv.get("lastReadMessage")
    if isinstance(last, dict):
        mid = last.get("id") or last.get("_id") or last.get("messageId")
        if isinstance(mid, str) and USER_ID_RE.match(mid):
            return mid
    mid = conv.get("lastReadMessageId")
    if isinstance(mid, str) and USER_ID_RE.match(mid):
        return mid
    return None


def _index_profile(user_id: str, person: dict) -> None:
    if not USER_ID_RE.match(user_id) or not isinstance(person, dict):
        return
    profile = person.get("profile") if isinstance(person.get("profile"), dict) else {}
    stats = ((profile.get("extended") or {}).get("stats") or {}) if isinstance(profile.get("extended"), dict) else {}
    age = stats.get("age")
    rec = _profiles.get(user_id) or {}
    photo = _sfw_photo(profile.get("images") or [])
    if photo:
        rec["photo"] = photo
    if isinstance(age, (int, float)) and 18 <= age <= 120:
        rec["age"] = int(age)
    rec["online"] = _online(person)
    _profiles[user_id] = rec


def _face(user_id: str) -> dict:
    rec = dict(_profiles.get(user_id) or {})
    if rec.get("photo"):
        return rec
    cached = _grid_cache.get("payload") if isinstance(_grid_cache.get("payload"), dict) else None
    if cached:
        for card in cached.get("cards") or []:
            if isinstance(card, dict) and card.get("id") == user_id:
                return {
                    "photo": card.get("photo"),
                    "age": card.get("age"),
                    "online": bool(card.get("online")),
                }
    return rec


def normalize_messages(payload: dict) -> dict:
    user_id = me()
    messages = []
    for raw in payload.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        file_name = raw.get("file")
        if file_name and not FILE_RE.match(str(file_name)):
            file_name = None
        author = raw.get("author")
        messages.append(
            {
                "id": raw.get("id") or raw.get("_id"),
                "body": raw.get("body"),
                "file": file_name,
                "image": None,
                "author": author,
                "mine": author == user_id,
                "createdAt": raw.get("createdAt"),
                "spam": bool(raw.get("isSpam")),
                "reactions": _sniffies_reactions(raw.get("messageReactions"), user_id),
            }
        )
    messages.sort(key=lambda item: item.get("createdAt") or "")
    return {"messages": messages}


def _cookies_from_headers(header_text: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for line in header_text.splitlines():
        if not line.lower().startswith("set-cookie:"):
            continue
        pair = line.split(":", 1)[1].strip().split(";", 1)[0]
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        if name.strip():
            jar[name.strip()] = value.strip()
    return jar


def _direct_call(method: str, url: str, body: dict) -> tuple[int, str, bytes]:
    """One direct call to Sniffies."""
    header_file = tempfile.NamedTemporaryFile("w", prefix="sniff-h-", delete=False)
    data_file = tempfile.NamedTemporaryFile("w", prefix="sniff-p-", delete=False)
    hdr_out = tempfile.NamedTemporaryFile("w+", prefix="sniff-d-", delete=False)
    out = tempfile.NamedTemporaryFile(prefix="sniff-b-", delete=False)
    try:
        header_file.write(
            f"User-Agent: {_user_agent}\n"
            "Accept: application/json, text/plain, */*\n"
            "Accept-Language: en-US,en;q=0.5\n"
            "Referer: https://sniffies.com/\n"
            "Origin: https://sniffies.com\n"
            "ngsw-bypass: true\n"
            "Content-Type: application/json\n"
        )
        header_file.close()
        json.dump(body, data_file)
        data_file.close()
        os.chmod(header_file.name, 0o600)
        os.chmod(data_file.name, 0o600)
        proc = subprocess.run(
            [
                "curl",
                "--http2",
                "--silent",
                "--show-error",
                "--max-time",
                "25",
                "--data-binary",
                f"@{data_file.name}",
                "-H",
                f"@{header_file.name}",
                "-D",
                hdr_out.name,
                "-o",
                out.name,
                "-w",
                "%{http_code}",
                "-X",
                method,
                url,
            ],
            capture_output=True,
        )
        status_text = proc.stdout.decode().strip() or "0"
        try:
            status = int(status_text)
        except ValueError:
            status = 0
        if proc.returncode != 0 and status == 0:
            raise ApiError(502, "Could not reach Sniffies.")
        hdr_out.seek(0)
        return status, hdr_out.read(), Path(out.name).read_bytes()
    finally:
        for path_ in (header_file.name, data_file.name, hdr_out.name, out.name):
            try:
                os.unlink(path_)
            except OSError:
                pass


def _remember_account(raw: bytes) -> None:
    global _own_loc, _units, _country, _account_type
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        return
    udata = user.get("data") if isinstance(user.get("data"), dict) else {}
    loc = udata.get("location") if isinstance(udata.get("location"), dict) else {}
    coords = loc.get("coordinates")
    if (
        isinstance(coords, list)
        and len(coords) >= 2
        and all(isinstance(n, (int, float)) for n in coords[:2])
    ):
        _own_loc = [float(coords[0]), float(coords[1])]
    options = udata.get("options") if isinstance(udata.get("options"), dict) else {}
    if options.get("units") in ("imperial", "metric"):
        _units = options["units"]
    profile = udata.get("profile") if isinstance(udata.get("profile"), dict) else {}
    code = profile.get("countryCode")
    if isinstance(code, str) and len(code) == 2 and code.isalpha():
        _country = code.upper()
    account = udata.get("accountType")
    if isinstance(account, str) and account.isalpha() and len(account) <= 20:
        _account_type = account.lower()
    private = user.get("private") if isinstance(user.get("private"), dict) else {}
    photos = private.get("chatPhotos")
    if not isinstance(photos, list):
        photos = udata.get("chatPhotos")
    if not isinstance(photos, list):
        photos = user.get("chatPhotos")
    if isinstance(photos, list):
        kept: list[str] = []
        for name in photos:
            if not isinstance(name, str):
                continue
            if "/" in name or "\\" in name:
                name = name.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            if FILE_RE.match(name) and name not in kept:
                kept.append(name)
        _chat_photos[:] = kept[:PHOTO_KEEP]


def _note_chat_photo(name: str, save: bool = True) -> None:
    # The account stores a short file name. A full chat URL is the same file.
    if "/" in name or "\\" in name:
        name = name.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not FILE_RE.match(name):
        return
    if name in _chat_photos:
        _chat_photos.remove(name)
    _chat_photos.insert(0, name)
    del _chat_photos[PHOTO_KEEP:]
    if save and _explicit and _accounts:
        _save_session()


def _account_label(acc: dict) -> str:
    email = acc.get("email") if isinstance(acc.get("email"), str) else ""
    if "@" in email:
        return email.strip()[:80]
    uid = acc.get("user_id") if isinstance(acc.get("user_id"), str) else ""
    return (uid[:8] + "…") if uid else "Account"


def _snapshot_account() -> dict:
    return {
        "jar": dict(_jar),
        "api_host": _api_host,
        "user_agent": _user_agent,
        "own_loc": list(_own_loc) if _own_loc else None,
        "account_type": _account_type,
        "units": _units,
        "country": _country,
        "chat_photos": list(_chat_photos),
        "email": _account_email,
        "user_id": _jar.get("id") or "",
    }


def _clear_live_caches() -> None:
    _known_partners.clear()
    _partner_account.clear()
    _partner_read.clear()
    _profiles.clear()
    _unflagged.clear()
    _grid_cache["at"] = 0.0
    _grid_cache["payload"] = None
    _places_cache["at"] = 0.0
    _places_cache["payload"] = None
    global _feed_raw
    _feed_raw = None


def _apply_account(acc: dict) -> None:
    global _explicit, _api_host, _user_agent, _own_loc, _account_type, _units, _country, _account_email
    jar = acc.get("jar") if isinstance(acc.get("jar"), dict) else {}
    cleaned: dict[str, str] = {}
    for key, value in jar.items():
        if isinstance(key, str) and isinstance(value, str) and key and len(key) < 80 and len(value) < 8000:
            cleaned[key] = value
    _jar.clear()
    _jar.update(cleaned)
    _explicit = bool(_jar.get("access_token") and USER_ID_RE.match(_jar.get("id") or ""))
    host = acc.get("api_host")
    if isinstance(host, str) and host in LOGIN_HOSTS:
        _api_host = host
    ua = acc.get("user_agent")
    if isinstance(ua, str) and 10 < len(ua) < 400:
        _user_agent = ua
    loc = acc.get("own_loc")
    _own_loc = None
    if isinstance(loc, list) and len(loc) >= 2:
        try:
            _own_loc = [float(loc[0]), float(loc[1])]
        except (TypeError, ValueError):
            _own_loc = None
    account = acc.get("account_type")
    _account_type = account.lower() if isinstance(account, str) and account.isalpha() and len(account) <= 20 else ""
    units = acc.get("units")
    _units = units if units in ("imperial", "metric") else "imperial"
    country = acc.get("country")
    _country = country.upper() if isinstance(country, str) and len(country) == 2 and country.isalpha() else "US"
    email = acc.get("email")
    _account_email = email.strip()[:80] if isinstance(email, str) and "@" in email else ""
    _chat_photos.clear()
    photos = acc.get("chat_photos")
    if isinstance(photos, list):
        for name in photos:
            if isinstance(name, str):
                _note_chat_photo(name, save=False)
    _clear_live_caches()


def _sniffies_in() -> bool:
    return bool(_explicit and _jar.get("access_token"))


def _grindr_in() -> bool:
    return bool(_grindr and _grindr.get("session_id") and _grindr.get("auth_token"))


def session_public() -> dict:
    logged = _sniffies_in()
    accounts = []
    for index, acc in enumerate(_accounts):
        if not isinstance(acc, dict):
            continue
        accounts.append(
            {
                "index": index,
                "label": _account_label(acc),
                "active": index == _active,
            }
        )
    grindr_email = str((_grindr or {}).get("email") or "")
    return {
        "loggedIn": logged or _grindr_in(),
        "sniffies": logged,
        "grindr": _grindr_in(),
        "pinned": isinstance(_pin, dict),
        "grindrLabel": (grindr_email[:2] + "…" + grindr_email[-6:]) if _grindr_in() and "@" in grindr_email else "",
        "active": _active if logged else -1,
        "canAdd": logged and len(_accounts) < 2,
        "accounts": accounts if logged else [],
    }


def _save_session() -> None:
    global _active
    if _sniffies_in():
        snap = _snapshot_account()
        if not _accounts:
            _accounts.append(snap)
            _active = 0
        elif 0 <= _active < len(_accounts):
            _accounts[_active] = snap
    if not _accounts and not _grindr_in():
        _clear_session_file()
        return
    payload: dict = {"active": _active, "accounts": list(_accounts)}
    if isinstance(_pin, dict):
        payload["pin"] = {"lat": _pin["lat"], "lng": _pin["lng"], "mode": _pin["mode"]}
    if _grindr_in():
        payload["grindr"] = {
            "email": _grindr.get("email") or "",
            "profile_id": _grindr.get("profile_id") or "",
            "session_id": _grindr.get("session_id") or "",
            "auth_token": _grindr.get("auth_token") or "",
            "device": _grindr.get("device") or "",
            "user_agent": _grindr.get("user_agent") or GRINDR_UA,
            "exp": int(_grindr.get("exp") or 0),
            "geohash": _grindr.get("geohash") if _valid_geohash(_grindr.get("geohash")) else "",
        }
    _write_private(SESSION_FILE, payload)


def _clear_session_file() -> None:
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def _account_from_legacy(raw: dict) -> dict | None:
    jar = raw.get("jar")
    if not isinstance(jar, dict):
        return None
    cleaned: dict[str, str] = {}
    for key, value in jar.items():
        if isinstance(key, str) and isinstance(value, str) and key and len(key) < 80 and len(value) < 8000:
            cleaned[key] = value
    user_id = cleaned.get("id") or ""
    if "access_token" not in cleaned or not USER_ID_RE.match(user_id):
        return None
    return {
        "jar": cleaned,
        "api_host": raw.get("api_host") if raw.get("api_host") in LOGIN_HOSTS else API_HOST_DEFAULT,
        "user_agent": raw.get("user_agent") if isinstance(raw.get("user_agent"), str) else _user_agent,
        "own_loc": raw.get("own_loc"),
        "account_type": raw.get("account_type") or "",
        "units": raw.get("units") or "imperial",
        "country": raw.get("country") or "US",
        "chat_photos": raw.get("chat_photos") if isinstance(raw.get("chat_photos"), list) else [],
        "email": raw.get("email") if isinstance(raw.get("email"), str) else "",
        "user_id": user_id,
    }


def _load_session() -> None:
    global _active, _accounts
    try:
        raw = json.loads(SESSION_FILE.read_text())
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    loaded: list[dict] = []
    if isinstance(raw.get("accounts"), list):
        for item in raw["accounts"][:2]:
            if isinstance(item, dict):
                acc = _account_from_legacy(item)
                if acc:
                    loaded.append(acc)
        active = raw.get("active")
        _active = active if isinstance(active, int) and 0 <= active < len(loaded) else 0
    else:
        acc = _account_from_legacy(raw)
        if acc:
            loaded = [acc]
            _active = 0
    if loaded:
        _accounts = loaded
        _apply_account(_accounts[_active])
        print("session restored")
    _restore_pin(raw.get("pin"))
    _load_grindr(raw.get("grindr"))


def _commit_account() -> None:
    global _active, _accounts
    snap = _snapshot_account()
    uid = snap.get("user_id")
    for index, acc in enumerate(_accounts):
        if acc.get("user_id") == uid:
            _accounts[index] = snap
            _active = index
            _save_session()
            return
    if len(_accounts) < 2:
        _accounts.append(snap)
        _active = len(_accounts) - 1
    elif 0 <= _active < len(_accounts):
        _accounts[_active] = snap
    else:
        _accounts = [snap]
        _active = 0
    _save_session()


def login(email: str, password: str) -> dict:
    global _explicit, _api_host, _account_email
    email = email.strip()
    if "@" not in email or len(email) > 200 or any(ch.isspace() for ch in email):
        raise ApiError(400, "Enter the email on the account.")
    if not password or len(password) > 200:
        raise ApiError(400, "Enter the password.")
    body = {
        "email": email,
        "password": password,
        "promoName": None,
        "appVersion": "27",
        "voluumCID": None,
    }
    last_status = 0
    for host in LOGIN_HOSTS:
        status, header, payload = _direct_call(
            "POST", f"https://{host}/api/authenticate", body
        )
        last_status = status
        if status != 200:
            continue
        jar = _cookies_from_headers(header)
        user_id = jar.get("id") or ""
        if "access_token" not in jar or not USER_ID_RE.match(user_id):
            raise ApiError(401, "Login did not return a session.")
        with _lock:
            if _explicit and _jar.get("access_token") and _accounts:
                _accounts[_active] = _snapshot_account()
            previous_jar = dict(_jar)
            previous_explicit = _explicit
            previous_email = _account_email
            previous_host = _api_host
            _jar.clear()
            _jar.update(jar)
            _explicit = True
            _api_host = host
            _account_email = email
            _chat_photos.clear()
            _clear_live_caches()
            try:
                _remember_account(payload)
                _commit_account()
            except Exception:
                _jar.clear()
                _jar.update(previous_jar)
                _explicit = previous_explicit
                _account_email = previous_email
                _api_host = previous_host
                raise
        return session_public() | {"ok": True}
    if last_status in (401, 403):
        raise ApiError(401, "That email or password was not accepted.")
    raise ApiError(502, "Sniffies did not accept the login.")


def switch_account(index: int) -> dict:
    global _active
    if not isinstance(index, int) or index < 0 or index >= len(_accounts):
        raise ApiError(400, "Unknown account.")
    with _lock:
        if index == _active and _explicit:
            return session_public()
        if _explicit and _jar.get("access_token"):
            _accounts[_active] = _snapshot_account()
        _active = index
        _apply_account(_accounts[_active])
        _save_session()
    return session_public()


def logout(network: str = "both") -> dict:
    global _explicit, _own_loc, _account_type, _account_email, _active, _grindr, _pin
    network = network if network in ("sniffies", "grindr", "both") else "both"
    with _lock:
        if network in ("grindr", "both"):
            _grindr = None
            _grindr_files.clear()
            _grindr_drawer.clear()
            _grid_cache["at"] = 0
            _grid_cache["payload"] = None
        if network in ("sniffies", "both"):
            if _accounts and 0 <= _active < len(_accounts):
                _accounts.pop(_active)
            if _accounts and network == "sniffies":
                _active = min(_active, len(_accounts) - 1)
                _apply_account(_accounts[_active])
                _save_session()
                return session_public() | {"ok": True}
            _jar.clear()
            _explicit = False
            _own_loc = None
            _account_type = ""
            _account_email = ""
            _accounts.clear()
            _active = 0
            _chat_photos.clear()
            _clear_live_caches()
        if not _sniffies_in() and not _grindr_in():
            _pin = None
        _save_session()
    return session_public() | {"ok": True}


def _sfw_photo(images: list) -> str | None:
    usable = []
    for img in images:
        if not isinstance(img, dict):
            continue
        path = str(img.get("path") or "")
        if FILE_RE.match(path):
            usable.append(img)
    def pool(kind: str) -> dict | None:
        group = [img for img in usable if img.get("classification") == kind]
        primary = [img for img in group if img.get("isPrimary")]
        return (primary or group or [None])[0]
    chosen = pool("SFW") or pool("NA")
    if not chosen:
        return None
    return str(chosen.get("path"))


def _sfw_photos(images: list) -> list[str]:
    usable = []
    for img in images:
        if not isinstance(img, dict):
            continue
        path = str(img.get("path") or "")
        kind = img.get("classification")
        if FILE_RE.match(path) and kind in ("SFW", "NA"):
            usable.append(img)
    primary = [img for img in usable if img.get("isPrimary")]
    rest = [img for img in usable if not img.get("isPrimary")]
    paths: list[str] = []
    for img in primary + rest:
        path = str(img.get("path"))
        if path not in paths:
            paths.append(path)
        if len(paths) >= 12:
            break
    return paths


def _load_map_feed() -> dict:
    global _feed_raw
    if not _home_coords():
        status, payload, _ctype = api("POST", "/api/authenticate/current", {})
        if status != 200:
            raise ApiError(status, _error_message(payload, status))
        with _lock:
            _remember_account(payload)
    point = _sniffies_coords()
    if not point:
        raise ApiError(502, "Your profile has no location yet.")
    lat, lng = point
    status, payload, _ctype = api(
        "POST",
        "/api/post-authentication",
        {
            "countryCode": _country,
            "physicalLocation": {"lat": lat, "lng": lng},
            "virtualLocation": {"lat": lat, "lng": lng},
            "place": {"radius": 20},
            "currentConversationUserIds": [],
            "otherConversationUserIds": [],
            "filters": {},
            "privateExtendedProfile": {},
        },
        {"timeThreshold": "0"},
    )
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Sniffies returned an unreadable map.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Sniffies returned an unreadable map.")
    _feed_raw = data
    _places_cache["payload"] = _normalize_places(data)
    _places_cache["at"] = time.time()
    return data


def _build_grid() -> dict:
    data = _load_map_feed()
    chats: dict[str, dict] = {}
    try:
        for convo in fetch_inbox().get("conversations") or []:
            chats[convo["userId"]] = convo
    except ApiError:
        chats = {}
    visitors = ((data.get("nearbyVisitors") or {}).get("visitors")) or []
    cards = []
    for raw in visitors:
        if not isinstance(raw, dict):
            continue
        user_id = raw.get("_id")
        if not isinstance(user_id, str) or not USER_ID_RE.match(user_id):
            continue
        person = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        if person.get("isSuspended") or person.get("isHidden"):
            continue
        _index_profile(user_id, person)
        profile = person.get("profile") if isinstance(person.get("profile"), dict) else {}
        stats = ((profile.get("extended") or {}).get("stats") or {}) if isinstance(profile.get("extended"), dict) else {}
        age = stats.get("age")
        age = int(age) if isinstance(age, (int, float)) and 18 <= age <= 120 else None
        distance = raw.get("distance")
        distance = round(float(distance), 1) if isinstance(distance, (int, float)) and 0 <= distance < 20000 else None
        chat = chats.get(user_id) or {}
        traits = _sniffies_traits(person)
        cards.append(
            {
                "id": user_id,
                "age": age,
                "distance": distance,
                "online": _online(person),
                "photo": _sfw_photo(profile.get("images") or []),
                "conversationId": chat.get("conversationId"),
                "unread": int(chat.get("unread") or 0),
                "source": "sniffies",
                "heightCm": traits["heightCm"],
                "weightKg": traits["weightKg"],
                "body": traits["body"],
                "position": traits["position"],
                "tags": traits["tags"],
            }
        )
    cards.sort(key=lambda card: (card["distance"] is None, card["distance"] or 0))
    return {"units": _units, "cards": cards}


def _load_grindr(raw: object) -> None:
    global _grindr
    if not isinstance(raw, dict):
        return
    email = raw.get("email") if isinstance(raw.get("email"), str) else ""
    session_id = raw.get("session_id") if isinstance(raw.get("session_id"), str) else ""
    auth_token = raw.get("auth_token") if isinstance(raw.get("auth_token"), str) else ""
    device = raw.get("device") if isinstance(raw.get("device"), str) else ""
    if "@" not in email or len(session_id) < 20 or len(auth_token) < 8 or ";" not in device:
        return
    _grindr = {
        "email": email[:200],
        "profile_id": str(raw.get("profile_id") or "")[:40],
        # Session JWTs carry feature flags and run past 4k. Cutting them
        # makes every later call look signed out.
        "session_id": session_id[:16000],
        "auth_token": auth_token[:16000],
        "device": device[:200],
        "user_agent": raw.get("user_agent") if isinstance(raw.get("user_agent"), str) else GRINDR_UA,
        "exp": int(raw.get("exp") or 0) if isinstance(raw.get("exp"), int) else 0,
        "geohash": raw.get("geohash") if _valid_geohash(raw.get("geohash")) else "",
    }
    print("grindr session restored")


def _new_grindr_device() -> str:
    # totalMem from a Pixel-class device. A round 8000000000 is not a real reading.
    return f"{secrets.token_hex(8)};GLOBAL;2;8026152960;2400x1080;{uuid.uuid4()}"


def _jwt_exp(token: str) -> int:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        exp = data.get("exp")
        return int(exp) if isinstance(exp, (int, float)) else 0
    except Exception:
        return 0


def _grindr_http_client():
    global _grindr_http
    if _grindr_http is not None:
        return _grindr_http
    with _grindr_http_lock:
        if _grindr_http is None:
            try:
                from wreq import Emulation
                from wreq.blocking import Client
                from wreq.redirect import Policy
            except ImportError as exc:
                raise ApiError(502, "Grindr sign-in needs the wreq package.") from exc
            # OkHttp's TLS and HTTP/2 fingerprint. curl is refused by Cloudflare
            # with an HTML 403 before the API sees the request.
            _grindr_http = Client(
                emulation=Emulation.OkHttp5,
                timeout=timedelta(seconds=25),
                redirect=Policy.none(),
                no_proxy=True,
                gzip=True,
            )
        return _grindr_http


def _grindr_headers(session_id: str | None, *, real_device: bool = False, anon_device: bool = False) -> list[tuple[str, str]]:
    current = _grindr or {}
    # Order matches the Android client: the device-info flag, then
    # authorization, timezone, roles, device, accept, user-agent, locale.
    headers: list[tuple[str, str]] = []
    if real_device:
        headers.append(("requirerealdeviceinfo", "true"))
    if anon_device:
        headers.append(("requireanondeviceinfo", "true"))
    if session_id:
        headers.append(("authorization", "Grindr3 " + session_id))
    headers.append(("l-time-zone", "America/Los_Angeles"))
    if session_id:
        headers.append(("l-grindr-roles", "[FREE]"))
    headers.append(("l-device-info", str(current.get("device") or "")))
    headers.append(("accept", "application/json"))
    headers.append(("user-agent", str(current.get("user_agent") or GRINDR_UA)))
    headers.append(("l-locale", "en_US"))
    headers.append(("accept-language", "en-US"))
    headers.append(("accept-encoding", "gzip"))
    return headers


def _grindr_call(
    method: str,
    path: str,
    body: dict | None = None,
    session_id: str | None = None,
    *,
    real_device: bool = False,
    anon_device: bool = False,
    raw: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    try:
        from wreq import Method
    except ImportError as exc:
        raise ApiError(502, "Grindr sign-in needs the wreq package.") from exc

    headers = _grindr_headers(session_id, real_device=real_device, anon_device=anon_device)
    payload = None
    if raw is not None:
        headers.append(("content-type", content_type or "application/octet-stream"))
        payload = raw
    elif body is not None:
        headers.append(("content-type", "application/json; charset=utf-8"))
        payload = json.dumps(body).encode()
    verb = getattr(Method, method.upper(), None)
    if verb is None:
        raise ApiError(500, "Grindr did not accept the request.")
    try:
        response = _grindr_http_client().request(
            verb,
            GRINDR_ORIGIN + path,
            headers=dict(headers),
            orig_headers=[name for name, _value in headers],
            body=payload,
        )
        return response.status.as_int(), bytes(response.bytes())
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(502, "Could not reach Grindr.") from exc


def _grindr_blocked(payload: bytes) -> bool:
    sample = payload.lstrip()[:600].lower()
    return sample.startswith(b"<") or b"cloudflare" in sample or b"<!doctype" in sample


def _grindr_error(payload: bytes, status: int) -> str:
    if _grindr_blocked(payload):
        return "Grindr's network check blocked the sign-in. Try again."
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        message = str(data.get("message") or data.get("title") or "")
        normalized = message.upper().replace(" ", "_")
        if normalized == "INVALID_INPUT_PARAMETERS":
            return "That Grindr email or password was not accepted."
        if message == "ACCOUNT_BANNED":
            return "Grindr blocked the sign-in. Open the official app once, then try again."
        if data.get("type") == "urn:gr:err:geo_hash_decode":
            return "Sign in to Sniffies so Grindr profiles can use your location."
        if message:
            return "Grindr did not accept the request."
    if status in (401, 403):
        return "Grindr asked you to sign in again."
    return "Grindr did not accept the request."


def _valid_geohash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 12 and set(value) <= set(_GEOHASH_ALPHABET)


def _store_grindr(email: str, data: dict, device: str, user_agent: str, geohash: object = None) -> None:
    global _grindr
    session_id = data.get("sessionId")
    auth_token = data.get("authToken")
    profile_id = data.get("profileId")
    if not isinstance(session_id, str) or not isinstance(auth_token, str):
        raise ApiError(502, "Grindr did not return a session.")
    if not _valid_geohash(geohash):
        current = _grindr.get("geohash") if isinstance(_grindr, dict) else ""
        geohash = current if _valid_geohash(current) else ""
    _grindr = {
        "email": email,
        "profile_id": str(profile_id or "")[:40],
        "session_id": session_id,
        "auth_token": auth_token,
        "device": device,
        "user_agent": user_agent,
        "exp": _jwt_exp(session_id),
        "geohash": geohash,
    }
    _grid_cache["at"] = 0
    _grid_cache["payload"] = None
    _save_session()


def grindr_login(email: str, password: str) -> dict:
    global _grindr
    email = email.strip()
    if "@" not in email or len(email) > 200 or any(ch.isspace() for ch in email):
        raise ApiError(400, "Enter the email on the Grindr account.")
    if not password or len(password) > 200:
        raise ApiError(400, "Enter the password.")
    device = (_grindr or {}).get("device") or _new_grindr_device()
    user_agent = (_grindr or {}).get("user_agent") or GRINDR_UA
    with _lock:
        previous = dict(_grindr) if _grindr else None
        _grindr = {"device": device, "user_agent": user_agent}
    try:
        status, payload = _grindr_call(
            "POST",
            "/v8/sessions",
            {"email": email, "password": password, "token": None, "geohash": None},
            real_device=True,
        )
    except ApiError:
        with _lock:
            _grindr = previous
        raise
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = None
    if _grindr_blocked(payload):
        with _lock:
            _grindr = previous
        raise ApiError(502, _grindr_error(payload, status))
    if status != 200 or not isinstance(data, dict):
        with _lock:
            _grindr = previous
        raise ApiError(401 if status in (401, 403) else 502, _grindr_error(payload, status))
    with _lock:
        kept = previous.get("geohash") if isinstance(previous, dict) else ""
        _store_grindr(email, data, device, user_agent, kept)
    return session_public() | {"ok": True}


def _refresh_grindr() -> None:
    global _grindr
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    if int(_grindr.get("exp") or 0) > time.time() + 90:
        return
    status, payload = _grindr_call(
        "POST",
        "/v8/sessions",
        {"email": _grindr["email"], "authToken": _grindr["auth_token"], "token": None, "geohash": None},
        real_device=True,
    )
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = None
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("sessionId"), str):
        raise ApiError(401, "Grindr asked you to sign in again.")
    with _lock:
        _store_grindr(_grindr["email"], data, _grindr["device"], _grindr["user_agent"])


def _geohash12(lat: float, lon: float) -> str:
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bits = (16, 8, 4, 2, 1)
    even = True
    bit = 0
    chunk = 0
    out: list[str] = []
    while len(out) < 12:
        if even:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                chunk |= bits[bit]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                chunk |= bits[bit]
                lat_range[0] = mid
            else:
                lat_range[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_GEOHASH_ALPHABET[chunk])
            bit = 0
            chunk = 0
    return "".join(out)


def _uk_coords(lat: float, lon: float) -> bool:
    # Grindr locks an account when a location is set inside the UK.
    return 49.5 <= lat <= 61.0 and -8.5 <= lon <= 2.0


def _pair_coords(lat: object, lng: object) -> tuple[float, float] | None:
    lat_n = _bounded_number(lat, -90, 90)
    lng_n = _bounded_number(lng, -180, 180)
    if lat_n is None or lng_n is None:
        return None
    return lat_n, lng_n


def _home_coords() -> tuple[float, float] | None:
    # Account home stays in _own_loc. A saved pin must not replace it, or the
    # next sign-in response would be the only copy of the real location.
    if not _own_loc or len(_own_loc) < 2:
        return None
    return _pair_coords(_own_loc[1], _own_loc[0])


def _pin_coords() -> tuple[float, float] | None:
    if not isinstance(_pin, dict):
        return None
    return _pair_coords(_pin.get("lat"), _pin.get("lng"))


def _sniffies_coords() -> tuple[float, float] | None:
    return _pin_coords() or _home_coords()


def _restore_pin(raw: object) -> None:
    global _pin
    _pin = None
    if not isinstance(raw, dict):
        return
    point = _pair_coords(raw.get("lat"), raw.get("lng"))
    mode = raw.get("mode")
    if point is None or mode not in ("live", "pin"):
        return
    lat, lng = point
    _pin = {"lat": round(lat, 7), "lng": round(lng, 7), "mode": mode}


def _drop_location_caches() -> None:
    global _feed_raw
    _grid_cache["at"] = 0.0
    _grid_cache["payload"] = None
    _places_cache["at"] = 0.0
    _places_cache["payload"] = None
    _feed_raw = None


def _grindr_location_block(lat: float, lon: float) -> str:
    if _country == "GB" or _uk_coords(lat, lon):
        return "Grindr locks accounts when a location is set in the UK, so those profiles stay off this grid."
    return ""


def location_public() -> dict:
    pin = _pin_coords()
    home = _home_coords()
    current = pin or home
    return {
        "lat": current[0] if current else None,
        "lng": current[1] if current else None,
        "pinned": pin is not None,
        "mode": _pin.get("mode") if isinstance(_pin, dict) and pin else ("home" if home else ""),
        "home": {"lat": home[0], "lng": home[1]} if home else None,
    }


def set_location(mode: str, lat: object, lng: object) -> dict:
    global _pin
    if not _sniffies_in() and not _grindr_in():
        raise ApiError(401, "Log in.")
    warning = ""
    if mode == "home":
        _pin = None
        _drop_location_caches()
        _save_session()
        home = _home_coords()
        if home:
            warning = _move_grindr(home[0], home[1])
        elif _grindr_in():
            warning = "Sign in to Sniffies so Grindr can use the account location."
        return location_public() | {"ok": True, "warning": warning}
    if mode not in ("live", "pin"):
        raise ApiError(400, "Unknown location.")
    point = _pair_coords(lat, lng)
    if point is None:
        raise ApiError(400, "That point is not on the map.")
    lat_n, lng_n = point
    _pin = {"lat": round(lat_n, 7), "lng": round(lng_n, 7), "mode": mode}
    _drop_location_caches()
    _save_session()
    warning = _move_grindr(lat_n, lng_n)
    return location_public() | {"ok": True, "warning": warning}


def _move_grindr(lat: float, lon: float) -> str:
    if not _grindr_in():
        return ""
    blocked = _grindr_location_block(lat, lon)
    if blocked:
        return blocked
    digest = _geohash12(lat, lon)
    status, payload = _grindr_authed("PUT", "/v4/location", {"geohash": digest})
    if status not in (200, 201, 204):
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    _remember_grindr_geohash(digest)
    return ""


_geocode_lock = threading.Lock()
_geocode_at = 0.0


def search_places(query: str) -> dict:
    global _geocode_at
    if not _sniffies_in() and not _grindr_in():
        raise ApiError(401, "Log in.")
    query = " ".join(str(query or "").split())
    if len(query) < 2 or len(query) > 80:
        raise ApiError(400, "Enter a place name.")
    with _geocode_lock:
        now = time.time()
        if now - _geocode_at < 1:
            raise ApiError(429, "Wait a moment and search again.")
        _geocode_at = now
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": "5"}
    )
    proc = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "12",
            "--max-redirs",
            "0",
            "-A",
            "Nearby/1.0 (personal local map)",
            "-H",
            "Accept: application/json",
            url,
        ],
        capture_output=True,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Search is unavailable.") from exc
    if not isinstance(data, list):
        raise ApiError(502, "Search is unavailable.")
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        point = _pair_coords(item.get("lat"), item.get("lon"))
        label = item.get("display_name")
        if point is None or not isinstance(label, str) or not label.strip():
            continue
        results.append({"label": label.strip()[:160], "lat": point[0], "lng": point[1]})
        if len(results) >= 5:
            break
    return {"results": results}


def _nearby_geohash() -> str:
    coords = _sniffies_coords()
    if coords is None:
        saved = (_grindr or {}).get("geohash")
        if _valid_geohash(saved):
            return str(saved)
        raise ApiError(502, "Sign in to Sniffies so Grindr profiles can use your location.")
    lat, lon = coords
    if _country == "GB" or _uk_coords(lat, lon):
        raise ApiError(502, "Grindr locks accounts when a location is set in the UK, so those profiles stay off this grid.")
    return _geohash12(lat, lon)


def _remember_grindr_geohash(digest: str) -> None:
    if not _grindr_in() or not _valid_geohash(digest) or _grindr.get("geohash") == digest:
        return
    _grindr["geohash"] = digest
    _save_session()


def _bounded_number(value: object, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if lo <= number <= hi:
        return number
    return None


def _choice_slug(value: object, aliases: dict[str, str]) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip().lower().replace("_", " ").replace("-", " ")
    text = " ".join(text.split())
    return aliases.get(text) or aliases.get(text.replace(" ", "-"))


def _id_slugs(value: object, table: dict[int, str]) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    slugs: list[str] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        slug = table.get(number)
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def _label_list(value: object) -> list[str]:
    raw = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    labels: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = " ".join(item.strip().lower().replace("_", " ").split())
        if text and text not in labels:
            labels.append(text)
    return labels


def _weight_kg(value: object) -> float | None:
    number = _bounded_number(value, 40, 400000)
    if number is None:
        return None
    if number > 400:
        number = number / 1000
    if 40 <= number <= 320:
        return round(number, 1)
    return None


def _sniffies_traits(person: dict) -> dict:
    profile = person.get("profile") if isinstance(person.get("profile"), dict) else {}
    extended = profile.get("extended") if isinstance(profile.get("extended"), dict) else {}
    stats = extended.get("stats") if isinstance(extended.get("stats"), dict) else {}
    sexuality = extended.get("sexuality") if isinstance(extended.get("sexuality"), dict) else {}
    identity = extended.get("identity") if isinstance(extended.get("identity"), dict) else {}
    scene = extended.get("scene") if isinstance(extended.get("scene"), dict) else {}
    height = _bounded_number(stats.get("heightInCm"), 120, 230)
    tags: list[str] = []
    for label in _label_list(identity.get("ethnicity")) + _label_list(identity.get("gender")):
        tribe = _TRIBE_ALIAS.get(label) or _TRIBE_ALIAS.get(label.replace(" ", "-"))
        if tribe and tribe not in tags:
            tags.append(tribe)
    for key in ("hosting", "public", "attraction", "fetishes", "kinks", "action", "interaction"):
        for label in _label_list(scene.get(key)):
            tribe = _TRIBE_ALIAS.get(label) or _TRIBE_ALIAS.get(label.replace(" ", "-"))
            if tribe and tribe not in tags:
                tags.append(tribe)
    for label in _label_list(sexuality.get("expression")):
        tribe = _TRIBE_ALIAS.get(label) or _TRIBE_ALIAS.get(label.replace(" ", "-"))
        if tribe and tribe not in tags:
            tags.append(tribe)
    return {
        "heightCm": int(round(height)) if height is not None else None,
        "weightKg": _weight_kg(stats.get("weightInKg")),
        "body": _choice_slug(stats.get("body"), _BODY_ALIAS),
        "position": _choice_slug(sexuality.get("attitude") or sexuality.get("position"), _POSITION_ALIAS),
        "tags": tags,
    }


def _grindr_traits(person: dict) -> dict:
    height = _bounded_number(person.get("heightCm") if person.get("heightCm") is not None else person.get("height"), 120, 230)
    weight = person.get("weightGrams") if person.get("weightGrams") is not None else person.get("weight")
    tribes = person.get("tribes")
    if not isinstance(tribes, list):
        tribes = person.get("grindrTribes")
    return {
        "heightCm": int(round(height)) if height is not None else None,
        "weightKg": _weight_kg(weight),
        "body": (_id_slugs(person.get("bodyType"), BODY_SLUGS) or [None])[0],
        "position": (_id_slugs(person.get("sexualPosition"), POSITION_SLUGS) or [None])[0],
        "tags": _id_slugs(tribes, TRIBE_SLUGS),
    }


def _empty_filters() -> dict:
    return {
        "photo": False,
        "online": False,
        "ageMin": None,
        "ageMax": None,
        "heightMin": None,
        "heightMax": None,
        "weightMin": None,
        "weightMax": None,
        "bodies": [],
        "positions": [],
        "tribes": [],
    }


def _query_filters(query: dict) -> dict:
    def flag(name: str) -> bool:
        return (query.get(name) or ["0"])[0] == "1"

    def bound(name: str, lo: float, hi: float) -> float | None:
        raw = (query.get(name) or [""])[0].strip()
        if not raw:
            return None
        return _bounded_number(raw, lo, hi)

    def slugs(name: str, allowed: dict[int, str]) -> list[str]:
        raw = (query.get(name) or [""])[0]
        chosen: list[str] = []
        for part in raw.split(","):
            if not part.strip().isdigit():
                continue
            slug = allowed.get(int(part))
            if slug and slug not in chosen:
                chosen.append(slug)
            if len(chosen) >= 13:
                break
        return chosen

    age_min = bound("ageMin", 18, 99)
    age_max = bound("ageMax", 18, 99)
    if age_min is not None and age_max is not None and age_min > age_max:
        age_min, age_max = age_max, age_min
    height_min = bound("heightMin", 120, 230)
    height_max = bound("heightMax", 120, 230)
    if height_min is not None and height_max is not None and height_min > height_max:
        height_min, height_max = height_max, height_min
    weight_min = bound("weightMin", 40, 320)
    weight_max = bound("weightMax", 40, 320)
    if weight_min is not None and weight_max is not None and weight_min > weight_max:
        weight_min, weight_max = weight_max, weight_min
    filters = {
        "photo": flag("photo"),
        "online": flag("online"),
        "ageMin": int(age_min) if age_min is not None else None,
        "ageMax": int(age_max) if age_max is not None else None,
        "heightMin": int(height_min) if height_min is not None else None,
        "heightMax": int(height_max) if height_max is not None else None,
        "weightMin": weight_min,
        "weightMax": weight_max,
        "bodies": slugs("bodies", BODY_SLUGS),
        "positions": slugs("positions", POSITION_SLUGS),
        "tribes": slugs("tribes", TRIBE_SLUGS),
    }
    return filters


def _filters_active(filters: dict) -> bool:
    return any(bool(value) for value in filters.values())


def _grindr_photo(person: dict) -> str | None:
    for key in ("profileImageMediaHash", "mediaHash", "primaryMediaHash"):
        value = person.get(key)
        if isinstance(value, str) and GRINDR_HASH_RE.match(value):
            return value
    url = person.get("primaryImageUrl")
    if isinstance(url, str):
        name = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
        if GRINDR_HASH_RE.match(name):
            return name
    hashes = person.get("photoMediaHashes")
    if isinstance(hashes, list):
        for value in hashes:
            if isinstance(value, str) and GRINDR_HASH_RE.match(value):
                return value
    return None


def _cards_from_cascade(data: dict) -> list[dict]:
    now_ms = time.time() * 1000
    cards = []
    seen: set[str] = set()
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind and "profile" not in kind:
            continue
        person = item.get("data") if isinstance(item.get("data"), dict) else item
        profile_id = person.get("profileId")
        if not isinstance(profile_id, (int, str)) or not str(profile_id).isdigit():
            continue
        card_id = "g:" + str(profile_id)
        if card_id in seen:
            continue
        seen.add(card_id)
        age = person.get("age")
        age = int(age) if isinstance(age, (int, float)) and 18 <= int(age) <= 120 else None
        meters = person.get("distanceMeters")
        if not isinstance(meters, (int, float)):
            meters = person.get("distance")
        distance = round(float(meters) / 1609.344, 1) if isinstance(meters, (int, float)) and 0 <= meters < 20_000_000 else None
        until = person.get("onlineUntil")
        online = isinstance(until, (int, float)) and until > now_ms
        name = person.get("displayName") if isinstance(person.get("displayName"), str) else ""
        traits = _grindr_traits(person)
        cards.append(
            {
                "id": card_id,
                "age": age,
                "distance": distance,
                "online": online,
                "photo": _grindr_photo(person),
                "name": name.strip()[:40],
                "source": "grindr",
                "heightCm": traits["heightCm"],
                "weightKg": traits["weightKg"],
                "body": traits["body"],
                "position": traits["position"],
                "tags": traits["tags"],
            }
        )
    return cards


def _slug_ids(slugs: list, table: dict[int, str]) -> list[int]:
    reverse = {slug: number for number, slug in table.items()}
    return [reverse[slug] for slug in slugs if slug in reverse]


def _grindr_cards(filters: dict | None = None) -> list[dict]:
    _refresh_grindr()
    digest = _nearby_geohash()
    params: dict[str, object] = {"nearbyGeoHash": digest, "pageNumber": 1}
    filters = filters or {}
    if filters.get("online"):
        params["onlineOnly"] = "true"
    if filters.get("photo"):
        params["photoOnly"] = "true"
    if filters.get("ageMin") is not None:
        params["ageMin"] = int(filters["ageMin"])
    if filters.get("ageMax") is not None:
        params["ageMax"] = int(filters["ageMax"])
    if filters.get("heightMin") is not None:
        params["heightCmMin"] = int(filters["heightMin"])
    if filters.get("heightMax") is not None:
        params["heightCmMax"] = int(filters["heightMax"])
    if filters.get("weightMin") is not None:
        params["weightGramsMin"] = int(round(float(filters["weightMin"]) * 1000))
    if filters.get("weightMax") is not None:
        params["weightGramsMax"] = int(round(float(filters["weightMax"]) * 1000))
    bodies = _slug_ids(filters.get("bodies") or [], BODY_SLUGS)
    positions = _slug_ids(filters.get("positions") or [], POSITION_SLUGS)
    tribes = _slug_ids(filters.get("tribes") or [], TRIBE_SLUGS)
    if bodies:
        params["bodyTypes"] = ",".join(str(item) for item in bodies)
    if positions:
        params["sexualPositions"] = ",".join(str(item) for item in positions)
    if tribes:
        params["tribes"] = ",".join(str(item) for item in tribes)
    path = "/v4/cascade?" + urllib.parse.urlencode(params)
    status, payload = _grindr_call("GET", path, session_id=_grindr["session_id"])
    if status == 401:
        _refresh_grindr()
        status, payload = _grindr_call("GET", path, session_id=_grindr["session_id"])
    if status != 200:
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Grindr returned an unreadable grid.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Grindr returned an unreadable grid.")
    _remember_grindr_geohash(digest)
    return _cards_from_cascade(data)


def fetch_grindr_photo(media_hash: str) -> tuple[bytes, str]:
    if not GRINDR_HASH_RE.match(media_hash):
        raise ApiError(404, "Not found.")
    urls = [
        f"https://cdns.grindr.com/images/thumb/320x320/{media_hash}",
        f"https://cdns.grindr.com/images/profile/1024x1024/{media_hash}",
    ]
    last = ApiError(404, "Photo unavailable.")
    for url in urls:
        try:
            return _fetch_image(url)
        except ApiError as exc:
            last = exc
    raise last


def fetch_grid(fresh: bool = False, filters: dict | None = None) -> dict:
    if not _sniffies_in() and not _grindr_in():
        raise ApiError(401, "Log in.")
    filters = filters or _empty_filters()
    active = _filters_active(filters)
    now = time.time()
    with _lock:
        cached = _grid_cache.get("payload")
        cached_at = float(_grid_cache.get("at") or 0)
    if not active and not fresh and cached and now - cached_at < GRID_TTL:
        return cached
    cards: list[dict] = []
    warnings: list[str] = []
    units = _units
    sniff_cards = None
    reused_sniff = False
    if not fresh and isinstance(cached, dict) and now - cached_at < GRID_TTL:
        sniff_cards = [card for card in (cached.get("cards") or []) if card.get("source") != "grindr"]
        units = cached.get("units") or units
        reused_sniff = True
    if _sniffies_in():
        if sniff_cards is None:
            try:
                sniff = _build_grid()
                sniff_cards = sniff.get("cards") or []
                units = sniff.get("units") or units
            except ApiError as exc:
                warnings.append(exc.message)
                sniff_cards = []
        cards.extend(sniff_cards)
    if _grindr_in():
        try:
            cards.extend(_grindr_cards(filters if active else None))
        except ApiError as exc:
            warnings.append(exc.message)
    cards.sort(key=lambda card: (card["distance"] is None, card["distance"] or 0))
    applied = {key: value for key, value in filters.items() if value} if active else {}
    payload = {"units": units, "cards": cards, "warnings": warnings, "applied": applied}
    if not active:
        with _lock:
            _grid_cache["at"] = time.time()
            _grid_cache["payload"] = payload
    elif _sniffies_in() and not reused_sniff:
        old_grindr = []
        if isinstance(cached, dict):
            old_grindr = [card for card in (cached.get("cards") or []) if card.get("source") == "grindr"]
        with _lock:
            _grid_cache["at"] = time.time()
            _grid_cache["payload"] = {
                "units": units,
                "cards": list(sniff_cards or []) + old_grindr,
                "warnings": [],
                "applied": {},
            }
    return payload


def _place_photo_ref(value: object) -> str | None:
    if not isinstance(value, str) or not value or ".." in value:
        return None
    if value.startswith("http"):
        parsed = urllib.parse.urlparse(value)
        if parsed.hostname != PLACE_HOST or parsed.scheme != "https":
            return None
        path = parsed.path.lstrip("/")
    else:
        path = value.lstrip("/")
    if not path or len(path) > 240:
        return None
    return path


def _place_photos(images: list, photo_path: object) -> list[str]:
    refs: list[str] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        kind = img.get("classification")
        if kind not in (None, "SFW", "NA"):
            continue
        ref = _place_photo_ref(img.get("path"))
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= 12:
            break
    extra = _place_photo_ref(photo_path)
    if extra and extra not in refs:
        refs.append(extra)
    return refs


def _format_address(addr: object) -> str:
    if isinstance(addr, str):
        return addr.strip()[:200]
    if not isinstance(addr, dict):
        return ""
    bits = []
    for key in ("road", "neighbourhood", "city", "county", "state"):
        value = addr.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in bits:
            bits.append(value.strip())
    return ", ".join(bits)[:200]


def _place_type_label(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        return "Place"
    return raw.replace("_", " ").strip().title()[:40]


def _normalize_places(feed: dict) -> dict:
    block = feed.get("places") if isinstance(feed.get("places"), dict) else {}
    visits = block.get("activeVisits") if isinstance(block.get("activeVisits"), list) else []
    counts: dict[str, int] = {}
    for visit in visits:
        if not isinstance(visit, dict):
            continue
        pid = visit.get("placeId")
        if isinstance(pid, str) and USER_ID_RE.match(pid):
            counts[pid] = counts.get(pid, 0) + 1
            if isinstance(visit.get("userId"), str) and USER_ID_RE.match(visit["userId"]):
                face = _face(visit["userId"])
                if face.get("photo"):
                    pass
    items = []
    for raw in block.get("places") or []:
        if not isinstance(raw, dict):
            continue
        place_id = raw.get("_id") or raw.get("id")
        if not isinstance(place_id, str) or not USER_ID_RE.match(place_id):
            continue
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        name = data.get("name") if isinstance(data.get("name"), str) else ""
        name = name.strip()[:80] or "Place"
        distance = raw.get("distance")
        distance = round(float(distance), 1) if isinstance(distance, (int, float)) and 0 <= distance < 20000 else None
        photos = _place_photos(data.get("images") or [], data.get("photoPath"))
        history = data.get("visits")
        history_n = len(history) if isinstance(history, list) else 0
        items.append(
            {
                "id": place_id,
                "name": name,
                "type": _place_type_label(data.get("type")),
                "distance": distance,
                "photo": photos[0] if photos else None,
                "visitors": counts.get(place_id, 0),
                "visits": history_n,
                "campus": bool(data.get("isCampus")),
                "darkroom": bool(data.get("hasDarkroom")),
            }
        )
    items.sort(key=lambda item: (-item["visitors"], item["distance"] is None, item["distance"] or 0))
    return {"places": items, "units": _units}


def fetch_places(fresh: bool = False) -> dict:
    now = time.time()
    cached = _places_cache.get("payload")
    cached_at = float(_places_cache.get("at") or 0)
    if not fresh and cached and now - cached_at < GRID_TTL:
        return cached
    _load_map_feed()
    return _places_cache.get("payload") or {"places": [], "units": _units}


def fetch_place(place_id: str) -> dict:
    if not USER_ID_RE.match(place_id):
        raise ApiError(400, "Unknown place.")
    status, payload, _ctype = api("GET", "/api/place/metadata", query={"placeId": place_id})
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Sniffies returned an unreadable place.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Sniffies returned an unreadable place.")
    place = data.get("place") if isinstance(data.get("place"), dict) else {}
    pdata = place.get("data") if isinstance(place.get("data"), dict) else {}
    photos = _place_photos(pdata.get("images") or [], pdata.get("photoPath"))
    listed = None
    cached = _places_cache.get("payload") if isinstance(_places_cache.get("payload"), dict) else None
    if cached:
        for item in cached.get("places") or []:
            if isinstance(item, dict) and item.get("id") == place_id:
                listed = item
                break
    people = []
    for visit in data.get("activeVisits") or []:
        if not isinstance(visit, dict):
            continue
        user_id = visit.get("userId")
        if not isinstance(user_id, str) or not USER_ID_RE.match(user_id):
            continue
        face = _face(user_id)
        convo_id = None
        for cid, other in _known_partners.items():
            if other == user_id:
                convo_id = cid
                break
        people.append(
            {
                "userId": user_id,
                "photo": face.get("photo"),
                "age": face.get("age"),
                "online": bool(face.get("online")),
                "arrivalTime": visit.get("arrivalTime") if isinstance(visit.get("arrivalTime"), str) else "",
                "conversationId": convo_id,
            }
        )
    me_id = me()
    messages = []
    for raw in data.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        author = raw.get("author")
        if not isinstance(author, str) or not USER_ID_RE.match(author):
            continue
        mid = raw.get("_id") or raw.get("id")
        if not isinstance(mid, str) or not USER_ID_RE.match(mid):
            continue
        body = raw.get("body") if isinstance(raw.get("body"), str) else ""
        if len(body) > 4000:
            body = body[:4000]
        face = _face(author)
        messages.append(
            {
                "id": mid,
                "author": author,
                "body": body,
                "createdAt": raw.get("createdAt") if isinstance(raw.get("createdAt"), str) else "",
                "mine": author == me_id,
                "photo": face.get("photo"),
                "age": face.get("age"),
            }
        )
    messages.sort(key=lambda item: item.get("createdAt") or "")
    summary = ""
    ai = pdata.get("aiSummary")
    if isinstance(ai, dict) and isinstance(ai.get("text"), str):
        summary = ai["text"].strip()[:800]
    description = pdata.get("description") if isinstance(pdata.get("description"), str) else ""
    description = description.strip()[:800]
    options = {}
    meta = pdata.get("metaOptions")
    if isinstance(meta, dict):
        for key, value in list(meta.items())[:20]:
            if isinstance(key, str) and isinstance(value, (str, bool, int)) and len(key) <= 40:
                options[key] = value
    history = pdata.get("visits")
    return {
        "id": place_id,
        "name": (pdata.get("name") if isinstance(pdata.get("name"), str) else "")[:80] or "Place",
        "type": _place_type_label(pdata.get("type")),
        "distance": listed.get("distance") if listed else None,
        "photo": photos[0] if photos else None,
        "photos": photos,
        "description": description,
        "summary": summary,
        "address": _format_address(pdata.get("address")),
        "campus": bool(pdata.get("isCampus")),
        "darkroom": bool(pdata.get("hasDarkroom")),
        "flagged": bool(pdata.get("isFlagged")),
        "visitors": len(people),
        "visits": len(history) if isinstance(history, list) else 0,
        "people": people,
        "messages": messages,
        "options": options,
    }


def send_place_message(place_id: str, text: str) -> dict:
    text = text.strip()
    if not USER_ID_RE.match(place_id):
        raise ApiError(400, "Unknown place.")
    if not text:
        raise ApiError(400, "Message is empty.")
    if len(text) > 2000:
        raise ApiError(400, "Message is too long.")
    status, payload, _ctype = api(
        "PUT",
        "/api/place/submit-message",
        {"placeId": place_id, "body": text, "parentPlaceMessageId": None},
    )
    if status not in (200, 201):
        raise ApiError(status, _error_message(payload, status))
    return fetch_place(place_id)


def fetch_inbox() -> dict:
    status, payload, _ctype = api("GET", "/api/v2/post-authentication/chat-data")
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    return normalize_inbox(json.loads(payload))


def _ms_iso(value: object) -> str:
    if not isinstance(value, (int, float)):
        return ""
    seconds = float(value) / 1000
    if seconds < 0 or seconds > 4_000_000_000:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _grindr_preview_text(preview: object) -> str:
    if not isinstance(preview, dict) or preview.get("unsent") is True:
        return ""
    text = preview.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()[:180]
    kind = str(preview.get("type") or "")
    return {
        "Image": "Photo",
        "ExpiringImage": "Photo",
        "Video": "Video",
        "PrivateVideo": "Video",
        "Audio": "Audio",
        "Location": "Location",
        "Giphy": "GIF",
        "Gaymoji": "Sticker",
        "Album": "Album",
        "ExpiringAlbum": "Album",
        "ExpiringAlbumV2": "Album",
    }.get(kind, "")


def _grindr_conversations() -> list[dict]:
    _refresh_grindr()
    body = {
        "unreadOnly": False,
        "chemistryOnly": False,
        "favoritesOnly": False,
        "rightNowOnly": False,
        "onlineNowOnly": False,
        "distanceMeters": None,
        "positions": [],
    }
    status, payload = _grindr_call("POST", "/v4/inbox?page=1", body, session_id=_grindr["session_id"])
    if status == 401:
        _refresh_grindr()
        status, payload = _grindr_call("POST", "/v4/inbox?page=1", body, session_id=_grindr["session_id"])
    if status != 200:
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Grindr returned an unreadable inbox.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Grindr returned an unreadable inbox.")
    me = str((_grindr or {}).get("profile_id") or "")
    now_ms = time.time() * 1000
    items = []
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        person = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        convo_id = person.get("conversationId")
        if not isinstance(convo_id, str) or not GRINDR_CONVO_RE.match(convo_id):
            continue
        other = ""
        photo = None
        online = False
        for part in person.get("participants") or []:
            if not isinstance(part, dict):
                continue
            profile_id = part.get("profileId")
            if not isinstance(profile_id, (int, str)) or not str(profile_id).isdigit():
                continue
            if str(profile_id) == me:
                continue
            other = str(profile_id)
            media = part.get("primaryMediaHash")
            if isinstance(media, str) and GRINDR_HASH_RE.match(media):
                photo = media
            until = part.get("onlineUntil")
            online = isinstance(until, (int, float)) and until > now_ms
            break
        if not other:
            continue
        preview = person.get("preview") if isinstance(person.get("preview"), dict) else {}
        sender = preview.get("senderId")
        name = person.get("name") if isinstance(person.get("name"), str) else ""
        unread = person.get("unreadCount")
        items.append(
            {
                "conversationId": convo_id,
                "userId": "g:" + other,
                "age": None,
                "name": name.strip()[:40],
                "photo": photo,
                "online": online,
                "unread": int(unread) if isinstance(unread, (int, float)) and unread >= 0 else 0,
                "preview": _grindr_preview_text(preview),
                "lastAt": _ms_iso(person.get("lastActivityTimestamp")),
                "mine": str(sender) == me if sender is not None else False,
                "source": "grindr",
                "theirReadId": "",
            }
        )
    return items


def fetch_chats() -> dict:
    items: list[dict] = []
    me_id = ""
    warnings: list[str] = []
    if _sniffies_in():
        try:
            sniff = fetch_inbox()
            me_id = str(sniff.get("me") or "")
            items.extend(sniff.get("conversations") or [])
        except ApiError as exc:
            warnings.append(exc.message)
    if _grindr_in():
        try:
            items.extend(_grindr_conversations())
        except ApiError as exc:
            warnings.append(exc.message)
    items.sort(key=lambda item: item.get("lastAt") or "", reverse=True)
    return {"me": me_id, "conversations": items, "warnings": warnings}


def _sniffies_reactions(raw: object, me_id: str) -> list[dict]:
    if not isinstance(raw, list):
        return []
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        emoji = entry.get("reaction")
        by = entry.get("byUserId")
        if emoji not in SNIFFIES_REACTIONS or not isinstance(by, str) or not USER_ID_RE.match(by):
            continue
        items.append({"by": by, "emoji": emoji, "mine": by == me_id})
    return items


def _cloudfront_host(host: str) -> bool:
    # Chat and drawer files are signed CloudFront URLs. The distribution label
    # changes, and the API tells us the full URL. One label only, so this is
    # not an open redirect onto some other suffix.
    suffix = ".cloudfront.net"
    if not host.endswith(suffix):
        return False
    label = host[: -len(suffix)]
    return bool(label) and re.fullmatch(r"[a-z0-9-]+", label) is not None


def _grindr_cdn_url(url: object) -> str | None:
    if not isinstance(url, str) or not url or len(url) > 4000:
        return None
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.port not in (None, 443):
        return None
    if host not in _GRINDR_CDN and not _cloudfront_host(host):
        return None
    return url


def _remember_grindr_file(url: object) -> str | None:
    clean = _grindr_cdn_url(url)
    if not clean:
        return None
    token = hashlib.sha256(clean.encode()).hexdigest()[:32]
    _grindr_files[token] = clean
    if len(_grindr_files) > 400:
        for key in list(_grindr_files)[:100]:
            _grindr_files.pop(key, None)
        _grindr_files[token] = clean
    return "/media/gfile/" + token


def _grindr_reactions(raw: object, me_id: str) -> list[dict]:
    if not isinstance(raw, list):
        return []
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        profile_id = entry.get("profileId")
        kind = entry.get("reactionType")
        if isinstance(profile_id, bool) or not isinstance(profile_id, int) or profile_id <= 0:
            continue
        if kind != 1:
            continue
        who = "g:" + str(profile_id)
        items.append({"by": who, "emoji": "fire", "mine": str(profile_id) == me_id})
    return items


def _grindr_message_text(raw: dict) -> str:
    if raw.get("unsent") is True:
        return ""
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    text = body.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()[:2000]
    return _grindr_preview_text({"type": raw.get("type"), "text": text})


def fetch_grindr_thread(conversation_id: str, before: str | None) -> dict:
    if not GRINDR_CONVO_RE.match(conversation_id):
        raise ApiError(400, "Unknown conversation.")
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    _refresh_grindr()
    path = "/v5/chat/conversation/" + urllib.parse.quote(conversation_id, safe=":") + "/message"
    if before:
        path += "?" + urllib.parse.urlencode({"pageKey": before})
    status, payload = _grindr_call("GET", path, session_id=_grindr["session_id"])
    if status == 401:
        _refresh_grindr()
        status, payload = _grindr_call("GET", path, session_id=_grindr["session_id"])
    if status != 200:
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Grindr returned an unreadable chat.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Grindr returned an unreadable chat.")
    me = str((_grindr or {}).get("profile_id") or "")
    messages = []
    for raw in data.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        message_id = raw.get("messageId")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 80:
            continue
        sender = raw.get("senderId")
        if isinstance(sender, bool):
            sender_id = ""
        elif isinstance(sender, (int, float)):
            sender_id = str(int(sender))
        elif isinstance(sender, str) and sender.isdigit():
            sender_id = sender
        else:
            sender_id = ""
        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        kind = str(raw.get("type") or "")
        image = None
        if kind in ("Image", "ExpiringImage") and raw.get("unsent") is not True:
            image = _remember_grindr_file(body.get("url"))
        text = None if image else _grindr_message_text(raw)
        messages.append(
            {
                "id": message_id,
                "body": text or None,
                "file": None,
                "image": image,
                "author": ("g:" + sender_id) if sender_id else "",
                "mine": bool(sender_id) and sender_id == me,
                "createdAt": _ms_iso(raw.get("timestamp")),
                "spam": False,
                "reactions": _grindr_reactions(raw.get("reactions"), me),
            }
        )
    messages.sort(key=lambda item: item.get("createdAt") or "")
    return {"messages": messages, "theirReadId": "", "source": "grindr"}


def fetch_more(earliest: str) -> dict:
    status, payload, _ctype = api(
        "POST",
        "/api/conversations/more",
        {"earliestLoadedConversationTimestamp": earliest},
    )
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    data = json.loads(payload)
    # Same conversation objects, partial users live under partialUsers.
    wrapped = {
        "conversationData": {"conversations": data.get("conversations") or []},
        "partialVisitorData": data.get("partialUsers") or data.get("partialVisitorData") or [],
    }
    return normalize_inbox(wrapped)


def release_spam_flag(conversation_id: str, force: bool = False) -> None:
    """Sniffies hides a system-flagged thread from the chat list.

    A reply from this app can set that flag. The website clears it with this
    call when you send from the browser, which is why the thread only came
    back after a browser message. 404 and 409 mean it was not flagged.
    """
    if not USER_ID_RE.match(conversation_id):
        return
    if not force and conversation_id in _unflagged:
        return
    status, payload, _ctype = api(
        "PUT",
        f"/api/v2/conversations/not-spam/{conversation_id}",
        {},
    )
    # 403: this account cannot clear the flag this way. Stop retrying so
    # every 6s thread poll does not serialize behind another PUT.
    if status in (200, 201, 204, 403, 404, 409):
        _unflagged.add(conversation_id)
        return
    print(f"spam flag clear returned {status}")


def fetch_thread(conversation_id: str, before: str | None) -> dict:
    if GRINDR_CONVO_RE.match(conversation_id):
        return fetch_grindr_thread(conversation_id, before)
    if not USER_ID_RE.match(conversation_id):
        raise ApiError(400, "Unknown conversation.")
    release_spam_flag(conversation_id)
    query = {
        "conversationId": conversation_id,
        "earliestLoadedMessageId": before or "null",
    }
    status, payload, _ctype = api("GET", "/api/messages", query=query)
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    data = normalize_messages(json.loads(payload))
    data["theirReadId"] = _partner_read.get(conversation_id)
    return data


def mark_read(conversation_id: str, user_id: str, message_id: str) -> None:
    if conversation_id not in _known_partners or _known_partners[conversation_id] != user_id:
        raise ApiError(400, "Open the conversation from your inbox first.")
    if not USER_ID_RE.match(message_id):
        return
    account = _partner_account.get(user_id) or "paid"
    api(
        "PUT",
        "/api/message/read",
        {
            "conversationId": conversation_id,
            "userId": user_id,
            "accountType": account,
            "lastReadMessageId": message_id,
        },
    )


def start_conversation(user_id: str, text: str) -> dict:
    if not USER_ID_RE.match(user_id):
        raise ApiError(400, "Unknown profile.")
    for cid, other in _known_partners.items():
        if other == user_id:
            return send_reply(cid, user_id, text)
    status, payload, _ctype = api(
        "POST",
        "/api/conversation/new",
        {
            "userIdTo": user_id,
            "global": False,
            "conversationId": None,
            "file": None,
            "body": text,
            "forcePhoneMessage": False,
        },
    )
    if status not in (200, 201):
        raise ApiError(status, _error_message(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}
    conv = data.get("conversation") if isinstance(data.get("conversation"), dict) else {}
    cid = conv.get("id") or conv.get("_id") or data.get("conversationId")
    if isinstance(cid, str) and USER_ID_RE.match(cid):
        _known_partners[cid] = user_id
        _unflagged.discard(cid)
        release_spam_flag(cid, force=True)
    return {
        "ok": True,
        "conversationId": cid,
        "message": data.get("submittedMessage"),
    }


def _grindr_other_id(user_id: str, conversation_id: str) -> str:
    if user_id.startswith("g:"):
        other = user_id[2:]
        if other.isdigit():
            return other
    if GRINDR_CONVO_RE.match(conversation_id):
        me = str((_grindr or {}).get("profile_id") or "")
        left, right = conversation_id.split(":", 1)
        if me and left == me:
            return right
        if me and right == me:
            return left
    return ""


def send_grindr_reply(conversation_id: str, user_id: str, text: str) -> dict:
    text = text.strip()
    if not text:
        raise ApiError(400, "Message is empty.")
    if len(text) > 2000:
        raise ApiError(400, "Message is too long.")
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    other = _grindr_other_id(user_id, conversation_id)
    if not other.isdigit():
        raise ApiError(400, "Unknown profile.")
    _refresh_grindr()
    body = {
        "type": "Text",
        "target": {"type": "Direct", "targetId": int(other)},
        "body": {"text": text},
    }
    status, payload = _grindr_call("POST", "/v4/chat/message/send", body, session_id=_grindr["session_id"])
    if status == 401:
        _refresh_grindr()
        status, payload = _grindr_call("POST", "/v4/chat/message/send", body, session_id=_grindr["session_id"])
    if status == 403:
        raise ApiError(403, "Grindr will not deliver a message to that profile.")
    if status not in (200, 201):
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}
    cid = data.get("conversationId") if isinstance(data, dict) else None
    if not isinstance(cid, str) or not GRINDR_CONVO_RE.match(cid):
        me = str((_grindr or {}).get("profile_id") or "")
        if me.isdigit():
            low, high = sorted((int(me), int(other)))
            cid = f"{low}:{high}"
        else:
            cid = conversation_id
    return {"ok": True, "conversationId": cid, "source": "grindr"}


def _is_grindr_chat(conversation_id: str, user_id: str) -> bool:
    return user_id.startswith("g:") or bool(GRINDR_CONVO_RE.match(conversation_id or ""))


def _grindr_authed(method: str, path: str, body: dict | None = None, *, raw: bytes | None = None, content_type: str | None = None) -> tuple[int, bytes]:
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    _refresh_grindr()
    status, payload = _grindr_call(
        method, path, body, session_id=_grindr["session_id"], raw=raw, content_type=content_type
    )
    if status == 401:
        _refresh_grindr()
        status, payload = _grindr_call(
            method, path, body, session_id=_grindr["session_id"], raw=raw, content_type=content_type
        )
    return status, payload


def _send_grindr_image(conversation_id: str, user_id: str, media_id: int) -> dict:
    if not isinstance(media_id, int) or isinstance(media_id, bool) or media_id <= 0:
        raise ApiError(400, "Unknown photo.")
    other = _grindr_other_id(user_id, conversation_id)
    if not other.isdigit():
        raise ApiError(400, "Unknown profile.")
    body = {
        "type": "Image",
        "target": {"type": "Direct", "targetId": int(other)},
        "body": {"mediaId": media_id},
    }
    status, payload = _grindr_authed("POST", "/v4/chat/message/send", body)
    if status == 403:
        raise ApiError(403, "Grindr will not deliver a message to that profile.")
    if status not in (200, 201):
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}
    cid = data.get("conversationId") if isinstance(data, dict) else None
    if not isinstance(cid, str) or not GRINDR_CONVO_RE.match(cid):
        me_id = str((_grindr or {}).get("profile_id") or "")
        if me_id.isdigit():
            low, high = sorted((int(me_id), int(other)))
            cid = f"{low}:{high}"
        else:
            cid = conversation_id
    return {"ok": True, "conversationId": cid, "source": "grindr"}


def _upload_grindr_chat_photo(data: bytes, content_type: str) -> int:
    status, payload = _grindr_authed(
        "POST",
        "/v5/chat/media/upload?takenOnGrindr=false",
        raw=data,
        content_type=content_type,
    )
    if status != 200:
        raise ApiError(status if status else 502, _grindr_error(payload, status) if payload else "Photo upload was not accepted.")
    try:
        meta = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Photo upload was not accepted.") from exc
    media_id = meta.get("mediaId") if isinstance(meta, dict) else None
    if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id <= 0:
        raise ApiError(502, "Photo upload was not accepted.")
    put_status, _put_payload = _grindr_authed("PUT", f"/v4/chat/media/drawer/{media_id}")
    if put_status not in (200, 201, 204, 500):
        pass
    clean = _grindr_cdn_url(meta.get("url") if isinstance(meta, dict) else None)
    if clean:
        _grindr_drawer[media_id] = clean
    return media_id


def _load_grindr_drawer() -> None:
    status, payload = _grindr_authed("GET", "/v4/chat/media/drawer")
    if status != 200:
        return
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return
    items = data if isinstance(data, list) else None
    if items is None and isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                items = value
                break
    if not isinstance(items, list):
        return
    found: dict[int, str] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        media_id = raw.get("id")
        if isinstance(media_id, str) and media_id.isdigit():
            media_id = int(media_id)
        clean = _grindr_cdn_url(raw.get("url"))
        if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id <= 0 or not clean:
            continue
        ctype = str(raw.get("contentType") or "").split(";", 1)[0].strip().lower()
        if ctype.startswith("video/") or ctype.startswith("audio/"):
            continue
        if ctype and not ctype.startswith("image/"):
            continue
        if not ctype:
            path = urllib.parse.urlparse(clean).path.lower()
            if not path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                continue
        found[media_id] = clean
    _grindr_drawer.clear()
    _grindr_drawer.update(found)


def _positive_media_id(value: object) -> int | None:
    # Drawer ids are 64-bit. The page sends them as strings so JavaScript
    # does not round them. A plain JSON number is still accepted.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        return None
    if parsed <= 0:
        return None
    return parsed


def _grindr_drawer_photos() -> list[dict]:
    try:
        _load_grindr_drawer()
    except ApiError:
        return []
    photos = []
    for media_id, url in _grindr_drawer.items():
        image = _remember_grindr_file(url)
        if image:
            photos.append({"id": str(media_id), "image": image})
    return photos


def _grindr_drawer_bytes(media_id: int) -> tuple[bytes, str]:
    url = _grindr_drawer.get(media_id)
    if not url:
        _load_grindr_drawer()
        url = _grindr_drawer.get(media_id)
    if not url:
        raise ApiError(400, "Unknown photo.")
    return _fetch_image(url, redirects=False)


def react_to_message(conversation_id: str, user_id: str, message_id: str, reaction: str) -> dict:
    reaction = reaction.strip().lower()
    if _is_grindr_chat(conversation_id, user_id):
        if reaction != "fire":
            raise ApiError(400, "Grindr reactions use fire.")
        if not GRINDR_CONVO_RE.match(conversation_id):
            raise ApiError(400, "Open the chat before reacting.")
        if not GRINDR_MESSAGE_RE.match(message_id):
            raise ApiError(400, "Unknown message.")
        status, payload = _grindr_authed(
            "POST",
            "/v4/chat/message/reaction",
            {"conversationId": conversation_id, "messageId": message_id, "reactionType": 1},
        )
        if status == 500:
            raise ApiError(404, "That message is not in the chat.")
        if status not in (200, 201, 204):
            raise ApiError(status if status else 502, _grindr_error(payload, status))
        return {"ok": True}
    if conversation_id not in _known_partners or _known_partners[conversation_id] != user_id:
        raise ApiError(400, "That conversation is not in your inbox.")
    if not USER_ID_RE.match(message_id):
        raise ApiError(400, "Unknown message.")
    if not USER_ID_RE.match(user_id):
        raise ApiError(400, "Unknown profile.")
    body = {
        "conversationId": conversation_id,
        "conversationLocation": None,
        "messageId": message_id,
        "userIdTo": user_id,
    }
    if reaction in ("", "remove"):
        path = "/api/message/reaction/remove"
    elif reaction in SNIFFIES_REACTIONS:
        path = "/api/message/reaction/add"
        body["reaction"] = reaction
    else:
        raise ApiError(400, "Unknown reaction.")
    status, payload, _ctype = api("POST", path, body)
    if status not in (200, 201, 204):
        raise ApiError(status, _error_message(payload, status))
    return {"ok": True}


def send_reply(conversation_id: str, user_id: str, text: str) -> dict:
    if user_id.startswith("g:") or GRINDR_CONVO_RE.match(conversation_id or ""):
        return send_grindr_reply(conversation_id, user_id, text)
    text = text.strip()
    if not text:
        raise ApiError(400, "Message is empty.")
    if len(text) > 2000:
        raise ApiError(400, "Message is too long.")
    if not conversation_id:
        return start_conversation(user_id, text)
    if conversation_id not in _known_partners or _known_partners[conversation_id] != user_id:
        raise ApiError(400, "That conversation is not in your inbox.")
    status, payload, _ctype = api(
        "POST",
        "/api/conversation/newMessage",
        {
            "conversationId": conversation_id,
            "chatObj": {
                "userIdTo": user_id,
                "global": False,
                "conversationId": conversation_id,
                "file": None,
                "body": text,
                "forcePhoneMessage": False,
            },
        },
    )
    if status not in (200, 201):
        raise ApiError(status, _error_message(payload, status))
    # A send can flag the thread. Clear it so Sniffies keeps the chat
    # and shows these messages in its own chat log.
    _unflagged.discard(conversation_id)
    release_spam_flag(conversation_id, force=True)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}
    return {"ok": True, "message": data.get("submittedMessage")}


def _ensure_location() -> list[float]:
    point = _sniffies_coords()
    if point:
        lat, lng = point
        return [lng, lat]
    status, payload, _ctype = api("POST", "/api/authenticate/current", {})
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    with _lock:
        _remember_account(payload)
    point = _sniffies_coords()
    if not point:
        raise ApiError(502, "Your profile has no location yet.")
    lat, lng = point
    return [lng, lat]


def _load_global_raw() -> list[dict]:
    lng, lat = _ensure_location()
    status, payload, _ctype = api(
        "POST",
        "/api/global-message/fetch",
        {"lng": lng, "lat": lat, "timeThreshold": False},
    )
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Sniffies returned an unreadable community chat.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Sniffies returned an unreadable community chat.")
    messages: list[dict] = []
    for key in ("messages", "otherMessages"):
        batch = data.get(key) or []
        if isinstance(batch, list):
            messages.extend(item for item in batch if isinstance(item, dict))
    return messages


def _chat_file(value: object, author: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    name = value
    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        if parsed.hostname != "chat.sniffiesassets.com":
            return None
        name = parsed.path
    name = name.strip().strip("/")
    if "/" in name:
        prefix, name = name.split("/", 1)
        if prefix != author:
            return None
    return name if FILE_RE.match(name) else None


def fetch_community() -> dict:
    user_id = me()
    items = []
    for msg in _load_global_raw():
        author = msg.get("author")
        if not isinstance(author, str) or not USER_ID_RE.match(author):
            continue
        message_id = msg.get("_id") or msg.get("id")
        if not isinstance(message_id, str) or not USER_ID_RE.match(message_id):
            continue
        deleted = bool(msg.get("isDeleted"))
        body = msg.get("body") if isinstance(msg.get("body"), str) else ""
        if len(body) > 4000:
            body = body[:4000]
        created = msg.get("createdAt") if isinstance(msg.get("createdAt"), str) else ""
        face = _face(author)
        items.append(
            {
                "id": message_id,
                "author": author,
                "mine": author == user_id,
                "body": "" if deleted else body,
                "file": None if deleted else _chat_file(msg.get("file"), author),
                "createdAt": created,
                "deleted": deleted,
                "photo": face.get("photo"),
                "age": face.get("age"),
                "online": bool(face.get("online")),
            }
        )
    items.sort(key=lambda item: item.get("createdAt") or "")
    return {"messages": items}


def _message_coordinates(message: dict) -> list[float] | None:
    loc = message.get("location")
    coords = loc.get("coordinates") if isinstance(loc, dict) else None
    if (
        not isinstance(coords, list)
        or len(coords) < 2
        or not all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in coords[:2])
    ):
        return None
    lng, lat = float(coords[0]), float(coords[1])
    if -180 <= lng <= 180 and -90 <= lat <= 90:
        return [lng, lat]
    return None


def _community_limit() -> int:
    # The site allows 8 visible community posts for a paid account and 1 otherwise.
    return 8 if _account_type in _MEMBER_TYPES else 1


def _trim_own_global() -> None:
    """Drop the oldest of your own community posts when a new one would pass the cap.

    Sniffies asks before it does this. Sending from here accepts that, so a
    full slot does not turn into a failed post.
    """
    user_id = me()
    limit = _community_limit()
    own = []
    for msg in _load_global_raw():
        if msg.get("author") != user_id or msg.get("isDeleted"):
            continue
        chat_id = msg.get("_id") or msg.get("id")
        if isinstance(chat_id, str) and USER_ID_RE.match(chat_id):
            own.append(msg)
    own.sort(key=lambda msg: str(msg.get("createdAt") or ""))
    while len(own) >= limit:
        oldest = own.pop(0)
        chat_id = oldest.get("_id") or oldest.get("id")
        coords = _message_coordinates(oldest) or _ensure_location()
        status, _payload, _ctype = api(
            "PUT",
            "/api/global-message/delete",
            {"chatId": chat_id, "coordinates": coords},
        )
        print(f"community delete {status}")
        if status not in (200, 201, 204):
            raise ApiError(
                409,
                "Community chat is full. Remove an older post on Sniffies, then try again.",
            )


def _save_global(body: str, file_url: str | None) -> None:
    _trim_own_global()
    lng, lat = _ensure_location()
    status, payload, _ctype = api(
        "POST",
        "/api/global-message/save",
        {
            "chatObj": {
                "userIdTo": None,
                "global": True,
                "conversationId": "global",
                "coordinates": [lng, lat],
                "file": file_url,
                "body": body,
            }
        },
    )
    if status not in (200, 201):
        raise ApiError(status, _global_error(payload, status))


def send_community(text: str) -> dict:
    text = text.strip()
    if not text:
        raise ApiError(400, "Message is empty.")
    if len(text) > 2000:
        raise ApiError(400, "Message is too long.")
    _save_global(text, None)
    return {"ok": True}


def _global_error(payload: bytes, status: int) -> str:
    try:
        data = json.loads(payload)
    except Exception:
        data = None
    if isinstance(data, dict):
        if data.get("error") == "phone-detected":
            return "That looks like a phone number, which Sniffies blocks."
        if data.get("label") or data.get("shortReasonKey"):
            return "Sniffies rejected that community post."
        if data.get("hasNotAcceptedPolicies"):
            return "Accept the current Sniffies policies in the browser, then try again."
    if status in (401, 403):
        return "Session expired. Log in again."
    return f"Sniffies returned {status}."


def _image_type(data: bytes, declared: str) -> str:
    declared = declared.split(";", 1)[0].strip().lower()
    if declared == "image/jpeg" and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if declared == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if (
        declared == "image/webp"
        and len(data) >= 12
        and data[:4] == b"RIFF"
        and data[8:12] == b"WEBP"
    ):
        return "image/webp"
    raise ApiError(400, "Use a JPEG, PNG, or WebP photo.")


def _put_chat_photo(url: str, data: bytes, content_type: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != CHAT_PHOTO_HOST:
        raise ApiError(502, "Photo upload was not accepted.")
    blob = tempfile.NamedTemporaryFile(prefix="sniff-img-", delete=False)
    try:
        blob.write(data)
        blob.close()
        os.chmod(blob.name, 0o600)
        proc = subprocess.run(
            [
                "curl",
                "--http1.1",
                "--silent",
                "--max-time",
                "60",
                "--max-redirs",
                "0",
                "-X",
                "PUT",
                "-H",
                f"Content-Type: {content_type}",
                "-H",
                "Expect:",
                "--data-binary",
                f"@{blob.name}",
                "-o",
                os.devnull,
                "-w",
                "%{http_code}",
                "--",
                url,
            ],
            capture_output=True,
        )
        status_text = proc.stdout.decode().strip() or "0"
        status = int(status_text) if status_text.isdigit() else 0
        if status not in (200, 201, 204):
            raise ApiError(502, "Photo upload failed.")
    finally:
        try:
            os.unlink(blob.name)
        except OSError:
            pass


def _upload_chat_photo(data: bytes, declared: str) -> str:
    content_type = _image_type(data, declared)
    ext = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp"}[content_type]
    filename = secrets.token_hex(16) + "-chat-pic." + ext
    status, payload, _ctype = api(
        "POST",
        "/api/upload-signature",
        {"fileName": filename, "photoGroup": "chat"},
    )
    if status != 200:
        raise ApiError(status, "Photo upload was not accepted.")
    try:
        meta = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Photo upload was not accepted.") from exc
    url = meta.get("url") if isinstance(meta, dict) else None
    key = meta.get("key") if isinstance(meta, dict) else None
    if not isinstance(url, str) or not isinstance(key, str) or not key.endswith("/" + filename):
        raise ApiError(502, "Photo upload was not accepted.")
    _put_chat_photo(url, data, content_type)
    return filename


def send_chat_photo(conversation_id: str, user_id: str, data: bytes, declared: str) -> dict:
    if conversation_id not in _known_partners or _known_partners[conversation_id] != user_id:
        raise ApiError(400, "That conversation is not in your inbox.")
    filename = _upload_chat_photo(data, declared)
    status, payload, _ctype = api(
        "POST",
        "/api/conversation/newMessage",
        {
            "conversationId": conversation_id,
            "chatObj": {
                "userIdTo": user_id,
                "global": False,
                "conversationId": conversation_id,
                "file": filename,
                "body": "",
                "forcePhoneMessage": False,
            },
        },
    )
    if status not in (200, 201):
        raise ApiError(status, _error_message(payload, status))
    _unflagged.discard(conversation_id)
    release_spam_flag(conversation_id, force=True)
    _note_chat_photo(filename)
    return {"ok": True}


def send_saved_photo(conversation_id: str, user_id: str, filename: str) -> dict:
    if not FILE_RE.match(filename):
        raise ApiError(400, "Unknown photo.")
    if conversation_id not in _known_partners or _known_partners[conversation_id] != user_id:
        raise ApiError(400, "That conversation is not in your inbox.")
    status, payload, _ctype = api(
        "POST",
        "/api/conversation/newMessage",
        {
            "conversationId": conversation_id,
            "chatObj": {
                "userIdTo": user_id,
                "global": False,
                "conversationId": conversation_id,
                "file": filename,
                "body": "",
                "forcePhoneMessage": False,
            },
        },
    )
    if status not in (200, 201):
        raise ApiError(status, _error_message(payload, status))
    _unflagged.discard(conversation_id)
    release_spam_flag(conversation_id, force=True)
    _note_chat_photo(filename)
    return {"ok": True}


def send_community_photo(data: bytes, declared: str, caption: str) -> dict:
    caption = caption.strip()
    if len(caption) > 2000:
        raise ApiError(400, "Message is too long.")
    filename = _upload_chat_photo(data, declared)
    # Community posts store the chat CDN URL. Private messages store the short name.
    file_url = f"https://chat.sniffiesassets.com/{me()}/{filename}"
    _save_global(caption, file_url)
    _note_chat_photo(filename)
    return {"ok": True}


def send_saved_community_photo(filename: str, caption: str) -> dict:
    caption = caption.strip()
    if len(caption) > 2000:
        raise ApiError(400, "Message is too long.")
    if not FILE_RE.match(filename):
        raise ApiError(400, "Unknown photo.")
    file_url = f"https://chat.sniffiesassets.com/{me()}/{filename}"
    _save_global(caption, file_url)
    _note_chat_photo(filename)
    return {"ok": True}


def fetch_photos() -> dict:
    photos: list[str] = []
    owner = ""
    if _sniffies_in():
        if not _chat_photos:
            status, payload, _ctype = api("POST", "/api/authenticate/current", {})
            if status == 200:
                with _lock:
                    _remember_account(payload)
        try:
            owner = me()
        except ApiError:
            owner = ""
        photos = list(_chat_photos)
    grindr = _grindr_drawer_photos() if _grindr_in() else []
    return {"me": owner, "photos": photos, "grindr": grindr}


def send_chosen_photo(conversation_id: str, user_id: str, filename: str, media_id: object) -> dict:
    chosen = _positive_media_id(media_id)
    if _is_grindr_chat(conversation_id, user_id):
        if chosen is not None:
            return _send_grindr_image(conversation_id, user_id, chosen)
        if not FILE_RE.match(filename) or filename not in _chat_photos:
            raise ApiError(400, "Unknown photo.")
        data, content_type = fetch_media("chat", me(), filename)
        uploaded = _upload_grindr_chat_photo(data, content_type)
        return _send_grindr_image(conversation_id, user_id, uploaded)
    if chosen is not None:
        data, content_type = _grindr_drawer_bytes(chosen)
        return send_chat_photo(conversation_id, user_id, data, content_type)
    return send_saved_photo(conversation_id, user_id, filename)


def fetch_profile(user_id: str) -> dict:
    if user_id.startswith("g:"):
        cached = _grid_cache.get("payload") if isinstance(_grid_cache.get("payload"), dict) else None
        for card in (cached or {}).get("cards") or []:
            if isinstance(card, dict) and card.get("id") == user_id:
                photo = card.get("photo")
                return {
                    "id": user_id,
                    "age": card.get("age"),
                    "distance": card.get("distance"),
                    "online": bool(card.get("online")),
                    "photo": photo,
                    "photos": [photo] if photo else [],
                    "name": card.get("name") or "",
                    "blurb": "",
                    "source": "grindr",
                }
        raise ApiError(404, "Open that profile from the grid again.")
    if not USER_ID_RE.match(user_id):
        raise ApiError(400, "Unknown profile.")
    status, payload, _ctype = api("POST", "/api/user/full", {"userId": user_id})
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Sniffies returned an unreadable profile.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Sniffies returned an unreadable profile.")
    node = data.get("user") if isinstance(data.get("user"), dict) else data
    person = node.get("data") if isinstance(node.get("data"), dict) else {}
    profile = person.get("profile") if isinstance(person.get("profile"), dict) else {}
    stats = ((profile.get("extended") or {}).get("stats") or {}) if isinstance(profile.get("extended"), dict) else {}
    age = stats.get("age")
    age = int(age) if isinstance(age, (int, float)) and 18 <= age <= 120 else None
    blurb = profile.get("blurb") if isinstance(profile.get("blurb"), str) else ""
    if profile.get("blurbClassification") == "NSFW":
        blurb = ""
    if len(blurb) > 400:
        blurb = blurb[:400]
    conversation_id = None
    for cid, other in _known_partners.items():
        if other == user_id:
            conversation_id = cid
            break
    distance = None
    cached = _grid_cache.get("payload") if isinstance(_grid_cache.get("payload"), dict) else None
    if cached:
        for card in cached.get("cards") or []:
            if isinstance(card, dict) and card.get("id") == user_id:
                distance = card.get("distance")
                break
    photos = _sfw_photos(profile.get("images") or [])
    loc = profile.get("location") if isinstance(profile.get("location"), dict) else {}
    if not loc and isinstance(person.get("location"), dict):
        loc = person["location"]
    city = str(
        loc.get("name") if isinstance(loc.get("name"), str) else
        (loc.get("city") if isinstance(loc.get("city"), str) else
         (profile.get("locationName") if isinstance(profile.get("locationName"), str) else
          (person.get("locationName") if isinstance(person.get("locationName"), str) else
           (person.get("city") if isinstance(person.get("city"), str) else ""))))
    ).strip()
    borough = str(
        loc.get("borough") if isinstance(loc.get("borough"), str) else
        (loc.get("neighbourhood") if isinstance(loc.get("neighbourhood"), str) else
         (profile.get("borough") if isinstance(profile.get("borough"), str) else
          (person.get("borough") if isinstance(person.get("borough"), str) else "")))
    ).strip()
    location_label = ""
    if borough and city:
        location_label = f"{borough}, {city}"
    elif city:
        location_label = city
    elif borough:
        location_label = borough
    return {
        "id": user_id,
        "age": age,
        "photo": photos[0] if photos else None,
        "photos": photos,
        "online": _online(person),
        "blurb": blurb,
        "conversationId": conversation_id,
        "distance": distance,
        "city": city,
        "borough": borough,
        "locationLabel": location_label,
        "source": "sniffies",
    }


def fetch_profiles_batch(user_ids: list[str]) -> dict:
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for raw in user_ids[:50]:
        uid = str(raw or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        try:
            out[uid] = fetch_profile(uid)
        except ApiError as exc:
            out[uid] = {"id": uid, "error": exc.message}
    return {"profiles": out}

def _error_message(payload: bytes, status: int) -> str:
    try:
        data = json.loads(payload)
    except Exception:
        data = None
    if isinstance(data, dict):
        for key in ("message", "error", "alert"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value[:240]
    if status == 401 or status == 403:
        return "Session expired. Log in again."
    return f"Sniffies returned {status}."


# ---------------------------------------------------------------------------
# Me / profile editing
# ---------------------------------------------------------------------------

# Editable fields shared between GET and PUT /v3.1/me/profile.
_GRINDR_EDITABLE = {
    "displayName",
    "aboutMe",
    "age",
    "showAge",
    "showDistance",
    "showPosition",
    "showTribes",
    "approximateDistance",
    "showOnMap",
    "nsfw",
    "bodyType",
    "ethnicity",
    "hivStatus",
    "lastTestedDate",
    "relationshipStatus",
    "sexualPosition",
    "height",
    "weight",
    "genders",
    "pronouns",
    "grindrTribes",
    "tribesImInto",
    "lookingFor",
    "meetAt",
    "vaccines",
    "sexualHealth",
    "profileTags",
    "socialNetworks",
}

# Fields that PATCH /v4/me/profile actually applies (structured fields only).
_GRINDR_PATCHABLE = {
    "aboutMe",  # accepted but ignored; kept for compatibility
    "nsfw",
    "bodyType",
    "displayName",  # accepted but ignored
    "ethnicity",
    "genders",
    "height",
    "hivStatus",
    "lastTestedDate",
    "lookingFor",
    "meetAt",
    "grindrTribes",
    "profileTags",
    "pronouns",
    "relationshipStatus",
    "sexualHealth",
    "sexualPosition",
    "showAge",
    "showDistance",
    "showPosition",
    "showTribes",
    "socialNetworks",
    "tribesImInto",
    "vaccines",
    "weight",
}


def _normalize_grindr_profile(person: dict) -> dict:
    traits = _grindr_traits(person)
    social = person.get("socialNetworks") if isinstance(person.get("socialNetworks"), dict) else {}
    return {
        "profileId": person.get("profileId"),
        "displayName": person.get("displayName"),
        "aboutMe": person.get("aboutMe"),
        "age": person.get("age"),
        "showAge": bool(person.get("showAge")),
        "showDistance": bool(person.get("showDistance")),
        "showPosition": bool(person.get("showPosition")),
        "showTribes": bool(person.get("showTribes")),
        "approximateDistance": bool(person.get("approximateDistance")),
        "showOnMap": bool(person.get("showOnMap")),
        "nsfw": person.get("nsfw"),
        "bodyType": person.get("bodyType"),
        "ethnicity": person.get("ethnicity"),
        "hivStatus": person.get("hivStatus"),
        "lastTestedDate": person.get("lastTestedDate"),
        "relationshipStatus": person.get("relationshipStatus"),
        "sexualPosition": person.get("sexualPosition"),
        "height": person.get("height"),
        "weight": person.get("weight"),
        "weightKg": traits.get("weightKg"),
        "genders": list(person.get("genders") or []),
        "pronouns": list(person.get("pronouns") or []),
        "grindrTribes": list(person.get("grindrTribes") or person.get("tribes") or []),
        "tribesImInto": list(person.get("tribesImInto") or []),
        "lookingFor": list(person.get("lookingFor") or []),
        "meetAt": list(person.get("meetAt") or []),
        "vaccines": list(person.get("vaccines") or []),
        "sexualHealth": list(person.get("sexualHealth") or []),
        "profileTags": list(person.get("profileTags") or []),
        "socialNetworks": {
            "twitter": {"userId": (social.get("twitter") or {}).get("userId")},
            "facebook": {"userId": (social.get("facebook") or {}).get("userId")},
            "instagram": {"userId": (social.get("instagram") or {}).get("userId")},
        },
        "traits": traits,
    }


def fetch_grindr_me_profile() -> dict:
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    status, payload = _grindr_authed("GET", "/v4/me/profile")
    if status != 200:
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Grindr returned an unreadable profile.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Grindr returned an unreadable profile.")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ApiError(502, "Grindr returned an unreadable profile.")
    person = profiles[0]
    if not isinstance(person, dict):
        raise ApiError(502, "Grindr returned an unreadable profile.")
    me_id = str((_grindr or {}).get("profile_id") or "")
    return {
        "ok": True,
        "source": "grindr",
        "profileId": me_id,
        "profile": _normalize_grindr_profile(person),
    }


def _grindr_banned_terms_message(payload: bytes) -> str | None:
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("type") != "urn:gr:err:hit_banned_terms":
        return None
    fields = []
    for key in ("display_name", "about_me", "gender_display", "pronouns_display"):
        block = data.get(key)
        if isinstance(block, dict) and isinstance(block.get("terms"), list):
            terms = [str(t) for t in block["terms"]]
            if terms:
                fields.append(f"{key}: {', '.join(terms)}")
    if fields:
        return "Profile text was blocked: " + "; ".join(fields)
    return "Profile text was blocked."


def patch_grindr_profile(patch: dict) -> dict:
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    cleaned = {k: v for k, v in patch.items() if k in _GRINDR_PATCHABLE}
    if not cleaned:
        raise ApiError(400, "Nothing to update.")
    status, payload = _grindr_authed("PATCH", "/v4/me/profile", cleaned)
    if status == 400:
        banned = _grindr_banned_terms_message(payload)
        if banned:
            raise ApiError(400, banned)
    if status not in (200, 201, 204):
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    return {"ok": True}


def _merge_grindr_full_profile(person: dict, patch: dict) -> dict:
    full: dict = {k: person.get(k) for k in _GRINDR_EDITABLE if k in person}
    # Ensure structured array fields exist as arrays when the source omits them,
    # so the replacement body is complete.
    for key in ("genders", "pronouns", "grindrTribes", "tribesImInto", "lookingFor",
                "meetAt", "vaccines", "sexualHealth", "profileTags"):
        if key not in full or full[key] is None:
            full[key] = []
    if "socialNetworks" not in full or not isinstance(full.get("socialNetworks"), dict):
        full["socialNetworks"] = {}
    for net in ("twitter", "facebook", "instagram"):
        if not isinstance(full["socialNetworks"].get(net), dict):
            full["socialNetworks"][net] = {}

    for key, value in patch.items():
        if key == "weightKg":
            if value is None:
                full["weight"] = None
            else:
                num = _bounded_number(value, 40, 320)
                if num is None:
                    raise ApiError(400, "Weight looks wrong.")
                full["weight"] = int(round(num * 1000))
        elif key in _GRINDR_EDITABLE:
            full[key] = value

    # Prune empty strings to null for moderated free-text fields.
    for key in ("displayName", "aboutMe"):
        if full.get(key) == "":
            full[key] = None
        if full.get(key) is not None:
            full[key] = str(full[key])[:200]

    # Age is server-derived; never let the client change it.
    if "age" in person:
        full["age"] = person["age"]

    return full


def update_grindr_profile_full(patch: dict) -> dict:
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    # We need the current profile to fill in every required field.
    status, payload = _grindr_authed("GET", "/v4/me/profile")
    if status != 200:
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Grindr returned an unreadable profile.") from exc
    profiles = data.get("profiles") if isinstance(data, dict) else None
    person = profiles[0] if isinstance(profiles, list) and profiles else None
    if not isinstance(person, dict):
        raise ApiError(502, "Grindr returned an unreadable profile.")
    full = _merge_grindr_full_profile(person, patch)
    status, payload = _grindr_authed("PUT", "/v3.1/me/profile", full)
    if status == 400:
        banned = _grindr_banned_terms_message(payload)
        if banned:
            raise ApiError(400, banned)
    if status not in (200, 201, 204):
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    return {"ok": True}


def _profile_thumb_coords(data: bytes) -> str:
    try:
        from PIL import Image
    except Exception as exc:
        raise ApiError(500, "Photo processing is unavailable. Install Pillow.") from exc
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    side = min(w, h)
    x1 = (w - side) / 2
    y1 = (h - side) / 2
    x2 = x1 + side
    y2 = y1 + side
    return f"{y2:.1f},{x1:.1f},{x2:.1f},{y1:.1f}"


def upload_grindr_profile_photo(data: bytes, content_type: str, taken_on_grindr: bool = False) -> dict:
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    content_type = _image_type(data, content_type)
    coords = _profile_thumb_coords(data)
    query = {
        "thumbCoords": coords,
        "takenOnGrindr": "true",
    }
    path = "/v4/media/upload?" + urllib.parse.urlencode(query)
    status, payload = _grindr_authed("POST", path, raw=data, content_type=content_type)
    if status != 200:
        print(f"[DEBUG] profile upload {status}: {payload.decode(errors='replace')[:800]}", flush=True)
        raise ApiError(status if status else 502, _grindr_error(payload, status) if payload else "Photo upload was not accepted.")
    try:
        meta = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Photo upload was not accepted.") from exc
    if not isinstance(meta, dict):
        raise ApiError(502, "Photo upload was not accepted.")
    media_hash = None
    sizes = meta.get("imageSizes")
    if isinstance(sizes, list) and sizes:
        first = sizes[0]
        if isinstance(first, dict) and isinstance(first.get("mediaHash"), str):
            media_hash = first["mediaHash"]
    if not media_hash and isinstance(meta.get("hash"), str):
        media_hash = meta["hash"]
    if not media_hash:
        raise ApiError(502, "Photo upload did not return a hash.")
    return {"ok": True, "mediaHash": media_hash}


def fetch_grindr_profile_images() -> dict:
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    status, payload = _grindr_authed("GET", "/v3.1/me/profile/images?selected=false")
    if status != 200:
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(502, "Grindr returned unreadable photos.") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "Grindr returned unreadable photos.")
    medias = data.get("medias")
    if not isinstance(medias, list):
        medias = []
    return {"ok": True, "medias": medias}


def set_grindr_profile_images(primary_image_hash: str | None, secondary_image_hashes: list[str]) -> dict:
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    body = {
        "primaryImageHash": primary_image_hash,
        "secondaryImageHashes": secondary_image_hashes if secondary_image_hashes else [],
    }
    status, payload = _grindr_authed("PUT", "/v3/me/profile/images", body)
    if status not in (200, 201, 204):
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    return {"ok": True}


def delete_grindr_profile_images(media_hashes: list[str]) -> dict:
    if not _grindr_in():
        raise ApiError(401, "Sign in to Grindr.")
    if not isinstance(media_hashes, list) or not media_hashes:
        raise ApiError(400, "No photos selected.")
    body = {"media_hashes": [str(h) for h in media_hashes]}
    status, payload = _grindr_authed("DELETE", "/v3/me/profile/images", body)
    if status not in (200, 201, 204):
        raise ApiError(status if status else 502, _grindr_error(payload, status))
    return {"ok": True}


# Account-aware dispatchers. Sniffies editing is not implemented yet.


def fetch_me_profile() -> dict:
    if _grindr_in():
        return fetch_grindr_me_profile()
    if _sniffies_in():
        raise ApiError(501, "Sniffies profile editing is not implemented yet.")
    raise ApiError(401, "Log in.")


def patch_me_profile(patch: dict) -> dict:
    if _grindr_in():
        return patch_grindr_profile(patch)
    if _sniffies_in():
        raise ApiError(501, "Sniffies profile editing is not implemented yet.")
    raise ApiError(401, "Log in.")


def update_me_profile(patch: dict) -> dict:
    if _grindr_in():
        return update_grindr_profile_full(patch)
    if _sniffies_in():
        raise ApiError(501, "Sniffies profile editing is not implemented yet.")
    raise ApiError(401, "Log in.")


def fetch_me_profile_images() -> dict:
    if _grindr_in():
        return fetch_grindr_profile_images()
    if _sniffies_in():
        raise ApiError(501, "Sniffies profile editing is not implemented yet.")
    raise ApiError(401, "Log in.")


def set_me_profile_images(body: dict) -> dict:
    if _grindr_in():
        primary = body.get("primaryImageHash")
        secondary = body.get("secondaryImageHashes") if isinstance(body.get("secondaryImageHashes"), list) else []
        return set_grindr_profile_images(primary, secondary)
    if _sniffies_in():
        raise ApiError(501, "Sniffies profile editing is not implemented yet.")
    raise ApiError(401, "Log in.")


def delete_me_profile_images(body: dict) -> dict:
    if _grindr_in():
        media_hashes = body.get("mediaHashes") if isinstance(body.get("mediaHashes"), list) else []
        return delete_grindr_profile_images(media_hashes)
    if _sniffies_in():
        raise ApiError(501, "Sniffies profile editing is not implemented yet.")
    raise ApiError(401, "Log in.")


def upload_me_profile_photo(data: bytes, content_type: str, taken_on_grindr: bool = False) -> dict:
    if _grindr_in():
        return upload_grindr_profile_photo(data, content_type, taken_on_grindr)
    if _sniffies_in():
        raise ApiError(501, "Sniffies profile editing is not implemented yet.")
    raise ApiError(401, "Log in.")


DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
VOICE_RE = re.compile(r"^[A-Za-z]{1,40}$")
_ALEXA_DEFAULTS = {
    "enabled": False,
    "readAloud": False,
    "token": "",
    "device": "",
    "voice": "",
}
_VM_ERRORS = {
    "MISSING_DEVICE": "Voice Monkey needs a speaker device.",
    "DEVICE_NOT_FOUND": "That Alexa device was not found.",
    "SCREEN_NOT_FOUND": "Voice Monkey could not find that screen.",
    "UNAUTHORIZED": "Voice Monkey token was rejected.",
    "INVALID_TOKEN": "Voice Monkey token was rejected.",
    "THROTTLED": "Voice Monkey is throttling announcements. Try again shortly.",
    "MONTHLY_QUOTA_EXCEEDED": "Voice Monkey monthly quota is used up.",
    "ALEXA_TRIGGER_FAILED": "Alexa did not play the announcement.",
}
_GENERIC_NOTICE = "You have a new Sniffies message."
_PREVIEW_LIMIT = 180


def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_alexa() -> dict:
    data = dict(_ALEXA_DEFAULTS)
    try:
        raw = json.loads(ALEXA_CONFIG.read_text())
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data
    data["enabled"] = bool(raw.get("enabled"))
    data["readAloud"] = bool(raw.get("readAloud"))
    token = raw.get("token")
    device = raw.get("device")
    voice = raw.get("voice")
    if isinstance(token, str):
        data["token"] = token.strip()
    if isinstance(device, str) and (device == "" or DEVICE_RE.fullmatch(device.strip())):
        data["device"] = device.strip()
    if isinstance(voice, str) and (voice.strip() == "" or VOICE_RE.fullmatch(voice.strip())):
        data["voice"] = voice.strip()
    return data


def load_state() -> dict:
    try:
        raw = json.loads(ALEXA_STATE.read_text())
    except Exception:
        return {"seeded": False, "seen": {}}
    if not isinstance(raw, dict):
        return {"seeded": False, "seen": {}}
    seen_in = raw.get("seen") if isinstance(raw.get("seen"), dict) else {}
    seen = {}
    for key, value in seen_in.items():
        if isinstance(key, str) and isinstance(value, str) and len(key) <= 80 and len(value) <= 40:
            if "\n" not in key and "\n" not in value:
                seen[key] = value
    return {"seeded": bool(raw.get("seeded")), "seen": seen}


def _set_status(error: str | None = None, ok: str | None = None) -> None:
    with _alexa_status_lock:
        if error is not None:
            _alexa_status["lastError"] = error
        if ok is not None:
            _alexa_status["lastOk"] = ok
            _alexa_status["lastError"] = ""


def alexa_public() -> dict:
    cfg = load_alexa()
    state = load_state()
    with _alexa_status_lock:
        last_error = _alexa_status["lastError"]
        last_ok = _alexa_status["lastOk"]
    if cfg["enabled"] and (not cfg["token"] or not cfg["device"]) and not last_error:
        last_error = "Add a Voice Monkey token and a speaker device."
    return {
        "enabled": cfg["enabled"],
        "readAloud": cfg["readAloud"],
        "device": cfg["device"],
        "voice": cfg["voice"],
        "hasToken": bool(cfg["token"]),
        "seeded": bool(state.get("seeded")),
        "lastError": last_error,
        "lastOk": last_ok,
    }


def _clean_token(token: str) -> str:
    token = token.strip()
    if not token or any(ch.isspace() or ord(ch) < 32 for ch in token):
        raise ApiError(400, "Token looks wrong.")
    if not 8 <= len(token) <= 400:
        raise ApiError(400, "Token looks wrong.")
    return token


def update_alexa(body: dict) -> dict:
    cfg = load_alexa()
    if "enabled" in body:
        cfg["enabled"] = bool(body.get("enabled"))
    if "readAloud" in body:
        cfg["readAloud"] = bool(body.get("readAloud"))
    if "device" in body:
        device = str(body.get("device") or "").strip()
        if device and not DEVICE_RE.fullmatch(device):
            raise ApiError(400, "Device id looks wrong.")
        cfg["device"] = device
    if "voice" in body:
        voice = str(body.get("voice") or "").strip()
        if voice and not VOICE_RE.fullmatch(voice):
            raise ApiError(400, "Voice name looks wrong.")
        cfg["voice"] = voice
    if body.get("clearToken"):
        cfg["token"] = ""
    elif "token" in body and str(body.get("token") or "").strip():
        cfg["token"] = _clean_token(str(body.get("token")))
    _write_private(ALEXA_CONFIG, {key: cfg[key] for key in _ALEXA_DEFAULTS})
    _set_status(error="")
    _alexa_wake.set()
    return alexa_public()


def _stamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _newer(current: str, previous: str | None) -> bool:
    if not current:
        return False
    if not previous:
        return True
    return _stamp(current) > _stamp(previous)


def diff_inbox(conversations: list, seen: dict, seeded: bool) -> tuple[list, dict]:
    """Return conversations to announce, plus the cursor map to store.

    The first pass (seeded False) records what is already in the inbox and
    announces nothing. Later, a chat is announced when its latest message is
    newer than the cursor and was not sent by you.
    """
    updated = dict(seen)
    fresh = []
    for convo in conversations:
        if not isinstance(convo, dict):
            continue
        cid = str(convo.get("conversationId") or "")
        last = convo.get("lastAt") or ""
        if not isinstance(last, str):
            last = ""
        if not cid or len(cid) > 80:
            continue
        prev = updated.get(cid)
        previous = prev if isinstance(prev, str) else None
        if seeded and not convo.get("mine") and _newer(last, previous):
            preview = convo.get("preview") if isinstance(convo.get("preview"), str) else ""
            fresh.append({"preview": preview})
        if last and (previous is None or _newer(last, previous) or previous == last):
            updated[cid] = last
        elif cid not in updated:
            updated[cid] = last
    if len(updated) > 500:
        ranked = sorted(updated.items(), key=lambda item: item[1] or "")
        updated = dict(ranked[-500:])
    return fresh, updated


def _plain(text: str) -> str:
    # Curly braces are Voice Monkey placeholders. Strip them so a message
    # cannot pull in a variable. Angle brackets stay out of the spoken text.
    chars = []
    for char in text:
        if char in "{}<>" or ord(char) < 32 or ord(char) == 127:
            chars.append(" ")
        elif char.isprintable():
            chars.append(char)
        else:
            chars.append(" ")
    squashed = " ".join("".join(chars).split())
    if len(squashed) > _PREVIEW_LIMIT:
        squashed = squashed[: _PREVIEW_LIMIT - 1].rstrip() + "…"
    return squashed


def speeches_for(items: list, read_aloud: bool) -> list[str]:
    if not items:
        return []
    if not read_aloud:
        if len(items) == 1:
            return [_GENERIC_NOTICE]
        return [f"You have {len(items)} new Sniffies messages."]
    speeches = []
    for item in items[:3]:
        preview = item.get("preview") or ""
        if preview == "Photo":
            speeches.append("New Sniffies message. They sent a photo.")
            continue
        plain = _plain(preview)
        speeches.append(f"New Sniffies message. {plain}" if plain else _GENERIC_NOTICE)
    extra = len(items) - min(len(items), 3)
    if extra == 1:
        speeches.append("You have one more Sniffies message.")
    elif extra > 1:
        speeches.append(f"You have {extra} more Sniffies messages.")
    return speeches


def _vm_message(status: int, raw: bytes) -> str:
    code = ""
    try:
        parsed = json.loads(raw.decode() or "{}")
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
            code = parsed["error"]
    except Exception:
        code = ""
    if code in _VM_ERRORS:
        return _VM_ERRORS[code]
    if status in (401, 403):
        return "Voice Monkey token was rejected."
    if status == 404:
        return "That Alexa device was not found."
    if status == 429:
        return "Voice Monkey is throttling announcements. Try again shortly."
    if status == 400:
        return "Voice Monkey rejected the announcement."
    if status >= 500:
        return "Alexa did not play the announcement."
    return "Voice Monkey could not play that."


def _voicemonkey(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    # Direct connection.
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "User-Agent": "sniffies-messages",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(VOICE_MONKEY + path, data=data, headers=headers, method=method)
    try:
        with _direct_opener.open(request, timeout=20) as response:
            raw = response.read(16000)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read(4000)
        status = exc.code
    except Exception:
        raise ApiError(502, "Could not reach Voice Monkey.")
    if status == 200:
        try:
            parsed = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            return {}
        if parsed.get("success") is False:
            raise ApiError(502, _vm_message(status, raw))
        return parsed
    mapped = status if 400 <= status <= 599 else 502
    raise ApiError(mapped, _vm_message(status, raw))


def _announce(cfg: dict, speech: str) -> None:
    payload = {"device": cfg["device"], "speech": speech}
    if cfg.get("voice"):
        payload["voice"] = cfg["voice"]
    _voicemonkey("POST", "/announce", cfg["token"], payload)
    _set_status(error="", ok=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def list_speakers() -> list[dict]:
    cfg = load_alexa()
    if not cfg["token"]:
        raise ApiError(400, "Add a Voice Monkey token first.")
    parsed = _voicemonkey("GET", "/devices", cfg["token"])
    devices = parsed.get("data")
    if not isinstance(devices, list):
        raise ApiError(502, "Voice Monkey did not return devices.")
    speakers = []
    for item in devices:
        if not isinstance(item, dict):
            continue
        if item.get("capability") not in (None, "speakers"):
            continue
        device_id = str(item.get("id") or "")
        if not DEVICE_RE.fullmatch(device_id):
            continue
        name = item.get("name")
        label = name.strip() if isinstance(name, str) and name.strip() else device_id
        speakers.append({"id": device_id, "name": label[:80]})
    return speakers


def alexa_test() -> None:
    cfg = load_alexa()
    if not cfg["token"] or not cfg["device"]:
        raise ApiError(400, "Add a Voice Monkey token and a speaker device first.")
    _announce(cfg, "Sniffies notifications are working.")


_NOTIFY_DEFAULTS = {"enabled": False, "words": []}


def _parse_words(raw: str) -> list[str]:
    words: list[str] = []
    for part in raw.split(","):
        word = " ".join(part.lower().split())
        if 1 <= len(word) <= 40 and word not in words:
            words.append(word)
        if len(words) >= 30:
            break
    return words


def load_notify() -> dict:
    data = {"enabled": False, "words": []}
    try:
        raw = json.loads(NOTIFY_CONFIG.read_text())
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data
    data["enabled"] = bool(raw.get("enabled"))
    words = raw.get("words")
    if isinstance(words, list):
        data["words"] = _parse_words(",".join(str(item) for item in words if isinstance(item, str)))
    elif isinstance(words, str):
        data["words"] = _parse_words(words)
    return data


def notify_public() -> dict:
    cfg = load_notify()
    state = load_state_notify()
    with _alexa_status_lock:
        last_error = _alexa_status["lastError"]
        last_ok = _alexa_status["lastOk"]
    return {
        "enabled": cfg["enabled"],
        "words": ", ".join(cfg["words"]),
        "seeded": bool(state.get("seeded")),
        "lastError": last_error,
        "lastOk": last_ok,
    }


def update_notify(body: dict) -> dict:
    cfg = load_notify()
    if "enabled" in body:
        cfg["enabled"] = bool(body.get("enabled"))
    if "words" in body:
        raw = body.get("words")
        if isinstance(raw, list):
            cfg["words"] = _parse_words(",".join(str(item) for item in raw))
        else:
            cfg["words"] = _parse_words(str(raw or ""))
    _write_private(NOTIFY_CONFIG, cfg)
    _set_status(error="")
    _alexa_wake.set()
    return notify_public()


def load_state_notify() -> dict:
    try:
        raw = json.loads(NOTIFY_STATE.read_text())
    except Exception:
        return {"seeded": False, "seen": {}, "community": []}
    if not isinstance(raw, dict):
        return {"seeded": False, "seen": {}, "community": []}
    seen_in = raw.get("seen") if isinstance(raw.get("seen"), dict) else {}
    seen = {}
    for key, value in seen_in.items():
        if isinstance(key, str) and isinstance(value, str) and len(key) <= 80 and len(value) <= 40:
            if "\n" not in key and "\n" not in value:
                seen[key] = value
    community = []
    for item in raw.get("community") or []:
        if isinstance(item, str) and USER_ID_RE.match(item):
            community.append(item)
    return {"seeded": bool(raw.get("seeded")), "seen": seen, "community": community[-400:]}


def _match_word(text: str, words: list[str]) -> str | None:
    hay = text.lower()
    for word in words:
        if word and word in hay:
            return word
    return None


_BEDROCK_DEFAULT_MODEL = "us.anthropic.claude-haiku-5-5"
_BEDROCK_DEFAULT_REGION = "us-east-1"
_BEDROCK_REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-[0-9]$")

_AUTO_RESPONDER_DEFAULTS = {
    "enabled": False,
    "provider": "bedrock",
    "mode": "draft",
    "bedrockRegion": _BEDROCK_DEFAULT_REGION,
    "bedrockKey": "",
    "openaiBase": "https://api.openai.com/v1",
    "openaiKey": "",
    "workerUrl": "",
    "key": "",
    "model": "",
    "prompt": "",
    "maxPerHour": 60,
    "minGapMinutes": 2,
    "acceptTerms": False,
    "targetMode": "selected",
    "targets": [],
    "sendPhotosWhenAsked": False,
    "autoResponderPhotos": [],
}


def _auto_responder_event(kind: str, detail: str = "") -> None:
    """Append an event to the debug ring buffer (newest last, max 200)."""
    with _auto_responder_status_lock:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = {"t": stamp, "kind": kind, "detail": detail[:300]}
        last = _auto_responder_events[-1] if _auto_responder_events else None
        if last and last["kind"] == kind and last["detail"] == detail:
            last["t"] = stamp
            last["count"] = int(last.get("count") or 1) + 1
        else:
            entry["count"] = 1
            _auto_responder_events.append(entry)
            del _auto_responder_events[:-200]


def _set_auto_responder_status(
    error: str | None = None, ok: str | None = None, enabled: bool | None = None
) -> None:
    with _auto_responder_status_lock:
        if error is not None:
            _auto_responder_status["lastError"] = error
        if ok is not None:
            _auto_responder_status["lastOk"] = ok
            _auto_responder_status["lastError"] = ""
        if enabled is not None:
            _auto_responder_status["enabled"] = enabled


def load_auto_responder() -> dict:
    data = dict(_AUTO_RESPONDER_DEFAULTS)
    if getattr(_sys, "frozen", False):
        data["provider"] = "openai"
    try:
        raw = json.loads(_AUTO_RESPONDER_CONFIG.read_text())
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data
    for key in _AUTO_RESPONDER_DEFAULTS:
        if key in raw:
            data[key] = raw[key]
    if "provider" not in raw and getattr(_sys, "frozen", False):
        data["provider"] = "openai"
    if data.get("targetMode") == "all":
        data["targetMode"] = "selected"
        _save_auto_responder(data)
    return data


def _save_auto_responder(cfg: dict) -> None:
    _write_private(_AUTO_RESPONDER_CONFIG, cfg)


def load_state_auto_responder() -> dict:
    try:
        raw = json.loads(_AUTO_RESPONDER_STATE.read_text())
    except Exception:
        return {"seeded": False, "last": {}, "replies": {}, "pending": {}, "photoLast": {}, "drafts": {}, "hourStart": 0, "hourCount": 0}
    if not isinstance(raw, dict):
        return {"seeded": False, "last": {}, "replies": {}, "pending": {}, "photoLast": {}, "drafts": {}, "hourStart": 0, "hourCount": 0}
    return {
        "seeded": bool(raw.get("seeded")),
        "last": raw.get("last") if isinstance(raw.get("last"), dict) else {},
        "replies": raw.get("replies") if isinstance(raw.get("replies"), dict) else {},
        "pending": raw.get("pending") if isinstance(raw.get("pending"), dict) else {},
        "photoLast": raw.get("photoLast") if isinstance(raw.get("photoLast"), dict) else {},
        "drafts": raw.get("drafts") if isinstance(raw.get("drafts"), dict) else {},
        "hourStart": raw.get("hourStart") or 0,
        "hourCount": raw.get("hourCount") or 0,
    }


def _save_state_auto_responder(state: dict) -> None:
    clean = {
        "seeded": bool(state.get("seeded")),
        "last": state.get("last") or {},
        "replies": state.get("replies") or {},
        "pending": state.get("pending") or {},
        "photoLast": state.get("photoLast") or {},
        "drafts": state.get("drafts") or {},
        "hourStart": state.get("hourStart") or 0,
        "hourCount": state.get("hourCount") or 0,
    }
    _write_private(_AUTO_RESPONDER_STATE, clean)


def _bedrock_key(cfg: dict) -> str:
    return str(cfg.get("bedrockKey") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()


def _ai_ready(cfg: dict) -> bool:
    """True when the chosen provider has what it needs to generate text."""
    provider = str(cfg.get("provider") or "bedrock")
    if provider == "worker":
        return bool(cfg.get("workerUrl") and cfg.get("key"))
    if provider == "openai":
        return bool(cfg.get("openaiKey") and str(cfg.get("model") or "").strip())
    return bool(_bedrock_key(cfg))


def _auto_mode(cfg: dict) -> str:
    return "auto" if str(cfg.get("mode") or "draft") == "auto" else "draft"


def _responder_on(cfg: dict) -> bool:
    """Watching runs when enabled and configured. Auto-send also needs the risk box."""
    if not (cfg.get("enabled") and _ai_ready(cfg)):
        return False
    if _auto_mode(cfg) == "auto" and not cfg.get("acceptTerms"):
        return False
    return True


def auto_responder_public() -> dict:
    cfg = load_auto_responder()
    state = load_state_auto_responder()
    with _auto_responder_status_lock:
        status = dict(_auto_responder_status)
    enabled = _responder_on(cfg)
    drafts = state.get("drafts") or {}
    return {
        "enabled": enabled,
        "enabledSetting": bool(cfg.get("enabled")),
        "provider": str(cfg.get("provider") or "bedrock"),
        "mode": _auto_mode(cfg),
        "bedrockRegion": str(cfg.get("bedrockRegion") or _BEDROCK_DEFAULT_REGION),
        "bedrockKeySet": bool(_bedrock_key(cfg)),
        "bedrockKeyFromEnv": bool(not cfg.get("bedrockKey") and os.environ.get("AWS_BEARER_TOKEN_BEDROCK")),
        "workerKeySet": bool(cfg.get("key")),
        "openaiBase": str(cfg.get("openaiBase") or "https://api.openai.com/v1"),
        "openaiKeySet": bool(cfg.get("openaiKey")),
        "defaultModel": _BEDROCK_DEFAULT_MODEL,
        "draftCount": len(drafts),
        "draftIds": sorted(drafts.keys())[:200],
        "workerUrl": str(cfg.get("workerUrl") or ""),
        "model": str(cfg.get("model") or ""),
        "prompt": str(cfg.get("prompt") or ""),
        "maxPerHour": int(cfg.get("maxPerHour") or 60),
        "minGapMinutes": int(cfg.get("minGapMinutes") or 2),
        "acceptTerms": bool(cfg.get("acceptTerms")),
        "targetMode": str(cfg.get("targetMode") or "all"),
        "targets": list(cfg.get("targets") or []),
        "sendPhotosWhenAsked": bool(cfg.get("sendPhotosWhenAsked")),
        "autoResponderPhotos": list(cfg.get("autoResponderPhotos") or []),
        "seeded": bool(state.get("seeded")),
        "lastError": status.get("lastError", ""),
        "lastOk": status.get("lastOk", ""),
    }


def update_auto_responder(body: dict) -> dict:
    cfg = load_auto_responder()
    if "enabled" in body:
        cfg["enabled"] = bool(body.get("enabled"))
    if "provider" in body:
        prov = str(body.get("provider"))
        cfg["provider"] = prov if prov in ("worker", "openai") else "bedrock"
    if "openaiBase" in body:
        base = str(body.get("openaiBase") or "").strip().rstrip("/") or "https://api.openai.com/v1"
        if not base.startswith(("https://", "http://127.0.0.1", "http://localhost")) or len(base) > 300:
            raise ApiError(400, "Base URL must be https:// (or a local http://127.0.0.1 server).")
        cfg["openaiBase"] = base
    if "openaiKey" in body:
        okey = str(body.get("openaiKey") or "").strip()
        if okey:
            if len(okey) < 8 or len(okey) > 1000 or any(c.isspace() for c in okey):
                raise ApiError(400, "That API key doesn't look right.")
            cfg["openaiKey"] = okey
    if "mode" in body:
        cfg["mode"] = "auto" if str(body.get("mode")) == "auto" else "draft"
    if "bedrockRegion" in body:
        region = str(body.get("bedrockRegion") or "").strip() or _BEDROCK_DEFAULT_REGION
        if not _BEDROCK_REGION_RE.match(region):
            raise ApiError(400, "That doesn't look like an AWS region (e.g. us-east-1).")
        cfg["bedrockRegion"] = region
    if "bedrockKey" in body:
        bkey = str(body.get("bedrockKey") or "").strip()
        if bkey:
            if len(bkey) < 20 or len(bkey) > 4000 or any(c.isspace() for c in bkey):
                raise ApiError(400, "That Bedrock API key doesn't look right.")
            cfg["bedrockKey"] = bkey
    if body.get("clearBedrockKey"):
        cfg["bedrockKey"] = ""
    if "targetMode" in body:
        mode = str(body.get("targetMode") or "all")
        if mode not in ("all", "selected"):
            mode = "all"
        cfg["targetMode"] = mode
    if "targets" in body:
        targets_raw = body.get("targets")
        if isinstance(targets_raw, list):
            targets = []
            for t in targets_raw:
                tid = str(t or "").strip()
                if USER_ID_RE.match(tid):
                    targets.append(tid)
            cfg["targets"] = targets[:200]
        else:
            cfg["targets"] = []
    if "sendPhotosWhenAsked" in body:
        cfg["sendPhotosWhenAsked"] = bool(body.get("sendPhotosWhenAsked"))
    if cfg.get("targetMode") == "all":
        cfg["targetMode"] = "selected"
    if "workerUrl" in body:
        url = str(body.get("workerUrl") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            raise ApiError(400, "Worker URL must start with http:// or https://.")
        if len(url) > 500:
            raise ApiError(400, "Worker URL is too long.")
        cfg["workerUrl"] = url
    if "key" in body:
        key = str(body.get("key") or "").strip()
        if key:
            if len(key) < 8:
                raise ApiError(400, "Worker key must be at least 8 characters.")
            if len(key) > 500:
                raise ApiError(400, "Worker key is too long.")
            cfg["key"] = key
    if "model" in body:
        model = str(body.get("model") or "").strip()
        if model and len(model) > 120:
            raise ApiError(400, "Model name is too long.")
        cfg["model"] = model
    if "prompt" in body:
        prompt = str(body.get("prompt") or "").strip()
        if len(prompt) > 4000:
            raise ApiError(400, "Prompt is too long.")
        cfg["prompt"] = prompt
    if "maxPerHour" in body:
        try:
            cfg["maxPerHour"] = max(1, min(600, int(body.get("maxPerHour"))))
        except (TypeError, ValueError):
            cfg["maxPerHour"] = 60
    if "minGapMinutes" in body:
        try:
            cfg["minGapMinutes"] = max(0, min(1440, int(body.get("minGapMinutes"))))
        except (TypeError, ValueError):
            cfg["minGapMinutes"] = 2
    if "acceptTerms" in body:
        cfg["acceptTerms"] = bool(body.get("acceptTerms"))

    effective_enabled = _responder_on(cfg)
    if not effective_enabled:
        _auto_responder_disable()
    _save_auto_responder(cfg)
    _set_auto_responder_status(error="")
    if effective_enabled:
        _auto_responder_wake.set()
    return auto_responder_public()


def update_auto_responder_target(conversation_id: str, enabled: bool) -> dict:
    cid = str(conversation_id or "").strip()
    if not USER_ID_RE.match(cid):
        raise ApiError(400, "Invalid conversation.")
    cfg = load_auto_responder()
    targets = set(cfg.get("targets") or [])
    if enabled:
        targets.add(cid)
    else:
        targets.discard(cid)
    cfg["targets"] = sorted(targets)[:200]
    cfg["targetMode"] = "selected"
    _save_auto_responder(cfg)
    state = load_state_auto_responder()
    if enabled:
        state["last"].pop(cid, None)
    state["pending"].pop(cid, None)
    _save_state_auto_responder(state)
    _set_auto_responder_status(error="")
    if _responder_on(cfg):
        _auto_responder_wake.set()
    return auto_responder_public()


def _prepare_thread_messages(thread: dict) -> list[dict]:
    out = []
    for raw in (thread.get("messages") or [])[-12:]:
        if not isinstance(raw, dict):
            continue
        body = raw.get("body")
        text = body if isinstance(body, str) and body else ""
        if not text and raw.get("file"):
            text = "[sent a photo]"
        if not text:
            continue
        role = "assistant" if raw.get("mine") else "user"
        out.append({"role": role, "content": text})
    return out


_PHOTO_REQUEST_RE = re.compile(
    r"\b(pic|pics|pix|photo|photos|picture|pictures|nude|nudes|face|body|dick|cock|ass|booty|hole|feet)\b",
    re.IGNORECASE,
)


_PHOTO_REFUSAL_RE = re.compile(
    r"\b(no|not|don'?t|dont|never|stop|without)\b[^.!?]{0,20}\b(pic|pics|pix|photo|photos|picture|pictures|nude|nudes)\b",
    re.IGNORECASE,
)


def _is_photo_request(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    lower = text.lower()
    if _PHOTO_REFUSAL_RE.search(lower):
        return False
    if _PHOTO_REQUEST_RE.search(lower):
        return True
    ask_words = ("send", "show", "trade", "swap", "more", "got", "have", "share", "give")
    if any(w in lower for w in ask_words) and re.search(
        r"\b(pic|pics|pix|photo|photos|picture|pictures|nude|nudes)\b", lower
    ):
        return True
    return False


def _pick_photo(pool: list[str], last: str | None) -> str:
    if len(pool) == 1:
        return pool[0]
    candidates = [p for p in pool if p != last]
    if not candidates:
        candidates = pool
    return secrets.choice(candidates)


def delete_auto_responder_photo(filename: str) -> dict:
    cfg = load_auto_responder()
    photos = list(cfg.get("autoResponderPhotos") or [])
    safe = Path(filename).name
    if safe not in photos:
        raise ApiError(404, "Photo not in pool.")
    photos.remove(safe)
    cfg["autoResponderPhotos"] = photos
    _save_auto_responder(cfg)
    return {"ok": True, "photos": photos}


def _call_ai_worker(cfg: dict, messages: list[dict]) -> str:
    url = str(cfg.get("workerUrl") or "").rstrip("/") + "/respond"
    key = str(cfg.get("key") or "")
    payload: dict = {"messages": messages}
    model = str(cfg.get("model") or "").strip()
    if model:
        payload["model"] = model
    prompt = str(cfg.get("prompt") or "").strip()
    if prompt:
        payload["prompt"] = prompt
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "sniffies-messages/1.0",
        "Authorization": "Bearer " + key,
    }
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _direct_opener.open(request, timeout=30) as response:
            raw = response.read(32000)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read(4000)
        status = exc.code
    except Exception:
        raise ApiError(502, "Could not reach the AI worker.")
    if status != 200:
        try:
            parsed = json.loads(raw.decode() or "{}")
            msg = parsed.get("error") if isinstance(parsed, dict) else None
        except Exception:
            msg = None
        raise ApiError(status if 400 <= status <= 599 else 502, msg or "AI worker returned an error.")
    try:
        parsed = json.loads(raw.decode() or "{}")
    except Exception:
        raise ApiError(502, "AI worker returned unreadable JSON.")
    if not isinstance(parsed, dict):
        raise ApiError(502, "AI worker returned an unexpected response.")
    reply = parsed.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise ApiError(502, "AI worker returned an empty reply.")
    return reply.strip()[:2000]


def _converse_messages(messages: list[dict]) -> list[dict]:
    """Bedrock Converse needs user-first, strictly alternating turns."""
    out: list[dict] = []
    for m in messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("content") or "").strip()[:6000]
        if not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"][0]["text"] += "\n" + text
        else:
            out.append({"role": role, "content": [{"text": text}]})
    out = out[-20:]
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _call_bedrock(cfg: dict, messages: list[dict]) -> str:
    key = _bedrock_key(cfg)
    if not key:
        raise ApiError(400, "Add a Bedrock API key first.")
    region = str(cfg.get("bedrockRegion") or _BEDROCK_DEFAULT_REGION)
    if not _BEDROCK_REGION_RE.match(region):
        region = _BEDROCK_DEFAULT_REGION
    model = str(cfg.get("model") or "").strip()
    if not model or model.startswith("@cf/"):
        model = _BEDROCK_DEFAULT_MODEL
    turns = _converse_messages(messages)
    if not turns:
        raise ApiError(400, "Nothing to reply to.")
    prompt = str(cfg.get("prompt") or "").strip() or _DEFAULT_AUTO_RESPONDER_PROMPT
    payload = {
        "system": [{"text": prompt[:4000]}],
        "messages": turns,
        "inferenceConfig": {"maxTokens": 256, "temperature": 0.75},
    }
    url = (
        f"https://bedrock-runtime.{region}.amazonaws.com/model/"
        + urllib.parse.quote(model, safe="")
        + "/converse"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "griffies/1.0",
        },
        method="POST",
    )
    try:
        with _direct_opener.open(request, timeout=45) as response:
            raw = response.read(64000)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read(4000)
        status = exc.code
    except Exception:
        raise ApiError(502, "Could not reach Amazon Bedrock.")
    try:
        parsed = json.loads(raw.decode() or "{}")
    except Exception:
        parsed = {}
    if status != 200:
        msg = parsed.get("message") or parsed.get("Message") if isinstance(parsed, dict) else None
        if status in (401, 403):
            msg = "Bedrock refused the key. " + (msg or "Check the key and that it can call this model.")
        raise ApiError(status if 400 <= status <= 599 else 502, (msg or "Bedrock returned an error.")[:300])
    try:
        parts = parsed["output"]["message"]["content"]
        reply = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    except Exception:
        raise ApiError(502, "Bedrock returned an unexpected response.")
    reply = re.sub(r"^\s*(user|assistant)\s*[:\-]\s*", "", reply.strip(), flags=re.IGNORECASE)
    if not reply:
        raise ApiError(502, "The model returned an empty reply.")
    return reply[:2000]


def _call_openai(cfg: dict, messages: list[dict]) -> str:
    key = str(cfg.get("openaiKey") or "").strip()
    model = str(cfg.get("model") or "").strip()
    if not key or not model:
        raise ApiError(400, "Add an API key and a model name first.")
    base = str(cfg.get("openaiBase") or "https://api.openai.com/v1").rstrip("/")
    prompt = str(cfg.get("prompt") or "").strip() or _DEFAULT_AUTO_RESPONDER_PROMPT
    turns = [{"role": "system", "content": prompt[:4000]}]
    for m in messages[-20:]:
        text = str(m.get("content") or "").strip()[:6000]
        if text:
            turns.append({"role": "assistant" if m.get("role") == "assistant" else "user", "content": text})
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps({"model": model, "messages": turns, "max_tokens": 256, "temperature": 0.75}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "griffies/1.0",
        },
        method="POST",
    )
    try:
        with _direct_opener.open(request, timeout=45) as response:
            raw = response.read(64000)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read(4000)
        status = exc.code
    except Exception:
        raise ApiError(502, "Could not reach the AI provider.")
    try:
        parsed = json.loads(raw.decode() or "{}")
    except Exception:
        parsed = {}
    if status != 200:
        err = parsed.get("error") if isinstance(parsed, dict) else None
        msg = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else None)
        raise ApiError(status if 400 <= status <= 599 else 502, (msg or "The AI provider returned an error.")[:300])
    try:
        reply = str(parsed["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        raise ApiError(502, "The AI provider returned an unexpected response.")
    if not reply:
        raise ApiError(502, "The model returned an empty reply.")
    return reply[:2000]


def _call_ai(cfg: dict, messages: list[dict]) -> str:
    provider = str(cfg.get("provider") or "bedrock")
    if provider == "worker":
        return _call_ai_worker(cfg, messages)
    if provider == "openai":
        return _call_openai(cfg, messages)
    return _call_bedrock(cfg, messages)


def _store_draft(cid: str, text: str, photo: str = "") -> dict:
    state = load_state_auto_responder()
    entry = {"text": text[:2000], "photo": photo, "at": time.time()}
    state["drafts"][cid] = entry
    if len(state["drafts"]) > 200:
        oldest = sorted(state["drafts"].items(), key=lambda kv: kv[1].get("at") or 0)
        for k, _ in oldest[:-200]:
            state["drafts"].pop(k, None)
    _save_state_auto_responder(state)
    return entry


def auto_responder_draft(conversation_id: str) -> dict:
    cid = str(conversation_id or "").strip()
    if not USER_ID_RE.match(cid):
        raise ApiError(400, "Invalid conversation.")
    state = load_state_auto_responder()
    return {"draft": state["drafts"].get(cid)}


def auto_responder_suggest(conversation_id: str) -> dict:
    cid = str(conversation_id or "").strip()
    if not USER_ID_RE.match(cid) or GRINDR_CONVO_RE.match(cid):
        raise ApiError(400, "Suggestions work on Sniffies chats.")
    cfg = load_auto_responder()
    if not _ai_ready(cfg):
        raise ApiError(400, "Set up AI Reply first (add an API key).")
    thread = fetch_thread(cid, None)
    messages = _prepare_thread_messages(thread)
    if not messages:
        raise ApiError(400, "There's nothing in this chat to reply to yet.")
    if messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": "[no new message; suggest a natural follow-up]"})
    reply = _call_ai(cfg, messages)
    entry = _store_draft(cid, reply)
    _auto_responder_event("draft", f"{cid[:12]}: suggested {reply[:50]}")
    return {"draft": entry}


def auto_responder_dismiss(conversation_id: str) -> dict:
    cid = str(conversation_id or "").strip()
    state = load_state_auto_responder()
    state["drafts"].pop(cid, None)
    _save_state_auto_responder(state)
    return {"ok": True}


def auto_responder_debug() -> dict:
    cfg = load_auto_responder()
    state = load_state_auto_responder()
    with _auto_responder_status_lock:
        status = dict(_auto_responder_status)
        events = list(_auto_responder_events)
    pending = state.get("pending") or {}
    hour_start = float(state.get("hourStart") or 0)
    return {
        "status": status,
        "config": {
            "enabled": bool(cfg.get("enabled")),
            "targetMode": cfg.get("targetMode"),
            "targets": len(cfg.get("targets") or []),
            "maxPerHour": cfg.get("maxPerHour"),
            "minGapMinutes": cfg.get("minGapMinutes"),
            "sendPhotosWhenAsked": bool(cfg.get("sendPhotosWhenAsked")),
            "photosInPool": len(cfg.get("autoResponderPhotos") or []),
            "model": cfg.get("model") or "(default)",
            "promptChars": len(str(cfg.get("prompt") or "")),
            "workerUrl": cfg.get("workerUrl"),
            "provider": cfg.get("provider") or "bedrock",
            "mode": _auto_mode(cfg),
            "bedrockRegion": cfg.get("bedrockRegion"),
            "drafts": len(state.get("drafts") or {}),
        },
        "state": {
            "signedIn": _sniffies_in(),
            "pollSeconds": AUTO_RESPONDER_POLL_SECONDS,
            "seeded": bool(state.get("seeded")),
            "trackedChats": len(state.get("last") or {}),
            "pending": {
                cid: {
                    "waits": round(max(0, 15 - (time.time() - float(e.get("since") or 0)), 0), 1),
                    "tries": int(e.get("tries") or 0),
                    "lastAt": e.get("lastAt"),
                }
                if isinstance(e, dict)
                else {"legacy": e}
                for cid, e in pending.items()
            },
            "hourCount": state.get("hourCount"),
            "hourWindowResetsIn": max(0, round(3600 - (time.time() - hour_start))),
            "repliesSent": len(state.get("replies") or {}),
        },
        "events": events[-200:],
    }


def auto_responder_test() -> dict:
    cfg = load_auto_responder()
    if not _ai_ready(cfg):
        provider = str(cfg.get("provider") or "bedrock")
        if provider == "worker":
            raise ApiError(400, "Add the Worker URL and key first.")
        if provider == "openai":
            raise ApiError(400, "Add an API key and a model name first.")
        raise ApiError(400, "Add a Bedrock API key first.")
    sample = [
        {"role": "user", "content": "hey, what are you up to tonight?"},
    ]
    reply = _call_ai(cfg, sample)
    return {"draft": reply}


def _auto_responder_once() -> None:
    cfg = load_auto_responder()
    enabled = _responder_on(cfg)
    draft_mode = _auto_mode(cfg) == "draft"
    _set_auto_responder_status(enabled=enabled)
    if not enabled:
        _auto_responder_event("tick", "off (needs enabled + key; auto-send also needs the risk box)")
        return
    if not _sniffies_in():
        _auto_responder_event("tick", "skipped: not signed in to Sniffies")
        return
    try:
        chats = fetch_chats()
    except ApiError as exc:
        _set_auto_responder_status(error=exc.message)
        raise
    conversations = chats.get("conversations") or []
    state = load_state_auto_responder()
    if not state.get("seeded"):
        for conv in conversations:
            cid = str(conv.get("conversationId") or "")
            if cid:
                state["last"][cid] = str(conv.get("lastAt") or "")
        state["seeded"] = True
        state["hourStart"] = time.time()
        _save_state_auto_responder(state)
        _set_auto_responder_status(ok="Watching. First check recorded existing chats.")
        _auto_responder_event("seed", f"recorded {len(conversations)} existing chats")
        print("auto-responder seeded")
        return

    now = time.time()
    if now - (state.get("hourStart") or 0) > 3600:
        state["hourCount"] = 0
        state["hourStart"] = now

    max_per_hour = max(1, int(cfg.get("maxPerHour") or 60))
    min_gap = max(0, int(cfg.get("minGapMinutes") or 2)) * 60
    sent = 0

    # Sort by recency so the newest unread threads are handled first.
    sorted_conversations = sorted(
        conversations,
        key=lambda c: c.get("lastAt") or "",
        reverse=True,
    )

    target_mode = str(cfg.get("targetMode") or "all")
    targets = set(cfg.get("targets") or [])
    pending = state.get("pending")
    if not isinstance(pending, dict):
        pending = state["pending"] = {}
    wake_soon = False

    def _consume(cid: str, last_at: str, entry: dict) -> None:
        # Mark the thread as handled only once we are done with it.
        if last_at:
            state["last"][cid] = last_at
        elif entry.get("lastAt"):
            state["last"][cid] = entry["lastAt"]
        pending.pop(cid, None)

    def _bump_tries(cid: str, last_at: str, entry: dict, why: str) -> None:
        entry["tries"] = int(entry.get("tries") or 0) + 1
        if entry["tries"] >= 5:
            print(f"auto-responder giving up on {cid} after {entry['tries']} tries: {why}")
            _auto_responder_event("giveup", f"{cid[:12]}: {why}")
            _consume(cid, last_at, entry)

    previews = {
        str(c.get("conversationId") or ""): str(c.get("preview") or "")
        for c in conversations
    }

    for conv in sorted_conversations:
        cid = str(conv.get("conversationId") or "")
        user_id = str(conv.get("userId") or "")
        if not cid or not user_id or user_id.startswith("g:") or GRINDR_CONVO_RE.match(cid):
            continue
        if target_mode == "selected" and cid not in targets:
            continue
        last_at = str(conv.get("lastAt") or "")
        prev = state["last"].get(cid) or ""
        is_new = _newer(last_at, prev)
        mine = bool(conv.get("mine"))

        entry = pending.get(cid)
        if isinstance(entry, (int, float)):
            entry = {"since": float(entry), "lastAt": "", "tries": 0}

        if is_new:
            if mine:
                # Our own message is the latest; nothing to answer.
                _consume(cid, last_at, entry or {})
                continue
            if entry is None:
                entry = {"since": now, "lastAt": last_at, "tries": 0}
                pending[cid] = entry
                _auto_responder_event("watch", f"{cid[:12]}: {previews.get(cid, '')}")
            elif _newer(last_at, entry.get("lastAt") or ""):
                entry["lastAt"] = last_at
        elif entry is None:
            continue

        # An entry exists: wait out the quiet period, then try to send.
        if now - float(entry["since"]) < 15:
            wake_soon = True
            continue
        if mine:
            _consume(cid, last_at, entry)
            continue
        last_reply_at = state["replies"].get(cid)
        if isinstance(last_reply_at, (int, float)) and now - float(last_reply_at) < min_gap:
            wake_soon = True
            continue
        if int(state.get("hourCount") or 0) >= max_per_hour:
            wake_soon = True
            continue
        try:
            thread = fetch_thread(cid, None)
        except ApiError as exc:
            print(f"auto-responder could not fetch thread {cid}: {exc.message}")
            _bump_tries(cid, last_at, entry, exc.message)
            continue
        messages = _prepare_thread_messages(thread)
        if not messages or messages[-1].get("role") != "user":
            # The latest message is ours or the thread is empty; nothing to answer.
            _auto_responder_event("skip", f"{cid[:12]}: nothing to answer (latest is ours)")
            _consume(cid, last_at, entry)
            continue
        last_text = messages[-1].get("content") or ""
        photo_pool = list(cfg.get("autoResponderPhotos") or [])
        if draft_mode:
            try:
                reply = _call_ai(cfg, messages)
            except ApiError as exc:
                _set_auto_responder_status(error=f"AI: {exc.message}")
                _bump_tries(cid, last_at, entry, exc.message)
                continue
            photo = ""
            if cfg.get("sendPhotosWhenAsked") and photo_pool and _is_photo_request(last_text):
                photo = _pick_photo(photo_pool, (state.get("photoLast") or {}).get(cid))
            state["drafts"][cid] = {"text": reply[:2000], "photo": photo, "at": now}
            state["hourCount"] = int(state.get("hourCount") or 0) + 1
            _consume(cid, last_at, entry)
            sent += 1
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            _set_auto_responder_status(ok=f"Draft ready at {ts}.")
            _auto_responder_event("draft", f"{cid[:12]}: {reply[:60]}")
            continue
        if cfg.get("sendPhotosWhenAsked") and photo_pool and _is_photo_request(last_text):
            photo_last = state.get("photoLast") or {}
            chosen = _pick_photo(photo_pool, photo_last.get(cid))
            try:
                send_saved_photo(cid, user_id, chosen)
                photo_last[cid] = chosen
                state["photoLast"] = photo_last
                state["replies"][cid] = now
                state["hourCount"] = int(state.get("hourCount") or 0) + 1
                _consume(cid, last_at, entry)
                sent += 1
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                _set_auto_responder_status(ok=f"Auto-sent photo at {ts}.")
                _auto_responder_event("sent", f"{cid[:12]}: photo ({chosen})")
            except ApiError as exc:
                _set_auto_responder_status(error=f"Photo send failed: {exc.message}")
                print(f"auto-responder photo send failed for {cid}: {exc.message}")
                _bump_tries(cid, last_at, entry, exc.message)
            continue
        try:
            reply = _call_ai(cfg, messages)
        except ApiError as exc:
            _set_auto_responder_status(error=f"AI: {exc.message}")
            print(f"auto-responder AI failed: {exc.message}")
            _bump_tries(cid, last_at, entry, exc.message)
            continue
        try:
            send_reply(cid, user_id, reply)
        except ApiError as exc:
            _set_auto_responder_status(error=f"Send failed: {exc.message}")
            print(f"auto-responder send failed for {cid}: {exc.message}")
            _bump_tries(cid, last_at, entry, exc.message)
            continue
        state["replies"][cid] = now
        state["hourCount"] = int(state.get("hourCount") or 0) + 1
        _consume(cid, last_at, entry)
        sent += 1
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        _set_auto_responder_status(ok=f"Auto-replied at {ts}.")
        _auto_responder_event("sent", f"{cid[:12]}: {reply[:60]}")

    if wake_soon:
        _auto_responder_schedule_wake(15)
    _save_state_auto_responder(state)
    _auto_responder_event(
        "tick",
        f"{len(conversations)} chats, {len(pending)} pending, {'drafted' if draft_mode else 'sent'} {sent}"
        + (", wake in 15s" if wake_soon else ""),
    )
    if sent:
        print(f"auto-responder {'drafted' if draft_mode else 'sent'} {sent} replies")


def _auto_responder_watcher() -> None:
    while not _auto_responder_stop.is_set():
        _auto_responder_wake.wait(AUTO_RESPONDER_POLL_SECONDS)
        _auto_responder_wake.clear()
        if _auto_responder_stop.is_set():
            return
        try:
            _auto_responder_once()
        except ApiError as exc:
            _set_auto_responder_status(error=exc.message)
            _auto_responder_event("error", exc.message)
            print(f"auto-responder check failed: {exc.message}")
        except Exception as exc:
            _set_auto_responder_status(error="Could not run auto-responder check.")
            _auto_responder_event("error", f"crash: {exc!r}")
            print("auto-responder check failed")


def _desktop_notify(title: str, body: str) -> None:
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    proc = subprocess.run(
        ["notify-send", "-a", "Nearby", "--", title[:80], body[:180]],
        env=env,
        capture_output=True,
        timeout=5,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:120]
        raise ApiError(502, err or "Desktop notification failed.")


def notify_test() -> None:
    _desktop_notify("Nearby", "Keyword alerts are working.")
    _set_status(error="", ok=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _watch_once() -> None:
    if not _explicit:
        return
    cfg = load_notify()
    if not cfg["enabled"]:
        return
    if not cfg["words"]:
        _set_status(error="Add at least one keyword.")
        return
    inbox = fetch_inbox()
    state = load_state_notify()
    seeded = bool(state.get("seeded"))
    fresh, seen = diff_inbox(inbox.get("conversations") or [], state.get("seen") or {}, seeded)
    community_seen = set(state.get("community") or [])
    community_ids = list(community_seen)
    hits: list[str] = []
    if seeded:
        for item in fresh:
            preview = item.get("preview") if isinstance(item.get("preview"), str) else ""
            word = _match_word(preview, cfg["words"])
            if word:
                hits.append(word)
        try:
            for msg in fetch_community().get("messages") or []:
                mid = msg.get("id")
                if not isinstance(mid, str):
                    continue
                if mid not in community_seen and not msg.get("mine") and not msg.get("deleted"):
                    body = msg.get("body") if isinstance(msg.get("body"), str) else ""
                    word = _match_word(body, cfg["words"])
                    if word:
                        hits.append(word)
                if USER_ID_RE.match(mid) and mid not in community_ids:
                    community_ids.append(mid)
        except ApiError:
            pass
    else:
        try:
            for msg in fetch_community().get("messages") or []:
                mid = msg.get("id")
                if isinstance(mid, str) and USER_ID_RE.match(mid):
                    community_ids.append(mid)
        except ApiError:
            pass
    community_ids = community_ids[-400:]
    if not seeded:
        _write_private(
            NOTIFY_STATE,
            {"seeded": True, "seen": seen, "community": community_ids},
        )
        print(f"notify seeded {len(seen)} chats")
        _set_status(error="")
        return
    unique = []
    for word in hits:
        if word not in unique:
            unique.append(word)
    for word in unique[:5]:
        if _alexa_stop.is_set():
            return
        _desktop_notify("Nearby", f"A chat mentioned “{word}”.")
    _write_private(
        NOTIFY_STATE,
        {"seeded": True, "seen": seen, "community": community_ids},
    )
    if unique:
        _set_status(error="", ok=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        print(f"notify matched {len(unique)}")
    else:
        _set_status(error="")


def _alexa_watcher() -> None:
    while not _alexa_stop.is_set():
        _alexa_wake.wait(ALEXA_POLL_SECONDS)
        _alexa_wake.clear()
        if _alexa_stop.is_set():
            return
        try:
            _watch_once()
        except ApiError as exc:
            _set_status(error=exc.message)
            print(f"alexa check failed: {exc.message}")
        except Exception:
            _set_status(error="Could not check for new messages.")
            print("alexa check failed")


def fetch_place_media(rel: str) -> tuple[bytes, str]:
    ref = _place_photo_ref("https://" + PLACE_HOST + "/" + urllib.parse.unquote(rel.lstrip("/")))
    if not ref:
        raise ApiError(404, "Not found.")
    return _fetch_image(f"https://{PLACE_HOST}/{ref}")


def fetch_media(kind: str, user_id: str, filename: str) -> tuple[bytes, str]:
    if kind not in ("profile", "chat") or not USER_ID_RE.match(user_id) or not FILE_RE.match(filename):
        raise ApiError(404, "Not found.")
    names = [filename]
    if kind == "profile":
        full = re.sub(r"-thumb(\.[^.]+)$", r"\1", filename)
        names = [full] if full == filename else [full, filename]
    host = "profile.sniffiesassets.com" if kind == "profile" else "chat.sniffiesassets.com"
    last_error = ApiError(404, "Photo unavailable.")
    for name in names:
        if not FILE_RE.match(name):
            continue
        try:
            return _fetch_image(f"https://{host}/{user_id}/{name}")
        except ApiError as exc:
            last_error = exc
    raise last_error


def _fetch_image(url: str, *, redirects: bool = True) -> tuple[bytes, str]:
    proc = subprocess.run(
        [
            "curl",
            "--http2",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            "--max-redirs",
            "5" if redirects else "0",
            "-D",
            "-",
            url,
        ],
        capture_output=True,
    )
    raw = proc.stdout
    split = raw.find(b"\r\n\r\n")
    if split < 0:
        raise ApiError(502, "Photo request failed.")
    header = raw[:split].decode("latin1", "replace")
    body = raw[split + 4 :]
    status = 0
    ctype = "image/jpeg"
    first = header.splitlines()[0] if header else ""
    parts = first.split()
    if len(parts) >= 2 and parts[1].isdigit():
        status = int(parts[1])
    for line in header.splitlines():
        if line.lower().startswith("content-type:"):
            ctype = line.split(":", 1)[1].strip()
    if status != 200 or not ctype.startswith("image/"):
        raise ApiError(404, "Photo unavailable.")
    return body, ctype


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        # Route only. Never log bodies, cookies, or ids.
        path = self.path.split("?", 1)[0]
        if path.startswith("/media/"):
            path = "/media"
        elif path.startswith("/api/thread"):
            path = "/api/thread"
        print(f"{self.address_string()} {self.command} {path}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 20_000:
            raise ApiError(413, "Request is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode())
        except json.JSONDecodeError as exc:
            raise ApiError(400, "Expected JSON.") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "Expected an object.")
        return data

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/":
                page = (STATIC / "index.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")
                return
            if path == "/api/update":
                self._json(200, update_check())
                return
            if path == "/api/app-config":
                self._json(200, app_config_public())
                return
            if path == "/api/health":
                self._json(200, {"ok": True})
                return
            if path == "/api/session":
                self._json(200, session_public())
                return
            if path == "/api/location":
                if not _sniffies_in() and not _grindr_in():
                    raise ApiError(401, "Log in.")
                self._json(200, location_public())
                return
            if path == "/api/geocode":
                self._json(200, search_places((query.get("q") or [""])[0]))
                return
            if path == "/vendor/leaflet.js":
                self._send(200, (STATIC / "vendor" / "leaflet.js").read_bytes(), "text/javascript; charset=utf-8")
                return
            if path == "/vendor/leaflet.css":
                self._send(200, (STATIC / "vendor" / "leaflet.css").read_bytes(), "text/css; charset=utf-8")
                return
            if path == "/vendor/maplibre-gl.js":
                self._send(200, (STATIC / "vendor" / "maplibre-gl.js").read_bytes(), "text/javascript; charset=utf-8")
                return
            if path == "/vendor/maplibre-gl.css":
                self._send(200, (STATIC / "vendor" / "maplibre-gl.css").read_bytes(), "text/css; charset=utf-8")
                return
            if path == "/logo.png":
                self._send(200, (STATIC / "logo.png").read_bytes(), "image/png")
                return
            if path == "/logo.svg":
                self._send(200, (STATIC / "logo.svg").read_bytes(), "image/svg+xml")
                return
            if path == "/api/grid":
                fresh = (query.get("fresh") or ["0"])[0] == "1"
                self._json(200, fetch_grid(fresh, _query_filters(query)))
                return
            if path == "/api/inbox":
                if not _sniffies_in() and not _grindr_in():
                    self._json(200, {"conversations": [], "me": ""})
                else:
                    self._json(200, fetch_chats())
                _auto_responder_wake.set()
                return
            if path == "/api/community":
                self._json(200, fetch_community() if _sniffies_in() else {"messages": []})
                return
            if path == "/api/places":
                fresh = (query.get("fresh") or ["0"])[0] == "1"
                self._json(200, fetch_places(fresh) if _sniffies_in() else {"places": []})
                return
            if path == "/api/place":
                self._json(200, fetch_place((query.get("placeId") or [""])[0]))
                return
            if path == "/api/notify/settings":
                self._json(200, notify_public())
                return
            if path == "/api/auto-responder/settings":
                self._json(200, auto_responder_public())
                return
            if path == "/api/auto-responder/draft":
                self._json(200, auto_responder_draft((query.get("conversationId") or [""])[0]))
                return
            if path == "/api/auto-responder/debug":
                self._json(200, auto_responder_debug())
                return
            if path == "/api/auto-responder/photos":
                owner = ""
                if _sniffies_in():
                    try:
                        owner = me()
                    except ApiError:
                        owner = ""
                self._json(200, {"me": owner, "photos": list(load_auto_responder().get("autoResponderPhotos") or [])})
                return
            if path == "/api/thread":
                conversation_id = (query.get("conversationId") or [""])[0]
                before = (query.get("before") or [None])[0]
                self._json(200, fetch_thread(conversation_id, before))
                return
            if path == "/api/photos":
                self._json(200, fetch_photos())
                return
            if path == "/api/profile":
                self._json(200, fetch_profile((query.get("userId") or [""])[0]))
                return
            if path == "/api/profiles/batch":
                ids = query.get("ids") or []
                self._json(200, fetch_profiles_batch(ids))
                return
            if path == "/api/me/profile":
                self._json(200, fetch_me_profile())
                return
            if path == "/api/me/profile/images":
                self._json(200, fetch_me_profile_images())
                return
            if path.startswith("/media/gfile/"):
                token = path[len("/media/gfile/") :]
                url = _grindr_files.get(token) if re.fullmatch(r"[0-9a-f]{32}", token) else None
                if not url:
                    raise ApiError(404, "Not found.")
                body, ctype = _fetch_image(url, redirects=False)
                self._send(200, body, ctype)
                return
            if path.startswith("/media/grindr/"):
                body, ctype = fetch_grindr_photo(urllib.parse.unquote(path[len("/media/grindr/") :]))
                self._send(200, body, ctype)
                return
            if path.startswith("/media/place/"):
                body, ctype = fetch_place_media(path[len("/media/place/") :])
                self._send(200, body, ctype)
                return
            if path.startswith("/media/"):
                bits = path.split("/")
                # /media/{profile|chat}/{userId}/{file}
                if len(bits) != 5:
                    raise ApiError(404, "Not found.")
                body, ctype = fetch_media(bits[2], bits[3], bits[4])
                self._send(200, body, ctype)
                return
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._json(500, {"error": "Something went wrong loading that."})

    def _post_me_profile_photo(self, parsed: urllib.parse.ParseResult) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > PHOTO_LIMIT:
            self.close_connection = True
            raise ApiError(413, "Photo must be under 8 MB.")
        declared = self.headers.get("Content-Type") or ""
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, "Photo upload was cut off.")
        query = urllib.parse.parse_qs(parsed.query)
        taken = (query.get("takenOnGrindr") or [""])[0] == "1"
        content_type = _image_type(data, declared)
        self._json(200, upload_me_profile_photo(data, content_type, taken))

    def _post_auto_responder_photo(self) -> None:
        if not _sniffies_in():
            raise ApiError(401, "Log in.")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > PHOTO_LIMIT:
            self.close_connection = True
            raise ApiError(413, "Photo must be under 8 MB.")
        declared = self.headers.get("Content-Type") or ""
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, "Photo upload was cut off.")
        content_type = _image_type(data, declared)
        filename = _upload_chat_photo(data, content_type)
        cfg = load_auto_responder()
        photos = list(cfg.get("autoResponderPhotos") or [])
        if filename not in photos:
            photos.append(filename)
            cfg["autoResponderPhotos"] = photos
            _save_auto_responder(cfg)
        self._json(200, {"filename": filename})

    def _post_photo(self, parsed: urllib.parse.ParseResult) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > PHOTO_LIMIT:
            self.close_connection = True
            raise ApiError(413, "Photo must be under 8 MB.")
        declared = self.headers.get("Content-Type") or ""
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, "Photo upload was cut off.")
        query = urllib.parse.parse_qs(parsed.query)
        if (query.get("scope") or [""])[0] == "community":
            caption = (query.get("caption") or [""])[0]
            self._json(200, send_community_photo(data, declared, caption))
            return
        conversation_id = (query.get("conversationId") or [""])[0]
        user_id = (query.get("userId") or [""])[0]
        if _is_grindr_chat(conversation_id, user_id):
            content_type = _image_type(data, declared)
            media_id = _upload_grindr_chat_photo(data, content_type)
            self._json(200, _send_grindr_image(conversation_id, user_id, media_id))
            return
        self._json(200, send_chat_photo(conversation_id, user_id, data, declared))

    def do_POST(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/photo":
                self._post_photo(parsed)
                return
            if parsed.path == "/api/auto-responder/photos":
                self._post_auto_responder_photo()
                return
            if parsed.path == "/api/me/profile/images":
                self._post_me_profile_photo(parsed)
                return
            data = self._read_json()
            if parsed.path == "/api/login":
                email = str(data.get("email") or "")
                password = str(data.get("password") or "")
                if str(data.get("network") or "sniffies") == "grindr":
                    self._json(200, grindr_login(email, password))
                else:
                    self._json(200, login(email, password))
                return
            if parsed.path == "/api/logout":
                self._json(200, logout(str(data.get("network") or "both")))
                return
            if parsed.path == "/api/inbox/more":
                earliest = str(data.get("earliest") or "")
                if not earliest:
                    raise ApiError(400, "Missing cursor.")
                self._json(200, fetch_more(earliest))
                return
            if parsed.path == "/api/location":
                self._json(
                    200,
                    set_location(
                        str(data.get("mode") or ""),
                        data.get("lat"),
                        data.get("lng"),
                    ),
                )
                return
            if parsed.path == "/api/reply":
                result = send_reply(
                    str(data.get("conversationId") or ""),
                    str(data.get("userId") or ""),
                    str(data.get("body") or ""),
                )
                self._json(200, result)
                return
            if parsed.path == "/api/community":
                self._json(200, send_community(str(data.get("body") or "")))
                return
            if parsed.path == "/api/place/message":
                self._json(200, send_place_message(str(data.get("placeId") or ""), str(data.get("body") or "")))
                return
            if parsed.path == "/api/photo/send":
                filename = str(data.get("file") or "")
                if str(data.get("scope") or "") == "community":
                    self._json(200, send_saved_community_photo(filename, str(data.get("body") or "")))
                    return
                self._json(
                    200,
                    send_chosen_photo(
                        str(data.get("conversationId") or ""),
                        str(data.get("userId") or ""),
                        filename,
                        data.get("mediaId"),
                    ),
                )
                return
            if parsed.path == "/api/react":
                self._json(
                    200,
                    react_to_message(
                        str(data.get("conversationId") or ""),
                        str(data.get("userId") or ""),
                        str(data.get("messageId") or ""),
                        str(data.get("reaction") or ""),
                    ),
                )
                return
            if parsed.path == "/api/read":
                mark_read(
                    str(data.get("conversationId") or ""),
                    str(data.get("userId") or ""),
                    str(data.get("messageId") or ""),
                )
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/notify/settings":
                self._json(200, update_notify(data))
                return
            if parsed.path == "/api/notify/test":
                notify_test()
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/auto-responder/settings":
                self._json(200, update_auto_responder(data))
                return
            if parsed.path == "/api/auto-responder/targets":
                conversation_id = str(data.get("conversationId") or "").strip()
                enabled = bool(data.get("enabled"))
                self._json(200, update_auto_responder_target(conversation_id, enabled))
                return
            if parsed.path == "/api/auto-responder/test":
                self._json(200, auto_responder_test())
                return
            if parsed.path == "/api/auto-responder/suggest":
                self._json(200, auto_responder_suggest(str(data.get("conversationId") or "")))
                return
            if parsed.path == "/api/auto-responder/dismiss":
                self._json(200, auto_responder_dismiss(str(data.get("conversationId") or "")))
                return
            if parsed.path == "/api/auto-responder/once":
                _auto_responder_once()
                self._json(200, auto_responder_public())
                return
            if parsed.path == "/api/account/switch":
                index = data.get("index")
                if not isinstance(index, int):
                    raise ApiError(400, "Unknown account.")
                self._json(200, switch_account(index))
                return
            if parsed.path == "/api/me/profile":
                if data.get("displayName") is not None or data.get("aboutMe") is not None:
                    self._json(200, update_me_profile(data))
                else:
                    self._json(200, patch_me_profile(data))
                return
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong."})

    def do_PUT(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/me/profile":
                self._json(200, update_me_profile(self._read_json()))
                return
            if parsed.path == "/api/me/profile/images":
                self._json(200, set_me_profile_images(self._read_json()))
                return
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong."})

    def do_PATCH(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/me/profile":
                self._json(200, patch_me_profile(self._read_json()))
                return
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong."})

    def do_DELETE(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/me/profile/images":
                self._json(200, delete_me_profile_images(self._read_json()))
                return
            if parsed.path == "/api/auto-responder/photos":
                body = self._read_json()
                filename = str(body.get("filename") or "").strip()
                if not filename:
                    raise ApiError(400, "filename is required.")
                self._json(200, delete_auto_responder_photo(filename))
                return
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong."})


def start_server(port: int | None = None) -> ThreadingHTTPServer:
    """Load the session, bind, and start background watchers. Caller runs serve_forever()."""
    if not getattr(_sys, "frozen", False):
        os.chdir(Path(__file__).resolve().parent)
    _load_session()
    server = ThreadingHTTPServer((HOST, port or PORT), Handler)
    server.request_queue_size = 64
    _alexa_wake.set()
    _auto_responder_wake.set()
    threading.Thread(target=_alexa_watcher, name="alexa", daemon=True).start()
    threading.Thread(target=_auto_responder_watcher, name="auto-responder", daemon=True).start()
    return server


def main() -> None:
    server = start_server()
    print(f"Sniffies inbox at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    finally:
        _alexa_stop.set()
        _alexa_wake.set()
        _auto_responder_stop.set()
        _auto_responder_wake.set()
        try:
            if _auto_responder_timer is not None:
                _auto_responder_timer.cancel()
        except Exception:
            pass


if __name__ == "__main__":
    main()
