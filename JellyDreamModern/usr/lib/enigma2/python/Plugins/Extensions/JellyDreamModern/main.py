# -*- coding: utf-8 -*-
"""Kompatibilitäts-Fassade. Die Logik liegt in den Untermodulen."""

from .library import (
    jellydream_initialize_plugin_services,
    jellydream_refresh_library_cache_if_stale_async,
    jellydream_stop_library_scheduler,
)
from .screens.home import JellyDreamMain
from .screens.movies import JellyDreamMovieListScreen
from .screens.favorites import JellyDreamFavoriteListScreen
from .screens.series import JellyDreamSeriesListScreen
from .screens.seasons import JellyDreamSeasonListScreen
from .screens.episodes import JellyDreamEpisodeListScreen
from .screens.search import JellyDreamSearchScreen
from .screens.boxsets import (
    JellyDreamBoxSetListScreen,
    JellyDreamBoxSetMoviesScreen,
)
from .screens.music import (
    JellyDreamMusicAlbumScreen,
    JellyDreamMusicListScreen,
)
from .screens.login import JellyDreamLoginScreen
from .screens.home_info import JellyDreamHomeMovieInfoScreen
from .screens.settings import JellyDreamSettingsScreen
from .playback import JellyDreamStreamScreen

__all__ = [
    "JellyDreamMain",
    "JellyDreamMovieListScreen",
    "JellyDreamFavoriteListScreen",
    "JellyDreamSeriesListScreen",
    "JellyDreamSeasonListScreen",
    "JellyDreamEpisodeListScreen",
    "JellyDreamSearchScreen",
    "JellyDreamBoxSetListScreen",
    "JellyDreamBoxSetMoviesScreen",
    "JellyDreamMusicListScreen",
    "JellyDreamMusicAlbumScreen",
    "JellyDreamLoginScreen",
    "JellyDreamHomeMovieInfoScreen",
    "JellyDreamSettingsScreen",
    "JellyDreamStreamScreen",
    "jellydream_initialize_plugin_services",
    "jellydream_refresh_library_cache_if_stale_async",
    "jellydream_stop_library_scheduler",
]
