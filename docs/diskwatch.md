# Disk space

A full disk stops a computer quietly: the camera PC cannot save a frame,
the spectrograph PC cannot write a spectrum, and nothing else notices. On
2026-11-24 a night's spectroscopy failed because the spectrograph PC's disk
had been full for two weeks. The disk watch (`robofast/diskwatch.py`) reads
the free space of every computer's disks every few minutes and raises an
alarm on the observer console when one is low, nearly full, full, filling
fast, or cannot be read. The notifier sends the console's alarms
(docs/notifier.md), so these reach email and phones too.

It only reads. It never deletes, moves or changes anything.

## Running it

```sh
python -m robofast.diskwatch --config diskwatch.json          # the service: a pass every interval
python -m robofast.diskwatch --config diskwatch.json --once   # one pass, a table; exit 1 if anything alarms
```

`deploy/robofast-diskwatch.service` is an example unit and
`deploy/diskwatch.example.json` an example config. `--once` is for an
operator: it reads the history from the status file (for the forecast)
but does not write it, and a disk it cannot read alarms at once.

## How a disk is read

Each entry of `targets` is one disk, or one computer's disks:

* **`"probe": "path"`** reads `os.statvfs` on a path on the gateway. That
  is the gateway's own disks, and also the other computers' disks through
  the gateway's CIFS or NFS mounts: `statvfs` on a network mount reports the
  remote volume. A network mount can hang. Each read runs in its own thread
  and is given up after `timeout` (10 s): the disk is *unknown*. While that
  read is still stuck, no second one is started for the same path (the row
  says "still hung since ..."), so a dead server costs one thread, not one
  per pass.
* **`"probe": "ssh"`** runs a command over `ssh -o BatchMode=yes` (key
  authentication only; never a password): `df -Pk` on Linux (`"os":
  "linux"`), or on Windows (`"os": "windows"`)
  `wmic logicaldisk where DriveType=3 get Caption,FreeSpace,Size /format:csv`.
  Every fixed filesystem (on a `/dev` device, loop devices left out) or
  fixed drive is a row of its own, `<name> C:` or `<name> /data`. This sees
  disks that no share exposes, like a full `D:`. `drives` (`["C:", "D:"]`)
  or `mounts` (`["/", "/data"]`) limit the rows. The ssh process is killed
  at `ssh_timeout` (30 s). The service's user needs a key that the PC
  accepts, and the host in its `known_hosts`.

A target that cannot be read is shown as *unknown* at once, and becomes a
`warning` alarm ("free space unknown") after `unknown_after` (600 s), so a
PC that is rebooting does not page anyone.

## Levels

Each disk is graded on every pass, worst first:

| Level | When (defaults) |
|---|---|
| `critical` | free space at or under `critical_free_gb` (1 GB): the disk is full |
| `alarm` | under `alarm_free_gb` (5 GB), or `alarm_pct` (97 %) used |
| `warning` | under `warn_free_gb` (20 GB), or `warn_pct` (90 %) used, or full within `warn_days` (7) at the present rate |

GB are 10^9 bytes. "Used" is counted as `df` counts it: blocks reserved for
root are left out, so a disk with nothing left for users is 100 % used.
Set a threshold to `null` to turn it off (a 10 TB archive disk 90 % full
still has 1 TB free: `"warn_pct": null`). The thresholds are set at the
top of the config for every target and can be set again in a target.

**The forecast.** Each disk keeps `[time, free]` samples for the last
`window_hours` (24) in the status file, so they survive a restart. The
rate is the least-squares slope through them, used once there are at least
3 samples over at least an hour. Free space going *up* by more than 1 GB or
1 % of the disk (someone cleaned it, a transfer finished) starts the
samples again, so a cleanup does not hide the next fill. A disk that is not
falling has no forecast.

## The same disk twice

Two shares of one disk (`//pc/data` and `//pc/log`), or a share and the ssh
row of the drive behind it, would raise the same alarm twice. A row is the
same as an earlier one when:

* its `volume` matches: a path target's `volume` label, or an ssh row's own,
  which is `<host> <drive or mount>` (`"192.0.2.22 D:"`); or
* it has exactly the same size and free space as an earlier row of the same
  `host` (two reads a few milliseconds apart of one volume).

It is listed with `same_as` and raises no alarm. Give `volume` when it must
be sure: a disk being written between two reads can differ by a few bytes.

## The status file and the console

Every `interval` (300 s) the status file is written atomically (a temporary
file, then a rename): per row the name, host, path or drive, total and free
bytes, used %, level, reason, days to full, the last good read and the
error; the alarms; the samples; and `max_age` (three intervals and a
minute).

The console reads it with `"diskwatch": "<the status file>"` at the top of
its config (docs/console.md). It belongs to the site, not an enclosure: the
status document has a `site` entry beside `enclosures`, the page a Disks
table, and the alarms come under enclosure `site`, source `diskwatch`. The
notifier reads the console's config, so it carries them; a notifier
configured with `enclosures` instead takes `diskwatch` itself.

An alarm's id hashes its text (docs/console.md), so **the text names the
disk and the level only**: "KIWISPEC-PC D: (192.0.2.22): disk full". The
free space, the threshold crossed and the forecast are in the alarm's
detail, which may change from one read to the next. A disk filling from 4
to 3 GB stays the same alarm (one notification, an acknowledgement
sticks). A disk that gets worse, from `alarm` to `critical`, is a new alarm
and is sent again.

The disk watch itself is watched: a status file older than its `max_age`
raises "disk watch not running (status file stale)" (`alarm`) and its rows
then raise nothing; a missing or unreadable file is an alarm too.

## Configuration (`deploy/diskwatch.example.json`)

| Key | Default | What |
|---|---|---|
| `status` | (required for the service) | the status file the console reads |
| `interval` | 300 s | between passes |
| `timeout` | 10 s | one `statvfs` |
| `connect_timeout`, `ssh_timeout` | 10 s, 30 s | ssh's connection, and the whole command |
| `unknown_after` | 600 s | an unreadable disk alarms after this long |
| `ssh_command` | `["ssh"]` | the ssh client (options such as `-F <file>` go here) |
| `warn_free_gb`, `alarm_free_gb`, `critical_free_gb` | 20, 5, 1 | free space |
| `warn_pct`, `alarm_pct` | 90, 97 | used % |
| `warn_days`, `window_hours` | 7, 24 | the forecast |
| `targets` | -- | each: `name`, `host` (a label, shown and used to match volumes), `probe`, then `path` and `volume`, or `ssh` (the host alias), `os`, `drives` or `mounts`; and any threshold |

The passes read all targets at once (up to 8 at a time), each bounded by
its timeout, so a pass takes at most about `ssh_timeout` even when PCs
are down.
