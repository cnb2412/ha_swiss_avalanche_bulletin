"""Client for the study plots of the official SLF measurement API.

Study plots ("Vergleichsstationen") are manual measuring fields: an observer
reads snow depth, new snow and its water equivalent once a day, in the
morning (06:00 UTC). Same API, license and attribution as the IMIS stations
(https://measurement-api.slf.ch/, CC BY 4.0).
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import STUDY_PLOT_MEASUREMENTS_URL, STUDY_PLOT_STATIONS_URL
from .imis import merge_records

# Measurement fields exposed as sensors: API key -> canonical short key.
FIELD_MAP = {
    "HS": "snow_height",
    "HN_1D": "new_snow_1d",
    "HNW_1D": "new_snow_water_equivalent",
}

# A reading counts as current until well into the next day, so the value of
# the morning stays shown until the observer has entered the next one, which
# can take a few hours. A day without a reading then reads unknown.
STUDY_PLOT_CURRENT_MAX_AGE = timedelta(hours=36)

# Days of readings asked for: enough that the last reading is still among
# them while the observer is late, the API offers 1, 3 or 7.
PERIOD_IN_DAYS = 3


async def async_fetch_study_plot_stations(hass: HomeAssistant) -> list[dict]:
    """Fetch the list of active study plots."""
    session = async_get_clientsession(hass)
    async with session.get(STUDY_PLOT_STATIONS_URL, timeout=25) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def async_fetch_study_plot_measurements(hass: HomeAssistant, code: str) -> dict:
    """Current readings of one study plot plus its available fields."""
    session = async_get_clientsession(hass)
    async with session.get(
        STUDY_PLOT_MEASUREMENTS_URL.format(code=code),
        params={"period_in_days": PERIOD_IN_DAYS},
        timeout=25,
    ) as resp:
        resp.raise_for_status()
        records = await resp.json(content_type=None)
    return merge_records(records, FIELD_MAP, STUDY_PLOT_CURRENT_MAX_AGE)
