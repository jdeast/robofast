"""The supervisor's command line names what its config is missing
(docs/design.md 3.10), before it starts."""

from __future__ import annotations

import json

import pytest

from robofast.supervisor_service import main


@pytest.mark.parametrize(
    "devices, missing",
    [
        (None, "devices"),
        ({}, "dome, safetymonitor"),
        ({"dome": "127.0.0.1:9/0"}, "safetymonitor"),
        ({"safetymonitor": "127.0.0.1:9/0"}, "dome"),
    ],
)
def test_missing_devices_are_named(tmp_path, capsys, devices, missing):
    config = {"name": "classroom", "telescopes": {"t1": {"telescope": "127.0.0.1:9/0"}}}
    if devices is not None:
        config["devices"] = devices
    path = tmp_path / "observatory.json"
    path.write_text(json.dumps(config))
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(path)])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(path) in err and f"missing required key(s) {missing}" in err
