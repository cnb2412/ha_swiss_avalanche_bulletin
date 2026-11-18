"""Entry setup end to end: the coordinators fetch (patched), the sensor
platform creates the station entities with the ids the card and the
dashboard strategy look for."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.slf_avalanche.const import (
    CONF_IMIS_STATIONS,
    CONF_MODE,
    CONF_STUDY_PLOTS,
    DOMAIN,
    MODE_IMIS,
    MODE_STUDY_PLOT,
)

COORDINATOR = "custom_components.slf_avalanche.coordinator."


def _reading(**values) -> dict:
    now = dt_util.utcnow().isoformat()
    return {
        "values": values,
        "dates": {key: now for key in values},
        "fields": set(values),
        "measure_date": now,
    }


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    # Skips registering the card's static path.
    hass.data["slf_avalanche_static_registered"] = True
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_study_plot_entry_creates_sensors_per_delivered_field(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="01J0STUDYPLOTENTRYAB12",
        title="Study plot Sedrun",
        data={CONF_MODE: MODE_STUDY_PLOT, CONF_STUDY_PLOTS: [{"code": "5SE0", "label": "Sedrun", "elevation": 1413}]},
    )
    with patch(COORDINATOR + "async_fetch_study_plot_measurements", return_value=_reading(snow_height=42.0, new_snow_1d=3.0)):
        await _setup(hass, entry)

    state = hass.states.get("sensor.slf_studyplot_5se0_ab12_snow_height")
    assert state is not None and float(state.state) == 42.0
    assert state.attributes["station_code"] == "5SE0"
    assert state.attributes["unit_of_measurement"] == "cm"
    assert hass.states.get("sensor.slf_studyplot_5se0_ab12_new_snow_1d").state == "3.0"
    # Not delivered by this plot: no sensor.
    assert hass.states.get("sensor.slf_studyplot_5se0_ab12_new_snow_water_equivalent") is None

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_sp_5SE0_snow_height")
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [(d.identifiers, d.name) for d in devices] == [
        ({(DOMAIN, f"{entry.entry_id}_sp_5SE0")}, "SLF Study Plot Sedrun (5SE0)")
    ]


async def test_study_plot_unreachable_station_is_unavailable(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="01J0STUDYPLOTENTRYCD34",
        title="Study plots",
        data={CONF_MODE: MODE_STUDY_PLOT, CONF_STUDY_PLOTS: [{"code": "5SE0", "label": "Sedrun"}, {"code": "5DF0", "label": "Davos"}]},
    )

    async def fetch(hass, code):
        if code == "5DF0":
            raise TimeoutError
        return _reading(snow_height=10.0)

    with patch(COORDINATOR + "async_fetch_study_plot_measurements", side_effect=fetch):
        await _setup(hass, entry)
    assert hass.states.get("sensor.slf_studyplot_5se0_cd34_snow_height").state == "10.0"
    # Never answered, so it never delivered a field: no sensors yet.
    assert hass.states.get("sensor.slf_studyplot_5df0_cd34_snow_height") is None


async def test_imis_entry_keeps_its_ids(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="01J0IMISENTRYXXXXXEF56",
        title="IMIS Weissfluhjoch",
        data={CONF_MODE: MODE_IMIS, CONF_IMIS_STATIONS: [{"code": "WFJ2", "label": "Weissfluhjoch"}]},
    )
    daily = {"WFJ2": {"station_code": "WFJ2", "HN_1D": 7, "HS": 80, "measure_date": "2026-11-18T06:00:00Z"}}
    with (
        patch(COORDINATOR + "async_fetch_station_measurements", return_value=_reading(snow_height=81.0, air_temperature=-4.5)),
        patch(COORDINATOR + "async_fetch_daily_snow", return_value=daily),
    ):
        await _setup(hass, entry)

    hs = hass.states.get("sensor.slf_imis_wfj2_ef56_snow_height")
    assert float(hs.state) == 81.0 and hs.attributes["daily_snow_height"] == 80
    hn = hass.states.get("sensor.slf_imis_wfj2_ef56_new_snow_1d")
    assert hn.state == "7" and hn.attributes["measure_date"] == "2026-11-18T06:00:00Z"
    assert hass.states.get("sensor.slf_imis_wfj2_ef56_air_temperature").state == "-4.5"
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_imis_WFJ2_snow_height")
