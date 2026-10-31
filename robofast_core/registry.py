"""The value registry (design.md 3.8; after RTS2's): named values, each with
the time it was measured and how long it stays valid.

Devices and services *publish* values; status pages and the FITS header
builder read *snapshots*. A value older than its ``max_age`` at the moment
of the snapshot -- or one whose last read failed -- is **stale**: the header
builder writes it as undefined, with the old value and its age in the
comment, never as current (:mod:`robofast_core.fits_writer`).

Names are ``<scope>.<name>``. The scope is a telescope name (``t1.ra``,
``t1.ccd_temp``: only that telescope's frames get them) or a shared one
(``site``, ``software``, ``weather``, ``enclosure``, ``sky``). Each value
carries the FITS keyword and comment it is written under (None: status
only) and its unit.

Publishers:

* the sequencer's frame pipeline (:mod:`robofast_core.imaging`): telescope
  pointing, focuser, rotator and filter at each exposure start; camera
  state at each exposure end -- read through the generation's own grants;
* :class:`Telemetry` (a background task): the weather bridge's
  ObservingConditions (read-only), the enclosure and the weather verdict from
  the safety supervisor's status, the Sun and Moon (astropy); and the
  static values (site, software version).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Value:
    """One published value."""

    name: str
    value: Any
    #: When it was measured (aware UTC).
    measured: datetime
    #: Seconds it stays current; None: never goes stale (site, version).
    max_age: float | None
    unit: str = ""
    #: FITS keyword (<= 8 characters) and comment; None: not in headers.
    keyword: str | None = None
    comment: str = ""
    #: The last attempt to refresh it failed (it is stale until refreshed).
    error: str | None = None

    def age(self, now: datetime) -> float:
        return (now - self.measured).total_seconds()

    def stale(self, now: datetime) -> bool:
        if self.error is not None:
            return True
        return self.max_age is not None and self.age(now) > self.max_age

    def as_json(self, now: datetime) -> dict[str, Any]:
        v = self.value
        if isinstance(v, float) and not math.isfinite(v):
            v = str(v)
        return {
            "value": v,
            "unit": self.unit,
            "measured": self.measured.isoformat(),
            "age": round(self.age(now), 3),
            "stale": self.stale(now),
            **({"error": self.error} if self.error else {}),
        }


@dataclass(frozen=True)
class Snapshot:
    """The registry at one instant: ``values`` by name, judged at ``taken``."""

    taken: datetime
    values: dict[str, Value]

    def get(self, name: str) -> Value | None:
        return self.values.get(name)

    def current(self, name: str, default: Any = None) -> Any:
        """The value if present and not stale, else ``default``."""
        v = self.values.get(name)
        return default if v is None or v.stale(self.taken) else v.value

    def stale(self, name: str) -> bool:
        v = self.values.get(name)
        return v is not None and v.stale(self.taken)


class ValueRegistry:
    """Named, time-stamped values (module doc). Used from one event loop."""

    def __init__(self) -> None:
        self._values: dict[str, Value] = {}

    def publish(
        self,
        name: str,
        value: Any,
        *,
        max_age: float | None,
        measured: datetime | None = None,
        unit: str = "",
        keyword: str | None = None,
        comment: str = "",
    ) -> Value:
        if keyword is not None and len(keyword) > 8:
            raise ValueError(f"FITS keyword {keyword!r} longer than 8 characters")
        v = Value(name, value, measured or utcnow(), max_age, unit, keyword, comment)
        self._values[name] = v
        return v

    def failed(self, name: str, error: str) -> None:
        """The last refresh of ``name`` failed: it is stale until the next
        successful one (its old value is kept, for the record)."""
        old = self._values.get(name)
        if old is not None:
            self._values[name] = replace(old, error=error)

    def get(self, name: str) -> Value | None:
        return self._values.get(name)

    def snapshot(
        self, scopes: Iterable[str] | None = None, now: datetime | None = None
    ) -> Snapshot:
        """The values whose scope is in ``scopes`` (all, if None)."""
        if scopes is None:
            values = dict(self._values)
        else:
            want = set(scopes)
            values = {k: v for k, v in self._values.items() if k.split(".", 1)[0] in want}
        return Snapshot(now or utcnow(), values)

    def as_json(self, now: datetime | None = None) -> dict[str, Any]:
        """For status pages."""
        now = now or utcnow()
        return {k: v.as_json(now) for k, v in sorted(self._values.items())}


# -- static values ---------------------------------------------------------------


def software_version() -> str:
    """``git describe`` of this checkout (``unknown`` outside one)."""
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--tags"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def publish_static(
    registry: ValueRegistry,
    *,
    site_name: str,
    site: dict[str, float] | None,
    version: str | None = None,
) -> None:
    """Site and software version: published once, never stale."""
    registry.publish(
        "site.name", site_name, max_age=None, keyword="OBSERVAT", comment="Observatory site"
    )
    if site:
        for key, kw, unit, comment in (
            ("latitude", "SITELAT", "deg", "Site latitude (north)"),
            ("longitude", "SITELONG", "deg", "Site longitude (east)"),
            ("elevation", "SITEELEV", "m", "Site elevation"),
        ):
            if key in site:
                registry.publish(
                    f"site.{key}",
                    float(site[key]),
                    max_age=None,
                    unit=unit,
                    keyword=kw,
                    comment=comment,
                )
    registry.publish(
        "software.version",
        version or software_version(),
        max_age=None,
        keyword="SWVER",
        comment="robofast version (git describe)",
    )


# -- background publishers --------------------------------------------------------

#: ObservingConditions members -> (keyword, unit, comment).
WEATHER_MEMBERS: dict[str, tuple[str, str, str]] = {
    "Temperature": ("AMBTEMP", "C", "Ambient temperature"),
    "Humidity": ("HUMIDITY", "%", "Relative humidity"),
    "DewPoint": ("DEWPOINT", "C", "Dew point"),
    "Pressure": ("PRESSURE", "hPa", "Atmospheric pressure"),
    "WindSpeed": ("WINDSPD", "m/s", "Wind speed"),
    "WindGust": ("WINDGUST", "m/s", "Wind gust"),
    "WindDirection": ("WINDDIR", "deg", "Wind direction (east of north)"),
    "SkyTemperature": ("SKYTEMP", "C", "Sky temperature"),
    "CloudCover": ("CLOUDCOV", "%", "Cloud cover"),
    "RainRate": ("RAINRATE", "mm/h", "Rain rate"),
}


class Telemetry:
    """Keeps the shared scopes of a :class:`ValueRegistry` fresh.

    * ``weather`` -- an ObservingConditions owner (the weather bridge's;
      only read). Each sensor's measurement time is now minus its
      ``TimeSinceLastUpdate`` when the server reports it. A member the
      server does not implement is not asked again.
    * ``supervisor`` -- the safety supervisor (local, or a
      :class:`~robofast_core.lease_client.RemoteSupervisor`): its
      ``status()`` gives the shutter and the weather verdict.
    * ``site`` -- (latitude, longitude, elevation) for the Sun and Moon.
    """

    def __init__(
        self,
        registry: ValueRegistry,
        *,
        weather: Any = None,
        supervisor: Any = None,
        site: dict[str, float] | None = None,
        weather_interval: float = 30.0,
        weather_max_age: float = 300.0,
        enclosure_interval: float = 10.0,
        sky_interval: float = 60.0,
        timeout: float = 5.0,
    ) -> None:
        self.registry = registry
        self.weather = weather
        self.supervisor = supervisor
        self.site = site
        #: The time the Sun and Moon are computed for (robofast_core.clock).
        self.clock: Callable[[], datetime] = utcnow
        self.weather_interval = weather_interval
        self.weather_max_age = weather_max_age
        self.enclosure_interval = enclosure_interval
        self.sky_interval = sky_interval
        self.timeout = timeout
        self._grant = weather.grant("telemetry") if weather is not None else None
        self._unsupported: set[str] = set()

    async def run(self) -> None:
        loops = []
        if self._grant is not None:
            loops.append(self._every(self.weather_interval, self.poll_weather))
        if self.supervisor is not None:
            loops.append(self._every(self.enclosure_interval, self.poll_enclosure))
        if self.site is not None:
            loops.append(self._every(self.sky_interval, self.poll_sky))
        await asyncio.gather(*loops)

    async def _every(self, interval: float, fn: Any) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telemetry: %s", getattr(fn, "__name__", fn))
            await asyncio.sleep(interval)

    async def poll_weather(self) -> None:
        from robofast_core.alpaca_client import NotImplementedException

        assert self._grant is not None
        for member, (kw, unit, comment) in WEATHER_MEMBERS.items():
            if member in self._unsupported:
                continue
            name = f"weather.{member.lower()}"
            try:
                value = float(await self._grant.get(member, timeout=self.timeout))
            except NotImplementedException:
                self._unsupported.add(member)
                continue
            except Exception as exc:
                self.registry.failed(name, f"{type(exc).__name__}: {exc}")
                continue
            measured = utcnow()
            try:
                age = float(
                    await self._grant.call("TimeSinceLastUpdate", member, timeout=self.timeout)
                )
                if math.isfinite(age) and age >= 0:
                    measured = datetime.fromtimestamp(measured.timestamp() - age, UTC)
            except Exception:
                pass  # the read time stands in for the measurement time
            self.registry.publish(
                name,
                value,
                max_age=self.weather_max_age,
                measured=measured,
                unit=unit,
                keyword=kw,
                comment=comment,
            )

    async def poll_enclosure(self) -> None:
        try:
            status = self.supervisor.status()
            if inspect.isawaitable(status):
                status = await asyncio.wait_for(status, self.timeout)
        except Exception as exc:
            for n in ("enclosure.shutter", "enclosure.safe"):
                self.registry.failed(n, f"supervisor status: {exc!r}")
            return
        age = 3 * self.enclosure_interval
        self.registry.publish(
            "enclosure.shutter",
            str(status.get("shutter") or "unknown"),
            max_age=age,
            keyword="SHUTTER",
            comment="Enclosure shutter state",
        )
        self.registry.publish(
            "enclosure.safe",
            bool(status.get("safe")),
            max_age=age,
            keyword="WXSAFE",
            comment="Safety supervisor: weather safe",
        )

    async def poll_sky(self) -> None:
        assert self.site is not None
        values = await asyncio.to_thread(sun_and_moon, self.site, self.clock())
        for name, (value, unit, kw, comment) in values.items():
            self.registry.publish(
                f"sky.{name}",
                value,
                max_age=2 * self.sky_interval,
                unit=unit,
                keyword=kw,
                comment=comment,
            )


def sun_and_moon(site: dict[str, float], when: datetime) -> dict[str, tuple[Any, str, str, str]]:
    """Sun altitude, Moon altitude, position and illuminated fraction
    (astropy; no downloads). Values: (value, unit, keyword, comment)."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_body
    from astropy.time import Time

    from robofast_core.sky import offline_iers

    offline_iers()
    loc = EarthLocation.from_geodetic(
        site["longitude"] * u.deg, site["latitude"] * u.deg, site.get("elevation", 0.0) * u.m
    )
    t = Time(when)
    frame = AltAz(obstime=t, location=loc)
    sun = get_body("sun", t, loc)
    moon = get_body("moon", t, loc)
    elong = sun.separation(moon).rad
    # Illuminated fraction from the Sun-Moon elongation (good to ~1%).
    illum = (1 - math.cos(elong)) / 2
    moon_icrs = moon.icrs
    return {
        "sun_alt": (
            round(float(sun.transform_to(frame).alt.deg), 3),
            "deg",
            "SUNALT",
            "Sun altitude",
        ),
        "moon_alt": (
            round(float(moon.transform_to(frame).alt.deg), 3),
            "deg",
            "MOONALT",
            "Moon altitude",
        ),
        "moon_illum": (round(illum, 4), "", "MOONILLU", "Moon illuminated fraction"),
        "moon_ra": (float(moon_icrs.ra.deg), "deg", None, "Moon RA (ICRS)"),
        "moon_dec": (float(moon_icrs.dec.deg), "deg", None, "Moon Dec (ICRS)"),
    }
