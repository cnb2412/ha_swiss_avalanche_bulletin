"""Sensors for the SLF Swiss avalanche bulletin.

Data source: WSL Institute for Snow and Avalanche Research SLF (aws.slf.ch),
licensed under CC BY 4.0 — https://www.slf.ch/en/services-and-products/slf-data-service/
Attribution is required when using/displaying this data.
"""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from . import RuntimeData
from .const import CONF_NAME, DOMAIN, MAX_PROBLEM_SENSORS, VERSION
from .coordinator import ImisCoordinator, SlfAvalancheCoordinator, StudyPlotCoordinator
from .device import device_info
from .localization import danger_level_text, problem_type_text, t

ATTRIBUTION = "Data: WSL Institute for Snow and Avalanche Research SLF (CC BY 4.0)"

# Sensor key -> (unit, device_class, state_class, icon)
IMIS_SENSOR_TYPES: dict[str, tuple] = {
    "snow_height": (UnitOfLength.CENTIMETERS, None, SensorStateClass.MEASUREMENT, "mdi:snowflake"),
    "new_snow_1d": (UnitOfLength.CENTIMETERS, None, SensorStateClass.MEASUREMENT, "mdi:weather-snowy"),
    "air_temperature": (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT, None),
    "snow_surface_temperature": (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT, "mdi:thermometer-low"),
    "humidity": (PERCENTAGE, SensorDeviceClass.HUMIDITY, SensorStateClass.MEASUREMENT, None),
    "wind_speed": (UnitOfSpeed.METERS_PER_SECOND, SensorDeviceClass.WIND_SPEED, SensorStateClass.MEASUREMENT, None),
    "wind_gust": (UnitOfSpeed.METERS_PER_SECOND, SensorDeviceClass.WIND_SPEED, SensorStateClass.MEASUREMENT, "mdi:weather-windy"),
    "wind_direction": (DEGREE, None, SensorStateClass.MEASUREMENT, "mdi:compass-outline"),
}

# Study plots: one manual reading a day.
STUDY_PLOT_SENSOR_TYPES: dict[str, tuple] = {
    "snow_height": (UnitOfLength.CENTIMETERS, None, SensorStateClass.MEASUREMENT, "mdi:snowflake"),
    "new_snow_1d": (UnitOfLength.CENTIMETERS, None, SensorStateClass.MEASUREMENT, "mdi:weather-snowy"),
    "new_snow_water_equivalent": (
        UnitOfPrecipitationDepth.MILLIMETERS,
        SensorDeviceClass.PRECIPITATION,
        SensorStateClass.MEASUREMENT,
        "mdi:water",
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data: RuntimeData = entry.runtime_data

    if data.bulletin is not None:
        coordinator = data.bulletin
        entities: list[SensorEntity] = [
            SlfDangerLevelSensor(hass, coordinator, entry),
            SlfRegionSensor(hass, coordinator, entry),
        ]
        for i in range(MAX_PROBLEM_SENSORS):
            entities.append(SlfProblemSensor(hass, coordinator, entry, i))
        async_add_entities(entities)

    if data.imis is not None:
        _async_setup_station_sensors(
            hass, entry, data.imis, async_add_entities, ImisMeasurementSensor
        )

    if getattr(data, "study_plot", None) is not None:
        _async_setup_station_sensors(
            hass, entry, data.study_plot, async_add_entities, StudyPlotSensor
        )


@callback
def _async_setup_station_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: ImisCoordinator | StudyPlotCoordinator,
    async_add_entities: AddEntitiesCallback,
    sensor_cls: type[_StationSensor],
) -> None:
    """Create the station sensors and keep adding new ones as stations report them.

    Policy: a station gets a sensor for every field it has delivered at least
    once. Sensors already in the entity registry from an earlier run are
    created right away, so a station that is unreachable at startup shows its
    sensors as unavailable instead of not at all; fields present in the
    current data are added at setup and after every refresh, so a station
    that only answers later gets its sensors without a reload. A field the
    station stops delivering keeps its entity and reads unknown - entities
    are only removed together with their station (see
    _cleanup_stale_station_devices in __init__.py).
    """
    registered = _registered_station_sensors(hass, entry, coordinator.stations, sensor_cls)
    created: set[tuple[str, str]] = set()

    @callback
    def _add_new_sensors() -> None:
        new: list[SensorEntity] = []
        for station in coordinator.stations:
            code = station["code"]
            delivered = sensor_cls.delivered_fields(coordinator, code)
            for key in sensor_cls.SENSOR_TYPES:
                if (code, key) in created:
                    continue
                if key not in delivered and (code, key) not in registered:
                    continue
                created.add((code, key))
                new.append(sensor_cls(hass, coordinator, entry, station, key))
        if new:
            async_add_entities(new)

    _add_new_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_sensors))


def _registered_station_sensors(
    hass: HomeAssistant, entry: ConfigEntry, stations: list[dict], sensor_cls: type[_StationSensor]
) -> set[tuple[str, str]]:
    """(station code, sensor key) of the station sensors already in the entity registry."""
    registry = er.async_get(hass)
    return {
        (station["code"], key)
        for station in stations
        for key in sensor_cls.SENSOR_TYPES
        if registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{sensor_cls.KIND}_{station['code']}_{key}"
        )
    }


def _slug(entry: ConfigEntry) -> str:
    name = entry.data.get(CONF_NAME) or entry.entry_id
    return slugify(name)


def _entry_suffix(entry: ConfigEntry) -> str:
    """Last four characters of the entry id, keeping suggested entity ids apart per entry."""
    return entry.entry_id[-4:].lower()


class SlfDangerLevelSensor(CoordinatorEntity[SlfAvalancheCoordinator], SensorEntity):
    """Current avalanche danger level (1-5) for the configured region."""

    _attr_has_entity_name = False
    _attr_attribution = ATTRIBUTION
    _attr_icon = "mdi:alert-octagon-outline"

    def __init__(
        self, hass: HomeAssistant, coordinator: SlfAvalancheCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = t("danger_level_sensor_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_danger_level"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.slf_avalanche_danger_level_{_slug(entry)}_{_entry_suffix(entry)}"

    @property
    def native_value(self):
        return self.coordinator.data.get("danger_level")

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        return {
            "active": d.get("active"),
            "danger_text": danger_level_text(d.get("danger_main_value"), self.hass),
            "region": d.get("sector_name"),
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
            "next_update": d.get("next_update"),
            "publication_time": d.get("publication_time"),
            "unscheduled": d.get("unscheduled"),
            "danger_ratings_raw": d.get("danger_ratings_raw"),
        }


class SlfRegionSensor(CoordinatorEntity[SlfAvalancheCoordinator], SensorEntity):
    """Shows the SLF warning region determined for the configured coordinates."""

    _attr_has_entity_name = False
    _attr_attribution = ATTRIBUTION
    _attr_icon = "mdi:map-marker-radius-outline"

    def __init__(
        self, hass: HomeAssistant, coordinator: SlfAvalancheCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = t("region_sensor_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_region"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.slf_avalanche_region_{_slug(entry)}_{_entry_suffix(entry)}"

    @property
    def native_value(self):
        return self.coordinator.data.get("sector_name")

    @property
    def extra_state_attributes(self):
        return {"sector_id": self.coordinator.data.get("sector_id")}


class SlfProblemSensor(CoordinatorEntity[SlfAvalancheCoordinator], SensorEntity):
    """One of up to 3 currently reported avalanche problems (empty if fewer are reported that day)."""

    _attr_has_entity_name = False
    _attr_attribution = ATTRIBUTION
    _attr_icon = "mdi:snowflake-alert"

    def __init__(
        self, hass: HomeAssistant, coordinator: SlfAvalancheCoordinator, entry: ConfigEntry, index: int
    ) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_name = t("problem_sensor_name", hass, n=index + 1)
        self._attr_unique_id = f"{entry.entry_id}_problem_{index + 1}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = (
            f"sensor.slf_avalanche_problem_{index + 1}_{_slug(entry)}_{_entry_suffix(entry)}"
        )

    def _problem(self) -> dict | None:
        problems = self.coordinator.data.get("avalanche_problems") or []
        if self._index < len(problems):
            return problems[self._index]
        return None

    @property
    def native_value(self):
        p = self._problem()
        if not p:
            return None
        return problem_type_text(p.get("problemType"), self.hass)

    @property
    def extra_state_attributes(self):
        p = self._problem()
        if not p:
            return {}
        elevation = p.get("elevation", {})
        return {
            "danger_level": p.get("dangerRatingValue"),
            "elevation_from": elevation.get("lowerBound"),
            "elevation_to": elevation.get("upperBound"),
            "aspects": p.get("aspects"),
            "comment": p.get("comment"),
        }


class _StationSensor(CoordinatorEntity[ImisCoordinator | StudyPlotCoordinator], SensorEntity):
    """One measured value of a station of the SLF measurement API."""

    _attr_has_entity_name = False
    _attr_attribution = ATTRIBUTION

    # Set by the subclasses. KIND goes into unique ids and device
    # identifiers (<entry_id>_<KIND>_<code>), ENTITY_PREFIX into the
    # suggested entity id, the keys into the localized names.
    SENSOR_TYPES: dict[str, tuple]
    KIND: str
    ENTITY_PREFIX: str
    NAME_PREFIX: str
    DEVICE_NAME_KEY: str
    MODEL_KEY: str

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ImisCoordinator | StudyPlotCoordinator,
        entry: ConfigEntry,
        station: dict,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._station = station
        self._key = key
        code = station["code"]
        unit, device_class, state_class, icon = self.SENSOR_TYPES[key]
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon:
            self._attr_icon = icon
        self._attr_name = t(f"{self.NAME_PREFIX}{key}", hass)
        self._attr_unique_id = f"{entry.entry_id}_{self.KIND}_{code}_{key}"
        # The bundled card and the dashboard strategy find a station's sensors
        # by this pattern, sensor.<prefix>_<station slug>_<key>. The station
        # slug carries the entry suffix so the ids of two entries can never
        # collide. Only a suggestion: entities that already exist keep their
        # registered id.
        self.entity_id = f"sensor.{self.ENTITY_PREFIX}_{slugify(code)}_{_entry_suffix(entry)}_{key}"
        label = station.get("label") or code
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{self.KIND}_{code}")},
            sw_version=VERSION,
            name=t(self.DEVICE_NAME_KEY, hass, name=f"{label} ({code})"),
            manufacturer=t("manufacturer", hass),
            model=t(self.MODEL_KEY, hass),
            entry_type=DeviceEntryType.SERVICE,
        )

    @classmethod
    def delivered_fields(cls, coordinator: ImisCoordinator | StudyPlotCoordinator, code: str) -> set[str]:
        """Sensor keys a station has data for."""
        station_data = (coordinator.data or {}).get(code) or {}
        return set(station_data.get("fields") or [])

    def _station_data(self) -> dict:
        return (self.coordinator.data or {}).get(self._station["code"]) or {}

    @property
    def available(self) -> bool:
        # A station the coordinator could not fetch is missing from its data.
        return super().available and self._station["code"] in (self.coordinator.data or {})

    @property
    def native_value(self):
        return (self._station_data().get("values") or {}).get(self._key)

    def _measured(self) -> str | None:
        """Timestamp of the record the shown value was taken from."""
        return (self._station_data().get("dates") or {}).get(self._key)

    @property
    def extra_state_attributes(self):
        return {
            "station_code": self._station["code"],
            "station_name": self._station.get("label"),
            "elevation": self._station.get("elevation"),
            "distance_km": self._station.get("distance_km"),
            # Timestamp of the record the shown value was taken from; the
            # station's newest record when there is no current value.
            "measure_date": self._measured() or self._station_data().get("measure_date"),
        }


class ImisMeasurementSensor(_StationSensor):
    """One measured value of an IMIS station (official SLF measurement API)."""

    SENSOR_TYPES = IMIS_SENSOR_TYPES
    KIND = "imis"
    ENTITY_PREFIX = "slf_imis"
    NAME_PREFIX = "imis_"
    DEVICE_NAME_KEY = "imis_device_name"
    MODEL_KEY = "imis_model"

    @classmethod
    def delivered_fields(cls, coordinator: ImisCoordinator | StudyPlotCoordinator, code: str) -> set[str]:
        """Sensor keys a station has data for (new snow comes with the snow depth)."""
        fields = super().delivered_fields(coordinator, code)
        station_data = (coordinator.data or {}).get(code) or {}
        if station_data.get("new_snow_1d") is not None or "snow_height" in fields:
            fields.add("new_snow_1d")
        return fields

    @property
    def native_value(self):
        if self._key == "new_snow_1d":
            return self._station_data().get("new_snow_1d")
        return super().native_value

    def _measured(self) -> str | None:
        if self._key == "new_snow_1d":
            return self._station_data().get("daily_measure_date")
        return super()._measured()

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        if self._key == "snow_height":
            attrs["daily_snow_height"] = self._station_data().get("daily_snow_height")
        return attrs


class StudyPlotSensor(_StationSensor):
    """One daily reading of a study plot (manual measuring field)."""

    SENSOR_TYPES = STUDY_PLOT_SENSOR_TYPES
    KIND = "sp"
    ENTITY_PREFIX = "slf_studyplot"
    NAME_PREFIX = "sp_"
    DEVICE_NAME_KEY = "sp_device_name"
    MODEL_KEY = "sp_model"
