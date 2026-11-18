"""
Spotify Lead Finder — find artists to pitch, with their Instagram & email.

A small local web app (Streamlit). Run it with:
    streamlit run app.py

No Spotify account or developer credentials needed — it uses the same public
web-player data your browser uses, at a polite pace.
"""
from __future__ import annotations

import datetime as _dt
import io
import re

import pandas as pd
import streamlit as st

import spotify_lib as S

st.set_page_config(page_title="Spotify Lead Finder", page_icon="🎧", layout="wide")

# ------------------------------------------------------------------ constants
GENRE_PRESETS = {
    "R&B / soul": ["alt r&b", "underground r&b", "neo soul", "genre:\"r&b\""],
    "Hip-hop": ["underground hip hop", "melodic rap", "genre:\"hip hop\"", "trap"],
    "Pop / indie": ["bedroom pop", "indie pop", "singer-songwriter"],
    "Afro / dance": ["afrobeats", "amapiano", "dancehall"],
}

EUROPE = {"GB", "IE", "FR", "DE", "ES", "IT", "NL", "BE", "SE", "NO",
          "DK", "FI", "PL", "PT", "AT", "CH", "CZ", "GR"}
US_CA = {"US", "CA"}

TIERS = ["<1k", "1k–10k", "10k–100k", "100k+"]


def tier_of(monthly_listeners) -> str:
    if monthly_listeners is None:
        return "unknown"
    if monthly_listeners < 1_000:
        return "<1k"
    if monthly_listeners < 10_000:
        return "1k–10k"
    if monthly_listeners < 100_000:
        return "10k–100k"
    return "100k+"


def region_set(choice: str, custom: list[str]) -> set[str] | None:
    if choice == "US + Canada":
        return US_CA
    if choice == "UK + Europe":
        return EUROPE
    if choice == "Custom countries":
        return {c.strip().upper() for c in custom if c.strip()}
    return None  # global


def matches_region(artist: dict, allowed: set[str] | None) -> bool:
    if not allowed:
        return True
    return bool(set(artist.get("top_countries") or []) & allowed)


# Spotify has no 'artist country' field, so we guess the artist's home base
# from their bio ('based in Lagos', 'Atlanta artist', 'Nigerian singer'...)
# and fall back to their #1 listener city (for small artists that is usually
# their hometown scene anyway).
CITY_COUNTRY = {
    "los angeles": "US", "new york": "US", "atlanta": "US", "chicago": "US",
    "houston": "US", "miami": "US", "detroit": "US", "seattle": "US",
    "phoenix": "US", "dallas": "US", "tampa": "US", "orlando": "US",
    "toronto": "CA", "vancouver": "CA", "montreal": "CA",
    "london": "GB", "manchester": "GB", "birmingham": "GB", "bristol": "GB",
    "paris": "FR", "berlin": "DE", "amsterdam": "NL", "stockholm": "SE",
    "madrid": "ES", "milan": "IT", "dublin": "IE", "brussels": "BE",
    "addis ababa": "ET", "lagos": "NG", "abuja": "NG", "nairobi": "KE",
    "accra": "GH", "kumasi": "GH", "johannesburg": "ZA", "cape town": "ZA",
    "durban": "ZA", "pretoria": "ZA", "kampala": "UG", "dar es salaam": "TZ",
    "kigali": "RW", "khartoum": "SD", "harare": "ZW", "abidjan": "CI",
    "dakar": "SN", "cairo": "EG", "casablanca": "MA", "kinshasa": "CD",
    "kingston": "JM", "sydney": "AU", "melbourne": "AU", "auckland": "NZ",
    "tokyo": "JP", "seoul": "KR", "mumbai": "IN", "delhi": "IN",
    "dubai": "AE", "istanbul": "TR",
}
NATIONALITY = {
    "american": "US", "british": "GB", "english": "GB", "scottish": "GB",
    "welsh": "GB", "irish": "IE", "canadian": "CA", "nigerian": "NG",
    "ghanaian": "GH", "kenyan": "KE", "ethiopian": "ET", "eritrean": "ET",
    "south african": "ZA", "ugandan": "UG", "tanzanian": "TZ", "australian": "AU",
    "jamaican": "JM", "french": "FR", "german": "DE", "dutch": "NL",
    "swedish": "SE", "spanish": "ES", "italian": "IT", "egyptian": "EG",
    "moroccan": "MA",
}


def guess_location(artist: dict) -> str:
    """Best guess of where the artist is based ('Lagos, NG'), or '' if unknown."""
    name = (artist.get("name") or "").lower()
    bio = (artist.get("biography") or "").lower().replace(name, " ")
    for city, cc in CITY_COUNTRY.items():
        if re.search(rf"\b{re.escape(city)}\b", bio):
            return f"{city.title()}, {cc}"
    for nat, cc in NATIONALITY.items():
        if re.search(rf"\b{re.escape(nat)}\b", bio):
            return f"{nat.title()}, {cc}"
    cities = artist.get("top_cities") or []
    if cities and cities[0].get("city"):
        c = cities[0]
        return f"{str(c['city']).title()}, {c.get('country') or '?'} (audience)"
    return ""


def artist_row(artist: dict, source: str) -> dict:
    """Flatten an enriched artist dict into one spreadsheet row."""
    top = (artist.get("top_tracks") or [{}])[0]
    ig = artist.get("instagram") or S.ig_from_bio(artist.get("biography", ""))
    ml = artist.get("monthly_listeners")
    cities = artist.get("top_cities") or []
    city_str = ", ".join(f"{c['city']} ({c['country']})" for c in cities[:3])
    snippet = re.sub(r"\s+", " ", (artist.get("biography") or ""))[:180]
    top_uri = top.get("uri") or ""
    return {
        "name": artist.get("name"),
        "tier": tier_of(ml),
        "monthly_listeners": ml,
        "last_release": S.release_age_label(S.release_age_days(artist)),
        "location": guess_location(artist),
        "instagram": ("@" + ig) if ig else "",
        "instagram_url": f"https://www.instagram.com/{ig}" if ig else "",
        "ig_followers": artist.get("ig_followers"),
        "emails": "; ".join(artist.get("emails") or []),
        "top_track": top.get("title") or "",
        "top_track_plays": top.get("playcount"),
        "top_track_url": f"https://open.spotify.com/track/{top_uri.split(':')[-1]}"
                         if top_uri else "",
        "top_cities": city_str,
        "countries": ", ".join(artist.get("top_countries") or []),
        "world_rank": artist.get("world_rank"),
        "verified": "yes" if artist.get("verified") else "",
        "bio_snippet": snippet,
        "spotify_url": artist.get("spotify_url", ""),
        "source": source,
        "added_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ------------------------------------------------------------------ session state
if "results" not in st.session_state:
    st.session_state.results = []
if "seen_ids" not in st.session_state:
    st.session_state.seen_ids = set()
# default artist size filter: keep artists with 1,000–50,000 monthly listeners
if "min_ml" not in st.session_state:
    st.session_state["min_ml"] = 1_000
if "max_ml" not in st.session_state:
    st.session_state["max_ml"] = 50_000
# default Instagram follower filter: keep artists with >= 1,000 IG followers
if "ig_min" not in st.session_state:
    st.session_state["ig_min"] = 1_000


def enrich_artists(uris: list[tuple[str, str]], source: str,
                   allowed_region: set[str] | None, lo: int, hi: int,
                   ig_min: int, skip_prod: bool, rel_days: int,
                   ig_days: int, delay: float) -> None:
    """Fetch overview for each artist, filter, append rows. Shows progress."""
    sp = S.SpotifyAnon(polite_delay=delay)
    prog = st.progress(0.0, text="Starting…")
    hits, skipped_region, skipped_range, skipped_prod, skipped_ig, errors = 0, 0, 0, 0, 0, 0
    skipped_stale = skipped_quiet = 0
    for i, (uri, name) in enumerate(uris, 1):
        aid = uri.split(":")[-1]
        prog.progress(i / len(uris), text=f"Enriching {i}/{len(uris)} — {name}")
        if aid in st.session_state.seen_ids:
            continue
        try:
            artist = S.artist_overview(sp, uri)
        except Exception as e:  # noqa: BLE001
            errors += 1
            st.session_state["last_errors"] = st.session_state.get("last_errors", []) + [
                f"{name}: {e}"]
            continue
        if not artist:
            continue
        st.session_state.seen_ids.add(aid)
        if not matches_region(artist, allowed_region):
            skipped_region += 1
            continue
        ml = artist.get("monthly_listeners")
        if ml is None or not (lo <= ml <= hi):
            skipped_range += 1
            continue
        if skip_prod and S.is_producer(artist):        # Spotify bio / name — free, no IG call
            skipped_prod += 1
            continue
        if rel_days and not S.passes_release_recency(artist, rel_days):
            skipped_stale += 1          # free: date came with the overview call
            continue
        reason, _n = S.check_instagram(artist, ig_min, delay=delay,
                                       skip_producers=skip_prod)
        if reason == "producer":
            skipped_prod += 1
            continue
        if reason in ("no_ig", "low"):
            skipped_ig += 1
            continue
        lp = artist.get("ig_last_post_days")
        if ig_days and lp is not None and lp > ig_days:
            skipped_quiet += 1
            continue
        st.session_state.results.append(artist_row(artist, source))
        hits += 1
    prog.progress(1.0, text="Done")
    st.toast(f"Added {hits} artists (skipped: {skipped_region} region, "
             f"{skipped_range} size, {skipped_prod} producers, "
             f"{skipped_stale} no release in {rel_days}d, {skipped_quiet} quiet on IG, "
             f"{skipped_ig} instagram, {errors} errors)")


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("⚙️ Filters")
    region_choice = st.selectbox("Audience region", ["Global", "US + Canada",
                                                     "UK + Europe", "Custom countries"])
    custom_countries = []
    if region_choice == "Custom countries":
        custom_countries = st.text_input("Country codes (comma separated)",
                                         "US, CA, GB, AU").split(",")

    st.subheader("💰 Artist size")
    pc1, pc2, pc3 = st.columns(3)
    if pc1.button("1k–50k", use_container_width=True,
                  help="Keep artists with 1,000–50,000 monthly listeners"):
        st.session_state["min_ml"] = 1_000
        st.session_state["max_ml"] = 50_000
    if pc2.button("2k–50k", use_container_width=True,
                  help="Keep artists with 2,000–50,000 monthly listeners"):
        st.session_state["min_ml"] = 2_000
        st.session_state["max_ml"] = 50_000
    if pc3.button("Any", use_container_width=True,
                  help="No size limit — keep everyone"):
        st.session_state["min_ml"] = 0
        st.session_state["max_ml"] = 10_000_000

    min_ml = st.number_input("Minimum monthly listeners", 0, 10_000_000,
                             step=500, key="min_ml")
    max_ml = st.number_input("Maximum monthly listeners", 0, 10_000_000,
                             step=1_000, key="max_ml")
    if min_ml > max_ml:
        st.warning("Min is bigger than max — using it as the max.")
        max_ml = min_ml
    st.caption("An artist is only saved if their monthly listeners fall "
                "inside this range. Change the numbers or tap a preset above.")

    st.subheader("📸 Instagram")
    ig_min = st.number_input("Minimum Instagram followers", 0, 100_000_000,
                             step=100, key="ig_min")
    st.caption("Artists with fewer followers are skipped (0 = filter off, "
                "no Instagram lookups). Artists with no Instagram linked count "
                "as 0. If Instagram refuses the lookup, the artist is kept "
                "with an unknown count — you never lose a lead to a block.")

    st.subheader("🎯 Activity filter")
    recency_pick = st.selectbox(
        "Latest release within",
        ["Off", "60 days", "90 days (recommended)", "120 days"], index=2,
        help="Artists in a release cycle (promoting a song, planning the next "
             "one) have budget and urgency; artists who have not dropped "
             "anything in months are usually dormant. 90 days = released "
             "music in the last 3 months — singles, albums, compilations and "
             "features on other artists' releases all count. Artists with "
             "unknown release dates are kept.")
    rel_days = {"Off": 0, "60 days": 60,
                "90 days (recommended)": 90, "120 days": 120}[recency_pick]
    ig_recency_pick = st.selectbox(
        "Last Instagram post within",
        ["Off", "30 days (recommended)"], index=1,
        help="An artist who released music but never posts is not promoting "
             "right now — you want artists who are currently visible. Unknown "
             "(IG blocked or no data) artists are always kept. Needs the "
             "Instagram lookups to run, i.e. the follower minimum above must "
             "be over 0.")
    ig_days = {"Off": 0, "30 days (recommended)": 30}[ig_recency_pick]
    st.caption("Active artists buy beats — dormant ones don't. 90 days for "
                "releases and 30 days for Instagram posts are the sweet spot: "
                "7 days is too narrow (misses artists 3 weeks into promo), "
                "180 is too broad (6-month-old releases are usually stale).")

    st.subheader("🚫 Producer filter")
    skip_prod = st.checkbox(
        "Skip artists who are also producers / engineers", value=True,
        help="Checks the Spotify bio and artist name always, plus the Instagram "
             "bio and display name whenever the Instagram lookup runs. Looks for "
             "words like producer, engineer, beatmaker. Credits like 'produced "
             "by …' do NOT trigger it.")
    st.caption("Producers are your competition, not your customers — bios saying "
                "'producer', 'engineer' or 'beatmaker' get skipped automatically.")

    st.divider()
    st.caption("Be polite: delays keep you unblocked.")
    delay = st.slider("Delay between lookups (seconds)", 0.5, 5.0, 1.2, 0.1)

    st.divider()
    if st.button("🧹 Clear all results"):
        st.session_state.results = []
        st.session_state.seen_ids = set()
        st.rerun()

allowed = region_set(region_choice, custom_countries)

# ------------------------------------------------------------------ tabs
tab_search, tab_playlist, tab_deep, tab_results, tab_help = st.tabs(
    ["🔎 Search artists", "🎵 Mine playlists", "🔗 Deep search",
     "📋 Results", "❓ How it works"])

with tab_search:
    st.subheader("Search artists by keyword / genre")
    st.caption("Plain keywords work (\"alt r&b\"). Spotify-style filters also work, "
               "e.g. genre:\"alt r&b\" or year:2023-2025. Big artists rank first in "
               "search — for small/underground artists, use the playlist tab.")
    chosen = st.multiselect("Genre presets", list(GENRE_PRESETS.keys()),
                            default=["R&B / soul", "Hip-hop", "Pop / indie"])
    preset_terms = [t for g in chosen for t in GENRE_PRESETS[g]]
    custom_terms = st.text_input("Custom search terms (comma separated)",
                                 placeholder="e.g. underground rnb, dark r&b")
    terms = preset_terms + [t.strip() for t in custom_terms.split(",") if t.strip()]
    n_each = st.slider("Artists to collect per term", 5, 100, 15)

    if st.button("▶️ Run search", type="primary", disabled=not terms):
        sp = S.SpotifyAnon(polite_delay=delay)
        found: dict[str, tuple[str, str]] = {}
        pbar = st.progress(0.0, text="Searching…")
        for j, term in enumerate(terms, 1):
            pbar.progress((j - 1) / len(terms), text=f"Searching: {term}")
            try:
                for a in S.search_artists(sp, term, n_each):
                    found.setdefault(a["uri"], (a["uri"], a["name"]))
            except Exception as e:  # noqa: BLE001
                st.warning(f"Search failed for '{term}': {e}")
        pbar.progress(1.0, text=f"Found {len(found)} unique artists — enriching…")
        st.info(f"Found **{len(found)}** unique artists from {len(terms)} terms. "
                f"Enriching those not already in your results…")
        enrich_artists(list(found.values()), "search", allowed, min_ml, max_ml,
                       ig_min, skip_prod, rel_days, ig_days, delay)

with tab_playlist:
    st.subheader("Mine artists from playlists")
    st.caption("Playlists are genre clusters — every artist on an underground R&B "
               "playlist is exactly your target. Search playlists, tick the good ones.")
    pl_term = st.text_input("Playlist search term", "underground rnb")
    max_tracks = st.slider("Tracks to scan per playlist", 25, 300, 100, 25)

    picked: dict[str, tuple[str, str]] = {}
    if st.button("🔍 Find playlists") and pl_term.strip():
        sp = S.SpotifyAnon(polite_delay=delay)
        try:
            pls = S.search_playlists(sp, pl_term.strip(), 12)
            st.session_state["playlist_hits"] = pls
        except Exception as e:  # noqa: BLE001
            st.error(f"Playlist search failed: {e}")

    for p in st.session_state.get("playlist_hits", []):
        if st.checkbox(f"**{p['name']}** — by {p['owner'] or 'unknown'}",
                       key=f"pl_{p['id']}"):
            picked[p["id"]] = (p["uri"], p["name"])

    pasted = st.text_input("…or paste playlist URLs/IDs (one per line)",
                           placeholder="https://open.spotify.com/playlist/…")
    for line in [l.strip() for l in pasted.split("\n") if l.strip()]:
        pid = line.rstrip("/").split("/")[-1].split("?")[0]
        picked[pid] = (f"spotify:playlist:{pid}", pid)

    if st.button("⛏️ Mine selected playlists", type="primary", disabled=not picked):
        sp = S.SpotifyAnon(polite_delay=delay)
        artists: dict[str, tuple[str, str]] = {}
        pbar = st.progress(0.0, text="Mining…")
        for k, (uri, name) in enumerate(picked.items(), 1):
            pbar.progress((k - 1) / len(picked), text=f"Mining: {name}")
            try:
                for a in S.playlist_artists(sp, uri.split(":")[-1], max_tracks):
                    artists.setdefault(a["uri"], (a["uri"], a["name"]))
            except Exception as e:  # noqa: BLE001
                st.warning(f"Could not mine '{name}': {e}")
        pbar.progress(1.0, text=f"{len(artists)} unique artists found — enriching…")
        st.info(f"Mined **{len(artists)}** unique artists from {len(picked)} playlist(s).")
        enrich_artists(list(artists.values()), "playlist", allowed, min_ml, max_ml,
                       ig_min, skip_prod, rel_days, ig_days, delay)

with tab_deep:
    st.subheader("Deep search — crawl an artist's radio")
    st.caption("Pick an artist whose sound you like. The tool walks Spotify's "
               "'fans also like' graph: their radio first, then the radios of the "
               "artists found there. Mainstream artists are not dead ends — "
               "the crawl digs through them and follows the smaller artists "
               "hiding next to the big names.")
    seed_input = st.text_input(
        "Artist name or Spotify artist URL",
        placeholder="e.g. Brent Faiyaz  or  https://open.spotify.com/artist/…")
    c1, c2, c3 = st.columns(3)
    with c1:
        depth = st.slider("Levels deep", 1, 3, 2,
                          help="1 = just their radio · 2 = also the radios of "
                               "artists found there · 3 = one hop further")
    with c2:
        breadth = st.slider("Related per artist", 3, 20, 6,
                            help="How many 'fans also like' artists to follow per artist")
    with c3:
        max_total = st.slider("Max artists to visit", 10, 120, 40, step=5)
    dig_mainstream = st.checkbox(
        "🕳 Dig through mainstream artists", value=True,
        help="When the crawl hits a big artist, it still digs into their "
             "'fans also like' list and follows the underground artists hiding "
             "next to them. Turn off to skip big artists instead (saves crawl "
             "budget, but misses the small artists around them).")

    seed = None
    txt = seed_input.strip()
    if txt:
        m = re.search(r'(?:artist[:/])([A-Za-z0-9]{22})', txt)
        if m:                                   # direct URL/URI paste
            seed = (f"spotify:artist:{m.group(1)}", "(pasted link)")
            st.info(f"Using pasted artist link: `{m.group(1)}`")
        elif st.button("🔍 Find the artist"):
            try:
                sp0 = S.SpotifyAnon(polite_delay=delay)
                st.session_state["seed_hits"] = S.search_artists(sp0, txt, 8)
            except Exception as e:  # noqa: BLE001
                st.error(f"Search failed: {e}")

    hits = st.session_state.get("seed_hits", [])
    if hits and not seed:
        names = [a["name"] for a in hits]
        pick = st.radio("Is this the artist you like?", names, index=None,
                        horizontal=True, key="seed_choice")
        if pick:
            seed = hits[names.index(pick)]

    if txt and not seed and not hits:
        st.caption("Type a name and hit **Find the artist**, or paste an artist URL.")

    if st.button("🕸 Run deep search", type="primary", disabled=not seed):
        sp = S.SpotifyAnon(polite_delay=delay)
        pbar = st.progress(0.0, text="Crawling…")

        def _prog(level, name, done, total, queue):  # noqa: ANN001
            pbar.progress(min(done / max(total, 1), 1.0),
                          text=f"Level {level} — {name} · visited {done}/{total} · {queue} in queue")

        try:
            visits = S.deep_search(sp, seed[0], depth=depth, breadth=breadth,
                                   max_total=max_total,
                                   expand_ceiling=None if dig_mainstream else 500_000,
                                   dig_through=dig_mainstream,
                                   on_progress=_prog)
        except Exception as e:  # noqa: BLE001
            st.error(f"Deep search failed: {e}")
            visits = []
        pbar.progress(1.0, text="Done")

        added = skipped_dup = skipped_region = skipped_range = 0
        skipped_prod = skipped_ig = skipped_stale = skipped_quiet = 0
        for v in visits:
            artist = v["artist"]
            aid = (artist.get("uri") or "").split(":")[-1]
            if aid in st.session_state.seen_ids:
                skipped_dup += 1
                continue
            st.session_state.seen_ids.add(aid)
            if not matches_region(artist, allowed):
                skipped_region += 1
                continue
            ml = artist.get("monthly_listeners")
            if ml is None or not (min_ml <= ml <= max_ml):
                skipped_range += 1
                continue
            if skip_prod and S.is_producer(artist):
                skipped_prod += 1
                continue
            if rel_days and not S.passes_release_recency(artist, rel_days):
                skipped_stale += 1
                continue
            reason, _n = S.check_instagram(artist, ig_min, delay=delay,
                                       skip_producers=skip_prod)
            if reason == "producer":
                skipped_prod += 1
                continue
            if reason in ("no_ig", "low"):
                skipped_ig += 1
                continue
            lp = artist.get("ig_last_post_days")
            if ig_days and lp is not None and lp > ig_days:
                skipped_quiet += 1
                continue
            st.session_state.results.append(
                artist_row(artist, f"radio L{v['level']}") if v["level"]
                else artist_row(artist, "radio seed"))
            added += 1
        parts = [f"**{added}** new leads"]
        if skipped_dup:
            parts.append(f"{skipped_dup} already in your results")
        if skipped_region:
            parts.append(f"{skipped_region} filtered by region")
        if skipped_range:
            parts.append(f"{skipped_range} outside the {min_ml:,}–{max_ml:,} listener range")
        if skipped_prod:
            parts.append(f"{skipped_prod} producers/engineers")
        if skipped_stale:
            parts.append(f"{skipped_stale} with no release in the last "
                         f"{rel_days} days")
        if skipped_quiet:
            parts.append(f"{skipped_quiet} with no IG post in the last "
                         f"{ig_days} days")
        if skipped_ig:
            parts.append(f"{skipped_ig} below the {ig_min:,} Instagram-follower minimum")
        summary = f"Visited **{len(visits)}** artists → " + ", ".join(parts) + "."
        if added == 0:
            st.warning(summary + "\n\n💡 **Nothing new was added.** Either these artists "
                       "are already in your 📋 Results, or the sidebar filters excluded "
                       "them. Most common fix: set **Audience region → Global** in the "
                       "sidebar, widen the artist size range (tap **Any**), set "
                       "🎯 **Latest release within → Off**, lower the "
                       "Instagram minimum, or turn off the producer filter. To collect "
                       "artists again from scratch, press 🧹 Clear all results.")
        else:
            st.success(summary + " Open the 📋 Results tab.")
        with st.expander("Crawl map"):
            for lv in sorted({v["level"] for v in visits}):
                group = [f"**{v['artist']['name']}** ← {v['parent'] or 'seed'}"
                         for v in visits if v["level"] == lv]
                st.markdown(f"Level {lv} ({len(group)}): " + " · ".join(group))

with tab_results:
    st.subheader(f"Your leads ({len(st.session_state.results)})")
    if not st.session_state.results:
        st.info("No leads yet — run a search or mine a playlist first.")
    else:
        f_tiers = st.multiselect("Show only tiers", TIERS, default=TIERS, key="res_tiers")
        df = pd.DataFrame(st.session_state.results)
        df = df[df["tier"].isin(f_tiers)]
        has_ig = st.toggle("Only rows with Instagram", value=False)
        if has_ig:
            df = df[df["instagram"] != ""]
        has_mail = st.toggle("Only rows with email", value=False)
        if has_mail:
            df = df[df["emails"] != ""]
        df = df.sort_values("monthly_listeners", ascending=False, na_position="last")

        st.dataframe(
            df,
            width="stretch",
            height=520,
            column_config={
                "name": st.column_config.TextColumn("Artist", width="medium"),
                "tier": st.column_config.TextColumn("Tier", width="small"),
                "monthly_listeners": st.column_config.NumberColumn("Monthly listeners",
                                                                   format="%,d"),
                "last_release": st.column_config.TextColumn("Last release", width="small"),
                "instagram": st.column_config.TextColumn("Instagram", width="small"),
                "instagram_url": st.column_config.LinkColumn("IG link", display_text="open ↗"),
                "ig_followers": st.column_config.NumberColumn("IG followers", format="%,d"),
                "emails": st.column_config.TextColumn("Emails", width="medium"),
                "top_track": st.column_config.TextColumn("Top track", width="medium"),
                "top_track_url": st.column_config.LinkColumn("Listen", display_text="▶"),
                "top_cities": st.column_config.TextColumn("Top cities", width="medium"),
                "location": st.column_config.TextColumn("Location", width="small"),
                "spotify_url": st.column_config.LinkColumn("Spotify", display_text="open ↗"),
            },
            hide_index=True,
        )

        buf = io.StringIO()
        df.to_csv(buf, index=False)
        st.download_button("⬇️ Download CSV", buf.getvalue(),
                           file_name=f"spotify_leads_{_dt.date.today()}.csv",
                           mime="text/csv")

        # self-contained DM list: clickable links that survive tab resets
        dm = S.dm_list_html(df.to_dict("records"))
        st.download_button("⬇️ Download DM list (HTML) — tappable links",
                           dm,
                           file_name=f"dm_list_{_dt.date.today()}.html",
                           mime="text/html")
        st.caption("The DM list is a small file that stays in your Downloads forever. "
                   "On DM days, open it in Chrome and tap the links — they open the "
                   "Instagram / Spotify apps directly, even if the app tab has reset.")

with tab_help:
    st.markdown(
        """
### What this tool does
For every artist it finds, it reads the public Spotify artist page and extracts:
- **Monthly listeners** and follower count, world rank
- **Instagram / Twitter links** the artist connected to their profile (+ `IG: @handle` mentions in the bio)
- **Emails** written in the bio text (booking/contact)
- **Top cities with countries** (used for the region filter — it's where their listeners are, not their nationality)
- **Size filter** — an artist is only saved if their monthly listeners are between your sidebar Min and Max (default **1,000–50,000**: big enough to have a real audience, small enough to actually reply to DMs). Tap a preset (**1k–50k**, **2k–50k**, **Any**) or type your own numbers.
- **Instagram follower filter** — before an artist is saved, the app checks the public follower count of their Instagram profile (no login, two methods: Instagram's JSON endpoint, then the profile page). Below your minimum (default **1,000**)? Skipped. No Instagram linked? Skipped — you can't DM them anyway. Instagram refuses the lookup (blocks/rate limits)? The artist is **kept** with an unknown count so a block never costs you a lead. Set it to 0 to turn both the filter and the lookups off.
- **Producer filter** — artists who are also producers or engineers are skipped (they make beats themselves, they don't buy them). Checked on the **Spotify bio + artist name** always, and on the **Instagram bio + display name** whenever the Instagram lookup runs. It looks for standalone words like *producer(s), engineer(s), beatmaker* — credits like "produced by …" do **not** trigger it. Turn it off in the sidebar (🚫 Producer filter) if it ever catches someone you want.
- **Activity filter (release recency)** — artists in a release cycle are promoting right now, so they have budget and urgency. **Latest release within 90 days** keeps only artists who dropped music in the last 3 months — their newest single, album, compilation, or feature on someone else's release all count (the release dates come free with the artist page, no extra lookups). **Last Instagram post within 30 days** additionally drops artists who are quiet on Instagram: released 2 months ago but no post since = not promoting. Artists with unknown dates are always kept — a missing date never kills a lead. Both live in the sidebar (🎯 Activity filter). Why 90/30? 7 days is too narrow (you'd miss artists 3 weeks into their promo), 180 is too broad (6-month-old releases are usually stale).
- **Location** — best guess of where the artist is *based*, parsed from the bio ("based in Lagos", "Nigerian singer"...) or their #1 listener city, marked *(audience)*. Spotify publishes no artist-country field, so this column is how to find artists from a specific place — sort by it.
- **Top track with play count** — so you can pre-qualify before DMing
- **DM list download** — besides the CSV, the Results tab has "⬇️ Download DM list (HTML)": one small file with every lead as a card of tappable links (Instagram, email, Spotify). It stays in your Downloads and works even when the live results table resets — your DM days never need manual re-searching.

### Good to know
- **No login, no API keys** — it talks to the same endpoints the Spotify web player uses, slowly and politely.
- The region filter passes an artist if **any of their top-5 listener cities** is in your region (it's audience location, not nationality).
- Scraping is against Spotify's ToS; this is public data at human pace, but don't run it 24/7.
- If searches suddenly fail, Spotify probably rotated an internal key — press **🛠 Repair** below (it also re-syncs the token clock, which fixes "HTTP 400" errors on PCs whose clock is off).
        """)
    if st.button("🛠 Repair"):
        try:
            S.HASHES.update(S.refresh_hashes())
            n = len(S.refresh_totp_secrets())
            st.success(f"Repaired: query hashes + {n} token keys refreshed from Spotify's live site. Try your search again.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not refresh: {e}")
    errs = st.session_state.get("last_errors", [])
    if errs:
        with st.expander("Recent errors"):
            st.write("\n".join(errs[-20:]))
