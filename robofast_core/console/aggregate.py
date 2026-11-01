"""What the observer console shows, gathered from the services' own APIs and files.

Nothing here commands anything: every read is a ``GET`` of a status
endpoint, bounded by ``status_timeout``, or a read of a file another process
writes (the sequencer's status snapshot, the operator holds, the nightly
plan record). A source that does not answer is shown as unreachable and
raises an alarm; it never blocks the rest of the page.

Sources, per enclosure (docs/console.md):

* the safety supervisor -- ``GET /status`` (weather verdict, shutter, fault
  channel, lease, alarms, heartbeat) and ``GET /timeline``;
* the weather bridge -- ``GET /weather/status`` on the safety monitor's
  address (reasons, key values per source, would-close rain);
* the sequencer -- its ``status_file`` snapshot (each telescope's state and
  current observation, the value registry: alt/az, focus, guiding, frames);
* each telescope's guider -- ``GET /status``;
* the spectrograph service -- ``GET /status`` (job, phase, exposure meter);
* the operator holds -- ``holds.json`` in ``operator_control``;
* the nightly plan record -- the tail of the night's JSON-lines file;
* the dispatch scheduler's queue -- through the request API's read-only
  view of its state file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from robofast_core.config import device_address, load_config, telescopes
from robofast_core.console.hold import read_holds

log = logging.getLogger(__name__)

#: Weather values shown first, if a source has them (others follow).
KEY_WEATHER = (
    "temperature",
    "humidity",
    "dewpoint",
    "wind_speed",
    "wind_gust",
    "sky_temperature",
    "cloud",
    "rain",
    "pressure",
)
#: Registry values shown per telescope (``<tel>.<name>``) -> label.
TELESCOPE_VALUES = {
    "tel.alt": "alt",
    "tel.az": "az",
    "tel.tracking": "tracking",
    "focuser.position": "focus",
    "focuser.temperature": "focus_temp",
    "guide_state": "guide_state",
    "guide_rms": "guide_rms",
    "guide_rms_arcsec": "guide_rms_arcsec",
    "guide_flux": "guide_flux",
    "camera.last_start": "last_frame_start",
    "camera.last_duration": "last_frame_exptime",
    "camera.ccd_temp": "ccd_temp",
    "image.fwhm": "fwhm",
}
#: How much of the plan record's tail is read (bytes).
RECORD_TAIL = 262144


def _resolve(p: str | Path, base: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else base / path


def alarm_id(enclosure: str, source: str, text: str) -> str:
    return hashlib.sha256(f"{enclosure}|{source}|{text}".encode()).hexdigest()[:16]


@dataclass
class ConsoleEnclosure:
    """One enclosure as the console sees it: where to read each part."""

    name: str
    supervisor_url: str
    telescopes: list[str]
    status_file: Path | None = None
    control_dir: Path | None = None
    weather_url: str | None = None
    spectrograph_url: str | None = None
    guiders: dict[str, str] = field(default_factory=dict)
    record_dir: Path | None = None
    record_name: str = "plan"
    #: The request API's view of the dispatch scheduler (queue, submit,
    #: cancel), if the plan is a dispatch plan with a state file and drop box.
    requests: Any = None
    #: The dome server (``http://host:port``), from ``devices.dome``.
    dome_url: str | None = None
    #: The emergency close channel (robofast_core.emergency): the file
    #: holding the token, and what to do when the supervisor does not
    #: answer: ``"dome"`` (close directly, without waiting for the
    #: telescopes to park) or ``"none"`` (report the failure only).
    emergency_token_file: Path | None = None
    emergency_fallback: str = "none"
    #: The enclosure parks its telescopes before closing (a roll-off roof).
    park_before_close: bool = False
    #: Where the sequencer saves frames (``images.data_root`` and ``site``;
    #: docs/data.md), and where the spectrograph's raw frames land
    #: (``spectrograph_frames`` in the console's entry): the image viewer.
    images_root: Path | None = None
    images_site: str = "site"
    spectrograph_frames: Path | None = None
    #: The enclosure config's ``site`` (latitude, longitude, elevation), for
    #: the sky view when there is no dispatch plan to give one.
    site: dict[str, Any] | None = None

    @classmethod
    def from_spec(
        cls, name: str, spec: dict[str, Any] | str, base: Path, *, clock: Any = None
    ) -> ConsoleEnclosure:
        """``spec``: the enclosure config's path, or ``{"config": path,
        "plan"?: path, "spectrograph"?: url, "weather"?: url}``."""
        spec = {"config": spec} if isinstance(spec, str) else dict(spec)
        unknown = set(spec) - {
            "config",
            "plan",
            "spectrograph",
            "weather",
            "emergency_close",
            "spectrograph_frames",
        }
        if unknown:
            raise ValueError(f"enclosure {name}: unknown keys {sorted(unknown)}")
        cpath = _resolve(spec["config"], base)
        config = load_config(cpath)
        cbase = cpath.parent
        if "plan" in spec:
            ppath = _resolve(spec["plan"], base)
            plan, pbase = load_config(ppath), ppath.parent
        else:
            plan, pbase = dict(config.get("plan") or {}), cbase
        tels = telescopes(config)
        guiders = {}
        for t, entry in tels.items():
            g = entry.get("guider")
            if isinstance(g, dict):
                g = g.get("url")
            if isinstance(g, str):
                guiders[t] = g.rstrip("/")
        weather = spec.get("weather")
        sm = (config.get("devices") or {}).get("safetymonitor")
        if weather is None and isinstance(sm, str):
            weather = f"http://{device_address(sm)[0]}"
        record_dir, record_name = None, "plan"
        rec = plan.get("record") if isinstance(plan, dict) else None
        if isinstance(rec, str):
            record_dir = _resolve(rec, pbase)
        elif isinstance(rec, dict) and "dir" in rec:
            record_dir = _resolve(rec["dir"], pbase)
            record_name = str(rec.get("name", "plan"))
        requests = None
        if (
            isinstance(plan, dict)
            and plan.get("type") == "dispatch"
            and "state" in plan
            and "too_dropbox" in plan
        ):
            from robofast_core.request_api import Enclosure  # astropy, via dispatch

            requests = Enclosure(
                name, config, plan, config_base=cbase, plan_base=pbase, clock=clock
            )
        sf, oc = config.get("status_file"), config.get("operator_control")
        dome = (config.get("devices") or {}).get("dome")
        images = config.get("images") if isinstance(config.get("images"), dict) else {}
        em = spec.get("emergency_close")
        token_file, fallback = None, "none"
        if em is not None:
            if not isinstance(em, dict) or set(em) - {"token_file", "fallback"}:
                raise ValueError(
                    f"enclosure {name}: emergency_close: {{'token_file': path, "
                    f"'fallback': 'dome'|'none'}}"
                )
            fallback = str(em.get("fallback", "dome"))
            if fallback not in ("dome", "none"):
                raise ValueError(f"enclosure {name}: emergency_close.fallback: 'dome' or 'none'")
            if "token_file" in em:
                token_file = _resolve(em["token_file"], base)
            elif fallback == "dome":
                raise ValueError(
                    f"enclosure {name}: emergency_close: fallback 'dome' needs a token_file"
                )
            if fallback == "dome" and not isinstance(dome, str):
                raise ValueError(f"enclosure {name}: emergency_close: no devices.dome to close")
        return cls(
            name=name,
            supervisor_url=(
                config.get("supervisor_url") or f"http://{config.get('listen', '127.0.0.1:8100')}"
            ).rstrip("/"),
            telescopes=list(tels),
            status_file=None if sf is None else _resolve(sf, cbase),
            control_dir=None if oc is None else _resolve(oc, cbase),
            weather_url=None if weather is None else str(weather).rstrip("/"),
            spectrograph_url=None
            if spec.get("spectrograph") is None
            else str(spec["spectrograph"]).rstrip("/"),
            guiders=guiders,
            record_dir=record_dir,
            record_name=record_name,
            requests=requests,
            dome_url=f"http://{device_address(dome)[0]}" if isinstance(dome, str) else None,
            emergency_token_file=token_file,
            emergency_fallback=fallback,
            park_before_close=bool((config.get("safety") or {}).get("park_before_close")),
            images_root=None
            if not images.get("data_root")
            else _resolve(images["data_root"], cbase),
            images_site=str(images.get("site", "site")),
            site=config.get("site") if isinstance(config.get("site"), dict) else None,
            spectrograph_frames=None
            if spec.get("spectrograph_frames") is None
            else _resolve(spec["spectrograph_frames"], base),
        )


class Aggregator:
    """Gathers one status document for every enclosure (module doc)."""

    def __init__(
        self,
        enclosures: dict[str, ConsoleEnclosure],
        http: httpx.AsyncClient,
        *,
        timeout: float = 2.0,
        record_lines: int = 40,
        timeline_entries: int = 60,
    ) -> None:
        self.enclosures = enclosures
        self.http = http
        self.timeout = timeout
        self.record_lines = record_lines
        self.timeline_entries = timeline_entries

    async def _get(self, url: str) -> Any:
        """GET ``url`` as JSON; raises on any failure."""
        r = await self.http.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    async def _try(self, url: str | None) -> tuple[Any, str | None]:
        if url is None:
            return None, None
        try:
            return await self._get(url), None
        except (httpx.HTTPError, ValueError) as exc:
            return None, f"{type(exc).__name__}: {exc}"[:200]

    async def supervisor_status(self, enc: ConsoleEnclosure) -> dict[str, Any] | None:
        """The supervisor's raw status (with the lease token: server side only)."""
        data, err = await self._try(f"{enc.supervisor_url}/status")
        return data if err is None and isinstance(data, dict) else None

    async def collect(self) -> dict[str, Any]:
        names = list(self.enclosures)
        results = await asyncio.gather(
            *(self.enclosure(self.enclosures[n]) for n in names), return_exceptions=True
        )
        out = {}
        for n, r in zip(names, results, strict=True):
            if isinstance(r, BaseException):
                log.error("console: gathering %s: %r", n, r)
                out[n] = {"error": "could not gather status", "alarms": []}
            else:
                out[n] = r
        return {"time": datetime.now(UTC).isoformat(), "enclosures": out}

    async def enclosure(self, enc: ConsoleEnclosure) -> dict[str, Any]:
        guider_urls = {t: f"{u}/status" for t, u in enc.guiders.items()}
        (sup, sup_err), (tl, _), (wx, wx_err), (spec, spec_err), *guides = await asyncio.gather(
            self._try(f"{enc.supervisor_url}/status"),
            self._try(f"{enc.supervisor_url}/timeline?n={self.timeline_entries}"),
            self._try(None if enc.weather_url is None else f"{enc.weather_url}/weather/status"),
            self._try(None if enc.spectrograph_url is None else f"{enc.spectrograph_url}/status"),
            *(self._try(u) for u in guider_urls.values()),
        )
        guider = dict(zip(guider_urls, guides, strict=True))
        alarms: list[dict[str, Any]] = []

        def alarm(source: str, text: str, level: str = "alarm", detail: str = "") -> None:
            """``text`` identifies the alarm (an acknowledgement sticks to it);
            ``detail`` may change from one read to the next."""
            alarms.append(
                {
                    "id": alarm_id(enc.name, source, text),
                    "enclosure": enc.name,
                    "source": source,
                    "level": level,
                    "text": text,
                    "detail": detail,
                }
            )

        enclosure = self._enclosure_view(sup, sup_err, alarm)
        weather = self._weather_view(wx, wx_err, sup, enc.weather_url is not None, alarm)
        seq, values = self._sequencer_view(enc, alarm)
        holds_doc = read_holds(enc.control_dir) if enc.control_dir is not None else None
        holds = (holds_doc or {}).get("holds") or {}
        enclosure["holds"] = holds
        enclosure["holds_updated"] = (holds_doc or {}).get("time")
        enclosure["operator_control"] = enc.control_dir is not None
        enclosure["emergency"] = {
            "fallback": enc.emergency_fallback,
            "park_before_close": enc.park_before_close,
        }
        tels = {}
        for t in enc.telescopes:
            tels[t] = self._telescope_view(t, seq, values, holds.get(t), guider.get(t), alarm)
        spectrograph = self._spectrograph_view(spec, spec_err, enc, alarm)
        return {
            "weather": weather,
            "enclosure": enclosure,
            "sequencer": {k: v for k, v in seq.items() if k not in ("telescopes", "timeline")},
            "telescopes": tels,
            "spectrograph": spectrograph,
            "plan": self._plan_view(enc),
            "timeline": tl if isinstance(tl, list) else [],
            "alarms": alarms,
            # For the console's log only (console/logstream.py); not shown.
            "_sequencer_timeline": seq.get("timeline") or [],
        }

    # -- the parts --------------------------------------------------------------------

    @staticmethod
    def _enclosure_view(sup: Any, err: str | None, alarm: Any) -> dict[str, Any]:
        if err is not None or not isinstance(sup, dict):
            alarm("supervisor", f"safety supervisor unreachable ({err})", "critical")
            return {"reachable": False}
        lease = sup.get("lease") if isinstance(sup.get("lease"), dict) else None
        for text in sup.get("alarms") or []:
            alarm("supervisor", str(text))
        if sup.get("fault"):
            alarm("enclosure", f"enclosure fault: {sup['fault']}")
        if lease is not None and lease.get("quarantined"):
            text = f"lease #{lease.get('id')} ({lease.get('holder')}) quarantined"
            alarm("lease", text, "critical")
        if sup.get("orphaned_for") is not None:
            alarm(
                "lease",
                f"lease #{(lease or {}).get('id')} revoked but unreleased "
                f"({(lease or {}).get('holder')})",
            )
        hb = sup.get("pinged_ago")
        return {
            "reachable": True,
            "shutter": sup.get("shutter"),
            "safe": sup.get("safe"),
            "reason": sup.get("reason"),
            "open_allowed": sup.get("open_allowed"),
            "fault": sup.get("fault"),
            "shutter_hold_for": sup.get("shutter_hold_for"),
            "heartbeat_age": None if hb is None else round(float(hb), 1),
            "enforced_ago": sup.get("enforced_ago"),
            "orphaned_for": sup.get("orphaned_for"),
            # Never the lease token: it stays in this process.
            "lease": None
            if lease is None
            else {
                k: lease.get(k)
                for k in (
                    "id",
                    "holder",
                    "live",
                    "opened",
                    "revoked",
                    "reason",
                    "released",
                    "quarantined",
                )
            },
        }

    @staticmethod
    def _weather_view(
        wx: Any, err: str | None, sup: Any, configured: bool, alarm: Any
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            # The verdict that matters is the supervisor's (what it enforces).
            "safe": sup.get("safe") if isinstance(sup, dict) else None,
            "reason": sup.get("reason") if isinstance(sup, dict) else None,
        }
        if not configured:
            out["bridge"] = None
            return out
        if err is not None or not isinstance(wx, dict):
            alarm("weather", f"weather bridge status unreachable ({err})", "warning")
            out["bridge"] = {"reachable": False}
            return out
        sources = {}
        for name, s in (wx.get("sources") or {}).items():
            if not isinstance(s, dict):
                continue
            vals = s.get("values") or {}
            keys = [k for k in KEY_WEATHER if k in vals] + sorted(set(vals) - set(KEY_WEATHER))
            sources[name] = {
                "state": s.get("state"),
                "required": s.get("required"),
                "age": s.get("age"),
                "fresh": s.get("fresh"),
                "reasons": s.get("reasons") or [],
                "values": {k: vals[k] for k in keys},
            }
        would = wx.get("would_close") or []
        for w in would:
            alarm("weather", f"would close: {w}", "warning")
        out["bridge"] = {
            "reachable": True,
            "safe": wx.get("safe"),
            "state": wx.get("state"),
            "limits": wx.get("limits"),
            "reasons": wx.get("reasons") or [],
            "rain_until": wx.get("rain_until"),
            "would_close": would,
            "time": wx.get("time"),
            "sources": sources,
        }
        return out

    @staticmethod
    def _sequencer_view(enc: ConsoleEnclosure, alarm: Any) -> tuple[dict[str, Any], dict]:
        if enc.status_file is None:
            return {"running": None, "detail": "no status_file in the enclosure config"}, {}
        try:
            snap = json.loads(enc.status_file.read_text())
            age = (datetime.now(UTC) - datetime.fromisoformat(snap["time"])).total_seconds()
        except (OSError, ValueError, KeyError, TypeError):
            alarm("sequencer", "no sequencer status snapshot", "warning")
            return {"running": False, "detail": "no status snapshot"}, {}
        fresh = age <= float(snap.get("max_age", 15))
        if not fresh:
            alarm("sequencer", "sequencer not running (status snapshot stale)", "warning")
        if snap.get("quarantined"):
            alarm("sequencer", "sequencer quarantined (an operator must clear it)", "critical")
        elif snap.get("needs_operator"):
            alarm("sequencer", "sequencer needs an operator", "critical")
        down = (snap.get("spectrograph") or {}).get("down") if fresh else None
        if down:
            alarm("spectrograph", f"spectrograph marked down (no spectra): {down}", "critical")
        values = snap.get("values") if fresh and isinstance(snap.get("values"), dict) else {}
        return {
            "running": fresh,
            "age": round(age, 1),
            "name": snap.get("sequencer"),
            "generation": snap.get("generation"),
            "quarantined": snap.get("quarantined"),
            "needs_operator": snap.get("needs_operator"),
            "spectrograph": snap.get("spectrograph") if fresh else None,
            "telescopes": snap.get("telescopes", {}) if fresh else {},
            "timeline": snap.get("timeline") if isinstance(snap.get("timeline"), list) else [],
        }, values

    @staticmethod
    def _telescope_view(
        tel: str,
        seq: dict[str, Any],
        values: dict[str, Any],
        hold: Any,
        guider: tuple[Any, str | None] | None,
        alarm: Any,
    ) -> dict[str, Any]:
        s = (seq.get("telescopes") or {}).get(tel)
        s = s if isinstance(s, dict) else {}
        out: dict[str, Any] = {
            "state": s.get("state", "unknown"),
            "exposing": s.get("exposing"),
            "current": s.get("current"),
            "consecutive_errors": s.get("consecutive_errors"),
            "frames_failed": s.get("frames_failed"),
            "hold": hold if isinstance(hold, dict) else None,
            "values": {},
        }
        if out["state"] == "gave_up":
            alarm(tel, f"{tel} gave up for the night", "critical")
        if out["frames_failed"]:
            alarm(tel, f"{tel}: frames lost ({out['frames_failed']})", "critical")
        for text in s.get("alarms") or []:
            alarm(tel, str(text), "alarm")
        for key, label in TELESCOPE_VALUES.items():
            v = values.get(f"{tel}.{key}")
            if isinstance(v, dict):
                out["values"][label] = {
                    "value": v.get("value"),
                    "unit": v.get("unit"),
                    "age": v.get("age"),
                    "stale": v.get("stale"),
                }
        if guider is not None:
            g, err = guider
            if err is not None or not isinstance(g, dict):
                out["guider"] = {"reachable": False}
                alarm(tel, f"{tel} guider unreachable ({err})", "warning")
            else:
                tm = g.get("telemetry") if isinstance(g.get("telemetry"), dict) else {}
                out["guider"] = {
                    "reachable": True,
                    "state": g.get("state"),
                    "reason": g.get("reason"),
                    "rms": tm.get("residual_rms"),
                    "rms_arcsec": tm.get("residual_rms_arcsec"),
                    "flux": tm.get("flux"),
                    "fwhm": tm.get("fwhm"),
                    "rate": tm.get("loop_rate"),
                }
                if g.get("state") == "failed":
                    alarm(tel, f"{tel} guider failed: {g.get('reason')}", "alarm")
                for a in (g.get("alarms") or [])[-5:]:
                    if isinstance(a, dict) and a.get("kind"):
                        alarm(
                            tel,
                            f"{tel} guider: {a['kind']}",
                            "warning",
                            f"{a.get('message', '')} ({a.get('utc', '')})",
                        )
        return out

    @staticmethod
    def _spectrograph_view(
        spec: Any, err: str | None, enc: ConsoleEnclosure, alarm: Any
    ) -> dict[str, Any] | None:
        if enc.spectrograph_url is None:
            return None
        if err is not None or not isinstance(spec, dict):
            alarm("spectrograph", f"spectrograph service unreachable ({err})", "warning")
            return {"reachable": False}
        for a in spec.get("alarms") or []:
            alarm("spectrograph", f"spectrograph: {a}", "warning")
        readings = spec.get("readings") if isinstance(spec.get("readings"), dict) else {}

        def reading(k: str) -> Any:
            r = readings.get(k)
            return r.get("value") if isinstance(r, dict) else None

        job = spec.get("job") if isinstance(spec.get("job"), dict) else None
        jv = None
        if job is not None:
            frames = job.get("frames") or []
            done = len(job.get("results") or [])
            cur = frames[min(done, len(frames) - 1)] if frames else {}
            started = job.get("created")
            jv = {
                "id": job.get("id"),
                "kind": job.get("kind"),
                "state": job.get("state"),
                "phase": job.get("phase"),
                "error": job.get("error"),
                "frames_done": done,
                "frames": len(frames),
                "frame_kind": cur.get("kind"),
                "object": cur.get("object"),
                "exptime": cur.get("exptime"),
                "target_counts": cur.get("target_counts"),
                "elapsed": None
                if started is None or job.get("ended") is not None
                else round(time.time() - float(started), 1),
                "last_counts": (job.get("results") or [{}])[-1].get("expmeter_counts")
                if done
                else None,
            }
        return {
            "reachable": True,
            "busy": spec.get("busy"),
            "job": jv,
            "expmeter_total": reading("expmeter_total"),
            "ccd_temp": reading("ccd_temp"),
            "stage": reading("stage"),
            "spec_pressure": reading("spec_pressure"),
            "led_on": spec.get("led_on"),
            # Who waits for the spectrograph (its shared queue).
            "queue": [q for q in spec.get("queue") or [] if isinstance(q, dict)],
            "backlight_on": spec.get("backlight_on"),
        }

    def _plan_view(self, enc: ConsoleEnclosure) -> dict[str, Any]:
        out: dict[str, Any] = {"recent": [], "upcoming": [], "record": None}
        if enc.record_dir is not None:
            files = sorted(enc.record_dir.glob(f"{enc.record_name}-*.jsonl"))
            if files:
                out["record"] = files[-1].name
                out["recent"] = [summarize(e) for e in tail_record(files[-1], self.record_lines)]
        if enc.requests is not None:
            try:
                entries = enc.requests.entries()
            except Exception as exc:  # a broken state file: show it, not crash
                log.warning("console: %s queue: %r", enc.name, exc)
                entries = {}
            up = []
            for gid, d in entries.items():
                if d.get("state") != "PENDING" and d.get("intake") != "QUEUED":
                    continue
                up.append(
                    {
                        "id": gid,
                        "intake": d.get("intake"),
                        "priority": d.get("ipp_value"),
                        "too": d.get("observation_type", "NORMAL") != "NORMAL",
                        "submitter": d.get("submitter"),
                        "submitted": d.get("created"),
                        "cancel_pending": d.get("cancel_pending", False),
                    }
                )
            up.sort(key=lambda r: (not r["too"], -(r["priority"] or 0), r["id"]))
            out["upcoming"] = up[:50]
            out["queue_count"] = len(up)
        return out


def tail_record(path: Path, n: int) -> list[dict[str, Any]]:
    """The last ``n`` entries of a plan record, snapshots left out."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - RECORD_TAIL))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > RECORD_TAIL:
        lines = lines[1:]  # the first is partial
    out = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("kind") != "snapshot":
            out.append(e)
            if len(out) >= n:
                break
    return out[::-1]


def summarize(e: dict[str, Any]) -> dict[str, Any]:
    """A plan record entry, cut down for the page."""
    keep = (
        "t",
        "kind",
        "telescope",
        "request",
        "config",
        "observation",
        "score",
        "outcome",
        "frames",
        "why",
        "policy",
        "accepted",
        "seconds",
        "reason",
    )
    out = {k: e[k] for k in keep if k in e and not isinstance(e[k], dict | list)}
    if isinstance(e.get("group"), dict):
        out["group"] = e["group"].get("id")
    elif isinstance(e.get("group"), str):
        out["group"] = e["group"]
    if isinstance(e.get("runners_up"), list):
        out["runners_up"] = len(e["runners_up"])
    if isinstance(e.get("rejected"), list):
        out["rejected"] = len(e["rejected"])
    return out
