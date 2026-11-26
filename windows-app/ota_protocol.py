"""TI CC23xx dual-image OAD framing and conservative MCUboot image checks."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

OAD_SERVICE_UUID = "f000ffc0-0451-4000-b000-000000000000"
OAD_IDENTIFY_UUID = "f000ffc1-0451-4000-b000-000000000000"
OAD_BLOCK_UUID = "f000ffc2-0451-4000-b000-000000000000"
OAD_CONTROL_UUID = "f000ffc5-0451-4000-b000-000000000000"

MAX_IMAGE_SIZE = 0x3B000  # This project's CC2340R53 dual-image slot.
_IMAGE_MAGICS = (b"\x3d\xb8\xf3\x96", b"\x3d\xb8\xf3\x97")
_SIGNATURE_TYPES = {0x20, 0x22, 0x23, 0x24}


@dataclass(frozen=True)
class OtaImage:
    payload: bytes
    version_numbers: tuple[int, int, int, int]
    sha256: str

    @property
    def version(self) -> str:
        major, minor, revision, build = self.version_numbers
        return f"{major}.{minor}.{revision}+{build}"

    @property
    def identify_header(self) -> bytes:
        # TI's dual-image OAD target uses ih_img_size to determine the number
        # of bytes to download. The transfer includes the signed TLV trailer.
        header = bytearray(self.payload[:18])
        struct.pack_into("<I", header, 12, len(self.payload))
        return bytes(header)


def validate_image(source: bytes) -> OtaImage:
    """Validate structure and trim slot padding; device verifies the signature.

    This does not cryptographically authenticate the image. The matching
    MCUboot instance on the target is responsible for that decision.
    """
    if not 0x120 <= len(source) <= MAX_IMAGE_SIZE:
        raise ValueError("Image size does not fit the dual-image partition")
    if source[:4] not in _IMAGE_MAGICS:
        raise ValueError("Unsupported MCUboot .bin image")

    header_size, protected_size = struct.unpack_from("<HH", source, 8)
    image_size = struct.unpack_from("<I", source, 12)[0]
    if header_size < 32 or image_size == 0:
        raise ValueError("Invalid MCUboot header length")
    tlv_at = header_size + image_size + protected_size
    if tlv_at + 4 > len(source):
        raise ValueError("Incomplete image data or signature area")
    magic, tlv_size = struct.unpack_from("<HH", source, tlv_at)
    if magic not in (0x6907, 0x6908) or tlv_size < 4:
        raise ValueError("MCUboot TLV signature area not found")
    signed_end = tlv_at + tlv_size
    if signed_end > len(source) or signed_end > MAX_IMAGE_SIZE:
        raise ValueError("MCUboot TLV length exceeds the file or partition")

    has_signature = False
    offset = tlv_at + 4
    while offset < signed_end:
        if offset + 4 > signed_end:
            raise ValueError("Incomplete MCUboot TLV entry")
        kind, _pad, length = struct.unpack_from("<BBH", source, offset)
        offset += 4
        if length == 0 or offset + length > signed_end:
            raise ValueError("Invalid MCUboot TLV entry length")
        if kind in _SIGNATURE_TYPES:
            has_signature = True
        offset += length
    if not has_signature:
        raise ValueError("Image signature entry missing")

    major, minor, revision, build = struct.unpack_from("<BBHI", source, 20)
    payload = source[:signed_end]
    return OtaImage(payload, (major, minor, revision, build),
                    hashlib.sha256(payload).hexdigest())


def parse_software_version(response: bytes) -> tuple[int, int, int, int]:
    # The CC23xx SDK 9.21 OAD implementation returns six version bytes:
    # major, minor, big-endian revision, big-endian truncated build number.
    if len(response) != 7 or response[0] != 0x07:
        raise ValueError("Unexpected OAD software version format")
    return (response[1], response[2],
            (response[3] << 8) | response[4],
            (response[5] << 8) | response[6])


def parse_block_size(response: bytes) -> int:
    if len(response) != 3 or response[0] != 0x01:
        raise ValueError("Invalid OAD block-size response")
    block_size = struct.unpack_from("<H", response, 1)[0]
    if not 5 <= block_size <= 244:
        raise ValueError("Invalid OAD block size")
    return block_size


def parse_block_request(response: bytes) -> tuple[int, int]:
    if len(response) != 6 or response[0] != 0x12:
        raise ValueError("Invalid OAD block request")
    return response[1], struct.unpack_from("<I", response, 2)[0]


def make_block(image: OtaImage, number: int, data_bytes_per_block: int) -> bytes:
    if data_bytes_per_block < 1 or number < 0:
        raise ValueError("Invalid OAD data block")
    offset = number * data_bytes_per_block
    if offset >= len(image.payload):
        raise ValueError("Requested block is outside the image")
    return struct.pack("<I", number) + image.payload[offset:offset + data_bytes_per_block]
