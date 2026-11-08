# First light: one command

From a telescope and camera plugged into this computer to a plate-solved
light frame (`robofast/first_light.py`):

```sh
pip install .        # this repository, or a wheel built from it (robofast is not on PyPI yet)
python -m robofast --no-enclosure
```

(`robofast ...` is the same command.) It:

1. finds the telescope and camera by Alpaca discovery (docs/discover.md;
   `--address 127.0.0.1` for devices served on this machine, `--server
   HOST:PORT` for one discovery does not reach);
2. reads the plate scale from them (the telescope's `FocalLength`, the
   camera's `PixelSizeX`/`PixelSizeY`) and the telescope's site ("The
   site", below; `--site` to give another);
3. writes the enclosure config itself, `first-light/observatory.json`
   (`--dir`; it never overwrites one);
4. asks you, with no enclosure, whether the sky is clear and whether you
   will stay with the telescope, and, when the site is the telescope's,
   whether that is where it stands (`--yes` answers yes to all of it): you
   are then the operator on duty (docs/no_enclosure.md) until it finishes;
5. runs robofast's safety supervisor and sequencer on that config, in this
   process: a first pointing calibration from three plate solves
   (docs/pointing_model.md, "A first calibration"), then one light frame
   at the zenith, plate-solved (docs/sky.md);
6. with an enclosure, takes you off duty and waits until the supervisor
   reads it closed (bounded by `safety.shutter_timeout`; if it does not
   close, it exits 1 saying so);
7. prints the frame, where it points, how far that is from the target, and
   the pointing model, and exits 0. Anything that stops it exits 1 and
   says what, with what the supervisor says (weather, the reopen hold, the
   lease) if it was waiting.

The frames and their index are under `first-light/data/` (docs/data.md),
the pointing model under `first-light/pointing/`. The config it wrote is
an ordinary enclosure config: `robofast-supervisor` and
`robofast-sequencer` run it as they run any other.

What each line means and what to do after it: docs/first_light_lines.md.

## What you say, and why

robofast reads what the devices can report and asks for what they cannot,
or what it must never guess:

| | |
|---|---|
| `--no-enclosure` | a telescope with no dome or roof: the operator on duty judges the weather. |
| `--dome HOST:PORT/N --safety-monitor HOST:PORT/N` | an enclosure, and the device whose verdict decides whether it may open. robofast lists the Dome and SafetyMonitor it finds but never chooses one. The enclosure opens only after the weather has been safe for the reopen hold (`safety.reopen_hold`, 20 min by default). |

It stops, naming what, when a device cannot report what it needs (a mount
that does not know its focal length or its site, a camera with no pixel
size or full scale, `MaxADU`, and, with a focuser, a mount that does not
report its aperture, `ApertureDiameter`: give the focal ratio with
`--f-ratio`), when
more than one telescope answers, and when the operator does not say yes.
What ASCOM lets a device leave out does not stop it: a focuser's
`StepSize` (the focus step measures the star's size per step itself) and
a mount's `ApertureArea` (no central obstruction is assumed, and said).
A mount that cannot park gets no lease (the supervisor grants none until
every telescope reports parked): first light gives up after `--timeout`,
saying so in the supervisor's words.

## The site

The site is the telescope's own: its `SiteLatitude` and `SiteLongitude`,
which a GPS mount fills in (its elevation is not read: 0 m unless
`--site LAT,LON,ELEV` gives one);
first light prints it, and asks, with the other question, whether that
is where the telescope stands; a no stops it before anything moves.
**`--yes` answers yes to that too: it confirms the printed site.**
`--site LAT,LON[,ELEV]` (deg north, deg east, m) is used instead when
given, and not asked about. A telescope that cannot report its site, or
reports 0, 0 (a site never set), with no `--site`, stops first light
before anything moves, saying so.

A mount with no GPS reports the site it was given, or its factory
default, which may be another continent; robofast cannot tell that from
the real one (the mount's own limits use the same site, so they do not
catch it), which is why it asks. Measured on the simulated telescope
standing at 41.0 N, 29.0 E with its mount reporting 34.1 N, 118.3 W,
answered yes: two of the three calibration points were commanded at true
altitudes of -22.7 and -16.6 deg, and the "zenith" frame at -9.1 deg,
while first light reported it 4.1 arcmin from the zenith (of the site it
was told).

The calibration lays its points in altitude from the site, and the first
light frame goes to the zenith of the site; the pointing model itself
does not use it. Measured on the simulated telescope (an injected IH of
150", ID -40", NP 20"), the site off by a distance north or east:

| site off by | calibration | IH, ID | frame from its target | frame from the zenith |
|---|---|---|---|---|
| 0 | solved | +159.5", -40.0" | 6.4" | 0.0 deg |
| 100 km | solved | +159.9", -40.0" (north) | 6.5" | 0.9 deg |
| 300 km | solved | +160.8", -40.0" (north) | 6.5" | 2.7 deg |
| 1000 km | solved | +163.6", -40.0" (north) | 7.1" | 9.0 deg |
| 3000 km | solved | +169.1", -40.0" (north) | 12.0" | 27 deg |

(east: IH and ID unchanged, the frame 6.3-6.6" from its target, measured
before the clock check below: since it, a `--site` whose longitude differs
from the telescope's own by more than about 15 arcmin, a minute of
sidereal time, stops first light at the check, as a wrong mount clock does.) A site
off by hundreds of km still calibrates; the "zenith" frame is then about
0.9 deg from the true zenith per 100 km. Not measured: a real mount's
altitude limits (the simulated one has none): with the site 1000 km off,
a point laid at the calibration's lowest altitude (25 deg) is near 16 deg.

A script that runs first light with `--yes` confirms the printed site
unseen: it owns the site it runs at (give it `--site`).

## The telescope's clock

The calibration points where the telescope reports it points, and the
telescope works that out from its own clock: a mount whose date, time,
time zone (daylight saving) or longitude is wrong reports one place and
looks at another, and no frame solves. So before anything moves, first
light compares the telescope's `SiderealTime` with the site's, from this
computer's clock, and stops if they differ by more than a minute, saying
by how many minutes and what to set. Measured on the simulated telescope,
its clock off by: 0 to 2 min, the calibration and frame solved (6.3-6.5"
from the target); 2.5, 3 and 4 min, only some frames solved; 5 min, 47 of
141 solved. The mount's own points stay where it meant them (its clock
error moves its sky and its slews together): a wrong clock does not send
the calibration below the horizon, it only stops the frames solving.

## Focus

A telescope plugged in for the first time can be so far from focus that
its stars are disks larger than the chip, and nothing can be measured:
so first light focuses first (`robofast/first_focus.py`), on the
brightest planet up, else the brightest star (above 30 deg, from the site
and this computer's clock; the Moon is not used yet: on a small chip it
is a disk with no edge in the frame, which needs its own measure).

* **Light, then a donut.** Far from focus a star is the image of the
  telescope's opening, a disk of diameter D = |dz| / N (docs/autofocus.md):
  its light is spread, so the signal is the frame's level over the sky
  beside the target (two frames there first, at the exposure and at a
  quarter of it: many cameras have no shutter for a dark, and the two give
  the camera's bias, which a shorter exposure keeps). The focuser steps the
  way the level rises until the disk is whole on the chip. A frame that
  clips is taken again at a quarter of the exposure, as long as that clips
  fewer pixels (a hot pixel stays clipped at any exposure, and is not the
  star's light). Two frames a known step apart then give the disk's
  size per step, measured (the focuser's units and how far it moves the
  focal plane are not assumed), and where it reaches zero. A planet in
  focus is a small disk, which measures as a donut a few pixels across: so
  from a start already near focus on a planet it still takes the donut's
  two frames (a few frames more, the same focus).
* **Then the autofocus's sweep** (docs/autofocus.md) from there, which
  fits the curve and so does not need that slope to be exact. One frame
  at the sweep's exposure first: if the only stars in it clip (a planet
  alone), the sweep is taken at the approach's last unclipped exposure
  times (seeing / star size)^2, on that one star, or every point of it
  would clip.
* **The camera's exposure limits.** Every focus frame is within the
  camera's `ExposureMin` and `ExposureMax` (where it reports them): the
  bright frames start at 0.01 s or its minimum, and are never shortened
  below it. A target too bright for the shortest exposure stops first
  light, saying so (focus by hand on a fainter star); a camera that
  refuses an exposure stops it at once, in the camera's words.
* **The end of the travel.** If the disk still shrinks at an end of the
  focuser's travel, the focus is beyond it: first light says which end,
  and to add (or remove) a spacer or extension.
* **No focuser.** Without `--yes`, first light walks you through it: a
  frame, a line saying what the star did and how to turn the knob (as a
  multiple of your last turn), until it is in focus; `--yes` answers no
  walk-through (it needs a person), so with `--yes` and no focuser there is
  no focus step.
* **A focuser that moves by steps.** A relative focuser (ASCOM `Absolute`
  false) takes a move as a number of steps, not a position: first light
  does not drive it (not supported yet), says so, and focus is by hand as
  with no focuser.

Measured on the simulated focuser and sky (a 512-pixel chip, f/6.5, a
47 % obstruction, 2" seeing; the tolerance is docs/autofocus.md's, a
quarter of the seeing: 43.9 steps):

| start (steps from focus) | frames | end (steps from focus) |
|---|---|---|
| 5000 to 40000, either side | 3 + 9 | -8.5 to +10.4 |
| 60000 (the disk 1.3 times the chip), the star centred | 12-13 + 9 | +7.8, -8.5 |
| 60000, the star 150 px from the centre | 25-26 + 9 | +7.8 |
| the star 230 px from the centre (26 px from the edge) | - | stops: point at it again |
| the walk-through, a hand exact / 5 % / 15 % off | 7-9 | within 11 / 24 (one 44) / 41 (two 55, 143) |

A disk of an obstructed telescope has a dark centre: one more than about
three chips across (the chip's diagonal over the obstruction) leaves a
chip at its centre unlit, and first light says it sees no light. A
person's hand is the floor of the walk-through: the more exact the turns,
the closer the focus.

## The catalog

`--catalog` names the catalog source (default `gaia`: Gaia DR3 tiles,
downloaded on first use and cached; docs/sky.md, "What is downloaded, from
where"); `--catalog-dir` its cache.

Before the telescope moves, first light downloads the tiles its four
frames' solves will read (the calibration's three points, laid out as the
pointing run lays them from the site and this computer's clock, and the
zenith), and says how many: a tile downloaded inside a solve would hold
the command until it arrived, past `--timeout` and Ctrl-C. `--timeout`
counts from the start (after the duty question), the download included.
Once cached, a run downloads nothing.

The calibration's points are fixed in altitude and azimuth, so their
fields move with the sky (0.25 deg a minute) while the catalog downloads.
First light fetches the fields where the points will be over the
calibration (allowing a minute a point besides its exposure, and the
last download's length), then again for where they are once that is
done, until nothing is missing; the zenith frame is placed after the
download. On a network slower than the sky turns (about six tiles a
minute for the three points: measured on the simulated sky, a download
of 2 s a tile caught up in four passes, one of 10 s a tile did not), it
stops after a pass that gains nothing, at most five, and says how many
tiles will download during the calibration instead.

## On the simulated telescope

`robofast.testing.sky_device` is a telescope and camera on a simulated
sky, served on loopback as plugged-in Alpaca devices (with discovery):

```sh
python -m robofast.testing.sky_device --discovery-port 32227 &
python -m robofast --no-enclosure --address 127.0.0.1 \
    --plugin robofast.testing.sky_device:register --catalog synthetic
```

Its sky is `synthetic` (seeded random stars over the whole sky, a catalog
source the plug-in registers; offline) or, with `--catalog gaia` on both,
the real one. Its mount points with an injected TPOINT error
(`--pointing-error`, default `IH=150,ID=-40,NP=20` arcsec), which the
calibration measures. `tests/core/test_first_light.py` runs the command on
it; `tests/e2e/test_first_light_focus.py` (`-m e2e`) runs it from 30000
focuser steps out of focus (`--focus-error`).

On the real sky the simulated camera needs its catalog tiles before it
renders: a tile downloaded inside an exposure can time the camera out.
Hold the sky at one instant with `--time` (its mount's sidereal time and
its stars), so every run points at the same four fields; fill the cache
for them once (`catalog_from_config(...).cone(ra, dec, 0.55)` around each:
the zenith at that instant's sidereal time, and the calibration's three
points, `pointmodel.sky_grid`), and every run after renders from it.
A real camera needs none of this: only the solver reads the catalog, and
first light downloads its tiles before it starts (above).
