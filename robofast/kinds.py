"""Observation kinds: one handler per kind, in a registry.

An observation's ``kind`` (:attr:`robofast.plan.Observation.kind`)
says what it does on its target. Each kind is one :class:`ObservationKind`
in :data:`OBSERVATION_KINDS`, which owns everything about it that used to be
spread across the plan, the request model, the dispatch scheduler and the
sequencer (docs/design.md 3.10):

* **validation** of an :class:`~robofast.plan.Observation` of its kind
  (:meth:`ObservationKind.validate`; the fields only it may set,
  :attr:`~ObservationKind.fields`);
* the **request mapping**: the OCS configuration types it serves
  (:attr:`~ObservationKind.request_types`), their checks
  (:meth:`~ObservationKind.check_configuration`), and the observation the
  dispatch scheduler makes from one instrument config
  (:meth:`~ObservationKind.observation`);
* its **execution** in the sequencer (:meth:`~ObservationKind.run`, at the
  point of the sequencer's set-up named by :attr:`~ObservationKind.stage`).

The built-in kinds below are registered the way a plug-in registers its own
(docs/plugins.md): ``OBSERVATION_KINDS.register(kind.name, kind)``. A kind's
:meth:`~ObservationKind.run` drives the sequencer through its methods (the
built-ins call the sequencer's own operations; a plug-in's kind can do the
same, or take frames with :meth:`KindRun.expose_frames`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from robofast.plugins import Registry

if TYPE_CHECKING:  # pragma: no cover
    from robofast.plan import Observation
    from robofast.request_model import Configuration, InstrumentConfig

__all__ = [
    "LAMP_TYPES",
    "OBSERVATION_KINDS",
    "KindRun",
    "ObservationKind",
    "RequestMapping",
    "kind_for_request",
    "request_types",
]

#: OCS configuration types for spectrograph lamp calibrations (kinds
#: ``arc`` and ``lamp_flat``; docs/pulizzi.md).
LAMP_TYPES = ("ARC", "LAMP_FLAT")


@dataclass
class KindRun:
    """What the sequencer hands a kind's :meth:`ObservationKind.run`: itself,
    the generation, the telescope and its grants, the observation, the list
    its frames are appended to, and (once on target) the target's
    coordinates."""

    sequencer: Any
    state: Any
    unit: Any
    tel: Any
    cam: Any
    obs: Observation
    taken: list[Any]
    ra: float | None = None
    dec: float | None = None

    async def expose_frames(self) -> None:
        """``obs.count`` exposures where the telescope points now."""
        await self.sequencer._expose_frames(
            self.state, self.unit, self.tel, self.cam, self.obs, self.taken
        )


@dataclass
class RequestMapping:
    """One instrument config of a configuration, as the dispatch scheduler
    hands it to :meth:`ObservationKind.observation`."""

    config: Configuration
    #: The instrument config's index and the frames of it already taken.
    index: int
    done: int
    #: When the visit started (spectrum ids).
    started: datetime
    #: The target's acquisition entry (``{"acquisition": {...}}`` or empty).
    acquisition: dict[str, Any] = field(default_factory=dict)
    #: The instrument's parameters for each frame, as the instrument takes
    #: them (:meth:`robofast.instruments.InstrumentType.frame_params`).
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def ic(self) -> InstrumentConfig:
        return self.config.instrument_configs[self.index]

    @property
    def suffix(self) -> str:
        return f"+{self.done}" if self.done else ""

    def name(self, label: str | None = None, partial: bool = True) -> str:
        """``label [config#index(+done)]`` (label: the target's name)."""
        c = self.config
        sfx = self.suffix if partial else ""
        return f"{c.target.name if label is None else label} [{c.id}#{self.index}{sfx}]"


class ObservationKind:
    """One kind of observation (module doc). Subclass it and register an
    instance in :data:`OBSERVATION_KINDS`."""

    #: The kind's name (:attr:`Observation.kind`).
    name: ClassVar[str] = ""
    #: OCS configuration types it serves (none: plans only, not requests).
    request_types: ClassVar[tuple[str, ...]] = ()
    #: An observation of this kind needs sky light (True), takes none
    #: (False), or either (None). As an :class:`Observation` field ``light``.
    light: ClassVar[bool | None] = None
    #: :class:`Observation` fields only this kind may set.
    fields: ClassVar[tuple[str, ...]] = ()
    #: Where in the sequencer's set-up :meth:`run` is called:
    #: ``"before_setup"`` (nothing done yet: manual control),
    #: ``"before_slew"`` (port, filter, focus and tracking set, the enclosure
    #: open, not yet at the target: a kind that chooses its own pointings),
    #: or ``"on_target"`` (slewed, the ADC tracking and guiding started if
    #: asked for; without light, after the set-up only).
    stage: ClassVar[str] = "on_target"
    #: It uses the enclosure's spectrograph: refused at once while the
    #: spectrograph is marked down, and one at a time per enclosure.
    spectrograph: ClassVar[bool] = False
    #: Locate the fiber with the backlight before guiding.
    fiber: ClassVar[bool] = False
    #: The camera shutter opens although no sky light is wanted (a lamp).
    shutter: ClassVar[bool] = False
    #: Its request types are always guided.
    always_guided: ClassVar[bool] = False
    #: It takes frames with the telescope's science camera: True (refused
    #: while that camera is disabled, and not scheduled on that telescope:
    #: robofast/sequencer.py, "A camera that cannot reach its setpoint"),
    #: False (it never does: spectra, through the guide camera and the
    #: spectrograph), or None (it depends: each exposure is checked).
    camera: ClassVar[bool | None] = True

    # -- validation -------------------------------------------------------------------

    def validate(self, obs: Observation) -> None:
        """Check (and normalise) an observation of this kind; ValueError."""
        if self.light is True and not obs.light:
            raise ValueError(f"observation {obs.name!r}: a {self.name} needs light")

    # -- requests ---------------------------------------------------------------------

    def frame_light(self, ctype: str) -> bool | None:
        """Whether a frame of configuration type ``ctype`` is light (True),
        dark (False), or either, as its instrument config says (None)."""
        return self.light

    def check_instrument_config(
        self, ctype: str, i: int, ic: InstrumentConfig, raw: dict[str, Any], what: str
    ) -> None:
        """Check instrument config ``i`` of configuration ``what`` (``raw``:
        as given); set its ``light`` where the type decides it."""
        at = f"{what}.instrument_configs[{i}]"
        if ic.sequence is not None:
            raise ValueError(
                f"{at}.extra_params.sequence: only for {' and '.join(LAMP_TYPES)} configurations"
            )
        if ic.lamp is not None:
            raise ValueError(
                f"{at}.optical_elements.lamp: only for {' and '.join(LAMP_TYPES)} configurations"
            )
        want = self.frame_light(ctype)
        if want is False:
            if ic.light and "light" in (raw.get("extra_params") or {}):
                raise ValueError(f"{at}: a {ctype} is not light")
            ic.light = False
        elif want is True and not ic.light:
            raise ValueError(f"{at}: a {ctype} frame is light")

    def check_configuration(
        self, ctype: str, ics: list[InstrumentConfig], guide: bool, gmode: str, what: str
    ) -> bool:
        """Check the configuration as a whole; returns whether it is guided."""
        if self.always_guided:
            if gmode == "OFF":
                raise ValueError(f"{what}: a {ctype} is always guided")
            return True
        return guide

    def observation(self, m: RequestMapping) -> dict[str, Any]:
        """The :class:`Observation` (its fields) for one instrument config."""
        raise NotImplementedError(f"kind {self.name!r} serves no requests")

    # -- execution --------------------------------------------------------------------

    async def run(self, r: KindRun) -> None:
        """Do the observation (sequencer, module doc)."""
        await r.expose_frames()


#: Every observation kind, by name.
OBSERVATION_KINDS: Registry[ObservationKind] = Registry("observation kind")


def kind_for_request(ctype: str) -> ObservationKind | None:
    """The kind that serves OCS configuration type ``ctype`` (None: none)."""
    for kind in OBSERVATION_KINDS.values():
        if ctype in kind.request_types:
            return kind
    return None


def request_types() -> tuple[str, ...]:
    """Every OCS configuration type some kind serves."""
    return tuple(t for kind in OBSERVATION_KINDS.values() for t in kind.request_types)


# -- the built-in kinds ---------------------------------------------------------------------


class Expose(ObservationKind):
    """``count`` frames where the telescope points (OCS EXPOSE, DARK, BIAS)."""

    name = "expose"
    request_types = ("EXPOSE", "DARK", "BIAS")

    def frame_light(self, ctype: str) -> bool | None:
        return False if ctype in ("DARK", "BIAS") else None

    def observation(self, m: RequestMapping) -> dict[str, Any]:
        c, ic = m.config, m.ic
        return dict(
            name=m.name(),
            ra_hours=c.target.ra_hours if ic.light else None,
            dec_deg=c.target.dec_deg if ic.light else None,
            exptime=ic.exptime,
            count=ic.count - m.done,
            light=ic.light,
            filter=ic.filter,
            port=ic.port,
            calstage=ic.calstage,
            allow_partial=c.allow_partial,
            object=c.target.name,
            guide=c.guide and ic.light,
        )


class Manual(ObservationKind):
    """The operator's manual control (console): the sequencer holds the
    telescope and carries out the operator's commands."""

    name = "manual"
    stage = "before_setup"
    #: The operator's: each exposure it takes is checked.
    camera = None

    def validate(self, obs: Observation) -> None:
        if not obs.light:
            raise ValueError(f"observation {obs.name!r}: manual control needs the lease")

    async def run(self, r: KindRun) -> None:
        await r.sequencer._manual(r.state, r.unit, r.tel, r.cam, r.obs, r.taken)


class _OneFrame(ObservationKind):
    """A light kind with exactly one instrument config."""

    light = True

    def check_configuration(
        self, ctype: str, ics: list[InstrumentConfig], guide: bool, gmode: str, what: str
    ) -> bool:
        if len(ics) != 1:
            raise ValueError(f"{what}: an {ctype} needs exactly one light instrument config")
        return guide


class Autofocus(_OneFrame):
    """Focus the telescope on the target (OCS AUTO_FOCUS; docs/autofocus.md)."""

    name = "autofocus"
    request_types = ("AUTO_FOCUS",)
    fields = ("autofocus",)

    def observation(self, m: RequestMapping) -> dict[str, Any]:
        c, ic = m.config, m.ic
        return dict(
            name=m.name(partial=False),
            ra_hours=c.target.ra_hours,
            dec_deg=c.target.dec_deg,
            exptime=ic.exptime,
            count=1,
            filter=ic.filter,
            port=ic.port,
            object=c.target.name,
            kind=self.name,
            autofocus={"exptime": ic.exptime} if ic.exptime > 0 else None,
        )

    async def run(self, r: KindRun) -> None:
        await r.sequencer._autofocus(r.state, r.unit, r.tel, r.cam, r.obs, r.taken)


class PointingModel(_OneFrame):
    """A pointing run (robofast's POINTING_MODEL; docs/pointing_model.md)."""

    name = "pointing_model"
    request_types = ("POINTING_MODEL",)
    fields = ("pointing",)
    stage = "before_slew"

    def observation(self, m: RequestMapping) -> dict[str, Any]:
        c, ic = m.config, m.ic
        return dict(
            name=m.name(partial=False),
            ra_hours=c.target.ra_hours,
            dec_deg=c.target.dec_deg,
            exptime=ic.exptime,
            count=ic.count,
            filter=ic.filter,
            port=ic.port,
            object=c.target.name,
            kind=self.name,
        )

    async def run(self, r: KindRun) -> None:
        await r.sequencer._pointing_run(r.state, r.unit, r.tel, r.cam, r.obs, r.taken)


class RotatorCalibration(_OneFrame):
    """The port's PA and parity (robofast's ROTATOR_CALIBRATION; docs/rotcal.md)."""

    name = "rotator_calibration"
    request_types = ("ROTATOR_CALIBRATION",)
    fields = ("rotator",)

    def observation(self, m: RequestMapping) -> dict[str, Any]:
        c, ic = m.config, m.ic
        return dict(
            name=m.name(partial=False),
            ra_hours=c.target.ra_hours,
            dec_deg=c.target.dec_deg,
            exptime=ic.exptime,
            count=1,
            filter=ic.filter,
            port=ic.port,
            object=c.target.name,
            kind=self.name,
        )

    async def run(self, r: KindRun) -> None:
        assert r.ra is not None and r.dec is not None
        await r.sequencer._rotator_calibration(
            r.state, r.unit, r.tel, r.cam, r.obs, r.taken, r.ra, r.dec
        )


class SkyFlat(ObservationKind):
    """Twilight flats opposite the Sun (robofast's SKY_FLAT; docs/skyflats.md)."""

    name = "sky_flat"
    request_types = ("SKY_FLAT",)
    light = True
    fields = ("flats",)
    stage = "before_slew"

    def observation(self, m: RequestMapping) -> dict[str, Any]:
        c, ic = m.config, m.ic
        ics = c.instrument_configs
        return dict(
            name=m.name(partial=False),
            exptime=ic.exptime,
            count=sum(i.count for i in ics),
            object="skyflat",
            kind=self.name,
            flats={
                "filters": [i.filter for i in ics if i.filter],
                "count": max(i.count for i in ics),
            }
            if any(i.filter for i in ics)
            else {"count": max(i.count for i in ics)},
        )

    async def run(self, r: KindRun) -> None:
        await r.sequencer._sky_flats(r.state, r.unit, r.tel, r.cam, r.obs, r.taken)


#: Keys of an observation's ``spectrum`` entry (``params``: the
#: instrument's parameters for each frame, sent to the spectrograph as they
#: are; docs/rv_night.md).
SPECTRUM_KEYS = frozenset({"id", "feed", "leader", "target", "acquisition", "params"})


class Spectrum(ObservationKind):
    """Fiber spectra with the enclosure's spectrograph, fed by several
    telescopes (OCS SPECTRUM; docs/rv_night.md)."""

    name = "spectrum"
    request_types = ("SPECTRUM",)
    light = True
    fields = ("spectrum",)
    spectrograph = True
    fiber = True
    always_guided = True
    camera = False

    def validate(self, obs: Observation) -> None:
        super().validate(obs)
        obs.guide = True
        spec = dict(obs.spectrum or {})
        unknown = set(spec) - SPECTRUM_KEYS
        if unknown:
            raise ValueError(
                f"observation {obs.name!r}: spectrum: unknown keys {sorted(unknown)}"
                " (the instrument's are in 'params')"
            )
        spec.setdefault("id", obs.name)
        spec["feed"] = list(spec.get("feed") or [])
        obs.spectrum = spec

    def check_configuration(
        self, ctype: str, ics: list[InstrumentConfig], guide: bool, gmode: str, what: str
    ) -> bool:
        guide = super().check_configuration(ctype, ics, guide, gmode, what)
        for i, ic in enumerate(ics):
            if ic.filter is not None or ic.port is not None:
                raise ValueError(
                    f"{what}.instrument_configs[{i}]: a {ctype} frame has no filter or port"
                )
        return guide

    def observation(self, m: RequestMapping) -> dict[str, Any]:
        c, ic = m.config, m.ic
        stamp = m.started.strftime("%Y%m%dT%H%M%S.%f")
        spectrum: dict[str, Any] = {
            "id": f"{c.id}#{m.index}{m.suffix}@{stamp}",
            "target": c.target.internal(),
            **m.acquisition,
        }
        if m.params:
            spectrum["params"] = dict(m.params)
        return dict(
            name=m.name(),
            ra_hours=c.target.ra_hours,
            dec_deg=c.target.dec_deg,
            exptime=ic.exptime,
            count=ic.count - m.done,
            allow_partial=c.allow_partial,
            object=c.target.name,
            calstage=ic.calstage,
            kind=self.name,
            spectrum=spectrum,
        )

    async def run(self, r: KindRun) -> None:
        await r.sequencer._spectrum(r.state, r.unit, r.tel, r.cam, r.obs, r.taken)


class SpectrographCalibration(ObservationKind):
    """The spectrograph's own calibration frames as one job (calibration
    plans; the telescope does nothing)."""

    name = "spectrograph_calibration"
    light = False
    fields = ("calibration",)
    spectrograph = True
    camera = False

    def validate(self, obs: Observation) -> None:
        if obs.light:
            raise ValueError(
                f"observation {obs.name!r}: spectrograph calibrations take no light (light: false)"
            )
        if not obs.calibration:
            raise ValueError(f"observation {obs.name!r}: no calibration frames")

    async def run(self, r: KindRun) -> None:
        await r.sequencer._spectrograph_calibration(r.state, r.unit, r.obs, r.taken)


class LampFrames(ObservationKind):
    """Frames of a calibration lamp on the telescope's lamp switch (OCS ARC
    and LAMP_FLAT; docs/pulizzi.md): no sky light, the shutter open."""

    light = False
    fields = ("lamp",)
    shutter = True

    def __init__(self, name: str, request_type: str) -> None:
        self.name = name  # type: ignore[misc]
        self.request_types = (request_type,)  # type: ignore[misc]

    def validate(self, obs: Observation) -> None:
        if obs.light:
            raise ValueError(
                f"observation {obs.name!r}: a {obs.kind} takes no sky light (light: false)"
            )
        if not obs.lamp:
            raise ValueError(f"observation {obs.name!r}: a {obs.kind} names its lamp")
        if obs.guide:
            raise ValueError(f"observation {obs.name!r}: a {obs.kind} is not guided")

    def check_instrument_config(
        self, ctype: str, i: int, ic: InstrumentConfig, raw: dict[str, Any], what: str
    ) -> None:
        if ic.sequence is not None and ic.lamp is not None:
            raise ValueError(
                f"{what}.instrument_configs[{i}]: name the lamp (optical_elements.lamp) or a"
                " site sequence (extra_params.sequence), not both: the sequence says its lamps"
            )
        if ic.lamp is None and ic.sequence is None:
            from robofast.request_model import LAMPS_HOW

            raise ValueError(
                f"{what}.type: {ctype!r} needs instrument_configs[{i}]"
                f".optical_elements.lamp: {LAMPS_HOW}"
            )
        if ic.light and "light" in (raw.get("extra_params") or {}):
            raise ValueError(f"{what}.instrument_configs[{i}]: an {ctype} takes no sky light")
        ic.light = False

    def check_configuration(
        self, ctype: str, ics: list[InstrumentConfig], guide: bool, gmode: str, what: str
    ) -> bool:
        if guide:
            raise ValueError(f"{what}: an {ctype} is not guided")
        return guide

    def observation(self, m: RequestMapping) -> dict[str, Any]:
        c, ic = m.config, m.ic
        if ic.sequence is not None:
            # A site sequence (docs/sequences.md, "Requests"): its template
            # made again with the request's exposure time and the frames
            # still to take; the sequence's own stage, lamp and cleanup.
            return dict(
                name=m.name(ic.sequence),
                exptime=ic.exptime,
                count=ic.count - m.done,
                light=False,
                object=c.target.name,
                kind="sequence",
                sequence=ic.sequence,
                sequence_params={"exptime": ic.exptime, "count": ic.count - m.done},
            )
        return dict(
            name=m.name(ic.lamp),
            exptime=ic.exptime,
            count=ic.count - m.done,
            light=False,
            filter=ic.filter,
            port=ic.port,
            calstage=ic.calstage,
            allow_partial=c.allow_partial,
            object=c.target.name,
            kind=self.name,
            lamp=ic.lamp,
        )

    async def run(self, r: KindRun) -> None:
        await r.sequencer._lamp_frames(r.state, r.unit, r.tel, r.cam, r.obs, r.taken)


class SequenceKind(ObservationKind):
    """A config-defined sequence (docs/sequences.md; robofast/sequences.py):
    the steps of the enclosure config's ``sequences`` entry the observation
    names, with its cleanup always run after them. Light or dark as its
    definition says (the sequencer sets ``light`` from it). Plans, and an
    ARC or LAMP_FLAT request naming one (``extra_params.sequence``: made by
    :class:`LampFrames`, with ``sequence_params``)."""

    name = "sequence"
    fields = ("sequence", "sequence_params")
    #: Its steps say: each exposure is checked.
    camera = None

    def validate(self, obs: Observation) -> None:
        if not isinstance(obs.sequence, str) or not obs.sequence:
            raise ValueError(f"observation {obs.name!r}: a sequence names its definition")
        p = obs.sequence_params
        if p is not None and (not isinstance(p, dict) or not all(isinstance(k, str) for k in p)):
            raise ValueError(f"observation {obs.name!r}: sequence_params: {{param: value}}")

    async def run(self, r: KindRun) -> None:
        await r.sequencer._sequence(r.state, r.unit, r.tel, r.cam, r.obs, r.taken)


for _kind in (
    Expose(),
    Autofocus(),
    Spectrum(),
    SpectrographCalibration(),
    Manual(),
    PointingModel(),
    RotatorCalibration(),
    SkyFlat(),
    LampFrames("arc", "ARC"),
    LampFrames("lamp_flat", "LAMP_FLAT"),
    SequenceKind(),
):
    OBSERVATION_KINDS.register(_kind.name, _kind)
del _kind
