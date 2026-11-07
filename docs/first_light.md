# First light: one command

From a telescope and camera plugged into this computer to a plate-solved
light frame (`robofast/first_light.py`):

```sh
pip install robofast
python -m robofast --site 42.38,-71.13 --no-enclosure
```

(`robofast --site ...` is the same command.) It:

1. finds the telescope and camera by Alpaca discovery (docs/discover.md;
   `--address 127.0.0.1` for devices served on this machine, `--server
   HOST:PORT` for one discovery does not reach);
2. reads the plate scale from them (the telescope's `FocalLength`, the
   camera's `PixelSizeX`/`PixelSizeY`);
3. writes the enclosure config itself, `first-light/observatory.json`
   (`--dir`; it never overwrites one);
4. asks you, with no enclosure, whether the sky is clear and whether you
   will stay with the telescope (`--yes` answers yes): you are then the
   operator on duty (docs/no_enclosure.md) until it finishes;
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

## What you say, and why

robofast reads what the devices can report and asks for what they cannot,
or what it must never guess:

| | |
|---|---|
| `--site LAT,LON[,ELEV]` | deg north, deg east, m. The calibration lays its points in altitude from it; a mount's own site setting is often wrong, so it is not read. |
| `--no-enclosure` | a telescope with no dome or roof: the operator on duty judges the weather. |
| `--dome HOST:PORT/N --safety-monitor HOST:PORT/N` | an enclosure, and the device whose verdict decides whether it may open. robofast lists the Dome and SafetyMonitor it finds but never chooses one. The enclosure opens only after the weather has been safe for the reopen hold (`safety.reopen_hold`, 20 min by default). |

It stops, naming what, when a device cannot report what it needs (a mount
that does not know its focal length, a camera with no pixel size), when
more than one telescope answers, and when the operator does not say yes.

## The catalog

`--catalog` names the catalog source (default `gaia`: Gaia DR3 tiles,
downloaded on first use and cached; docs/sky.md, "What is downloaded, from
where"); `--catalog-dir` its cache.

## On the simulated telescope

`robofast.testing.sky_device` is a telescope and camera on a simulated
sky, served on loopback as plugged-in Alpaca devices (with discovery):

```sh
python -m robofast.testing.sky_device --discovery-port 32227 &
python -m robofast --site 42.38,-71.13 --no-enclosure --address 127.0.0.1 \
    --plugin robofast.testing.sky_device:register --catalog synthetic
```

Its sky is `synthetic` (seeded random stars over the whole sky, a catalog
source the plug-in registers; offline) or, with `--catalog gaia` on both,
the real one. Its mount points with an injected TPOINT error
(`--pointing-error`, default `IH=150,ID=-40,NP=20` arcsec), which the
calibration measures. `tests/core/test_first_light.py` runs the command on
it.

On the real sky the simulated camera needs its catalog tiles before it
renders: a tile downloaded inside an exposure can time the camera out.
Hold the sky at one instant with `--time` (its mount's sidereal time and
its stars), so every run points at the same four fields; fill the cache
for them once (`catalog_from_config(...).cone(ra, dec, 0.55)` around each:
the zenith at that instant's sidereal time, and the calibration's three
points, `pointmodel.sky_grid`), and every run after renders from it.
A real camera needs none of this: only the solver reads the catalog, and
it downloads what it lacks as it solves.
