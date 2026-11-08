#!/usr/bin/env python3
"""Read an MQ-2 on a Raspberry Pi and send it to LeafLink (POST /api/telemetry/gas).

LeafLink makes the threshold decision; this script only reports readings.

Wiring (MQ-2 module, powered from Pi 3.3V so DO is safe for the GPIO):
  VCC -> 3.3V (pin 1)   GND -> GND (pin 6)   DO -> GPIO17 (pin 11)
  Optional analog: AO -> divider -> ADS1115 A0 (I2C, SDA pin 3, SCL pin 5).

Run:
  export LEAFLINK_URL=http://<laptop-ip>:8080
  export LEAFLINK_TOKEN=<token printed by LeafLink at startup>
  python3 mq2_pi.py                 # digital DO pin
  python3 mq2_pi.py --adc           # ADS1115 analog (needs: sudo apt install python3-smbus)
  python3 mq2_pi.py --fake 600      # no hardware: send a fixed test value
"""
import argparse, json, os, time, urllib.error, urllib.request
from datetime import datetime, timezone


def post(url, token, row):
    req = urllib.request.Request(url.rstrip('/') + '/api/telemetry/gas', json.dumps(row).encode(),
        {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'{e.code}: {e.read(300).decode(errors="replace")}') from None


def make_reader(args):
    if args.fake is not None:
        return lambda: {'value': args.fake, 'units': 'raw'}
    if args.adc:
        import smbus  # ADS1115 single-shot, A0 vs GND, +/-4.096 V
        bus = smbus.SMBus(1)
        def read():
            bus.write_i2c_block_data(0x48, 0x01, [0xC3, 0x83])
            time.sleep(0.01)
            hi, lo = bus.read_i2c_block_data(0x48, 0x00, 2)
            raw = (hi << 8) | lo
            raw -= 65536 if raw & 0x8000 else 0
            return {'value': round(raw * 4.096 / 32768 * 1000, 1), 'units': 'mV'}  # mV at the ADC pin
        return read
    from gpiozero import DigitalInputDevice
    pin = DigitalInputDevice(args.pin, pull_up=True)
    # MQ-2 modules pull DO LOW when gas exceeds the pot setting.
    return lambda: {'alarm': not pin.value}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default=os.getenv('LEAFLINK_URL', 'http://localhost:8080'))
    p.add_argument('--device', default='pi-mq2')
    p.add_argument('--pin', type=int, default=17)
    p.add_argument('--interval', type=float, default=1.0)
    p.add_argument('--adc', action='store_true')
    p.add_argument('--fake', type=float)
    args = p.parse_args()
    token = os.getenv('LEAFLINK_TOKEN')
    if not token:
        p.error('Set LEAFLINK_TOKEN')
    read = make_reader(args)
    while True:
        row = {'device': args.device, 'measured_at': datetime.now(timezone.utc).isoformat(), **read()}
        try:
            r = post(args.url, token, row)
            print(r['status'], row, flush=True)
        except Exception as e:
            print('send failed:', e, flush=True)
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
