"""Differential drive. Hardware libs are imported inside each driver so --mock needs none."""
from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

log = logging.getLogger(__name__)


class Motors(Protocol):
    def drive(self, left: float, right: float) -> None: ...  # -1.0..1.0 each, clamped
    def stop(self) -> None: ...


def _clamp(x: float, lim: float = 1.0) -> float:
    return max(-lim, min(lim, float(x)))


class MockMotors:
    def __init__(self, cfg=None):
        self.left = 0.0
        self.right = 0.0

    def drive(self, left: float, right: float) -> None:
        self.left, self.right = left, right

    def stop(self) -> None:
        self.left = self.right = 0.0

    def close(self) -> None:
        self.stop()


class ServoMotors:
    """Two continuous-rotation servos (rover v1). value -1..1 = full reverse..full forward."""

    def __init__(self, cfg):
        from gpiozero import Servo  # lazy

        s = cfg.servo
        kw = dict(min_pulse_width=s.min_pulse_ms / 1000, max_pulse_width=s.max_pulse_ms / 1000)
        self.trim = (cfg.trim_left, cfg.trim_right)
        self.l = Servo(s.left_pin, initial_value=self.trim[0], **kw)
        self.r = Servo(s.right_pin, initial_value=self.trim[1], **kw)

    def drive(self, left: float, right: float) -> None:
        self.l.value = _clamp(left + self.trim[0])
        self.r.value = _clamp(right + self.trim[1])

    def stop(self) -> None:
        # Servo at the trimmed midpoint = stopped. detach() would also stop but can twitch.
        self.l.value, self.r.value = self.trim

    def close(self) -> None:
        self.stop()
        self.l.close()
        self.r.close()


class _HBridgeChannel:
    """One TB6612/L298N channel: IN1/IN2 set direction, PWM (or ENA) sets speed.

    gpiozero's Motor holds `enable` high and PWMs IN1/IN2 instead, which leaves the
    TB6612 coasting between pulses. Here PWM low = short brake (TB6612) or free run
    (L298N), so low speeds have more torque and a stop actually stops.
    """

    def __init__(self, in1: int, in2: int, pwm: int, freq: int, min_duty: float):
        from gpiozero import DigitalOutputDevice, PWMOutputDevice  # lazy

        self.in1 = DigitalOutputDevice(in1)
        self.in2 = DigitalOutputDevice(in2)
        self.pwm = PWMOutputDevice(pwm, frequency=freq)
        self.min_duty = min_duty

    def set(self, v: float) -> None:
        v = _clamp(v)
        if v == 0:
            self.pwm.value = 0
            return
        # Gear motors don't turn below some duty; map (0, 1] onto [min_duty, 1].
        duty = self.min_duty + (1 - self.min_duty) * abs(v)
        self.in1.value, self.in2.value = (v > 0), (v < 0)
        self.pwm.value = duty

    def close(self) -> None:
        self.pwm.value = 0
        for d in (self.pwm, self.in1, self.in2):
            d.close()


class GpioZeroMotors:
    """TB6612FNG (or L298N, ENA/ENB on the PWM pins) via gpiozero. Works on Pi 4 and Pi 5."""

    def __init__(self, cfg):
        from gpiozero import DigitalOutputDevice  # lazy

        h = cfg.hbridge
        freq, min_duty = h.get("pwm_hz", 1000), h.get("min_duty", 0.0)
        self.l = _HBridgeChannel(h.left_in1, h.left_in2, h.left_pwm, freq, min_duty)
        self.r = _HBridgeChannel(h.right_in1, h.right_in2, h.right_pwm, freq, min_duty)
        # STBY low = both channels off (TB6612 only). Raised last so nothing twitches on init.
        self.stby = DigitalOutputDevice(h.stby, initial_value=True) if h.get("stby") is not None else None

    def drive(self, left: float, right: float) -> None:
        self.l.set(left)
        self.r.set(right)

    def stop(self) -> None:
        self.l.set(0)
        self.r.set(0)

    def close(self) -> None:
        if self.stby:
            self.stby.off()
            self.stby.close()
        self.l.close()
        self.r.close()


class SerialMotors:
    """Arduino motor shield over USB serial: `M <l> <r>\\n` (-255..255), `S\\n` stop."""

    def __init__(self, cfg):
        import serial  # lazy (pyserial)

        self.ser = serial.Serial(cfg.serial.port, cfg.serial.baud, timeout=0.2)
        time.sleep(2.0)  # Arduino resets when the port opens
        self.lock = threading.Lock()

    def _send(self, line: str) -> None:
        with self.lock:
            self.ser.write(line.encode())

    def drive(self, left: float, right: float) -> None:
        self._send(f"M {int(_clamp(left) * 255)} {int(_clamp(right) * 255)}\n")

    def stop(self) -> None:
        self._send("S\n")

    def close(self) -> None:
        self.stop()
        self.ser.close()


class SafeMotors:
    """Wraps any driver: clamps, applies inversion and max speed, and runs the watchdog.

    The watchdog stops the motors `watchdog_ms` after the last drive() call, so a dropped
    phone connection (or a hung mission loop) can't run the robot off the table.
    """

    def __init__(self, inner, cfg):
        self.inner = inner
        self.cfg = cfg
        self.timeout = cfg.watchdog_ms / 1000
        self.left = self.right = 0.0
        self.last_cmd = 0.0
        self.lock = threading.Lock()
        self.watchdog_trips = 0
        self._running = True
        threading.Thread(target=self._watchdog, name="motor-watchdog", daemon=True).start()

    def drive(self, left: float, right: float) -> None:
        m = self.cfg.max_speed
        left, right = _clamp(left, m), _clamp(right, m)
        out_l = -left if self.cfg.invert_left else left
        out_r = -right if self.cfg.invert_right else right
        with self.lock:
            self.last_cmd = time.monotonic()
            try:
                self.inner.drive(out_l, out_r)
                self.left, self.right = left, right
            except Exception:
                log.exception("motor drive failed, stopping")
                self._stop_locked()

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self.left = self.right = 0.0
        try:
            self.inner.stop()
        except Exception:
            log.exception("motor stop failed")

    @property
    def moving(self) -> bool:
        return self.left != 0.0 or self.right != 0.0

    def _watchdog(self) -> None:
        while self._running:
            time.sleep(0.1)
            with self.lock:
                if self.moving and time.monotonic() - self.last_cmd > self.timeout:
                    log.warning("watchdog: no drive command for %.0f ms, stopping", self.timeout * 1000)
                    self.watchdog_trips += 1
                    self._stop_locked()

    def close(self) -> None:
        self._running = False
        self.stop()
        close = getattr(self.inner, "close", None)
        if close:
            close()


DRIVERS = {"mock": MockMotors, "servo": ServoMotors, "gpiozero": GpioZeroMotors, "serial": SerialMotors}


def make(cfg):
    """Returns (SafeMotors, status). Falls back to mock if the real driver won't init."""
    try:
        inner, status = DRIVERS[cfg.driver](cfg), ("mock" if cfg.driver == "mock" else "ok")
    except Exception as e:
        log.error("motors (%s) failed to init: %s; using mock", cfg.driver, e)
        inner, status = MockMotors(), "offline"
    return SafeMotors(inner, cfg), status
