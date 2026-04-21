"""
Pumped Storage Hydropower Scale Model — Main Firmware
MicroPython (ESP32 recommended)
======================================================
REQUIRED FILES ON THE DEVICE (copy all to root /):
  main.py          ← this file
  hx710b.py        ← HX710B pressure sensor driver
  sdcard.py        ← from micropython-lib  (SD card driver)
                     https://github.com/micropython/micropython-lib/tree/master/micropython/drivers/storage/sdcard

PIN ASSIGNMENTS
───────────────────────────────────────────────────────
Flow sensor   (Hall effect, 1 pulse/rev):  GPIO 14
Water level 1 (analog voltage 0–3.6 V):   GPIO 34  (ADC, input-only on ESP32)
Water level 2 (binary emergency low):      GPIO 35  (interrupt on both edges)
Output valve  (active-HIGH relay):         GPIO 26
HX710B DOUT   (pressure):                  GPIO 32
HX710B SCK    (pressure):                  GPIO 33
SD card SPI:
    SCK  → GPIO 18
    MOSI → GPIO 23
    MISO → GPIO 19
    CS   → GPIO 5
Load sensor   (future, placeholder):       GPIO 27 (DOUT) / GPIO 25 (SCK)
───────────────────────────────────────────────────────
"""

import machine
import utime
import uos
import network
import usocket
import _thread
from machine import Pin, ADC, SPI

from hx710b import HX710B

# ════════════════════════════════════════════════════
# CONFIGURATION  — edit before flashing
# ════════════════════════════════════════════════════

WIFI_SSID     = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"

# Flow sensor: pulses per litre.
#   YF-S201  ≈  450  pulses/L  (most common hobby flow sensor)
#   FS300A   ≈ 5880  pulses/L
# Set to 1 during bench testing to watch raw tick rates first.
FLOW_PULSES_PER_LITRE = 450.0

# Water level 1 — analogue calibration (12-bit ADC → % full).
# Measure these raw values with an empty tank and a full tank, then update.
WL1_RAW_EMPTY = 300    # ADC count when completely empty
WL1_RAW_FULL  = 3800   # ADC count when completely full

# Water level 2 — which logic level means "water IS present".
# Flip to 0 if your sensor pulls HIGH when dry.
WL2_WATER_PRESENT_STATE = 1

# HX710B — counts per kPa for your specific pressure sensor.
# MPS20N0040D at full VCC:  ≈ 710 000 counts/kPa
# With 1.5 kΩ bridge resistor for extended range: ≈ 174 380 counts/kPa
PRESSURE_SCALE_COUNTS_PER_KPA = 710_000.0

# How often to write a row to the SD card (seconds)
LOG_INTERVAL_S = 2

# HTTP port for the live dashboard (80 = no port number needed in browser)
WEB_PORT = 80

# ════════════════════════════════════════════════════
# SHARED STATE  (guarded by a mutex; written by main loop, read by web thread)
# ════════════════════════════════════════════════════

_lock = _thread.allocate_lock()

state = {
    "flow_ml_s":    0.0,
    "flow_l_min":   0.0,
    "wl1_raw":      0,
    "wl1_pct":      0.0,
    "wl2_present":  True,   # False → emergency low level detected
    "pressure_raw": 0,
    "pressure_kpa": 0.0,
    "load_raw":     None,   # placeholder until load sensor is installed
    "valve_open":   True,
    "emergency":    False,
    "uptime_s":     0,
}

# ════════════════════════════════════════════════════
# OUTPUT VALVE
# ════════════════════════════════════════════════════

valve_pin = Pin(26, Pin.OUT, value=1)   # HIGH = open

def open_valve():
    valve_pin.value(1)
    with _lock:
        state["valve_open"] = True

def close_valve():
    """Safe to call from ISR — no lock, direct dict write."""
    valve_pin.value(0)
    state["valve_open"] = False

# ════════════════════════════════════════════════════
# SENSOR 1 — FLOW  (Hall-effect pulse counting)
# ════════════════════════════════════════════════════

_flow_pulses     = 0
_flow_last_ms    = utime.ticks_ms()
flow_pin         = Pin(14, Pin.IN, Pin.PULL_UP)

def _flow_isr(pin):
    global _flow_pulses
    _flow_pulses += 1

flow_pin.irq(trigger=Pin.IRQ_FALLING, handler=_flow_isr)

def read_flow():
    """Return (mL/s, L/min) over the period since the last call. Resets counter."""
    global _flow_pulses, _flow_last_ms
    now_ms  = utime.ticks_ms()
    elapsed = utime.ticks_diff(now_ms, _flow_last_ms) / 1000.0

    irq_state = machine.disable_irq()
    pulses    = _flow_pulses
    _flow_pulses = 0
    machine.enable_irq(irq_state)
    _flow_last_ms = now_ms

    if elapsed <= 0 or pulses == 0:
        return 0.0, 0.0

    litres = pulses / FLOW_PULSES_PER_LITRE
    return round(litres / elapsed * 1000.0, 3), round(litres / elapsed * 60.0, 4)

# ════════════════════════════════════════════════════
# SENSOR 2 — WATER LEVEL 1  (analogue)
# ════════════════════════════════════════════════════

wl1_adc = ADC(Pin(34))
wl1_adc.atten(ADC.ATTN_11DB)   # 0–3.6 V on ESP32

def read_wl1():
    """Return (raw_adc 0-4095, percent_full 0.0-100.0)."""
    raw  = wl1_adc.read()
    span = max(WL1_RAW_FULL - WL1_RAW_EMPTY, 1)
    pct  = max(0.0, min(100.0, (raw - WL1_RAW_EMPTY) / span * 100.0))
    return raw, round(pct, 1)

# ════════════════════════════════════════════════════
# SENSOR 3 — WATER LEVEL 2  (binary, interrupt-driven emergency)
# ════════════════════════════════════════════════════

wl2_pin = Pin(35, Pin.IN, Pin.PULL_UP)

def _wl2_isr(pin):
    """
    Fires on any edge.  If water vanishes → close valve and latch emergency.
    The latch is intentional: manual reset (reboot) required for safety.
    """
    present = (pin.value() == WL2_WATER_PRESENT_STATE)
    state["wl2_present"] = present
    if not present:
        close_valve()
        state["emergency"] = True

wl2_pin.irq(trigger=Pin.IRQ_BOTH, handler=_wl2_isr)

# ════════════════════════════════════════════════════
# SENSOR 4 — HX710B PRESSURE
# ════════════════════════════════════════════════════

pressure_sensor = HX710B(
    dout_pin=32,
    sck_pin=33,
    scale=PRESSURE_SCALE_COUNTS_PER_KPA
)

def init_pressure():
    print("[Pressure] Taring at atmospheric pressure (takes ~2 s)...")
    utime.sleep_ms(500)
    pressure_sensor.tare(samples=10)
    print("[Pressure] Tare complete — zero point set.")

# ════════════════════════════════════════════════════
# FUTURE — LOAD SENSOR  (placeholder, uncomment when hardware arrives)
# ════════════════════════════════════════════════════
#
# The output load sensor will likely use an HX711 (similar to HX710B).
# When you have the hardware:
#   1. Add hx711.py to the device (same micropython-lib repo).
#   2. Uncomment the three lines below.
#   3. In the main loop, uncomment the load_raw line.
#
# from hx711 import HX711
# load_sensor = HX711(dout_pin=27, sck_pin=25)
# load_sensor.tare()

# ════════════════════════════════════════════════════
# MICRO-SD CARD  (SPI)
# ════════════════════════════════════════════════════

_sd_spi     = SPI(1, baudrate=4_000_000, polarity=0, phase=0,
                  sck=Pin(18), mosi=Pin(23), miso=Pin(19))
_sd_cs      = Pin(5, Pin.OUT, value=1)
_sd_mounted = False

_CSV_HEADER = (
    "uptime_s,flow_ml_s,flow_l_min,"
    "wl1_raw,wl1_pct,"
    "wl2_present,pressure_raw,pressure_kpa,"
    "load_raw,valve_open,emergency\n"
)

def mount_sd():
    global _sd_mounted
    try:
        import sdcard
        sd  = sdcard.SDCard(_sd_spi, _sd_cs)
        vfs = uos.VfsFat(sd)
        uos.mount(vfs, "/sd")
        _sd_mounted = True
        print("[SD] Card mounted at /sd")
    except Exception as exc:
        print(f"[SD] Mount failed: {exc}")

def log_to_sd(snap: dict):
    if not _sd_mounted:
        return
    path = "/sd/hydro_log.csv"
    try:
        # Write header only if file is new
        try:
            uos.stat(path)
        except OSError:
            with open(path, "w") as fh:
                fh.write(_CSV_HEADER)
        row = (
            f"{snap['uptime_s']},"
            f"{snap['flow_ml_s']:.3f},"
            f"{snap['flow_l_min']:.4f},"
            f"{snap['wl1_raw']},"
            f"{snap['wl1_pct']:.1f},"
            f"{'1' if snap['wl2_present'] else '0'},"
            f"{snap['pressure_raw']},"
            f"{snap['pressure_kpa']:.5f},"
            f"{'' if snap['load_raw'] is None else snap['load_raw']},"
            f"{'1' if snap['valve_open'] else '0'},"
            f"{'1' if snap['emergency'] else '0'}\n"
        )
        with open(path, "a") as fh:
            fh.write(row)
    except Exception as exc:
        print(f"[SD] Write error: {exc}")

# ════════════════════════════════════════════════════
# WI-FI
# ════════════════════════════════════════════════════

def connect_wifi() -> str | None:
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return wlan.ifconfig()[0]
    print(f"[WiFi] Connecting to '{WIFI_SSID}'...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    t = utime.ticks_ms()
    while not wlan.isconnected():
        if utime.ticks_diff(utime.ticks_ms(), t) > 15_000:
            print("[WiFi] Timeout — continuing without web server")
            return None
        utime.sleep_ms(250)
    ip = wlan.ifconfig()[0]
    print(f"[WiFi] Connected. Open  http://{ip}  on any device.")
    return ip

# ════════════════════════════════════════════════════
# WEB DASHBOARD  (second thread)
# ════════════════════════════════════════════════════
#
# The page auto-refreshes every 2 seconds.
# No JavaScript frameworks — works on every phone browser with no app needed.
# ════════════════════════════════════════════════════

_PAGE_TMPL = """\
HTTP/1.0 200 OK\r
Content-Type: text/html; charset=utf-8\r
Cache-Control: no-cache, no-store\r
\r
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="2">
  <title>Hydro Monitor</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Courier New',monospace;background:#0d1117;
          color:#c9d1d9;padding:16px;max-width:520px;margin:0 auto}}
    h1{{color:#58a6ff;font-size:1.05rem;margin-bottom:14px;letter-spacing:2px;
        text-transform:uppercase}}
    .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;
           padding:10px 14px;margin-bottom:10px}}
    .row{{display:flex;justify-content:space-between;align-items:baseline;
          padding:6px 0;border-bottom:1px solid #21262d}}
    .row:last-child{{border-bottom:none}}
    .lbl{{color:#8b949e;font-size:.82rem}}
    .val{{font-size:.98rem;font-weight:bold;text-align:right}}
    .sub{{color:#484f58;font-size:.78rem;margin-left:6px}}
    .ok  {{color:#3fb950}}
    .warn{{color:#d29922}}
    .bad {{color:#f85149}}
    footer{{color:#484f58;font-size:.72rem;text-align:center;margin-top:12px}}
  </style>
</head>
<body>
  <h1>&#9889; Pumped Storage Monitor</h1>

  <div class="card">
    <div class="row">
      <span class="lbl">Uptime</span>
      <span class="val">{uptime_s} s</span>
    </div>
    <div class="row">
      <span class="lbl">Flow</span>
      <span class="val ok">{flow_ml_s:.1f} mL/s<span class="sub">{flow_l_min:.3f} L/min</span></span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <span class="lbl">Water Level 1</span>
      <span class="val">{wl1_pct:.1f}%<span class="sub">raw {wl1_raw}</span></span>
    </div>
    <div class="row">
      <span class="lbl">Water Level 2 (emergency)</span>
      <span class="val {wl2_cls}">{wl2_txt}</span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <span class="lbl">Pressure</span>
      <span class="val">{pressure_kpa:.4f} kPa<span class="sub">raw {pressure_raw}</span></span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <span class="lbl">Load sensor</span>
      <span class="val {load_cls}">{load_txt}</span>
    </div>
    <div class="row">
      <span class="lbl">Output valve</span>
      <span class="val {valve_cls}">{valve_txt}</span>
    </div>
    <div class="row">
      <span class="lbl">System status</span>
      <span class="val {status_cls}">{status_txt}</span>
    </div>
  </div>

  <footer>Auto-refresh every 2 s &nbsp;|&nbsp; Hydro Scale Model</footer>
</body>
</html>
"""

def _render_page(snap: dict) -> str:
    emerg   = snap["emergency"]
    wl2_ok  = snap["wl2_present"]
    v_open  = snap["valve_open"]
    load    = snap["load_raw"]
    return _PAGE_TMPL.format(
        uptime_s     = snap["uptime_s"],
        flow_ml_s    = snap["flow_ml_s"],
        flow_l_min   = snap["flow_l_min"],
        wl1_pct      = snap["wl1_pct"],
        wl1_raw      = snap["wl1_raw"],
        wl2_cls      = "ok"   if wl2_ok  else "bad",
        wl2_txt      = "WATER PRESENT" if wl2_ok else "LOW — EMERGENCY",
        pressure_kpa = snap["pressure_kpa"],
        pressure_raw = snap["pressure_raw"],
        load_cls     = "val"  if load is not None else "sub",
        load_txt     = str(load) if load is not None else "not connected",
        valve_cls    = "ok"   if v_open  else "warn",
        valve_txt    = "OPEN" if v_open  else "CLOSED",
        status_cls   = "bad"  if emerg   else "ok",
        status_txt   = "EMERGENCY — low water!" if emerg else "NORMAL",
    )

def _web_server(_dummy):
    srv = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
    srv.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
    srv.bind(("", WEB_PORT))
    srv.listen(3)
    srv.settimeout(0.5)
    print(f"[Web] Listening on port {WEB_PORT}")
    while True:
        try:
            conn, _ = srv.accept()
            try:
                conn.recv(1024)
                with _lock:
                    snap = dict(state)
                conn.sendall(_render_page(snap).encode())
            except Exception:
                pass
            finally:
                conn.close()
        except OSError:
            pass   # accept() timeout — loop

# ════════════════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════════════════

def main():
    print("=" * 52)
    print("  Pumped Storage Hydro Model — Starting")
    print("=" * 52)

    mount_sd()
    init_pressure()

    ip = connect_wifi()
    if ip:
        _thread.start_new_thread(_web_server, (None,))

    t_start   = utime.ticks_ms()
    t_log     = utime.ticks_ms()

    print("[Main] Sensor loop running at 2 Hz (500 ms tick).")

    while True:
        now       = utime.ticks_ms()
        uptime_s  = utime.ticks_diff(now, t_start) // 1000

        # ── Read all sensors ─────────────────────────
        ml_s,  l_min  = read_flow()
        wl1_r, wl1_p  = read_wl1()
        wl2_now        = (wl2_pin.value() == WL2_WATER_PRESENT_STATE)
        p_raw, p_kpa   = pressure_sensor.read_kpa()

        # Load sensor (future):
        # load_raw = load_sensor.read_value()

        # ── Write to shared state ─────────────────────
        with _lock:
            state["uptime_s"]    = uptime_s
            state["flow_ml_s"]   = ml_s
            state["flow_l_min"]  = l_min
            state["wl1_raw"]     = wl1_r
            state["wl1_pct"]     = wl1_p
            state["wl2_present"] = wl2_now
            if p_raw is not None:
                state["pressure_raw"] = p_raw
                state["pressure_kpa"] = p_kpa or 0.0
            # state["load_raw"] = load_raw    ← uncomment when ready

            # Belt-and-suspenders emergency check (ISR handles the fast path)
            if not wl2_now and not state["emergency"]:
                state["emergency"] = True
                close_valve()

        # ── SD logging (every LOG_INTERVAL_S) ────────
        if utime.ticks_diff(now, t_log) >= LOG_INTERVAL_S * 1000:
            with _lock:
                snap = dict(state)
            log_to_sd(snap)
            t_log = now

        # ── Serial monitor ────────────────────────────
        emerg_tag = " [!EMERGENCY!]" if state["emergency"] else ""
        print(
            f"[{uptime_s:6d}s] "
            f"Flow {ml_s:6.2f}mL/s "
            f"WL1 {wl1_p:.0f}%(r{wl1_r}) "
            f"WL2 {'OK' if wl2_now else 'LOW!'} "
            f"P {state['pressure_kpa']:.3f}kPa "
            f"Valve {'OPEN' if state['valve_open'] else 'CLOSED'}"
            f"{emerg_tag}"
        )

        utime.sleep_ms(500)

main()
