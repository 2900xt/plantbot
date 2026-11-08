"""Tank drive from a USB mouse plugged into the Pi: each button runs one wheel one way.

Commands go through Mission.handle, same as the dashboard drive pad, so E-stop, auto mode
and scans still lock the wheels out. evdev is imported lazily so laptops/--mock need none.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

RESEND_S = 0.1  # re-send while held so the motor watchdog stays fed


def _find_mouse(evdev, path):
    if path:
        return evdev.InputDevice(path)
    for p in evdev.list_devices():
        d = evdev.InputDevice(p)
        caps = d.capabilities()
        if evdev.ecodes.EV_REL in caps and evdev.ecodes.BTN_LEFT in caps.get(evdev.ecodes.EV_KEY, []):
            return d
        d.close()
    return None


async def run(mission, cfg) -> None:
    """Reads the mouse forever; reconnects if it's unplugged."""
    try:
        import evdev  # lazy
    except ImportError:
        log.warning("mouse drive: python evdev not installed, disabled")
        return
    b = cfg.buttons
    codes = {name: evdev.ecodes.ecodes[getattr(b, name)]
             for name in ("left_fwd", "left_back", "right_fwd", "right_back")}
    names = {code: f"{getattr(b, name)}({name})" for name, code in codes.items()}
    log.info("mouse drive: %s", ", ".join(names.values()))
    while True:
        dev = _find_mouse(evdev, cfg.get("device"))
        if dev is None:
            await asyncio.sleep(2)
            continue
        log.info("mouse drive: using %s (%s)", dev.path, dev.name)
        held: set[int] = set()
        sender = None
        try:
            if cfg.get("grab", True):
                dev.grab()  # keep clicks away from any desktop on the Pi
            async for ev in dev.async_read_loop():
                if ev.type != evdev.ecodes.EV_KEY or ev.code not in codes.values():
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
        except OSError as e:
            log.warning("mouse drive: lost %s (%s), waiting for it to come back", dev.path, e)
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
