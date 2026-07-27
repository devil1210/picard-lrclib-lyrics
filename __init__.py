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


def _clean_title_for_query(title: str) -> str:
    """Extract primary title without dual-language romaji suffix or trailing feat."""
    if not title:
        return ""
    parts = re.split(r'\s+[\-\–\—]\s+', title)
    clean = parts[0].strip()
    return clean


def _do_fetch_lyrics(album, metadata, clean_title, raw_artist, cache_key):
    try:
        req_args = {
            "track_name": clean_title,
            "artist_name": raw_artist,
        }
        log.info("Lrclib Lyrics: Querying lrclib.net for title=%r, artist=%r", clean_title, raw_artist)
        handler = partial(response_handler, metadata, cache_key)
        album.tagger.webservice.get_url(
            method="GET",
            handler=handler,
            parse_response_type='json',
            url=URL,
            unencoded_queryargs=req_args
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


def response_handler(metadata, cache_key, document, reply, error):
    if document and not error:
        unsynced_lyrics = document.get("plainLyrics")
        synced_lyrics = document.get("syncedLyrics")
        chosen = synced_lyrics or unsynced_lyrics
        if chosen:
            lyrics_cache[cache_key] = chosen
            if not (_get_option(NEVER_REPLACE_LYRICS, False) and metadata.get("lyrics")):
                metadata["lyrics"] = chosen
        else:
            failed_lyrics_cache.add(cache_key)
    else:
        failed_lyrics_cache.add(cache_key)
        log.debug(f"Lrclib Lyrics: Could not fetch lyrics for {cache_key}")


def get_lrc_file_name(file):
    lrc_fmt = _get_option(LRC_FILENAME, "%filename%")
    filename = f"{tags_pattern.sub('{}', lrc_fmt)}"
    if _get_option(LRC_AS_SIDECAR, False):
        filename = f"{os.path.splitext(file.filename)[0]}.lrc"
        return filename
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
    if _get_option(EXPORT_LRC, False):
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
        self.ui = Ui_OptionLrclibLyrics()
        self.ui.setupUi(self)
        if hasattr(self.ui, 'syncedlyrics'):
            self.ui.syncedlyrics.setEnabled(True)
            self.ui.syncedlyrics.setCheckable(True)

    def load(self):
        if hasattr(self.ui, 'lyrics'):
            self.ui.lyrics.setChecked(bool(_get_option(ADD_UNSYNCED_LYRICS, True)))
        if hasattr(self.ui, 'syncedlyrics'):
            self.ui.syncedlyrics.setChecked(bool(_get_option(ADD_SYNCED_LYRICS, False)))
        if hasattr(self.ui, 'replace_embedded'):
            self.ui.replace_embedded.setChecked(bool(_get_option(NEVER_REPLACE_LYRICS, False)))
        if hasattr(self.ui, 'export_lyrics'):
            self.ui.export_lyrics.setChecked(bool(_get_option(EXPORT_LRC, False)))
        if hasattr(self.ui, 'lrc_as_sidecar'):
            self.ui.lrc_as_sidecar.setChecked(bool(_get_option(LRC_AS_SIDECAR, False)))
        if hasattr(self.ui, 'lrc_name'):
            self.ui.lrc_name.setText(str(_get_option(LRC_FILENAME, "%filename%")))
        if hasattr(self.ui, 'replace_exported'):
            self.ui.replace_exported.setChecked(bool(_get_option(NEVER_REPLACE_LRC, False)))

    def save(self):
        cfg_map = {}
        if hasattr(self.ui, 'lyrics'):
            cfg_map[ADD_UNSYNCED_LYRICS] = self.ui.lyrics.isChecked()
        if hasattr(self.ui, 'syncedlyrics'):
            cfg_map[ADD_SYNCED_LYRICS] = self.ui.syncedlyrics.isChecked()
        if hasattr(self.ui, 'replace_embedded'):
            cfg_map[NEVER_REPLACE_LYRICS] = self.ui.replace_embedded.isChecked()
        if hasattr(self.ui, 'export_lyrics'):
            cfg_map[EXPORT_LRC] = self.ui.export_lyrics.isChecked()
        if hasattr(self.ui, 'lrc_as_sidecar'):
            cfg_map[LRC_AS_SIDECAR] = self.ui.lrc_as_sidecar.isChecked()
        if hasattr(self.ui, 'lrc_name'):
            cfg_map[LRC_FILENAME] = str(self.ui.lrc_name.text())
        if hasattr(self.ui, 'replace_exported'):
            cfg_map[NEVER_REPLACE_LRC] = self.ui.replace_exported.isChecked()

        if hasattr(self, 'api') and self.api and hasattr(self.api, 'plugin_config'):
            for k, v in cfg_map.items():
                try:
                    self.api.plugin_config[k] = v
                except Exception:
                    pass

        if hasattr(config, 'setting'):
            for k, v in cfg_map.items():
                try:
                    config.setting[k] = v
                except Exception:
                    pass


def enable(api: PluginApi):
    global _api
    _api = api
    if hasattr(api, "plugin_config") and hasattr(api.plugin_config, "register_option"):
        try:
            api.plugin_config.register_option(ADD_UNSYNCED_LYRICS, True)
            api.plugin_config.register_option(ADD_SYNCED_LYRICS, False)
            api.plugin_config.register_option(NEVER_REPLACE_LYRICS, False)
            api.plugin_config.register_option(EXPORT_LRC, False)
            api.plugin_config.register_option(LRC_AS_SIDECAR, False)
            api.plugin_config.register_option(LRC_FILENAME, "%filename%")
            api.plugin_config.register_option(NEVER_REPLACE_LRC, False)
        except Exception:
            pass
    api.register_file_post_addition_to_track_processor(get_lyrics)
    api.register_file_post_save_processor(export_lrc_file)
    api.register_track_action(ImportLrc)
    api.register_options_page(LrclibLyricsOptions)