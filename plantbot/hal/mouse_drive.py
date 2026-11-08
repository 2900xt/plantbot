"""Tank drive from a USB mouse plugged into the Pi: each button runs one wheel one way.

Commands go through Mission.handle, same as the dashboard drive pad, so E-stop, auto mode
and scans still lock the wheels out. evdev is imported lazily so laptops/--mock need none.

The state of the mouse is shown as devices["mouse"] (LeafLink Rover tab, /api/status):
"ok", "not found", "no permission", "busy" (another program grabbed it), "evdev missing".
Every failure is logged once per change, never silently retried.
"""
from __future__ import annotations

import asyncio
import errno
import glob
import logging
import os

log = logging.getLogger(__name__)

RESEND_S = 0.1  # re-send while held so the motor watchdog stays fed
RETRY_S = 2.0   # look for the mouse again this often while it's missing


def _find_mouse(evdev, path):
    """(device, problem). device is None when no usable mouse; problem says why."""
    if path:
        try:
            return evdev.InputDevice(path), None
        except PermissionError:
            return None, "no permission"
        except OSError:
            return None, "not found"
    nodes = sorted(glob.glob("/dev/input/event*"))
    readable = [p for p in nodes if os.access(p, os.R_OK | os.W_OK)]
    for p in readable:
        try:
            d = evdev.InputDevice(p)
        except OSError:  # unplugged between listing and opening
            continue
        try:
            keys = d.capabilities().get(evdev.ecodes.EV_KEY, [])
            if evdev.ecodes.BTN_LEFT in keys and evdev.ecodes.BTN_RIGHT in keys:
                return d, None
        except OSError:
            pass
        d.close()
    if nodes and not readable:
        return None, "no permission"
    return None, "not found"


_HINTS = {
    "not found": "no mouse on /dev/input; is it plugged into the Pi?",
    "no permission": "can't open /dev/input/event*: add the service user to the input group "
                     "(sudo usermod -aG input $USER, then reboot or restart plantbot)",
    "busy": "another program has grabbed the mouse; stop it "
            "(sudo systemctl disable --now mouse-drive wheel-capture, or quit tools/mouse_drive.py)",
}


async def run(mission, cfg) -> None:
    """Reads the mouse forever; reconnects if it's unplugged. Never dies silently."""
    devices = mission.r.devices
    try:
        import evdev  # lazy
    except ImportError:
        log.error("mouse drive: python evdev not installed (pip install -r requirements-pi.txt), disabled")
        devices["mouse"] = "evdev missing"
        return
    b = cfg.buttons
    codes = {name: evdev.ecodes.ecodes[getattr(b, name)]
             for name in ("left_fwd", "left_back", "right_fwd", "right_back")}
    names = {code: f"{getattr(b, name)}({name})" for name, code in codes.items()}
    # Wheel press toggles the photo recorder (plantbot/recorder.py), if both are configured.
    recorder = getattr(mission.r, "recorder", None)
    record_code = evdev.ecodes.ecodes[b.record] if b.get("record") and recorder else None
    log.info("mouse drive: %s%s", ", ".join(names.values()),
             f", {b.record}(record)" if record_code is not None else "")

    def status(s: str) -> None:
        if devices.get("mouse") != s:
            devices["mouse"] = s
            (log.info if s == "ok" else log.warning)("mouse drive: %s%s", s,
                                                      f" - {_HINTS[s]}" if s in _HINTS else "")

    devices["mouse"] = "searching"
    while True:
        try:
            dev, problem = _find_mouse(evdev, cfg.get("device"))
        except Exception as e:  # noqa: BLE001 - keep looking, but say why
            dev, problem = None, f"error: {e}"
        if dev is None:
            status(problem)
            await asyncio.sleep(RETRY_S)
            continue
        if cfg.get("grab", True):
            try:
                dev.grab()  # keep clicks away from any desktop on the Pi
            except OSError as e:
                dev.close()
                status("busy" if e.errno == errno.EBUSY else f"error: {e}")
                await asyncio.sleep(RETRY_S)
                continue
        held: set[int] = set()
        sender = None
        try:
            log.info("mouse drive: using %s (%s)", dev.path, dev.name)
            status("ok")
            async for ev in dev.async_read_loop():
                if ev.type != evdev.ecodes.EV_KEY:
                    continue
                if ev.code == record_code:
                    if ev.value == 1:
                        try:
                            log.info("mouse %s -> recorder %s", b.record, recorder.toggle(ev.timestamp()))
                        except Exception:  # a recorder fault must not take the drive down
                            log.exception("mouse %s: recorder toggle failed", b.record)
                    continue
                if ev.code not in names:
                    continue
                if ev.value == 1:
                    held.add(ev.code)
                elif ev.value == 0:
                    held.discard(ev.code)
                else:
                    continue  # key repeat
                left, right = _command(cfg.speed, codes, held)
                log.info("mouse %s %s -> drive L=%+.2f R=%+.2f", names[ev.code], "down" if ev.value else "up",
                         left, right)
                if held and (sender is None or sender.done()):
                    sender = asyncio.create_task(_send_while_held(mission, cfg.speed, codes, held))
                elif not held:
                    if sender:
                        sender.cancel()
                    await mission.handle({"drive": [0.0, 0.0]})
        except asyncio.CancelledError:
            raise
        except OSError as e:
            log.warning("mouse drive: lost %s (%s), waiting for it to come back", dev.path, e)
            devices["mouse"] = "not found"
        except Exception:  # noqa: BLE001 - log it and start over rather than lose the mouse for good
            log.exception("mouse drive: crashed, restarting")
            devices["mouse"] = "error"
        finally:
            if sender:
                sender.cancel()
            await mission.handle({"drive": [0.0, 0.0]})
            try:
                dev.close()
            except OSError:
                pass
        await asyncio.sleep(1)


def _command(speed: float, codes: dict, held: set) -> tuple[float, float]:
    left = speed * ((codes["left_fwd"] in held) - (codes["left_back"] in held))
    right = speed * ((codes["right_fwd"] in held) - (codes["right_back"] in held))
    return left, right


async def _send_while_held(mission, speed: float, codes: dict, held: set) -> None:
    while held:
        await mission.handle({"drive": list(_command(speed, codes, held))})
        await asyncio.sleep(RESEND_S)
