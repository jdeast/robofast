"""robofast_core: a de-risking prototype of robofast's control core.

This package is deliberately separate from the legacy ``robofast`` package
(which configures file logging at import time and depends on Windows-only
modules). Nothing here imports ``robofast``.

The pieces:

* :mod:`robofast_core.owner` -- one :class:`DeviceOwner` per physical device.
  Every command to a device goes through its owner, which serializes them
  (asyncio, no threads), bounds each with a timeout, records it in the
  :class:`Timeline`, and refuses commands from revoked :class:`Grant` objects
  immediately before they are sent.
* :mod:`robofast_core.alpaca_client` -- a small asyncio ASCOM Alpaca client
  (one HTTP client per device, per-call timeouts, no process-wide lock);
  :mod:`robofast_core.alpaca` builds owners around it.
* :mod:`robofast_core.safety` -- the :class:`SafetySupervisor`, the only
  component allowed to open or close the enclosure. It fails closed: unsafe,
  stale, missing or erroring weather all mean "close". It hands out expiring
  :class:`OpenLease` objects; it reopens only after the weather has been
  continuously safe for a hold time.
* :mod:`robofast_core.sequencer` -- one asyncio :class:`Sequencer` per
  enclosure. Its observing task is cancelled when the lease is revoked, and
  cleanup aborts the exposure and parks the telescope.
* :mod:`robofast_core.imaging` -- the sequencer's frame pipeline: telemetry
  into the value registry (:mod:`robofast_core.registry`), the image
  download (ImageBytes, :mod:`robofast_core.imagebytes`), and a bounded
  background writer of FITS files (:mod:`robofast_core.fits_writer`;
  ``docs/data.md``).
* :mod:`robofast_core.dispatch` -- the dispatch scheduler, a plan provider
  that picks the best observable request for a telescope now, and takes
  targets of opportunity (preempting one telescope through the sequencer);
  requests are modelled in :mod:`robofast_core.request_model`, positions
  come from :mod:`robofast_core.sky` (astropy); every decision goes to the
  nightly plan record (:mod:`robofast_core.plan_record`, replayable).
* :mod:`robofast_core.when` -- waits for a Sun altitude, so the night's
  sequencer starts at dusk (``deploy/robofast-sequencer@.service``).
* :mod:`robofast_core.supervisor_service` -- the supervisor as its own
  process with a small HTTP/JSON lease API (its docstring says how to run
  it); :mod:`robofast_core.lease_client` is the sequencer's side of it.
* :mod:`robofast_core.heartbeat_watchdog` -- the enclosure hardware
  heartbeat contract, and a watchdog that adds one to a simulator's dome.
* :mod:`robofast_core.weather_server` -- a small Alpaca SafetyMonitor +
  ObservingConditions server, with fault injection for tests.
* :mod:`robofast_core.weather_bridge` -- the site weather bridge: polls the
  site's weather sources, a Redis server and web pages
  (:mod:`robofast_core.weather_sources`, site adapters in
  :mod:`robofast_core.sites`), judges them
  (:mod:`robofast_core.weather_eval`) and publishes to the weather server;
  also a shadow mode that only logs (``docs/weather_bridge.md``).
"""

from robofast_core.owner import (
    DeviceError,
    DeviceOwner,
    DeviceTimeout,
    Grant,
    GrantRevoked,
    NotAuthorized,
)
from robofast_core.safety import OpenLease, SafetyConfig, SafetySupervisor
from robofast_core.sequencer import Observation, Sequencer, SequencerConfig
from robofast_core.timeline import Timeline

__all__ = [
    "DeviceError",
    "DeviceOwner",
    "DeviceTimeout",
    "Grant",
    "GrantRevoked",
    "NotAuthorized",
    "Observation",
    "OpenLease",
    "SafetyConfig",
    "SafetySupervisor",
    "Sequencer",
    "SequencerConfig",
    "Timeline",
]
