# -*- coding: utf-8 -*-
#
# Copyright (C) 2024 Giorgio Fontanive (twodoorcoupe)
#

import os
import re
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



def response_handler(album, metadata, clean_title, clean_artist, cache_key, document, reply, error):
    # Whether the track title itself uses CJK characters (e.g. actual Japanese
    # or Korean title).  Romaji titles like "Gurenge" / "Renai Circulation"
    # will be False even though the song is Japanese.
    title_is_cjk = _contains_cjk(clean_title)

    if document and not error and isinstance(document, dict):
        unsynced_lyrics = document.get("plainLyrics")
        synced_lyrics = document.get("syncedLyrics")
        chosen = synced_lyrics or unsynced_lyrics
        if chosen:
            # If LRCLIB provided word-level timestamps <mm:ss.xxx>, use it immediately
            if "<" in chosen and ">" in chosen:
                lyrics_cache[cache_key] = chosen
                if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                    metadata["lyrics"] = chosen
                return
            # Otherwise, try Musixmatch RichSync for word-level timestamps, passing LRCLIB line lyrics as backup
            log.info("Lrclib Lyrics: LRCLIB returned line lyrics for %r; trying Musixmatch RichSync for word-level sync", cache_key)
            fetch_musixmatch_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=chosen)
            return

    # Fallback to /api/search or Musixmatch RichSync
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
                for lyrics in candidates:
                    if "<" in lyrics and ">" in lyrics:
                        lyrics_cache[cache_key] = lyrics
                        if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                            metadata["lyrics"] = lyrics
                        return
                # Line lyrics backup
                log.debug("Lrclib Lyrics: LRCLIB search yielded line lyrics for %r, trying Musixmatch RichSync", cache_key)
                fetch_musixmatch_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=candidates[0])
                return

        log.debug("Lrclib Lyrics: LRCLIB search yielded no lyrics for %r, trying Musixmatch RichSync", cache_key)
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


def fetch_musixmatch_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=None):
    token_url = "https://apic-desktop.musixmatch.com/ws/1.1/token.get"
    title_is_cjk = _contains_cjk(clean_title)

    def apply_fallback():
        if lrclib_backup:
            lyrics_cache[cache_key] = lrclib_backup
            if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                metadata["lyrics"] = lrclib_backup
            log.info("Lrclib Lyrics: Used LRCLIB backup line lyrics for %r", cache_key)
        else:
            fetch_netease_lyrics(album, metadata, clean_title, clean_artist, cache_key, lrclib_backup=None)
    
    def token_handler(doc, rep, err):
        if doc and not err and isinstance(doc, dict):
            tok = doc.get("message", {}).get("body", {}).get("user_token")
            if tok:
                search_url = "https://apic-desktop.musixmatch.com/ws/1.1/track.search"
                
                def search_handler(s_doc, s_rep, s_err):
                    if s_doc and not s_err and isinstance(s_doc, dict):
                        track_list = s_doc.get("message", {}).get("body", {}).get("track_list", [])
                        if track_list and len(track_list) > 0:
                            track_id = track_list[0].get("track", {}).get("track_id")
                            if track_id:
                                rich_url = "https://apic-desktop.musixmatch.com/ws/1.1/track.richsync.get"
                                
                                def rich_handler(r_doc, r_rep, r_err):
                                    if r_doc and not r_err and isinstance(r_doc, dict):
                                        rich_body = r_doc.get("message", {}).get("body", {}).get("richsync", {}).get("richsync_body")
                                        if rich_body:
                                            try:
                                                import json
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
                                                        log.warning("Lrclib Lyrics: Musixmatch RichSync returned CJK for non-CJK track %r, rejecting", cache_key)
                                                    else:
                                                        lyrics_cache[cache_key] = enhanced_lrc
                                                        if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                                                            metadata["lyrics"] = enhanced_lrc
                                                        log.info("Lrclib Lyrics: Successfully fetched Musixmatch RichSync Word-by-Word lyrics for %r", cache_key)
                                                        return
                                            except Exception as ex:
                                                log.error("Musixmatch RichSync parse error: %s", ex)

                                    sub_url = "https://apic-desktop.musixmatch.com/ws/1.1/track.subtitle.get"
                                    
                                    def sub_handler(sub_doc, sub_rep, sub_err):
                                        if sub_doc and not sub_err and isinstance(sub_doc, dict):
                                            sub_body = sub_doc.get("message", {}).get("body", {}).get("subtitle", {}).get("subtitle_body")
                                            if sub_body and isinstance(sub_body, str) and sub_body.strip():
                                                if not title_is_cjk and _contains_cjk(sub_body):
                                                    log.warning("Lrclib Lyrics: Musixmatch subtitle returned CJK for non-CJK track %r, rejecting", cache_key)
                                                else:
                                                    lyrics_cache[cache_key] = sub_body
                                                    if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                                                        metadata["lyrics"] = sub_body
                                                    log.info("Lrclib Lyrics: Successfully fetched Musixmatch subtitle lyrics for %r", cache_key)
                                                    return
                                        apply_fallback()

                                    try:
                                        album.tagger.webservice.get_url(
                                            method="GET",
                                            handler=sub_handler,
                                            parse_response_type='json',
                                            url=sub_url,
                                            unencoded_queryargs={"format": "json", "track_id": str(track_id), "usertoken": str(tok), "app_id": "web-desktop-app-v1.0"},
                                            important=False
                                        )
                                    except Exception:
                                        apply_fallback()

                                try:
                                    album.tagger.webservice.get_url(
                                        method="GET",
                                        handler=rich_handler,
                                        parse_response_type='json',
                                        url=rich_url,
                                        unencoded_queryargs={"format": "json", "track_id": str(track_id), "usertoken": str(tok), "app_id": "web-desktop-app-v1.0"},
                                        important=False
                                    )
                                except Exception:
                                    apply_fallback()
                                return
                    apply_fallback()

                try:
                    album.tagger.webservice.get_url(
                        method="GET",
                        handler=search_handler,
                        parse_response_type='json',
                        url=search_url,
                        unencoded_queryargs={"format": "json", "q_artist": clean_artist, "q_track": clean_title, "page_size": "1", "usertoken": str(tok), "app_id": "web-desktop-app-v1.0"},
                        important=False
                    )
                    return
                except Exception:
                    apply_fallback()
        apply_fallback()

    try:
        album.tagger.webservice.get_url(
            method="GET",
            handler=token_handler,
            parse_response_type='json',
            url=token_url,
            unencoded_queryargs={"app_id": "web-desktop-app-v1.0"},
            important=False
        )
    except Exception:
        apply_fallback()


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
    add_synced = _get_option(ADD_SYNCED_LYRICS, False)
    if not (add_unsynced or add_synced):
        return

    raw_title = _clean_str(metadata.get("_original_title") or metadata.get("title"))
    raw_artist = _clean_str(metadata.get("_original_artist") or metadata.get("artist"))

    if not (raw_title and raw_artist):
        return

    never_replace = _get_option(NEVER_REPLACE_LYRICS, False)
    if never_replace and metadata.get("lyrics"):
        return

    # Prepare lookup key
    clean_title = _clean_title_for_query(raw_title)
    cache_key = (clean_title.lower().strip(), raw_artist.lower().strip())

    # Check positive cache
    if cache_key in lyrics_cache:
        cached_lyrics = lyrics_cache[cache_key]
        if cached_lyrics and not metadata.get("lyrics"):
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


class ImportLrc(BaseAction):
    NAME = "Import LRC file"

    def callback(self, objs):
        pass


class PublishToLrclibAction(BaseAction):
    NAME = "Publish / Submit lyrics to LRCLIB"

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
                # 1. Request PoW Challenge
                req = Request("https://lrclib.net/api/request-challenge", method="POST")
                with urlopen(req, timeout=10) as resp:
                    challenge = json.loads(resp.read().decode("utf-8"))

                prefix = challenge.get("prefix", "")
                target = challenge.get("target", "")

                # 2. Solve PoW
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

                # 3. Publish Lyrics
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
    api.register_file_post_save_processor(export_lrc_file)
    api.register_track_action(ImportLrc)
    api.register_track_action(PublishToLrclibAction)
    api.register_file_action(PublishToLrclibAction)
    api.register_options_page(LrclibLyricsOptions)