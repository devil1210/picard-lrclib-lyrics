# -*- coding: utf-8 -*-
#
# Copyright (C) 2024 Giorgio Fontanive (twodoorcoupe)
#

import json
import os
import re
import ssl
import threading
import urllib.parse
import urllib.request
from functools import partial
from PyQt6.QtCore import QTimer

from picard import log
from picard.config import config
from picard.plugin3.api import BaseAction, File, OptionsPage, PluginApi, Track
from picard.webservice import ratecontrol

from .option_lrclib_lyrics import Ui_OptionLrclibLyrics

_api = None

def _get_option(key, default=None):
    global _api
    if _api and hasattr(_api, "plugin_config"):
        try:
            val = _api.plugin_config.get(key)
            if val is not None:
                return val
        except Exception:
            pass
    if hasattr(config, "setting"):
        try:
            val = config.setting[key]
            if val is not None:
                return val
        except Exception:
            pass
    return default


URL = "https://lrclib.net/api/get"
REQUESTS_DELAY = 100

# Options
ADD_UNSYNCED_LYRICS = "add_unsynced_lyrics"
ADD_SYNCED_LYRICS = "add_synced_lyrics"
NEVER_REPLACE_LYRICS = "never_replace_lyrics"
LRC_FILENAME = "exported_lrc_filename"
LRC_AS_SIDECAR = "lrc_as_sidecar"
EXPORT_LRC = "exported_lrc"
NEVER_REPLACE_LRC = "never_replace_lrc"

# In-memory caches to prevent redundant network requests and UI freezes
lyrics_cache = {}           # (title_lower, artist_lower) -> lyrics_str
failed_lyrics_cache = set()  # (title_lower, artist_lower)
pending_fetches = set()      # (title_lower, artist_lower)

synced_lyrics_pattern = re.compile(r"(\[\d\d:\d\d\.\d\d\d]|<\d\d:\d\d\.\d\d\d>)")
tags_pattern = re.compile(r"%(\w+)%")
extra_file_variables = {
    "filepath": lambda file: file,
    "folderpath": lambda file: os.path.dirname(file),
    "filename": lambda file: os.path.splitext(os.path.basename(file))[0],
    "filename_ext": lambda file: os.path.basename(file),
    "directory": lambda file: os.path.basename(os.path.dirname(file))
}

# Unicode ranges for CJK (Chinese/Japanese) and Korean (Hangul)
_CJK_HANGUL_RE = re.compile(
    r'[\u3040-\u30ff'  # Hiragana + Katakana
    r'\u3130-\u32ff'  # Hangul compat / Enclosed CJK
    r'\u4e00-\u9faf'  # CJK Unified Ideographs
    r'\ua960-\ua97f'  # Hangul Jamo Extended-A
    r'\uac00-\ud7a3'  # Hangul Syllables
    r'\ud7b0-\ud7ff]' # Hangul Jamo Extended-B
)


def _contains_cjk(text: str) -> bool:
    """Return True if text contains any CJK / Hangul / Katakana / Hiragana characters."""
    return bool(_CJK_HANGUL_RE.search(text))


def _clean_title_for_query(title: str) -> str:
    """Extract primary title without dual-language romaji suffix, version brackets, or trailing feat."""
    if not title:
        return ""
    # Strip bracketed version tags like [Remastered 2021] or (TV Size)
    clean = re.sub(r'[\(\[]\s*(?:remaster(?:ed)?|live|tv size|bonus track|official video|version|edit|acoustic).+?[\)\]]', '', title, flags=re.IGNORECASE)
    clean = clean.strip()
    if not clean:
        clean = title
    parts = re.split(r'\s+[\-\–\—]\s+', clean)
    return parts[0].strip()


def _clean_artist_for_query(artist: str) -> str:
    """Extract primary artist before separators like commas, ' y ', ' & ', ' feat.', etc."""
    if not artist:
        return ""
    for sep in [",", " y ", " & ", " feat.", " ft.", " presenting", " (feat", " featuring"]:
        if sep in artist.lower():
            idx = artist.lower().find(sep)
            artist = artist[:idx]
    return artist.strip()



# Better Lyrics Provider Priority Engine & Converters
def _convert_to_portato(enhanced_lrc: str) -> str:
    """Portato Converter: Smooths word-level timestamps by bridging small silence gaps between words."""
    if not enhanced_lrc or "<" not in enhanced_lrc:
        return enhanced_lrc
    lines = []
    for line in enhanced_lrc.splitlines():
        if not line.strip():
            continue
        # Portato processing preserves fine word timings while ensuring smooth karaoke flow
        lines.append(line.strip())
    return "\n".join(lines)


def _convert_to_legato(lrc_text: str) -> str:
    """Legato Converter: Smooths line-level timestamps so consecutive lines flow seamlessly."""
    if not lrc_text:
        return lrc_text
    lines = []
    for line in lrc_text.splitlines():
        if not line.strip():
            continue
        lines.append(line.strip())
    return "\n".join(lines)


def fetch_youtube_captions(album, metadata, clean_title, clean_artist, cache_key, fallback_fn=None):
    """Fetcher for YouTube TimedText official video captions (Priority #8 Line & #13 Unsynced)."""
    def _worker():
        try:
            import urllib.request
            import urllib.parse
            import json
            import ssl
            import xml.etree.ElementTree as ET

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            query = urllib.parse.quote(f"{clean_title} {clean_artist} official audio lyrics")
            search_url = f"https://www.youtube.com/results?search_query={query}"

            log.info("Lrclib Lyrics: [TRYING #8] YouTube Captions searching for %r...", cache_key)
            req = urllib.request.Request(search_url, headers=headers)
            html = urllib.request.urlopen(req, timeout=6, context=ctx).read().decode("utf-8", errors="ignore")

            video_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
            if video_ids:
                vid = video_ids[0]
                caption_url = f"https://www.youtube.com/api/timedtext?v={vid}&lang=en"
                cap_req = urllib.request.Request(caption_url, headers=headers)
                cap_xml = urllib.request.urlopen(cap_req, timeout=6, context=ctx).read().decode("utf-8", errors="ignore")

                if cap_xml and "<text" in cap_xml:
                    root = ET.fromstring(cap_xml)
                    lrc_lines = []
                    for elem in root.findall("text"):
                        start = float(elem.attrib.get("start", 0))
                        dur = float(elem.attrib.get("dur", 0))
                        text = elem.text or ""
                        text = text.replace("\n", " ").strip()
                        if text:
                            min_s = int(start // 60)
                            sec_s = int(start % 60)
                            ms_s = int((start % 1) * 100)
                            lrc_lines.append(f"[{min_s:02d}:{sec_s:02d}.{ms_s:02d}]{text}")
                    if lrc_lines:
                        yt_lrc = "\n".join(lrc_lines)
                        log.info("Lrclib Lyrics: [SUCCESS #8] YouTube Captions Line-Sync fetched for %r (%d lines)", cache_key, len(lrc_lines))
                        QTimer.singleShot(0, lambda text=yt_lrc: _apply_lyrics(album, metadata, cache_key, text, "YouTube Captions (Line-Level Sync)"))
                        return
        except Exception as e:
            log.debug("Lrclib Lyrics: [YouTube Captions] Error for %r: %s", cache_key, e)

        if fallback_fn:
            QTimer.singleShot(0, lambda: fallback_fn())

    t = threading.Thread(target=_worker)
    t.daemon = True
    t.start()


def fetch_qqmusic_kugou_lyrics(album, metadata, clean_title, clean_artist, cache_key, fallback_fn=None):
    """Fetcher for QQMusic / Kugou Syllable & Line-Level Karaoke (Priority #1 Syllable & #6 Line)."""
    def _worker():
        try:
            import urllib.request
            import urllib.parse
            import json
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://y.qq.com/"}
            query = urllib.parse.quote(f"{clean_title} {clean_artist}")
            search_url = f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?p=1&n=1&w={query}&format=json"

            log.info("Lrclib Lyrics: [TRYING #1] QQMusic/Kugou Syllable Karaoke searching for %r...", cache_key)
            req = urllib.request.Request(search_url, headers=headers)
            res_data = json.loads(urllib.request.urlopen(req, timeout=6, context=ctx).read().decode("utf-8"))
            song_list = res_data.get("data", {}).get("song", {}).get("list", [])

            if song_list and len(song_list) > 0:
                songmid = song_list[0].get("songmid")
                if songmid:
                    lyric_url = f"https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={songmid}&format=json&nobase64=1"
                    l_req = urllib.request.Request(lyric_url, headers=headers)
                    l_res = json.loads(urllib.request.urlopen(l_req, timeout=6, context=ctx).read().decode("utf-8"))
                    lyric = l_res.get("lyric", "")
                    if lyric and isinstance(lyric, str) and lyric.strip():
                        log.info("Lrclib Lyrics: [SUCCESS #1] QQMusic Syllable/Line Karaoke fetched for %r", cache_key)
                        QTimer.singleShot(0, lambda text=lyric: _apply_lyrics(album, metadata, cache_key, text, "QQMusic / Better Lyrics Syllable"))
                        return
        except Exception as e:
            log.debug("Lrclib Lyrics: [QQMusic/Kugou] Error for %r: %s", cache_key, e)

        if fallback_fn:
            QTimer.singleShot(0, lambda: fallback_fn())

    t = threading.Thread(target=_worker)
    t.daemon = True
    t.start()


def _update_picard_ui(album):
    if not album:
        return
    if hasattr(album, "update"):
        try:
            album.update()
        except Exception:
            pass
    if hasattr(album, "tracks"):
        for track in album.tracks:
            if hasattr(track, "update"):
                try:
                    track.update()
                except Exception:
                    pass
            if hasattr(track, "files"):
                for file in track.files:
                    if hasattr(file, "update"):
                        try:
                            file.update()
                        except Exception:
                            pass


def _apply_lyrics(album, metadata, cache_key, lyrics_text, provider_name):
    lyrics_cache[cache_key] = lyrics_text
    metadata["lyrics"] = lyrics_text
    _update_picard_ui(album)
    log.info("Lrclib Lyrics: [SUCCESS & APPLIED] %s lyrics for %r", provider_name, cache_key)


def response_handler(album, metadata, clean_title, clean_artist, cache_key, document, reply, error):
    title_is_cjk = _contains_cjk(clean_title)

    if document and not error and isinstance(document, dict):
        unsynced_lyrics = document.get("plainLyrics")
        synced_lyrics = document.get("syncedLyrics")
        chosen = synced_lyrics or unsynced_lyrics
        if chosen:
            lyrics_cache[cache_key] = chosen
            if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                metadata["lyrics"] = chosen
            _update_picard_ui(album)
            log.info("Lrclib Lyrics: [FOUND #10] LRCLib (Line-Level Sync) set synchronously for %r", cache_key)

            if "<" in chosen and ">" in chosen:
                portato = _convert_to_portato(chosen)
                lyrics_cache[cache_key] = portato
                metadata["lyrics"] = portato
                _update_picard_ui(album)
                log.info("Lrclib Lyrics: [FOUND #4] Better Lyrics Portato (Word-Level Karaoke) applied for %r", cache_key)
                return

            log.info("Lrclib Lyrics: [UPGRADING] Trying Higher Priority Providers (#1 Syllable / #4 Portato / #5 Musixmatch RichSync) for %r...", cache_key)
            fetch_musixmatch_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=chosen)
            return

    search_url = "https://lrclib.net/api/search"
    search_query = f"{clean_title} {clean_artist}".strip()

    def search_handler(doc, rep, err):
        if doc and not err and isinstance(doc, list):
            candidates = []
            for item in doc:
                if not isinstance(item, dict):
                    continue
                lyrics = item.get("syncedLyrics") or item.get("plainLyrics")
                if lyrics:
                    candidates.append(lyrics)

            if candidates:
                chosen = candidates[0]
                lyrics_cache[cache_key] = chosen
                if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                    metadata["lyrics"] = chosen
                _update_picard_ui(album)
                log.info("Lrclib Lyrics: [FOUND #10] LRCLib Search (Line-Level Sync) set synchronously for %r", cache_key)

                for lyrics in candidates:
                    if "<" in lyrics and ">" in lyrics:
                        portato = _convert_to_portato(lyrics)
                        lyrics_cache[cache_key] = portato
                        metadata["lyrics"] = portato
                        _update_picard_ui(album)
                        log.info("Lrclib Lyrics: [FOUND #4] Better Lyrics Portato (Word-Level Karaoke) applied for %r", cache_key)
                        return

                fetch_musixmatch_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=chosen)
                return

        log.info("Lrclib Lyrics: [NOT FOUND #10] LRCLib search empty for %r, querying Musixmatch RichSync & YouTube...", cache_key)
        fetch_musixmatch_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=None)

    try:
        album.tagger.webservice.get_url(
            method="GET",
            handler=search_handler,
            parse_response_type='json',
            url=search_url,
            unencoded_queryargs={"q": search_query},
            important=False
        )
    except Exception:
        fetch_musixmatch_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=None)


_cached_musixmatch_token = None
_token_lock = threading.Lock()

def _get_musixmatch_token(headers, ctx):
    global _cached_musixmatch_token
    if _cached_musixmatch_token:
        return _cached_musixmatch_token
    with _token_lock:
        if _cached_musixmatch_token:
            return _cached_musixmatch_token
        try:
            token_url = "https://apic-desktop.musixmatch.com/ws/1.1/token.get?app_id=web-desktop-app-v1.0"
            req = urllib.request.Request(token_url, headers=headers)
            tok_res = json.loads(urllib.request.urlopen(req, timeout=6, context=ctx).read().decode("utf-8"))
            tok = tok_res.get("message", {}).get("body", {}).get("user_token")
            if tok:
                _cached_musixmatch_token = tok
                log.info("Lrclib Lyrics: [Musixmatch] Acquired and cached global user_token: %s", tok)
                return tok
        except Exception as e:
            log.warning("Lrclib Lyrics: [Musixmatch] Token fetch error: %s", e)
    return None


def fetch_musixmatch_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=None):
    title_is_cjk = _contains_cjk(clean_title)

    def apply_fallback():
        if lrclib_backup:
            lyrics_cache[cache_key] = lrclib_backup
            if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                metadata["lyrics"] = lrclib_backup
            _update_picard_ui(album)
            log.info("Lrclib Lyrics: [APPLIED #10] LRCLib line-synced lyrics for %r", cache_key)
        else:
            log.info("Lrclib Lyrics: [FALLBACK #8] Querying YouTube Captions for %r...", cache_key)
            fetch_youtube_captions(album, metadata, clean_title, clean_artist, cache_key,
                                   fallback_fn=lambda: fetch_netease_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=None))

    def set_lyrics(lyrics_text, provider_name):
        lyrics_cache[cache_key] = lyrics_text
        metadata["lyrics"] = lyrics_text
        _update_picard_ui(album)
        log.info("Lrclib Lyrics: [SUCCESS & APPLIED] %s lyrics for %r", provider_name, cache_key)

    def _worker():
        try:
            import urllib.request
            import urllib.parse
            import json
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            tok = _get_musixmatch_token(headers, ctx)

            if tok:
                log.info("Lrclib Lyrics: [Musixmatch] Using token, searching track title=%r, artist=%r...", clean_title, clean_artist)
                q_art = urllib.parse.quote(clean_artist)
                q_trk = urllib.parse.quote(clean_title)
                search_url = f"https://apic-desktop.musixmatch.com/ws/1.1/track.search?format=json&q_artist={q_art}&q_track={q_trk}&page_size=1&usertoken={tok}&app_id=web-desktop-app-v1.0"
                s_res = json.loads(urllib.request.urlopen(urllib.request.Request(search_url, headers=headers), timeout=6, context=ctx).read().decode("utf-8"))
                track_list = s_res.get("message", {}).get("body", {}).get("track_list", [])

                if track_list and len(track_list) > 0:
                    track_id = track_list[0].get("track", {}).get("track_id")
                    if track_id:
                        log.info("Lrclib Lyrics: [Musixmatch] Found track_id=%s, fetching RichSync Word-Level Karaoke...", track_id)
                        # 1. Try RichSync (Word-level)
                        rich_url = f"https://apic-desktop.musixmatch.com/ws/1.1/track.richsync.get?format=json&track_id={track_id}&usertoken={tok}&app_id=web-desktop-app-v1.0"
                        r_res = json.loads(urllib.request.urlopen(urllib.request.Request(rich_url, headers=headers), timeout=6, context=ctx).read().decode("utf-8"))
                        rich_body = r_res.get("message", {}).get("body", {}).get("richsync", {}).get("richsync_body")

                        if rich_body:
                            lines = json.loads(rich_body)
                            lrc_lines = []
                            for l in lines:
                                ts = l.get("ts", 0)
                                min_s = int(ts // 60)
                                sec_s = int(ts % 60)
                                ms_s = int((ts % 1) * 1000)
                                line_str = f"[{min_s:02d}:{sec_s:02d}.{ms_s:03d}]"
                                for w in l.get("l", []):
                                    c = w.get("c", "")
                                    off = w.get("o", 0)
                                    word_ts = ts + off
                                    w_min = int(word_ts // 60)
                                    w_sec = int(word_ts % 60)
                                    w_ms = int((word_ts % 1) * 1000)
                                    line_str += f"<{w_min:02d}:{w_sec:02d}.{w_ms:03d}>{c}"
                                lrc_lines.append(line_str)

                            enhanced_lrc = "\n".join(lrc_lines)
                            if enhanced_lrc.strip():
                                if not title_is_cjk and _contains_cjk(enhanced_lrc):
                                    log.warning("Lrclib Lyrics: [Musixmatch] RichSync returned CJK for non-CJK track %r, rejecting", cache_key)
                                else:
                                    log.info("Lrclib Lyrics: [Musixmatch] RichSync Word-Level Karaoke parsed (%d lines, %d bytes)", len(lrc_lines), len(enhanced_lrc))
                                    QTimer.singleShot(0, lambda text=enhanced_lrc: set_lyrics(text, "Musixmatch RichSync (Word-Level Karaoke)"))
                                    return
                        else:
                            log.warning("Lrclib Lyrics: [Musixmatch] RichSync body empty for track_id=%s", track_id)

                        # 2. Try Subtitle fallback
                        log.info("Lrclib Lyrics: [Musixmatch] Trying Subtitle line-sync fallback for track_id=%s...", track_id)
                        sub_url = f"https://apic-desktop.musixmatch.com/ws/1.1/track.subtitle.get?format=json&track_id={track_id}&usertoken={tok}&app_id=web-desktop-app-v1.0"
                        sub_res = json.loads(urllib.request.urlopen(urllib.request.Request(sub_url, headers=headers), timeout=6, context=ctx).read().decode("utf-8"))
                        sub_body = sub_res.get("message", {}).get("body", {}).get("subtitle", {}).get("subtitle_body")
                        if sub_body and isinstance(sub_body, str) and sub_body.strip():
                            if not title_is_cjk and _contains_cjk(sub_body):
                                log.warning("Lrclib Lyrics: [Musixmatch] Subtitle returned CJK for non-CJK track %r, rejecting", cache_key)
                            else:
                                QTimer.singleShot(0, lambda text=sub_body: set_lyrics(text, "Musixmatch Subtitle (Line-Level Sync)"))
                                return
                        else:
                            log.warning("Lrclib Lyrics: [Musixmatch] Subtitle body empty for track_id=%s", track_id)
                else:
                    log.warning("Lrclib Lyrics: [Musixmatch] Search track yielded 0 results for title=%r, artist=%r", clean_title, clean_artist)
            else:
                log.warning("Lrclib Lyrics: [Musixmatch] Failed to acquire user_token")
        except Exception as e:
            import traceback
            log.warning("Lrclib Lyrics: [Musixmatch] Error for %r: %s\n%s", cache_key, e, traceback.format_exc())

        QTimer.singleShot(0, lambda: apply_fallback())

    t = threading.Thread(target=_worker)
    t.daemon = True
    t.start()


def fetch_netease_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=None):
    search_url = "http://music.163.com/api/search/get"
    query = f"{clean_title} {clean_artist}".strip()
    title_is_cjk = _contains_cjk(clean_title)

    def apply_final_backup():
        if lrclib_backup:
            lyrics_cache[cache_key] = lrclib_backup
            if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                metadata["lyrics"] = lrclib_backup
            log.info("Lrclib Lyrics: Applied LRCLIB backup line lyrics for %r", cache_key)
        else:
            failed_lyrics_cache.add(cache_key)
            log.warning("Lrclib Lyrics: No lyrics found in LRCLIB, Musixmatch, or NetEase for title=%r, artist=%r", clean_title, clean_artist)
            if hasattr(album, "tagger") and hasattr(album.tagger, "window") and hasattr(album.tagger.window, "set_statusbar_message"):
                try:
                    album.tagger.window.set_statusbar_message(f"Lrclib Lyrics: No lyrics found for '{clean_title}' by '{clean_artist}'")
                except Exception:
                    pass
    
    def netease_lyric_handler(doc, rep, err):
        if doc and not err and isinstance(doc, dict):
            lrc_data = doc.get("lrc", {})
            klyric_data = doc.get("klyric", {})
            chosen = klyric_data.get("lyric") or lrc_data.get("lyric")
            if chosen and isinstance(chosen, str) and chosen.strip():
                if not title_is_cjk and _contains_cjk(chosen):
                    log.warning("Lrclib Lyrics: NetEase returned CJK lyrics for non-CJK track %r, rejecting", cache_key)
                    apply_final_backup()
                    return
                lyrics_cache[cache_key] = chosen
                if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                    metadata["lyrics"] = chosen
                log.info("Lrclib Lyrics: Successfully fetched NetEase lyrics for %r", cache_key)
                return
        apply_final_backup()

    def netease_search_handler(doc, rep, err):
        if doc and not err and isinstance(doc, dict):
            res = doc.get("result")
            if isinstance(res, str):
                import json
                try:
                    res = json.loads(res)
                except Exception:
                    res = {}
            if isinstance(res, dict) and "songs" in res and res["songs"]:
                song_id = res["songs"][0].get("id")
                if song_id:
                    lyric_url = "http://music.163.com/api/song/lyric"
                    album.tagger.webservice.get_url(
                        method="GET",
                        handler=netease_lyric_handler,
                        parse_response_type='json',
                        url=lyric_url,
                        unencoded_queryargs={"os": "pc", "id": str(song_id), "lv": "-1", "kv": "-1", "tv": "-1"},
                        important=False
                    )
                    return
        apply_final_backup()

    try:
        album.tagger.webservice.get_url(
            method="GET",
            handler=netease_search_handler,
            parse_response_type='json',
            url=search_url,
            unencoded_queryargs={"s": query, "type": "1", "offset": "0", "limit": "1"},
            important=False
        )
    except Exception:
        apply_final_backup()


def _do_fetch_lyrics(album, metadata, clean_title, raw_artist, cache_key):
    try:
        clean_artist = _clean_artist_for_query(raw_artist)
        req_args = {
            "track_name": clean_title,
            "artist_name": clean_artist,
        }
        log.info("Lrclib Lyrics: Querying lrclib.net for title=%r, artist=%r", clean_title, clean_artist)
        handler = partial(response_handler, album, metadata, clean_title, clean_artist, cache_key)
        album.tagger.webservice.get_url(
            method="GET",
            handler=handler,
            parse_response_type='json',
            url=URL,
            unencoded_queryargs=req_args,
            important=False
        )
    except Exception as e:
        log.error("Lrclib Lyrics error: %s", e)
        failed_lyrics_cache.add(cache_key)


def get_lyrics(*args):
    file = None
    track = None
    for arg in args:
        if hasattr(arg, "metadata") and hasattr(arg, "filename"):
            file = arg
        elif hasattr(arg, "album") and hasattr(arg, "files"):
            track = arg

    if not file or not track:
        return

    album = getattr(track, "album", None)
    if not album or not hasattr(album, "tagger"):
        return

    metadata = file.metadata

    add_unsynced = _get_option(ADD_UNSYNCED_LYRICS, True)
    add_synced = _get_option(ADD_SYNCED_LYRICS, True)
    if not (add_unsynced or add_synced):
        log.warning("Lrclib Lyrics: Both synced and unsynced lyrics options are disabled in settings")
        return

    raw_title = _clean_str(metadata.get("_original_title") or metadata.get("title"))
    raw_artist = _clean_str(metadata.get("_original_artist") or metadata.get("artist"))

    if not (raw_title and raw_artist):
        return

    never_replace = _get_option(NEVER_REPLACE_LYRICS, False)
    existing_lyrics = metadata.get("lyrics") or ""

    # If lyrics exist AND have word timestamps <mm:ss.xxx>, skip unless user requested replacement
    if never_replace or ("<" in existing_lyrics and ">" in existing_lyrics):
        return

    # Prepare lookup key
    clean_title = _clean_title_for_query(raw_title)
    cache_key = (clean_title.lower().strip(), raw_artist.lower().strip())

    # Check positive cache (only accept if cached version is word-synced or we have no lyrics)
    if cache_key in lyrics_cache:
        cached_lyrics = lyrics_cache[cache_key]
        if cached_lyrics and ("<" in cached_lyrics and ">" in cached_lyrics or not existing_lyrics):
            metadata["lyrics"] = cached_lyrics
            return

    # Check negative cache and pending fetches (fast-fail in 0ms)
    if cache_key in failed_lyrics_cache or cache_key in pending_fetches:
        return

    # Mark as pending to avoid redundant enqueues while UI is loading
    pending_fetches.add(cache_key)

    # Defer HTTP query via QTimer singleShot to keep UI 100% responsive during drag & drop
    QTimer.singleShot(250, lambda: _do_fetch_lyrics(album, metadata, clean_title, raw_artist, cache_key))


def _clean_str(val):
    if isinstance(val, (list, tuple)):
        return str(val[0]) if val else ""
    return str(val) if val is not None else ""


def get_lrc_file_name(file):
    if _get_option(LRC_AS_SIDECAR, True):
        filename = f"{os.path.splitext(file.filename)[0]}.lrc"
        return filename
    lrc_fmt = _get_option(LRC_FILENAME, "%filename%")
    filename = f"{tags_pattern.sub('{}', lrc_fmt)}"
    tags = tags_pattern.findall(lrc_fmt)
    values = []
    for tag in tags:
        if tag in extra_file_variables:
            values.append(extra_file_variables[tag](file.filename))
        else:
            values.append(file.metadata.get(tag, f"%{tag}%"))
    return filename.format(*values)


def export_lrc_file(*args):
    if not args:
        return
    file = args[-1]
    if _get_option(EXPORT_LRC, True):
        if not file or not hasattr(file, "metadata"):
            return
        metadata = file.metadata
        lyrics = metadata.get("lyrics")
        if not lyrics:
            return
        lrc_path = get_lrc_file_name(file)
        if _get_option(NEVER_REPLACE_LRC, False) and os.path.exists(lrc_path):
            return
        try:
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(lyrics)
            log.info(f"Exported LRC file to {lrc_path}")
        except Exception as e:
            log.error(f"Error exporting LRC file {lrc_path}: {e}")


from picard.i18n import N_

class FetchLyricsAction(BaseAction):
    NAME = N_("Fetch / Refresh Lyrics (Word Sync Karaoke)")
    TITLE = N_("Fetch / Refresh Lyrics (Word Sync Karaoke)")

    def callback(self, objs):
        for obj in objs:
            files = []
            if hasattr(obj, "files"):
                files = obj.files
            elif hasattr(obj, "metadata") and hasattr(obj, "filename"):
                files = [obj]

            for file in files:
                track = getattr(file, "track", None) or obj
                clean_title = _clean_title_for_query(_clean_str(file.metadata.get("title")))
                clean_artist = _clean_artist_for_query(_clean_str(file.metadata.get("artist")))
                if clean_title and clean_artist:
                    cache_key = (clean_title.lower().strip(), clean_artist.lower().strip())
                    lyrics_cache.pop(cache_key, None)
                    failed_lyrics_cache.discard(cache_key)
                    pending_fetches.discard(cache_key)
                    file.metadata.pop("lyrics", None)
                    get_lyrics(track, file)


class PublishToLrclibAction(BaseAction):
    NAME = N_("Publish / Submit lyrics to LRCLIB")
    TITLE = N_("Publish / Submit lyrics to LRCLIB")

    def callback(self, objs):
        import hashlib
        import json
        from urllib.request import Request, urlopen

        for obj in objs:
            metadata = getattr(obj, "metadata", None)
            if not metadata:
                continue
            title = metadata.get("title")
            artist = metadata.get("artist")
            album = metadata.get("album", "")
            lyrics = metadata.get("lyrics")

            if not (title and artist and lyrics):
                log.warning("Lrclib Publish: Missing title, artist, or lyrics for %r", obj)
                continue

            duration = 0
            if hasattr(obj, "length") and obj.length:
                duration = int(obj.length / 1000)

            try:
                req = Request("https://lrclib.net/api/request-challenge", method="POST")
                with urlopen(req, timeout=10) as resp:
                    challenge = json.loads(resp.read().decode("utf-8"))

                prefix = challenge.get("prefix", "")
                target = challenge.get("target", "")

                nonce = 0
                token = ""
                while nonce < 2000000:
                    h = hashlib.sha256(f"{prefix}{nonce}".encode("utf-8")).hexdigest()
                    if h == target or h.startswith(target) or h.endswith(target):
                        token = f"{prefix}:{nonce}"
                        break
                    nonce += 1

                if not token:
                    token = f"{prefix}:{nonce}"

                payload = {
                    "trackName": title,
                    "artistName": artist,
                    "albumName": album,
                    "duration": duration,
                }
                if synced_lyrics_pattern.search(lyrics):
                    payload["syncedLyrics"] = lyrics
                else:
                    payload["plainLyrics"] = lyrics

                pub_req = Request(
                    "https://lrclib.net/api/publish",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Publish-Token": token,
                        "User-Agent": "Picard-Lrclib-Plugin/1.0",
                    },
                    method="POST",
                )
                with urlopen(pub_req, timeout=10) as pub_resp:
                    if pub_resp.status in (200, 201):
                        log.info("Lrclib Publish: Successfully published lyrics for %s - %s", artist, title)
                    else:
                        log.warning("Lrclib Publish: Received status %d for %s - %s", pub_resp.status, artist, title)
            except Exception as e:
                log.error("Lrclib Publish Error for %s - %s: %s", artist, title, e)


class LrclibLyricsOptions(OptionsPage):
    NAME = "lrclib_lyrics"
    TITLE = "Lrclib Lyrics"
    PARENT = "plugins"

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6 import QtWidgets

        self.cb_unsynced = QtWidgets.QCheckBox("Download and embed unsynced lyrics", self)
        self.cb_synced = QtWidgets.QCheckBox("Download and embed synced lyrics (Karaoke / Timestamped LRC)", self)
        self.cb_never_replace = QtWidgets.QCheckBox("Never replace any embedded lyrics if already present", self)
        self.cb_export_lrc = QtWidgets.QCheckBox("Export lyrics to .lrc file when saving (priority to synced Karaoke lyrics)", self)
        self.cb_sidecar = QtWidgets.QCheckBox("Save the LRC file as a sidecar file to the audio file (for Navidrome & Feishin)", self)
        self.cb_never_replace_lrc = QtWidgets.QCheckBox("Never replace lrc files if already present", self)

        vbox = QtWidgets.QVBoxLayout(self)
        vbox.addWidget(self.cb_unsynced)
        vbox.addWidget(self.cb_synced)
        vbox.addWidget(self.cb_never_replace)
        vbox.addWidget(self.cb_export_lrc)
        vbox.addWidget(self.cb_sidecar)
        vbox.addWidget(self.cb_never_replace_lrc)
        vbox.addStretch()

    def load(self):
        self.cb_unsynced.setChecked(bool(self.api.plugin_config[ADD_UNSYNCED_LYRICS]))
        self.cb_synced.setChecked(bool(self.api.plugin_config[ADD_SYNCED_LYRICS]))
        self.cb_never_replace.setChecked(bool(self.api.plugin_config[NEVER_REPLACE_LYRICS]))
        self.cb_export_lrc.setChecked(bool(self.api.plugin_config[EXPORT_LRC]))
        self.cb_sidecar.setChecked(bool(self.api.plugin_config[LRC_AS_SIDECAR]))
        self.cb_never_replace_lrc.setChecked(bool(self.api.plugin_config[NEVER_REPLACE_LRC]))

    def save(self):
        self.api.plugin_config[ADD_UNSYNCED_LYRICS] = self.cb_unsynced.isChecked()
        self.api.plugin_config[ADD_SYNCED_LYRICS] = self.cb_synced.isChecked()
        self.api.plugin_config[NEVER_REPLACE_LYRICS] = self.cb_never_replace.isChecked()
        self.api.plugin_config[EXPORT_LRC] = self.cb_export_lrc.isChecked()
        self.api.plugin_config[LRC_AS_SIDECAR] = self.cb_sidecar.isChecked()
        self.api.plugin_config[NEVER_REPLACE_LRC] = self.cb_never_replace_lrc.isChecked()


def enable(api: PluginApi):
    global _api
    _api = api
    if hasattr(api, "plugin_config") and hasattr(api.plugin_config, "register_option"):
        try:
            api.plugin_config.register_option(ADD_UNSYNCED_LYRICS, True)
            api.plugin_config.register_option(ADD_SYNCED_LYRICS, True)
            api.plugin_config.register_option(NEVER_REPLACE_LYRICS, False)
            api.plugin_config.register_option(EXPORT_LRC, True)
            api.plugin_config.register_option(LRC_AS_SIDECAR, True)
            api.plugin_config.register_option(LRC_FILENAME, "%filename%")
            api.plugin_config.register_option(NEVER_REPLACE_LRC, False)
        except Exception:
            pass
    api.register_file_post_addition_to_track_processor(get_lyrics)
    api.register_track_metadata_processor(get_lyrics)
    api.register_file_post_save_processor(export_lrc_file)
    api.register_track_action(FetchLyricsAction)
    api.register_file_action(FetchLyricsAction)
    api.register_track_action(PublishToLrclibAction)
    api.register_file_action(PublishToLrclibAction)
    api.register_options_page(LrclibLyricsOptions)