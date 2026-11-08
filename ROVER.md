# Rover tab (plantbot)

The **Rover** tab drives the plantbot rover (Raspberry Pi, `http://vittles.local:8000`) from LeafLink: live camera, drive pad (hold W A S D or the arrows), Scan plant, Auto row, E-stop and the rover's sensor readings. The browser talks to the rover directly over `ws://<rover>/ws` and `/stream.mjpg`; LeafLink's server isn't in that path. The rover's 500 ms watchdog stops the motors when keys are released, the tab is hidden or the connection drops.

Scan photos and VOC readings reach LeafLink through the rover's own uplink (device `rover-1`), so scans appear under **Images** and VOC anomalies on the **Field Overview** like any other device.

## Setup on the GPU laptop

1. LeafLink must listen on the network, not only localhost: set `HOST=0.0.0.0` in `.env.field`, then restart. Allow port 8080 through the firewall.
2. Open LeafLink over **http** (e.g. `http://localhost:8080/#rover`). Browsers block an https page from opening `ws://` to the rover.
3. On the rover, put this laptop's address and the API token in `~/plantbot/.env` (see the plantbot README), then restart `plantbot`. The Rover tab shows `Uplink to LeafLink: sending as rover-1` when that works.

VOC samples arrive as `top` = plant-side sensor, `bottom` = background sensor, in `100 * ln(baseline / now)` units (≈ % change, rises with VOCs). The default event rules (top rise 10, differential 5) mean "plant side up about 10%, and 5% more than the background".

The CSP allows `ws:`/`wss:` connections and `http:`/`https:` images so the tab can reach the rover's address.
