"""BLE operations run on one dedicated asyncio thread; Tk never calls Bleak."""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from typing import Any

from bleak import BleakClient, BleakScanner

from protocol import NOTIFY_UUID, SERVICE_UUID, WRITE_UUID, decode_notification
from ota_protocol import (
    OAD_BLOCK_UUID, OAD_CONTROL_UUID, OAD_IDENTIFY_UUID, OAD_SERVICE_UUID,
    OtaImage, make_block, parse_block_request, parse_block_size, parse_software_version,
)


class OtaCancelled(Exception):
    pass


class OtaDisconnected(Exception):
    pass


class BleWorker:
    def __init__(self, events: queue.Queue[tuple[str, Any]]) -> None:
        self.events = events
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="PhSense-BLE", daemon=True)
        self.started = threading.Event()
        self.client: BleakClient | None = None
        self.writer = None
        self.disconnect_event: asyncio.Event | None = None
        self.write_lock: asyncio.Lock | None = None
        self.connection_future: concurrent.futures.Future | None = None
        self.ota_future: concurrent.futures.Future | None = None
        self.ota_queue: asyncio.Queue[tuple[str, bytes]] | None = None
        self.ota_cancel_requested = False
        self.oad_identify = None
        self.oad_block = None
        self.oad_control = None
        self.thread.start()
        self.started.wait(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.write_lock = asyncio.Lock()
        self.started.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def _submit(self, coroutine) -> concurrent.futures.Future:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def report_error(done: concurrent.futures.Future) -> None:
            try:
                done.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception as exc:
                self.events.put(("error", str(exc)))

        future.add_done_callback(report_error)
        return future

    def scan(self) -> None:
        self._submit(self._scan())

    async def _scan(self) -> None:
        self.events.put(("status", "Scanning for Bluetooth devices…"))
        try:
            found = await BleakScanner.discover(timeout=8.0, return_adv=True)
            devices = []
            for device, advertisement in found.values():
                name = advertisement.local_name or device.name or "Unnamed device"
                rssi = advertisement.rssi
                devices.append((name, device.address, rssi, device))
            devices.sort(key=lambda item: ("data stream" not in item[0].lower(), -item[2]))
            self.events.put(("devices", devices))
            self.events.put(("status", f"Scan complete: {len(devices)} devices found"))
        finally:
            self.events.put(("scan_done", None))

    def connect(self, device) -> None:
        if self.connection_future and not self.connection_future.done():
            self.events.put(("status", "Disconnect the current device first"))
            return
        self.connection_future = self._submit(self._connect(device))

    async def _connect(self, device) -> None:
        self.events.put(("status", f"Connecting to {device.address}…"))
        self.disconnect_event = asyncio.Event()
        client = BleakClient(device, disconnected_callback=self._on_disconnected, timeout=20.0)
        try:
            await client.connect()
            service = client.services.get_service(SERVICE_UUID)
            if service is None:
                raise RuntimeError("Data Stream service not found. Select the CC2340 or check its firmware")
            writer = service.get_characteristic(WRITE_UUID)
            notifier = service.get_characteristic(NOTIFY_UUID)
            if writer is None or notifier is None:
                raise RuntimeError("Data Stream characteristics are incomplete; check the firmware")
            await client.start_notify(notifier, self._on_notification)
            oad_service = client.services.get_service(OAD_SERVICE_UUID)
            identify = oad_service.get_characteristic(OAD_IDENTIFY_UUID) if oad_service else None
            block = oad_service.get_characteristic(OAD_BLOCK_UUID) if oad_service else None
            control = oad_service.get_characteristic(OAD_CONTROL_UUID) if oad_service else None
            oad_reason = "TI OAD is unavailable; install a matching bootloader and OAD firmware via a wired debugger"
            if identify is not None and block is not None and control is not None:
                try:
                    await client.start_notify(identify, self._on_oad_notification)
                    await client.start_notify(control, self._on_oad_notification)
                except Exception as exc:
                    oad_reason = f"Could not enable OAD notifications: {exc}"
                    try:
                        await client.stop_notify(identify)
                    except Exception:
                        pass
                else:
                    self.oad_identify = identify
                    self.oad_block = block
                    self.oad_control = control
                    oad_reason = "TI OAD service detected"
            self.client = client
            self.writer = writer
            self.events.put(("connected", device.address))
            self.events.put(("ota_available", (self.oad_control is not None, oad_reason)))
            self.events.put(("status", "Connected to CC2340; reading sampling settings…"))
            await self._write(b"Q")
            await self.disconnect_event.wait()
        finally:
            self.writer = None
            self.client = None
            self.oad_identify = None
            self.oad_block = None
            self.oad_control = None
            try:
                if client.is_connected:
                    await client.disconnect()
            finally:
                self.disconnect_event = None
                self.events.put(("disconnected", None))

    def _on_disconnected(self, _client: BleakClient) -> None:
        if self.disconnect_event is not None:
            self.loop.call_soon_threadsafe(self.disconnect_event.set)
        if self.ota_queue is not None:
            self.loop.call_soon_threadsafe(self.ota_queue.put_nowait, ("disconnected", b""))

    def _on_notification(self, _characteristic, data: bytearray) -> None:
        try:
            message = decode_notification(bytes(data))
            self.events.put(("notification", message))
        except ValueError as exc:
            self.events.put(("protocol_warning", str(exc)))

    def _on_oad_notification(self, characteristic, data: bytearray) -> None:
        transfer_queue = self.ota_queue
        if transfer_queue is not None:
            kind = "identify" if characteristic.uuid.lower() == OAD_IDENTIFY_UUID else "control"
            self.loop.call_soon_threadsafe(transfer_queue.put_nowait, (kind, bytes(data)))

    def send(self, payload: bytes) -> None:
        if self.ota_future is not None and not self.ota_future.done():
            self.events.put(("error", "Cannot change measurement settings during an upgrade"))
            return
        self._submit(self._write(payload))

    def start_ota(self, image: OtaImage) -> None:
        if self.ota_future is not None and not self.ota_future.done():
            self.events.put(("ota_done", ("error", "An upgrade is already in progress")))
            return
        if self.client is None or self.oad_control is None:
            self.events.put(("ota_done", ("error", "Not connected to a TI OAD device")))
            return
        self.ota_future = self._submit(self._run_ota(image))

    def cancel_ota(self) -> None:
        if self.ota_future is not None and not self.ota_future.done():
            self.loop.call_soon_threadsafe(self._request_ota_cancel)

    def _request_ota_cancel(self) -> None:
        self.ota_cancel_requested = True
        if self.ota_queue is not None:
            self.ota_queue.put_nowait(("cancel", b""))

    async def _next_oad(self, kind: str, opcode: int | None = None,
                        timeout: float = 30.0) -> bytes:
        assert self.ota_queue is not None
        deadline = self.loop.time() + timeout
        while True:
            if self.ota_cancel_requested:
                raise OtaCancelled()
            remaining = deadline - self.loop.time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for an OTA response")
            item_kind, data = await asyncio.wait_for(self.ota_queue.get(), remaining)
            if item_kind == "cancel":
                raise OtaCancelled()
            if item_kind == "disconnected":
                raise OtaDisconnected()
            if item_kind != kind:
                raise ValueError("Unexpected OTA notification")
            if not data:
                raise ValueError("Empty OTA notification")
            if data[0] == 0xFF:
                raise ValueError("The device does not support this OTA command")
            if opcode is not None and data[0] != opcode:
                raise ValueError(f"Unexpected OTA response 0x{data[0]:02x}")
            return data

    async def _run_ota(self, image: OtaImage) -> None:
        client = self.client
        identify = self.oad_identify
        block = self.oad_block
        control = self.oad_control
        if client is None or identify is None or block is None or control is None:
            self.events.put(("ota_done", ("error", "OAD service disconnected")))
            return
        self.ota_queue = asyncio.Queue()
        self.ota_cancel_requested = False
        started = False
        enable_sent = False
        try:
            self.events.put(("ota_progress", (0, "Querying transfer block size…")))
            await client.write_gatt_char(control, b"\x01", response=False)
            target_block_size = parse_block_size(await self._next_oad("control", 0x01))
            max_write = min(client.mtu_size - 3, block.max_write_without_response_size)
            if target_block_size > max_write:
                raise ValueError(
                    f"Device requires {target_block_size}-byte blocks, but Windows allows {max_write} bytes. "
                    "Check the Bluetooth adapter/MTU; the block size cannot be reduced arbitrarily"
                )
            data_bytes_per_block = target_block_size - 4
            total_blocks = (len(image.payload) + data_bytes_per_block - 1) // data_bytes_per_block

            await client.write_gatt_char(control, b"\x07", response=False)
            current_version = parse_software_version(await self._next_oad("control", 0x07))
            current_text = (f"{current_version[0]}.{current_version[1]}."
                            f"{current_version[2]}+{current_version[3]}")
            if image.version_numbers <= current_version:
                raise ValueError(f"Device version {current_text} is not older than image version {image.version}; upgrade rejected")
            self.events.put(("ota_progress", (0, f"Device version {current_text} → Image version {image.version}")))

            self.events.put(("ota_progress", (0, "Sending image header; waiting for acceptance…")))
            await client.write_gatt_char(identify, image.identify_header, response=True)
            response = await self._next_oad("identify")
            if len(response) != 1 or response[0] != 0:
                raise ValueError(f"Image rejected; status {response.hex()}")

            await client.write_gatt_char(control, b"\x03", response=False)
            started = True
            expected = 0
            last_percent = -1
            while True:
                response = await self._next_oad("control", timeout=45.0)
                if response[0] == 0x03:
                    if len(response) < 2 or response[1] != 0:
                        raise ValueError(f"Device rejected upgrade start: {response.hex()}")
                    continue
                status, number = parse_block_request(response)
                if status == 14:
                    # SDK 9.21's local profile reports currentBlkNum here;
                    # TI's published OAD protocol also allows 0xFFFFFFFF.
                    if number not in (total_blocks, 0xFFFFFFFF) or expected != total_blocks:
                        raise ValueError("Device reported completion before the full image was transferred")
                    break
                if status != 0:
                    raise ValueError(f"Block rejected: status {status}, block {number}")
                if number != expected:
                    raise ValueError(f"Device requested block {number}; expected {expected}. Transfer stopped")
                payload = make_block(image, number, data_bytes_per_block)
                await client.write_gatt_char(block, payload, response=False)
                expected += 1
                percent = min(99, 100 * expected // total_blocks)
                if percent != last_percent:
                    last_percent = percent
                    self.events.put(("ota_progress", (percent, f"Transferring firmware {percent}% ({expected}/{total_blocks} blocks)")))

            self.events.put(("ota_progress", (99, "All data received; verifying and enabling…")))
            await client.write_gatt_char(control, b"\x04", response=False)
            enable_sent = True
            response = await self._next_oad("control", 0x04, timeout=15.0)
            if len(response) < 2 or response[1] != 0:
                raise ValueError(f"Device did not confirm image activation: {response.hex()}")
            self.events.put(("ota_progress", (100, "Upgrade accepted; reconnect after restart to verify")))
            self.events.put(("ota_done", ("accepted", "Upgrade accepted; wait for restart and reconnect to verify")))
        except OtaCancelled:
            try:
                if client.is_connected:
                    await client.write_gatt_char(control, b"\x05", response=False)
            except Exception:
                pass
            self.events.put(("ota_done", ("cancelled", "Cancellation requested; verify the device can still connect")))
        except OtaDisconnected:
            self.events.put(("ota_done", ("error", "Disconnected during upgrade; reconnect to check the device")))
        except Exception as exc:
            if started and not enable_sent:
                try:
                    if client.is_connected:
                        await client.write_gatt_char(control, b"\x05", response=False)
                except Exception:
                    pass
            self.events.put(("ota_done", ("error", f"Upgrade failed: {exc}. Reconnect to check the device")))
        finally:
            self.ota_queue = None
            self.ota_cancel_requested = False

    async def _write(self, payload: bytes) -> None:
        if self.client is None or self.writer is None or not self.client.is_connected:
            raise RuntimeError("Device disconnected; command not sent")
        assert self.write_lock is not None
        async with self.write_lock:
            await self.client.write_gatt_char(self.writer, payload, response=True)

    def disconnect(self) -> None:
        if self.disconnect_event is not None:
            self.loop.call_soon_threadsafe(self.disconnect_event.set)

    def close(self) -> None:
        self.cancel_ota()
        self.disconnect()
        if self.ota_future and not self.ota_future.done():
            try:
                self.ota_future.result(timeout=3)
            except Exception:
                pass
        if self.connection_future and not self.connection_future.done():
            try:
                self.connection_future.result(timeout=3)
            except (concurrent.futures.TimeoutError, Exception):
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3)
