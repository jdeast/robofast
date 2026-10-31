# The alarm notifier

`robofast_core/notifier/` sends the observer console's alarms off the page:
by email, to a chat channel (a Slack-style webhook), or by running a command
(to page or phone the observer on duty at the 60-inch, for example). It is
its own small process:

```
python -m robofast_core.notifier --config notifier.json           # run
python -m robofast_core.notifier --config notifier.json --check   # validate the config and secrets
python -m robofast_core.notifier --config notifier.json --test    # one test message to every sink
```

`deploy/robofast-notifier.service` is an example unit and
`deploy/notifier.example.json` an example config (`docs/deploy.md` covers
installing both).

## Design: poll what the console reads

The notifier does not invent alarms and the services push nothing to it. It
loads the console's own config and reuses the console's
`Aggregator` (`robofast_core/console/aggregate.py`), so each poll (every
`interval` seconds, 10 by default) it sees exactly the alarms the page
shows, with the same ids:

| Source | What raises an alarm |
|---|---|
| safety supervisor, `GET /status` | its own alarms (shutter not reaching the commanded state, heartbeat, ...), an enclosure fault, a quarantined lease, a revoked lease not released, the supervisor not answering (critical) |
| sequencer, its `status_file` snapshot | quarantined or needing an operator (critical), not running (snapshot stale or missing), a telescope that gave up for the night or lost frames (critical) |
| each guider, `GET /status` | failed, its own alarms, not answering |
| KiwiSpec, `GET /status` | its alarms, not answering |
| weather bridge, `GET /weather/status` | *would close* (shadow rain), not answering |
| the console | its acknowledgements file (`acks` in its config) acknowledges; with `console_url`, the console not answering `/healthz` is itself an alarm |
| the notifier | a source that raises or times out (critical) |

Why polling rather than a shared alarm sink that each service writes to:

* **Nothing changes in the safety paths.** The supervisor and sequencer
  already publish their state; adding a "send an alarm" call to them would put
  a new dependency, and a new way to block or fail, into the code that closes
  the roof. Here the safety processes do not know the notifier exists, and a
  dead or wedged notifier costs only the messages.
* **One definition of an alarm.** The console and the notifier read the same
  sources through the same code, so what the observer acknowledges on the page
  is what stopped the email, with the same id.
* **Silence is detected.** A service that dies cannot push "I died"; a poll
  notices that it stopped answering.

The cost is latency (at most `interval` plus the status timeouts) and an
alarm that clears between two polls is never seen. The console's alarm ids
hash the enclosure, source and text, and some texts include the error (e.g.
`safety supervisor unreachable (ConnectError: ...)`), so a change of error
message is a new alarm; the rate limit bounds what that can cost.

The notifier needs only read access: the console's config, the enclosure
configs, the sequencer's status snapshot, the console's acknowledgements and
the services' status ports. It never writes anything but its own state file,
and it never commands anything.

## What it sends

Every notification has a *kind* and a *level*. Levels, lowest first:
`info` < `warning` < `alarm` < `critical` (the console's levels; `alarm` is
its default).

| Kind | When |
|---|---|
| `raise` | an alarm first seen. While it stays active it is **not sent again** (deduplicated by id) |
| `escalate` | an alarm at `escalate_level` (default `critical`) or above, **not acknowledged** `escalate_after` seconds (600) after it was raised; again every `escalate_repeat` seconds if set. An acknowledgement on the console stops it |
| `ack` | an observer acknowledged it on the console (who, when, why) |
| `clear` | gone for `clear_after` seconds (120); a shorter gap is a flap, not a clear. If it comes back later that is a new episode, sent again |
| `digest` | once a day at `digest_at` local time (`timezone`, an IANA name; default the host's): every alarm since the last digest, whether acknowledged, escalated, cleared or still active, and each sink's delivery counts (sent, failed, suppressed, dropped) |
| `suppressed` | the rate limit held some back (below) |
| `test` | `--test` |

The digest doubles as a daily check that the notifier is alive: **no digest
means the notifier is not running**.

An acknowledgement counts for the episode it belongs to: one older than the
episode's first sighting (minus `ack_grace`, 60 s, because the console may
see an alarm and an observer acknowledge it before the notifier's next poll)
is from an earlier episode of the same alarm and does not stop escalation.

`ignore` is a list of regular expressions; an alarm whose text matches is
never notified (still shown on the console). The example ignores the
sequencer being down, which is true all day (the sequencer runs one night
per process, `deploy/robofast-sequencer@.service`); a sequencer that needs an
operator or is quarantined is a different alarm and still goes out.

With `state` set, the active alarms, the episodes for the next digest and the
digest schedule are written (atomically) after every poll and read at start,
so a restart neither re-sends active alarms nor resets their escalation
clocks nor skips a digest.

## Sinks

Each entry of `sinks` is one sink; `type` picks it, and every sink takes:

| Key | Default | Meaning |
|---|---|---|
| `min_level` | `warning` | lowest level sent (`digest`, `suppressed` and `test` always pass) |
| `on` | `["raise", "escalate", "digest"]` | kinds it takes; an escalation-only sink (the pager) is `["escalate"]` |
| `rate` | `{"max": 20, "window": 3600}` | at most `max` in any `window` seconds; the rest are counted, and when there is room again one `suppressed` message says how many, the highest level and the most recent subjects |
| `timeout` | per type | one attempt |
| `retries`, `retry_base`, `retry_max` | 3, 5 s, 300 s | retries with exponential backoff (with jitter) |
| `queue_size` | 100 | pending notifications; when full, new ones are dropped (and counted) rather than block |

**Delivery never blocks anything.** Each sink has its own asyncio task and
bounded queue; the poll only enqueues (`put_nowait`). Each attempt is bounded
(the sink's own timeout, and a backstop of that plus 10 s, `deliver_timeout`);
an exception is logged and retried with backoff, and after the last retry
the notification is counted as failed and the worker goes on to the next.
A failing or hanging sink delays only its own queue. And the notifier itself
is a separate process: nothing in observing waits on it.

### `email`

SMTP with `smtplib`, run in a thread (it blocks), `timeout` (20 s) on the
connection and every command.

```
{"type": "email", "host": "smtp.example.org", "port": 587, "tls": "starttls",
 "sender": "robofast@example.org", "to": ["observers@example.org"],
 "credentials_file": "$CREDENTIALS_DIRECTORY/smtp.json"}
```

* `tls`: `starttls` (default, port 587; refuses to continue if the server
  does not offer STARTTLS, never falls back to plain text), `ssl` (implicit
  TLS, port 465), or `none` -- only to a relay on loopback (a local
  Postfix), and never with credentials. The server's certificate is checked
  against the system's CAs.
* `credentials_file`: `{"username": ..., "password": ...}`, outside the
  repository. It is checked at start (so a mistake shows at deployment, not
  at 3 am) and re-read for each message (so rotating it needs no restart).

### `webhook`

POSTs JSON with `httpx` to a Slack-style incoming webhook (Slack, Mattermost,
a Matrix hookshot generic webhook, ...):
`{"text": "<subject>\n<body>", "robofast": {kind, level, subject, body, time, alarm}}`
plus any static `extra` fields (`username`, `channel`, ...), or with
`raw: true` the notification object alone. `timeout` 10 s; any non-2xx
answer is a failure. The URL is `url` or, since such URLs usually carry a
token, `url_file`; it must be `https` (plain `http` only to loopback).
Redirects are not followed and proxy environment variables are ignored.

### `command`

Runs a program -- `argv` is a list, `argv[0]` an absolute path; there is no
shell -- with the notification as JSON on stdin and in the environment:
`ROBOFAST_NOTIFY_KIND`, `_LEVEL`, `_SUBJECT`, `_ALARM_ID`, `_ENCLOSURE`, plus
the sink's `env` and `PATH`. Nothing else of the notifier's environment is
passed. A nonzero exit is a failure (retried); past `timeout` (30 s) the
program and its process group are killed. This is the hook for whatever
reaches a person at night: an SMS or voice gateway's client, a pager, a
script that calls the observer's phone.

```
{"type": "command", "argv": ["/usr/local/bin/page-observer", "--to", "observer-on-duty"],
 "timeout": 30, "min_level": "critical", "on": ["escalate"]}
```

### Secrets

Credentials files and `url_file` must be regular files that neither group nor
others can read or write (mode `0600` or `0400`); anything else is refused,
at start and at each read. `$CREDENTIALS_DIRECTORY` in their paths is
expanded, so systemd's `LoadCredential=` can hand them over
(`deploy/robofast-notifier.service`): the files then live only in
`/etc/robofast/secrets/` (root-owned, `0600`) and the service sees private
copies. Nothing secret is in the config, the repository or the logs.

## Config

```
{
  "console": "/etc/robofast/console.json",   # the console's config: enclosures and acks
  "console_url": "http://127.0.0.1:8400",    # optional: alarm if the console is down
  "state": "/var/lib/robofast-notifier/state.json",
  "status_timeout": 3,                       # each status read
  "interval": 10, "poll_timeout": 20,
  "clear_after": 120,
  "escalate_after": 600, "escalate_repeat": 1800, "escalate_level": "critical",
  "digest_at": "08:00", "timezone": "America/Phoenix",
  "ignore": ["^sequencer not running \\(status snapshot stale\\)$"],
  "sinks": {"email": {...}, "chat": {...}, "page-observer": {...}}
}
```

Instead of `console`, `enclosures` (in the console's form) and `acks` can be
given directly. Relative paths are relative to the config file. Unknown keys
are refused.

## Tests

`tests/core/test_notifier.py`, on a fake clock, fake sources and fake sinks:
the per-sink level filter, deduplication and clearing, the rate limit and its
summary, escalation (and its repeat) to an escalation-only sink, an
acknowledgement stopping it (and an old one not), acknowledgements read from
the console's file, the digest (once a day, across a DST change), a failing
sink and a hanging sink not delaying a good one, a full queue dropping, a
failing source becoming an alarm, state across a restart; the console's
aggregator as the source (`httpx.MockTransport`); the email sink's
STARTTLS-then-login sequence on a fake SMTP class, implicit TLS and the
loopback-only plain relay; the credentials permission check; the webhook's
payload and its https rule; the command hook's stdin and environment, its
exit status and its timeout killing it; and the config, `--check` and
`--test`. Nothing leaves the machine.

## Not done yet

* An end-to-end run against the simulated night (`docs/demo.md`) with a
  local SMTP catcher.
* Acknowledging from the message itself (a reply or a link); acknowledgement
  is on the console only.
* Quiet hours or an on-call rota (who is paged depends on the night); for now
  the command hook's program decides.
