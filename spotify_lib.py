"""
spotify_lib.py — anonymous Spotify data pipeline (no developer credentials needed).

Uses the same internal web-player APIs the browser uses:
  1. Anonymous access token via /api/token (TOTP-protected, auto-computed)
  2. GraphQL pathfinder queries with persisted-query hashes

Data available: artist search, playlist search + track listing, artist overview
(monthly listeners, followers, bio, social links, top cities, top tracks).

The TOTP token is computed against Spotify's own server clock (/api/server-time),
so it keeps working even if the local PC clock is wrong — which is the #1 cause
of "HTTP 400" token failures on machines that aren't always online.

Self-healing: if Spotify rotates a persisted-query hash or a TOTP secret,
`refresh_hashes()` re-extracts them from Spotify's own JS bundles.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# --- TOTP (as computed by Spotify's web player; versions rotate, keep several) ---
TOTP_SECRETS = [
    {"secret": ',7/*F("rLJ2oxaKL^f+E1xvP@N', "version": 61},
    {"secret": 'OmE{ZA.J^":0FG\\Uz?[@WW', "version": 60},
    {"secret": '{iOFn;4}<1PFYKPV?5{%u14]M>/V0hDH', "version": 59},
]

# Persisted-query hashes (extracted from live bundles; refresh_hashes() can update them)
HASHES = {
    "searchArtists": "7bf95d754fdbe32c8b161fbbe54d1ae50974900df4dce4c8f1afcbcad153224d",
    "searchPlaylists": "d520014e748f9ea44f7707d8df1819867ac1205e8b7f3e28f22fe5fc858921b1",
    "fetchPlaylist": "21ea11a6ac75a5f3e01fd365d29c18e0a4a8b7dc09f89bbb5d73910190c6acaf",
    "queryArtistOverview": "9f8134ef565e78621f1e1793555bd6633c5ac144ae0f89604ed3ae3f80b3c8e6",
}

POLITE_DELAY = 1.2  # seconds between pathfinder calls
SERVER_TIME_URL = "https://open.spotify.com/api/server-time"
BUNDLE_PAGE = "https://open.spotify.com/artist/4q3ewBCX7sLwd24euuV69X"

_server_time_cache = {"ts": 0.0, "at": 0.0}


def server_time() -> float | None:
    """Spotify's own server clock (seconds), used for the TOTP code.

    The web player does exactly this (see 'fetch("/api/server-time")' in its
    bundle). Falls back to None so callers can use the local clock.
    """
    now = time.time()
    if _server_time_cache["ts"] and now - _server_time_cache["at"] < 25:
        return _server_time_cache["ts"]
    try:
        data = _http_json(SERVER_TIME_URL, timeout=8, retries=0)
        n = float(data["serverTime"])
        if n > 1e11:          # some deployments return milliseconds
            n /= 1000.0
        _server_time_cache.update(ts=n, at=now)
        return n
    except Exception:         # noqa: BLE001 — fall back to local clock
        return None


def _get_text(url: str, timeout: int = 30) -> str:
    """GET a URL, transparently gunzip if needed, return decoded text."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "ignore")


def _secret_bytes(s: str) -> bytes:
    """Undo Spotify's secret obfuscation (charCode ^ (idx % 33 + 9) -> decimal-digit string)."""
    nums = [ord(c) ^ (i % 33 + 9) for i, c in enumerate(s)]
    return "".join(str(n) for n in nums).encode()


def totp(secret: str, ts: float | None = None) -> str:
    """Standard RFC-6238 TOTP: SHA1, 6 digits, 30s period."""
    ts = ts if ts is not None else time.time()
    counter = int(ts // 30)
    digest = hmac.new(_secret_bytes(secret), struct.pack(">q", counter), hashlib.sha1).digest()
    o = digest[19] & 0xF
    code = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _http_json(url: str, headers: dict | None = None, timeout: int = 30,
               retries: int = 2) -> dict:
    """GET a URL and parse JSON, with gzip handling and light retries."""
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip", **(headers or {})}
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429,):          # rate limited: back off and retry
                time.sleep(2.5 * (attempt + 1))
                continue
            if e.code in (401, 400) and attempt < retries:
                time.sleep(1.0)
                continue
            raise
        except Exception as e:            # noqa: BLE001 — network hiccups
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Request failed after retries: {url} ({last_err})")


class SpotifyAnon:
    """Anonymous web-player session: token management + pathfinder GraphQL."""

    def __init__(self, polite_delay: float = POLITE_DELAY):
        self.polite_delay = polite_delay
        self._tok_lock = threading.Lock()
        self._token: str | None = None
        self._token_exp_ms: int = 0
        self._last_call = 0.0

    # ---------- token ----------
    def token(self, force_refresh: bool = False) -> str:
        with self._tok_lock:
            now_ms = int(time.time() * 1000)
            if not force_refresh and self._token and now_ms < self._token_exp_ms - 60_000:
                return self._token
            last_err: Exception | None = None
            # Pass 1: known secrets, TOTP computed on Spotify's server clock
            # (this is what makes wrong-PC-clocks harmless). Pass 2: re-extract
            # secrets from the live bundle first, in case Spotify rotated them.
            for secrets in (TOTP_SECRETS, refresh_totp_secrets()):
                ts = server_time() or time.time()
                for sec in secrets:
                    code = totp(sec["secret"], ts)
                    params = {
                        "reason": "init", "productType": "web-player",
                        "totp": code, "totpServer": code,
                        "totpVer": str(sec["version"]),
                    }
                    url = "https://open.spotify.com/api/token?" + urllib.parse.urlencode(params)
                    try:
                        data = _http_json(url)
                        tok = data.get("accessToken")
                        if tok:
                            self._token = tok
                            self._token_exp_ms = int(data.get("accessTokenExpirationTimestampMs",
                                                               now_ms + 3_600_000))
                            return tok
                        last_err = RuntimeError(f"token response missing accessToken: {str(data)[:120]}")
                    except Exception as e:  # noqa: BLE001
                        last_err = e
            raise RuntimeError(
                f"Could not obtain anonymous access token: {last_err}. "
                "Spotify may be blocking this network or rotated its token keys — "
                "try the 🛠 Repair button; if it still fails, check this PC's "
                "internet connection (and Windows clock: Settings > Time & language).")

    # ---------- graphql ----------
    def query(self, op: str, variables: dict, retry_auth: bool = True) -> dict:
        q = {
            "operationName": op,
            "variables": json.dumps(variables),
            "extensions": json.dumps({"persistedQuery": {"version": 1,
                                                         "sha256Hash": HASHES[op]}}),
        }
        url = ("https://api-partner.spotify.com/pathfinder/v1/query?"
               + urllib.parse.urlencode(q))
        wait = self.polite_delay - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()
        try:
            return _http_json(url, {"Authorization": f"Bearer {self.token()}"})
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_auth:
                self.token(force_refresh=True)
                return self.query(op, variables, retry_auth=False)
            raise


# ---------------- response parsers ----------------

def _ig_handle(url: str) -> str:
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", url or "")
    return m.group(1).rstrip("/") if m else ""


def parse_artist_overview(data: dict) -> dict | None:
    au = (data.get("data") or {}).get("artistUnion") or (data.get("data") or {}).get("artist")
    if not au:
        return None
    profile = au.get("profile") or {}
    stats = au.get("stats") or {}
    bio = ((profile.get("biography") or {}).get("text")) or ""
    links = {l.get("name", "").upper(): l.get("url", "")
             for l in (profile.get("externalLinks") or {}).get("items", [])}
    cities = [
        {"city": c.get("city"), "country": c.get("country"),
         "listeners": c.get("numberOfListeners")}
        for c in ((stats.get("topCities") or {}).get("items") or [])
    ]
    top_tracks = []
    for item in (((au.get("discography") or {}).get("topTracks")) or {}).get("items", [])[:5]:
        tr = item.get("track") or item
        top_tracks.append({
            "title": tr.get("title") or tr.get("name"),
            "uri": tr.get("uri"),
            "playcount": tr.get("playcount"),
        })
    related = [
        {"uri": (it.get("uri") or ""), "name": ((it.get("profile") or {}).get("name"))}
        for it in ((au.get("relatedContent") or {}).get("relatedArtists") or {}).get("items", [])
    ]
    artist = {
        "uri": au.get("uri"),
        "id": (au.get("uri") or "").split(":")[-1],
        "name": profile.get("name"),
        "image": ((au.get("visuals") or {}).get("avatarImage") or {}).get("url")
                 or ((au.get("headerImage") or {}).get("image") or {}).get("url"),
        "verified": bool(profile.get("verified")),
        "biography": bio,
        "monthly_listeners": stats.get("monthlyListeners"),
        "followers": stats.get("followers"),
        "world_rank": stats.get("worldRank"),
        "top_cities": cities,
        "top_countries": sorted({c["country"] for c in cities if c.get("country")}),
        "instagram": _ig_handle(links.get("INSTAGRAM", "")),
        "twitter": links.get("TWITTER", ""),
        "website": links.get("WEBSITE", "") or links.get("HOMEPAGE", ""),
        "top_tracks": top_tracks,
        "related": [r for r in related if r["uri"] and r["name"]],
        "spotify_url": f"https://open.spotify.com/artist/{(au.get('uri') or '').split(':')[-1]}",
    }
    artist["emails"] = sorted(set(re.findall(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", bio)))
    return artist


def dm_list_html(rows: list[dict]) -> str:
    """One self-contained HTML page: every lead as a card with tappable links.

    Downloaded from the Results tab, the file lives in the phone's Downloads
    forever — it survives app-tab resets and phone restarts, works offline,
    and its links open the Instagram / Spotify apps directly, so DM days
    need no manual re-searching.
    """
    from html import escape

    def _num(x) -> int | None:
        """int or None; tolerates NaN, None, '' and stray strings."""
        try:
            if x is None or x == "":
                return None
            n = float(x)
            return None if n != n else int(n)   # n != n  ->  NaN
        except (TypeError, ValueError):
            return None

    def card(r: dict) -> str:
        name = escape(str(r.get("name") or "Unnamed"))
        ml = _num(r.get("monthly_listeners"))
        ml_s = f"{ml:,} monthly listeners" if ml is not None else "monthly listeners unknown"
        loc = str(r.get("location") or "")

        track = str(r.get("top_track") or "")
        track_url = str(r.get("top_track_url") or "")
        track_line = ""
        if track and track_url:
            track_line = (f' · <a style="color:#1db954;text-decoration:none;" '
                          f'href="{escape(track_url)}">▶ {escape(track)}</a>')
        elif track:
            track_line = f" · {escape(track)}"

        head = (f'<div style="font-size:19px;font-weight:800;">{name}</div>'
                f'<div style="color:#666;font-size:13px;margin:2px 0 10px;">{ml_s}'
                + (f" · {escape(loc)}" if loc else "") + track_line + "</div>")

        pills = []
        ig_handle = str(r.get("instagram") or "").lstrip("@")
        if ig_handle:
            fc = _num(r.get("ig_followers"))
            fc_s = f" ({fc:,} followers)" if fc is not None else ""
            pills.append(
                f'<a style="display:inline-block;background:#e1306c;color:#fff;padding:10px 16px;'
                f'border-radius:999px;text-decoration:none;font-weight:700;margin:4px 6px 4px 0;" '
                f'href="https://www.instagram.com/{escape(ig_handle)}">DM · @{escape(ig_handle)}</a>'
                f'<span style="color:#666;font-size:13px;">{fc_s}</span>')
        for mail in str(r.get("emails") or "").split(";"):
            mail = mail.strip()
            if mail:
                pills.append(
                    f'<a style="display:inline-block;background:#2563eb;color:#fff;padding:10px 16px;'
                    f'border-radius:999px;text-decoration:none;font-weight:700;margin:4px 6px 4px 0;" '
                    f'href="mailto:{escape(mail)}">Email · {escape(mail)}</a>')
        sp = str(r.get("spotify_url") or "")
        if sp:
            pills.append(
                f'<a style="display:inline-block;background:#1db954;color:#fff;padding:10px 16px;'
                f'border-radius:999px;text-decoration:none;font-weight:700;margin:4px 6px 4px 0;" '
                f'href="{escape(sp)}">Spotify ↗</a>')
        return (f'<div style="border:1px solid #ddd;border-radius:14px;padding:16px;margin:12px 0;'
                f"font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#fff;\">"
                f"{head}{''.join(pills)}</div>")

    body = "".join(card(r) for r in rows) or "<p style='font-family:sans-serif;'>No leads yet.</p>"
    day = time.strftime("%Y-%m-%d")
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>DM list · {day}</title></head>"
            "<body style='max-width:640px;margin:0 auto;padding:16px;background:#fafafa;'>"
            f"<h2 style=\"font-family:sans-serif;\">DM list · {day} · {len(rows)} artists</h2>"
            f"{body}</body></html>")


# ---------------- high-level operations ----------------

def _search_section(sp: SpotifyAnon, term: str, section: str, limit: int) -> list[dict]:
    """Generic searchV2 section fetcher ('artists' or 'playlists')."""
    op = "searchArtists" if section == "artists" else "searchPlaylists"
    out, offset = [], 0
    while len(out) < limit:
        n = min(30, limit - len(out))
        r = sp.query(op, {"searchTerm": term, "offset": offset, "limit": n})
        sec = ((r.get("data", {}).get("searchV2") or {}).get(section)) or {}
        items = sec.get("items") or []
        if not items:
            break
        for it in items:
            node = it.get("data") or {}
            uri = node.get("uri") or ""
            if not uri:
                continue
            if section == "artists":
                name = (node.get("profile") or {}).get("name")
            else:
                name = node.get("name")
            if name:
                out.append({"uri": uri, "name": name, "node": node})
        offset += n
        total = sec.get("totalCount")
        if total is not None and offset >= total:
            break
        if len(items) < n:
            break
    return out[:limit]


def search_artists(sp: SpotifyAnon, term: str, limit: int = 30) -> list[dict]:
    return [{"uri": x["uri"], "name": x["name"]}
            for x in _search_section(sp, term, "artists", limit)]


def search_playlists(sp: SpotifyAnon, term: str, limit: int = 10) -> list[dict]:
    out = []
    for x in _search_section(sp, term, "playlists", limit):
        node = x["node"]
        owner = ((node.get("ownerV2") or {}).get("data") or {}).get("name") or ""
        out.append({"uri": x["uri"], "id": x["uri"].split(":")[-1],
                    "name": x["name"], "owner": owner})
    return out


def playlist_artists(sp: SpotifyAnon, playlist_id: str, max_tracks: int = 200) -> list[dict]:
    """Return unique artists from a playlist, in playlist order."""
    artists: dict[str, dict] = {}
    offset, batch = 0, 100
    while offset < max_tracks:
        r = sp.query("fetchPlaylist", {"uri": f"spotify:playlist:{playlist_id}",
                                       "offset": offset, "limit": batch,
                                       "enableWatchFeedEntrypoint": False})
        content = (((r.get("data") or {}).get("playlistV2") or {}).get("content")) or {}
        items = content.get("items") or []
        if not items:
            break
        for it in items:
            tr = (((it.get("itemV2") or {}).get("data") or {}).get("albumOfTrack") or {}) \
                 .get("artists", {}).get("items", [])
            for a in tr:
                uri = a.get("uri") or ""
                prof = a.get("profile") or {}
                if uri.startswith("spotify:artist:") and prof.get("name"):
                    artists.setdefault(uri, {"uri": uri, "name": prof["name"]})
        offset += len(items)
        if len(items) < batch:
            break
    return list(artists.values())[:max_tracks]


def artist_overview(sp: SpotifyAnon, artist_uri: str) -> dict | None:
    r = sp.query("queryArtistOverview", {"uri": artist_uri, "locale": "en",
                                         "includePrerelease": False})
    return parse_artist_overview(r)


# ---------------- instagram (public profile data, no login) ----------------

IG_APP_ID = "936619743392459"   # the public app id instagram.com itself uses
IG_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
         "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

_ig_cache: dict[str, dict] = {}        # handle -> profile dict (session-long)
_ig_state = {"last": 0.0}
_ig_lock = threading.Lock()

# Producer / engineer detector: standalone words only, so credits like
# "produced by …" in an artist's bio do NOT trigger it.
# 'engeneer' (the common misspelling) is matched too.
PRODUCER_WORDS = re.compile(
    r"\b(producers?|engineers?|engeneers?|beat\s?makers?)\b", re.I)


def producer_bio(text: str) -> bool:
    """True if a bio / display name reads like a producer or engineer."""
    return bool(PRODUCER_WORDS.search(text or ""))


def is_producer(artist: dict, ig_profile: dict | None = None) -> bool:
    """Producer-skip check: the Spotify bio (always) plus, when already
    fetched, the Instagram display name, bio and category label (the little
    tag under the profile name, e.g. 'Producer'). Word matches only —
    'produced by …' does not count."""
    texts = [artist.get("biography") or "", artist.get("name") or ""]
    if ig_profile:
        texts += [ig_profile.get("bio") or "", ig_profile.get("full_name") or "",
                  ig_profile.get("category") or "",
                  ig_profile.get("business_category") or ""]
    return any(producer_bio(t) for t in texts)


def ig_from_bio(bio: str) -> str:
    """Fallback handle finder: 'instagram.com/handle' or 'IG: @handle' in the bio."""
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]{2,30})", bio or "")
    if m:
        return m.group(1).rstrip(".")
    m = re.search(r"\b(?:IG|ig|insta)\s*[:=]\s*@([A-Za-z0-9_.]{2,30})", bio or "")
    if m:
        return m.group(1).rstrip(".")
    return ""


def _ig_pace(delay: float) -> None:
    wait = delay - (time.time() - _ig_state["last"])
    if wait > 0:
        time.sleep(wait)
    _ig_state["last"] = time.time()


def _ig_profile_api(handle: str) -> dict | None:
    """Method 1: Instagram's own web endpoint (the one the logged-out site uses)."""
    url = ("https://www.instagram.com/api/v1/users/web_profile_info/?username="
           + urllib.parse.quote(handle))
    req = urllib.request.Request(url, headers={
        "User-Agent": IG_UA,
        "X-IG-App-ID": IG_APP_ID,
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/{handle}/",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        data = json.loads(raw)
    user = (data.get("data") or {}).get("user") or {}
    if not user:
        return None
    return {
        "handle": handle,
        "followers": (user.get("edge_followed_by") or {}).get("count"),
        "bio": user.get("biography") or "",
        "full_name": user.get("full_name") or "",
        "category": user.get("category") or "",
        "business_category": user.get("business_category_name") or "",
        "private": bool(user.get("is_private")),
        "verified": bool(user.get("is_verified")),
    }


def _ig_profile_html(handle: str) -> dict | None:
    """Method 2: read profile info out of the profile page's HTML."""
    html = _get_text(f"https://www.instagram.com/{urllib.parse.quote(handle)}/")

    def _js_str(m: re.Match) -> str:      # unescape a JSON string literal
        try:
            return json.loads(f'"{m.group(1)}"')
        except Exception:                 # noqa: BLE001
            return m.group(1)

    out = {"handle": handle, "followers": None, "bio": "", "full_name": "",
           "category": "", "business_category": ""}
    m = re.search(r'"edge_followed_by":\{"count":(\d+)\}', html)
    if m:
        out["followers"] = int(m.group(1))
    else:
        m = re.search(r'content="[^"]*?([\d.,]+)\s*Followers', html)
        if m:
            try:
                out["followers"] = int(round(float(m.group(1).replace(",", ""))))
            except ValueError:
                pass
    m = re.search(r'"biography":"((?:[^"\\]|\\.)*)"', html)
    if m:
        out["bio"] = _js_str(m)
    m = re.search(r'"full_name":"((?:[^"\\]|\\.)*)"', html)
    if m:
        out["full_name"] = _js_str(m)
    m = re.search(r'"category_name":"((?:[^"\\]|\\.)*)"', html)
    if m:
        out["category"] = _js_str(m)
    m = re.search(r'"business_category_name":"((?:[^"\\]|\\.)*)"', html)
    if m:
        out["business_category"] = _js_str(m)
    return out if out["followers"] is not None else None


def instagram_profile(handle: str, delay: float = 1.5) -> dict | None:
    """Public Instagram profile info without login: followers, bio, name.

    Tries the JSON endpoint first, then the profile page. Returns None when
    Instagram refuses the request (rate limit / block) or the profile does
    not exist — callers keep the artist and mark data unknown.
    Successful results are cached for the whole session.
    """
    handle = (handle or "").strip().lstrip("@").rstrip(".")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle or ""):
        return None
    with _ig_lock:
        if handle in _ig_cache:
            return _ig_cache[handle]
        _ig_pace(max(delay, 1.0))
        prof = None
        for attempt in (0, 1):            # 0 = json API, 1 = HTML fallback
            try:
                prof = (_ig_profile_api(handle) if attempt == 0
                        else _ig_profile_html(handle))
                if prof is not None:
                    break
            except Exception:             # noqa: BLE001 — blocked / rate limited
                if attempt == 0:
                    time.sleep(1.0)       # brief breather before the fallback
        if prof is not None:
            _ig_cache[handle] = prof
        return prof


def instagram_followers(handle: str, delay: float = 1.5) -> int | None:
    """Just the follower count (thin wrapper around instagram_profile)."""
    prof = instagram_profile(handle, delay=delay)
    return prof["followers"] if prof else None


def check_instagram(artist: dict, ig_min: int, delay: float = 1.5,
                    skip_producers: bool = True) -> tuple[str, int | None]:
    """Instagram gate for one artist. Returns (reason, followers) with reason:

    - 'pass'     — save the artist
    - 'no_ig'    — no Instagram linked anywhere (only when ig_min > 0)
    - 'low'      — follower count below ig_min
    - 'producer' — producer / engineer words found in the IG bio or display name
    - 'unknown'  — Instagram refused the lookup: KEEP the artist, count unknown

    The follower count is stored on artist['ig_followers'] when known.
    ig_min <= 0 turns the follower filter and the Instagram lookups off
    (the IG-side producer check then cannot run; the Spotify-bio check in
    is_producer() still applies).
    """
    if ig_min <= 0:
        return "pass", None
    ig = artist.get("instagram") or ig_from_bio(artist.get("biography", ""))
    if not ig:
        return "no_ig", None              # no Instagram linked -> below minimum
    prof = instagram_profile(ig, delay=delay)
    if prof is None:
        return "unknown", None            # blocked -> keep the artist
    n = prof.get("followers")
    if n is not None:
        artist["ig_followers"] = n
    if skip_producers and is_producer(artist, prof):
        return "producer", n
    if n is None:
        return "unknown", None            # page without a count -> keep
    return ("pass", n) if n >= ig_min else ("low", n)


def deep_search(sp: SpotifyAnon, seed_uri: str, depth: int = 2, breadth: int = 6,
                max_total: int = 40, expand_ceiling: int | None = None,
                dig_through: bool = True, on_progress=None) -> list[dict]:
    """BFS crawl of Spotify's related-artists graph ('fans also like').

    Starts at seed_uri (level 0). Every visited artist is enriched via
    artist_overview() — which also yields that artist's own related list, used
    to keep crawling. The seed's radio (level 1) and its radios (level 2) are
    always expanded. With `dig_through=True` (default), mainstream artists are
    no longer dead ends either: the crawl digs into their related list and
    follows the smaller artists hiding next to the big names, so the deepest
    level reaches the underground scene around them. With `dig_through=False`,
    artists above `expand_ceiling` monthly listeners (from level 2 on) are
    recorded but not expanded — the old budget-saving behaviour.

    Returns a list of {"level", "parent", "artist"} in visit order.
    `on_progress(level, name, done, total, queue_len)` is called after each visit.
    """
    out: list[dict] = []
    visited = {seed_uri}
    frontier: list[tuple[int, str, str, str]] = [(0, seed_uri, "", "")]
    while frontier and len(out) < max_total:
        level, uri, name, parent = frontier.pop(0)
        try:
            ov = artist_overview(sp, uri)
        except Exception:            # noqa: BLE001 — skip dead ends, keep crawling
            continue
        if not ov:
            continue
        name = ov.get("name") or name
        if on_progress:
            on_progress(level, name, len(out), max_total, len(frontier))
        out.append({"level": level, "parent": parent, "artist": ov})
        if level >= depth:
            continue
        if not dig_through and level >= 2 and expand_ceiling and \
                (ov.get("monthly_listeners") or 0) > expand_ceiling:
            continue                 # record big artists, don't crawl through them
        for rel in (ov.get("related") or [])[:breadth]:
            if rel["uri"] not in visited:
                visited.add(rel["uri"])
                frontier.append((level + 1, rel["uri"], rel["name"], name))
    return out


def refresh_totp_secrets() -> list[dict]:
    """Re-extract TOTP secrets from Spotify's live web-player bundle (self-healing).

    Updates the module-level TOTP_SECRETS in place (newest version first) and
    returns the new list. Returns [] if extraction fails (then callers just
    keep using the built-in secrets).
    """
    try:
        html = _get_text(BUNDLE_PAGE)
        urls = re.findall(r'src="(https://[^"]+?/web-player\.[0-9a-f]+\.js)"', html)
        if not urls:                      # fallback: bundle manifest
            m = re.search(r'__CDN_FILE_URLS__" type="text/plain">([^<]+)<', html)
            if m:
                files = json.loads(base64.b64decode(m.group(1)))
                urls = [v for k, v in files.items()
                        if k.startswith("build/web-player/web-player") and k.endswith(".js")]
        pat = re.compile(r'secret:\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')'
                         r'\s*,\s*version:\s*(\d+)')
        found: list[dict] = []
        for url in dict.fromkeys(urls):
            try:
                src = _get_text(url)
            except Exception:         # noqa: BLE001
                continue
            for mm in pat.finditer(src):
                raw = mm.group(1) if mm.group(1) is not None else mm.group(2)
                secret = re.sub(r"\\(.)", r"\1", raw)   # undo JS string escapes
                entry = {"secret": secret, "version": int(mm.group(3))}
                if entry not in found:
                    found.append(entry)
            if found:
                break
        if found:
            found.sort(key=lambda x: -x["version"])
            TOTP_SECRETS[:] = found
        return found
    except Exception:             # noqa: BLE001 — never crash the app over healing
        return []


def refresh_hashes() -> dict:
    """Re-extract persisted-query hashes (and TOTP secrets) from Spotify's live JS."""
    found = dict(HASHES)
    html_src = _get_text(BUNDLE_PAGE)
    m = re.search(r'__CDN_FILE_URLS__" type="text/plain">([^<]+)<', html_src)
    if not m:
        refresh_totp_secrets()    # still worth refreshing token keys
        return found
    files = json.loads(base64.b64decode(m.group(1)))
    op_pat = re.compile(r'"([A-Za-z0-9]+)","query","([0-9a-f]{64})"')
    want = set(HASHES)
    candidates = [v for k, v in files.items()
                  if k.endswith(".js") and re.search(r"search|artist|web-player\.", k)
                  and "mobile" not in k and "vendor" not in k][:12]
    for url in candidates:
        if not want:
            break
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                src = raw.decode("utf-8", "ignore")
            for name, h in op_pat.findall(src):
                if name in want:
                    found[name] = h
                    want.discard(name)
        except Exception:  # noqa: BLE001
            continue
    refresh_totp_secrets()        # keep token keys fresh in the same pass
    return found
