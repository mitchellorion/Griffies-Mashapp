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

import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("SNIFFIES_PORT", "8765"))
STATIC = Path(__file__).resolve().parent / "static"
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
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_explicit = False
_unflagged: set[str] = set()
_own_loc: list[float] | None = None
_account_type = ""
_units = "imperial"
_country = "US"
_chat_photos: list[str] = []
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
                "author": author,
                "mine": author == user_id,
                "createdAt": raw.get("createdAt"),
                "spam": bool(raw.get("isSpam")),
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
    photos = udata.get("chatPhotos")
    if not isinstance(photos, list):
        photos = user.get("chatPhotos")
    if isinstance(photos, list):
        for name in photos:
            if isinstance(name, str):
                _note_chat_photo(name, save=False)


def _note_chat_photo(name: str, save: bool = True) -> None:
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


def session_public() -> dict:
    logged = bool(_explicit and _jar.get("access_token"))
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
    return {
        "loggedIn": logged,
        "active": _active if logged else -1,
        "canAdd": logged and len(_accounts) < 2,
        "accounts": accounts if logged else [],
    }


def _save_session() -> None:
    if not _accounts:
        _clear_session_file()
        return
    if _explicit and _jar.get("access_token") and 0 <= _active < len(_accounts):
        _accounts[_active] = _snapshot_account()
    payload = {"active": _active, "accounts": list(_accounts)}
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
    if not loaded:
        return
    _accounts = loaded
    _apply_account(_accounts[_active])
    print("session restored")


def _commit_account() -> None:
    global _active
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
            _jar.clear()
            _jar.update(jar)
            _explicit = True
            _api_host = host
            _account_email = email
            _chat_photos.clear()
            _clear_live_caches()
            _remember_account(payload)
            _commit_account()
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


def logout() -> dict:
    global _explicit, _own_loc, _account_type, _account_email, _active
    with _lock:
        if _accounts and 0 <= _active < len(_accounts):
            _accounts.pop(_active)
        if _accounts:
            _active = min(_active, len(_accounts) - 1)
            _apply_account(_accounts[_active])
            _save_session()
            return session_public() | {"ok": True}
        _jar.clear()
        _explicit = False
        _own_loc = None
        _account_type = ""
        _account_email = ""
        _active = 0
        _chat_photos.clear()
        _clear_live_caches()
    _clear_session_file()
    return {"ok": True, "loggedIn": False, "accounts": [], "canAdd": False, "active": -1}


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
    global _own_loc, _feed_raw
    if not _own_loc:
        status, payload, _ctype = api("POST", "/api/authenticate/current", {})
        if status != 200:
            raise ApiError(status, _error_message(payload, status))
        with _lock:
            _remember_account(payload)
    if not _own_loc:
        raise ApiError(502, "Your profile has no location yet.")
    lng, lat = _own_loc
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
            }
        )
    cards.sort(key=lambda card: (card["distance"] is None, card["distance"] or 0))
    return {"units": _units, "cards": cards}


def fetch_grid(fresh: bool = False) -> dict:
    now = time.time()
    with _lock:
        cached = _grid_cache.get("payload")
        cached_at = float(_grid_cache.get("at") or 0)
    if not fresh and cached and now - cached_at < GRID_TTL:
        return cached
    payload = _build_grid()
    with _lock:
        _grid_cache["at"] = time.time()
        _grid_cache["payload"] = payload
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


def send_reply(conversation_id: str, user_id: str, text: str) -> dict:
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
    if _own_loc and len(_own_loc) >= 2:
        return [float(_own_loc[0]), float(_own_loc[1])]
    status, payload, _ctype = api("POST", "/api/authenticate/current", {})
    if status != 200:
        raise ApiError(status, _error_message(payload, status))
    with _lock:
        _remember_account(payload)
    if not _own_loc:
        raise ApiError(502, "Your profile has no location yet.")
    return [float(_own_loc[0]), float(_own_loc[1])]


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
    if not _chat_photos:
        status, payload, _ctype = api("POST", "/api/authenticate/current", {})
        if status == 200:
            with _lock:
                _remember_account(payload)
    return {"me": me(), "photos": list(_chat_photos)}


def fetch_profile(user_id: str) -> dict:
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
    return {
        "id": user_id,
        "age": age,
        "photo": photos[0] if photos else None,
        "photos": photos,
        "online": _online(person),
        "blurb": blurb,
        "conversationId": conversation_id,
        "distance": distance,
        "source": "sniffies",
    }


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


def _fetch_image(url: str) -> tuple[bytes, str]:
    proc = subprocess.run(
        ["curl", "--http2", "--silent", "--show-error", "--max-time", "20", "-D", "-", url],
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
            if path == "/api/health":
                self._json(200, {"ok": True})
                return
            if path == "/api/session":
                self._json(200, session_public())
                return
            if path == "/api/grid":
                fresh = (query.get("fresh") or ["0"])[0] == "1"
                self._json(200, fetch_grid(fresh))
                return
            if path == "/api/inbox":
                self._json(200, fetch_inbox())
                return
            if path == "/api/community":
                self._json(200, fetch_community())
                return
            if path == "/api/places":
                fresh = (query.get("fresh") or ["0"])[0] == "1"
                self._json(200, fetch_places(fresh))
                return
            if path == "/api/place":
                self._json(200, fetch_place((query.get("placeId") or [""])[0]))
                return
            if path == "/api/notify/settings":
                self._json(200, notify_public())
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
        self._json(
            200,
            send_chat_photo(
                (query.get("conversationId") or [""])[0],
                (query.get("userId") or [""])[0],
                data,
                declared,
            ),
        )

    def do_POST(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/photo":
                self._post_photo(parsed)
                return
            data = self._read_json()
            if parsed.path == "/api/login":
                self._json(200, login(str(data.get("email") or ""), str(data.get("password") or "")))
                return
            if parsed.path == "/api/logout":
                self._json(200, logout())
                return
            if parsed.path == "/api/inbox/more":
                earliest = str(data.get("earliest") or "")
                if not earliest:
                    raise ApiError(400, "Missing cursor.")
                self._json(200, fetch_more(earliest))
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
                    send_saved_photo(
                        str(data.get("conversationId") or ""),
                        str(data.get("userId") or ""),
                        filename,
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
            if parsed.path == "/api/account/switch":
                index = data.get("index")
                if not isinstance(index, int):
                    raise ApiError(400, "Unknown account.")
                self._json(200, switch_account(index))
                return
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._json(500, {"error": "Something went wrong."})


def main() -> None:
    os.chdir(Path(__file__).resolve().parent)
    _load_session()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.request_queue_size = 64
    _alexa_wake.set()
    threading.Thread(target=_alexa_watcher, name="alexa", daemon=True).start()
    print(f"Sniffies inbox at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    finally:
        _alexa_stop.set()
        _alexa_wake.set()


if __name__ == "__main__":
    main()
