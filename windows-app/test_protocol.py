import struct
import unittest

from protocol import (
    DeviceError,
    Settings,
    Sweep,
    Sample,
    ScanSettings,
    configuration_command,
    decode_notification,
    gain_command,
    interval_command,
    parse_interval_seconds,
)


class ProtocolTests(unittest.TestCase):
    def test_complete_eight_channel_frame(self):
        frame = bytes((0xA5, 2, 255, 1)) + struct.pack(
            "<8H", 10, 20, 30, 40, 50, 60, 70, 80
        )
        self.assertEqual(
            decode_notification(frame),
            Sweep(2, 255, (10, 20, 30, 40, 50, 60, 70, 80)),
        )

    def test_failed_adc_is_missing_value(self):
        frame = bytes((0xA5, 0, 1, 1)) + struct.pack(
            "<8H", 0xFFFF, 1, 2, 3, 4, 5, 6, 7
        )
        self.assertIsNone(decode_notification(frame).millivolts[0])

    def test_reject_incomplete_frame(self):
        with self.assertRaises(ValueError):
            decode_notification(bytes((0xA5, 0, 1, 1)))

    def test_settings_and_error(self):
        self.assertEqual(decode_notification(b"ACK,G=3,S=10000"), Settings(3, 10000))
        self.assertEqual(decode_notification(b"ERR,DEBUG"), DeviceError("DEBUG"))

    def test_commands(self):
        self.assertEqual(gain_command(1), b"G=1")
        self.assertEqual(interval_command(100), b"S=100")
        self.assertEqual(interval_command(10000), b"S=10000")
        with self.assertRaises(ValueError):
            interval_command(99)

    def test_manual_interval_input(self):
        self.assertEqual(parse_interval_seconds("0.1"), 100)
        self.assertEqual(parse_interval_seconds(" 1.25 "), 1250)
        self.assertEqual(parse_interval_seconds("10"), 10000)
        self.assertEqual(parse_interval_seconds("24"), 24000)
        self.assertEqual(parse_interval_seconds("10.001"), 10001)
        for invalid in ("", "0.099", "24.001", "1.2345", "nan", "abc", "1,5"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                parse_interval_seconds(invalid)

    def test_v2_atomic_configuration_roundtrip(self):
        gains = (0, 1, 2, 3, 3, 2, 1, 0)
        for interval in (100, 1234, 24000):
            for mask in (0, 1, 0x85, 255):
                command = configuration_command(interval, mask, gains)
                self.assertEqual(len(command), 13)
                self.assertEqual(decode_notification(b"\xc4" + command[1:]),
                                 ScanSettings(interval, mask, gains))

    def test_v2_one_channel_sample(self):
        frame = bytes((0xA5, 3, 255, 2, 7)) + struct.pack("<HII", 1234, 24000, 19)
        self.assertEqual(decode_notification(frame), Sample(3, 255, 7, 1234, 24000, 19))
        error_frame = bytes((0xA5, 0, 0, 2, 0)) + struct.pack("<HII", 0xFFFF, 24013, 20)
        self.assertIsNone(decode_notification(error_frame).millivolts)
        for invalid in (frame[:-1], frame[:4] + b"\x08" + frame[5:],
                        b"\xa5\x04" + frame[2:], frame[:3] + b"\x03" + frame[4:]):
            with self.assertRaises(ValueError):
                decode_notification(invalid)

    def test_reject_bad_configuration(self):
        for interval, mask, gains in ((99, 255, (0,) * 8), (24001, 255, (0,) * 8),
                                      (100, 256, (0,) * 8), (100, 255, (4,) * 8),
                                      (100, 255, (0,) * 7)):
            with self.assertRaises(ValueError):
                configuration_command(interval, mask, gains)

    def test_start_capability_and_run_state(self):
        from protocol import run_command
        settings = configuration_command(1000, 255, (0,) * 8)
        for running in (0, 1):
            result = decode_notification(b"\xc4" + settings[1:] + bytes((running,)))
            self.assertIs(result.running, bool(running))
        self.assertEqual(run_command(True), b"R=1")
        self.assertEqual(run_command(False), b"R=0")
        with self.assertRaises(ValueError):
            decode_notification(b"\xc4" + settings[1:] + b"\x02")


if __name__ == "__main__":
    unittest.main()
