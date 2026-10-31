# Deploying robofast on a control host

How to install and run `robofast_core` on a modern Linux control host:
users, directories, the systemd units in `deploy/`, secrets, the firewall,
logs, time, backups, upgrades and rollback, and checking that what runs is
what is in the repository. Everything here is generic: host names are
`example.org` placeholders and addresses are from the documentation ranges
(RFC 5737: `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`). Adapt
names, ports and paths to the site; keep the shape.

## The host

robofast needs Python 3.12 or later (CI runs 3.12 to 3.14), a current
systemd (249 or later for the sequencer unit's `OnSuccess=`; `LoadCredential=`
needs 247) and `chrony`. Any maintained distribution with those works, e.g.
Debian 12/13, Ubuntu 24.04 or RHEL/Alma/Rocky 9 or later. **An old site
gateway (for example one still on Ubuntu 12.04 with Python 2.7) cannot run
robofast**: keep it for what it does now and run robofast on a new host on
the same control network.

Which process runs where is a site choice (`docs/design.md` 3.9): the
supervisor, sequencer, request API, console and notifier usually share the
control host; a guider runs on its guide computer, the KiwiSpec service next
to the spectrograph, the weather bridge and device servers (Aqawan, TCSGO)
wherever their hardware is. The same release layout and checks apply on
each.

## Users and groups

One system user per service, no login shell, no home: a bug or a stolen
token in one service then reaches only what that user can write.

| User | Runs | Writes |
|---|---|---|
| `robofast-supervisor` | `robofast-supervisor@<enclosure>` | nothing on disk (its state is in memory; logs to the journal) |
| `robofast-sequencer` | `robofast-sequencer@<enclosure>` (and its timer), `robofast-calibrations@<enclosure>` | frames under `data_root`, the status snapshot, the dispatch state, the plan records, the focus store, `operator_control/holds.json`; consumes the drop box and the console's commands |
| `robofast-api` | `robofast-request-api` | the drop box (`too_dropbox`), its audit log, the tokens file (`tokens add`) |
| `robofast-console` | `robofast-console` | `operator_control/inbox/`, the drop box (requests through the request API's code), its acknowledgements, its audit log |
| `robofast-notifier` | `robofast-notifier` | its own state (`StateDirectory=`) |
| `robofast-guider`, `robofast-kiwispec`, `robofast-weather`, ... | the device services, on their hosts | their calibration or state directories |

and three groups, for the deliberate file exchanges of `docs/design.md` 3.8:

| Group | Members | Shares |
|---|---|---|
| `robofast` | every service user | read-only: `/etc/robofast`, the sequencer's status snapshot, the console's acknowledgements |
| `robofast-dropbox` | `robofast-api`, `robofast-sequencer`, `robofast-console` | the drop box and the dispatch scheduler's state file (request API and console write requests in, the sequencer takes them and writes the state out) |
| `robofast-control` | `robofast-console`, `robofast-sequencer` | the console's `operator_control` directory (commands in, `holds.json` out) |

```
for g in robofast robofast-dropbox robofast-control; do groupadd --system "$g"; done
for u in supervisor sequencer api console notifier; do
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
        --gid robofast "robofast-$u"
done
usermod -aG robofast-dropbox,robofast-control robofast-sequencer
usermod -aG robofast-dropbox,robofast-control robofast-console
usermod -aG robofast-dropbox robofast-api
```

The shared directories are setgid (mode `2770`), so every file created in
them belongs to the directory's group whoever writes it, and the services
run with `UMask=0027` (add it to the units, below) so what they write is
group-readable but never world-readable. Everything robofast writes is
replaced atomically (a temporary file renamed into place), so a reader never
sees half a file.

## Directory layout

```
/opt/robofast/
    releases/<tag>/           a checkout of the tag, root-owned, read-only to the services
    releases/<tag>/venv/      that release's virtualenv (ignored by git)
    current -> releases/<tag> the release that runs
/etc/robofast/                configs (root:robofast 0750, files 0640)
    aqawan1.json              one per enclosure: supervisor and sequencer (sites/minerva/ has examples)
    aqawan1-calibration.json  its calibration plan
    request_api.json, console.json, notifier.json
    api-tokens.json           robofast-api:robofast 0640 (salted hashes; the console reads it)
    secrets/                  root:root 0700: SMTP login, webhook URL (LoadCredential=)
/var/lib/robofast/            root:robofast 0750
    dispatch/aqawan1.json     dispatch state        in a robofast-sequencer:robofast-dropbox 2750 dir
    too/                      drop box              root:robofast-dropbox 2770
    status/aqawan1.json       status snapshot       in a robofast-sequencer:robofast 2750 dir
    control/aqawan1/          operator_control      root:robofast-control 2770
    plans/                    plan records          robofast-sequencer:robofast 2750
    focus/focus_t1.json, ...  focus stores          robofast-sequencer 0750 dir
    console/acks.json         console acks          robofast-console:robofast 2750 dir
/var/lib/robofast-notifier/   notifier state (systemd StateDirectory=, 0700)
/var/log/robofast/            root:robofast 0750: the audit logs, one file per service
<data_root>/                  frames and sidecars (docs/data.md), robofast-sequencer, on its own filesystem
```

Point the configs at these paths (`status_file`, `operator_control`,
`state`, `too_dropbox`, `record`, the focus `store`, `acks`, `audit_log`,
`data_root`). Each process writes its files atomically, through a temporary
file next to the target, so it needs write access to the *directory*: that
is why each writer has its own directory rather than files in
`/var/lib/robofast` itself. A `systemd-tmpfiles` snippet keeps the tree right across
reboots and mistakes (`/etc/tmpfiles.d/robofast.conf`):

```
d /var/lib/robofast                 0750 root               robofast         -
d /var/lib/robofast/dispatch        2750 robofast-sequencer robofast-dropbox -
d /var/lib/robofast/too             2770 root               robofast-dropbox -
d /var/lib/robofast/focus           0750 robofast-sequencer robofast         -
d /var/lib/robofast/status          2750 robofast-sequencer robofast         -
d /var/lib/robofast/control         0750 root               robofast-control -
d /var/lib/robofast/control/aqawan1 2770 root               robofast-control -
d /var/lib/robofast/plans           2750 robofast-sequencer robofast         -
d /var/lib/robofast/console         2750 robofast-console   robofast         -
d /var/log/robofast                 0750 root               robofast         -
f /var/log/robofast/request-api-audit.jsonl 0640 robofast-api     robofast -
f /var/log/robofast/console-audit.jsonl     0640 robofast-console robofast -
```

The sequencer writes the dispatch state in its setgid directory, so the
request API and the console (group `robofast-dropbox`) can read it; it
moves what it takes from the drop box into `accepted/` and `rejected/`
there.

## The systemd units

`deploy/` holds one example unit per control-host service. They run
`/opt/robofast/current/venv/bin/python -m robofast_core....` with
`WorkingDirectory=/opt/robofast/current` (the code is run from the checkout,
not installed) and read `/etc/robofast/`. Install them as files, not
symlinks into a release, so a release switch never changes a unit behind
systemd's back:

```
cd /opt/robofast/current/deploy
install -m 0644 robofast-supervisor@.service robofast-sequencer@.service \
    robofast-sequencer@.timer robofast-calibrations@.service \
    robofast-request-api.service robofast-console.service robofast-notifier.service \
    /etc/systemd/system/
systemctl daemon-reload
```

Site changes go in drop-ins (`systemctl edit <unit>`, kept in
`/etc/systemd/system/<unit>.d/`), never in the installed copies, so the
installed unit stays identical to `deploy/` and the differences are in one
place. A drop-in every service should get:

```
# /etc/systemd/system/robofast-console.service.d/site.conf (and the others)
[Service]
UMask=0027
```

| Unit | What it is | Enable |
|---|---|---|
| `robofast-supervisor@.service` | templated: the safety supervisor of one enclosure (`%i`, config `/etc/robofast/%i.json`). Restarted on any exit, forever (`StartLimitIntervalSec=0`); starts closed and closes on SIGTERM | `systemctl enable --now robofast-supervisor@aqawan1` |
| `robofast-sequencer@.timer` | starts that enclosure's sequencer every afternoon (14:00 local, `Persistent=true`) | `systemctl enable --now robofast-sequencer@aqawan1.timer` |
| `robofast-sequencer@.service` | templated: one night: evening calibrations while the Sun is up, wait for the Sun, the night, exit at dawn. Restarted on failure, not after exit 3 (needs an operator) or 0. Started by the timer; **do not enable it**, only the timer | (by the timer; `systemctl start` by hand) |
| `robofast-calibrations@.service` | templated oneshot: morning calibrations, started by the sequencer's `OnSuccess=` after a dawn exit | (by the sequencer) |
| `robofast-request-api.service` | the request API on loopback, behind the TLS proxy | `systemctl enable --now robofast-request-api` |
| `robofast-console.service` | the observer console on loopback, behind the TLS proxy | `systemctl enable --now robofast-console` |
| `robofast-notifier.service` | the alarm notifier (`docs/notifier.md`); its secrets by `LoadCredential=` | `systemctl enable --now robofast-notifier` |

`Caddyfile.request-api` and `nginx-request-api.conf` are the TLS proxy for
the request API; the console uses the same with its port (8400) and response
buffering off (nginx `proxy_buffering off;`) for its event stream. The device
services (guider, KiwiSpec, weather bridge, Aqawan and TCSGO servers) have
no units in `deploy/` yet; write them on the console's model (their own
user, `Restart=always`, `ProtectSystem=strict`, `ReadWritePaths=` their
state directory).

After installing, `systemctl list-timers 'robofast-*'` shows the next
sequencer start and `systemctl status 'robofast-*'` the services;
`journalctl -u robofast-supervisor@aqawan1 -f` follows one.

## Secrets

Nothing secret is ever in the repository or in a config that is: request API
tokens (`api-tokens.json`: only salted hashes, but still; `.gitignore`
excludes `*tokens*.json`), the SMTP login, chat webhook URLs (they embed a
token), weather-page credentials.

* Each lives in its own file, owned by the service user that reads it, mode
  `0600` (`0640` with a group only where two services must read it, as the
  tokens file is for the request API and the console).
* The notifier refuses a credentials or webhook-URL file that group or
  others can read. The better way is systemd's `LoadCredential=` (as in
  `deploy/robofast-notifier.service`): the file stays `root:root 0600` in
  `/etc/robofast/secrets/`, and systemd gives the service a private,
  read-only copy in `$CREDENTIALS_DIRECTORY` that no other user sees; the
  config refers to `$CREDENTIALS_DIRECTORY/smtp.json`.
* Keep `/etc/robofast` itself under version control in a **private**
  site-config repository (or `etckeeper`), with `secrets/` excluded.
* Rotating: replace the file atomically (`install -m 0600 new path`); the
  notifier re-reads its credentials for each message, the request API and
  console re-read the tokens file; a `LoadCredential=` secret needs a
  `systemctl restart`.

## Firewall

Alpaca has no authentication, nor do the supervisor's, guiders' and
KiwiSpec's HTTP APIs: they must be reachable **only on the isolated control
network** (`docs/design.md` 3.9). From outside, only the TLS proxy (443) is
reachable, and it forwards only to the request API and the console on
loopback. SSH comes from an admin network only.

Ports in the examples: supervisor 8100, request API 8300 and console 8400
(both loopback), guider 8140, KiwiSpec 8130, weather bridge (Alpaca) 11111,
Aqawan Alpaca server 11113, Alpaca discovery UDP 32227. An nftables example
for a host with a public interface `pub0` and a control interface `ctl0`
(`/etc/nftables.conf`):

```
table inet filter {
  set admin   { type ipv4_addr; flags interval; elements = { 203.0.113.0/28 } }
  set control { type ipv4_addr; flags interval; elements = { 198.51.100.0/24 } }

  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif "lo" accept
    meta l4proto { icmp, ipv6-icmp } accept
    tcp dport 22 ip saddr @admin accept
    # The TLS proxy: request API and console only.
    iifname "pub0" tcp dport { 80, 443 } accept
    # Control services: the control network, on its interface, only.
    iifname "ctl0" ip saddr @control tcp dport { 8100, 8130, 8140, 11111, 11113 } accept
    iifname "ctl0" ip saddr @control udp dport 32227 accept
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output priority 0; policy accept; }
}
```

The same with ufw:

```
ufw default deny incoming
ufw allow in on pub0 to any port 443 proto tcp
ufw allow in on pub0 to any port 80 proto tcp
ufw allow from 203.0.113.0/28 to any port 22 proto tcp
for p in 8100 8130 8140 11111 11113; do
    ufw allow in on ctl0 from 198.51.100.0/24 to any port $p proto tcp
done
ufw allow in on ctl0 from 198.51.100.0/24 to any port 32227 proto udp
```

Also bind each service to the address it needs (`listen`: loopback for the
request API and console; the control-network address for the supervisor
and device services), so a firewall mistake alone does not expose it. The
notifier needs outbound SMTP submission (587 or 465) and HTTPS to the chat
service; restrict output too if the site's policy allows, and never route
the control network to the internet.

## Logs

Services log to the journal. Make it persistent and bounded
(`/etc/systemd/journald.conf.d/robofast.conf`):

```
[Journal]
Storage=persistent
SystemMaxUse=4G
MaxRetentionSec=1year
```

then `systemctl restart systemd-journald`. Night logs are evidence when
something went wrong: keep at least a season, and export what matters
(`journalctl -u 'robofast-*' --since ... -o json`) to the backup.

The audit logs (`/var/log/robofast/*-audit.jsonl`: every request-API and
console action, who and why) are opened for each write, so logrotate can
move them and create the next file (`/etc/logrotate.d/robofast`):

```
/var/log/robofast/request-api-audit.jsonl {
    monthly
    rotate 36
    compress
    delaycompress
    missingok
    notifempty
    create 0640 robofast-api robofast
}
/var/log/robofast/console-audit.jsonl {
    monthly
    rotate 36
    compress
    delaycompress
    missingok
    notifempty
    create 0640 robofast-console robofast
}
```

Plan records (`plans/<name>-<YYYY-MM-DD>.jsonl`) are one file per night and
the night's provenance: do not rotate them; back them up and archive old
nights (e.g. compress files older than 30 days with a timer). Frame logs
(`*.frames.jsonl`) live with the frames under `data_root`.

## Time

Every host must keep UTC to well under a second, with `chrony` on NTP
servers the site trusts (a GPS-disciplined server on the control network,
if there is one):

* the exposure timestamps and FITS `DATE-OBS` come from the clock of the
  host driving the camera (`docs/data.md`);
* the barycentric correction of an RV spectrum is computed at the
  exposure's (flux-weighted) mid-time; Earth's rotation alone changes it by
  about 3 cm/s per second of clock error;
* the lease and heartbeat timings are measured within each process on a
  monotonic clock, but the status snapshot's age, the plan record, the
  console, the notifier's escalation and the request API compare wall-clock
  times written by different processes, and the Sun-based starts
  (`robofast_core.when`, the timer) use the wall clock. A clock that steps
  makes services look stale or start at the wrong time.

Check:

```
chronyc tracking        # "System time" offset, "Leap status : Normal"
chronyc sources -v      # reachable sources, '*' on the selected one
timedatectl             # "System clock synchronized: yes"
```

and alarm on it: a timer that fails when the offset exceeds a limit, with
`OnFailure=` to a unit that sends a message, or a check the site's
monitoring already runs, e.g.

```
#!/bin/sh
# /usr/local/sbin/check-clock: exit 1 if the clock is off by more than 0.1 s.
off=$(chronyc -c tracking | cut -d, -f5)          # system time offset, seconds
awk -v o="$off" 'BEGIN { exit (o < 0 ? -o : o) > 0.1 }'
```

Allow chrony to slew, not step, during the night (the default `makestep`
steps only at boot).

## Backups

What to back up, from each host that has it:

| What | Where | Why |
|---|---|---|
| dispatch state | `/var/lib/robofast/dispatch/` | every request group, its state and what was observed |
| drop box | `/var/lib/robofast/too/` | requests submitted and not yet taken |
| focus stores | `/var/lib/robofast/focus/` | the focus model per telescope |
| operator holds | `operator_control/holds.json` | which telescopes an observer holds |
| console acknowledgements, notifier state | `/var/lib/robofast/console/`, `/var/lib/robofast-notifier/` | small; saves re-acknowledging |
| tokens | `/etc/robofast/api-tokens.json` | otherwise every client needs a new token |
| configs | `/etc/robofast/` (secrets separately, encrypted) | the site |
| plan records, audit logs | `/var/lib/robofast/plans/`, `/var/log/robofast/` | provenance, accountability |
| guider calibrations | `/var/lib/robofast/guider-calibrations/` | on the guide computers |
| frames | `<data_root>` | the data: the site's data-transfer plan, not this backup |

**When:** daily, in the day, after the morning calibrations and before the
sequencer's timer (e.g. 10:00 local): the sequencer is not running then, so
the state files are at rest. Every file is written atomically, so a copy
taken at another time is still each file whole, just not mutually
consistent. Also take one immediately before every upgrade.

**How:** a systemd timer running the site's backup tool (`restic`, `borg`,
or `rsync -a --numeric-ids` to another host), as root, preserving owner,
group and mode, to storage off the control host. Keep secrets only in an
encrypted backup.

**Restore:** in a safe window (day, roof closed), stop the services that
write the files (`systemctl stop robofast-sequencer@aqawan1.timer
robofast-sequencer@aqawan1 robofast-request-api robofast-console`), restore
with ownership and modes (`rsync -a --numeric-ids`, then
`systemd-tmpfiles --create` to put the directory modes right), and start
them again. A dispatch state older than the last night forgets what was
observed since: compare it with that night's plan record before the next
night, or the scheduler may observe a completed request again.

## Upgrades and rollback

A release is a git tag; the host runs exactly a tag, never a branch.

```
TAG=v0.4.0
cd /opt/robofast/releases
git clone --branch "$TAG" --depth 1 https://github.com/jdeast/robofast.git "$TAG"
cd "$TAG"
git describe --exact-match --tags          # prints $TAG
python3.12 -m venv venv
venv/bin/pip install --upgrade "pip>=25.1"
venv/bin/pip install --group core          # reads ./pyproject.toml; guider hosts add --group guider-hardware
venv/bin/pip freeze > venv/requirements.lock
```

(`pip install --group` needs pip 25.1 or later and is run in the release
directory.) Before switching, check the new release against the live
configs, as the service users:

```
sudo -u robofast-notifier /opt/robofast/releases/$TAG/venv/bin/python -m robofast_core.notifier \
    --config /etc/robofast/notifier.json --check
```

read the release notes and `git diff --stat <old-tag> $TAG -- deploy/` (a
changed unit is reinstalled and `daemon-reload`ed), and optionally run the
fast tests in a scratch venv (`pip install --group test`, `python -m pytest`).

**Switch in a safe window:** daytime, every enclosure closed
(`curl -s http://127.0.0.1:8100/status` shows the shutter closed and no
lease), no sequencer running (`systemctl is-active 'robofast-sequencer@*'`).
Then

```
ln -sfn "releases/$TAG" /opt/robofast/current.new && mv -T /opt/robofast/current.new /opt/robofast/current
systemctl restart 'robofast-supervisor@*' robofast-request-api robofast-console robofast-notifier
systemctl status 'robofast-*'
```

The sequencer picks up the new release at its next timer start. Watch the
first night on the console.

**Rollback** is the same switch back: point `current` at the previous
release directory (still there, venv and all), restart the same units, and
restore the pre-upgrade backup of the state files only if the new release
changed their format (its notes say so). Keep the last two or three
releases; delete older ones.

## Is what runs what is in the repository?

```
R=/opt/robofast/current
readlink -f $R                              # the release directory
git -C $R describe --exact-match --tags     # the tag
git -C $R status --porcelain                # empty: no modified or extra tracked-path files
git -C $R diff --stat "$(git -C $R describe --tags)"   # empty
```

`git` trusts its own index, so for an independent check keep a manifest
made from the tag itself and verify the tree against it:

```
# at install time, from a fresh clone elsewhere:
git -C /tmp/check archive "$TAG" | tar -x -C /tmp/tree
(cd /tmp/tree && find . -type f -print0 | sort -z | xargs -0 sha256sum) > /var/lib/robofast/manifest-$TAG.sha256
# any time later:
(cd /opt/robofast/releases/$TAG && sha256sum --quiet -c /var/lib/robofast/manifest-$TAG.sha256)
```

The units:

```
systemd-analyze verify /etc/systemd/system/robofast-*.service /etc/systemd/system/robofast-*.timer
for u in /opt/robofast/current/deploy/robofast-*; do
    diff -u "$u" "/etc/systemd/system/$(basename "$u")"
done
systemd-delta --type=extended,overridden   # which units have drop-ins
systemctl cat robofast-console             # the unit as systemd runs it, drop-ins included
```

Any difference between `deploy/` and `/etc/systemd/system/` should be an
upgrade not yet installed; site changes belong in the drop-ins. And
`git -C /etc/robofast status` (the private site-config repository) shows any
config edited on the host and not committed.
