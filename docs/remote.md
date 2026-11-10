# Running robofast off-site through an SSH tunnel

How to run robofast's device servers on a machine away from the site,
reaching the real hardware through SSH port forwards on the site gateway.
This is for **attended engineering sessions**: someone watches a webcam,
an abort is at hand, and nothing runs unattended. The configs are in
`sites/minerva/remote/`; the telescope server and its first-session
checklist are in [pwi.md](pwi.md).

## The tunnel

You supply the forwards; nothing in this repository names a site host or
address. One `ssh` command on your machine, kept open for the session:

```
ssh -N -p <gateway-ssh-port> \
    -L 7100:<aqawan1-pac>:22004 \
    -L 7101:<t1-pc>:44444  -L 7102:<t2-pc>:44444 \
    -L 7151:<t1-pc>:<ascom-remote-port> -L 7152:<t2-pc>:<ascom-remote-port> \
    <user>@<gateway>
```

### PWI4 through the telescope PC's sshd

PWI4's HTTP server listens on the telescope PC's own loopback
(127.0.0.1:8220) only, so a forward from the gateway cannot reach it.
Hop through the gateway to the telescope PC's sshd and forward to *its*
loopback (one command per telescope; in `~/.ssh/config` a `ProxyJump`
entry does the same):

```
ssh -N -J <user>@<gateway>:<gateway-ssh-port> \
    -L 7141:127.0.0.1:8220 <user>@<t1-pc>
ssh -N -J <user>@<gateway>:<gateway-ssh-port> \
    -L 7142:127.0.0.1:8220 <user>@<t2-pc>
```

The alternative, making PWI4 listen on the LAN with a Windows firewall
rule that admits only the gateway, puts the telescope's HTTP control on
the site network; prefer the hop.

`ssh -L` binds 127.0.0.1 by default; keep it that way (never `-g`, never
`0.0.0.0`). The local ports robofast's remote configs expect:

| Local port | Far end | Used by | Status |
|---|---|---|---|
| 7100 | Aqawan 1 PAC (TCP 22004) | `aqawan1_shadow.json`, `aqawan1_control.json` | see the Aqawan caution |
| 7101 / 7102 | T1 / T2 PWI2 HTTP (44444) | `t1_pwi2.json`, `t2_pwi2.json` | in use by the legacy system today |
| 7141 / 7142 | T1 / T2 PWI4 HTTP (8220, the PC's loopback: second hop, above) | `t1_pwi4.json`, `t2_pwi4.json` | T1: PWI4 4.1.6 installed; T2: not installed |
| 7151 / 7152 | T1 / T2 ASCOM Remote (Alpaca: cameras, the PC's loopback 11111) | `aqawan1.json` cameras | installed (6.7.1, 32-bit server): camera 0 = science camera, 1 = ZWO; T2 filterwheel 0 |
| 7161 / 7162 | T1 / T2 PDU through `scripts/snmp_relay.py` on the gateway (below) | `t1_pdu.json`, `t2_pdu.json` | relay: start it for the session |

Check what is forwarded before starting anything: `ss -ltn | grep ':71'`.

### What a tunnel cannot carry

* **UDP.** `ssh -L` forwards TCP only, and the PDUs speak SNMP on UDP
  161. `scripts/snmp_relay.py` bridges that: run it on the gateway (it
  needs only Python 2.7's standard library), listening on the gateway's
  loopback and relaying to **one** PDU, forward its port, and set
  `"relay": true` in the PDU server's `pdu` block:

  ```
  # on the gateway (a user process; it touches nothing but the PDU it names)
  python snmp_relay.py --listen 127.0.0.1:7237 --target <pdu address>:161
  # here
  ssh -N -L 7162:127.0.0.1:7237 <gateway>
  robofast-pdu-server --config sites/minerva/remote/t2_pdu.json --hardware --status
  ```

  `--status` reads every outlet; `--check-write N` also sets outlet N to
  the state it already has, which proves the write community works
  without switching anything. Alpaca discovery (UDP 32227) still does not
  cross a tunnel.
* **Anything the far end does not expose on TCP**: serial devices on a
  site PC stay with that PC's own server.

## Why a tunnel needs the hardware gate

A forward puts real hardware on `127.0.0.1`. robofast therefore never
treats loopback as "a simulator": without `"allow_hardware": true` in the
config **and** `--hardware` on the command line, a server talks only to a
simulator it has verified itself (design.md, [the hardware
gate](design.md#the-hardware-gate)). Started without `--hardware`, every
remote config is refused before a byte is sent:

```
$ robofast-aqawan-server --config sites/minerva/remote/aqawan1_shadow.json
... PAC at 127.0.0.1:7100/tcp is loopback but not a simulator robofast could
verify (... an SSH tunnel to real hardware looks exactly like this) ...
```

Every file in `sites/minerva/remote/` says `"allow_hardware": true`, so
`--hardware` is the one deliberate step that reaches hardware. The
enclosure config's mode is `engineering`: nothing autonomous, no plan, no
automatic opening.

## The Aqawan caution

The legacy dome daemon (`domeControl.py` on the site gateway) is
connected to the Aqawan 1 PAC and is mission-critical. Whether the PAC
tolerates a **second** client is not known. Until it is:

* do not start any robofast Aqawan server against the real PAC;
* the first connection, when JDE decides to try it, is the **shadow**
  server, `aqawan1_shadow.json`: `"read_only": true`, so it sends only
  `STATUS`, `GET_FAULTS` and `GET_ERRORS` (the PAC client refuses every
  other command before the wire; `OpenShutter`, `CloseShutter`,
  `AbortSlew`, `Heartbeat` and `ClearFaults` are refused), and at most one
  `STATUS` per 10 s (`status_max_age`). Watch the legacy dome log while it
  runs, and stop it at the first sign of trouble;
* a read-only server **cannot close the enclosure**. Never point a safety
  supervisor at it as its only dome: the supervisor would believe it can
  close and could not.
* `aqawan1_control.json` (full control, it can open) is only for after
  the second-client question is answered, or with the legacy daemon
  stopped by JDE.

## Which servers, in what order

### First hardware session: telescope status and small moves only

The enclosure stays closed and under the legacy system. No supervisor, no
sequencer, no Aqawan server.

1. Before anything: the checklist in [pwi.md](pwi.md#first-hardware-session)
   (enclosure closed, legacy users of PWI stopped, webcam watching, PDU
   outlet known, abort at hand).
2. Open the tunnel (above); check the port with `ss -ltn`.
3. Read the mount's status only, and leave a watch running (T1 on PWI4
   through the 7141 hop; `t1_pwi2.json` and 7101 for PWI2):

       C=sites/minerva/remote/t1_pwi4.json
       robofast-pwi-jog --config $C --hardware status
       robofast-pwi-jog --config $C --hardware status --watch 2

   Compare with the webcam and the PWI window (the status shows PWI's
   version, alt/az, connection, motors and whether the mount is homed). In
   a second terminal, type `robofast-pwi-jog --config $C --hardware stop`
   and leave it ready.
4. Tiny bounded jogs **before any home**, one at a time, each confirmed
   (`yes`), watched on the webcam, to see which way each axis goes:
   `robofast-pwi-jog --config $C --hardware --allow-unhomed altaz --dalt 0.2`
   and back, then `--daz 0.2` and back (`connect`, `enable` and
   `tracking-off` first only if the status says so). The exact sequence is
   in [pwi.md](pwi.md#first-hardware-session). Ctrl-C during a move sends
   stop.
5. Only then, optionally, the PWI Alpaca server
   (`robofast-pwi-server --config $C --hardware`), reading status through
   Alpaca first, to check the mapping.

**No home until the settings are checked.** T1's home sensor was replaced years
ago and the PWI2 home-direction setting changed with it, so a Find Home
from PWI4 may drive the wrong way: nothing in robofast homes implicitly,
and pwi.md's checklist confirms the axis directions with tiny jogs and maps
the PWI2 settings to PWI4 (pwi.md checklist 7) before the first home, one
axis at a time where PWI4 allows it.

Stop a move **before** closing the tunnel: when the tunnel drops, the mount
carries on with whatever it was last told.

### Later sessions (with JDE's approval for each step)

1. Weather bridge: `robofast-weather-bridge --config sites/minerva/weather.yaml`
   (off-site, sources it cannot reach read as unknown, so the weather is
   unsafe and nothing opens).
2. Aqawan: the shadow server, then (only per the caution) the control one.
3. Telescope servers: `robofast-pwi-server` for T1 and T2 (PWI2 now, PWI4
   once installed: one or the other per telescope, never both).
4. Supervisor, only with the **control** Aqawan server:
   `robofast-supervisor --config sites/minerva/remote/aqawan1.json`.
5. Sequencer, for the console's manual control (engineering mode asks no
   plan): `robofast-sequencer --config sites/minerva/remote/aqawan1.json`.

Stop in the reverse order. Each server's log names the hardware it talks
to (`HARDWARE MODE: ...`) at start.
