"""The guider's parts: star images, centroiders, the controller, calibration,
the tip/tilt drivers and their simulators, and the hardware gate."""

from __future__ import annotations

import asyncio
import json
import math

import numpy as np
import pytest

from robofast_core.guider.base import (
    ROI,
    Frame,
    HardwareNotAllowed,
    MoveResult,
    TipTiltError,
    VirtualClock,
    check_address,
    range_fraction,
)
from robofast_core.guider.calibration import (
    Calibration,
    CalibrationStore,
    fit_matrix,
    from_sx_legacy,
    from_tres_legacy,
    measure,
    sky_matrix,
)
from robofast_core.guider.camera import AlpacaCamera, FrameGrabberCamera, ZWOCamera
from robofast_core.guider.centroid import (
    AnnulusCentroider,
    GaussianFit,
    QuadraticPeak,
    WindowedCOM,
    find_stars,
    make_centroider,
)
from robofast_core.guider.controller import Controller, Gains
from robofast_core.guider.sim import (
    Fiber,
    Jitter,
    SimCamera,
    SimMount,
    SimTipTilt,
    SimWorld,
    render_window,
)
from robofast_core.guider.tiptilt import (
    PIGCS,
    POS_OUT_OF_LIMITS,
    SXAO,
    GCSError,
    MountOffset,
    SerialTransport,
    SimGCS,
    SXAOSim,
    alpaca_telescope,
    open_pi_gcs,
)
from robofast_core.testing.fakes import FakeCamera

ROI32 = ROI(100, 200, 32, 32)


def _frame(x, y, *, flux=2e5, fwhm=4.0, fiber=None, rng=None, roi=ROI32, noise=True):
    data = render_window(
        roi,
        [(x, y, flux)],
        fwhm=fwhm,
        sky=20.0,
        fiber=fiber,
        rng=rng or np.random.default_rng(1),
        noise=noise,
    )
    return Frame(data, roi, 0.01, 0.0, 0.01, gain=1.0)


# -- star images ---------------------------------------------------------------------


def test_render_puts_the_flux_where_the_star_is():
    f = _frame(117.3, 214.6, noise=False)
    img = f.data.astype(float) - 100 - 20  # bias + sky
    rows, cols = np.mgrid[0:32, 0:32]
    assert img.sum() == pytest.approx(2e5, rel=0.01)
    assert (img * cols).sum() / img.sum() + 100 == pytest.approx(117.3, abs=0.02)
    assert (img * rows).sum() / img.sum() + 200 == pytest.approx(214.6, abs=0.02)


def test_fiber_hole_and_backlight():
    roi = ROI(0, 0, 40, 40)
    hole = Fiber(20.0, 20.0, radius=3.0, backlight=500.0)
    d = render_window(roi, [(20.0, 20.0, 1e5)], sky=10.0, fiber=hole, noise=False)
    assert d[20, 20] == 110  # bias + sky: the star's core falls into the fiber
    lit = render_window(roi, [], sky=10.0, fiber=hole, backlit=True, noise=False)
    assert lit[20, 20] == 610 and lit[0, 0] == 110


def test_jitter_has_the_requested_rms_and_mean_is_the_average():
    j = Jitter(rms=0.7, knee=2.0, duration=32.0, seed=4)
    xs = np.array([j.at(t) for t in np.arange(0, 32, 0.002)])
    assert xs.std(axis=0) == pytest.approx([0.7, 0.7], rel=0.05)
    t0, t1 = 3.0, 3.25
    brute = np.mean([j.at(t) for t in np.linspace(t0, t1, 2001)], axis=0)
    np.testing.assert_allclose(j.mean(t0, t1), brute, atol=2e-3)


def test_wind_shake_and_drift():
    j = Jitter(rms=0.0, shake=[(10.0, 1.0)], drift=(0.1, -0.2), seed=0)
    amp = max(np.hypot(*(j.at(t) - np.array([0.1, -0.2]) * t)) for t in np.arange(0, 0.2, 1e-3))
    assert amp == pytest.approx(1.0, rel=0.01)
    # A whole period of shake averages out, leaving the drift.
    np.testing.assert_allclose(j.mean(1.0, 1.1), [0.105, -0.21], atol=1e-6)


# -- centroiders ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("centroider", "tol"),
    [(WindowedCOM(), 0.02), (QuadraticPeak(), 0.1), (GaussianFit(), 0.02)],
)
def test_centroiders_find_a_bright_star(centroider, tol):
    rng = np.random.default_rng(7)
    errs = []
    for _ in range(40):
        x, y = 116 + rng.uniform(-3, 3), 216 + rng.uniform(-3, 3)
        m = centroider.measure(_frame(x, y, rng=rng), None)
        assert m.valid
        errs.append((m.x - x, m.y - y))
    e = np.array(errs)
    assert np.abs(e.mean(axis=0)).max() < tol
    assert e.std(axis=0).max() < 2 * tol
    assert m.fwhm == pytest.approx(4.0, rel=0.15)
    assert m.snr > 100


def test_centroiders_report_no_star_on_blank_sky():
    rng = np.random.default_rng(3)
    data = render_window(ROI32, [], sky=20.0, rng=rng)
    frame = Frame(data, ROI32, 0.01, 0, 0.01)
    for c in (WindowedCOM(), QuadraticPeak(), GaussianFit(), AnnulusCentroider(116, 216)):
        m = c.measure(frame, None)
        assert not m.valid and "no star" in m.reason


def test_annulus_centroider_is_unbiased_on_the_fiber_hole():
    rng = np.random.default_rng(11)
    hole = Fiber(116, 216, 3.0)
    plain, ann, fast = [], [], []
    for _ in range(60):
        x, y = 116 + rng.uniform(-2.5, 2.5), 216 + rng.uniform(-2.5, 2.5)
        f = _frame(x, y, fiber=hole, rng=rng)
        m = WindowedCOM().measure(f, None)
        plain.append(math.hypot(m.x - x, m.y - y))
        m = AnnulusCentroider(116, 216, 3.0).measure(f, None)
        ann.append(math.hypot(m.x - x, m.y - y))
        m = AnnulusCentroider(116, 216, 3.0, fit=False).measure(f, None)
        fast.append(math.hypot(m.x - x, m.y - y))
    # The hole drags a plain centre of mass by up to a pixel or more.
    assert np.mean(plain) > 0.4
    assert np.mean(ann) < 0.05
    assert np.mean(fast) < 0.3


def test_find_stars_brightest_first():
    data = render_window(
        ROI(0, 0, 120, 100),
        [(30, 40, 1e5), (80, 70, 4e5), (10, 90, 5e4)],
        fwhm=3.0,
        sky=20.0,
        rng=np.random.default_rng(0),
    )
    stars = find_stars(data)
    assert [(round(x), round(y)) for x, y, _ in stars[:3]] == [(80, 70), (30, 40), (10, 90)]


def test_make_centroider():
    assert make_centroider({"kind": "quadratic"}).name == "quadratic"
    a = make_centroider({"kind": "annulus"}, (1.0, 2.0, 4.0))
    assert (a.hole_x, a.hole_y, a.hole_radius) == (1.0, 2.0, 4.0)
    with pytest.raises(ValueError):
        make_centroider({"kind": "annulus"})
    with pytest.raises(ValueError):
        make_centroider({"kind": "sextractor"})


# -- the controller -----------------------------------------------------------------


def _plant_loop(ctrl: Controller, disturbance, n: int, dt: float = 0.01):
    """An integrating actuator: the star is at disturbance(t) + actuator;
    each frame the controller's output is added to the actuator (one frame
    of delay, as a real loop has)."""
    act = np.zeros(2)
    errs = []
    for k in range(n):
        star = np.asarray(disturbance(k * dt)) + act
        errs.append(star - ctrl.target)
        act = act + ctrl.update(star, dt)
    return np.array(errs)


def test_controller_removes_a_step():
    c = Controller(Gains(kp=0.5))
    c.set_target(0.0, 0.0)
    e = _plant_loop(c, lambda t: (3.0, -2.0), 40)
    assert np.hypot(*e[0]) == pytest.approx(math.hypot(3, 2))
    assert np.abs(e[-1]).max() < 1e-3


def test_integral_term_removes_a_ramp_that_p_alone_lags():
    ramp = lambda t: (5.0 * t, -2.0 * t)  # noqa: E731 - pixels per second
    p_only = _plant_loop(Controller(Gains(kp=0.4)), ramp, 300)
    pi = _plant_loop(Controller(Gains(kp=0.4, ki=0.05, i_max=50)), ramp, 300)
    assert np.abs(p_only[-1]).max() > 0.05  # a steady lag
    assert np.abs(pi[-1]).max() < 1e-3


def test_sinusoid_rejection_falls_with_frequency():
    def rejection(freq):
        c = Controller(Gains(kp=0.5))
        e = _plant_loop(c, lambda t: (math.sin(2 * math.pi * freq * t), 0.0), 2000, dt=0.005)
        return e[500:, 0].std() / (1 / math.sqrt(2))

    slow, fast = rejection(1.0), rejection(20.0)
    assert slow < 0.1  # 1 Hz at 200 Hz frames: strongly rejected
    assert slow < fast


def test_changing_the_setpoint_keeps_the_integrator():
    """The legacy bug: setPoint() zeroed I and D every frame (P-only)."""
    c = Controller(Gains(kp=0.3, ki=0.1, kd=0.2, i_max=100))
    c.set_target(10.0, 10.0)
    for _ in range(5):
        c.update((8.0, 9.0), 0.01)
    integ = c.integrator.copy()
    last = c.last_measurement.copy()
    assert np.abs(integ).sum() > 0
    c.set_target(10.5, 10.0)
    np.testing.assert_array_equal(c.integrator, integ)
    np.testing.assert_array_equal(c.last_measurement, last)
    # Derivative on the measurement: a setpoint step adds only P and I.
    out = c.update(last, 0.01)
    err = np.array([2.5, 1.0])
    np.testing.assert_allclose(out, 0.3 * err + 0.1 * (integ + err))
    c.reset()
    assert not c.integrator.any() and c.last_measurement is None


def test_deadband_and_limits():
    c = Controller(Gains(kp=1.0, deadband=0.5))
    assert not c.update((0.3, -0.4), 0.01).any()
    np.testing.assert_allclose(c.update((-2.0, 0.0), 0.01), [1.5, 0.0])
    c = Controller(Gains(kp=1.0, max_step=1.0))
    out = c.update((-3.0, -4.0), 0.01)
    assert np.hypot(*out) == pytest.approx(1.0)
    c = Controller(Gains(kp=1.0, max_rate=50.0))  # 0.5 px in a 0.01 s frame
    assert np.hypot(*c.update((-3.0, -4.0), 0.01)) == pytest.approx(0.5)


def test_anti_windup_stops_integrating_into_a_limit():
    c = Controller(Gains(kp=0.2, ki=0.2, i_max=1000, max_step=0.1))
    for _ in range(50):
        c.update((-5.0, 0.0), 0.01)  # clipped every frame
    assert abs(c.integrator[0]) <= 5.0 + 1e-9  # it stopped growing at once
    c2 = Controller(Gains(kp=0.2, ki=0.2, i_max=1000))
    for _ in range(50):
        c2.update((-5.0, 0.0), 0.01)
        c2.saturated((1.0, 0.0))  # the actuator says it is at its limit
    assert abs(c2.integrator[0]) <= 5.0 + 1e-9


def test_gains_validation():
    with pytest.raises(ValueError):
        Gains.from_dict({"kp": -1})
    with pytest.raises(ValueError):
        Gains.from_dict({"kp": 3})
    with pytest.raises(ValueError):
        Gains.from_dict({"kq": 1})
    c = Controller(Gains(kp=0.5, ki=0.2))
    c.integrator = np.array([1.0, -1.0])
    c.set_gains(Gains(kp=0.5, ki=0.1))
    np.testing.assert_allclose(c.integrator, [2.0, -2.0])  # same contribution


# -- calibration ---------------------------------------------------------------------


def test_fit_matrix_recovers_rotation_scale_and_flip():
    m_true = np.array([[0.05, 0.08], [0.09, -0.04]])  # det < 0: a flip
    pos = [(0, 0), (10, 0), (-10, 0), (0, 10), (0, -10), (7, 7)]
    pix = [tuple(np.array([300.0, 200.0]) + m_true @ p) for p in pos]
    m, p0, _rms = fit_matrix(pos, pix)
    np.testing.assert_allclose(m, m_true, atol=1e-9)
    np.testing.assert_allclose(p0, [300, 200])
    cal = Calibration("tt", "fau", m.tolist())
    assert cal.flip
    np.testing.assert_allclose(cal.to_pixels(cal.to_device([1.0, -2.0])), [1.0, -2.0])


def test_sky_matrix_conventions():
    s = sky_matrix(0.5, 0.0)
    np.testing.assert_allclose(s @ [0, 1], [0, 0.5])  # +y is north
    np.testing.assert_allclose(s @ [1, 0], [-0.5, 0])  # +x is west
    s90 = sky_matrix(1.0, 90.0)
    np.testing.assert_allclose(s90 @ [0, 1], [1, 0], atol=1e-12)  # +y east at PA 90
    np.testing.assert_allclose(sky_matrix(1.0, 0.0, -1) @ [1, 0], [1, 0])


def test_legacy_conversions_are_consistent():
    # tres-guider: STEPS_PER_PIXEL 50, THETA 90, FLIP: its move_x_y(x, y)
    cal = from_tres_legacy(50.0, 50.0, 90.0, True)
    t = math.radians(90.0)
    for x, y in ((1.0, 0.0), (0.0, 2.0)):
        tip = -50.0 * (x * math.cos(t) - y * math.sin(t))
        tilt = 50.0 * (x * math.sin(t) + y * math.cos(t))
        np.testing.assert_allclose(cal.to_device([x, y]), [tip, tilt], atol=1e-9)
    sx = from_sx_legacy(0.128, 214.7, plate_scale=0.5)
    sky = sx.sky()
    r = math.radians(214.7)
    east, north = 1.0, 0.0
    steps = [
        -(east * math.cos(r) - north * math.sin(r)) / 0.128,
        (east * math.sin(r) + north * math.cos(r)) / 0.128,
    ]
    np.testing.assert_allclose(sky @ steps, [east, north], atol=1e-9)


def test_calibration_store_round_trip(tmp_path):
    store = CalibrationStore(tmp_path / "cal")
    assert store.load("sxao", "fau") is None
    cal = Calibration("sxao", "fau", [[0.1, 0.0], [0.0, -0.1]], units="steps", residual=0.02)
    path = store.save(cal)
    assert path.name == "sxao__fau.json"
    back = store.load("sxao", "fau")
    assert back.matrix == cal.matrix and back.units == "steps" and back.flip
    assert json.loads(path.read_text())["describe"]["flip"] is True
    path.write_text(json.dumps({"device": "x", "port": "", "matrix": [[0, 0], [0, 0]]}))
    with pytest.raises(ValueError):
        store.load("sxao", "fau")


async def test_measure_calibration_in_the_simulator():
    clock = VirtualClock()
    tt = SimTipTilt(clock=clock)
    m_true = np.array([[0.06, -0.05], [0.05, 0.06]])
    world = SimWorld(
        clock=clock,
        star=(320.0, 240.0),
        tiptilt=tt,
        tt_matrix=m_true,
        jitter=Jitter(rms=0.2, seed=2),
    )
    cam = SimCamera(world)
    cal = await measure(
        cam,
        WindowedCOM(),
        tt,
        step=60,
        exposure=0.2,
        frames=5,
        roi=ROI(280, 200, 80, 80),
        clock=clock,
    )
    np.testing.assert_allclose(cal.m, m_true, atol=0.004)
    assert cal.positions == 9 and cal.residual < 0.3 and tt.position == tt.centre


# -- tip/tilt devices ---------------------------------------------------------------


async def test_sxao_moves_and_tallies():
    sim = SXAOSim(travel=1000)
    sx = SXAO(sim, limits=((-100, 100), (-100, 100)))
    await sx.open()
    assert sim.commands == ["K"]
    r = await sx.move_by(12.4, -30.6)
    assert sim.commands[1:] == ["GN00012", "GW00031"]
    assert r.position == (12.0, -31.0) == tuple(sim.position) and not r.at_limit
    # Clipped to the software range *before* anything is sent; the tally
    # is what was acknowledged.
    r = await sx.move_by(150.0, 0.0)
    assert sim.commands[-1] == "GN00088" and r.at_limit and r.position == (100.0, -31.0)
    assert r.clipped == pytest.approx((62.0, 0.0), abs=0.6)
    assert range_fraction(sx) == pytest.approx(1.0)
    await sx.home()
    assert sx.position == (0.0, 0.0) and sim.position == [0, 0]


async def test_sxao_limit_reply_marks_the_position_uncertain():
    sim = SXAOSim(travel=50)
    sx = SXAO(sim, limits=((-500, 500), (-500, 500)))
    await sx.open()
    r = await sx.move_by(80, 0)
    assert r.at_limit and sx.uncertain
    assert sx.position == (0.0, 0.0)  # an L reply is not counted as moved
    await sx.home()
    assert not sx.uncertain


async def test_sxao_silent_unit_times_out_instead_of_hanging():
    clock = VirtualClock()
    sim = SXAOSim(clock=clock)
    sx = SXAO(sim, timeout=0.3)
    sx.clock = clock
    await sx.open()
    sim.silent = True
    with pytest.raises(TipTiltError, match="no reply"):
        await sx.move_by(5, 0)
    assert sx.uncertain and sx.position == (0.0, 0.0)


async def test_sxao_terminator_is_configurable():
    sim = SXAOSim(terminator=b"\r\n")
    sx = SXAO(sim, terminator=b"\r\n")
    await sx.open()
    await sx.move_by(0, 3)
    assert sim.commands == ["K", "GT00003"]


async def test_pi_gcs_driver_on_the_simulator():
    clock = VirtualClock()
    gcs = SimGCS(clock=clock, tau=1e-3)
    with pytest.raises(GCSError):
        gcs.qTMN()  # not connected
    gcs.ConnectUSB("123")
    pi = PIGCS(gcs, margin=0.05)
    pi.clock = clock
    await pi.open()
    assert gcs.servo == {"A": True, "B": True}
    assert pi.centre == (1.0, 1.0) and pi.position == (1.0, 1.0)
    assert pi.limits == ((0.05, 1.95), (0.05, 1.95))
    r = await pi.move_by(0.3, -0.2)
    assert r.position == pytest.approx((1.3, 0.8)) and gcs.target == pytest.approx(
        {"A": 1.3, "B": 0.8}
    )
    await clock.sleep(0.01)
    assert gcs.qPOS()["A"] == pytest.approx(1.3, abs=1e-4)
    r = await pi.move_by(5.0, 0.0)
    assert r.at_limit and r.position[0] == pytest.approx(1.95)
    with pytest.raises(GCSError) as exc:
        gcs.MOV("A", 2.5)
    assert exc.value.val == POS_OUT_OF_LIMITS


async def test_pi_gcs_errors_become_tiptilt_errors():
    gcs = SimGCS()
    gcs.ConnectUSB("1")
    pi = PIGCS(gcs)
    await pi.open()
    gcs.SVO("A", False)
    with pytest.raises(TipTiltError, match="servo off"):
        await pi.move_by(0.1, 0.0)


async def test_mount_offset_action_and_pulseguide():
    mount = SimMount(guide_rate=5.0)
    off = MountOffset(mount, method="offset", max_offset=10.0)
    r = await off.move_by(1.5, -2.25)
    assert mount.calls[-1] == ("Action", ("Offset", json.dumps({"east": 1.5, "north": -2.25})))
    assert r.position == (1.5, -2.25)
    r = await off.move_by(20.0, 0.0)
    assert r.at_limit and r.clipped == (10.0, 0.0)
    pg = MountOffset(mount, method="pulseguide", guide_rate=5.0)
    pg.dec_deg = 60.0
    mount.calls.clear()
    await pg.move_by(-1.0, 2.0)
    # North 2" at 5"/s = 400 ms; west 1" at cos(60) = 400 ms.
    assert mount.calls == [("PulseGuide", (0, 400)), ("PulseGuide", (3, 400))]
    mount.refuse = "parked"
    with pytest.raises(TipTiltError, match="parked"):
        await pg.move_by(1.0, 0.0)


async def test_alpaca_camera_backend_on_a_fake_camera():
    cam = FakeCamera(numx=64, numy=48)
    ac = AlpacaCamera(cam, poll=0.001)
    await ac.open()
    assert ac.sensor == (64, 48)
    frame = await ac.expose(0.01, ROI(8, 4, 32, 16), gain=5)
    assert frame.data.shape == (16, 32) and frame.roi == ROI(8, 4, 32, 16)
    assert (cam.StartX, cam.StartY, cam.NumX, cam.NumY, cam.Gain) == (8, 4, 32, 16, 5)
    assert frame.t_end - frame.t_start == pytest.approx(0.01, abs=0.01)


# -- the hardware gate -------------------------------------------------------------------


def test_real_backends_are_refused_without_the_hardware_gate():
    with pytest.raises(HardwareNotAllowed):
        SerialTransport("/dev/ttyUSB0")
    with pytest.raises(HardwareNotAllowed):
        open_pi_gcs(usb_serial="0123")
    with pytest.raises(HardwareNotAllowed):
        ZWOCamera()
    with pytest.raises(HardwareNotAllowed):
        FrameGrabberCamera()
    with pytest.raises(HardwareNotAllowed):
        alpaca_telescope("192.0.2.10:11111")
    with pytest.raises(HardwareNotAllowed):
        AlpacaCamera.at("192.0.2.10:11111")
    check_address("127.0.0.1:11111", "x", allowed=False)  # loopback: a simulator


async def test_frame_grabber_is_only_a_hook():
    fg = FrameGrabberCamera(allow_hardware=True)
    with pytest.raises(NotImplementedError):
        await fg.open()


def test_move_result_defaults():
    r = MoveResult((1.0, 2.0))
    assert not r.at_limit and r.clipped == (0.0, 0.0)


async def test_virtual_clock_orders_concurrent_sleepers():
    clock = VirtualClock()
    seen = []

    async def sleeper(name, dt, n):
        for _ in range(n):
            await clock.sleep(dt)
            seen.append((round(clock.now(), 6), name))

    await asyncio.gather(sleeper("a", 0.3, 3), sleeper("b", 0.5, 2))
    assert seen == [(0.3, "a"), (0.5, "b"), (0.6, "a"), (0.9, "a"), (1.0, "b")]
