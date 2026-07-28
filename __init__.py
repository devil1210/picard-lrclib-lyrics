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
    """Extract primary title without dual-language romaji suffix or trailing feat."""
    if not title:
        return ""
    parts = re.split(r'\s+[\-\–\—]\s+', title)
    clean = parts[0].strip()
    return clean


def _clean_artist_for_query(artist: str) -> str:
    """Extract primary artist before separators like commas, ' y ', ' & ', ' feat.', etc."""
    if not artist:
        return ""
    for sep in [",", " y ", " & ", " feat.", " ft.", " presenting", " (feat"]:
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
            # If title is Latin-script AND the returned lyrics are CJK, skip
            # and fall through to search so we can find a Latin/romaji version.
            # (A Spanish song should not get Korean lyrics; a Japanese song
            # written in romaji WILL still get Japanese lyrics if no romaji
            # version exists — see two-pass logic in search_handler below.)
            if not title_is_cjk and _contains_cjk(chosen):
                log.debug(
                    "Lrclib Lyrics: /api/get returned CJK lyrics for Latin-title track %r, trying search",
                    cache_key,
                )
            else:
                lyrics_cache[cache_key] = chosen
                if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                    metadata["lyrics"] = chosen
                return

    # Fallback to /api/search
    search_url = "https://lrclib.net/api/search"
    search_query = f"{clean_title} {clean_artist}".strip()

    def search_handler(doc, rep, err):
        if doc and not err and isinstance(doc, list):
            # Two-pass selection:
            #  Pass 1 – prefer lyrics whose script matches the title script.
            #    • Latin title  → prefer non-CJK lyrics first.
            #    • CJK title    → prefer CJK lyrics first.
            #  Pass 2 – accept anything that has lyrics.
            #
            # This correctly handles:
            #   ✓ "Un regalo mágico" (Spanish) → skips Korean translation, picks Spanish.
            #   ✓ "Renai Circulation" (romaji) → no Latin lyrics exist, accepts Japanese.
            #   ✓ "紅蓮華" (kanji title)        → CJK lyrics accepted directly.

            candidates = []
            for item in doc:
                if not isinstance(item, dict):
                    continue
                lyrics = item.get("syncedLyrics") or item.get("plainLyrics")
                if lyrics:
                    candidates.append(lyrics)

            if candidates:
                # Pass 1: prefer matching script
                for lyrics in candidates:
                    lyrics_is_cjk = _contains_cjk(lyrics)
                    if title_is_cjk == lyrics_is_cjk:
                        lyrics_cache[cache_key] = lyrics
                        if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                            metadata["lyrics"] = lyrics
                        return

                # Pass 2: accept best available regardless of script
                lyrics = candidates[0]
                lyrics_cache[cache_key] = lyrics
                if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                    metadata["lyrics"] = lyrics
                return

        failed_lyrics_cache.add(cache_key)
        log.debug("Lrclib Lyrics: Could not fetch lyrics for %r", cache_key)

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
        failed_lyrics_cache.add(cache_key)


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


class LrclibLyricsOptions(OptionsPage):
    NAME = "lrclib_lyrics"
    TITLE = "Lrclib Lyrics"
    PARENT = "plugins"

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6 import QtWidgets

        self.cb_unsynced = QtWidgets.QCheckBox("Download and embed unsynced lyrics", self)
        self.cb_synced = QtWidgets.QCheckBox("Download and embed synced lyrics", self)
        self.cb_never_replace = QtWidgets.QCheckBox("Never replace any embedded lyrics if already present", self)
        self.cb_export_lrc = QtWidgets.QCheckBox("Export lyrics to lrc file when saving (priority to synced lyrics)", self)
        self.cb_sidecar = QtWidgets.QCheckBox("Save the LRC file as a sidecar file to the audio file", self)
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
    api.register_options_page(LrclibLyricsOptions)