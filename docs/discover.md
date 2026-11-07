# Discovery and a first exposure

`robofast-discover` (`robofast/discover.py`) finds the ASCOM Alpaca
devices on the network and writes a starter enclosure config from what
they report, so a new telescope gets to its first exposure without the
addresses typed by hand. It reads only: no device is connected, and no
device member is read or set.

## 1. Find the devices

```sh
robofast-discover                         # broadcast on the local network
robofast-discover --address 127.0.0.1     # servers on this machine only
robofast-discover --server 10.0.0.5:11111 # one server, no UDP (a tunnel)
```

It sends Alpaca discovery (`alpacadiscovery1`, UDP 32227), waits
`--timeout` seconds (default 2) for every `{"AlpacaPort": N}`, and asks
each server for its devices (`GET /management/v1/configureddevices`):

```
Camera               127.0.0.1:32323/0        Alpaca Camera Sim
Dome                 127.0.0.1:32323/0        Alpaca Dome Simulator
SafetyMonitor        127.0.0.1:32323/0        Alpaca Safety Monitor Simulator
Telescope            127.0.0.1:32323/0        Alpaca Telescope Simulator
...
```

Discovery does not cross a router or an SSH tunnel (docs/remote.md): give
those servers with `--server`. A server that does not answer discovery
(some ASCOM Remote setups turn it off) is also given with `--server`.

## 2. Write the starter config

```sh
robofast-discover --write observatory.json --name classroom
```

It never overwrites a file. What it writes:

| Key | From |
|---|---|
| `telescopes.t1`, `t2`, ... | each Telescope, with the Camera, FilterWheel, Focuser and Rotator of the same device number on the same server; for a lone telescope, the only one of a type |
| `images` | frames under `data/` next to the config (without `images` the sequencer saves nothing) |
| `plan` | one bias: `{"type": "calibration", "biases": 1}` |
| `name`, `listen` | `--name`; the supervisor on `127.0.0.1:8100` |

What it **never** writes, because it is the operator's (SAFETY.md):
`devices` (the dome, the safety monitor, the weather), `limits`, `site`,
`mode`, `safety`. A Dome, SafetyMonitor or ObservingConditions that
answered is named in `_note` with the line to add, and so is anything it
could not pair. Add them yourself:

```json
"devices": {"dome": "127.0.0.1:32323/0", "safetymonitor": "127.0.0.1:32323/0"}
```

The supervisor does not start without `devices.dome` and
`devices.safetymonitor`. Without `limits` the observer console refuses
manual slews (`robofast/config.py`). The mode is `attended` until you
change it (docs/modes.md).

## 3. The first exposure

In one terminal, the safety supervisor (it keeps running):

```sh
robofast-supervisor --config observatory.json
```

In another, the sequencer:

```sh
robofast-sequencer --config observatory.json
```

The sequencer **parks every telescope when it starts** (its startup
cleanup), then takes the bias with the enclosure closed and no lease, writes
`data/classroom/t1/nYYYYMMDD/nYYYYMMDD.t1.bias.0001.fits` and its frame
index (docs/data.md), and exits when the plan is done. From here, replace
the `plan` (docs/request_language.md, docs/sequences.md) and add the
site's own entries.

`tests/core/test_discover.py` runs these steps on loopback against fakes
behind `FakeAlpacaServer` (which answers the management API, and discovery
with `discovery_port`).
