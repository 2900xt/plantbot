#!/usr/bin/env python3
"""Tank-style mouse control for two brushed DC motors on a TB6612FNG.

Hold LEFT mouse button  -> left motor  (driver channel A) runs forward
Hold RIGHT mouse button -> right motor (driver channel B) runs forward
Release a button        -> that motor stops
Hold both               -> drive straight

GPIO numbers are BCM numbers (physical pin in parentheses):
  PWMA 13 (33)  AIN1 20 (38)  AIN2 21 (40)
  PWMB 12 (32)  BIN1  5 (29)  BIN2  6 (31)
  STBY 26 (37)

Requires:  sudo apt install -y python3-evdev python3-gpiozero python3-lgpio
Run:       python3 mouse_drive.py      (use sudo if the mouse gives a
                                        permission error)
"""
import signal
import sys

from evdev import InputDevice, ecodes, list_devices
from gpiozero import OutputDevice, PWMOutputDevice

SPEED = 0.65  # duty cap: keeps ~6 V motors safe on a ~9 V battery (6 / 9)


class Channel:
    """One TB6612FNG channel: a PWM speed pin plus two direction pins."""

    def __init__(self, pwm_gpio, in1_gpio, in2_gpio):
        self.pwm = PWMOutputDevice(pwm_gpio)
        self.in1 = OutputDevice(in1_gpio)
        self.in2 = OutputDevice(in2_gpio)

    def forward(self):
        self.in1.on()
        self.in2.off()
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

stby = OutputDevice(26)
left = Channel(13, 20, 21)   # PWMA, AIN1, AIN2
right = Channel(12, 5, 6)    # PWMB, BIN1, BIN2

mouse = find_mouse()
if mouse is None:
    sys.exit("No mouse found. Is it plugged into the Pi?")
print("Using mouse:", mouse.name)
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
finally:
    left.stop()
    right.stop()
    stby.off()
