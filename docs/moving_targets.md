# Moving targets: comets and asteroids by their orbits

A request's target may be an OCS `ORBITAL_ELEMENTS` target: a comet or a
minor planet given by its heliocentric osculating elements (ecliptic and
equinox J2000), as the Minor Planet Center publishes them and as TOM
Toolkit's `OCSFacility` sends them (`docs/tom_facility.md`). robofast
computes its place from the site at each time, schedules it where it will
be, and follows it while it exposes. The code is `robofast/orbit.py`; the
fields are in `docs/request_language.md` ("Target").

```json
"target": {
  "type": "ORBITAL_ELEMENTS", "name": "P/2009 B1", "scheme": "MPC_COMET",
  "epochofel": 61349.0, "orbinc": 22.2651, "longascnode": 297.0644,
  "argofperih": 129.0801, "eccentricity": 0.634533,
  "perihdist": 2.447830, "epochofperih": 61219.7613
}
```

## Schemes

| `scheme` | fields beyond `epochofel`, `orbinc`, `longascnode`, `argofperih`, `eccentricity` |
|---|---|
| `MPC_MINOR_PLANET` | `meandist` (au), `meananom` (deg, at `epochofel`); `eccentricity` at most 0.9 (as the OCS asks) |
| `MPC_COMET` | `perihdist` (au), `epochofperih` (MJD); any `eccentricity` (> 1: hyperbolic, an interstellar object's) |
| `JPL_MINOR_PLANET` | as `MPC_COMET` |

Epochs are MJD (TT); angles degrees. The major-planet and Astronomical
Almanac schemes (`JPL_MAJOR_PLANET`, `ASA_*`) are refused, with the reason.
A field a scheme does not use is refused, as an unknown key is.

## The place

`Orbit.place(when, location)` is the target's **astrometric** ICRS RA and
Dec from `location` at `when` (UTC): light-time corrected and topocentric,
the same kind of place as a star's catalog coordinates, so a mount that
takes J2000 coordinates points at it the way it points at a star. The
orbit is a two-body (Sun only) conic, propagated with universal variables
(one path for ellipses, parabolas and hyperbolas); the Sun and the Earth
are astropy's built-in ephemeris, so nothing is downloaded.

Checked against JPL Horizons (its own integrator and Earth; quantities 1
and 3, user-supplied elements; for 433 Eros, Horizons' own orbit) at
MINERVA every 30 minutes through the night of 2026-11-09, elements within
five days of the night: P/2009 B1 0.028" at most, 88P/Howell 0.008",
C/2022 N2 (hyperbolic) 0.027", 433 Eros 0.006"; the rates within 0.26"/h
(`tests/core/test_moving_targets.py` keeps three times of each).

The planets are ignored. Within a few weeks of the elements' epoch (the
MPC re-osculates its elements every few months) that is hundredths of an
arcsecond for a comet or a main-belt asteroid; an old epoch, or an object
near a planet (a near-Earth asteroid close to the Earth), can be off by
much more. Send elements near the night.

## Scheduling

The dispatch scheduler puts each moving target at its place from the site
at the middle of the configuration's planned time before its altitude,
hour-angle and Moon checks, and the observation it makes carries that
place (`ra_hours`, `dec_deg`) and the orbit and site
(`Observation.follow`). Over one configuration's minutes, a comet or
main-belt asteroid moves well under an arcminute.

## Following

Only `EXPOSE` configurations, unguided (`guiding_config.mode` `OFF`), take
a moving target: the guider holds a field or a fiber, which would fight
the target's motion, so a guided or fiber configuration is refused.

There is one way to follow: the mount's tracking rates. A telescope whose
mount cannot set them (ASCOM `CanSetRightAscensionRate` or
`CanSetDeclinationRate` false) refuses the observation before it slews
with `Unsupported`, like anything else it cannot do (no guider, no lamp);
it is never followed some other way. That is not an error of the
telescope: the observation is reported `unsupported`, the telescope goes
on at once with its next target (no park, no backoff, no error count), and
the dispatch scheduler never offers that configuration to it again; it
stays eligible on a telescope whose mount has rates, and is refused
(`FAILED`, with the reasons) once none can do it. The sequencer also reads
each mount's `CanSetRightAscensionRate`/`CanSetDeclinationRate` before its
first observation and tells the scheduler, which then never offers a moving
target to a mount without rates at all (robofast.dispatch, "What a
telescope cannot do"). On MINERVA's dry run of 2026-11-09 the Unsupported
was still counted as t1's error: dispatch retried the Eris ToO three times
and a healthy t1 gave up for the night.

The sequencer slews to the target's place (through the pointing model and
the slew guard, as every slew), and before each exposure sets
`RightAscensionRate` (seconds of RA per sidereal second) and
`DeclinationRate` (arcsec per second) to the target's motion at the
exposure's middle, so it stays put on the detector. The rates go back to 0
(sidereal) when the observation ends, in the cleanup if it is cut short,
and, if a reset failed, before any later slew: nothing else is ever
tracked at a comet's rates.

Each step is a `follow` event (`ra`, `dec`, `rate_arcsec_s`) and each
reset a `follow_rates_reset` event.

A plan file's observation may carry `follow` itself (`orbit` as
`Orbit.to_dict()`, `site` with `latitude`, `longitude` and `elevation`),
for an `expose` with light and no guiding.

## Not done

- No guiding on a moving target, and no fiber spectrum of one.
- The simulated cameras (`robofast/testing`) draw the star field where the
  telescope points; they draw no comet or asteroid, so a simulated night
  shows the pointing and the timing, not the object.
- robofast's PWI and TCS servers report `CanSet...Rate` false, so no
  MINERVA or FLWO telescope follows a moving target yet. Each could, in
  its own wrapper and so through the same one path: the PWI server with
  PWI4's rate offsets, the TCS server with the TCS's "chase target" mode
  (`docs/tcsgo.md`, `#12` mode 3).
