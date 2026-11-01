# robofast data: frames on disk

The sequencer saves every frame it takes when the enclosure config has an
`images` entry (`robofast_core/imaging.py`, `robofast_core/fits_writer.py`;
design.md sections 3.4 and 3.8). This page is the contract for anything that
reads those files: the layout, the times, and the header keywords.

## Configuration

```json
"images": {
  "data_root": "/data",
  "site": "minerva",
  "compress": false,
  "queue_size": 4,
  "imagebytes": true,
  "telescopes": {
    "t1": {"plate_scale": 0.6, "position_angle": 0.0, "flip": false,
           "aperture": 700.0, "focal_length": 4560.0}
  }
}
```

| Field | Default | Meaning |
|---|---|---|
| `data_root` | required | top of the tree; relative to the config file if relative |
| `site` | `site` | a directory level and `OBSERVAT` |
| `compress` | false | Rice-compress integer images (`.fits.fz`); float images are never quantized, so they stay uncompressed |
| `queue_size` | 4 | frames per telescope waiting to be written; beyond that the next exposure waits |
| `imagebytes` | true | download images by the Alpaca ImageBytes transfer (else JSON) |
| `download_timeout_base`, `download_min_rate` | 30 s, 2 MB/s | download timeout = base + (pixels x 8 bytes for ImageBytes, x 12 for JSON) / rate |
| `night_rollover_hour`, `timezone` | 12, none | the night convention (below) |
| `pointing_max_age`, `camera_max_age` | 60 s, 60 s | how long telescope and camera readings stay current |
| `weather_interval`, `weather_max_age` | 30 s, 300 s | weather polling, and when a weather value goes stale |
| `enclosure_interval`, `sky_interval` | 10 s, 60 s | shutter/safety and Sun/Moon refresh |
| `telemetry_timeout` | 15 s | bound on one exposure's batch of telemetry reads |
| `flush_timeout` | 120 s | bound on waiting for a telescope's frames to be written |
| `index` | true | write the nightly index |
| `telescopes.<name>` | none | optics: `plate_scale` (arcsec per unbinned pixel; enables the approximate WCS), `position_angle` (deg, image +y east of north), `flip` (east to the right), `aperture`, `focal_length` (mm) |

A telescope entry may also name a `focuser` and a `rotator` (Alpaca
devices). They are only read, for the headers.

## Layout

```
<data_root>/<site>/<telescope>/n<YYYYMMDD>/n<YYYYMMDD>.<telescope>.<object>.<seq:04d>.fits
<data_root>/<site>/<telescope>/n<YYYYMMDD>/n<YYYYMMDD>.<telescope>.frames.jsonl
```

* **Night**: the local date of the evening. A frame taken before
  `night_rollover_hour` (12:00) local time belongs to the previous date's
  night. Local time comes from `timezone` (an IANA name) if it is set, otherwise
  from mean solar time at the site's longitude. This is the convention the
  plan record uses (`robofast_core.plan_record.night_of`). The night is
  computed from the frame's own `DATE-OBS`.
* **Object**: the observation's `object`. That is the target name from the
  dispatch scheduler, or the observation name for static and calibration
  plans. Characters other than letters, digits, `+`, `-` and `_` become `_`,
  so dots only separate fields.
* **Sequence number**: counts up per telescope and night across all objects,
  starting at one more than the highest number already in the directory.
  The legacy system numbered frames the same way.
* **The index**: one JSON line per frame, appended (and fsynced) after the
  frame is in place. Each line has `file` (relative to `data_root`),
  `telescope`, `object`, `imagetyp`, `date_obs`, `exptime`, `filter`,
  `observation`, `frame`, `seq`, the request ids, `shape`, `bytes` and
  `written`.

**Writing never overwrites and is atomic.** A frame is written under a
temporary name in its directory, fsynced, then hard-linked to its final
name. The link fails if that name already exists; the writer then takes the
next number, rewrites the header and tries again. Finally the temporary
name is removed and the directory is fsynced. A reader never sees a partial
file, and no file is ever modified after it is written. (The legacy
`fix_fits.py` rewrote raw files in place. Nothing here does that.)

**A frame is never dropped silently.** If a write fails, the sequencer
raises a critical `frame_write_failed` alarm that describes the frame, and
that telescope stops for the night (exit status 3: needs an operator). The
other telescopes keep observing. The sequencer's status snapshot shows
`frames_failed` for that telescope.

**Data pointers.** An observation is reported `complete` only after all of
its frames are on disk. The report carries their paths relative to
`data_root`, so the dispatch scheduler's records and the request API's
`GET /requestgroups/{id}/data` give the real file names.

## Times

All times are UTC. `DATE-OBS` is the camera's `LastExposureStartTime`,
which ASCOM defines as UTC. It is written with milliseconds: `CCYY-MM-DDThh:mm:ss.sss`.
`EXPTIME` is the camera's `LastExposureDuration`. Nothing is ever computed
as local time plus an offset. If the camera does not report a start time,
`DATE-OBS` is the host's UTC time just before `StartExposure` and `TIMESRC`
is `host`. `DATE-CMD` (the host's time when the command was sent) and
`CLKSKEW` (`DATE-OBS - DATE-CMD`) are always written, so a camera clock
that drifts is visible. Some cameras report whole seconds (OmniSim does),
which makes `CLKSKEW` negative by up to a second.

Note: astropy's `CHECKSUM`/`DATASUM` *comments* carry its own timestamp in
the host's local time. These are comments and do not affect any keyword
value.

## Stale values

Header values come from the value registry (design.md 3.8). Each value
carries the time it was measured and a maximum age. A snapshot is taken at
exposure start (telescope, focuser, rotator, filter, weather, enclosure,
Sun and Moon) and another at exposure end (camera). A value that is older
than its maximum age at its snapshot, or whose last read failed, is
**stale**. It is written with an undefined value, and its comment says
`STALE (<age>s old)` or `STALE (read failed)` followed by the old value. A
`COMMENT` card lists every stale keyword. A stale value is never written as
current. A device member that the device does not implement is left out.

## Keywords

Units are in square brackets at the start of the comment. "start" and "end"
name the snapshot that a value comes from.

| Keyword | Source | Meaning |
|---|---|---|
| `DATE` | host | UTC time the file was written |
| `DATE-OBS` | camera | UTC start of exposure (ms) |
| `DATE-END` | derived | `DATE-OBS + EXPTIME` |
| `TIMESYS` | | `UTC` |
| `TIMESRC` | | `camera` or `host` (see Times) |
| `MJD-OBS`, `JD` | derived | at `DATE-OBS`, UTC |
| `DATE-CMD` | host | UTC when `StartExposure` was sent |
| `CLKSKEW` | derived | [s] `DATE-OBS - DATE-CMD` |
| `EXPTIME` | camera | [s] `LastExposureDuration` (requested time if absent) |
| `EXPREQ` | plan | [s] requested exposure time |
| `OBJECT` | plan | target name |
| `IMAGETYP` | plan | `LIGHT`, `FLAT` (name starts with flat/skyflat/domeflat), `DARK`, `BIAS` (dark with zero exposure) |
| `TELESCOP` | config | telescope name |
| `ORIGIN` | | `robofast` |
| `FILTER`, `PORT` | plan | filter and M3 port the observation asked for (and the sequencer selected) |
| `FILTNAME`, `FILTPOS` | filter wheel | the selected filter and wheel position |
| `M3PORT` | telescope | the M3 port selected last |
| `APTDIA`, `FOCALLEN` | config | [mm] optics |
| `REQGRP`, `REQUEST`, `CONFIGID` | dispatch | request group, request and configuration ids |
| `OBSNAME` | sequencer | observation name |
| `FRAMENUM` | sequencer | frame number within the observation (0-based) |
| `SEQUENCR`, `GENERATN` | sequencer | lease holder name and generation |
| `OBJRA`, `OBJDEC` | plan | [deg] requested position (ICRS); light frames |
| `RA`, `DEC` | telescope, start | [deg] mount position at exposure start. A J2000 mount is written with `RADESYS = 'FK5'` and `EQUINOX = 2000`. A topocentric mount (`EquatorialSystem = 1`, OmniSim) is converted to `ICRS` with astropy (approximate: no refraction correction) |
| `RADESYS`, `EQUINOX` | derived | frame of `RA`/`DEC` |
| `HA` | derived | [h] `LST - RA` (mount), at start |
| `AIRMASS` | derived | from `ALT` (Kasten and Young 1989) |
| `ALT`, `AZ` | telescope, start | [deg] |
| `LST` | telescope, start | [h] sidereal time from the mount |
| `TRACKING` | telescope, start | mount tracking |
| `FOCPOS`, `FOCTEMP` | focuser, start | [step], [C] |
| `ROTPOS`, `ROTMECH` | rotator, start | [deg] sky and mechanical angle |
| `ADCTRACK`, `ADCSETTL`, `ADCDEFL`, `ADCANGA`, `ADCANGB` | the ADC server (docs/adc.md), start of light frames | tracking, settled on this target, prism deflection [deg], each motor's angle from home [deg] |
| `INSTRUME`, `DETECTOR` | camera | camera `Name`, `SensorName` |
| `PIXSIZE1`, `PIXSIZE2` | camera | [um] unbinned pixel size |
| `EGAIN`, `MAXADU` | camera | [e-/ADU], [ADU] |
| `CCD-TEMP`, `SET-TEMP` | camera, end | [C] |
| `COOLERON`, `COOLPOW` | camera, end | cooler state, [%] power |
| `GAIN`, `OFFSET` | camera, end | gain and offset settings (left out if the camera has none) |
| `XBINNING`, `YBINNING` | camera, end | binning |
| `XORGSUBF`, `YORGSUBF` | camera, end | [px] subframe origin |
| `READMODE` | camera, end | readout mode name |
| `SHUTTER`, `WXSAFE` | safety supervisor | enclosure shutter state, and its weather verdict |
| `AMBTEMP`, `HUMIDITY`, `DEWPOINT`, `PRESSURE`, `WINDSPD`, `WINDGUST`, `WINDDIR`, `SKYTEMP`, `CLOUDCOV`, `RAINRATE` | weather bridge (ObservingConditions) | [C], [%], [C], [hPa], [m/s], [m/s], [deg], [C], [%], [mm/h]; measured at now minus `TimeSinceLastUpdate` |
| `SUNALT`, `MOONALT`, `MOONILLU` | astropy | [deg], [deg], Moon illuminated fraction |
| `MOONSEP` | derived | [deg] Moon to `RA`/`DEC` |
| `OBSERVAT`, `SITELAT`, `SITELONG`, `SITEELEV` | config | site name, [deg N], [deg E], [m] |
| `SWVER` | | `git describe` of the robofast checkout |
| `WCSAXES`, `CTYPE1/2`, `CUNIT1/2`, `CRVAL1/2`, `CRPIX1/2`, `CD1_1..CD2_2`, `PIXSCALE`, `WCSSRC` | derived | **approximate** WCS, written only when `plate_scale` is configured and `RA`/`DEC` are current: TAN projection at the mount's position, the reference pixel at the full chip's centre, the nominal scale times binning, and the configured position angle and parity. `WCSSRC = 'mount'` marks it as unsolved |
| `NIGHT` | derived | `n<YYYYMMDD>` |
| `FILENAME`, `SEQNUM` | writer | file name and sequence number |
| `CHECKSUM`, `DATASUM` | astropy | on every HDU |

Dark and bias frames carry no pointing keywords (`RA`, `DEC`, `ALT`, `AZ`,
`LST`, `AIRMASS`, WCS, focuser and rotator). The mount's state has nothing
to do with them.

## Pixels

FITS order is `NAXIS1` = `NumX` and `NAXIS2` = `NumY`. A colour camera's
planes become `NAXIS3`. Integer images whose values fit are stored as
unsigned 16 bits (`BITPIX = 16`, `BZERO = 32768`); otherwise they are stored
as 32- or 64-bit integers. Floating-point images are stored as sent.

ImageBytes transfer. The client asks with `Accept: application/imagebytes`.
The body has a 44-byte little-endian header: `MetadataVersion` (1),
`ErrorNumber`, `ClientTransactionID`, `ServerTransactionID`, `DataStart`,
`ImageElementType`, `TransmissionElementType`, `Rank`, `Dimension1..3`. The
pixels follow in .NET order, where the last index varies fastest
(`ImageArray[x, y]`). The transmission type may be narrower than the
camera's. OmniSim sends its Int32 images as Byte; the values are the same.
Servers without ImageBytes, and errors (always, from OmniSim 0.4), answer
JSON instead, and the client reads `Value` straight into numpy.

Measured on OmniSim 0.4 (800 x 600 full frame, localhost, median of five):
ImageBytes 0.48 MB in about 5 ms, JSON 1.34 MB in about 65 ms. The
simulator CI job publishes these measurements with its other timings.
