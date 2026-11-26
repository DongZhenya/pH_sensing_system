"""Desktop integration checks with a simulated BLE device (no radio needed)."""

import csv
import io
import tkinter as tk
import unittest
from unittest.mock import patch

from app import CSV_HEADER, PhSenseApp
from protocol import Sample, ScanSettings, configuration_command, decode_notification


class AppTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.worker_patch = patch("app.BleWorker")
        self.worker = self.worker_patch.start().return_value
        self.app = PhSenseApp(self.root)
        self.app.connected = True
        self.app._handle_message(ScanSettings(1000, 255, (0,) * 8, False))

    def tearDown(self):
        self.root.update_idletasks()
        for task in self.root.tk.call("after", "info"):
            self.root.after_cancel(task)
        self.root.destroy()
        self.worker_patch.stop()

    def test_channel_selection_and_independent_gains(self):
        for ch, value in enumerate((True, False, True, False, False, False, False, True)):
            self.app.channel_vars[ch].set(value)
        self.app.gain_vars[0].set("25×")
        self.app.gain_vars[2].set("100×")
        self.app.gain_vars[7].set("200×")
        self.app.interval_entry_var.set("24")
        self.app._apply_manual_interval()
        self.worker.send.assert_called_with(configuration_command(24000, 0x85, (0, 0, 2, 0, 0, 0, 0, 3)))
        self.assertIn("8 s", self.app.schedule_var.get())
        self.app._handle_message(ScanSettings(1000, 255, (0,) * 8, False))
        self.assertEqual(self.app.interval_ms, 24000)  # Ignore obsolete ACK.
        self.app._handle_message(ScanSettings(24000, 0x85, (0, 0, 2, 0, 0, 0, 0, 3), False))
        self.assertIsNone(self.app.pending_settings)

    def test_exact_manual_milliseconds_and_pause(self):
        self.app.interval_entry_var.set("1.234")
        self.app._apply_manual_interval()
        self.assertEqual(self.app.pending_settings.interval_ms, 1234)
        for value in self.app.channel_vars:
            value.set(False)
        self.app._send_configuration()
        self.assertEqual(self.app.pending_settings.channel_mask, 0)
        self.assertIn("paused", self.app.schedule_var.get())

    def test_sample_updates_only_own_chart_and_csv_gain(self):
        self.app._handle_message(ScanSettings(1000, 255, (0,) * 8, True))
        self.app.csv_file = io.StringIO()
        self.app.csv_writer = csv.writer(self.app.csv_file)
        self.app.csv_writer.writerow(CSV_HEADER)
        for sample in (Sample(3, 255, 7, 1234, 24000, 3), Sample(1, 0, 2, 654, 32000, 3)):
            self.app._handle_message(sample)
        self.assertEqual(len(self.app.charts[7].points), 1)
        self.assertEqual(len(self.app.charts[2].points), 1)
        self.assertEqual(len(self.app.charts[0].points), 0)
        rows = list(csv.DictReader(io.StringIO(self.app.csv_file.getvalue())))
        self.assertEqual([r["channel"] for r in rows], ["7", "2"])
        self.assertEqual([r["gain_v_per_v"] for r in rows], ["200", "50"])
        self.assertEqual([r["device_time_ms"] for r in rows], ["24000", "32000"])
        self.assertEqual(self.app.recorded_rows, 2)

    def test_legacy_firmware_cannot_receive_new_commands(self):
        self.app.protocol_v2 = False
        self.app._set_measurement_controls(True)
        self.app._send_configuration()
        self.worker.send.assert_not_called()
        self.assertEqual(str(self.app.gain_boxes[0]["state"]), "disabled")

    def test_independent_chart_draws_line_and_breaks_at_adc_error(self):
        chart = self.app.charts[3]
        import time
        now = time.monotonic()
        for when, value in ((now - 4, 100), (now - 3, 200), (now - 2, None), (now - 1, 300)):
            chart.add_sample(when, value)
        with patch.object(chart, "winfo_width", return_value=450), patch.object(chart, "winfo_height", return_value=160):
            chart.redraw()
        colored = [item for item in chart.find_all() if chart.itemcget(item, "fill") == "#43a047"]
        self.assertEqual([chart.type(item) for item in colored], ["line", "oval"])

    def test_start_waits_for_configuration_then_run_ack(self):
        sample = Sample(0, 1, 0, 900, 1000, 1)
        self.app._handle_message(sample)
        self.assertEqual(len(self.app.history), 0)
        self.app._start_sampling()
        self.assertTrue(self.app.start_requested)
        self.assertFalse(self.app.sampling_running)
        self.worker.send.assert_called_with(configuration_command(1000, 255, (0,) * 8))
        self.app._handle_message(ScanSettings(1000, 255, (0,) * 8, False))
        self.worker.send.assert_called_with(b"R=1")
        self.assertTrue(self.app.pending_run)
        self.app._handle_message(ScanSettings(1000, 255, (0,) * 8, True))
        self.app._handle_message(sample)
        self.assertEqual(len(self.app.history), 1)
        self.assertFalse(self.app.pending_run)
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_clear_resets_all_plots_but_continues_sampling_and_preserves_csv(self):
        self.app._handle_message(ScanSettings(1000, 255, (0,) * 8, True))
        self.app.csv_file = io.StringIO()
        self.app.csv_writer = csv.writer(self.app.csv_file)
        self.app._handle_message(Sample(1, 1, 3, 50, 1000, 1))
        saved = self.app.csv_file.getvalue()
        self.app._clear_data()
        self.assertTrue(self.app.sampling_running)
        self.assertFalse(self.app.history)
        self.assertTrue(all(not c.points for c in self.app.charts))
        self.assertEqual(self.app.csv_file.getvalue(), saved)
        self.app._handle_message(Sample(2, 2, 7, 75, 2000, 1))
        self.assertEqual(len(self.app.history), 1)
        self.assertEqual(self.app.recorded_rows, 2)

    def test_accumulation_keeps_old_points_and_more_than_old_limits(self):
        self.app._handle_message(ScanSettings(100, 255, (0,) * 8, True))
        import time
        for index in range(6100):
            self.app._handle_message(Sample(0, index & 255, 0, index % 3300, index * 100, index))
        self.app.charts[0].points[0] = (time.monotonic() - 3600, 1)
        chart = self.app.charts[0]
        with patch.object(chart, "winfo_width", return_value=450), patch.object(chart, "winfo_height", return_value=160):
            chart.redraw()
        self.assertEqual(len(chart.points), 6100)
        self.assertEqual(len(self.app.history), 6100)

    def test_older_v2_firmware_without_start_is_disabled(self):
        self.app._handle_message(ScanSettings(1000, 255, (0,) * 8))
        self.assertFalse(self.app.start_supported)
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_old_and_new_points_are_drawn_without_compression(self):
        chart = self.app.charts[0]
        points = [(float(i), 100 + (i % 3) * 10) for i in range(130)]
        for timestamp, value in points:
            chart.add_sample(timestamp, value)
        self.assertEqual(list(chart._plot_points()), points)
        with patch.object(chart, "winfo_width", return_value=450), patch.object(chart, "winfo_height", return_value=160):
            chart.redraw()
        line = next(item for item in chart.find_all() if chart.type(item) == "line"
                    and chart.itemcget(item, "fill") == "#e53935")
        self.assertEqual(len(chart.coords(line)), 2 * len(points))
        for timestamp, value in [(130.0, 75), (131.0, 90)]:
            chart.add_sample(timestamp, value)
        self.assertEqual(list(chart._plot_points())[:130], points)

    def test_interface_strings_are_english(self):
        import re
        from pathlib import Path
        for filename in ("app.py", "ble_worker.py", "protocol.py", "ota_protocol.py"):
            self.assertIsNone(re.search(r"[\u4e00-\u9fff]", Path(__file__).with_name(filename).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
