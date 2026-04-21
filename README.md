# Pumped Storage Hydropower Scale Model — Firmware

MicroPython firmware for an ESP32-based pumped storage hydropower scale model.
Reads all sensors, logs to MicroSD, and serves a live dashboard over Wi-Fi.

---

## Files to copy to the ESP32

| File | Purpose |
|---|---|
| `main.py` | Main firmware (runs on boot) |
| `hx710b.py` | HX710B pressure sensor driver |
| `sdcard.py` | SD card driver (download separately — see below) |

**Get `sdcard.py`:**
Download from the official micropython-lib repository:
https://github.com/micropython/micropython-lib/blob/master/micropython/drivers/storage/sdcard/sdcard.py

Copy it to the root of your device with Thonny or `mpremote cp sdcard.py :`.

---

## Pin wiring

```
ESP32 GPIO   →  Component
─────────────────────────────────────────────────────
GPIO 14      →  Flow sensor signal (Hall effect output)
               + Pull-up already configured in firmware (Pin.PULL_UP)
               + Wire: sensor VCC → 3.3V, GND → GND, signal → GPIO 14

GPIO 34      →  Water level sensor 1 — analog voltage out
               + This is an input-only ADC pin on ESP32 (no internal pull-up)
               + Calibrate WL1_RAW_EMPTY and WL1_RAW_FULL in main.py

GPIO 35      →  Water level sensor 2 — binary (emergency low)
               + Also input-only on ESP32
               + Interrupt fires on both edges
               + Default: HIGH = water present (flip WL2_WATER_PRESENT_STATE if needed)

GPIO 26      →  Output valve relay (active-HIGH)
               + HIGH energises relay → valve opens
               + LOW de-energises relay → valve closes (safe/default on emergency)

GPIO 32      →  HX710B DOUT (data out from pressure sensor)
GPIO 33      →  HX710B SCK  (clock to pressure sensor)
               + HX710B VCC → 3.3V or 5V (check your module)
               + If using 5V module with 3.3V ESP32: add a voltage divider on DOUT

GPIO 18      →  SD card SCK
GPIO 23      →  SD card MOSI
GPIO 19      →  SD card MISO
GPIO 5       →  SD card CS
               + SD card VCC → 3.3V, GND → GND
               + Most micro-SD breakout boards have a built-in 3.3V regulator

GPIO 27      →  Load sensor DOUT  (future — not yet active)
GPIO 25      →  Load sensor SCK   (future — not yet active)
```

---

## Configuration (top of main.py)

```python
WIFI_SSID     = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"

FLOW_PULSES_PER_LITRE = 450.0   # YF-S201 default; adjust for your sensor

WL1_RAW_EMPTY = 300    # measure this with an empty tank
WL1_RAW_FULL  = 3800   # measure this with a full tank

WL2_WATER_PRESENT_STATE = 1   # flip to 0 if your sensor logic is inverted

PRESSURE_SCALE_COUNTS_PER_KPA = 710_000.0  # refine after calibration
```

---

## Calibration procedures

### Flow sensor
1. Set `FLOW_PULSES_PER_LITRE = 1` and watch the serial output.
2. Run a known volume of water (e.g. 500 mL measured in a jug) through the sensor.
3. Record the total pulse count from serial. Divide pulses ÷ litres.
4. Update `FLOW_PULSES_PER_LITRE` with that value.

### Water level 1 (analogue)
1. Empty the tank completely. Read `wl1_raw` from the serial monitor or web page.
   Set `WL1_RAW_EMPTY` to that value.
2. Fill the tank to your defined maximum. Read `wl1_raw` again.
   Set `WL1_RAW_FULL` to that value.
3. The firmware linearly maps between these two points to 0–100%.

### Pressure sensor (HX710B)
The firmware automatically tares to atmosphere on every boot.
For accurate absolute kPa readings:
1. Leave the sensor exposed to atmosphere, let it tare (first ~2 s on boot).
2. Apply a known pressure (e.g. from a hand pump with a gauge).
3. Note the `pressure_raw` value shown on the web page.
4. Calculate: `PRESSURE_SCALE_COUNTS_PER_KPA = raw_value ÷ known_kPa`
5. Update the constant in `main.py` and reflash.

Alternatively call `pressure_sensor.calibrate(known_kpa, raw_at_known)` once
over serial/REPL and print `pressure_sensor.scale` to get the new constant.

### Water level 2 (emergency binary)
- Verify which state your sensor outputs when dry vs. wet.
- If the serial monitor shows `WL2 LOW!` when the tank is full, flip:
  `WL2_WATER_PRESENT_STATE = 0`

---

## Web dashboard

Once the ESP32 connects to Wi-Fi, the boot message prints:
```
[WiFi] Connected. Open  http://192.168.x.x  on any device.
```
Open that URL on any phone, tablet, or laptop on the same network.
The page auto-refreshes every 2 seconds and shows all live data.
No app required — works in any browser.

---

## SD card log format

Log file: `/sd/hydro_log.csv`

```
uptime_s, flow_ml_s, flow_l_min, wl1_raw, wl1_pct, wl2_present,
pressure_raw, pressure_kpa, load_raw, valve_open, emergency
```

A new row is appended every `LOG_INTERVAL_S` seconds (default 2 s).
The file persists across reboots. The header is written only if the file
is new. Pull the SD card and open in Excel / LibreOffice for analysis.

---

## Adding the load sensor (future)

When the output load sensor arrives (expected to be HX711-based):
1. Download `hx711.py` from https://github.com/robert-hh/hx711
2. Copy it to the device.
3. In `main.py`, uncomment the three load sensor lines near the top.
4. In the main loop, uncomment `state["load_raw"] = load_sensor.read_value()`.
5. The web page and SD log will automatically include load data.

---

## Emergency behaviour

If water level 2 goes LOW:
- The output valve closes **immediately** via GPIO interrupt (< 1 ms response).
- The `emergency` flag is set in shared state.
- The web dashboard turns red and shows "EMERGENCY — low water!"
- The SD log records the event.
- **The valve will NOT reopen automatically.** Reboot the ESP32 after
  restoring water level to clear the latch.

This is intentional — requiring a deliberate reboot prevents auto-restart
flooding an already-dry system.

---

## Flashing / development tools

Recommended: **Thonny IDE** (free, cross-platform)
- File → Save as → MicroPython device

Or use the `mpremote` CLI:
```bash
pip install mpremote
mpremote cp main.py :main.py
mpremote cp hx710b.py :hx710b.py
mpremote cp sdcard.py :sdcard.py
mpremote reset
mpremote connect auto repl   # watch serial output
```
