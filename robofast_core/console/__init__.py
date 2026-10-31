"""The observer console (docs/console.md): one web page for the observer on
site (the FLWO 60-inch's attended automation) and for the people watching a
robotic night (MINERVA).

It shows, live, the weather, the enclosure, each telescope, the
spectrograph, the night's plan record, the alarms and the timeline, all read
from the services' own status APIs and files
(:mod:`robofast_core.console.aggregate`); and it lets an observer with an
``operator`` token acknowledge alarms, pause, resume or stop a telescope or
the whole enclosure, clear a dead lease or the enclosure's faults, and
submit or cancel requests and ToOs through the request API
(:mod:`robofast_core.console.server`). Pause, resume and stop reach the
sequencer only through a command directory
(:mod:`robofast_core.console.hold`).

It can never open an enclosure or get around the safety supervisor: it can
ask the supervisor to close (revoke the lease), and opening happens only by
the supervisor's own policy, when a sequencer holds a lease.

Run it with ``python -m robofast_core.console serve --config console.json``.
"""
