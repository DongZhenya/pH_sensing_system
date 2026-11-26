"""Wire protocol shared with the CC2340 Data Stream firmware."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from decimal import Decimal

SERVICE_UUID = "f000c0c0-0451-4000-b000-000000000000"
WRITE_UUID = "f000c0c1-0451-4000-b000-000000000000"
NOTIFY_UUID = "f000c0c2-0451-4000-b000-000000000000"

GAINS = (25, 50, 100, 200)
MIN_INTERVAL_MS = 100
MAX_INTERVAL_MS = 24000


@dataclass(frozen=True)
class Sweep:
    gain_state: int
    sequence: int
    millivolts: tuple[int | None, ...]


@dataclass(frozen=True)
class Settings:
    gain_state: int
    interval_ms: int


@dataclass(frozen=True)
class DeviceError:
    code: str


@dataclass(frozen=True)
class Sample:
    gain_state: int
    sequence: int
    channel: int
    millivolts: int | None
    device_time_ms: int
    cycle: int


@dataclass(frozen=True)
class ScanSettings:
    interval_ms: int
    channel_mask: int
    gains: tuple[int, ...]
    running: bool | None = None  # None: older firmware without Start support.


Message = Sweep | Settings | Sample | ScanSettings | DeviceError
_ACK = re.compile(r"^ACK,G=([0-3]),S=(\d{3,5})$")


def decode_notification(payload: bytes) -> Message:
    """Decode one complete GATT notification; never treat partial frames as data."""
    if len(payload) == 15 and payload[0] == 0xA5 and payload[3] == 2:
        gain, sequence, channel = payload[1], payload[2], payload[4]
        if gain >= len(GAINS) or channel >= 8:
            raise ValueError("Invalid channel or gain in measurement")
        value, device_time_ms, cycle = struct.unpack_from("<HII", payload, 5)
        return Sample(gain, sequence, channel, None if value == 0xFFFF else value,
                      device_time_ms, cycle)
    if len(payload) in (13, 14) and payload[:2] == b"\xc4\x02":
        mask = payload[2]
        interval = struct.unpack_from("<H", payload, 3)[0]
        gains = tuple(payload[5:13])
        configuration_command(interval, mask, gains)  # Validate reply too.
        if len(payload) == 14 and payload[13] not in (0, 1):
            raise ValueError("Invalid sampling run state")
        return ScanSettings(interval, mask, gains, bool(payload[13]) if len(payload) == 14 else None)
    if len(payload) == 20 and payload[0] == 0xA5:
        if payload[3] != 1:
            raise ValueError(f"Unsupported measurement frame version {payload[3]}")
        gain = payload[1]
        if gain >= len(GAINS):
            raise ValueError(f"Invalid gain state {gain}")
        raw = struct.unpack_from("<8H", payload, 4)
        return Sweep(gain, payload[2], tuple(None if x == 0xFFFF else x for x in raw))

    try:
        text = payload.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("Unknown non-ASCII notification") from exc
    match = _ACK.fullmatch(text)
    if match:
        gain, interval = map(int, match.groups())
        if not MIN_INTERVAL_MS <= interval <= MAX_INTERVAL_MS:
            raise ValueError("Invalid sampling interval in device reply")
        return Settings(gain, interval)
    if text.startswith("ERR,"):
        return DeviceError(text[4:])
    raise ValueError(f"Unrecognized device notification: {text[:32]}")


def gain_command(state: int) -> bytes:
    if not 0 <= state < len(GAINS):
        raise ValueError("Gain state must be 0..3")
    return f"G={state}".encode("ascii")


def run_command(running: bool) -> bytes:
    return b"R=1" if running else b"R=0"


def interval_command(milliseconds: int) -> bytes:
    if not MIN_INTERVAL_MS <= milliseconds <= MAX_INTERVAL_MS:
        raise ValueError("Scan interval must be 100..24000 ms")
    return f"S={milliseconds}".encode("ascii")


def configuration_command(milliseconds: int, channel_mask: int,
                          gains: tuple[int, ...]) -> bytes:
    """One atomic command fits the default 20-byte ATT payload."""
    if not MIN_INTERVAL_MS <= milliseconds <= MAX_INTERVAL_MS:
        raise ValueError("Sampling interval must be between 0.1 and 24 seconds")
    if not 0 <= channel_mask <= 255 or len(gains) != 8 or any(g not in range(4) for g in gains):
        raise ValueError("Invalid channel mask or per-channel gain")
    return b"\xc3\x02" + bytes((channel_mask,)) + struct.pack("<H", milliseconds) + bytes(gains)


def parse_interval_seconds(value: str) -> int:
    """Convert a user-entered number of seconds to exact whole milliseconds."""
    value = value.strip()
    if not re.fullmatch(r"\d+(?:\.\d{1,3})?", value):
        raise ValueError("Enter a number with up to three decimal places, such as 0.1 or 1.25")
    milliseconds = Decimal(value) * 1000
    if not MIN_INTERVAL_MS <= milliseconds <= MAX_INTERVAL_MS:
        raise ValueError("Sampling interval must be between 0.1 and 24 seconds")
    return int(milliseconds)
