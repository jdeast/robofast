# Site weather bridge

Roadmap step 4 (`docs/design.md` section 8): turn a site's real weather
feeds into the Alpaca SafetyMonitor + ObservingConditions that the safety
supervisor reads.

```
site pages --HTTP--> sources --> evaluator --> WeatherState --Alpaca--> supervisor
 (SAO ridge,         (fetch,     (staleness,    (weather_server.py)
  cloud page)         parse,      limits,
                      timestamp)  hysteresis,
                                  rain)
```

| Module | Role |
|---|---|
| `robofast_core/weather_sources.py` | `WeatherSource`, `Reading` (values + the data's own time), HTTP fetch with basic auth from a credentials file, the Sun as a source, units |
| `robofast_core/sites/minerva.py` | MINERVA adapter: the ridge station page, the cloud-sensor page, the (disabled) MEarth page |
| `robofast_core/weather_eval.py` | limits, hysteresis, per-source staleness, rain, required/advisory combiner |
| `robofast_core/sun.py` | Sun altitude (Astronomical Almanac low-precision formulae; no dependencies) |
| `robofast_core/weather_bridge.py` | config, poll/evaluate/publish loop, command line (shadow mode) |
| `robofast_core/weather_server.py` | the Alpaca server it publishes to |
| `sites/minerva/weather.yaml` | MINERVA's config, each limit commented with its legacy origin |

## Running

```
# Shadow mode: fetch, evaluate, log one line per poll; serve nothing.
python -m robofast_core.weather_bridge --config sites/minerva/weather.yaml --shadow

# Serve Alpaca on the config's `listen` address.
python -m robofast_core.weather_bridge --config sites/minerva/weather.yaml
```

`--once` polls once, prints and exits (0 if safe); `--json` prints each poll
as a JSON object; `--poll-interval` overrides the config. Dependencies:
`pip install --group core` (httpx, PyYAML).

Point the supervisor's `devices.safetymonitor` and
`devices.observingconditions` at the bridge's `listen` address. The
supervisor's `stale_after` (default 60 s) is compared with
`TimeSinceLastUpdate("")`, which is the time since the bridge's last
evaluation (every `evaluate_interval`, 5 s), not the age of the weather
data; data age is already part of `IsSafe`.

## Behaviour

- **Staleness per source, every evaluation.** Each reading carries the time
  its data was measured (from the page). Data older than the source's
  `stale_after`, or more than `max_clock_skew` in the future, makes the
  source *unknown*. Evaluations run every `evaluate_interval` on the cached
  readings, so data goes stale between polls too.
- **Fail closed.** A fetch that errors, times out (`timeout` per source) or
  does not parse makes its source unknown at once; the last good values are
  dropped, not kept. An error inside an evaluation publishes unsafe. The
  Alpaca server reports unsafe if the bridge stops publishing for
  `server_stale_after`.
- **Limits** are `[low, high]` per field (inclusive, `null` = open-ended),
  in the source's own units, with a stricter `open` pair and a looser
  `close` pair (`close` defaults to `open`; an `open` limit looser than its
  `close` limit is a config error).
- **Hysteresis input: the bridge's own previous verdict.** While the bridge
  last said safe, the close limits apply; otherwise (including at startup)
  the open limits do. The supervisor only opens while the bridge says safe,
  so "bridge safe" is a fail-safe stand-in for "enclosure open": reading the
  shutter would add a device dependency and a failure mode for no gain. The
  one difference from legacy: with the enclosure closed for some other
  reason (no lease) while the bridge is safe, legacy would require the open
  limits again before opening; here conditions must have passed the open
  limits when the bridge last became safe, and stayed inside the close
  limits since (plus the supervisor's reopen hold).
- **Groups.** A group (`groups: {sky: [mearth, hat, minerva, aurora]}`) is
  decided by its first field with a valid value, as legacy chose its cloud
  sensor; none valid is unknown. The group's value is a derived field
  (`sky`), servable as SkyTemperature.
- **Rain** comes from the *increase* of a cumulative counter within
  `window` seconds of data time (`rain_counters: {field: {window,
  threshold}}`); a decrease is a counter reset and history restarts there.
  A source can also flag rain itself (the cloud page's `rain_values`
  sentinel). Rain holds the verdict unsafe for `rain_hold` seconds after the
  last detection. Each counter also yields `<field>_rate` (units per hour),
  servable as RainRate.
- **Combiner.** Unsafe if any source (required or advisory) is unsafe, or
  rain is being held; unknown (unsafe) if a required source is unknown or
  every fetched source is unknown; otherwise safe. A config needs at least
  one required source besides the Sun.
- **Reasons** (which limit tripped, which source is stale, ...) are in the
  status JSON: `GET /weather/status` on the Alpaca server, and
  `Action("WeatherStatus")` on the SafetyMonitor.

## ObservingConditions

`conditions` maps ASCOM members to a source field and the field's unit; the
bridge converts to ASCOM units (degC, %, m/s, hPa, mm/h, deg). A member not
mapped is `NotImplemented`; a mapped member whose source is stale or whose
value is missing answers DriverException (0x500), never an old value.
`TimeSinceLastUpdate(<member>)` is the age of that member's data;
`SensorDescription(<member>)` says where it comes from.

## MINERVA notes

The limits in `sites/minerva/weather.yaml` are ported from legacy
`minerva_library/env.py` (MinervaCollaboration/minerva-control). Units the
legacy code does not state were established as follows:

- **Ridge wind: mph** (legacy `plotweather.py` axis labels).
- **Ridge barometer: inHg.** 22.68 inHg = 768 hPa, the standard-atmosphere
  pressure at MINERVA's 2316 m; `plotweather.py` labels it "mbar", which
  cannot be right for that number. Served as Pressure in hPa.
- **Ridge temperatures: C**; humidity %; wxt510Rain mm (`plotweather.py`).
  **totalRain's unit is not established** (mm assumed).
- **Cloud page** times are MST (UTC-7, no DST); the 6/5/4-field layouts
  are legacy's; 0.0 and -999.9 mean "sensor down"; 998.0 from MEarth means
  rain.

Legacy behaviour not ported: the snow/ice manual-inspection lockout
(`manualDecision.txt`, coldest-temperature tracking), the per-enclosure
`sunOverride`/`cloudOverride` files, and the FLWO 48" sky-limit feed
(already disabled in legacy). Legacy checked the cumulative rain totals
against 99999-style limits, which never trip; they are rain counters here,
with a new threshold to be tuned in shadow mode.
