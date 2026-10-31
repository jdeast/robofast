# The observer console

`robofast_core/console/` is one web page for the people watching a night:
the observer on site at the FLWO 60-inch, where robofast runs the night as
*attended automation* (the observer supervises, gets every alarm and can
override), and the PI and collaborators watching MINERVA's robotic nights.
It is a small robofast service like the others: plain asyncio HTTP, the page
and its script inline, no build step, nothing loaded from the internet.

```
python -m robofast_core.console serve --config console.json
```

prints `{"url": ...}` once listening (`deploy/robofast-console.service` is an
example unit). The config (`sites/minerva/console.example.json`):

```
{
  "listen": "127.0.0.1:8400",
  "tokens": "/etc/robofast/api-tokens.json",        # the request API's tokens file
  "audit_log": "/var/log/robofast/console-audit.jsonl",
  "acks": "/var/lib/robofast/console-acks.json",    # alarm acknowledgements
  "enclosures": {
    "aqawan1": {"config": "aqawan1.json",           # the enclosure config
                "plan": "dispatch.example.json",     # if the sequencer runs with --plan
                "spectrograph": "http://127.0.0.1:8130"}
  },
  ...ConsoleConfig fields (rate limits, timeouts, refresh, TLS)
}
```

Paths are relative to the config file. The enclosure config says where
everything else is: `supervisor_url` (or `listen`), `status_file`, each
telescope's `guider`, the weather bridge (the `safetymonitor` device's
address, or `"weather"` in the console's enclosure entry), and
`operator_control` (below). An enclosure whose plan is a dispatch plan with
`state` and `too_dropbox` also gets the request queue and the request forms.

## What it shows

Everything comes from the services' own status APIs and files; the console
reads, and a source that does not answer is shown as unreachable and raised
as an alarm, never a blank or an old value. The page refreshes by
server-sent events (`/console/api/events`, every `refresh` seconds and at
once after any action) and falls back to polling `/console/api/status`.

| Section | What | From |
|---|---|---|
| Alarms | every alarm below, newest state, with **acknowledge** (who, when, why) | all of them |
| Weather | the supervisor's verdict and reason (what it enforces); each weather source's state, age, key values first (temperature, humidity, dew point, wind, sky, cloud, rain, pressure) and reasons; *would close* (shadow rain) and a rain hold | supervisor `GET /status`, weather bridge `GET /weather/status` |
| Enclosure | shutter, fault channel, whether opening is allowed, the lease (holder, live, opened, revoked and why, quarantined), heartbeat age, operator holds, whether the sequencer runs | supervisor `GET /status`, `operator_control/holds.json`, the status snapshot |
| Telescopes | state (observing, idle, waiting, backoff, gave up, finished), exposing, current request and observation, alt/az, focus, guiding (state, rms, flux), last frame, hold | the sequencer's `status_file` (with its value registry), the guider's `GET /status` |
| Spectrograph | busy, job, phase, frame *n* of *m*, exposure progress, target counts, exposure meter, CCD temperature, iodine stage, pressure | KiwiSpec `GET /status` |
| Plan | the night's plan record, newest first (decisions with score and runner-up count, outcomes, idles, preemptions, ToOs); the pending queue (ToOs first, then priority) | `record` directory of the dispatch plan; the scheduler's state file, read as the request API reads it |
| Timeline | the supervisor's recent commands and events | supervisor `GET /timeline` |
| Log | one merged, scrolling, colour-coded stream: the supervisor's and the sequencer's timelines, alarms raised and cleared, weather verdict and sources, shutter, telescope and guider states, the spectrograph's job, holds, and every console action; filter by source and level, search, pause | supervisor `GET /timeline`, the snapshot's `timeline`, the changes between reads (`robofast_core/console/logstream.py`) |
| Sky | an alt/az chart: each telescope's pointing, the targets (current ones highlighted, ToOs marked), the Sun and the Moon, the altitude limits; a target table with altitude, azimuth, airmass now and in 1 and 2 h, and hour angle | the snapshot's `tel.alt`/`tel.az` and `current`; the dispatch scheduler's requests; astropy (`robofast_core/console/skyview.py`) |
| Images | each guider's live frame with the fiber, the centroid and the guide window drawn on it; each telescope's latest saved frame; the spectrograph's latest raw frame (below) | guider `GET /frame`, the frame index, `spectrograph_frames` |
| Requests | submit, validate or cancel a request group or ToO; list one's own groups | the request API, in process |

Alarms raised: the supervisor's own alarms; an enclosure fault; a quarantined
lease, or one revoked but not released; the sequencer quarantined, needing an
operator, or not running (its snapshot is stale); a telescope that gave up or
lost frames; *would close* weather; a guider that failed or alarmed; the
spectrograph's alarms; any service unreachable. An alarm's id is a hash of
its enclosure, source and text, so an acknowledgement sticks to that alarm
(kept in `acks`, across restarts) and a new one is not hidden by an old ack.

The page works on a phone (one column under 360 px), and has three themes,
kept in the browser: dark (the default), **red** (red on black, nothing
else, for a dark control room) and light.

## Log

Each time the console gathers the services' status it adds to one log:
the supervisor's timeline (`GET /timeline`) and the sequencer's (the last
100 entries of its status snapshot, `timeline`), each entry once, with
every command's outcome ("sent" lines left out) and every event; and what
changed since the last read: an alarm raised or cleared (so every guider's,
the spectrograph's, the weather bridge's and the supervisor's alarms), the
weather verdict and each weather source's state, the shutter, each
telescope's and guider's state, the spectrograph's job and phase, the
holds. The console's own actions are added as they happen. Entries carry
an increasing `id`, a time, the enclosure, the source and a level
(`info`, `warning`, `alarm`, `critical`).

The event stream pushes new entries as `event: log` (the last
`log_backlog`, 300, on connecting); `GET /console/api/log?after=<id>`
serves the same for polling. The page lists the newest first, colour-coded
by level, with a checkbox per source (supervisor, sequencer, weather,
enclosure, telescopes, guiders, spectrograph, console), a level filter, a
search box and **pause** (new entries are counted, not drawn, until
resumed). The console keeps `log_size` (3000) entries in memory. Changes
are noticed while someone is watching (the console gathers on demand); the
two timelines are complete either way.

## Sky

`GET /console/api/sky` (`read` scope; recomputed at most every
`sky_cache`, 10 s) gives, per enclosure: the site and the local sidereal
time; the Sun's and Moon's altitude and azimuth (and the Moon's
illuminated fraction); each telescope's pointing (from the sequencer's
value registry; none while stale) and limits (the dispatch plan's
`min_alt`/`max_alt`, else 21 and 83 deg); and every unfinished target of the
dispatch scheduler's requests, the ones a telescope is on first, then ToOs,
then by priority, each with altitude, azimuth and airmass now and 1 and 2 h
later, and its hour angle. Positions are computed with the same code and
bundled IERS tables as the scheduler (never a download). The page draws it
as an SVG chart (zenith at the centre, north up, east to the left as seen
from below; the limits dashed) next to the table, refreshed every 15 s.
Without a dispatch plan the chart shows the telescopes, Sun and Moon only
(the enclosure config's `site`).

## Images

The **Images** section shows, in the browser (no DS9, no plug-in: it works
remotely and on a phone):

* a strip of **guider thumbnails**, one per telescope with a `guider`,
  refreshed at a chosen rate (paused, 0.5, 1, **2** or 4 Hz) while the page
  is visible, with the target (fiber) circle and cross, the last centroid
  (an X) and the guide window drawn over the frame. Tap one to open it in
  the viewer;
* a **viewer** for any source: the guiders, each telescope's latest saved
  frame (`imager-<tel>`: the newest line of its nightly frame index under
  `images.data_root`, docs/data.md) and the spectrograph's latest raw
  frame (`spectrograph`: the newest `*.fits` in the enclosure entry's
  `spectrograph_frames` directory, or its newest subdirectory, wherever
  the spectrograph PC's files land). Scale (zscale, 99.5 %, 99 %,
  min-max), stretch (linear, asinh, log, sqrt) and resolution (256, 512 or
  1024 px), zoom (+/- or the wheel), pan (drag), **click for a pixel
  value** (and its detector position), and **FITS** to download the frame.

The console does the work and the browser gets little: each frame is read
once however many people watch (a guider at most every
`image_min_interval`, 0.4 s; files at most every `image_file_poll`, 2 s),
display limits come from the whole frame (so panning keeps the stretch),
and each view is binned to the requested size, scaled to 8 bits and sent as
a PNG with what to draw over it in an `X-Frame` header. An unchanged frame
costs a `304` and no pixels (ETag). The guider bins on the guide computer
(`guider_frame_max`, 256 px), so a full acquisition frame does not cross
the network at full size.

**Bandwidth.** A guiding thumbnail (the 48-pixel guide window) is about
1.7 kB of PNG plus about 0.6 kB of headers: at 2 Hz, about 5 kB/s
(40 kbit/s) per guider, measured in `tests/core/test_console_images.py`. A
512-pixel view of a sky frame is typically 100 to 250 kB, fetched only when
the frame changes. The page shows the image traffic it is using; pause the
guiders on a slow link. Images have their own rate limit per token
(`image_per_minute`, 900; `image_burst`, 60), apart from the page's.

Endpoints (`read` scope): `GET /console/api/images` (the sources),
`GET /console/api/image/<enclosure>/<source>?max=&scale=&stretch=&x0=&y0=&x1=&y1=`
(PNG), `GET /console/api/pixel/<enclosure>/<source>?x=&y=` and
`GET /console/api/fits/<enclosure>/<source>` (the file itself, or a FITS
copy of a guider frame).

**DS9.** For an observer at a desktop who wants DS9, download the frame
with the **FITS** link (or a script with a `read` token) and open it; DS9
is never needed by the console:

```
curl -s -H "Authorization: Token $TOKEN" -o t1.fits \
  https://console.example/console/api/fits/aqawan1/imager-t1
ds9 t1.fits                      # or, to a running DS9: xpaset -p ds9 fits $PWD/t1.fits
```

(SAMP works the same way: any SAMP client can send DS9 an `image.load.fits`
message with the downloaded file's `file://` URL; the console itself does
not speak SAMP, since the browser cannot reach a desktop's SAMP hub.)

## What an observer can do

Each action is a `POST /console/api/<action>` with a JSON body, needs the
`operator` scope and a non-empty `reason`, and is written to the audit log
(time, address, token name and id, action, enclosure, telescope, reason,
outcome) whether it succeeds or not. The page asks for the reason in a
dialog; the destructive ones (marked *confirm*) must also send
`"confirm": true`.

| Action | Body | What happens |
|---|---|---|
| `ack` | `id` | acknowledge an alarm |
| `pause` | `enclosure`, `telescope` (omit: all) | the telescope finishes the exposure in progress (it is read out and kept), then holds: it stops tracking and stays where it is. **Never touches the roof.** |
| `resume` | same | lift the hold; the telescope asks its plan afresh. If the enclosure is closed, the sequencer asks the supervisor for a lease as always; the supervisor alone decides when to open |
| `stop` (*confirm*) | `enclosure`, `telescope` | one telescope: abort its exposure and slew now and hold it (stopped tracking; it is parked when the enclosure next closes; the roof is not touched) |
| `stop` (*confirm*) | `enclosure` only | the whole enclosure: hold every telescope, then ask the supervisor to close (`POST /close`, which revokes the lease). The sequencer aborts every exposure and parks every telescope as for any revocation, and with no lease the supervisor closes. While held, the sequencer asks for no new lease, so it stays closed until someone resumes |
| `clear_lease` (*confirm*) | `enclosure` | release a dead (crashed or quarantined) lease once the telescopes are safe: the supervisor's `clear`, with the observer's name (design.md 3.3). Refused while the lease is live |
| `clear_faults` (*confirm*) | `enclosure` | the enclosure's `ClearFaults` (the supervisor refuses unless it is closed); also releases an emergency close latch |
| `emergency_close` (*confirm*) | `enclosure` (omit: all), `direct`, `reason` (optional) | close now, by any path that answers (below). Never rate limited |

Requests and ToOs go through the request API's own code
(`/console/api/v1/...` is `/api/v1/...` for the signed-in observer): the same
validation, scopes (`submit`, `too`), ownership (an observer cancels their
own groups; `admin` any) and drop box.

### Pause, resume and stop reach the sequencer through a directory

The console has no channel into the sequencer. An enclosure config with
`"operator_control": "control"` makes the sequencer service wrap its plan in
`OperatorHold` (`robofast_core/console/hold.py`); the console drops a command
file into `control/inbox/`, and the wrapper, polling every second, applies it
through the two hooks every plan has: while a telescope is held its plan
answers `Idle` (so the sequencer starts nothing on it), and the observation in
progress is stopped with the sequencer's own `preempt` (`now-after-readout`
for pause, `now` for stop), reported `interrupted` and re-queued. The wrapper
writes `control/holds.json` (the holds and the commands applied) after every
poll, which the console shows and an enclosure stop waits for (up to
`hold_ack_timeout`) before it asks the supervisor to close. Holds are
reloaded from that file when the sequencer restarts. Without
`operator_control` only an enclosure stop is possible (the lease is revoked;
the sequencer may ask again at once, and the supervisor reopens by its usual
policy).

Holding every telescope of an open enclosure for the sequencer's
`idle_release_after` ends the generation as for any idle period: the
telescopes park, the lease is released and the supervisor closes.

## Emergency close

A red **EMERGENCY CLOSE** button sits at the left of the header on every
page (operators only; it stays in view while scrolling, and on a phone).
One press opens a dialog that says what will happen to each enclosure; one
more (**CLOSE NOW**) does it. A reason is optional. For each enclosure (all
of them, or the one chosen in the dialog), in this order:

1. **Hold every telescope** (`stop`, through `operator_control`), so the
   sequencer asks for no new lease and the supervisor does not simply
   reopen. Not waited for: closing comes first.
2. **Ask the safety supervisor** (`POST /close`; the lease is revoked, the
   sequencer aborts and parks, the supervisor closes; on a roll-off roof
   with `park_before_close` it parks the telescopes first, bounded by
   `park_wait`).
3. **If the supervisor does not answer within `emergency_timeout`**
   (2 s), or answers with an error, close **through the dome's emergency
   channel**: `PUT /robofast/v1/emergency_close` on the dome server, with a
   bearer token (`robofast_core/emergency.py`). The dialog's *skip the
   supervisor* box goes straight to this step (for a supervisor that
   answers but whose close does not happen).

Every emergency close is audited (the console's audit log, and on the dome
server its own `emergency_close.log`) and raised as an alarm: `critical`
when it went to the dome or failed, `alarm` through the supervisor. The
answer says which path closed each enclosure; the action fails (502) if any
enclosure's close was not accepted.

### The dome's close-only channel

The supervisor stays the single owner of the enclosure for opening: the
dome server's emergency endpoint takes no parameters and does one thing,
the enclosure's own `CloseShutter`. It needs a token whose SHA-256 is in
the dome server's config; nothing else on that server changes, and the
console's client (`close_dome`) has no other call. After an emergency close
the enclosure is **latched shut**: the dome reports it on its fault channel
(`Action("Faults")`), so the supervisor, once back, alarms and will not
open, and the dome itself refuses `OpenShutter` until `ClearFaults` (the
console's `clear_faults`, through the supervisor, which the dome accepts
only with both shutters closed). The latch is in the dome's log, so a
restart of the dome server keeps it.

Set it up (Aqawan; the TCSGO server for the 60-inch has no such channel
yet):

```
python -m robofast_core.emergency token         # {"token": ..., "sha256": ...}
```

The token goes in a file only the console's user can read (mode 600; the
console refuses anything looser), the hash in the dome server's config:

```
"emergency_close": {"token_sha256": ["<sha256>"],
                    "log": "/var/log/robofast/aqawan1-emergency.jsonl"}
```

and the console's enclosure entry names the file:

```
"aqawan1": {"config": "aqawan1.json",
            "emergency_close": {"token_file": "/etc/robofast/aqawan1-emergency.token",
                                "fallback": "dome"}}
```

The console must be able to reach the dome server's address
(`devices.dome` in the enclosure config): the same host, or the control
network.

### Emergency close and park-before-close

On a roll-off roof the supervisor parks the telescopes before it closes
(`park_before_close`). **The dome channel does not wait for parking**: if
the supervisor is not answering, nothing is there to park them, and the
roof closes over the telescopes wherever they point. The dialog says so.
Each enclosure chooses with `emergency_close.fallback`:

* `"dome"` (the default when a `token_file` is given): close through the
  dome. This is the default because the channel is used only when the
  supervisor is not answering, and then the enclosure closes without
  parking anyway: the Aqawan PAC's own heartbeat watchdog closes it 60 s
  after the supervisor's pings stop. The emergency channel only brings that
  forward by up to a minute, at an observer's deliberate request. It is also
  design.md 7.1's choice for a park that fails (rain on the optics is worse
  than an unlikely roof contact), and MINERVA's roll-off roofs clear the
  telescopes' park positions in any case (aqawan1's T1 and T2 cannot
  collide).
* `"none"`: report the failure and alarm; do not touch the dome. For an
  enclosure where a closing roof can strike an unparked telescope (until
  its mechanical clearance is confirmed). The observer must then close at
  the enclosure's own panel.

Without an `emergency_close` entry an enclosure has no fallback (`"none"`).
None of this replaces the hardware E-stop or the enclosure's local
controls.

## The console cannot open the roof

Opening happens only one way: the safety supervisor opens when the weather
has been safe for the reopen hold and a live sequencer holds a lease. The
console can do none of that. It reaches the supervisor through
`SupervisorClient`, whose whole list is `GET /status`, `GET /timeline`,
`POST /close`, `POST /faults/clear` and `POST /leases/{token}/clear`; it
refuses anything else before sending it (requesting, renewing or recovering a
lease included). It never sends the lease token to the browser. Its only
other way to an enclosure is the dome's emergency channel, which can only
close (above). A test runs
every action against a real supervisor and sequencer and checks that the
shutter never opened while held, even with the weather safe and the reopen
hold long past, that a `resume` in unsafe weather opened nothing, and that
when it did reopen it was the supervisor's own command, after the reopen
hold, after a lease (`tests/core/test_console.py`).

## Security

The request API's posture (design.md 3.7):

* **Bind.** Loopback by default, behind a TLS reverse proxy on the same
  host for anyone else (the request API's nginx and Caddy examples in
  `deploy/` work with the port changed; the proxy must pass server-sent
  events unbuffered, e.g. nginx `proxy_buffering off`). A non-loopback
  `listen` needs `tls_cert`/`tls_key` here, or `--insecure-bind`.
* **Tokens.** The request API's tokens file and scheme
  (`robofast_core/api_auth.py`): `read` sees the page, `operator` acts,
  `submit`/`too` submit, `admin` is everything. Give each observer a named
  token so the audit log says who:
  `python -m robofast_core.request_api tokens add --config request_api.json --name alice --scope read,operator,submit,too`.
  A revoked or expired token ends its sessions at their next request.
* **No secrets in the page.** The observer pastes their token once; the
  answer sets an `HttpOnly`, `SameSite=Strict` session cookie (`Secure` when
  TLS ends here, or with `cookie_secure: true` behind a proxy) and gives the
  page a CSRF token, kept only in memory. The lease token and the emergency
  token never leave the console process.
* **CSRF.** Every state-changing call needs the cookie and an `X-CSRF-Token`
  header matching the session, a same-origin `Origin` when the browser sends
  one, and a JSON body (a form post is refused). A script may instead send
  `Authorization: Token ...` on each call, with no cookie and so no CSRF.
* **Content-Security-Policy.** `default-src 'none'`; the one inline script
  and style run only with a fresh per-response nonce; no frames, no forms,
  no third-party anything. The page builds its DOM with `textContent`, so no
  string from a device, a weather feed or a request becomes markup.
* **Limits.** Per token: reads and event streams (`rate_per_minute`,
  `max_streams_per_token`), actions (`action_per_minute`); per address:
  failed sign-ins. Bounded request line, headers and body, timeouts and a
  connection limit (the request API's HTTP layer).

## Not done yet

* Alarm delivery off the page is the notifier's job (`docs/notifier.md`,
  built): it reads the same sources and this page's acknowledgements file, so
  an acknowledgement here stops its escalation. Acknowledging from an email
  or a chat message is not done.
* A per-telescope "upcoming" from the scheduler's look-ahead (the queue and
  the sky's target table are shown instead).
* Parking a single stopped telescope at once (it parks when the enclosure
  next closes); that would need a sequencer hook.
* Guider and spectrograph controls (start, stop, abort) -- by design the
  sequencer drives them; an observer stops the telescope instead.
* The spectrograph frame needs the raw files where the console can read
  them (`spectrograph_frames`); the spectrograph PC's own directory is not
  shared yet. Overlays on the science and spectrograph frames (WCS, order
  traces) are not drawn.
