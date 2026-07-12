"""Linux uinput backend exposing PartyPad slots as ordinary gamepads."""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from evdev import AbsInfo, UInput, ecodes
except ImportError:  # Keep Dolphin-only imports usable without the optional backend.
    AbsInfo = UInput = ecodes = None


AXIS_MIN = -32768
AXIS_MAX = 32767


def axis_value(value: float) -> int:
    """Convert PartyPad's -1..1 axes to signed evdev axes."""
    value = max(-1.0, min(1.0, float(value)))
    return round(value * (AXIS_MAX if value >= 0 else -AXIS_MIN))


def pad_events(pad) -> dict[tuple[int, int], int]:
    """Return the complete evdev state for a PadState-like object."""
    if ecodes is None:
        raise RuntimeError("RetroArch support requires the 'evdev' Python package")
    b = pad.buttons
    return {
        (ecodes.EV_KEY, ecodes.BTN_SOUTH): int(b["cross"]),
        (ecodes.EV_KEY, ecodes.BTN_EAST): int(b["circle"]),
        (ecodes.EV_KEY, ecodes.BTN_NORTH): int(b["triangle"]),
        (ecodes.EV_KEY, ecodes.BTN_WEST): int(b["square"]),
        (ecodes.EV_KEY, ecodes.BTN_TL): int(b["l1"]),
        (ecodes.EV_KEY, ecodes.BTN_TR): int(b["r1"]),
        (ecodes.EV_KEY, ecodes.BTN_TL2): int(b["l2"]),
        (ecodes.EV_KEY, ecodes.BTN_TR2): int(b["r2"]),
        (ecodes.EV_KEY, ecodes.BTN_THUMBL): int(b["l3"]),
        (ecodes.EV_KEY, ecodes.BTN_THUMBR): int(b["r3"]),
        (ecodes.EV_KEY, ecodes.BTN_SELECT): int(b["share"]),
        (ecodes.EV_KEY, ecodes.BTN_START): int(b["options"]),
        (ecodes.EV_KEY, ecodes.BTN_MODE): int(b["ps"]),
        (ecodes.EV_ABS, ecodes.ABS_X): axis_value(pad.left_x),
        (ecodes.EV_ABS, ecodes.ABS_Y): axis_value(-pad.left_y),
        (ecodes.EV_ABS, ecodes.ABS_RX): axis_value(pad.right_x),
        (ecodes.EV_ABS, ecodes.ABS_RY): axis_value(-pad.right_y),
        (ecodes.EV_ABS, ecodes.ABS_HAT0X): int(b["dpad_right"]) - int(b["dpad_left"]),
        (ecodes.EV_ABS, ecodes.ABS_HAT0Y): int(b["dpad_down"]) - int(b["dpad_up"]),
    }


@dataclass
class UInputPad:
    slot: int
    device: object
    previous: dict[tuple[int, int], int] = field(default_factory=dict)

    def update(self, pad, *, force: bool = False) -> None:
        current = pad_events(pad)
        changed = (
            current
            if force
            else {key: val for key, val in current.items() if self.previous.get(key) != val}
        )
        for (event_type, code), value in changed.items():
            self.device.write(event_type, code, value)
        if changed:
            self.device.syn()
        self.previous = current

    def neutralize(self) -> None:
        if not self.previous:
            return
        for (event_type, code), value in self.previous.items():
            neutral = 0
            if value != neutral:
                self.device.write(event_type, code, neutral)
        self.device.syn()
        self.previous = {key: 0 for key in self.previous}

    def close(self) -> None:
        self.neutralize()
        self.device.close()


class UInputBackend:
    """Own four stable virtual devices for the lifetime of the server."""

    def __init__(self, pad_count: int = 4, device_factory=None):
        if UInput is None:
            raise RuntimeError(
                "RetroArch support requires the 'evdev' Python package; run 'uv sync'"
            )
        factory = device_factory or UInput
        axis = AbsInfo(0, AXIS_MIN, AXIS_MAX, 128, 256, 0)
        hat = AbsInfo(0, -1, 1, 0, 0, 0)
        capabilities = {
            ecodes.EV_KEY: [
                ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_NORTH, ecodes.BTN_WEST,
                ecodes.BTN_TL, ecodes.BTN_TR, ecodes.BTN_TL2, ecodes.BTN_TR2,
                ecodes.BTN_THUMBL, ecodes.BTN_THUMBR, ecodes.BTN_SELECT,
                ecodes.BTN_START, ecodes.BTN_MODE,
            ],
            ecodes.EV_ABS: [
                (ecodes.ABS_X, axis), (ecodes.ABS_Y, axis),
                (ecodes.ABS_RX, axis), (ecodes.ABS_RY, axis),
                (ecodes.ABS_HAT0X, hat), (ecodes.ABS_HAT0Y, hat),
            ],
        }
        self.pads = []
        try:
            for slot in range(pad_count):
                device = factory(
                    capabilities,
                    name="PartyPad Controller",
                    vendor=0x1209,
                    product=0x5050,
                    version=1,
                    bustype=ecodes.BUS_VIRTUAL,
                    phys=f"partypad/input{slot}",
                )
                self.pads.append(UInputPad(slot, device))
        except Exception:
            self.close()
            raise

    def update(self, pads) -> None:
        for output, pad in zip(self.pads, pads):
            output.update(pad)

    def neutralize(self, slot: int) -> None:
        self.pads[slot].neutralize()

    def close(self) -> None:
        for pad in getattr(self, "pads", []):
            pad.close()
        self.pads = []
