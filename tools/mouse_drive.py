#!/usr/bin/env python3
"""Tank-style mouse control for two brushed DC motors on a TB6612FNG.

Hold LEFT mouse button  -> left wheel runs forward
Hold RIGHT mouse button -> right wheel runs forward
Release a button        -> that wheel stops
Hold both               -> drive straight

Pins, speed and wheel direction come from config.yaml, the same settings the
plantbot service uses (motors.hbridge.*, motors.invert_*, mouse_drive.speed),
so this and the service always agree.

Stop the service first (it owns the motor pins and grabs the mouse):
    sudo systemctl stop plantbot
    .venv/bin/python tools/mouse_drive.py
Runs fine next to tools/wheel_capture.py (both just read the mouse).
"""
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evdev import InputDevice, ecodes, list_devices  # noqa: E402
from gpiozero import OutputDevice, PWMOutputDevice  # noqa: E402

from plantbot import config  # noqa: E402

cfg = config.load()
H = cfg.motors.hbridge
SPEED = min(float(cfg.mouse_drive.speed), float(cfg.motors.max_speed))


class Channel:
    """One TB6612FNG channel: a PWM speed pin plus two direction pins."""

    def __init__(self, pwm_gpio, in1_gpio, in2_gpio, invert=False):
        self.pwm = PWMOutputDevice(pwm_gpio, frequency=H.pwm_hz)
        self.in1 = OutputDevice(in1_gpio)
        self.in2 = OutputDevice(in2_gpio)
        self.invert = invert

    def forward(self):
        a, b = (self.in2, self.in1) if self.invert else (self.in1, self.in2)
        a.on()
        b.off()
        self.pwm.value = SPEED

    def stop(self):
        self.pwm.value = 0
        self.in1.off()
        self.in2.off()


def find_mouse():
    for path in list_devices():
        dev = InputDevice(path)
        keys = dev.capabilities().get(ecodes.EV_KEY, [])
        if ecodes.BTN_LEFT in keys and ecodes.BTN_RIGHT in keys:
            return dev
    return None


def shutdown(*_):
    sys.exit(0)  # lets the finally block below run


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGHUP, shutdown)  # SSH connection dropped

if (H.left_in1, H.left_in2, H.left_pwm) == (H.right_in1, H.right_in2, H.right_pwm):
    sys.exit("config.yaml gives both wheels the same pins; set motors.hbridge left_*/right_* apart.")
try:
    stby = OutputDevice(H.stby)
    left = Channel(H.left_pwm, H.left_in1, H.left_in2, cfg.motors.invert_left)
    right = Channel(H.right_pwm, H.right_in1, H.right_in2, cfg.motors.invert_right)
except Exception as e:  # typically "GPIO busy"
    sys.exit(f"Can't use the motor pins ({e}). Is the plantbot service running? "
             "Stop it with: sudo systemctl stop plantbot")

mouse = find_mouse()
if mouse is None:
    sys.exit("No mouse found. Is it plugged into the Pi?")
print("Using mouse:", mouse.name)
print(f"left wheel GPIO {H.left_in1}/{H.left_in2}/PWM {H.left_pwm}, "
      f"right wheel GPIO {H.right_in1}/{H.right_in2}/PWM {H.right_pwm}, speed {SPEED:.0%}")
print("Hold left/right click to drive. Ctrl+C to quit.")

try:
    stby.on()
    for e in mouse.read_loop():
        if e.type != ecodes.EV_KEY:
            continue
        if e.code == ecodes.BTN_LEFT:
            left.forward() if e.value else left.stop()
        elif e.code == ecodes.BTN_RIGHT:
            right.forward() if e.value else right.stop()
except KeyboardInterrupt:
    pass
except OSError as err:
    print(f"Mouse disconnected ({err}).")
finally:
    left.stop()
    right.stop()
    stby.off()
