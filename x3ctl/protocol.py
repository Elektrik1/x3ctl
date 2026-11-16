"""Attack Shark X3 configuration protocol.

A Python port of the protocol layer of asctl (https://github.com/yourchocomate/attacksharkx3,
MIT, (c) 2026 Habibur Rahman), which reverse-engineered the vendor's Windows software.

Every configuration write is a HID *feature* report. The protocol is write-only:
the device cannot report its current settings, so the application remembers what
it last sent. Each report must be preceded by PREAMBLE or the device acknowledges
it and then ignores it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VENDOR_ID = 0x1D57
PRODUCT_IDS = (0xFA60, 0xFA61)

# Sent before every configuration report. Without it a report is acknowledged
# and silently discarded.
PREAMBLE = bytes([0x0C, 0x0A, 0x01, 0xFE, 0x01, 0xFE, 0, 0, 0, 0])


def checksum(payload: bytes | bytearray) -> tuple[int, int]:
    """16-bit sum of the payload bytes, returned (high, low)."""
    total = sum(payload) & 0xFFFF
    return total >> 8, total & 0xFF


def _put_checksum(report: bytearray, start: int, end: int) -> None:
    """Sum report[start:end] and store it big-endian at report[end]."""
    hi, lo = checksum(report[start:end])
    report[end] = hi
    report[end + 1] = lo


# ---------------------------------------------------------------- DPI, report 0x04

DPI_STEP = 50
DPI_MIN = 50
DPI_MAX = 26000
DPI_SLOTS = 8

RGB = tuple[int, int, int]


@dataclass
class Stage:
    dpi: int
    enabled: bool
    colour: RGB


# The vendor's reset table: six stages, active slot 1 (1600 DPI).
VENDOR_FACTORY_STAGES = [
    Stage(800, True, (255, 0, 0)),
    Stage(1600, True, (0, 255, 0)),
    Stage(2400, True, (0, 0, 255)),
    Stage(3200, True, (255, 255, 0)),
    Stage(5000, True, (0, 255, 255)),
    Stage(26000, True, (255, 0, 255)),
    Stage(0, False, (255, 64, 0)),
    Stage(0, False, (255, 255, 255)),
]
VENDOR_FACTORY_ACTIVE = 1

# What the mouse ships with, as observed on hardware by the asctl author.
STOCK_STAGES = [
    Stage(800, True, (0, 0, 255)),
    Stage(1600, True, (0, 255, 255)),
    Stage(3200, True, (0, 255, 0)),
    Stage(5000, True, (255, 255, 0)),
    Stage(26000, True, (255, 0, 0)),
    Stage(0, False, (128, 0, 128)),
    Stage(0, False, (255, 165, 0)),
    Stage(0, False, (255, 255, 255)),
]
STOCK_ACTIVE = 0


@dataclass
class SensorToggles:
    lift_off_2mm: bool = False
    ripple_control: bool = False
    angle_snap: bool = False
    motion_sync: bool = False


def dpi_wire_value(dpi: int) -> int:
    return max(0, dpi // DPI_STEP - 1)


def dpi_from_wire(wire: int) -> int:
    return (wire + 1) * DPI_STEP


def build_dpi_report(stages: list[Stage], active: int, toggles: SensorToggles) -> bytes:
    """Report 0x04: eight fixed DPI slots plus the four sensor toggles.

    Slots are addressed by index; a disabled slot still carries its DPI and
    colour, enablement lives only in the bitmask at byte 5.
    """
    if len(stages) != DPI_SLOTS:
        raise ValueError("the DPI report has exactly 8 slots")
    r = bytearray(56)
    r[0] = 0x04
    r[1] = 56
    r[2] = 0x01
    r[3] = int(toggles.lift_off_2mm)
    r[4] = int(toggles.ripple_control)
    r[6] = int(toggles.angle_snap)
    r[7] = int(toggles.motion_sync)

    mask = 0
    for i, s in enumerate(stages):
        if s.enabled:
            mask |= 1 << i
    r[5] = mask

    for i, s in enumerate(stages):
        stored = s.dpi // DPI_STEP
        if stored < 1:
            continue  # the vendor leaves a sub-step slot as zero
        wire = min(stored - 1, 0xFFFF)
        r[8 + i] = wire & 0xFF
        r[16 + i] = wire >> 8

    r[24] = min(max(active + 1, 0), 255)
    for i, s in enumerate(stages):
        r[25 + i * 3 : 28 + i * 3] = bytes(s.colour)
    r[49] = 0x01
    _put_checksum(r, 3, 50)
    return bytes(r)


# ------------------------------------------------------- polling rate, report 0x06

POLLING_RATES = (125, 250, 500, 1000)


def build_polling_report(hz: int) -> bytes:
    if hz not in POLLING_RATES:
        raise ValueError(f"unsupported polling rate {hz}")
    divider = 1000 // hz
    return bytes([0x06, 0x09, 0x01, divider, (~divider) & 0xFF, 0, 0, 0, 0])


# ------------------------------------------------- power and debounce, report 0x05


def effective_debounce_ms(wire: int) -> int:
    """The debounce field counts 2 ms units (measured by the asctl author)."""
    return wire * 2


def build_power_report(sleep_minutes: int, deep_sleep_minutes: int, debounce: int) -> bytes:
    """Report 0x05. It also carries lighting fields, which do nothing on the X3."""
    sleep_minutes = _clamp(sleep_minutes, 0, 255)
    deep = _clamp(deep_sleep_minutes, 0, 255)
    debounce = _clamp(debounce, 0, 255)
    brightness_slot = 8  # mode "off" does not use brightness
    speed = 4
    r = bytearray(15)
    r[0] = 0x05
    r[1] = 15
    r[2] = 0x01
    r[3] = 0  # lighting mode off
    r[4] = (deep & 0xF0) | (9 - speed)
    r[5] = ((deep & 0x0F) << 4) | brightness_slot
    r[6], r[7], r[8] = 255, 0, 0
    r[9] = sleep_minutes
    r[10] = debounce
    _put_checksum(r, 3, 11)
    return bytes(r)


# ------------------------------------------------------- button mapping, report 0x08

BUTTON_ENTRIES = 18

# Which report entry each physical button drives, established on hardware by
# the asctl author. Entries not listed here trigger nothing on this shell.
PHYSICAL_BUTTONS = [
    (0, "Left button"),
    (1, "Right button"),
    (2, "Middle (wheel) button"),
    (3, "DPI button"),
    (4, "Mode key"),
    (6, "Forward (side)"),
    (7, "Back (side)"),
]
MODE_BUTTON_ENTRY = 4

MODIFIERS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "super": 0x08, "win": 0x08, "meta": 0x08}

# action key -> (code, modifier, key). Shortcuts use Linux conventions.
ACTIONS: dict[str, tuple[int, int, int]] = {
    "none": (0x00, 0, 0),
    "button_off": (0x01, 0, 0),
    "left": (0x02, 0, 0),
    "right": (0x03, 0, 0),
    "middle": (0x04, 0, 0),
    "backward": (0x05, 0, 0),
    "forward": (0x06, 0, 0),
    "double_click": (0x07, 0, 0),
    "fire_button": (0x08, 0, 0),
    "scroll_up": (0x09, 0, 0),
    "scroll_down": (0x0A, 0, 0),
    "tilt_left": (0x0B, 0, 0),
    "tilt_right": (0x0C, 0, 0),
    "dpi_cycle": (0x0D, 0, 0),
    "dpi_up": (0x0E, 0, 0),
    "dpi_down": (0x0F, 0, 0),
    "easy_aim": (0x10, 0x00, 0x03),
    "led_loop": (0x29, 0x00, 0x03),
    "media_player": (0x15, 0, 0),
    "previous_track": (0x16, 0, 0),
    "next_track": (0x17, 0, 0),
    "play_pause": (0x18, 0, 0),
    "media_stop": (0x19, 0, 0),
    "mute": (0x1A, 0, 0),
    "volume_up": (0x1B, 0, 0),
    "volume_down": (0x1C, 0, 0),
    "calculator": (0x1D, 0, 0),
    "email": (0x1E, 0, 0),
    "browser_forward": (0x20, 0, 0),
    "browser_backward": (0x21, 0, 0),
    "browser_stop": (0x22, 0, 0),
    "my_computer": (0x23, 0, 0),
    "browser_refresh": (0x24, 0, 0),
    "browser_home": (0x25, 0, 0),
    "browser_search": (0x26, 0, 0),
    "profile_cycle": (0x34, 0, 0),
    "profile_up": (0x35, 0, 0),
    "profile_down": (0x36, 0, 0),
    "mode_switch": (0x3C, 0, 0),
    # Keyboard shortcuts, Linux style
    "copy": (0x11, 0x01, 0x06),  # Ctrl+C
    "cut": (0x11, 0x01, 0x1B),  # Ctrl+X
    "paste": (0x11, 0x01, 0x19),  # Ctrl+V
    "undo": (0x11, 0x01, 0x1D),  # Ctrl+Z
    "redo": (0x11, 0x03, 0x1D),  # Ctrl+Shift+Z
    "select_all": (0x11, 0x01, 0x04),  # Ctrl+A
    "find": (0x11, 0x01, 0x09),  # Ctrl+F
    "save": (0x11, 0x01, 0x16),  # Ctrl+S
    "new_tab": (0x11, 0x01, 0x17),  # Ctrl+T
    "close_tab": (0x11, 0x01, 0x1A),  # Ctrl+W
    "reopen_tab": (0x11, 0x03, 0x17),  # Ctrl+Shift+T
    "zoom_in": (0x11, 0x01, 0x2E),  # Ctrl+=
    "zoom_out": (0x11, 0x01, 0x2D),  # Ctrl+-
    "close_window": (0x11, 0x04, 0x3D),  # Alt+F4
    "switch_window": (0x11, 0x04, 0x2B),  # Alt+Tab
    "activities": (0x11, 0x00, 0xE3),  # Super
    "screenshot": (0x11, 0x00, 0x46),  # Print Screen
    "lock_screen": (0x11, 0x08, 0x0F),  # Super+L
    "show_desktop": (0x11, 0x08, 0x07),  # Super+D
}

ACTION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Mouse", [
        ("left", "Left click"), ("right", "Right click"), ("middle", "Middle click"),
        ("backward", "Back"), ("forward", "Forward"), ("double_click", "Double click"),
        ("fire_button", "Fire button (rapid click)"), ("scroll_up", "Scroll up"),
        ("scroll_down", "Scroll down"), ("tilt_left", "Tilt left"), ("tilt_right", "Tilt right"),
    ]),
    ("Device", [
        ("dpi_cycle", "DPI cycle"), ("dpi_up", "DPI up"), ("dpi_down", "DPI down"),
        ("mode_switch", "Mode switch (Bluetooth channel)"), ("easy_aim", "Easy aim (sniper)"),
        ("profile_cycle", "Profile cycle"),
    ]),
    ("Media", [
        ("play_pause", "Play / pause"), ("media_stop", "Stop"),
        ("previous_track", "Previous track"), ("next_track", "Next track"),
        ("mute", "Mute"), ("volume_up", "Volume up"), ("volume_down", "Volume down"),
        ("media_player", "Media player"),
    ]),
    ("Shortcuts", [
        ("copy", "Copy (Ctrl+C)"), ("cut", "Cut (Ctrl+X)"), ("paste", "Paste (Ctrl+V)"),
        ("undo", "Undo (Ctrl+Z)"), ("redo", "Redo (Ctrl+Shift+Z)"),
        ("select_all", "Select all (Ctrl+A)"), ("find", "Find (Ctrl+F)"),
        ("save", "Save (Ctrl+S)"), ("new_tab", "New tab (Ctrl+T)"),
        ("close_tab", "Close tab (Ctrl+W)"), ("reopen_tab", "Reopen tab (Ctrl+Shift+T)"),
        ("zoom_in", "Zoom in (Ctrl+=)"), ("zoom_out", "Zoom out (Ctrl+-)"),
        ("switch_window", "Switch window (Alt+Tab)"), ("close_window", "Close window (Alt+F4)"),
        ("activities", "Activities (Super)"), ("show_desktop", "Show desktop (Super+D)"),
        ("screenshot", "Screenshot (Print)"), ("lock_screen", "Lock screen (Super+L)"),
    ]),
    ("System", [
        ("browser_home", "Browser home"), ("browser_search", "Browser search"),
        ("browser_refresh", "Browser refresh"), ("browser_backward", "Browser back"),
        ("browser_forward", "Browser forward"), ("calculator", "Calculator"),
        ("email", "Email"), ("my_computer", "File manager"),
    ]),
    ("Off", [("button_off", "Disabled")]),
]

# The vendor reset table: 18 entries.
FACTORY_BUTTONS = [
    "left", "right", "middle", "dpi_cycle", "mode_switch",
    "dpi_down", "forward", "backward", "mode_switch",
    "button_off", "button_off", "button_off", "button_off",
    "button_off", "button_off", "button_off",
    "scroll_down", "scroll_up",
]


def action_label(spec: str) -> str:
    if spec.startswith("key:"):
        return "Shortcut " + spec[4:]
    if spec.startswith("macro:"):
        return "Macro " + spec[6:]
    for _, items in ACTION_GROUPS:
        for key, label in items:
            if key == spec:
                return label
    return spec


def parse_action(spec: str) -> tuple[int, int, int]:
    """`left`, `key:ctrl+c`, `macro:2`, `raw:11.01.06` -> three wire bytes."""
    text = spec.strip().lower()
    if not text:
        return ACTIONS["none"]
    if text in ACTIONS:
        return ACTIONS[text]
    if text.startswith("raw:"):
        parts = [int(p, 16) for p in text[4:].split(".")]
        if len(parts) != 3 or any(not 0 <= p <= 255 for p in parts):
            raise ValueError(f"bad raw action {spec!r}")
        return tuple(parts)  # type: ignore[return-value]
    if text.startswith("macro:"):
        slot = int(text[6:])
        if not 0 <= slot <= 255:
            raise ValueError(f"bad macro slot {spec!r}")
        return (0x12, 0, slot)
    if text.startswith("key:"):
        from .keys import HID_USAGE

        modifier = 0
        usage = None
        for token in text[4:].split("+"):
            token = token.strip()
            if token in MODIFIERS:
                modifier |= MODIFIERS[token]
            elif token in HID_USAGE:
                usage = HID_USAGE[token]
            else:
                raise ValueError(f"unknown key {token!r} in {spec!r}")
        if usage is None:
            raise ValueError(f"shortcut {spec!r} has no key")
        return (0x11, modifier, usage)
    raise ValueError(f"unknown action {spec!r}")


def build_button_report(actions: list[str]) -> bytes:
    r = bytearray(59)
    r[0] = 0x08
    r[1] = 59
    r[2] = 0x01
    for i in range(BUTTON_ENTRIES):
        if i < len(actions):
            r[3 + i * 3 : 6 + i * 3] = bytes(parse_action(actions[i]))
    _put_checksum(r, 3, 57)
    return bytes(r)


def has_left_click(actions: list[str]) -> bool:
    return any(parse_action(a)[0] == 0x02 for a in actions)


# --------------------------------------------------------------- macros, report 0x09

MACRO_MAX_EVENTS = 50
LOOP_TIMES, LOOP_UNTIL_KEY, LOOP_WHILE_HELD = 0, 1, 2
MOUSE_MACRO_CODES = {
    "mouse_left": 0xF1, "mouse_right": 0xF2, "mouse_middle": 0xF3,
    "mouse_forward": 0xF4, "mouse_backward": 0xF5,
}


@dataclass
class MacroEvent:
    key: int  # HID usage, or 0xF1..0xF5 for mouse buttons
    down: bool
    delay_ms: int


def split_delay(delay_ms: int) -> list[int]:
    """The device cannot store more than 1270 ms per event; split like the vendor."""
    if delay_ms <= 1270:
        return [max(1, delay_ms)]
    parts = [delay_ms % 200]
    remaining = delay_ms // 200
    while remaining > 0 and len(parts) < 10:
        parts.append(200)
        remaining -= 1
    return [max(1, p) for p in parts]


def encode_macro_event(ev: MacroEvent) -> bytes:
    units = int(max(1, ev.delay_ms) / 10.0 + 0.5)
    flag = 0x01 if ev.down else 0x81
    return bytes([(units & 0xFF) | flag, ev.key & 0xFF])


def build_macro_buffer(index: int, events: list[MacroEvent], repeat: int = 1,
                       loop_mode: int = LOOP_TIMES) -> bytes:
    if len(events) > MACRO_MAX_EVENTS:
        raise ValueError(f"a macro holds at most {MACRO_MAX_EVENTS} events")
    b = bytearray(131)
    b[0] = 0x09
    b[1] = 131
    b[2] = index & 0xFF
    b[3] = loop_mode
    b[7] = max(1, min(repeat, 255))
    b[28] = len(events)
    off = 29
    for ev in events:
        b[off : off + 2] = encode_macro_event(ev)
        off += 2
    _put_checksum(b, 3, 129)
    return bytes(b)


def fragment(command: int, payload: bytes) -> list[bytes]:
    """Split a long logical buffer into 64-byte report-0x09 chunks (60 payload bytes each)."""
    chunks = []
    for seq, start in enumerate(range(0, max(len(payload), 1), 60)):
        part = payload[start : start + 60]
        c = bytearray(64)
        c[0] = 0x09
        c[1] = 4 + len(part)
        c[2] = command & 0xFF
        c[3] = seq
        c[4 : 4 + len(part)] = part
        chunks.append(bytes(c))
    return chunks


def build_macro_reports(index: int, events: list[MacroEvent], repeat: int = 1,
                        loop_mode: int = LOOP_TIMES) -> list[bytes]:
    buf = build_macro_buffer(index, events, repeat, loop_mode)
    return fragment(index, buf[3:])


# -------------------------------------------------- status events, input report 0x03


@dataclass
class StatusEvent:
    code: int
    value: int

    @property
    def battery_percent(self) -> int | None:
        if self.code == 0x4010:
            level = self.value >> 8
        elif self.code == 0x6000:
            level = self.value
        else:
            return None
        return level * 10 if 1 <= level <= 10 else None

    def describe(self) -> str:
        if self.code == 0x1000:
            return f"DPI stage changed to {self.value}"
        if self.code == 0x5010:
            return f"device acknowledged report 0x{self.value >> 8:02X}"
        if self.code in (0x4010, 0x6000):
            pct = self.battery_percent
            return f"battery {pct}%" if pct is not None else f"battery event 0x{self.value:04X}"
        if self.code == 0x2000:
            return f"polling rate index changed to {self.value}"
        if self.code == 0x8000:
            return f"profile changed to {self.value}"
        return f"event 0x{self.code:04X} value 0x{self.value:04X}"


def parse_status(data: bytes) -> StatusEvent | None:
    if len(data) < 5 or data[0] != 0x03:
        return None
    return StatusEvent(code=data[2] << 8 | data[1], value=data[4] << 8 | data[3])


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


@dataclass
class Settings:
    """Everything the app can write. Persisted, since the mouse cannot be read back."""

    stages: list[Stage] = field(default_factory=lambda: [Stage(s.dpi, s.enabled, s.colour) for s in STOCK_STAGES])
    active_stage: int = STOCK_ACTIVE
    toggles: SensorToggles = field(default_factory=SensorToggles)
    polling_hz: int = 1000
    sleep_minutes: int = 10
    deep_sleep_minutes: int = 10
    debounce: int = 10
    buttons: list[str] = field(default_factory=lambda: list(FACTORY_BUTTONS))

    def to_dict(self) -> dict:
        return {
            "stages": [{"dpi": s.dpi, "enabled": s.enabled, "colour": list(s.colour)} for s in self.stages],
            "active_stage": self.active_stage,
            "toggles": vars(self.toggles).copy(),
            "polling_hz": self.polling_hz,
            "sleep_minutes": self.sleep_minutes,
            "deep_sleep_minutes": self.deep_sleep_minutes,
            "debounce": self.debounce,
            "buttons": list(self.buttons),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        if "stages" in d and len(d["stages"]) == DPI_SLOTS:
            s.stages = [Stage(int(x["dpi"]), bool(x["enabled"]), tuple(int(c) for c in x["colour"]))  # type: ignore[misc]
                        for x in d["stages"]]
        s.active_stage = int(d.get("active_stage", s.active_stage))
        t = d.get("toggles", {})
        s.toggles = SensorToggles(**{k: bool(v) for k, v in t.items() if k in vars(SensorToggles())})
        s.polling_hz = int(d.get("polling_hz", s.polling_hz))
        s.sleep_minutes = int(d.get("sleep_minutes", s.sleep_minutes))
        s.deep_sleep_minutes = int(d.get("deep_sleep_minutes", s.deep_sleep_minutes))
        s.debounce = int(d.get("debounce", s.debounce))
        b = d.get("buttons")
        if isinstance(b, list) and len(b) == BUTTON_ENTRIES:
            s.buttons = [str(x) for x in b]
        return s

    def dpi_report(self) -> bytes:
        return build_dpi_report(self.stages, self.active_stage, self.toggles)

    def polling_report(self) -> bytes:
        return build_polling_report(self.polling_hz)

    def power_report(self) -> bytes:
        return build_power_report(self.sleep_minutes, self.deep_sleep_minutes, self.debounce)

    def button_report(self) -> bytes:
        return build_button_report(self.buttons)
