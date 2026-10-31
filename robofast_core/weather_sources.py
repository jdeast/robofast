"""Weather sources for the site weather bridge: fetch, parse, timestamp.

A :class:`WeatherSource` produces a :class:`Reading`: named values plus the
time the *data* was measured (from the data itself, never the fetch time),
so the bridge can judge staleness per source on every evaluation. A failed
fetch or parse raises; the bridge turns that into "unknown" for the source,
never into the last good values.

Concrete pages are parsed by site adapters (:mod:`robofast_core.sites`);
this module has the generic pieces: the HTTP fetch with optional basic auth
from a credentials *file* (never from the config itself), the Sun as a
source, and unit conversions to what ASCOM ObservingConditions reports.
"""

from __future__ import annotations

import abc
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from robofast_core import resp
from robofast_core.sun import LEGACY_PRESSURE_HPA, LEGACY_TEMPERATURE_C, sun_altitude


class SourceError(Exception):
    """A source could not be fetched or its data could not be parsed."""


@dataclass(frozen=True)
class Reading:
    """One fetch of one source.

    ``values`` maps field names to floats; ``None`` means the field is known
    to exist but has no valid value right now (a sensor's connection-loss
    sentinel, an unparsable number). ``time`` is the measurement time (aware,
    UTC) taken from the data. ``rain`` is set when the data itself says it
    is raining (e.g. a cloud sensor's rain sentinel).

    A field measured at its own time (a Redis key with its own timestamp
    key) has that time in ``field_times`` and its own staleness bound in
    ``field_stale_after``; the evaluator judges it at every evaluation, as
    it judges ``time``. Other fields are as old as ``time``.
    """

    source: str
    time: datetime
    values: Mapping[str, float | None]
    rain: bool = False
    notes: tuple[str, ...] = ()
    field_times: Mapping[str, datetime] = field(default_factory=dict)
    field_stale_after: Mapping[str, float] = field(default_factory=dict)


# -- units ------------------------------------------------------------------------

#: unit -> (ASCOM ObservingConditions unit, factor, offset): ascom = v * f + o.
UNITS: dict[str, tuple[str, float, float]] = {
    "degC": ("degC", 1.0, 0.0),
    "degF": ("degC", 5.0 / 9.0, -32.0 * 5.0 / 9.0),
    "percent": ("percent", 1.0, 0.0),
    "deg": ("deg", 1.0, 0.0),
    "m/s": ("m/s", 1.0, 0.0),
    "mph": ("m/s", 0.44704, 0.0),
    "km/h": ("m/s", 1.0 / 3.6, 0.0),
    "knot": ("m/s", 1852.0 / 3600.0, 0.0),
    "hPa": ("hPa", 1.0, 0.0),
    "inHg": ("hPa", 33.8638866667, 0.0),
    "mmHg": ("hPa", 1.33322387415, 0.0),
    "mm/h": ("mm/h", 1.0, 0.0),
    "in/h": ("mm/h", 25.4, 0.0),
}

#: The unit ASCOM ObservingConditions uses for each member we serve.
ASCOM_UNITS: dict[str, str] = {
    "temperature": "degC",
    "dewpoint": "degC",
    "skytemperature": "degC",
    "humidity": "percent",
    "cloudcover": "percent",
    "winddirection": "deg",
    "windspeed": "m/s",
    "windgust": "m/s",
    "pressure": "hPa",
    "rainrate": "mm/h",
}


def to_ascom(value: float, unit: str) -> float:
    _, factor, offset = UNITS[unit]
    return value * factor + offset


def convert(value: float, unit: str, to: str) -> float:
    """``value`` in ``unit`` expressed in ``to`` (both in :data:`UNITS`, same dimension)."""
    if UNITS[unit][0] != UNITS[to][0]:
        raise ValueError(f"cannot convert {unit} to {to}")
    _, factor, offset = UNITS[to]
    return (to_ascom(value, unit) - offset) / factor


# -- credentials ------------------------------------------------------------------


def read_credentials(path: str) -> tuple[str, str]:
    """``(username, password)`` from the first two lines of a file.

    The same format as the legacy ``credentials/webpage.txt``. ``~`` and
    ``$VARS`` in the path are expanded, so the config can name an
    environment variable instead of a fixed path. The file is read on every
    fetch (so it can be rotated) and must not be committed anywhere.
    """
    expanded = os.path.expanduser(os.path.expandvars(path))
    if "$" in expanded:
        raise SourceError(f"credentials path {path!r}: unset environment variable")
    try:
        with open(expanded) as f:
            lines = f.read().splitlines()
    except OSError as exc:
        raise SourceError(f"cannot read credentials file {path!r}: {exc.strerror}") from None
    if len(lines) < 2 or not lines[0].strip():
        raise SourceError(f"credentials file {path!r}: need a username and a password line")
    return lines[0].strip(), lines[1].strip()


# -- sources ----------------------------------------------------------------------


@dataclass
class SourceConfig:
    """The site-independent part of a source's configuration."""

    name: str
    type: str
    #: A required source that is unknown (stale, erroring, missing a limited
    #: field) makes the whole bridge unknown, i.e. unsafe. An advisory one
    #: only contributes when it positively says "unsafe".
    required: bool = True
    #: The data's own timestamp may be at most this old (s).
    stale_after: float = 300.0
    #: Bound on one fetch (connect + read), seconds.
    timeout: float = 10.0
    enabled: bool = True
    url: str | None = None
    credentials_file: str | None = None
    #: Everything else, for the site adapter to interpret.
    options: dict[str, Any] = field(default_factory=dict)


class WeatherSource(abc.ABC):
    """Something that produces :class:`Reading` objects.

    ``live`` sources (the Sun) are computed at every evaluation instead of
    being fetched on the poll schedule.
    """

    live = False

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self.name = config.name

    @abc.abstractmethod
    async def fetch(self, now: datetime) -> Reading:
        """Fetch and parse; raise on any failure."""

    def read(self, now: datetime) -> Reading:
        """A live source's reading for ``now`` (live sources only)."""
        raise NotImplementedError

    def fields(self) -> set[str] | None:
        """The field names this source can produce, if known up front."""
        return None

    async def aclose(self) -> None:
        return None


class HttpSource(WeatherSource):
    """A page fetched over HTTP GET and parsed by :meth:`parse`."""

    def __init__(
        self, config: SourceConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(config)
        if not config.url:
            raise ValueError(f"source {config.name}: needs a url")
        self.url = config.url
        # trust_env=False: no proxies or netrc credentials picked up implicitly.
        self._http = httpx.AsyncClient(trust_env=False, transport=transport, timeout=config.timeout)

    async def fetch(self, now: datetime) -> Reading:
        auth = None
        if self.config.credentials_file:
            auth = read_credentials(self.config.credentials_file)
        try:
            r = await self._http.get(self.url, auth=auth)
        except httpx.HTTPError as exc:
            raise SourceError(f"GET {self.url}: {type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            raise SourceError(f"GET {self.url}: HTTP {r.status_code}")
        try:
            return self.parse(r.text, now)
        except SourceError:
            raise
        except (ValueError, IndexError, KeyError) as exc:
            raise SourceError(f"cannot parse {self.url}: {exc}") from exc

    @abc.abstractmethod
    def parse(self, text: str, now: datetime) -> Reading:
        """Turn the page into a reading; raise on anything unexpected."""

    async def aclose(self) -> None:
        await self._http.aclose()


class SunSource(WeatherSource):
    """The Sun at the site, from astropy (:mod:`robofast_core.sun`).

    Fields: ``altitude`` (apparent, refracted for the configured pressure
    and temperature; geometric if ``pressure_hpa`` is 0) and
    ``altitude_geometric``, both degrees, of the Sun's centre.

    Options (all optional): ``pressure_hpa`` and ``temperature_c`` (default
    PyEphem's 1010 hPa and 15 degC, which the legacy code's refraction
    used), ``relative_humidity`` (0..1, default 0), ``wavelength_um``
    (default 0.55).
    """

    live = True
    _OPTIONS = frozenset({"pressure_hpa", "temperature_c", "relative_humidity", "wavelength_um"})

    def __init__(
        self,
        config: SourceConfig,
        *,
        latitude: float,
        longitude: float,
        elevation: float = 0.0,
    ) -> None:
        super().__init__(config)
        extra = set(config.options) - self._OPTIONS
        if extra:
            raise ValueError(f"source {config.name}: unknown options {sorted(extra)}")
        self.latitude, self.longitude, self.elevation = latitude, longitude, elevation
        opts = config.options
        self.refraction = {
            "pressure_hpa": float(opts.get("pressure_hpa", LEGACY_PRESSURE_HPA)),
            "temperature_c": float(opts.get("temperature_c", LEGACY_TEMPERATURE_C)),
            "relative_humidity": float(opts.get("relative_humidity", 0.0)),
            "wavelength_um": float(opts.get("wavelength_um", 0.55)),
        }
        if self.refraction["pressure_hpa"] < 0:
            raise ValueError(f"source {config.name}: pressure_hpa must be >= 0")

    async def fetch(self, now: datetime) -> Reading:
        return self.read(now)

    def read(self, now: datetime) -> Reading:
        where = (now, self.latitude, self.longitude, self.elevation)
        apparent = sun_altitude(*where, **self.refraction)
        geometric = sun_altitude(*where)
        return Reading(self.name, now, {"altitude": apparent, "altitude_geometric": geometric})

    def fields(self) -> set[str]:
        return {"altitude", "altitude_geometric"}


# -- Redis ------------------------------------------------------------------------


def _number(raw: bytes) -> float | None:
    try:
        v = float(raw.decode().strip())
    except (UnicodeDecodeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


@dataclass(frozen=True)
class RedisField:
    """One reading field taken from one Redis key."""

    key: str
    #: The key holding this value's measurement time.
    time: str
    #: The unit Redis sends it in and the unit the field is reported in
    #: (limits and ``conditions`` use ``to``). Without ``to``: unconverted.
    unit: str | None = None
    to: str | None = None
    #: Values meaning "sensor down" (unknown).
    missing_values: tuple[float, ...] = ()


class RedisSource(WeatherSource):
    """Named values read from one Redis database (``MGET``; never a write).

    ``url``: ``redis://host[:port]/db``. No credentials: the client
    (:mod:`robofast_core.resp`) sends only SELECT and MGET. Options:

    ``fields``
        ``{field: {key, time?, unit?, to?, missing_values?}}``: the explicit
        key -> field mapping. ``time`` names the key holding this value's
        measurement time (default ``time_key``); ``unit``/``to`` convert
        (``{unit: km/h, to: mph}`` keeps the limits in mph whatever the
        server turns out to send); a value in ``missing_values`` is unknown.
    ``time_key``
        The key with the reading's main measurement time (required; if it is
        absent or unreadable the fetch fails). Its staleness bound is the
        source's ``stale_after``.
    ``timestamps``
        ``{key: stale_after}`` for every *other* timestamp key a field uses:
        those fields go stale on their own clocks.
    ``time_format``, ``utc_offset_hours``
        How timestamps are written (default ``%Y-%m-%d %H:%M:%S``) and the
        fixed offset of that local time from UTC (MINERVA: -7, MST).
    """

    def __init__(self, config: SourceConfig, **_: Any) -> None:
        super().__init__(config)
        name = config.name
        if config.credentials_file:
            raise ValueError(f"source {name}: redis sources take no credentials")
        parts = urlsplit(config.url or "")
        if parts.scheme != "redis" or not parts.hostname:
            raise ValueError(f"source {name}: url must be redis://host[:port]/db")
        if parts.username or parts.password or parts.query:
            raise ValueError(f"source {name}: the url takes no credentials or query")
        self.host = parts.hostname
        self.port = parts.port or 6379
        path = parts.path.strip("/") or "0"
        if not path.isdigit():
            raise ValueError(f"source {name}: database {path!r} is not a number")
        self.db = int(path)

        opts = dict(config.options)
        try:
            fields = opts.pop("fields")
            self.time_key = str(opts.pop("time_key"))
        except KeyError as exc:
            raise ValueError(f"source {name}: missing option {exc}") from None
        self.time_format = str(opts.pop("time_format", "%Y-%m-%d %H:%M:%S"))
        self.tz = timezone(timedelta(hours=float(opts.pop("utc_offset_hours", 0.0))))
        self.timestamps = {
            str(k): float(v) for k, v in (opts.pop("timestamps", None) or {}).items()
        }
        if opts:
            raise ValueError(f"source {name}: unknown options {sorted(opts)}")
        if self.time_key in self.timestamps:
            raise ValueError(
                f"source {name}: {self.time_key} is the time_key (its bound is stale_after)"
            )
        self.mapping: dict[str, RedisField] = {}
        for field_name, spec in (fields or {}).items():
            spec = dict(spec or {})
            what = f"source {name}: field {field_name}"
            if "key" not in spec:
                raise ValueError(f"{what}: needs a key")
            unit, to = spec.pop("unit", None), spec.pop("to", None)
            if to is not None and (
                unit not in UNITS or to not in UNITS or UNITS[unit][0] != UNITS[to][0]
            ):
                raise ValueError(f"{what}: cannot convert {unit} to {to}")
            f = RedisField(
                key=str(spec.pop("key")),
                time=str(spec.pop("time", self.time_key)),
                unit=unit,
                to=to,
                missing_values=tuple(float(v) for v in spec.pop("missing_values", [])),
            )
            if spec:
                raise ValueError(f"{what}: unknown {sorted(spec)}")
            if f.time != self.time_key and f.time not in self.timestamps:
                raise ValueError(f"{what}: timestamp key {f.time} is not in timestamps")
            self.mapping[field_name] = f
        if not self.mapping:
            raise ValueError(f"source {name}: no fields")
        keys = [self.time_key, *self.timestamps, *(f.key for f in self.mapping.values())]
        #: Every key read, once each, in one MGET.
        self.keys = list(dict.fromkeys(keys))

    def fields(self) -> set[str]:
        return set(self.mapping)

    def _time(self, raw: bytes | None) -> datetime | None:
        if raw is None:
            return None
        try:
            local = datetime.strptime(raw.decode().strip(), self.time_format)
        except (UnicodeDecodeError, ValueError):
            return None
        return local.replace(tzinfo=self.tz).astimezone(UTC)

    async def fetch(self, now: datetime) -> Reading:
        try:
            raw = await resp.mget(
                self.host, self.port, self.db, self.keys, timeout=self.config.timeout
            )
        except (OSError, TimeoutError, resp.RedisError, ValueError) as exc:
            where = f"redis://{self.host}:{self.port}/{self.db}"
            raise SourceError(f"{where}: {type(exc).__name__}: {exc}") from exc
        return self.parse(dict(zip(self.keys, raw, strict=True)))

    def parse(self, raw: Mapping[str, bytes | None]) -> Reading:
        """A reading from ``{key: value bytes, or None if the key is absent}``."""
        when = self._time(raw.get(self.time_key))
        if when is None:
            raise SourceError(f"{self.time_key}={raw.get(self.time_key)!r} is not a timestamp")
        stamps = {k: self._time(raw.get(k)) for k in self.timestamps}
        values: dict[str, float | None] = {}
        field_times: dict[str, datetime] = {}
        field_stale: dict[str, float] = {}
        notes: list[str] = []
        for name, f in self.mapping.items():
            values[name] = None
            if f.time != self.time_key:
                stamp = stamps[f.time]
                if stamp is None:
                    notes.append(f"{name}: timestamp {f.time}={raw.get(f.time)!r} unreadable")
                    continue
                field_times[name] = stamp
                field_stale[name] = self.timestamps[f.time]
            value = raw.get(f.key)
            if value is None:
                notes.append(f"{name}: key {f.key} absent")
                continue
            v = _number(value)
            if v is None:
                notes.append(f"{name}: {f.key}={value!r} is not a number")
            elif v in f.missing_values:
                notes.append(f"{name}: sensor-down value {v:g}")
            else:
                values[name] = convert(v, f.unit, f.to) if f.unit and f.to else v
        return Reading(
            self.name,
            when,
            values,
            notes=tuple(notes),
            field_times=field_times,
            field_stale_after=field_stale,
        )


#: Source type name -> factory(config, site) -> WeatherSource. Site adapters
#: register theirs (see :mod:`robofast_core.sites`).
SourceFactory = Callable[..., WeatherSource]
SOURCE_TYPES: dict[str, SourceFactory] = {
    "sun": lambda cfg, site, **kw: SunSource(
        cfg,
        latitude=site["latitude"],
        longitude=site["longitude"],
        elevation=float(site.get("elevation", 0.0)),
    ),
    "redis": lambda cfg, site, **kw: RedisSource(cfg),
}
