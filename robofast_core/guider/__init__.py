"""robofast's guider: a generic guide-camera service (docs/guider.md).

It runs on the computer the guide camera is plugged into, so the loop never
sends a frame over the network: centroids are computed next to the camera
and only corrections and low-rate telemetry leave it.

* :mod:`~robofast_core.guider.base` -- the typed parts (Camera, Centroider,
  TipTilt), frames, clocks and the hardware gate;
* :mod:`~robofast_core.guider.camera` -- Alpaca (slow path), ZWO ASI
  (optional ``zwoasi``), a frame-grabber hook;
* :mod:`~robofast_core.guider.centroid` -- windowed centre of mass,
  three-point peak, Gaussian fit, the fiber-hole (annulus) centroider;
* :mod:`~robofast_core.guider.controller` -- the PI(D) controller;
* :mod:`~robofast_core.guider.tiptilt` -- Starlight Xpress AO, PI E-518
  (optional ``pipython``), mount offsets, and their simulators;
* :mod:`~robofast_core.guider.calibration` -- the measured 2x2 matrix per
  device and port;
* :mod:`~robofast_core.guider.offload` -- tip/tilt to mount offloading;
* :mod:`~robofast_core.guider.loop` -- acquisition onto the fiber, the
  guide loop, telemetry, alarms;
* :mod:`~robofast_core.guider.sim` -- the simulated sky, tip/tilt and mount;
* :mod:`~robofast_core.guider.redis_pub` -- optional tres-guider-style
  ``tracking_data`` publisher (publish only);
* :mod:`~robofast_core.guider.service` -- the HTTP service and
  :class:`~robofast_core.guider.service.GuiderRemote`, the sequencer's
  client.
"""
