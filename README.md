# pH Sensing System

This repository provides the firmware and companion applications for an eight-channel pH sensing system built around the Texas Instruments CC2340R53 microcontroller. The board samples the selected channels sequentially and streams measurements over Bluetooth Low Energy (BLE). The apps configure the sampling period and per-channel gain, display live measurements, and record or export data as CSV.

## Repository contents

- [Firmware](firmware/) — CC2340R53 source code, Code Composer Studio (CCS) project files, and prebuilt `.out` and `.hex` images.
- [Android app](android-app/) — APK, Android Studio source archive, and setup instructions. The app can also record the received Bluetooth signal strength (RSSI).
- [Windows app](windows-app/) — Windows executable, Python source code, dependencies, and usage instructions.

## Quick start

1. Program the CC2340R53 board using the prebuilt `.out` image with CCS and an XDS110 debugger. Follow the [firmware instructions](firmware/README_CHANNELS.md) for wiring and flashing. End the debug session, disconnect the SWD signal lines, and power-cycle the board.
2. Install the [Android APK](android-app/PhSense-Android-RSSI.apk) or run the [Windows executable](windows-app/PhSenseWindows.exe). Enable Bluetooth, scan for the board, and connect from one app at a time.
3. Select the channels, set each channel’s gain and the sampling period, and apply the settings. Select **Start** to begin acquisition.
4. View the live channel plots. Use **Record to CSV** to log new measurements or **Export All Data** to save the accumulated readings.
