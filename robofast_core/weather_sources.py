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
from datetime import datetime
from typing import Any

import httpx

from robofast_core.sun import sun_altitude


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
    """

    source: str
    time: datetime
    values: Mapping[str, float | None]
    rain: bool = False
    notes: tuple[str, ...] = ()


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
    """The Sun's altitude (field ``altitude``, degrees) at the site."""

    live = True

    def __init__(self, config: SourceConfig, *, latitude: float, longitude: float) -> None:
        super().__init__(config)
        self.latitude, self.longitude = latitude, longitude

    async def fetch(self, now: datetime) -> Reading:
        return self.read(now)

    def read(self, now: datetime) -> Reading:
        alt = sun_altitude(now, self.latitude, self.longitude)
        return Reading(self.name, now, {"altitude": alt})

    def fields(self) -> set[str]:
        return {"altitude"}


#: Source type name -> factory(config, site) -> WeatherSource. Site adapters
#: register theirs (see :mod:`robofast_core.sites`).
SourceFactory = Callable[..., WeatherSource]
SOURCE_TYPES: dict[str, SourceFactory] = {
    "sun": lambda cfg, site, **kw: SunSource(
        cfg, latitude=site["latitude"], longitude=site["longitude"]
    ),
}
