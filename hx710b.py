"""
hx710b.py — Minimal MicroPython driver for HX710B 24-bit ADC
Used with MPS20N0040D differential pressure sensor module.

Based on the HX710B datasheet bit-bang protocol and community-verified
conversion factors (see MicroPython discussion #14028).

Usage:
    from hx710b import HX710B
    sensor = HX710B(dout_pin=32, sck_pin=33)
    raw, kpa = sensor.read_kpa()
"""

import machine
import utime


class HX710B:
    """
    Bit-bang driver for the HX710B 24-bit differential ADC.

    Mode A  (25 clocks) — external bridge sensor, gain 128, 10 Hz
    Mode B  (26 clocks) — internal temperature,   gain 32
    Mode C  (27 clocks) — external bridge sensor, gain 64, 40 Hz

    For the MPS20N0040D pressure sensor, use Mode A (default).
    """

    # Empirically derived conversion factor: raw counts per kPa.
    # Standard MPS20N0040D at full voltage: ~710 000 counts/kPa.
    # With 1.5 kΩ sensitivity-reducing resistor:          ~174 380 counts/kPa.
    DEFAULT_SCALE = 710_000.0

    def __init__(self, dout_pin: int, sck_pin: int, scale: float = None):
        self._dout = machine.Pin(dout_pin, machine.Pin.IN)
        self._sck  = machine.Pin(sck_pin,  machine.Pin.OUT, value=0)
        self.scale = scale if scale is not None else self.DEFAULT_SCALE
        self._offset = 0     # set by tare()

    # ── Low-level read ───────────────────────────────────────────

    def is_ready(self, timeout_ms: int = 300) -> bool:
        """Return True if the HX710B has data ready (DOUT pulled LOW)."""
        t = utime.ticks_ms()
        while self._dout.value() == 1:
            if utime.ticks_diff(utime.ticks_ms(), t) > timeout_ms:
                return False
        return True

    def read_raw(self, mode: int = 1) -> int | None:
        """
        Read one 24-bit signed value from the HX710B.

        mode:
          1 → Mode A: external sensor, gain 128, 10 Hz  (default, use this)
          2 → Mode B: internal temperature, gain 32
          3 → Mode C: external sensor, gain 64, 40 Hz
        """
        if not self.is_ready():
            return None

        # Disable interrupts to prevent timing jitter on critical bit-bang loop
        irq_state = machine.disable_irq()
        try:
            raw = 0
            for _ in range(24):
                self._sck.value(1)
                utime.sleep_us(1)
                raw = (raw << 1) | self._dout.value()
                self._sck.value(0)
                utime.sleep_us(1)

            # Extra pulses select mode for the NEXT conversion
            for _ in range(mode):
                self._sck.value(1)
                utime.sleep_us(1)
                self._sck.value(0)
                utime.sleep_us(1)
        finally:
            machine.enable_irq(irq_state)

        # Sign-extend: the HX710B outputs MSB-first two's complement
        if raw & 0x800000:
            raw -= 0x1000000

        return raw

    # ── Higher-level helpers ─────────────────────────────────────

    def tare(self, samples: int = 10):
        """
        Record the zero-point offset.  Call once at startup with no
        pressure applied (sensor open to atmosphere).
        """
        total = 0
        count = 0
        for _ in range(samples):
            v = self.read_raw()
            if v is not None:
                total += v
                count += 1
            utime.sleep_ms(110)   # > 1/10 Hz sample period
        if count:
            self._offset = total // count

    def read_value(self) -> int | None:
        """Return raw count with tare offset removed."""
        raw = self.read_raw()
        if raw is None:
            return None
        return raw - self._offset

    def read_kpa(self) -> tuple:
        """
        Return (raw_count, pressure_kpa).
        raw_count is the tare-compensated ADC value.
        pressure_kpa is converted using self.scale.
        Returns (None, None) if the sensor is not ready.
        """
        val = self.read_value()
        if val is None:
            return None, None
        kpa = val / self.scale
        return val, round(kpa, 5)

    def read_kpa_averaged(self, samples: int = 5) -> tuple:
        """Average multiple readings to reduce noise."""
        total = 0
        count = 0
        for _ in range(samples):
            v = self.read_value()
            if v is not None:
                total += v
                count += 1
            utime.sleep_ms(20)
        if count == 0:
            return None, None
        avg = total / count
        return int(avg), round(avg / self.scale, 5)

    def calibrate(self, known_kpa: float, raw_at_known: int):
        """
        Single-point calibration.
        Apply a known pressure, read raw_at_known from read_value(),
        then call this method to update self.scale.

        Example:
            sensor.tare()                        # zero at atmosphere
            # ... apply 10 kPa ...
            raw = sensor.read_value()
            sensor.calibrate(10.0, raw)
        """
        if raw_at_known == 0:
            raise ValueError("raw_at_known must be non-zero")
        self.scale = raw_at_known / known_kpa
        print(f"[HX710B] Calibrated: scale = {self.scale:.1f} counts/kPa")
