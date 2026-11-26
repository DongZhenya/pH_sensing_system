"""PhSense desktop app for Windows and the CC2340 Data Stream firmware."""

from __future__ import annotations

import csv
import queue
import struct
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ble_worker import BleWorker
from ota_protocol import MAX_IMAGE_SIZE, OtaImage, validate_image
from protocol import (
    DeviceError, GAINS, Sample, ScanSettings, Settings, Sweep,
    configuration_command, parse_interval_seconds, run_command,
)

BG = "#f3f7fb"
WHITE = "#ffffff"
INK = "#173247"
MUTED = "#587084"
BLUE = "#1768b7"
COLORS = (
    "#e53935", "#fb8c00", "#c5a000", "#43a047",
    "#00acc1", "#1e88e5", "#8e24aa", "#d81b60",
)
CSV_HEADER = (
    "sample", "pc_timestamp", "device_time_ms", "cycle", "device_sequence", "channel",
    "gain_v_per_v", "vsen_mv", "valid",
)


class TrendChart(tk.Canvas):
    def __init__(self, parent: tk.Misc, channel: int) -> None:
        super().__init__(parent, background=WHITE, highlightthickness=0)
        self.channel = channel
        self.points: list[tuple[float, int | None]] = []
        self.start_time: float | None = None
        self.end_time: float | None = None
        self.minimum: int | None = None
        self.maximum: int | None = None
        self._scheduled = False
        self._draw_key = None
        self.bind("<Configure>", lambda _event: self.request_draw())

    def add_sample(self, timestamp: float, value: int | None) -> None:
        self.points.append((timestamp, value))
        if value is not None:
            self.minimum = value if self.minimum is None else min(self.minimum, value)
            self.maximum = value if self.maximum is None else max(self.maximum, value)

    def clear(self) -> None:
        self.points.clear()
        self._draw_key = None
        self.minimum = self.maximum = None
        self.start_time = self.end_time = None
        self.request_draw()

    def _plot_points(self):
        # Draw every original point, including historical data and ADC gaps.
        yield from self.points

    def request_draw(self) -> None:
        if not self._scheduled:
            self._scheduled = True
            self.after_idle(self.redraw)

    def redraw(self) -> None:
        self._scheduled = False
        width = self.winfo_width()
        height = self.winfo_height()
        if width < 120 or height < 65:
            return
        left, right, top, bottom = 55, width - 14, 16, height - 26
        start = self.start_time if self.start_time is not None else (self.points[0][0] if self.points else 0)
        end = self.end_time if self.end_time is not None else (self.points[-1][0] if self.points else start)
        span = max(1.0, end - start)
        draw_key = (width, height, len(self.points), start, end, self.minimum, self.maximum)
        if draw_key == self._draw_key:
            return
        self._draw_key = draw_key
        self.delete("all")

        if self.minimum is not None:
            minimum, maximum = self.minimum, self.maximum
            margin = max(20, (maximum - minimum) // 8)
            minimum = max(0, minimum - margin)
            maximum += margin
            if maximum <= minimum:
                maximum = minimum + 1
        else:
            minimum, maximum = 0, 3300

        for step in range(5):
            y = top + (bottom - top) * step / 4
            value = maximum - (maximum - minimum) * step / 4
            self.create_line(left, y, right, y, fill="#e5edf3")
            self.create_text(left - 8, y, text=f"{value:.0f}", anchor="e", fill=MUTED, font=("Segoe UI", 9))
        self.create_text(12, 8, text="mV", anchor="nw", fill=MUTED, font=("Segoe UI", 9))
        for step in range(5):
            x = left + (right - left) * step / 4
            self.create_line(x, top, x, bottom, fill="#f0f3f7")
            label = f"{span * step / 4:.3g} s"
            self.create_text(x, bottom + 14, text=label, fill=MUTED, font=("Segoe UI", 9))

        def draw_segment(coords: list[float]) -> None:
            if len(coords) >= 4:
                self.create_line(*coords, fill=COLORS[self.channel], width=2)
            elif coords:
                x, y = coords
                self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=COLORS[self.channel], outline="")

        coords: list[float] = []
        for timestamp, value in self._plot_points():
            if value is None:
                draw_segment(coords)
                coords = []
                continue
            x = left + (right - left) * (timestamp - start) / span
            y = bottom - (bottom - top) * (value - minimum) / (maximum - minimum)
            coords.extend((x, y))
        draw_segment(coords)


class PhSenseApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("pH Sense · CC2340 · Channel Sampling")
        root.geometry("1440x940")
        root.minsize(1100, 740)
        root.configure(bg=BG)

        self.events: queue.Queue = queue.Queue()
        self.worker = BleWorker(self.events)
        self.devices: list[tuple] = []
        self.connected = False
        self.sample_number = 0
        self.last_sequence: int | None = None
        self.history: list[tuple[float, int, str, Sample]] = []
        self.plot_origin: float | None = None
        self.latest_plot_time: float | None = None
        self.sampling_running = False
        self.start_supported = False
        self.start_requested = False
        self.pending_run = False
        self.protocol_v2 = False
        self.pending_settings: ScanSettings | None = None
        self.applied_settings: ScanSettings | None = None
        self.settings_request_id = 0
        self.csv_file = None
        self.csv_writer = None
        self.csv_path: Path | None = None
        self.recorded_rows = 0
        self.ota_available = False
        self.ota_reason = "Not connected"
        self.ota_image: OtaImage | None = None
        self.ota_path: Path | None = None
        self.ota_active = False
        self.ota_window: tk.Toplevel | None = None

        self.gain_vars = [tk.StringVar(value="25×") for _ in range(8)]
        self.channel_vars = [tk.BooleanVar(value=True) for _ in range(8)]
        self.interval_var = tk.DoubleVar(value=1.0)
        self.interval_ms = 1000
        self.interval_entry_var = tk.StringVar(value="1.0")
        self.status_var = tk.StringVar(value="Disconnected. Power on the CC2340 and scan.")
        self.interval_label_var = tk.StringVar(value="Total wait per cycle T: 1 s")
        self.schedule_var = tk.StringVar(value="8 channels selected; wait 0.125 s per channel")
        self.record_var = tk.StringVar(value="Not recording")
        self.ota_service_var = tk.StringVar(value="OAD service: Not connected")
        self.ota_file_var = tk.StringVar(value="No firmware selected")
        self.ota_status_var = tk.StringVar(value="Select a matching MCUboot signed .bin file")
        self.ota_progress_var = tk.IntVar(value=0)
        self.value_vars = [tk.StringVar(value="— mV") for _ in range(8)]

        self._build_ui()
        root.after(50, self._process_events)
        root.after(500, self._refresh_charts)
        root.protocol("WM_DELETE_WINDOW", self._close)

    @staticmethod
    def _section(parent: tk.Misc, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=12)
        frame.pack(fill="x", pady=(0, 14))
        return frame

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabelframe", background=WHITE, bordercolor="#d9e3eb")
        style.configure("TLabelframe.Label", background=BG, foreground=INK, font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background=WHITE, foreground=INK, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=(8, 7))
        style.configure("TRadiobutton", background=WHITE, foreground=INK, font=("Segoe UI", 10))

        body = ttk.Frame(self.root, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(body, width=300)
        sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 18))
        sidebar.pack_propagate(False)
        sidebar.grid_propagate(False)

        heading = tk.Label(sidebar, text="pH Sense", bg=BG, fg=INK, font=("Segoe UI", 23, "bold"), anchor="w")
        heading.pack(fill="x", pady=(0, 4))
        tk.Label(sidebar, text="CC2340 · 8-channel measurement", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10), anchor="w").pack(fill="x", pady=(0, 20))

        connection = self._section(sidebar, "Bluetooth Connection")
        self.scan_button = ttk.Button(connection, text="Scan for Devices", command=self._scan)
        self.scan_button.pack(fill="x")
        tk.Label(connection, text="Select a Data Stream / CC2340 device", bg=WHITE, fg=MUTED,
                 anchor="w", font=("Segoe UI", 9)).pack(fill="x", pady=(12, 5))
        self.device_list = tk.Listbox(connection, height=4, selectmode="single", exportselection=False,
                                      font=("Segoe UI", 9), relief="solid", bd=1,
                                      selectbackground=BLUE, selectforeground=WHITE)
        self.device_list.pack(fill="x")
        self.device_list.bind("<<ListboxSelect>>", lambda _event: self._selection_changed())
        self.connect_button = ttk.Button(connection, text="Connect", command=self._connect, state="disabled")
        self.connect_button.pack(fill="x", pady=(9, 0))
        self.disconnect_button = ttk.Button(connection, text="Disconnect", command=self.worker.disconnect,
                                            state="disabled")
        self.disconnect_button.pack(fill="x", pady=(6, 0))

        sampling = self._section(sidebar, "Sampling Settings")
        tk.Label(sampling, textvariable=self.interval_label_var, bg=WHITE, fg=INK,
                 anchor="w", font=("Segoe UI", 10)).pack(fill="x")
        self.interval_scale = tk.Scale(sampling, from_=0.1, to=24.0, resolution=0.1,
                                       orient="horizontal", showvalue=False, variable=self.interval_var,
                                       bg=WHITE, highlightthickness=0,
                                       troughcolor="#dbe7f0", activebackground=BLUE, length=260,
                                       state="disabled")
        self.interval_scale.pack(fill="x", pady=(4, 0))
        self.interval_scale.bind("<B1-Motion>", lambda _event: self.root.after_idle(self._interval_preview))
        self.interval_scale.bind("<ButtonRelease-1>", lambda _event: self._send_interval())
        self.interval_scale.bind("<KeyRelease>", lambda _event: self._send_interval())
        manual = tk.Frame(sampling, bg=WHITE)
        manual.pack(fill="x", pady=(8, 3))
        tk.Label(manual, text="Enter T", bg=WHITE, fg=INK,
                 font=("Segoe UI", 10)).pack(side="left")
        self.interval_entry = ttk.Entry(manual, textvariable=self.interval_entry_var,
                                        width=9, state="disabled")
        self.interval_entry.pack(side="left", padx=(9, 4))
        self.interval_entry.bind("<Return>", lambda _event: self._apply_manual_interval())
        tk.Label(manual, text="s", bg=WHITE, fg=INK,
                 font=("Segoe UI", 10)).pack(side="left")
        self.interval_apply_button = ttk.Button(manual, text="Apply",
                                                command=self._apply_manual_interval,
                                                state="disabled")
        self.interval_apply_button.pack(side="right")
        tk.Label(sampling, text="T = 0.1–24 s; select channels and gains.", bg=WHITE,
                 fg=MUTED, font=("Segoe UI", 9), anchor="w").pack(fill="x")
        tk.Label(sampling, textvariable=self.schedule_var, bg=WHITE, fg=BLUE,
                 font=("Segoe UI", 10), wraplength=260, justify="left",
                 anchor="w").pack(fill="x", pady=(8, 0))
        tk.Label(sampling, text="Cycle through enabled channels, CH0 to CH7.\nSet channel and gain; wait T/N; sample.\nEach sample updates its own chart.", bg=WHITE,
                 fg=MUTED, font=("Segoe UI", 9), justify="left", wraplength=260).pack(fill="x", pady=(8, 0))

        recorder = self._section(sidebar, "Data Recording / Export")
        self.record_button = ttk.Button(recorder, text="Record to CSV", command=self._toggle_recording)
        self.record_button.pack(fill="x")
        ttk.Button(recorder, text="Export All Data", command=self._export_all).pack(fill="x", pady=(7, 0))
        tk.Label(recorder, textvariable=self.record_var, bg=WHITE, fg=MUTED,
                 wraplength=260, justify="left", anchor="w", font=("Segoe UI", 9)).pack(fill="x", pady=(8, 0))

        main = ttk.Frame(body)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        heading_row = ttk.Frame(main)
        heading_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        title = tk.Label(heading_row, text="Live Measurements", bg=BG, fg=INK,
                         font=("Segoe UI", 20, "bold"), anchor="w")
        title.pack(side="left")
        ttk.Button(heading_row, text="Firmware OTA", command=self._open_ota_window).pack(side="right")
        self.clear_button = ttk.Button(heading_row, text="Clear", command=self._clear_data)
        self.clear_button.pack(side="right", padx=(8, 8))
        self.start_button = ttk.Button(heading_row, text="Start", command=self._start_sampling, state="disabled")
        self.start_button.pack(side="right")
        self.run_label = tk.Label(heading_row, text="Idle", bg=BG, fg=MUTED)
        self.run_label.pack(side="right", padx=(8, 8))

        cards = ttk.Frame(main)
        cards.grid(row=1, column=0, sticky="nsew")
        for col in range(2):
            cards.columnconfigure(col, weight=1)
        for row in range(4):
            cards.rowconfigure(row, weight=1, uniform="channel")
        self.charts = []
        self.channel_buttons = []
        self.gain_boxes = []
        for channel in range(8):
            card = tk.Frame(cards, bg=WHITE, highlightbackground="#dfe8ef", highlightthickness=1)
            card.grid(row=channel // 2, column=channel % 2, sticky="nsew", padx=(0, 10), pady=(0, 8))
            controls = tk.Frame(card, bg=WHITE)
            controls.pack(fill="x", padx=8, pady=(5, 0))
            toggle = tk.Checkbutton(controls, text=f"CH{channel} Enable", variable=self.channel_vars[channel],
                                    command=self._send_configuration, bg=WHITE, fg=COLORS[channel],
                                    selectcolor=WHITE, font=("Segoe UI", 10, "bold"), state="disabled")
            toggle.pack(side="left")
            self.channel_buttons.append(toggle)
            tk.Label(controls, textvariable=self.value_vars[channel], bg=WHITE, fg=INK,
                     font=("Segoe UI", 11, "bold")).pack(side="left", padx=(8, 0))
            gain_box = ttk.Combobox(controls, values=[f"{g}×" for g in GAINS],
                                    textvariable=self.gain_vars[channel], width=6, state="disabled")
            gain_box.pack(side="right")
            gain_box.bind("<<ComboboxSelected>>", lambda _event: self._send_configuration())
            self.gain_boxes.append(gain_box)
            tk.Label(controls, text="Gain", bg=WHITE, fg=MUTED).pack(side="right", padx=(0, 4))
            chart = TrendChart(card, channel)
            chart.pack(fill="both", expand=True, padx=4, pady=(0, 2))
            self.charts.append(chart)

        footer = tk.Label(main, textvariable=self.status_var, bg="#e9f1f8", fg=INK,
                          font=("Segoe UI", 10), anchor="w", padx=12, pady=10,
                          wraplength=790, justify="left")
        footer.grid(row=2, column=0, sticky="ew")

    def _scan(self) -> None:
        self.scan_button.configure(state="disabled")
        self.connect_button.configure(state="disabled")
        self.device_list.delete(0, "end")
        self.devices.clear()
        self.worker.scan()

    def _selection_changed(self) -> None:
        if self.device_list.curselection() and not self.connected:
            self.connect_button.configure(state="normal")

    def _connect(self) -> None:
        selected = self.device_list.curselection()
        if not selected:
            return
        device = self.devices[selected[0]][3]
        self.connect_button.configure(state="disabled")
        self.worker.connect(device)

    def _refresh_charts(self) -> None:
        for chart in self.charts:
            chart.start_time = self.plot_origin
            chart.end_time = self.latest_plot_time
            chart.request_draw()
        self.root.after(500, self._refresh_charts)

    def _refresh_start(self) -> None:
        ready = (self.connected and self.start_supported and not self.ota_active
                 and not self.sampling_running and not self.start_requested
                 and not self.pending_run and any(v.get() for v in self.channel_vars))
        self.start_button.configure(state="normal" if ready else "disabled")
        self.run_label.configure(text="Starting…" if self.start_requested or self.pending_run
                                 else "Running" if self.sampling_running else "Idle")

    def _start_sampling(self) -> None:
        if (not self.connected or not self.start_supported or self.sampling_running
                or self.ota_active or self.start_requested or self.pending_run):
            return
        if not any(v.get() for v in self.channel_vars):
            self.status_var.set("Select at least one channel before starting.")
            return
        try:
            self.interval_ms = parse_interval_seconds(self.interval_entry_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid Sampling Interval", str(exc), parent=self.root)
            return
        self.start_requested = True
        self._send_configuration()  # Start only after this exact configuration is acknowledged.
        self._refresh_start()

    def _clear_data(self) -> None:
        self.history.clear()
        self.plot_origin = self.latest_plot_time = None
        for chart, value in zip(self.charts, self.value_vars):
            chart.clear()
            value.set("— mV")
        self.status_var.set("All charts and export data cleared. CSV recording files are preserved.")

    def _run_timeout(self) -> None:
        if self.pending_run and self.connected:
            self.pending_run = False
            self.status_var.set("Start confirmation timed out; querying device state…")
            self.worker.send(b"Q")
            self._refresh_start()

    def _update_schedule(self) -> None:
        count = sum(v.get() for v in self.channel_vars)
        self.schedule_var.set(
            f"{count} channels selected; wait {self.interval_ms / 1000 / count:.4g} s per channel"
            if count else "No channels selected: sampling paused"
        )
        self._refresh_start()

    def _send_configuration(self) -> None:
        self._update_schedule()
        if not self.connected or not self.protocol_v2 or self.ota_active:
            return
        interval = self.interval_ms
        mask = sum(1 << ch for ch, selected in enumerate(self.channel_vars) if selected.get())
        gains = tuple(GAINS.index(int(value.get().rstrip("×"))) for value in self.gain_vars)
        desired = ScanSettings(interval, mask, gains)
        self.pending_settings = desired
        self.settings_request_id += 1
        request_id = self.settings_request_id
        self.worker.send(configuration_command(interval, mask, gains))
        self.status_var.set("Applying sampling settings…")
        self.root.after(5000, lambda: self._settings_timeout(request_id))

    def _settings_timeout(self, request_id: int) -> None:
        if request_id == self.settings_request_id and self.pending_settings is not None and self.connected:
            self.pending_settings = None
            self.start_requested = False
            self.status_var.set("Settings confirmation timed out; querying device…")
            self.worker.send(b"Q")
            self._refresh_start()

    def _interval_preview(self) -> None:
        seconds = self.interval_var.get()
        self.interval_ms = round(seconds * 1000)
        self.interval_entry_var.set(f"{seconds:.1f}")
        self.interval_label_var.set(f"Total wait per cycle T: {seconds:.1f} s")
        self._update_schedule()

    def _send_interval(self) -> None:
        if self.connected:
            self._interval_preview()
            self._send_configuration()

    def _apply_manual_interval(self) -> None:
        if not self.connected:
            return
        try:
            milliseconds = parse_interval_seconds(self.interval_entry_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid Sampling Interval", str(exc), parent=self.root)
            self.interval_entry.focus_set()
            return
        seconds = milliseconds / 1000
        self.interval_ms = milliseconds
        self.interval_var.set(seconds)
        self.interval_entry_var.set(f"{seconds:g}")
        self.interval_label_var.set(f"Total wait per cycle T: {seconds:g} s")
        self._send_configuration()

    def _open_ota_window(self) -> None:
        if self.ota_window is not None and self.ota_window.winfo_exists():
            self.ota_window.lift()
            self.ota_window.focus_set()
            return
        window = tk.Toplevel(self.root)
        self.ota_window = window
        window.title("pH Sense · Windows Bluetooth OTA")
        window.geometry("640x420")
        window.minsize(560, 390)
        window.configure(bg=WHITE)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self._close_ota_window)
        panel = ttk.Frame(window, padding=22)
        panel.pack(fill="both", expand=True)

        tk.Label(panel, text="CC2340 Firmware Upgrade", bg=BG, fg=INK,
                 font=("Segoe UI", 17, "bold"), anchor="w").pack(fill="x")
        tk.Label(panel, textvariable=self.ota_service_var, bg=BG, fg=INK,
                 wraplength=550, justify="left", anchor="w",
                 font=("Segoe UI", 10)).pack(fill="x", pady=(13, 4))
        warning = (
            "Requires matching MCUboot and OAD firmware installed via XDS110. "
            "Keep power stable. OTA and recovery are not yet verified on the custom PCB."
        )
        tk.Label(panel, text=warning, bg=BG, fg="#a65020", wraplength=550,
                 justify="left", anchor="w", font=("Segoe UI", 10)).pack(fill="x", pady=(2, 14))

        file_row = ttk.Frame(panel)
        file_row.pack(fill="x")
        self.ota_file_button = ttk.Button(file_row, text="Select .bin Firmware", command=self._choose_ota_file)
        self.ota_file_button.pack(side="left")
        tk.Label(panel, textvariable=self.ota_file_var, bg=BG, fg=MUTED,
                 wraplength=550, justify="left", anchor="w",
                 font=("Segoe UI", 9)).pack(fill="x", pady=(8, 15))

        self.ota_progress_bar = ttk.Progressbar(panel, maximum=100,
                                                variable=self.ota_progress_var)
        self.ota_progress_bar.pack(fill="x")
        tk.Label(panel, textvariable=self.ota_status_var, bg=BG, fg=INK,
                 wraplength=550, justify="left", anchor="w",
                 font=("Segoe UI", 10)).pack(fill="x", pady=(9, 17))

        actions = ttk.Frame(panel)
        actions.pack(fill="x")
        self.ota_start_button = ttk.Button(actions, text="Start OTA", command=self._start_ota)
        self.ota_start_button.pack(side="left")
        self.ota_cancel_button = ttk.Button(actions, text="Cancel OTA", command=self._cancel_ota)
        self.ota_cancel_button.pack(side="left", padx=(10, 0))
        ttk.Button(actions, text="Close", command=self._close_ota_window).pack(side="right")
        tk.Label(panel, text="File checks validate structure only; the bootloader verifies signatures. Reconnect after upgrade to verify.",
                 bg=BG, fg=MUTED, wraplength=550, justify="left", anchor="w",
                 font=("Segoe UI", 9)).pack(fill="x", pady=(18, 0))
        self._refresh_ota_controls()

    def _close_ota_window(self) -> None:
        if self.ota_active:
            messagebox.showwarning("Upgrade in Progress", "Wait for the upgrade to finish or cancel it first.",
                                   parent=self.ota_window)
            return
        if self.ota_window is not None:
            self.ota_window.destroy()
            self.ota_window = None

    def _refresh_ota_controls(self) -> None:
        self.ota_service_var.set(f"OAD service: {self.ota_reason}")
        if self.ota_window is None or not self.ota_window.winfo_exists():
            return
        can_start = self.connected and self.ota_available and self.ota_image is not None
        self.ota_file_button.configure(state="disabled" if self.ota_active else "normal")
        self.ota_start_button.configure(state="normal" if can_start and not self.ota_active else "disabled")
        self.ota_cancel_button.configure(state="normal" if self.ota_active else "disabled")

    def _choose_ota_file(self) -> None:
        if self.ota_active:
            return
        filename = filedialog.askopenfilename(parent=self.ota_window, title="Select OTA Firmware",
                                              filetypes=(("MCUboot Firmware", "*.bin"),))
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > MAX_IMAGE_SIZE:
                raise ValueError("File exceeds the dual-image OTA partition capacity")
            image = validate_image(path.read_bytes())
        except (OSError, ValueError) as exc:
            self.ota_image = None
            self.ota_path = None
            self.ota_file_var.set("Invalid file; select a matching signed .bin image")
            messagebox.showerror("Invalid Firmware", str(exc), parent=self.ota_window)
        else:
            self.ota_image = image
            self.ota_path = path
            self.ota_file_var.set(
                f"{path.name}\nVersion {image.version} · {len(image.payload):,} bytes · "
                f"SHA-256 {image.sha256[:16]}…"
            )
            self.ota_status_var.set("Structure valid; confirm matching chip, partition layout and signing key")
            self.ota_progress_var.set(0)
        self._refresh_ota_controls()

    def _start_ota(self) -> None:
        if self.ota_active or not self.connected or not self.ota_available or self.ota_image is None:
            return
        confirmed = messagebox.askyesno(
            "Confirm Firmware Upgrade",
            "New firmware will be written via Bluetooth. Confirm:\n\n"
            "• Matching MCUboot and initial OAD firmware are installed\n"
            "• The .bin matches the CC2340R53 layout and signing key\n"
            "• Power is stable and CCS debugging is stopped\n\n"
            "Recovery on the custom PCB is not yet verified. Continue?",
            parent=self.ota_window,
        )
        if not confirmed:
            return
        self.ota_active = True
        self.ota_progress_var.set(0)
        self.ota_status_var.set("Starting upgrade…")
        self._refresh_ota_controls()
        self._set_measurement_controls(False)
        self.disconnect_button.configure(state="disabled")
        self.worker.start_ota(self.ota_image)

    def _cancel_ota(self) -> None:
        if self.ota_active:
            self.ota_status_var.set("Requesting cancellation…")
            self.ota_cancel_button.configure(state="disabled")
            self.worker.cancel_ota()

    def _set_measurement_controls(self, enabled: bool) -> None:
        enabled = enabled and self.protocol_v2 and self.start_supported
        state = "normal" if enabled else "disabled"
        self.interval_scale.configure(state=state)
        self.interval_entry.configure(state=state)
        self.interval_apply_button.configure(state=state)
        for button in self.channel_buttons:
            button.configure(state=state)
        for box in self.gain_boxes:
            box.configure(state="readonly" if enabled else "disabled")
        self._refresh_start()

    def _process_events(self) -> None:
        for _ in range(200):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status_var.set(payload)
            elif kind == "error":
                self.status_var.set(f"Operation failed: {payload}")
                self.connect_button.configure(state="normal" if self.devices else "disabled")
            elif kind == "protocol_warning":
                self.status_var.set(f"Protocol warning: {payload}")
            elif kind == "devices":
                self.devices = payload
                self.device_list.delete(0, "end")
                for name, address, rssi, _device in payload:
                    self.device_list.insert("end", f"{name}  [{address}]  {rssi} dBm")
                if payload:
                    self.device_list.selection_set(0)
                    self._selection_changed()
            elif kind == "scan_done":
                self.scan_button.configure(state="normal")
            elif kind == "connected":
                self.connected = True
                self.sampling_running = self.start_supported = False
                self.start_requested = self.pending_run = False
                self.protocol_v2 = False
                self.pending_settings = None
                self.applied_settings = None
                self.last_sequence = None
                self.ota_available = False
                self.ota_reason = "Checking for TI OAD support…"
                self.scan_button.configure(state="disabled")
                self.connect_button.configure(state="disabled")
                self.disconnect_button.configure(state="normal")
                self._set_measurement_controls(not self.ota_active)
                self._refresh_ota_controls()
            elif kind == "ota_available":
                self.ota_available, self.ota_reason = payload
                self._refresh_ota_controls()
            elif kind == "ota_progress":
                percent, detail = payload
                self.ota_progress_var.set(percent)
                self.ota_status_var.set(detail)
                self.status_var.set(f"OTA: {detail}")
            elif kind == "ota_done":
                outcome, detail = payload
                self.ota_active = False
                self.ota_status_var.set(detail)
                self.status_var.set(f"OTA: {detail}")
                if outcome == "accepted":
                    self.ota_available = False
                    self.ota_reason = "Device is restarting; reconnect to verify the firmware"
                else:
                    self._set_measurement_controls(self.connected)
                self.disconnect_button.configure(state="normal" if self.connected else "disabled")
                self._refresh_ota_controls()
            elif kind == "disconnected":
                was_connected = self.connected
                self.connected = False
                self.sampling_running = self.start_supported = False
                self.start_requested = self.pending_run = False
                self.protocol_v2 = False
                self.pending_settings = None
                self.ota_available = False
                self.ota_reason = "Device disconnected; reconnect to check firmware"
                self.scan_button.configure(state="normal")
                self.disconnect_button.configure(state="disabled")
                self.connect_button.configure(state="normal" if self.devices else "disabled")
                self._set_measurement_controls(False)
                self._refresh_ota_controls()
                if was_connected and not self.ota_active:
                    self.status_var.set("Device disconnected; scan or connect again")
            elif kind == "notification":
                self._handle_message(payload)
        self.root.after(50, self._process_events)

    def _show_settings(self, settings: ScanSettings) -> None:
        self.interval_ms = settings.interval_ms
        seconds = settings.interval_ms / 1000
        self.interval_var.set(seconds)
        self.interval_entry_var.set(f"{seconds:g}")
        self.interval_label_var.set(f"Total wait per cycle T: {seconds:g} s")
        for channel in range(8):
            self.channel_vars[channel].set(bool(settings.channel_mask & (1 << channel)))
            self.gain_vars[channel].set(f"{GAINS[settings.gains[channel]]}×")
        self._update_schedule()

    def _handle_message(self, message: Sweep | Settings | Sample | ScanSettings | DeviceError) -> None:
        if isinstance(message, ScanSettings):
            self.protocol_v2 = True
            self.start_supported = message.running is not None
            if self.start_supported:
                self.sampling_running = message.running
                if message.running:
                    self.pending_run = False
            self.applied_settings = message
            if self.pending_settings is not None and (
                    self.pending_settings.interval_ms, self.pending_settings.channel_mask,
                    self.pending_settings.gains) != (message.interval_ms, message.channel_mask, message.gains):
                self._refresh_start()
                return  # Earlier acknowledgement; keep the latest requested values.
            self.pending_settings = None
            self._show_settings(message)
            self._set_measurement_controls(self.connected and not self.ota_active)
            self.status_var.set(f"Device confirmed: T={message.interval_ms / 1000:g} s; {self.schedule_var.get()}")
            if not self.start_supported:
                self.status_var.set("Flash the new XDS110 firmware to enable Start-controlled sampling.")
            elif self.start_requested:
                self.start_requested = False
                self.pending_run = True
                self.worker.send(run_command(True))
                self.root.after(5000, self._run_timeout)
            elif self.sampling_running:
                self.status_var.set(f"Sampling running. {self.schedule_var.get()}")
            else:
                self.status_var.set(f"Ready. Click Start. {self.schedule_var.get()}")
            self._refresh_start()
        elif isinstance(message, Settings):
            self.status_var.set("Older firmware detected. Flash the matching firmware via XDS110 to enable these controls.")
        elif isinstance(message, DeviceError):
            self.start_requested = self.pending_run = False
            self.pending_settings = None
            if self.applied_settings is not None:
                self._show_settings(self.applied_settings)
            extra = " (Stop CCS debugging, disconnect SWD and power-cycle the board.)" if message.code == "DEBUG" else ""
            self.status_var.set(f"Device rejected command: {message.code}{extra}")
            self._refresh_start()
        elif isinstance(message, Sample):
            self._handle_sample(message)
        else:
            for channel, value in enumerate(message.millivolts):
                self._handle_sample(Sample(message.gain_state, message.sequence, channel,
                                           value, 0, self.sample_number // 8 + 1), check_sequence=False)

    def _handle_sample(self, sample: Sample, check_sequence: bool = True) -> None:
        if not self.sampling_running:
            return
        self.sample_number += 1
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        monotonic_time = time.monotonic()
        self.history.append((monotonic_time, self.sample_number, timestamp, sample))
        if self.plot_origin is None:
            self.plot_origin = monotonic_time
        self.latest_plot_time = monotonic_time
        self.charts[sample.channel].add_sample(monotonic_time, sample.millivolts)
        self.value_vars[sample.channel].set(
            "ADC error" if sample.millivolts is None else f"{sample.millivolts} mV · {GAINS[sample.gain_state]}×"
        )
        if check_sequence and self.last_sequence is not None and ((sample.sequence - self.last_sequence) & 0xFF) != 1:
            self.status_var.set("Missing sample sequence detected; check Bluetooth stability")
        if check_sequence:
            self.last_sequence = sample.sequence
        if self.csv_writer is not None:
            try:
                self._write_csv_sample(self.csv_writer, self.sample_number, timestamp, sample)
                self.csv_file.flush()
                self.recorded_rows += 1
                self.record_var.set(
                    f"Recording: {self.recorded_rows} samples\n{self.csv_path}"
                )
            except OSError as exc:
                self._stop_recording()
                messagebox.showerror("Recording Interrupted", f"CSV write failed: {exc}")

    @staticmethod
    def _write_csv_sample(writer, number: int, timestamp: str, sample: Sample) -> None:
        value = sample.millivolts
        writer.writerow((number, timestamp, sample.device_time_ms, sample.cycle,
                         sample.sequence, sample.channel, GAINS[sample.gain_state],
                         "" if value is None else value, 0 if value is None else 1))

    def _toggle_recording(self) -> None:
        if self.csv_file is not None:
            self._stop_recording()
            return
        name = f"phsense-{datetime.now():%Y%m%d-%H%M%S}.csv"
        filename = filedialog.asksaveasfilename(parent=self.root, title="Save CSV Recording",
                                                defaultextension=".csv", initialfile=name,
                                                filetypes=(("CSV Files", "*.csv"),))
        if not filename:
            return
        try:
            self.csv_file = open(filename, "w", encoding="utf-8-sig", newline="")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(CSV_HEADER)
            self.csv_file.flush()
            self.csv_path = Path(filename)
            self.recorded_rows = 0
            self.record_button.configure(text="Stop Recording")
            self.record_var.set(f"Recording: 0 samples\n{self.csv_path}")
        except OSError as exc:
            self._stop_recording()
            messagebox.showerror("Recording Failed", str(exc))

    def _stop_recording(self) -> None:
        if self.csv_file is not None:
            self.csv_file.close()
        self.csv_file = None
        self.csv_writer = None
        self.record_button.configure(text="Record to CSV")
        self.record_var.set(
            f"Saved {self.recorded_rows} samples\n{self.csv_path}"
            if self.csv_path else "Not recording"
        )

    def _export_all(self) -> None:
        if not self.history:
            messagebox.showinfo("No Data", "Connect a device and acquire samples before exporting.")
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title="Export Accumulated Data",
                                                defaultextension=".csv",
                                                initialfile="phsense-all.csv",
                                                filetypes=(("CSV Files", "*.csv"),))
        if not filename:
            return
        try:
            with open(filename, "w", encoding="utf-8-sig", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(CSV_HEADER)
                for _when, number, timestamp, sample in self.history:
                    self._write_csv_sample(writer, number, timestamp, sample)
            self.status_var.set(f"Exported accumulated data: {filename}")
        except OSError as exc:
            messagebox.showerror("Export Failed", str(exc))

    def _close(self) -> None:
        if self.ota_active and not messagebox.askyesno(
            "Upgrade in Progress", "Closing interrupts the upgrade and may require wired recovery. Exit?",
            parent=self.root,
        ):
            return
        if self.csv_file is not None:
            self._stop_recording()
        self.worker.close()
        self.root.destroy()


def main() -> int:
    if "--self-test" in sys.argv:
        import bleak.backends.winrt.client  # Verify the packaged Windows BLE backend imports.
        import bleak.backends.winrt.scanner
        from ota_protocol import parse_block_size
        from protocol import decode_notification

        payload = bytes((0xA5, 3, 7, 2, 5)) + struct.pack("<HII", 1234, 24000, 19)
        result = decode_notification(payload)
        settings = configuration_command(24000, 0x85, (0, 1, 2, 3, 0, 1, 2, 3))
        return 0 if (result == Sample(3, 7, 5, 1234, 24000, 19)
                     and decode_notification(b"\xc4" + settings[1:] + b"\x00").running is False
                     and run_command(True) == b"R=1"
                     and parse_block_size(b"\x01\x14\x00") == 20) else 1
    root = tk.Tk()
    PhSenseApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
