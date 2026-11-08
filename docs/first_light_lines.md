# What first light says, and what to do next

`python -m robofast` prints one line a step. This page says what each
line means and what to do after it, for someone who has never used a
telescope (or a program reading the lines for them). The numbers in the
lines are named; nothing here needs astronomy.

## Before anything moves

| The line starts | It means | Do |
|---|---|---|
| `found Telescope ...`, `found Camera ...` | the devices it found on the network | nothing; if one is missing, check it is on and plugged in |
| `plate scale ... arcsec/pixel` | how much sky one pixel sees, from the telescope and camera | nothing |
| `site LAT, LON, ELEV m: the telescope's` | where the telescope thinks it stands | check it is right; if not, run again with `--site LAT,LON` |
| `first light: the telescope's clock disagrees ... by N min` | the mount's clock, date, time zone or longitude is wrong | set them on the mount (or give `--site`), then run again |
| `first light: ... reports its site as 0, 0` | the mount was never told where it is | run again with `--site LAT,LON` |
| `No enclosure: ... Is the sky clear ... Is this where the telescope stands? [y/N]` | the one question | answer `y` only if the sky is clear, you will stay, and the site is right |
| `first light: fetching the catalog ...: N tiles` | downloading star maps for the frames it will take | wait (once; later runs reuse them) |
| `first light: the sky turns faster than the catalog downloads here` | a slow network: some maps download later | nothing |
| `first light: focus first, with the focuser, on <name>` | it will focus on that planet or star | nothing |
| `first light: Focuser ... moves by steps, not to positions ...` | the focuser is a relative one, which first light does not drive yet | focus by hand (run without `--yes` for the walk-through) |
| `first light: Telescope ... ApertureArea: not reported; no central obstruction assumed` | the mount does not say how much of its opening is blocked | nothing (on the simulated telescope, 47 % blocked, the focus lands the same) |
| `first light: Telescope ... ApertureDiameter: not reported; ... give it with --f-ratio` | the mount does not report its aperture, and the focuser's sweep needs the focal ratio | run again with `--f-ratio N` (the focal length over the aperture, from the telescope's label or manual) |
| `first light: no focuser: no focus step` | `--yes` was given and there is no focuser | focus by hand first, or run without `--yes` |

## Focus

| The line starts | It means | Do |
|---|---|---|
| `focus: light ...`, `focus: climb ...`, `focus: donut ...` | it is moving the focuser: how bright, and the star's size (`donut`, pixels). Far out, before a star is seen: the light on the chip as a multiple of the sky beside the target (`light 400 times the sky beside it`; it grows as the star comes in), or `light on the chip` when that sky is too faint to count. Once a star is seen: its brightest pixel as a multiple of the sky around it in the same frame (`star 900 times the sky around it`), or of the frame's noise when the sky there is fainter than the noise (`star 40 times the noise around it`, short exposures). Then the exposure it took | nothing |
| `focus: ... no light above the sky beside it` | at that position nothing bright reached the chip | nothing: it steps on, both ways; if no position finds light it stops with `no light at any focus position` |
| `focus: done at position N` | in focus | nothing |
| `focus: knob ... turn ...` | the walk-through: turn the focus knob as it says, then press Enter | turn the knob that way and that much (a multiple of your last turn) |
| `... in focus: ...; stop here` | the walk-through is done | press Enter; leave the knob alone |
| `first light: focus: the target is too bright for this camera's shortest exposure ...: run again with --focus-on NAME ...` | even the camera's shortest exposure saturates on the planet | run again as it says, with the star it names (or another fainter star up now, from docs/first_light.md's list) |
| `first light: --focus-on NAME: not one first light knows; ...` | the name is not on the list | run again with a name from the list it prints |
| `first light: --focus-on NAME: at N deg it is below 30 deg now; ...` | that target is too low now | choose one higher, or wait for it to rise |
| `first light: focus: the camera, asked for a N s exposure: ...` | the camera refused an exposure; its own words follow | check the camera (its driver, its connection); the words say why |
| `first light: the mount: ...; the telescope parked` (or `the focuser`, `the camera`) | that device failed while first light ran (its own words follow) | check that device (its power, cable, driver); the telescope is parked; run again (a new `--dir`, and the focus step again) |
| `first light: ...; the telescope did not report parked after the sequencer's cleanup (N s): park it by hand now` | first light stopped, and the telescope was not parked when the sequencer's cleanup, which waits for the park, had ended | park it by hand now, then check the mount |
| `first light: focus: no light at any focus position` | nothing bright reached the camera | check the cover is off and the telescope points at the sky; focus by day on something far away first |
| `first light: focus: the focus is beyond the focuser's upper end` | the focuser cannot reach focus | add a spacer or extension between the telescope and the camera |
| `... lower end` | the same, the other way | remove a spacer or extension |
| `first light: focus: the star's light peaks with no whole donut` | the bright star is at the edge of the picture | run again: the telescope points at it again from the start |

## The calibration and the frame

| The line starts | It means | Do |
|---|---|---|
| `first light: a pointing calibration (3 plate solves), then one frame` | it is learning where the telescope really points | nothing |
| `first light frame: <path>` | the picture it took | open it |
| `solved: RA ... Dec ... from its target` | where the picture is in the sky, and how close to the aim | nothing: it worked |
| `first light: the frames did not solve` | no picture could be matched to the sky | check the sky is clear and the focus; the line says what it saw |
| `first light: not done after N s` | it ran out of time | the line says what it was waiting for |
| `first light: stopped; the telescope parked` | you pressed Ctrl-C and it parked | nothing |
| `first light: stopped; the telescope DID NOT PARK` | it could not park | park the telescope by hand now |

Every line that stops first light starts with `first light:` and says
what to do; the exit status is 0 when a frame was solved, 1 otherwise.
