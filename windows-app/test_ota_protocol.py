import struct
import unittest
from pathlib import Path

from ota_protocol import (
    make_block, parse_block_request, parse_block_size, parse_software_version,
    validate_image,
)


def example_image() -> bytes:
    header = bytearray(32)
    header[:4] = b"\x3d\xb8\xf3\x96"
    struct.pack_into("<H", header, 8, 32)
    struct.pack_into("<I", header, 12, 256)
    struct.pack_into("<BBHI", header, 20, 2, 1, 0, 4)
    signature = struct.pack("<HH", 0x6907, 12) + struct.pack("<BBH", 0x22, 0, 4) + b"SIGN"
    return bytes(header) + b"A" * 256 + signature + b"\xff" * 64


class OtaProtocolTests(unittest.TestCase):
    def test_validate_and_trim_image(self):
        image = validate_image(example_image())
        self.assertEqual(len(image.payload), 300)
        self.assertEqual(image.version, "2.1.0+4")
        self.assertEqual(struct.unpack_from("<I", image.identify_header, 12)[0], 300)
        self.assertEqual(make_block(image, 1, 16), struct.pack("<I", 1) + image.payload[16:32])

    def test_reject_bad_image(self):
        with self.assertRaises(ValueError):
            validate_image(b"not a firmware image")
        broken = bytearray(example_image())
        broken[292] = 0xff  # Corrupt first TLV entry's length/shape.
        with self.assertRaises(ValueError):
            validate_image(bytes(broken))

    def test_block_responses(self):
        self.assertEqual(parse_block_size(b"\x01\xf0\x00"), 240)
        self.assertEqual(parse_block_request(b"\x12\x00\x05\x00\x00\x00"), (0, 5))
        for invalid in (b"\x01", b"\x01\x00\x00", b"\x02\xf0\x00"):
            with self.subTest(response=invalid), self.assertRaises(ValueError):
                parse_block_size(invalid)
        self.assertEqual(parse_software_version(b"\x07\x01\x02\x00\x03\x00\x04"),
                         (1, 2, 3, 4))
        with self.assertRaises(ValueError):
            parse_software_version(b"\x07\x01\x02\x03\x04")

    def test_project_image(self):
        project_image = (Path(__file__).resolve().parents[1] / "ota_firmware" /
                         "freertos" / "ticlang" / "phsense_ota_v2.bin")
        if project_image.exists():
            image = validate_image(project_image.read_bytes())
            self.assertTrue(image.payload)
            self.assertEqual(image.version.split(".")[0], "2")


if __name__ == "__main__":
    unittest.main()
