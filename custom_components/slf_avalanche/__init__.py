"""Swiss Avalanche Bulletin (SLF) integration."""
from __future__ import annotations

from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MODE,
    CONF_NAME,
    DOMAIN,
    MODE_BULLETIN,
    MODE_IMIS,
    MODE_STUDY_PLOT,
    STATIC_URL_BASE,
    STATIONS_KEY,
)
from .coordinator import ImisCoordinator, SlfAvalancheCoordinator, StudyPlotCoordinator
from .issues import async_clear_entry_issues

PLATFORMS = ["sensor"]

# Device identifiers of a station: <entry_id>_<kind>_<code>.
STATION_DEVICE_KINDS = ("imis", "sp")


class RuntimeData:
    """Per-entry runtime data: the coordinator of the entry's mode."""

    def __init__(
        self,
        bulletin: SlfAvalancheCoordinator | None,
        imis: ImisCoordinator | None,
        study_plot: StudyPlotCoordinator | None = None,
    ) -> None:
        self.bulletin = bulletin
        self.imis = imis
        self.study_plot = study_plot


type SlfAvalancheConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SlfAvalancheConfigEntry) -> bool:
    if not hass.data.get(f"{DOMAIN}_static_registered"):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    STATIC_URL_BASE,
                    str(Path(__file__).parent / "static"),
                    cache_headers=True,
                )
            ]
        )
        hass.data[f"{DOMAIN}_static_registered"] = True

    mode = entry.data.get(CONF_MODE, MODE_BULLETIN)

    bulletin = None
    if mode == MODE_BULLETIN:
        bulletin = SlfAvalancheCoordinator(
            hass,
            entry.data[CONF_LATITUDE],
            entry.data[CONF_LONGITUDE],
            entry.data.get(CONF_NAME) or entry.title,
            entry.entry_id,
        )
        await bulletin.async_config_entry_first_refresh()

    stations = entry_stations(entry)

    imis = None
    if mode == MODE_IMIS and stations:
        imis = ImisCoordinator(hass, stations, entry.title, entry.entry_id)
        await imis.async_config_entry_first_refresh()

    study_plot = None
    if mode == MODE_STUDY_PLOT and stations:
        study_plot = StudyPlotCoordinator(hass, stations, entry.title, entry.entry_id)
        await study_plot.async_config_entry_first_refresh()

    _cleanup_stale_station_devices(hass, entry, stations)

    entry.runtime_data = RuntimeData(bulletin, imis, study_plot)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Favourite stations changed -> rebuild coordinators and entities.
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


def entry_stations(entry: ConfigEntry) -> list[dict]:
    """The favourite stations of a station entry (the options win over the
    data); empty for a bulletin entry."""
    key = STATIONS_KEY.get(entry.data.get(CONF_MODE, MODE_BULLETIN))
    if key is None:
        return []
    return entry.options.get(key, entry.data.get(key) or [])


def _cleanup_stale_station_devices(
    hass: HomeAssistant, entry: ConfigEntry, stations: list[dict]
) -> None:
    """Remove devices of stations that are no longer selected as favourites."""
    prefixes = tuple(f"{entry.entry_id}_{kind}_" for kind in STATION_DEVICE_KINDS)
    keep = {f"{prefix}{s['code']}" for prefix in prefixes for s in stations}
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if (
                domain == DOMAIN
                and identifier.startswith(prefixes)
                and identifier not in keep
            ):
                registry.async_remove_device(device.id)
                break


async def async_unload_entry(hass: HomeAssistant, entry: SlfAvalancheConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_clear_entry_issues(hass, entry.entry_id)
    return unloaded
