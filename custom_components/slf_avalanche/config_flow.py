"""Config flow for the Swiss Avalanche Bulletin (SLF) integration.

Setup starts with a menu: the avalanche bulletin for a location, favourite
IMIS measuring stations, or favourite study plots (manual measuring
fields). Reconfigure (the entry's menu under Settings → Devices & services)
changes an entry in place with the form of its own mode: another location
or label for a bulletin, other stations for an IMIS or study plot entry. The entry keeps its id, so its entities keep their ids and
history; the devices of stations that are no longer selected are removed on
the reload, as they are after a change in the options.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

from . import entry_stations
from .const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MODE,
    CONF_NAME,
    DOMAIN,
    MODE_BULLETIN,
    MODE_IMIS,
    MODE_STUDY_PLOT,
    STATIONS_KEY,
)
from .coordinator import API_ERRORS, async_resolve_sector
from .imis import async_fetch_stations, stations_by_distance
from .localization import t
from .study_plot import async_fetch_study_plot_stations

_LOGGER = logging.getLogger(__name__)

# Shown as "cannot connect": the service is unreachable, or (UpdateFailed) it
# knows no warning region for the coordinates.
_CANNOT_CONNECT = (UpdateFailed, *API_ERRORS)

# Station type markers shown in the picker. Study plots have no type; they
# show their canton instead.
_TYPE_LABEL = {
    "SNOW_FLAT": "❄",
    "SNOW_SLOPE": "❄⛰",
    "WIND": "🌬",
    "FLOWCAPT": "🌬❄",
}


def _station_options(stations: list[dict]) -> list[SelectOptionDict]:
    """Selector options for all stations, nearest first, searchable by name."""
    return [
        SelectOptionDict(
            value=s["code"],
            label=(
                f"{s.get('label') or s['code']} "
                f"({s['code']}, {round(s.get('elevation') or 0)} m, "
                f"{s['distance_km']} km) "
                f"{_TYPE_LABEL.get(s.get('type')) or s.get('canton_code') or ''}"
            ).rstrip(),
        )
        for s in stations
    ]


def _selected_stations(stations: list[dict], codes: list[str]) -> list[dict]:
    """Compact metadata of the selected stations for storage in the entry."""
    by_code = {s["code"]: s for s in stations}
    result = []
    for code in codes:
        s = by_code.get(code)
        if s is None:
            continue
        result.append(
            {
                "code": s["code"],
                "label": s.get("label") or s["code"],
                "elevation": s.get("elevation"),
                "lat": s.get("lat"),
                "lon": s.get("lon"),
                "type": s.get("type"),
                "distance_km": s.get("distance_km"),
            }
        )
    return result


def _stations_configured_elsewhere(
    hass: HomeAssistant, mode: str, exclude_entry_id: str | None = None
) -> set[str]:
    """Codes of the stations that are already favourites of another entry of the mode."""
    codes: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id:
            continue
        if entry.data.get(CONF_MODE, MODE_BULLETIN) != mode:
            continue
        codes.update(s["code"] for s in entry_stations(entry))
    return codes


def _station_title(hass: HomeAssistant, mode: str, stations: list[dict]) -> str:
    labels = [s["label"] for s in stations]
    shown = ", ".join(labels[:2])
    if len(labels) > 2:
        shown += f" +{len(labels) - 2}"
    prefix = "IMIS" if mode == MODE_IMIS else t("study_plot_entry_title", hass)
    return f"{prefix} {shown}" if shown else prefix


def _bulletin_schema(defaults: dict[str, Any]) -> vol.Schema:
    name_key = (
        vol.Optional(CONF_NAME, default=defaults[CONF_NAME])
        if defaults.get(CONF_NAME)
        else vol.Optional(CONF_NAME)
    )
    return vol.Schema(
        {
            name_key: str,
            vol.Required(CONF_LATITUDE, default=defaults[CONF_LATITUDE]): vol.Coerce(float),
            vol.Required(CONF_LONGITUDE, default=defaults[CONF_LONGITUDE]): vol.Coerce(float),
        }
    )


def _station_schema(mode: str, stations: list[dict], current_codes: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(STATIONS_KEY[mode], default=current_codes): SelectSelector(
                SelectSelectorConfig(
                    options=_station_options(stations),
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


async def _async_fetch_station_list(hass: HomeAssistant, mode: str) -> list[dict]:
    """Every station of the mode, sorted by distance from home."""
    if mode == MODE_IMIS:
        stations = await async_fetch_stations(hass)
    else:
        stations = await async_fetch_study_plot_stations(hass)
    return stations_by_distance(stations, hass.config.latitude, hass.config.longitude)


def _selection_errors(
    hass: HomeAssistant, mode: str, stations: list[dict], entry: ConfigEntry | None
) -> dict[str, str]:
    """Errors of a station selection: none chosen, or one taken by another entry."""
    taken = _stations_configured_elsewhere(hass, mode, entry.entry_id if entry else None)
    if not stations:
        return {STATIONS_KEY[mode]: "no_station"}
    if any(s["code"] in taken for s in stations):
        return {STATIONS_KEY[mode]: "station_already_configured"}
    return {}


class SlfAvalancheConfigFlow(ConfigFlow, domain=DOMAIN):
    """Mode menu first: avalanche bulletin per location, IMIS station or
    study plot favourites."""

    VERSION = 1

    def __init__(self) -> None:
        self._stations: dict[str, list[dict]] = {}

    async def _async_station_list(self, mode: str) -> list[dict] | None:
        """Every station of the mode sorted by distance from home, fetched
        once per flow; None when the list could not be read."""
        if not self._stations.get(mode):
            try:
                self._stations[mode] = await _async_fetch_station_list(self.hass, mode)
            except API_ERRORS:
                return None
        return self._stations[mode]

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="user", menu_options=["bulletin", "imis", "study_plot"]
        )
    # ---------------------------------------------------------- bulletin

    async def async_step_bulletin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up the avalanche bulletin for a location (one instance per location)."""
        return await self._async_bulletin_form("bulletin", user_input, None)

    async def async_step_reconfigure_bulletin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_bulletin_form(
            "reconfigure_bulletin", user_input, self._get_reconfigure_entry()
        )

    async def _async_bulletin_form(
        self, step_id: str, user_input: dict[str, Any] | None, entry: ConfigEntry | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            lat = user_input[CONF_LATITUDE]
            lon = user_input[CONF_LONGITUDE]
            unique_id = f"{round(lat, 3)}_{round(lon, 3)}"
            await self.async_set_unique_id(unique_id)
            if entry is None or unique_id != entry.unique_id:
                self._abort_if_unique_id_configured()

            try:
                sector = await async_resolve_sector(self.hass, lat, lon)
            except _CANNOT_CONNECT:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - shown as "unknown", logged for the bug report
                _LOGGER.exception("Unexpected error while resolving the SLF warning region")
                errors["base"] = "unknown"
            else:
                name = (user_input.get(CONF_NAME) or "").strip() or sector["sector_name"]
                title = t("device_name", self.hass, name=name)
                data = {
                    CONF_MODE: MODE_BULLETIN,
                    CONF_NAME: name,
                    CONF_LATITUDE: lat,
                    CONF_LONGITUDE: lon,
                }
                if entry is None:
                    return self.async_create_entry(title=title, data=data)
                # Update only: the entry's update listener reloads it.
                self.hass.config_entries.async_update_entry(
                    entry, unique_id=unique_id, title=title, data=data
                )
                return self.async_abort(reason="reconfigure_successful")

        defaults = {
            CONF_NAME: entry.data.get(CONF_NAME, "") if entry else "",
            CONF_LATITUDE: entry.data.get(CONF_LATITUDE, self.hass.config.latitude)
            if entry
            else self.hass.config.latitude,
            CONF_LONGITUDE: entry.data.get(CONF_LONGITUDE, self.hass.config.longitude)
            if entry
            else self.hass.config.longitude,
        }
        schema = _bulletin_schema(defaults)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    # ------------------------------------------------- imis / study plots

    async def async_step_imis(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set up favourite IMIS measuring stations (independent of a bulletin)."""
        return await self._async_station_form("imis", MODE_IMIS, user_input, None)

    async def async_step_reconfigure_imis(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_station_form(
            "reconfigure_imis", MODE_IMIS, user_input, self._get_reconfigure_entry()
        )

    async def async_step_study_plot(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up favourite study plots (manual measuring fields)."""
        return await self._async_station_form("study_plot", MODE_STUDY_PLOT, user_input, None)

    async def async_step_reconfigure_study_plot(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_station_form(
            "reconfigure_study_plot", MODE_STUDY_PLOT, user_input, self._get_reconfigure_entry()
        )

    async def _async_station_form(
        self,
        step_id: str,
        mode: str,
        user_input: dict[str, Any] | None,
        entry: ConfigEntry | None,
    ) -> ConfigFlowResult:
        key = STATIONS_KEY[mode]
        errors: dict[str, str] = {}

        if user_input is not None:
            stations = _selected_stations(
                self._stations.get(mode) or [], user_input.get(key) or []
            )
            errors = _selection_errors(self.hass, mode, stations, entry)
            if not errors:
                title = _station_title(self.hass, mode, stations)
                data = {CONF_MODE: mode, key: stations}
                if entry is None:
                    return self.async_create_entry(title=title, data=data)
                # The new selection goes to the data; an older selection in
                # the options would win over it, so it is dropped.
                # Update only: the entry's update listener reloads it.
                self.hass.config_entries.async_update_entry(
                    entry,
                    title=title,
                    data=data,
                    options={k: v for k, v in entry.options.items() if k != key},
                )
                return self.async_abort(reason="reconfigure_successful")

        stations = await self._async_station_list(mode)
        if stations is None:
            return self.async_abort(reason=f"{mode}_unavailable")

        current_codes = [s["code"] for s in entry_stations(entry)] if entry else []
        return self.async_show_form(
            step_id=step_id,
            data_schema=_station_schema(mode, stations, current_codes),
            errors=errors,
        )

    # ------------------------------------------------------- reconfigure

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change an entry with the form of its own mode."""
        mode = self._get_reconfigure_entry().data.get(CONF_MODE, MODE_BULLETIN)
        if mode == MODE_IMIS:
            return await self.async_step_reconfigure_imis(user_input)
        if mode == MODE_STUDY_PLOT:
            return await self.async_step_reconfigure_study_plot(user_input)
        return await self.async_step_reconfigure_bulletin(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SlfAvalancheOptionsFlow()


class SlfAvalancheOptionsFlow(OptionsFlow):
    """Manage the favourite stations of an IMIS or study plot entry."""

    def __init__(self) -> None:
        self._stations: list[dict] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        mode = self.config_entry.data.get(CONF_MODE, MODE_BULLETIN)
        if mode == MODE_STUDY_PLOT:
            return await self.async_step_study_plot(user_input)
        if mode != MODE_IMIS:
            return self.async_abort(reason="no_options")
        return await self._async_station_form("init", mode, user_input)

    async def async_step_study_plot(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_station_form("study_plot", MODE_STUDY_PLOT, user_input)

    async def _async_station_form(
        self, step_id: str, mode: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        key = STATIONS_KEY[mode]
        errors: dict[str, str] = {}
        if user_input is not None:
            stations = _selected_stations(self._stations, user_input.get(key) or [])
            errors = _selection_errors(self.hass, mode, stations, entry)
            if not errors:
                return self.async_create_entry(title="", data={key: stations})

        if not self._stations:
            try:
                self._stations = await _async_fetch_station_list(self.hass, mode)
            except API_ERRORS:
                return self.async_abort(reason=f"{mode}_unavailable")
            except Exception:  # noqa: BLE001 - shown as "unknown", logged for the bug report
                _LOGGER.exception("Unexpected error while fetching the station list")
                return self.async_abort(reason="unknown")

        current_codes = [s["code"] for s in entry_stations(entry)]
        return self.async_show_form(
            step_id=step_id,
            data_schema=_station_schema(mode, self._stations, current_codes),
            errors=errors,
        )
