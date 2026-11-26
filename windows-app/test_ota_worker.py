"""Offline OTA transfer tests; no radio or event-loop socket is required."""

import queue
import struct
import unittest

from ble_worker import BleWorker
from ota_protocol import validate_image
from test_ota_protocol import example_image


class FakeCharacteristic:
    max_write_without_response_size = 20


class FakeClient:
    mtu_size = 23
    is_connected = True

    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, characteristic, payload, *, response):
        self.writes.append((characteristic, bytes(payload), response))


def drive(coroutine):
    try:
        while True:
            coroutine.send(None)
    except StopIteration:
        pass


def make_worker(responses):
    worker = BleWorker.__new__(BleWorker)
    worker.events = queue.Queue()
    worker.client = FakeClient()
    worker.oad_identify = object()
    worker.oad_block = FakeCharacteristic()
    worker.oad_control = object()
    worker.ota_queue = None
    worker.ota_cancel_requested = False
    iterator = iter(responses)

    async def next_oad(_kind, _opcode=None, timeout=30.0):
        return next(iterator)

    worker._next_oad = next_oad
    return worker


class OtaWorkerTests(unittest.TestCase):
    def test_complete_request_driven_transfer(self):
        image = validate_image(example_image())
        blocks = (len(image.payload) + 15) // 16
        responses = [b"\x01\x14\x00", b"\x07\x01\x00\x00\x00\x00\x00", b"\x00"]
        responses += [b"\x12\x00" + struct.pack("<I", number)
                      for number in range(blocks)]
        responses += [b"\x12\x0e\xff\xff\xff\xff", b"\x04\x00"]
        worker = make_worker(responses)
        drive(worker._run_ota(image))
        events = list(worker.events.queue)
        self.assertIn(("ota_done", ("accepted", "Upgrade accepted; wait for restart and reconnect to verify")), events)
        writes = worker.client.writes
        self.assertEqual(writes[0][1], b"\x01")
        self.assertEqual(writes[1][1], b"\x07")
        self.assertEqual(writes[2][1], image.identify_header)
        self.assertTrue(writes[2][2])  # Identify uses GATT write with response.
        self.assertEqual(writes[3][1], b"\x03")
        self.assertEqual(writes[-1][1], b"\x04")
        self.assertEqual(len(writes), blocks + 5)
        for number, write in enumerate(writes[4:-1]):
            self.assertEqual(write[1], struct.pack("<I", number) + image.payload[number*16:(number+1)*16])
            self.assertFalse(write[2])

    def test_accept_sdk_completion_block_number(self):
        image = validate_image(example_image())
        blocks = (len(image.payload) + 15) // 16
        responses = [b"\x01\x14\x00", b"\x07\x01\x00\x00\x00\x00\x00", b"\x00"]
        responses += [b"\x12\x00" + struct.pack("<I", number)
                      for number in range(blocks)]
        responses += [b"\x12\x0e" + struct.pack("<I", blocks), b"\x04\x00"]
        worker = make_worker(responses)
        drive(worker._run_ota(image))
        self.assertEqual(list(worker.events.queue)[-1][1][0], "accepted")

    def test_reject_out_of_order_block(self):
        worker = make_worker((b"\x01\x14\x00", b"\x07\x01\x00\x00\x00\x00\x00",
                              b"\x00", b"\x12\x00\x01\x00\x00\x00"))
        drive(worker._run_ota(validate_image(example_image())))
        self.assertEqual(list(worker.events.queue)[-1][0], "ota_done")
        self.assertEqual(list(worker.events.queue)[-1][1][0], "error")
        self.assertNotIn(b"\x04", [write[1] for write in worker.client.writes])
        self.assertEqual(worker.client.writes[-1][1], b"\x05")

    def test_reject_block_larger_than_windows_mtu(self):
        worker = make_worker((b"\x01\xf0\x00",))
        drive(worker._run_ota(validate_image(example_image())))
        self.assertEqual(list(worker.events.queue)[-1][1][0], "error")
        self.assertEqual(len(worker.client.writes), 1)

    def test_reject_non_newer_image(self):
        worker = make_worker((b"\x01\x14\x00", b"\x07\x03\x00\x00\x00\x00\x00"))
        drive(worker._run_ota(validate_image(example_image())))
        self.assertEqual(list(worker.events.queue)[-1][1][0], "error")
        self.assertEqual([write[1] for write in worker.client.writes], [b"\x01", b"\x07"])


if __name__ == "__main__":
    unittest.main()
