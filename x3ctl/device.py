"""Talking to the X3 receiver through Linux hidraw. No third-party dependencies."""

from __future__ import annotations

import errno
import fcntl
import glob
import os
import select
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .protocol import PREAMBLE, PRODUCT_IDS, VENDOR_ID, hexs, parse_status, StatusEvent

# Gap between reports; the vendor tool and asctl both pace their writes.
WRITE_GAP_S = 0.12


def _hidiocsfeature(length: int) -> int:
    # _IOC(_IOC_WRITE | _IOC_READ, 'H', 0x06, len)
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x06


def feature_report_ids(descriptor: bytes) -> set[int]:
    """Report IDs that carry Feature items, from a raw HID report descriptor."""
    ids: set[int] = set()
    report_id = 0
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item
            if i + 1 >= len(descriptor):
                break
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        data = int.from_bytes(descriptor[i + 1 : i + 1 + size], "little")
        tag = prefix & 0xFC
        if tag == 0x84:  # Report ID (global)
            report_id = data
        elif tag == 0xB0:  # Feature (main)
            ids.add(report_id)
        i += 1 + size
    return ids


@dataclass
class DeviceInfo:
    path: str  # /dev/hidrawN
    name: str
    product_id: int
    readable: bool


def find_config_interface() -> DeviceInfo | None:
    """The receiver exposes four hidraw nodes; config lives on the one with feature report 0x04."""
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sys_path, "device", "uevent")) as f:
                uevent = dict(line.strip().split("=", 1) for line in f if "=" in line)
            bus, vid, pid = uevent.get("HID_ID", "0:0:0").split(":")
            vid, pid = int(vid, 16), int(pid, 16)
            if vid != VENDOR_ID or pid not in PRODUCT_IDS:
                continue
            with open(os.path.join(sys_path, "device", "report_descriptor"), "rb") as f:
                descriptor = f.read()
        except (OSError, ValueError):
            continue
        if 0x04 not in feature_report_ids(descriptor):
            continue
        dev = "/dev/" + os.path.basename(sys_path)
        return DeviceInfo(
            path=dev,
            name=uevent.get("HID_NAME", "Attack Shark X3"),
            product_id=pid,
            readable=os.access(dev, os.R_OK | os.W_OK),
        )
    return None


class DeviceError(Exception):
    pass


class Device:
    """An open config interface. Writes feature reports; optionally reads status events."""

    def __init__(self, info: DeviceInfo):
        self.info = info
        try:
            self.fd = os.open(info.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as e:
            raise DeviceError(
                f"no permission to open {info.path}. Install the udev rule "
                "(see README) and replug the receiver.") from e
        except OSError as e:
            raise DeviceError(f"cannot open {info.path}: {e.strerror}") from e
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    def close(self) -> None:
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=1)
        try:
            os.close(self.fd)
        except OSError:
            pass

    def set_feature(self, report: bytes, attempts: int = 3) -> None:
        buf = bytearray(report)
        last: OSError | None = None
        for _ in range(attempts):
            try:
                fcntl.ioctl(self.fd, _hidiocsfeature(len(buf)), buf)
                return
            except OSError as e:
                last = e
                # The receiver stalls the first control transfer after idle.
                if e.errno in (errno.EPIPE, errno.EAGAIN, errno.ETIMEDOUT, errno.EIO):
                    time.sleep(0.05)
                    continue
                break
        raise DeviceError(f"writing report 0x{report[0]:02X} failed: {last}")

    def send(self, reports: list[bytes], log: Callable[[str], None] = lambda s: None) -> None:
        """Send reports, each preceded by the mandatory preamble."""
        with self._lock:
            for report in reports:
                self.set_feature(PREAMBLE)
                time.sleep(WRITE_GAP_S)
                log("→ " + hexs(report))
                self.set_feature(report)
                time.sleep(WRITE_GAP_S)

    def send_chunked(self, chunks: list[bytes], log: Callable[[str], None] = lambda s: None) -> None:
        """Macro upload: one preamble, then every chunk."""
        with self._lock:
            self.set_feature(PREAMBLE)
            time.sleep(WRITE_GAP_S)
            for chunk in chunks:
                log("→ " + hexs(chunk))
                self.set_feature(chunk)
                time.sleep(WRITE_GAP_S)

    def start_status_reader(self, on_event: Callable[[StatusEvent], None],
                            on_gone: Callable[[], None]) -> None:
        def run() -> None:
            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([self.fd], [], [], 0.5)
                    if not ready:
                        continue
                    data = os.read(self.fd, 64)
                except BlockingIOError:
                    continue
                except OSError:
                    if not self._stop.is_set():
                        on_gone()
                    return
                event = parse_status(data)
                if event:
                    on_event(event)

        self._reader = threading.Thread(target=run, name="x3-status", daemon=True)
        self._reader.start()
