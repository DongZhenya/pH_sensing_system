# pH Sense Windows App

An English-language Windows 10/11 BLE app for the custom CC2340R53 PCB. Use the matching
`wired_firmware/Release/phsense_wired.out`, programmed through CCS and XDS110.
This firmware does not require MCUboot or OTA setup.

## Measurement workflow

1. Flash the new OUT file in CCS. End debugging, disconnect SWD signals and power-cycle the board.
   GS0 and A2 share SWD pins; a board started under an attached debug session cannot control gain.
2. Launch `dist/PhSenseWindows.exe`, click **Scan for Devices**, select Data Stream, and **Connect**.
3. Set T between 0.1 and 24 seconds (up to three decimal places). Select channels with the eight
   **CHn Enable** toggles and set their independent gains (25/50/100/200x).
4. Click **Start**. The board acknowledges settings before sampling is enabled. Power-up, connection
   and applying settings do not start sampling. Disconnection stops sampling; reconnect and click Start
   to resume. Parameters remain adjustable during acquisition.
5. For N enabled channels, select a channel and its gain, wait T/N, sample, publish immediately, then
   select the next enabled channel in CH0–CH7 order. T is the sum of waits; ADC/GPIO/queueing add small
   overhead. Wait resolution is 1ms. Selecting zero channels pauses acquisition.
6. Each channel has an independent line chart with automatic Y scaling. The X axis expands with the
   acquisition period. All readings accumulate until **Clear** or app exit; no 60-second or point-count
   truncation remains. Every original sample is drawn in sequence without compression, averaging or
   downsampling. Export also retains all samples. ADC failures break lines.
7. **Clear** clears all plots, voltage displays and export history. It does not stop sampling, change
   settings or erase an existing CSV recording. Subsequent readings begin a new plot period.
8. **Record to CSV** continuously appends readings to a file. **Export All Data** exports all accumulated
   samples since Clear, including older readings. CSV includes host receive time, device sampling time,
   cycle, sequence, channel, actual gain, voltage and ADC validity.

Defaults: all channels, T=1s, 25x per channel. Board settings are volatile across power cycles.
Accumulated readings remain in app memory until Clear or exit; use CSV recording for persistent storage.
Reconnecting does not clear existing plots. Firmware without run-state acknowledgement must be updated.
The Android app also supports this protocol, with independent channel gains and connected RSSI logging.

## Protocol

Service: `f000c0c0-0451-4000-b000-000000000000`.
Write: `f000c0c1-0451-4000-b000-000000000000`.
Notify: `f000c0c2-0451-4000-b000-000000000000`.

- Query: ASCII `Q`.
- Configuration (13 bytes): `C3 02 mask T_ms[2] gains[8]`.
- Start/stop: ASCII `R=1` / `R=0`.
- Acknowledgement (14 bytes): `C4 02 mask T_ms[2] gains[8] running`.
- Sample (15 bytes): `A5 gain sequence 02 channel millivolts[2] time_ms[4] cycle[4]`.

Multibyte fields are little-endian. Gain states 0–3 represent 25/50/100/200x. Mask bit0 selects CH0.
FFFF means ADC failure. Time is a wrapping 32-bit millisecond counter since board startup.
All messages fit the default ATT MTU. The UI waits for configuration confirmation before sending Start,
then waits for running-state confirmation before accepting samples.

## Development

Run `.venv/Scripts/python.exe app.py`; tests: `.venv/Scripts/python.exe -m unittest discover -v`.
Build: `.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean PhSenseWindows.spec`.
Output: `dist/PhSenseWindows.exe`. Run with `--self-test` to check packaged BLE backends and protocol.
Local checks cover Start, Clear, cumulative data, English UI, per-channel controls and CSV.
Real PCB timing, gain accuracy and wireless operation need hardware verification.

## Optional OTA window

**Firmware OTA** is available only on devices providing TI OAD; the wired firmware for this workflow
does not provide OAD. OTA requires a matching MCUboot, OAD initial image, layout and signing key installed
through a wired debugger. Candidates in `ota_firmware` use a public TI example key and are development
artifacts. Custom-PCB OTA/recovery is unverified; desktop structure checks do not verify signatures.
