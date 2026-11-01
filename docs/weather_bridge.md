# Site weather bridge

Roadmap step 4 (`docs/design.md` section 8): turn a site's real weather
feeds into the Alpaca SafetyMonitor + ObservingConditions that the safety
supervisor reads.

```
site Redis --RESP--> sources --> evaluator --> WeatherState --Alpaca--> supervisor
site pages --HTTP--> (fetch,     (staleness,    (weather_server.py)
 (SAO ridge,          parse,      limits,
  cloud page)         timestamp)  hysteresis,
                                  rain)
```

> **ACTION: validate rain false-positive rate in shadow mode before enforcing.**
> MINERVA's WXT510 rain sensor was disabled in legacy for false positives
> ("can be triggered by wind"). Rain from every MINERVA source (Redis, the
> ridge page, and the cloud page's MEarth 998 sentinel) is evaluated and
> logged as `would close: rain ...` but does not make `IsSafe` false until
> `enforce_rain: true` is set for that source in
> `sites/minerva/weather.yaml` (see [Rain](#rain)).

| Module | Role |
|---|---|
| `robofast_core/weather_sources.py` | `WeatherSource`, `Reading` (values + the data's own time, per field where the data has it), HTTP fetch with basic auth from a credentials file, the Redis source, the Sun as a source, units |
| `robofast_core/resp.py` | a minimal read-only Redis client (SELECT/MGET only) |
| `robofast_core/sites/minerva.py` | MINERVA adapter: the ridge station page, the cloud-sensor page, the (disabled) MEarth page |
| `robofast_core/weather_eval.py` | limits (static and dynamic), hysteresis, per-source and per-field staleness, rain (enforced or shadow), required/advisory combiner |
| `robofast_core/sun.py` | the Sun's altitude, from astropy (offline-safe) |
| `robofast_core/weather_bridge.py` | config, poll/evaluate/publish loop, command line (shadow mode) |
| `robofast_core/weather_server.py` | the Alpaca server it publishes to |
| `robofast_core/testing/redis_feed.py`, `weather_feed.py` | fake Redis and SAO-page servers for tests |
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
`pip install --group core` (astropy, httpx, PyYAML).

Point the supervisor's `devices.safetymonitor` and
`devices.observingconditions` at the bridge's `listen` address. The
supervisor's `stale_after` (default 60 s) is compared with
`TimeSinceLastUpdate("")`, which is the time since the bridge's last
evaluation (every `evaluate_interval`, 5 s), not the age of the weather
data; data age is already part of `IsSafe`.

### Shadow mode at MINERVA, from any host

The bridge needs Python >= 3.12, which the site gateway (Ubuntu 12.04,
Python 2.7) does not have; run it on any machine that can reach

- `starlord.sao.arizona.edu:6379` (Redis, TCP; db 7),
- `http://linmax.sao.arizona.edu/weather/weather.cur_cond` (ridge page),
- `http://linmax.sao.arizona.edu/temps/sky_temps_now` (cloud page, HTTP
  basic auth).

Check reachability first (`nc -vz starlord.sao.arizona.edu 6379`; `curl -sS`
the ridge page). Then:

```
git clone https://github.com/jdeast/robofast && cd robofast
python3.12 -m venv .venv && . .venv/bin/activate
pip install --upgrade "pip>=25.1" && pip install --group core

# The cloud page's credentials, as the site publishes them to us: username
# on line 1, password on line 2. Never commit this file or put the
# credentials in the config.
install -m 600 /dev/null ~/.config/robofast/sao-weather.credentials   # then edit it

python -m robofast_core.weather_bridge --config sites/minerva/weather.yaml \
    --shadow --json >> weather-shadow.jsonl
```

The config's `credentials_file` is `~/.config/robofast/sao-weather.credentials`;
`~` and `$VARS` are expanded, so a different location can be given with
e.g. `credentials_file: $SAO_WEATHER_CREDENTIALS` in a local copy of the
config. Without the file the cloud page is unknown (advisory: logged, not
unsafe). One JSON line per poll (60 s) holds the verdict, every source's
values and notes, and `would_close`; grep it for `would close: rain` to
measure the rain false-positive rate, and compare `redis.wind` with
`ridge.windSpeed` to settle the wind unit. Run it under `nohup`, tmux or a
systemd user unit; it never writes to Redis or anything at the site.

## Behaviour

- **Staleness per source, every evaluation.** Each reading carries the time
  its data was measured (from the data). Data older than the source's
  `stale_after`, or more than `max_clock_skew` in the future, makes the
  source *unknown*. Evaluations run every `evaluate_interval` on the cached
  readings, so data goes stale between polls too.
- **Staleness per field.** A field with its own timestamp (Redis
  `boltwood_time`, `mbolt_time`) goes stale on its own bound: it becomes
  missing (a cloud sensor then falls back to the next in its group; a
  limited field that is missing makes the source unknown).
- **Fail closed.** A fetch that errors, times out (`timeout` per source) or
  does not parse makes its source unknown at once; the last good values are
  dropped, not kept. An error inside an evaluation publishes unsafe. The
  Alpaca server reports unsafe if the bridge stops publishing for
  `server_stale_after`.
- **Limits** are `[low, high]` per field (inclusive, `null` = open-ended),
  in the field's units, with a stricter `open` pair and a looser `close`
  pair (`close` defaults to `open`; an `open` limit looser than its `close`
  limit is a config error).
- **Dynamic limits** (`dynamic_limits: {field: {enabled, field, side,
  open_offset, close_offset, range}}`) take one side of a field's limits
  from another field of the same reading. Off unless `enabled: true`; a
  value outside `range` or missing falls back to the static limits (noted).
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
- **Groups.** A group (`groups: {sky: [mearth, aurora]}`) is decided by its
  first field with a valid value, as legacy chose its cloud sensor; none
  valid is unknown. The group's value is a derived field (`sky`), servable
  as SkyTemperature.
- **Combiner.** Unsafe if any source (required or advisory) is unsafe, or
  enforced rain is being held; unknown (unsafe) if a required source is
  unknown or every fetched source is unknown; otherwise safe. So an
  *advisory* source that fails, times out or goes stale does not close
  anything (it is listed in the reasons as `(advisory)`), but one that
  answers with a value outside its limits does. A config needs at least one
  required source besides the Sun.
- **Reasons** (which limit tripped, which source is stale, what would close
  if rain were enforced, ...) are in the status JSON: `GET /weather/status`
  on the Alpaca server, and `Action("WeatherStatus")` on the SafetyMonitor.

### Rain

Rain is detected three ways: the *increase* of a cumulative counter within
`window` seconds of data time (`rain_counters: {field: {window,
threshold}}`; a decrease is a counter reset and history restarts there,
and each counter yields `<field>_rate` in units per hour); a rate field
above a threshold (`rain_rates: {field: threshold}`, e.g. the WXT's
`rain_intensity` in mm/h); or a source flagging rain itself (the cloud
page's `rain_values` sentinel). Rain holds for `rain_hold` seconds (MINERVA:
3600, "any rain in the last hour means closed") after the last detection.

Each source has `enforce_rain` (default true). With `enforce_rain: false`
its rain is evaluated and held exactly the same way, but only *reported*:
the reason reads `would close: rain: <why> at <time>; holding until <time>
(rain not enforced)`, it is listed in the status JSON's `would_close`, and
`IsSafe` is unaffected; missing rain data from such a source is a note, not
"unknown". MINERVA: every source has `enforce_rain: false`. Redis and the ridge page
are both the WXT510. The cloud page's MEarth 998.0 sentinel, which legacy
enforced as "much more reliable than wxt510Rain", is shadow only too (JDE,
2026-10-31): it is reported as `would close`, and the roof is not closed on
it.

### Redis sources

`type: redis`, `url: redis://host[:port]/db`. The client
(`robofast_core/resp.py`, about 100 lines of RESP2 over asyncio streams)
sends only `SELECT` and `MGET`, one connection per poll, and refuses any
other command by construction -- the bridge cannot write to the site's
Redis. A dependency (redis-py) would have brought a connection pool, retry
logic and a write API for two read commands; the small reader is easier to
bound (every call is under `timeout`; replies are size-capped) and to fake
in tests.

Options: `fields` (`{field: {key, time?, unit?, to?, missing_values?}}`, the
explicit key -> field mapping; `unit`/`to` convert, e.g. `{unit: km/h, to:
mph}`), `time_key` (the reading's main timestamp key; unreadable = fetch
failed), `timestamps` (`{key: stale_after}` for other timestamp keys a field
names as its `time`), `time_format` (default `%Y-%m-%d %H:%M:%S`) and
`utc_offset_hours`.

## The Sun

`robofast_core/sun.py` uses astropy (`get_body("sun")` -> `AltAz` at the
site's `EarthLocation`), so the bridge's number agrees exactly with the same
calculation made elsewhere with the same parameters (JDE 2026-10-31). The
Sun source reports `altitude` (apparent, refracted) and
`altitude_geometric`.

**Refraction: on, matching legacy.** Legacy `site.sunalt` used an
`ephem.Observer` without setting `pressure`, so PyEphem refracted with its
defaults, 1010 mbar and 15 degC; the MINERVA config passes the same
(`pressure_hpa: 1010`, `temperature_c: 15`; `pressure_hpa: 0` gives the
geometric altitude). Against PyEphem 4.2.1 astropy agrees within 0.002 deg
from +6 deg up (the limit); its refraction model is not meant for the
horizon (0.04 deg apart at +3.3 deg, 0.15 at +1, and below the horizon
astropy keeps adding ~0.17 deg where PyEphem stops refracting).

**Offline.** Importing `robofast_core.sun` sets
`astropy.utils.iers.conf.auto_download = False` and `auto_max_age = None`:
astropy never downloads IERS or leap-second tables (a download could stall
or fail an evaluation) and accepts the bundled predictions however old
(UT1-UTC errors under a second move the Sun under 0.005 deg). A test runs it
with the network blocked, an empty astropy cache and a clock years past the
bundled tables.

## ObservingConditions

`conditions` maps ASCOM members to a source field and the field's unit; the
bridge converts to ASCOM units (degC, %, m/s, hPa, mm/h, deg). A member not
mapped is `NotImplemented`; a mapped member whose source is stale or whose
value is missing answers DriverException (0x500), never an old value.
`TimeSinceLastUpdate(<member>)` is the age of that member's data (its own
timestamp where it has one); `SensorDescription(<member>)` says where it
comes from.

## MINERVA notes

**Sources (JDE 2026-10-31).** The site Redis server is the required
primary: `starlord.sao.arizona.edu:6379`, no auth, **db 7** = current site
weather. db 8 is an older station last written 2024-06-21 and must not be
used (it would read stale, i.e. unknown). The ridge and cloud pages are
advisory backups. The SAO pages may be flaky; a reliability logger now
running at the site will tell; as advisory sources their failures no longer
close the roof (set `required: true` to change that).

**Redis db 7 mapping** (capture 2026-10-31 02:27 MST; its values match the
ridge page at the same moment, so it is the same WXT510 station):

| field | key | unit (as read -> as used) | notes |
|---|---|---|---|
| (time) | `time` | MST, UTC-7, no DST | main timestamp; `mjd` agrees |
| temperature | `temperature` | degC | |
| humidity | `humidity` | % | |
| dewpoint | `dew` | degC | |
| pressure | `pressure` | inHg | 22.69 = ridge `barometer` |
| wind, wind_gust | `wind`, `wind_gust` | **km/h (unverified)** -> mph | see below |
| wind_direction | `wind_direction` | deg | |
| rain_wxt | `rain_current` | mm, cumulative | = ridge `wxt510Rain` |
| rain_total | `rain` | cumulative | = ridge `totalRain` |
| rain_intensity | `rain_intensity` | mm/h | rain rate; served as RainRate |
| hail_intensity | `hail_intensity` | | rain rate (shadow) |
| rain_duration, hail, hail_duration | same | cumulative | logged only |
| mearth | `m_bolt` (time `mbolt_time`) | degC | the MEarth cloud sensor (confirmed, JDE 2026-10-31); limits as the MEarth cloud column |
| aurora | `aurora` | degC | the Aurora sensor (confirmed, JDE 2026-10-31); limits as the Aurora cloud column; `a_sky` equalled it |
| boltwood | `boltwood` (time `boltwood_time`) | degC | read -0.0 (= "sensor down"); which sensor is unknown: logged only |
| sky_limit | `sky_limit` | degC | the FLWO observers' MEarth limit; dynamic limit, off |

**Wind units are not established.** At the capture Redis said wind 11.3,
gust 38.4 while the ridge page said 7.21 and 20.38 mph. 11.3 km/h = 7.02
mph fits the mean wind (gusts depend on the averaging window), and the
MEarth station that shares these WXT keys reported km/h in legacy
`getWeatherMearth`, so the config reads km/h and converts to mph (the
legacy limits' unit). If the server really sends mph, Redis wind reads 1.6x
low; the ridge page's mph limits still close the roof while it answers.
Settle it in shadow mode, then fix `unit:` in the config (the limits stay
in mph).

**sky_limit.** Legacy `domeControl.py` fetched the FLWO 48" observers'
`me_sky_limit` (`getskylimit.sh`, ssh to flwo48) every 30 min, defaulting
to -38, and `oktoopen` applied it to the MEarth sensor only: close limit =
sky_limit, open limit = sky_limit - 2. Redis's `sky_limit` (-35.0 at
capture) is most likely that setting. The config carries this as a dynamic
limit on `mearth`, `enabled: false` (JDE: off by default), with a sanity
range of [-50, -25].

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
  rain (shadow only at MINERVA).

Legacy behaviour not ported: the snow/ice manual-inspection lockout
(`manualDecision.txt`, coldest-temperature tracking) and the per-enclosure
`sunOverride`/`cloudOverride` files. Legacy checked the cumulative rain
totals against 99999-style limits, which never trip; they are rain counters
here, with a new threshold to be tuned in shadow mode.
