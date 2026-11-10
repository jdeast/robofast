# First light: one command

From a telescope and camera plugged into this computer to a plate-solved
light frame (`robofast/first_light.py`):

```sh
pip install .        # this repository, or a wheel built from it (robofast is not on PyPI yet)
robofast first-light --no-enclosure                   # a telescope on a tripod
robofast first-light --supervisor http://HOST:8100    # under an enclosure's supervisor
```

(`python -m robofast first-light ...` is the same command; `robofast` or
`python -m robofast` alone prints the subcommands and runs nothing.) It:

1. finds the telescope and camera by Alpaca discovery (docs/discover.md;
   `--address 127.0.0.1` for devices served on this machine, `--server
   HOST:PORT` for one discovery does not reach);
2. reads the plate scale from them (the telescope's `FocalLength`, the
   camera's `PixelSizeX`/`PixelSizeY`) and the telescope's site ("The
   site", below; `--site` to give another);
3. writes the enclosure config itself, `first-light/observatory.json`
   (`--dir`; it never overwrites one);
4. checks the Sun ("The Sun", below): in daylight, or with the Sun above
   `--max-sun-alt` (default -12 deg), it stops here;
5. asks you, with no enclosure, whether the sky is clear and whether you
   will stay with the telescope, and, when the site is the telescope's,
   whether that is where it stands (`--yes` answers yes to all of it): you
   are then the operator on duty (docs/no_enclosure.md) until it finishes;
6. runs robofast's sequencer on that config, in this process: a focus
   ("Focus", below), a first pointing calibration from three plate solves
   (docs/pointing_model.md, "A first calibration"), then one light frame
   at the zenith, plate-solved (docs/sky.md). The lease it observes under
   comes from a safety supervisor ("The enclosure", below);
7. prints the frame, where it points, how far that is from the target, and
   the pointing model, and exits 0. Anything that stops it exits 1 and
   says what, with what the supervisor says (weather, the reopen hold, the
   lease) if it was waiting.

The frames and their index are under `first-light/data/` (docs/data.md),
the pointing model under `first-light/pointing/`. The config it wrote is
an ordinary config: with no enclosure, `robofast-no-enclosure-server`
(on 127.0.0.1:11130), `robofast-supervisor` and `robofast-sequencer` run
it as they run any other (docs/no_enclosure.md); under an enclosure,
`robofast-sequencer` runs it against the site's supervisor.

What each line means and what to do after it: docs/first_light_lines.md.

## What you say, and why

robofast reads what the devices can report and asks for what they cannot,
or what it must never guess:

| | |
|---|---|
| `--no-enclosure` | a telescope with no dome or roof: the operator on duty judges the weather. |
| `--supervisor URL` | an enclosure: the site's running supervisor service, which owns it. robofast never chooses a Dome or SafetyMonitor it finds. |

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
saying so in the supervisor's words. A telescope that fails while first light runs
(a slew refused, a device not answering) stops it at once, in the
device's words; first light does not wait out the sequencer's
back-off. Every stop ends with what the mount did, read once the
sequencer's cleanup (which waits for the park) is over: "the telescope
parked", or, only if it had not parked by then, "park it by hand now"
with the time waited. It is one run that stops: running it again takes a
new `--dir` and focuses again.

## The enclosure

First light never runs an enclosure itself; the safety supervisor does
(docs/design.md), in one of two ways.

* **No enclosure** (`--no-enclosure`): first light runs the supervisor
  here, in this process, on the no-enclosure devices (docs/no_enclosure.md:
  the sky as a Dome that opens and closes at once, the operator's duty as
  the SafetyMonitor), served on a free loopback port. That is the
  supervisor's one path, with nothing special for first light: you are on
  duty while it runs (renewed for you), and going off duty, Ctrl-C or the
  end revokes the lease, the sequencer parks, and the supervisor parks the
  telescope again before it "closes" the sky (`park_before_close`, held by
  the park interlock until the mount reports parked).
* **An enclosure** (`--supervisor URL`, a permanent installation): the
  site's own `robofast-supervisor` runs it, and first light starts no
  supervisor: it asks that one for a lease, as `robofast-sequencer` does,
  and the enclosure opens and closes only by its rules (its weather, its
  reopen hold, its modes, its operator duty). First light refuses, before
  anything moves, if no supervisor answers there ("start robofast-supervisor
  on the site's config"), if it has a lease out to another sequencer
  (another program is observing: stop it first), or if it has a critical
  alarm. At the end it releases the lease and says whether the supervisor
  then reads the enclosure closed. The telescope first light drives must
  be one of the site supervisor's telescopes, so that its
  `park_before_close` parks it: first light cannot check that from the
  supervisor's API; it is the site's config.

## The Sun

`robofast/sun_gate.py`. With an enclosure or without:

* **Only in the dark.** First light is refused while the Sun is above
  `--max-sun-alt` (default **-12 deg**: the end of nautical twilight, the
  dispatch scheduler's default too), saying when it next is, and checked
  again once the catalog is downloaded. While it runs, it stops (the
  sequencer parks) when the Sun rises to that altitude. Nautical rather
  than astronomical twilight (-18): the bright stars, planets and plate
  solves first light needs are there by then, and the Sun is far below
  the horizon (the calibration's fields, above 25 deg altitude, are then
  at least 37 deg from it).
* **Nowhere near the Sun.** Nothing it points at (the focus target, the
  calibration's fields over their span, the zenith) may be within
  `--min-sun-separation` (default **30 deg**) of the Sun: a focus target
  that is (Venus, low in a twilight sky, can be) is passed
  over for the next brightest; one named with `--focus-on` is refused;
  a field is refused before anything moves.

Neither rule can be turned off: `--max-sun-alt` is at most 0 (the Sun
below the horizon) and `--min-sun-separation` at least 15 deg. With the
defaults the second rule only matters at a site where the first is
loosened.

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
its stars are disks larger than the chip, and nothing can be measured.
So first light focuses first, with the autofocus's **coarse mode**
(docs/autofocus.md, "Coarse mode": the defocused disk's light, then its
donut, then the fine sweep), as an ordinary `autofocus` observation, on
the brightest planet up, else the brightest star (above 30 deg and at
least `--min-sun-separation` from the Sun, from the site and this
computer's clock; the Moon is not used: on a small chip it is a disk with
no edge in the frame). `--focus-on NAME` chooses it instead, from these:
Venus, Jupiter, Mars, Saturn; Sirius, Canopus, Arcturus, Vega, Capella,
Rigel, Procyon, Betelgeuse, Altair, Aldebaran, Antares, Spica, Pollux,
Fomalhaut, Deneb, Regulus. A name not on the list, one below 30 deg now,
or one too near the Sun is refused before anything moves.

The telescope's `autofocus` entry is written from what the devices report:
the focal ratio from `FocalLength` and `ApertureDiameter` (or
`--f-ratio`), the obstruction from `ApertureArea`, the pixel size from the
plate scale, the camera's full scale (`MaxADU`), the focuser's `StepSize`;
focus frames stay within the camera's `ExposureMin` and `ExposureMax`.
The same coarse mode is every autofocus's fallback when its fine mode
fails, at night too (docs/autofocus.md).

* **A target too bright** for the camera's shortest exposure stops first
  light, saying so and what to run next: `--focus-on` with the brightest
  listed star up now that is fainter by the magnitudes the exposure is
  short (2.5 log10 of the camera's shortest over what focus needs).
* **The end of the travel.** If the disk still shrinks at an end of the
  focuser's travel, the focus is beyond it: it says which end, and to add
  (or remove) a spacer or extension.
* **No focuser.** There is no focus step: focus by hand first (on a bright
  star, until it is smallest), then run first light.
* **A focuser that moves by steps.** A relative focuser (ASCOM `Absolute`
  false) takes a move as a number of steps, not a position: first light
  does not drive it, says so, and focus is by hand as with no focuser.
* **A failed focus** (no light, the focus beyond the travel, a device
  error) stops first light in its words, and the telescope parks.

Measured on the simulated focuser and sky (a 512-pixel chip, f/6.5, a
47 % obstruction, 2" seeing; the tolerance is docs/autofocus.md's, a
quarter of the seeing: 43.9 steps), by Guven Gurkan's first version:

| start (steps from focus) | frames | end (steps from focus) |
|---|---|---|
| 5000 to 40000, either side | 3 + 9 | -8.5 to +10.4 |
| 60000 (the disk 1.3 times the chip), the star centred | 12-13 + 9 | +7.8, -8.5 |
| 60000, the star 150 px from the centre | 25-26 + 9 | +7.8 |
| the star 230 px from the centre (26 px from the edge) | - | stops: point at it again |

A disk of an obstructed telescope has a dark centre: one more than about
three chips across (the chip's diagonal over the obstruction) leaves a
chip at its centre unlit, and no light is seen.

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
robofast first-light --no-enclosure --address 127.0.0.1 \
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
