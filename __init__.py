_api = None
from picard.config import config
from picard import log
# -*- coding: utf-8 -*-
#
# Copyright (C) 2024 Giorgio Fontanive (twodoorcoupe)
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.


from picard.plugin3.api import PluginApi

from picard.plugin3.api import (
    BaseAction,
    File,
    OptionsPage,
    Track,
)

import os
import re
from functools import partial





from picard.webservice import ratecontrol

from .option_lrclib_lyrics import Ui_OptionLrclibLyrics


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

lyrics_cache = {}
synced_lyrics_pattern = re.compile(r"(\[\d\d:\d\d\.\d\d\d]|<\d\d:\d\d\.\d\d\d>)")
tags_pattern = re.compile(r"%(\w+)%")
extra_file_variables = {
    "filepath": lambda file: file,
    "folderpath": lambda file: os.path.dirname(file),  # pylint: disable=unnecessary-lambda
    "filename": lambda file: os.path.splitext(os.path.basename(file))[0],
    "filename_ext": lambda file: os.path.basename(file),  # pylint: disable=unnecessary-lambda
    "directory": lambda file: os.path.basename(os.path.dirname(file))
}


def get_lyrics(*args):
    file = None
    track = None
    for arg in args:
        if hasattr(arg, "metadata") and hasattr(arg, "filename"):
            file = arg
        elif hasattr(arg, "album") and hasattr(arg, "files"):
            track = arg

    if not file or not track:
        log.debug("Lrclib Lyrics: Could not identify file/track from args: %r", args)
        return

    album = getattr(track, "album", None)
    metadata = file.metadata

    add_unsynced = _api.plugin_config[ADD_UNSYNCED_LYRICS]
    add_synced = config.setting[ADD_SYNCED_LYRICS]
    if add_unsynced is None:
        add_unsynced = True
    if not (add_unsynced or add_synced):
        log.debug("Lrclib Lyrics: Both ADD_UNSYNCED_LYRICS and ADD_SYNCED_LYRICS are disabled")
        return

    title = _clean_str(metadata.get("_original_title") or metadata.get("title"))
    artist = _clean_str(metadata.get("_original_artist") or metadata.get("artist"))

    if not (title and artist):
        log.debug("Skipping fetching lyrics for track in %s as both title and artist are required", album)
        return

    never_replace = config.setting[NEVER_REPLACE_LYRICS]
    if never_replace and metadata.get("lyrics"):
        log.debug("Skipping fetching lyrics for %s as lyrics are already embedded", title)
        return

    req_args = {
        "track_name": title,
        "artist_name": artist,
    }
    log.info("Lrclib Lyrics: Querying lrclib.net for title=%r, artist=%r", title, artist)
    handler = partial(response_handler, metadata)
    album.tagger.webservice.get_url(
        method="GET",
        handler=handler,
        parse_response_type='json',
        url=URL,
        unencoded_queryargs=req_args
    )


def _clean_str(val):
    if isinstance(val, (list, tuple)):
        return str(val[0]) if val else ""
    return str(val) if val is not None else ""


def response_handler(metadata, document, reply, error):
    if document and not error:
        unsynced_lyrics = document.get("plainLyrics")
        synced_lyrics = document.get("syncedLyrics")
        chosen = synced_lyrics or unsynced_lyrics
        if chosen:
            title_key = _clean_str(metadata.get("title"))
            lyrics_cache[title_key] = chosen
            if config.setting[NEVER_REPLACE_LYRICS] and metadata.get("lyrics"):
                return
            metadata["lyrics"] = chosen
    else:
        log.debug(f"Could not fetch lyrics for {metadata.get('title')}")


def get_lrc_file_name(file):
    filename = f"{tags_pattern.sub('{}', config.setting[LRC_FILENAME])}"
    # If sidecar option is selected, override any pattern
    if config.setting[LRC_AS_SIDECAR]:
        filename = f"{os.path.splitext(file.filename)[0]}.lrc"
        log.debug(f"LRC sidecar filename for {file.metadata['title']}: {filename}")
        return filename
    # Otherwise, parse the pattern
    tags = tags_pattern.findall(config.setting[LRC_FILENAME])
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
    if config.setting[EXPORT_LRC]:
        metadata = file.metadata
        title_key = _clean_str(metadata.get("title"))
        lyrics = lyrics_cache.pop(title_key, metadata.get("lyrics"))
        if isinstance(lyrics, (list, tuple)):
            lyrics = "\n".join(str(x) for x in lyrics)
        elif lyrics is not None:
            lyrics = str(lyrics)

        if lyrics:
            filename = get_lrc_file_name(file)
            if config.setting[NEVER_REPLACE_LRC] and os.path.exists(filename):
                return
            try:
                with open(filename, 'w', encoding='utf-8', errors='ignore') as f:
                    f.write(lyrics)
                log.debug(f"Created new lyrics file at {filename}")
            except Exception as e:
                log.debug(f"Could not create the lrc file for {title_key}: {e}")
        else:
            log.debug(f"Could not export any lyrics for {title_key}")


class ImportLrc(BaseAction):
    TITLE = "Import lyrics from lrc files"

    def callback(self, objs):
        for track in objs:
            if isinstance(track, Track):
                file = track.files[0]
                filename = get_lrc_file_name(file)
                try:
                    with open(filename, 'r') as lyrics_file:
                        lyrics = lyrics_file.read()
                        if synced_lyrics_pattern.search(lyrics):
                            # Support for syncedlyrics is not available yet
                            # file.metadata["syncedlyrics"] = lyrics
                            pass
                        else:
                            file.metadata["lyrics"] = lyrics
                except FileNotFoundError:
                    log.debug(f"Could not find matching lrc file for {file.metadata['title']}")


class LrclibLyricsOptions(OptionsPage):

    NAME = "lrclib_lyrics"
    TITLE = "Lrclib Lyrics"
    PARENT = "plugins"

    __default_naming = f"%folderpath%{os.sep}%filename%.lrc"

    def __init__(self, parent=None):
        super(LrclibLyricsOptions, self).__init__(parent)
        self.ui = Ui_OptionLrclibLyrics()
        self.ui.setupUi(self)

    def load(self):
        self.ui.lyrics.setChecked(self.api.plugin_config[ADD_UNSYNCED_LYRICS])
        self.ui.syncedlyrics.setChecked(self.api.plugin_config[ADD_SYNCED_LYRICS])
        self.ui.replace_embedded.setChecked(self.api.plugin_config[NEVER_REPLACE_LYRICS])
        self.ui.lrc_name.setText(self.api.plugin_config[LRC_FILENAME])
        self.ui.lrc_as_sidecar.setChecked(self.api.plugin_config[LRC_AS_SIDECAR])
        self.ui.export_lyrics.setChecked(self.api.plugin_config[EXPORT_LRC])
        self.ui.replace_exported.setChecked(self.api.plugin_config[NEVER_REPLACE_LRC])

        self.update_lrc_name_field_state()
        self.ui.lrc_as_sidecar.toggled.connect(self.update_lrc_name_field_state)

    def save(self):
        self.api.plugin_config[ADD_UNSYNCED_LYRICS] = self.ui.lyrics.isChecked()
        self.api.plugin_config[ADD_SYNCED_LYRICS] = self.ui.syncedlyrics.isChecked()
        self.api.plugin_config[NEVER_REPLACE_LYRICS] = self.ui.replace_embedded.isChecked()
        self.api.plugin_config[LRC_FILENAME] = self.ui.lrc_name.text()
        self.api.plugin_config[LRC_AS_SIDECAR] = self.ui.lrc_as_sidecar.isChecked()
        self.api.plugin_config[EXPORT_LRC] = self.ui.export_lyrics.isChecked()
        self.api.plugin_config[NEVER_REPLACE_LRC] = self.ui.replace_exported.isChecked()


def enable(api: PluginApi):
    global _api
    _api = api
    """Called when plugin is enabled."""
    api.plugin_config.register_option(ADD_UNSYNCED_LYRICS, True)
    api.plugin_config.register_option(ADD_SYNCED_LYRICS, False)
    api.plugin_config.register_option(NEVER_REPLACE_LYRICS, False)
    api.plugin_config.register_option(LRC_FILENAME, f"%folderpath%{os.sep}%filename%.lrc")
    api.plugin_config.register_option(LRC_AS_SIDECAR, True)
    api.plugin_config.register_option(EXPORT_LRC, True)
    api.plugin_config.register_option(NEVER_REPLACE_LRC, False)

    api.register_file_post_addition_to_track_processor(get_lyrics)
    api.register_file_post_save_processor(export_lrc_file)
    api.register_track_action(ImportLrc)
    api.register_options_page(LrclibLyricsOptions)