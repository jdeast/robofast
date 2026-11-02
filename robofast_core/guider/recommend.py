"""On-target or offset guiding? A prediction from photon statistics
(docs/offset_guiding.md, "When to offset guide").

For a target of magnitude ``m`` (in the guide camera's band) under a sky of
``sky`` mag/arcsec^2 and seeing ``seeing`` (FWHM, arcsec), with an
instrument's :class:`GuideModel` (aperture, fiber, guide camera and its
exposure), :func:`recommend` predicts the guiding error of each method and
picks one, with the reason:

* **on the target** (the method's key is ``annulus``, the request
  language's word for guiding on the target itself). How the guide camera
  sees the target while it is on the fiber is the instrument's geometry,
  ``on_target``:

  - ``annulus`` -- a mirror round the fiber (TRES): the guider centroids
    the target's light that misses the fiber. A Gaussian star of rms ``s =
    seeing / 2.355`` centred on a fiber of radius ``r`` spills ``exp(-r^2 /
    2 s^2)`` of its light, times ``spill_efficiency`` (how much of that the
    guide optics deliver);
  - ``beamsplitter`` -- a beamsplitter in front of the fiber sends a small
    fraction of the light to the guide camera (MINERVA's FAU, ~2 % by
    design): the guider sees the **whole** star and centroids it as any
    star, with ``light_fraction`` of an off-fiber star's electrons
    (``throughput`` already includes the beamsplitter when the whole guide
    field is behind it, so 1 there);
* **offset** -- the guider centroids a guide star of magnitude ``star_g``
  at ``separation`` arcsec, plus the systematic error of the offset itself
  (plate scale and PA errors times the separation, the residual of the
  differential-refraction correction, flexure between the guide camera and
  the fiber), added in quadrature.

Per frame, signal ``S`` electrons over ``n`` pixels (a disk of radius
1.5 FWHM) with sky, dark and read noise give ``SNR = S / sqrt(S + n (sky +
dark + rn^2))``, and a centroid error per axis of ``k * sigma / SNR``
(with the centroiders' floor, ``centroid_floor`` pixels, in quadrature):
``sigma`` is the star's rms width (for the annulus, of the ring: ``sqrt(s^2
+ r^2)``) and ``k`` a factor per method (``com_factor`` for a whole star,
``annulus_factor`` for the ring) calibrated against the guider's own centroiders on
simulated frames (tests/core/test_guide_recommend.py). A guide loop with
integrator gain ``kp`` turns that into a tracking error of ``sqrt(2 kp /
(2 - kp))`` times it (rms, both axes). Below ``min_snr`` the centroider
does not find the star at all.

**Cadence.** Each method's achievable correction rate is ``1 / t``, ``t``
the shortest exposure (at most ``exposure``) at which the star is found
and the error is within ``max_error``. A method whose rate is below
``min_rate`` (Hz; 0: no requirement) does not work: a faint target guided
on only every minute or so cannot follow the image motion that matters.

The choice: on the target if it works (found, error within ``max_error``,
rate at least ``min_rate``) and is no worse than ``prefer_annulus`` times
the offset's error (it needs no second star and has no systematic error);
otherwise offset if it works; otherwise whichever is better, flagged. So
offset guiding is chosen only when the target is too faint to see at the
light the guide camera gets of it, or too faint to guide on often enough.

``python -m robofast_core.guider.recommend --config <dispatch or model
JSON> [--instrument tres] --mag 13.5 [--sky 21] [--seeing 1.5]`` prints
the prediction, or a table over ``--grid 8:16:0.5``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

__all__ = [
    "GuideModel",
    "Prediction",
    "Recommendation",
    "predict",
    "recommend",
    "sky_brightness",
]

METHODS = ("annulus", "offset")
#: How the guide camera sees the target on the fiber (module doc).
ON_TARGET = ("annulus", "beamsplitter")


@dataclass
class GuideModel:
    """One instrument's guiding (a dispatch config's ``instruments.<name>.
    guiding``). Every value describes hardware and is a site value: no
    defaults for those; the thresholds have defaults."""

    #: Telescope aperture (m) and the fraction of it the secondary blocks.
    aperture: float
    #: Fiber (or slit width) on the sky, arcsec diameter.
    fiber_diameter: float
    #: Guide camera: arcsec per pixel, read noise (e-), dark (e-/s/px).
    plate_scale: float
    read_noise: float
    #: Fraction of a star's photons that reach the guide camera as
    #: electrons (optics, beam splitter, QE) when it is off the fiber.
    throughput: float
    #: Guide exposure (s) for a faint target: the longest the guider uses.
    exposure: float
    #: How the guide camera sees the target on the fiber: ``annulus`` (a
    #: mirror round the fiber: the spill) or ``beamsplitter`` (the whole
    #: star, with ``light_fraction`` of an off-fiber star's electrons).
    on_target: str = "annulus"
    light_fraction: float = 1.0
    #: The slowest correction rate (Hz) a method may need (module doc,
    #: "Cadence"); 0: no requirement.
    min_rate: float = 0.0
    obscuration: float = 0.0
    dark: float = 0.0
    #: Of the spill round the fiber, the fraction the guide optics deliver
    #: relative to an off-fiber star (a reflective fiber face: ~1).
    spill_efficiency: float = 1.0
    #: Photons per second per m^2 at magnitude 0 in the guide camera's band
    #: (unfiltered silicon: ~3e10).
    zero_point: float = 3e10
    #: Defaults for the conditions when none are given: the dark sky and
    #: the sky under a full Moon well up (mag/arcsec^2, guide band).
    sky: float = 21.0
    sky_full_moon: float = 18.0
    seeing: float = 1.5
    #: The guide loop's integrator gain (noise propagation).
    kp: float = 0.5
    #: Centroid error factors, calibrated against the guider's centroiders
    #: on simulated frames (module doc): the windowed centre of mass
    #: follows ``sigma / SNR``; the annulus fit (seeing width known) beats
    #: the ring's ``sqrt(s^2 + r^2) / SNR`` by about 3x.
    com_factor: float = 1.0
    annulus_factor: float = 0.3
    #: The centroiders' floor on a bright star (pixels per axis; the
    #: windowed centre of mass's ~0.005 px), added in quadrature.
    centroid_floor: float = 0.005
    #: Offset systematics: fractional plate-scale error, PA error (deg),
    #: differential-refraction residual and flexure (arcsec).
    scale_error: float = 0.002
    pa_error: float = 0.1
    refraction_residual: float = 0.02
    flexure: float = 0.05
    #: When the guide star is not known yet ("auto"): its assumed G and
    #: separation (arcsec).
    star_g: float = 11.0
    separation: float = 60.0
    #: Thresholds: the tolerated tracking error (arcsec), the SNR below
    #: which a centroider finds nothing, and how much better offset
    #: guiding must be to be preferred.
    max_error: float = 0.3
    min_snr: float = 5.0
    prefer_annulus: float = 1.5

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GuideModel:
        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        names = {f.name for f in fields(cls)}
        if set(d) - names:
            raise ValueError(f"guiding model: unknown keys {sorted(set(d) - names)}")
        on_target = d.pop("on_target", "annulus")
        if on_target not in ON_TARGET:
            raise ValueError(f"guiding model: on_target must be one of {', '.join(ON_TARGET)}")
        try:
            m = cls(on_target=on_target, **{k: float(v) for k, v in d.items()})
        except TypeError as exc:
            raise ValueError(f"guiding model: {exc}") from None
        if not 0 < m.light_fraction <= 1:
            raise ValueError("guiding model: light_fraction must be in (0, 1]")
        if m.min_rate < 0:
            raise ValueError("guiding model: min_rate must be >= 0")
        for k in ("aperture", "fiber_diameter", "plate_scale", "throughput", "exposure"):
            if not getattr(m, k) > 0:
                raise ValueError(f"guiding model: {k} must be > 0")
        if not 0 < m.kp < 2:
            raise ValueError("guiding model: kp must be in (0, 2)")
        return m

    @property
    def area(self) -> float:
        return math.pi * (self.aperture / 2) ** 2 * (1.0 - self.obscuration)

    def electrons(self, mag: float, seconds: float | None = None) -> float:
        """Electrons on the guide camera from a star of ``mag`` (off the fiber)."""
        t = self.exposure if seconds is None else seconds
        return self.zero_point * 10 ** (-0.4 * mag) * self.area * self.throughput * t


@dataclass
class Prediction:
    """One method: per-frame SNR, centroid error per axis and tracking
    error (rms, arcsec), the systematic part (offset), and whether the
    centroider would find the star at all."""

    method: str
    snr: float
    centroid: float
    tracking: float
    systematic: float = 0.0
    found: bool = True
    #: The achievable correction rate (Hz; 0: none at any allowed
    #: exposure; module doc, "Cadence").
    rate: float = 0.0

    @property
    def error(self) -> float:
        """Total error (arcsec rms): tracking and systematic in quadrature;
        infinite if the star is not found."""
        if not self.found:
            return math.inf
        return math.hypot(self.tracking, self.systematic)

    def ok(self, model: GuideModel) -> bool:
        """Found, within ``max_error`` and fast enough (``min_rate``)."""
        return self.found and self.error <= model.max_error and self.rate >= model.min_rate

    def as_json(self) -> dict[str, Any]:
        d = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}
        d["error"] = None if not math.isfinite(self.error) else round(self.error, 4)
        return d


def _snr_and_sigma(
    model: GuideModel,
    signal: float,
    width: float,
    seeing: float,
    sky: float,
    exposure: float | None = None,
) -> tuple[float, float]:
    """(SNR, centroid error per axis in arcsec) of ``signal`` electrons
    spread with rms ``width`` arcsec, in an ``exposure`` (default the
    model's)."""
    t = model.exposure if exposure is None else exposure
    ps = model.plate_scale
    npix = math.pi * (1.5 * seeing + width) ** 2 / ps**2
    sky_e = model.zero_point * 10 ** (-0.4 * sky) * model.area * model.throughput * ps**2
    noise2 = signal + npix * ((sky_e + model.dark) * t + model.read_noise**2)
    snr = signal / math.sqrt(noise2) if noise2 > 0 else 0.0
    sigma = width / snr if snr > 0 else math.inf
    return snr, sigma


def sky_brightness(model: GuideModel, illumination: float, moon_alt: float) -> float:
    """The sky (mag/arcsec^2): the dark sky plus the Moon's light, scaled by
    its illuminated fraction while it is up (a rough model; no
    separation or airmass terms)."""
    if moon_alt <= 0 or not math.isfinite(illumination) or illumination <= 0:
        return model.sky
    dark = 10 ** (-0.4 * model.sky)
    moon = 10 ** (-0.4 * model.sky_full_moon) * min(illumination, 1.0)
    return -2.5 * math.log10(dark + moon)


def predict(
    model: GuideModel,
    mag: float,
    *,
    sky: float | None = None,
    seeing: float | None = None,
    star_g: float | None = None,
    separation: float | None = None,
) -> dict[str, Prediction]:
    """Both methods' predictions (module doc)."""
    sky = model.sky if sky is None else sky
    seeing = model.seeing if seeing is None else seeing
    star_g = model.star_g if star_g is None else star_g
    sep = model.separation if separation is None else separation
    gain = math.sqrt(2.0 * model.kp / (2.0 - model.kp))
    s = seeing / (2.0 * math.sqrt(2.0 * math.log(2.0)))
    floor = model.centroid_floor * model.plate_scale

    def on_target(t: float) -> tuple[float, float]:
        if model.on_target == "beamsplitter":
            signal = model.electrons(mag, t) * model.light_fraction
            snr, sig = _snr_and_sigma(model, signal, s, seeing, sky, t)
            return snr, math.hypot(sig * model.com_factor, floor)
        r = model.fiber_diameter / 2.0
        spill = math.exp(-(r**2) / (2 * s**2)) * model.spill_efficiency
        snr, sig = _snr_and_sigma(
            model, model.electrons(mag, t) * spill, math.hypot(s, r), seeing, sky, t
        )
        return snr, math.hypot(sig * model.annulus_factor, floor)

    def offset_star(t: float) -> tuple[float, float]:
        snr, sig = _snr_and_sigma(model, model.electrons(star_g, t), s, seeing, sky, t)
        return snr, math.hypot(sig * model.com_factor, floor)

    sys_err = math.sqrt(
        (sep * model.scale_error) ** 2
        + (sep * math.radians(model.pa_error)) ** 2
        + model.refraction_residual**2
        + model.flexure**2
    )
    out = {}
    for name, fn, sys_e in (("annulus", on_target, 0.0), ("offset", offset_star, sys_err)):
        snr, sig = fn(model.exposure)
        out[name] = Prediction(
            name,
            snr,
            sig,
            gain * sig,
            systematic=sys_e,
            found=snr >= model.min_snr,
            rate=_rate(model, fn, gain, sys_e),
        )
    return out


def _rate(model: GuideModel, fn: Any, gain: float, systematic: float) -> float:
    """The achievable correction rate (module doc, "Cadence"): ``1 / t`` for
    the shortest exposure ``t`` (at most the model's) at which the star is
    found and the error is within ``max_error``; 0 if none is."""

    def works(t: float) -> bool:
        snr, sig = fn(t)
        return snr >= model.min_snr and math.hypot(gain * sig, systematic) <= model.max_error

    hi = model.exposure
    if not works(hi):
        return 0.0
    lo = hi * 1e-4
    if works(lo):
        return 1.0 / lo
    for _ in range(40):  # SNR grows with t: bisect on the log
        mid = math.sqrt(lo * hi)
        lo, hi = (lo, mid) if works(mid) else (mid, hi)
    return 1.0 / hi


@dataclass
class Recommendation:
    method: str
    reason: str
    annulus: Prediction
    offset: Prediction
    conditions: dict[str, float]
    #: The instrument's ``on_target`` geometry (module doc).
    on_target: str = "annulus"

    def as_json(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "on_target": self.on_target,
            "reason": self.reason,
            "annulus": self.annulus.as_json(),
            "offset": self.offset.as_json(),
            "conditions": self.conditions,
        }


def recommend(
    model: GuideModel,
    mag: float,
    *,
    sky: float | None = None,
    seeing: float | None = None,
    star_g: float | None = None,
    separation: float | None = None,
) -> Recommendation:
    """Annulus or offset guiding for a target of ``mag`` (module doc)."""
    p = predict(model, mag, sky=sky, seeing=seeing, star_g=star_g, separation=separation)
    a, o = p["annulus"], p["offset"]
    cond = {
        "mag": mag,
        "sky": model.sky if sky is None else sky,
        "seeing": model.seeing if seeing is None else seeing,
        "star_g": model.star_g if star_g is None else star_g,
        "separation": model.separation if separation is None else separation,
    }
    lim = model.max_error
    a_ok, o_ok = a.ok(model), o.ok(model)
    # What the guider sees of the target: the spill round the fiber, or the
    # whole star through the beamsplitter.
    seen = "spill round the fiber" if model.on_target == "annulus" else "target (beamsplitter)"
    if a_ok and (not o_ok or a.error <= model.prefer_annulus * o.error):
        method = "annulus"
        why = (
            f'on target: {seen} error {a.error:.2f}" (SNR {a.snr:.0f}, up to'
            f' {_hz(a.rate)}) within {lim:g}"'
        )
        if o_ok:
            why += f'; offset {o.error:.2f}" is not {model.prefer_annulus:g}x better'
    elif o_ok:
        method = "offset"
        if not a.found:
            why = (
                f"the {seen} is too faint to centroid in {model.exposure:g} s"
                f" (SNR {a.snr:.1f} < {model.min_snr:g})"
            )
        elif a.error > lim:
            why = f'on-target error {a.error:.2f}" exceeds {lim:g}"'
        elif a.rate < model.min_rate:
            why = (
                f"the {seen} is too faint to guide on often enough"
                f" ({_hz(a.rate)} < min_rate {model.min_rate:g} Hz)"
            )
        else:
            x = model.prefer_annulus
            why = f'offset {o.error:.2f}" is over {x:g}x better than on target {a.error:.2f}"'
        why += f'; offset star G={cond["star_g"]:g} at {cond["separation"]:g}": {o.error:.2f}"'
    else:
        method = "annulus" if a.error <= o.error else "offset"
        why = (
            f'neither works (on target {_fmt(a.error)}" at {_hz(a.rate)}, offset'
            f' {_fmt(o.error)}" at {_hz(o.rate)}; limits {lim:g}", {model.min_rate:g} Hz):'
            f" {'on target' if method == 'annulus' else 'offset'} is the lesser evil"
        )
    return Recommendation(method, why, a, o, cond, model.on_target)


def _hz(rate: float) -> str:
    return "never" if rate <= 0 else f"{rate:.2g} Hz"


def _fmt(x: float) -> str:
    return "inf" if not math.isfinite(x) else f"{x:.2f}"


# -- the command line --------------------------------------------------------------------


def load_model(path: str | Path, instrument: str | None = None) -> GuideModel:
    """A :class:`GuideModel` from a JSON file: the model itself, or a
    dispatch config whose ``instruments.<instrument>.guiding`` holds it."""
    d = json.loads(Path(path).read_text())
    if "instruments" in d:
        inst = d["instruments"]
        names = [n for n, v in inst.items() if isinstance(v, dict) and "guiding" in v]
        if instrument is None:
            if len(names) != 1:
                raise ValueError(f"--instrument: one of {names}")
            instrument = names[0]
        if instrument not in names:
            raise ValueError(f"no guiding model for instrument {instrument!r} (have {names})")
        d = inst[instrument]["guiding"]
    return GuideModel.from_dict(d)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m robofast_core.guider.recommend", description=__doc__.split("\n")[0]
    )
    ap.add_argument("--config", required=True, help="a guiding model, or a dispatch config")
    ap.add_argument("--instrument", help="the instrument in a dispatch config")
    ap.add_argument("--mag", type=float, help="the target's magnitude (guide camera band)")
    ap.add_argument("--grid", help="a magnitude range lo:hi[:step] instead of --mag")
    ap.add_argument("--sky", type=float, help="sky brightness, mag/arcsec^2")
    ap.add_argument("--seeing", type=float, help="seeing FWHM, arcsec")
    ap.add_argument("--star-g", type=float, help="the offset star's G")
    ap.add_argument("--separation", type=float, help="the offset star's separation, arcsec")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        model = load_model(args.config, args.instrument)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.grid:
        parts = [float(x) for x in args.grid.split(":")]
        lo, hi, step = [*parts, 0.5][:3] if len(parts) >= 2 else (parts[0], parts[0], 1.0)
        mags = [lo + i * step for i in range(round((hi - lo) / step) + 1)]
    elif args.mag is not None:
        mags = [args.mag]
    else:
        print("error: --mag or --grid", file=sys.stderr)
        return 2
    kw = dict(sky=args.sky, seeing=args.seeing, star_g=args.star_g, separation=args.separation)
    recs = [recommend(model, m, **kw) for m in mags]
    if args.json:
        print(json.dumps([r.as_json() for r in recs], indent=1))
        return 0
    tgt = "target" if model.on_target == "beamsplitter" else "annulus"
    print(f"on target: {model.on_target}; exposure up to {model.exposure:g} s")
    print(f"{'mag':>5} {tgt:>9} {'SNR':>6} {'rate':>8} {'offset':>8} {'SNR':>6}  choice   reason")
    for r in recs:
        choice = tgt if r.method == "annulus" else r.method
        print(
            f'{r.conditions["mag"]:5.1f} {_fmt(r.annulus.error):>8}" {r.annulus.snr:6.1f}'
            f" {_hz(r.annulus.rate):>8}"
            f' {_fmt(r.offset.error):>7}" {r.offset.snr:6.0f}  {choice:8s} {r.reason}'
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
