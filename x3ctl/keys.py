"""Key names, HID keyboard usages and Linux evdev keycode translation."""

HID_USAGE: dict[str, int] = {
    "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
    "minus": 0x2D, "equals": 0x2E, "leftbracket": 0x2F, "rightbracket": 0x30,
    "backslash": 0x31, "semicolon": 0x33, "quote": 0x34, "grave": 0x35,
    "comma": 0x36, "period": 0x37, "slash": 0x38, "capslock": 0x39,
    "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48, "insert": 0x49,
    "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "end": 0x4D, "pagedown": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "numlock": 0x53,
    "numslash": 0x54, "numstar": 0x55, "numminus": 0x56, "numplus": 0x57,
    "numenter": 0x58, "num1": 0x59, "num2": 0x5A, "num3": 0x5B, "num4": 0x5C,
    "num5": 0x5D, "num6": 0x5E, "num7": 0x5F, "num8": 0x60, "num9": 0x61,
    "num0": 0x62, "numperiod": 0x63, "menu": 0x65,
    "lctrl": 0xE0, "lshift": 0xE1, "lalt": 0xE2, "lsuper": 0xE3,
    "rctrl": 0xE4, "rshift": 0xE5, "ralt": 0xE6, "rsuper": 0xE7,
}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    HID_USAGE[_c] = 0x04 + _i
for _i, _c in enumerate("123456789"):
    HID_USAGE[_c] = 0x1E + _i
HID_USAGE["0"] = 0x27
for _i in range(12):
    HID_USAGE[f"f{_i + 1}"] = 0x3A + _i

# Mouse buttons inside a macro.
HID_USAGE.update({
    "mouse_left": 0xF1, "mouse_right": 0xF2, "mouse_middle": 0xF3,
    "mouse_forward": 0xF4, "mouse_backward": 0xF5,
})

USAGE_NAME: dict[int, str] = {}
for _name, _code in HID_USAGE.items():
    USAGE_NAME.setdefault(_code, _name)

# Linux evdev keycode (linux/input-event-codes.h) -> key name.
_EVDEV = {
    1: "esc", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9",
    11: "0", 12: "minus", 13: "equals", 14: "backspace", 15: "tab",
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u", 23: "i", 24: "o",
    25: "p", 26: "leftbracket", 27: "rightbracket", 28: "enter", 29: "lctrl",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g", 35: "h", 36: "j", 37: "k", 38: "l",
    39: "semicolon", 40: "quote", 41: "grave", 42: "lshift", 43: "backslash",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m", 51: "comma",
    52: "period", 53: "slash", 54: "rshift", 55: "numstar", 56: "lalt", 57: "space",
    58: "capslock", 59: "f1", 60: "f2", 61: "f3", 62: "f4", 63: "f5", 64: "f6",
    65: "f7", 66: "f8", 67: "f9", 68: "f10", 69: "numlock", 70: "scrolllock",
    71: "num7", 72: "num8", 73: "num9", 74: "numminus", 75: "num4", 76: "num5",
    77: "num6", 78: "numplus", 79: "num1", 80: "num2", 81: "num3", 82: "num0",
    83: "numperiod", 87: "f11", 88: "f12", 96: "numenter", 97: "rctrl",
    98: "numslash", 99: "printscreen", 100: "ralt", 102: "home", 103: "up",
    104: "pageup", 105: "left", 106: "right", 107: "end", 108: "down",
    109: "pagedown", 110: "insert", 111: "delete", 119: "pause", 125: "lsuper",
    126: "rsuper", 127: "menu",
}


def name_for_x11_keycode(keycode: int) -> str | None:
    """GTK reports X11-style keycodes, which are evdev codes plus 8."""
    return _EVDEV.get(keycode - 8)


def name_for_usage(code: int) -> str:
    return USAGE_NAME.get(code, f"0x{code:02X}")


def usage_for_name(name: str) -> int:
    name = name.strip().lower()
    if name in HID_USAGE:
        return HID_USAGE[name]
    if name.startswith("0x"):
        return int(name, 16) & 0xFF
    raise ValueError(f"unknown key {name!r}")
