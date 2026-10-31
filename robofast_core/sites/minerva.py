"""MINERVA (Fred Lawrence Whipple Observatory ridge) weather pages.

The formats follow the legacy ``minerva_library/env.py`` in
MinervaCollaboration/minerva-control (``getWeatherRidge``, ``getWeather``,
``getWeatherMearth``), but none of its known bugs:

* staleness is judged by the bridge from each reading's own timestamp, at
  every evaluation (legacy froze ``utcnow() - 5 min`` into its limits at
  startup and kept the last good reading after a failed fetch);
* the cloud page is parsed by an explicit, configured column layout per
  field count, and a sensor that is absent from the current layout or
  reports a connection-loss sentinel is *unknown*, never a shifted column;
* the cloud page's local timestamps (MST, UTC-7, no DST in Arizona) are
  converted with a configured offset.

Source types registered here (see :data:`robofast_core.weather_sources.SOURCE_TYPES`):

``minerva.ridge``
    ``linmax.sao.arizona.edu/weather/weather.cur_cond``: a first line
    ``YYYY, MM, DD, hh, mm, ss, fff`` (UTC), then ``key=value`` lines
    (``outsideTemp``, ``windSpeed``, ``barometer``, ``wxt510Rain``, ...).
``minerva.sky_temps``
    ``linmax.sao.arizona.edu/temps/sky_temps_now``: one line
    ``Mon-DD-YYYY hh:mm:ss v1 v2 ...`` in local time; which sensor each
    value belongs to depends on how many there are (option ``layouts``).
``minerva.mearth``
    ``mearth.sao.arizona.edu/weather/now``: 15 whitespace-separated
    numbers, the first an MJD (UTC). Disabled in legacy since 2022-10-20.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from robofast_core.weather_sources import (
    SOURCE_TYPES,
    HttpSource,
    Reading,
    SourceConfig,
    SourceError,
)

_MJD_UNIX = 40587.0  # MJD of 1970-01-01


def _float(text: str) -> float | None:
    try:
        v = float(text)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


class RidgeSource(HttpSource):
    """The ridge weather station page (UTC timestamp, then ``key=value``)."""

    TIME_FORMAT = "%Y, %m, %d, %H, %M, %S, %f"

    def __init__(self, config: SourceConfig, **kw: Any) -> None:
        if config.options:
            raise ValueError(f"source {config.name}: unknown options {sorted(config.options)}")
        super().__init__(config, **kw)

    def parse(self, text: str, now: datetime) -> Reading:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise SourceError("empty ridge weather page")
        try:
            when = datetime.strptime(lines[0], self.TIME_FORMAT).replace(tzinfo=UTC)
        except ValueError as exc:
            raise SourceError(f"bad ridge timestamp {lines[0]!r}") from exc
        values: dict[str, float | None] = {}
        notes = []
        for line in lines[1:]:
            key, sep, value = line.partition("=")
            if not sep or not key.strip():
                raise SourceError(f"bad ridge line {line!r}")
            v = _float(value.strip())
            if v is None:
                notes.append(f"{key.strip()}={value.strip()!r} is not a number")
            values[key.strip()] = v
        return Reading(self.name, when, values, notes=tuple(notes))


class SkyTempsSource(HttpSource):
    """The cloud-sensor page: local timestamp, then one sky temperature per sensor.

    Options:

    ``layouts``
        ``{field_count: [sensor name or null, ...]}`` -- the sensor of each
        value column (after the date and time) for each total field count. A
        line with a count not listed is an error (all sensors unknown); a
        ``null`` column is ignored.
    ``sensors``
        ``{name: {scale, offset, missing_values, rain_values}}``. The value
        reported is the calibrated ``(raw - offset) / scale`` (legacy
        ``cloudScale``: its limits were ``limit * scale + offset`` in raw
        units, which is the same comparison). A raw value in
        ``missing_values`` (connection loss) is unknown; one in
        ``rain_values`` is unknown *and* signals rain.
    ``utc_offset_hours``
        The page's fixed offset from UTC (MINERVA: -7, MST, no DST).
    """

    TIME_FORMAT = "%b-%d-%Y %H:%M:%S"

    def __init__(self, config: SourceConfig, **kw: Any) -> None:
        opts = dict(config.options)
        try:
            layouts = opts.pop("layouts")
            sensors = opts.pop("sensors")
            offset = float(opts.pop("utc_offset_hours"))
        except KeyError as exc:
            raise ValueError(f"source {config.name}: missing option {exc}") from None
        if opts:
            raise ValueError(f"source {config.name}: unknown options {sorted(opts)}")
        self.tz = timezone(timedelta(hours=offset))
        self.sensors: dict[str, dict[str, Any]] = {}
        for name, spec in sensors.items():
            spec = dict(spec or {})
            cal = {
                "scale": float(spec.pop("scale", 1.0)),
                "offset": float(spec.pop("offset", 0.0)),
                "missing_values": [float(v) for v in spec.pop("missing_values", [])],
                "rain_values": [float(v) for v in spec.pop("rain_values", [])],
            }
            if spec:
                raise ValueError(f"source {config.name}: sensor {name}: unknown {sorted(spec)}")
            if cal["scale"] <= 0:
                raise ValueError(f"source {config.name}: sensor {name}: scale must be > 0")
            self.sensors[name] = cal
        self.layouts: dict[int, list[str | None]] = {}
        for count, columns in layouts.items():
            n = int(count)
            if len(columns) != n - 2:
                raise ValueError(
                    f"source {config.name}: layout for {n} fields needs {n - 2} columns"
                )
            for col in columns:
                if col is not None and col not in self.sensors:
                    raise ValueError(f"source {config.name}: layout names unknown sensor {col}")
            self.layouts[n] = list(columns)
        super().__init__(config, **kw)

    def fields(self) -> set[str]:
        return set(self.sensors)

    def parse(self, text: str, now: datetime) -> Reading:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise SourceError("empty cloud page")
        parts = lines[-1].split()
        layout = self.layouts.get(len(parts))
        if layout is None:
            raise SourceError(
                f"cloud page has {len(parts)} fields, no configured layout: {lines[-1]!r}"
            )
        try:
            local = datetime.strptime(" ".join(parts[:2]), self.TIME_FORMAT)
        except ValueError as exc:
            raise SourceError(f"bad cloud timestamp {' '.join(parts[:2])!r}") from exc
        when = local.replace(tzinfo=self.tz).astimezone(UTC)
        values: dict[str, float | None] = dict.fromkeys(self.sensors)
        notes, rain = [], False
        for name, raw_text in zip(layout, parts[2:], strict=True):
            if name is None:
                continue
            raw = _float(raw_text)
            cal = self.sensors[name]
            if raw is None:
                notes.append(f"{name}: {raw_text!r} is not a number")
            elif raw in cal["rain_values"]:
                notes.append(f"{name}: rain sentinel {raw:g}")
                rain = True
            elif raw in cal["missing_values"]:
                notes.append(f"{name}: connection-loss sentinel {raw:g}")
            else:
                values[name] = (raw - cal["offset"]) / cal["scale"]
        return Reading(self.name, when, values, rain=rain, notes=tuple(notes))


class MearthSource(HttpSource):
    """The MEarth weather page: 15 numbers, the first an MJD (UTC).

    Columns and units as legacy ``getWeatherMearth`` read them; its comments
    flag several as assumptions ("I assume", gust "km/h?"), so field names
    carry the unit the legacy code converted from.
    """

    COLUMNS: Mapping[int, str] = {
        1: "windDirectionDegrees",  # deg ("I assume")
        2: "windSpeed",  # km/h
        3: "outsideTemp",  # C
        4: "outsideDewPt",  # C ("I assume")
        5: "outsideHumidity",  # % ("I assume")
        6: "barometer",  # mmHg (legacy: * 1.33322 -> hPa)
        8: "rainDuration",  # s
        9: "rainIntensity",  # mm/h (legacy also stored it as wxt510Rain)
        10: "hailAccumulation",  # mm
        11: "hailDuration",  # s
        12: "hailIntensity",  # mm/h
        13: "skyTemp",  # C
        14: "windGustSpeed",  # km/h ("km/h?")
    }

    def __init__(self, config: SourceConfig, **kw: Any) -> None:
        if config.options:
            raise ValueError(f"source {config.name}: unknown options {sorted(config.options)}")
        super().__init__(config, **kw)

    def fields(self) -> set[str]:
        return set(self.COLUMNS.values())

    def parse(self, text: str, now: datetime) -> Reading:
        parts = text.split()
        if len(parts) != 15:
            raise SourceError(f"MEarth page has {len(parts)} fields, expected 15")
        mjd = _float(parts[0])
        if mjd is None:
            raise SourceError(f"bad MEarth MJD {parts[0]!r}")
        when = datetime.fromtimestamp((mjd - _MJD_UNIX) * 86400.0, UTC)
        values = {name: _float(parts[i]) for i, name in self.COLUMNS.items()}
        return Reading(self.name, when, values)


def register() -> None:
    SOURCE_TYPES["minerva.ridge"] = lambda cfg, site, **kw: RidgeSource(cfg, **kw)
    SOURCE_TYPES["minerva.sky_temps"] = lambda cfg, site, **kw: SkyTempsSource(cfg, **kw)
    SOURCE_TYPES["minerva.mearth"] = lambda cfg, site, **kw: MearthSource(cfg, **kw)
