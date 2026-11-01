"""The simulated sky: real catalog stars at their pixels, through a mount
that does not point where it is told.

:class:`SkyScene` renders what a camera on a telescope sees (``docs/sky.md``):

1. **Where the telescope really points.** The mount is *commanded* to
   (RA, Dec); its pointing-error model (:class:`robofast_core.pointing.PointingModel`,
   TPOINT terms; the "truth" a pointing-model builder has to recover) says
   where it actually points: the commanded position is the mount
   (encoder) position, :meth:`~robofast_core.pointing.PointingModel.true_from_mount`
   gives the true one. Equatorial models work in hour angle and
   declination (from the local sidereal time), alt-az models in azimuth and
   elevation. Commanded coordinates are taken as ICRS (no precession,
   nutation, aberration or refraction: those are the mount software's
   business and would only add a smooth term).
2. **Which stars are there**: the catalog (:class:`robofast_core.catalog.Catalog`)
   around the true pointing, moved to the date by their proper motions.
3. **Where they land**: a gnomonic projection about the true pointing, the
   camera's plate scale, rotator position angle (``pa``: +y axis east of
   north) and parity, the optical axis at ``crpix``, and an optional
   radial distortion ``k1`` (``r -> r (1 + k1 (r / r_corner)**2)``).
4. **How bright**: each star's G magnitude through the port's zero point
   (:class:`robofast_core.photometry.PortPhotometry`), extinction at the
   airmass; the sky from the Sun, Moon and the dark sky
   (:func:`robofast_core.photometry.sky_brightness`); seeing, defocus,
   noise and the detector by the shared renderer
   (:func:`robofast_core.testing.starfield.render`).

Opt-in users: :class:`SkyFocusScene` (a
:class:`~robofast_core.testing.focus_sim.FocusScene` whose stars follow a
mount, for the star-field cameras of the simulated night) and
:func:`sky_world_stars` (the guider simulator's field stars).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import numpy as np

from robofast_core.catalog import Catalog, CatalogUnavailable, Stars, _tan, _untan, decimal_year
from robofast_core.photometry import PortPhotometry, SkyConditions, sky_brightness
from robofast_core.pointing import PointingModel, altaz_to_hadec, hadec_to_altaz
from robofast_core.testing.focus_sim import FocusScene
from robofast_core.testing.starfield import Detector, Frame, SimOptics, Star, render

log = logging.getLogger(__name__)

__all__ = [
    "SkyCamera",
    "SkyFocusScene",
    "SkyScene",
    "lst_deg",
    "sky_world_stars",
]


def lst_deg(when: datetime, longitude: float) -> float:
    """Local mean sidereal time (deg) at east ``longitude`` (IAU 1982 GMST,
    good to a fraction of an arcsec over decades: plenty for a simulator)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    jd = when.timestamp() / 86400.0 + 2440587.5
    d = jd - 2451545.0
    t = d / 36525.0
    gmst = 280.46061837 + 360.98564736629 * d + 0.000387933 * t * t - t**3 / 38710000.0
    return (gmst + longitude) % 360.0


@dataclass(frozen=True)
class SkyCamera:
    """A camera on the sky: geometry plus the detector."""

    #: Full frame (rows, columns), unbinned.
    shape: tuple[int, int] = (2048, 2048)
    #: arcsec per unbinned pixel.
    plate_scale: float = 0.61
    #: Position angle of +y, deg east of north (the rotator's sky angle).
    pa: float = 0.0
    #: East to the right with north up.
    flip: bool = False
    #: The optical axis (0-based unbinned pixel); None: the chip centre.
    crpix: tuple[float, float] | None = None
    #: Radial distortion at the corner, as a fraction of the radius.
    k1: float = 0.0
    detector: Detector = field(default_factory=lambda: Detector(shape=(2048, 2048)))
    #: Pixel size (um) and focal ratio, for defocus (the shared renderer).
    pixel_size: float = 13.5
    f_ratio: float = 6.5
    obstruction: float = 0.47

    @property
    def centre(self) -> tuple[float, float]:
        if self.crpix is not None:
            return self.crpix
        ny, nx = self.shape
        return (nx - 1) / 2.0, (ny - 1) / 2.0

    @property
    def radius(self) -> float:
        """deg from the optical axis to the farthest corner."""
        ny, nx = self.shape
        cx, cy = self.centre
        far = max(math.hypot(x - cx, y - cy) for x in (0, nx - 1) for y in (0, ny - 1))
        return far * self.plate_scale / 3600.0 * (1 + abs(self.k1))

    def cd(self) -> np.ndarray:
        """deg per pixel (fits_writer's convention)."""
        s = self.plate_scale / 3600.0
        p = math.radians(self.pa)
        parity = 1.0 if self.flip else -1.0
        return np.array(
            [
                [parity * s * math.cos(p), -parity * s * math.sin(p)],
                [s * math.sin(p), s * math.cos(p)],
            ]
        )

    def project(self, ra0: float, dec0: float, ra: Any, dec: Any) -> tuple[np.ndarray, np.ndarray]:
        """0-based pixels of sky positions with the optical axis at (ra0, dec0)."""
        xi, eta = _tan(ra0, dec0, ra, dec)
        w = np.stack([np.degrees(xi), np.degrees(eta)])
        u, v = np.linalg.solve(self.cd(), w)
        if self.k1:
            ny, nx = self.shape
            rc = math.hypot(nx, ny) / 2.0
            f = 1.0 + self.k1 * (u * u + v * v) / rc**2
            u, v = u * f, v * f
        cx, cy = self.centre
        return u + cx, v + cy

    def unproject(self, ra0: float, dec0: float, x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
        """Sky position of pixels (the inverse of :meth:`project`)."""
        cx, cy = self.centre
        u = np.asarray(x, dtype=float) - cx
        v = np.asarray(y, dtype=float) - cy
        if self.k1:
            ny, nx = self.shape
            rc = math.hypot(nx, ny) / 2.0
            ud, vd = u.copy(), v.copy()
            for _ in range(20):
                f = 1.0 + self.k1 * (u * u + v * v) / rc**2
                u, v = ud / f, vd / f
        w = self.cd() @ np.stack([np.atleast_1d(u), np.atleast_1d(v)])
        return _untan(ra0, dec0, np.radians(w[0]), np.radians(w[1]))


@dataclass
class SkyScene:
    """The sky, a mount and a camera (module doc).

    ``site`` is (latitude, east longitude) deg. ``pointing`` is the mount's
    *true* pointing-error model (default: perfect). ``conditions``, if
    given, fixes the Sun and Moon (else they are computed from the time
    with astropy); ``sky_mag`` overrides the sky brightness altogether."""

    catalog: Catalog | Stars
    camera: SkyCamera = field(default_factory=SkyCamera)
    photometry: PortPhotometry = field(default_factory=PortPhotometry)
    site: tuple[float, float] = (31.680407, -110.878977)
    pointing: PointingModel = field(default_factory=PointingModel)
    seeing: float = 1.5
    conditions: SkyConditions | None = None
    sky_mag: float | None = None
    #: Faintest catalog star rendered.
    mag_limit: float | None = None
    seed: int = 1
    rng: np.random.Generator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)
        if self.pointing.latitude == 0.0 and self.site[0] != 0.0:
            self.pointing = replace(self.pointing, latitude=self.site[0])

    # -- the mount -------------------------------------------------------------

    def true_pointing(self, ra: float, dec: float, when: datetime) -> tuple[float, float]:
        """Where the telescope points (deg) when commanded to (ra, dec) deg."""
        if not self.pointing.terms:
            return ra % 360.0, dec
        lst = lst_deg(when, self.site[1])
        ha = (lst - ra + 180.0) % 360.0 - 180.0
        lat = self.site[0]
        if self.pointing.mount == "altaz":
            az, alt = hadec_to_altaz(ha, dec, lat)
            taz, talt = self.pointing.true_from_mount(az, alt)
            tha, tdec = altaz_to_hadec(taz, talt, lat)
        else:
            tha, tdec = self.pointing.true_from_mount(ha, dec)
        return float(lst - tha) % 360.0, float(tdec)

    def command_for(self, ra: float, dec: float, when: datetime) -> tuple[float, float]:
        """What to command (deg) to really point at (ra, dec): the model
        applied forwards (what a perfect pointing model would do)."""
        lst = lst_deg(when, self.site[1])
        ha = (lst - ra + 180.0) % 360.0 - 180.0
        lat = self.site[0]
        if self.pointing.mount == "altaz":
            az, alt = hadec_to_altaz(ha, dec, lat)
            maz, malt = self.pointing.mount_from_true(az, alt)
            mha, mdec = altaz_to_hadec(maz, malt, lat)
        else:
            mha, mdec = self.pointing.mount_from_true(ha, dec)
        return float(lst - mha) % 360.0, float(mdec)

    def altitude(self, ra: float, dec: float, when: datetime) -> float:
        lst = lst_deg(when, self.site[1])
        _, alt = hadec_to_altaz(lst - ra, dec, self.site[0])
        return float(alt)

    # -- the sky ----------------------------------------------------------------

    def sky_rate(self, ra: float, dec: float, when: datetime) -> float:
        """e-/s per unbinned pixel from the sky."""
        if self.sky_mag is not None:
            mu = self.sky_mag
        else:
            c = self.conditions
            if c is None:
                from robofast_core.photometry import sky_conditions

                c = sky_conditions(self.site[0], self.site[1], when, ra, dec)
            else:
                c = replace(c, target_alt=self.altitude(ra, dec, when))
            mu = sky_brightness(c, k=self.photometry.extinction)
        return self.photometry.sky_rate(mu, self.camera.plate_scale)

    def field_stars(self, ra: float, dec: float, when: datetime) -> Stars:
        """Catalog stars around a (true) pointing at ``when``."""
        r = self.camera.radius * 1.05 + 10.0 / 3600.0
        ep = decimal_year(when)
        if isinstance(self.catalog, Stars):
            s = self.catalog.at_epoch(ep).cone(ra, dec, r)
        else:
            s = self.catalog.cone(ra, dec, r, epoch=ep)
        return s.brighter(self.mag_limit)

    def stars(
        self, ra: float, dec: float, when: datetime, *, pa: float | None = None
    ) -> tuple[list[Star], Stars, np.ndarray, np.ndarray]:
        """Renderer stars (pixels, e-/s) for a true pointing; also the
        catalog rows and their exact pixels (ground truth)."""
        cam = self.camera if pa is None else replace(self.camera, pa=pa)
        s = self.field_stars(ra, dec, when)
        x, y = cam.project(ra, dec, s.ra, s.dec)
        alt = self.altitude(ra, dec, when)
        airmass = 1.0 / math.sin(math.radians(max(alt, 5.0)))
        rates = 10.0 ** (-0.4 * (s.g + self.photometry.extinction * airmass - self.photometry.zp))
        ny, nx = cam.shape
        m = 30
        inside = (x > -m) & (x < nx + m) & (y > -m) & (y < ny + m)
        out = [
            Star(float(a), float(b), float(f))
            for a, b, f in zip(x[inside], y[inside], rates[inside], strict=True)
        ]
        return out, s.take(inside), x[inside], y[inside]

    def render(
        self,
        ra: float,
        dec: float,
        when: datetime,
        exposure: float,
        *,
        pa: float | None = None,
        roi: tuple[int, int, int, int] | None = None,
        binning: int = 1,
        defocus: float = 0.0,
        seeing: float | None = None,
        transparency: float = 1.0,
        noise: bool = True,
    ) -> Frame:
        """One exposure with the mount *commanded* to (ra, dec) deg at
        ``when`` (rotator at ``pa``, default the camera's). ``frame.truth``
        adds the true pointing (``ra``, ``dec``, ``pa``), the commanded one,
        and the catalog stars with their exact pixels."""
        tra, tdec = self.true_pointing(ra, dec, when)
        stars, rows, x, y = self.stars(tra, tdec, when, pa=pa)
        if transparency != 1.0:
            stars = [Star(s.x, s.y, s.flux * transparency) for s in stars]
        cam = self.camera
        det = replace(cam.detector, shape=cam.shape, sky=self.sky_rate(tra, tdec, when))
        optics = SimOptics(
            pixel_scale=cam.plate_scale,
            pixel_size=cam.pixel_size,
            f_ratio=cam.f_ratio,
            obstruction=cam.obstruction,
        )
        frame = render(
            stars,
            optics,
            det,
            seeing=self.seeing if seeing is None else seeing,
            defocus=defocus,
            exposure=exposure,
            roi=roi,
            binning=binning,
            noise=noise,
            rng=self.rng,
        )
        frame.truth.update(
            {
                "commanded": (ra % 360.0, dec),
                "ra": tra,
                "dec": tdec,
                "pa": cam.pa if pa is None else pa,
                "plate_scale": cam.plate_scale,
                "flip": cam.flip,
                "sky_e_per_s": det.sky,
                "stars": rows,
                "x": x,
                "y": y,
            }
        )
        return frame


class SkyFocusScene(FocusScene):
    """A :class:`~robofast_core.testing.focus_sim.FocusScene` whose stars
    come from a :class:`SkyScene` at a mount's current pointing (opt-in for
    the simulated night's star-field cameras). ``mount`` has
    ``RightAscension`` (hours) and ``Declination`` (deg) like an Alpaca
    telescope; ``clock`` gives the (UTC) time of each exposure."""

    def __init__(
        self,
        sky: SkyScene,
        mount: Any,
        clock: Callable[[], datetime],
        *,
        true_focus: float = 20000.0,
        focal_um_per_step: float = 1.0,
        effective_exposure: float | None = None,
        seed: int = 1,
    ) -> None:
        cam = sky.camera
        super().__init__(
            [],
            optics=SimOptics(
                pixel_scale=cam.plate_scale,
                pixel_size=cam.pixel_size,
                f_ratio=cam.f_ratio,
                obstruction=cam.obstruction,
            ),
            detector=replace(cam.detector, shape=cam.shape),
            true_focus=true_focus,
            focal_um_per_step=focal_um_per_step,
            seeing=sky.seeing,
            effective_exposure=effective_exposure,
            seed=seed,
        )
        self.sky = sky
        self.mount = mount
        self.clock = clock
        #: Truth of the last frame (true pointing, stars).
        self.last_truth: dict[str, Any] = {}

    def render(self, optical: float, duration: float, roi, binning: int) -> np.ndarray:
        self.frames += 1
        exposure = self.effective_exposure if self.effective_exposure is not None else duration
        ra = float(self.mount.RightAscension) * 15.0
        dec = float(self.mount.Declination)
        try:
            self.sky.field_stars(*self.sky.true_pointing(ra, dec, self.clock()), self.clock())
        except CatalogUnavailable as exc:
            # No catalog for this field: an empty sky (and say so), not an
            # exception inside the camera.
            log.warning("sky simulator: %s; rendering an empty field", exc)
            self.last_truth = {"error": str(exc)}
            ny, nx = roi[3] // binning, roi[2] // binning
            return np.full((ny, nx), int(self.detector.bias), dtype=np.int32)
        frame = self.sky.render(
            ra,
            dec,
            self.clock(),
            max(exposure, 1e-3),
            roi=roi,
            binning=binning,
            defocus=self.defocus(optical),
            transparency=self.transparency,
        )
        self.last_truth = frame.truth
        return frame.image


def sky_world_stars(
    sky: SkyScene,
    ra: float,
    dec: float,
    when: datetime,
    *,
    target: tuple[float, float] | None = None,
) -> tuple[tuple[float, float, float], list[tuple[float, float, float]]]:
    """For the guider simulator (:class:`robofast_core.guider.sim.SimWorld`):
    the star nearest ``target`` (ra, dec deg; default the pointing) and the
    other field stars, as (x, y, e-/s), with the mount commanded to (ra,
    dec). Use as ``SimWorld(star=main[:2], flux=main[2], others=others,
    sensor=(nx, ny))``."""
    tra, tdec = sky.true_pointing(ra, dec, when)
    stars, rows, _, _ = sky.stars(tra, tdec, when)
    if not stars:
        raise ValueError("no catalog stars in the field")
    t_ra, t_dec = target if target is not None else (ra, dec)
    from robofast_core.catalog import separation

    k = int(np.argmin(separation(t_ra, t_dec, rows.ra, rows.dec)))
    main = (stars[k].x, stars[k].y, stars[k].flux)
    others = [(s.x, s.y, s.flux) for i, s in enumerate(stars) if i != k]
    return main, others
