# LeafLink field web app

This is the assembled web application. The compiled browser app is in `dist/`; the Node API/queue, Python model bridge, SQLite storage, both supplied model bundles, and hardware senders are included. Open it through the server, not by double-clicking `dist/index.html`.

## Start on Windows

1. Install Node.js 24+ and Python with a matching CUDA-enabled PyTorch/torchvision pair. Use the [official PyTorch installation selector](https://pytorch.org/get-started/locally/) for your OS, Python version and GPU. The supplied artifact's original audit stack is documented in `models/LeafCNN-Epoch8-GPU/gpu-benchmark.json`; prefer that stack where possible.
2. Install the remaining Python dependencies: `py -m pip install -r requirements-field.txt`. This includes transformers, NumPy, scipy, Pillow and scikit-image. It does not guarantee that your PyTorch build supports CUDA: check `py -c "import torch; print(torch.__version__, torch.cuda.is_available())"`.
3. Double-click **Start-LeafLink.cmd**, or run `./start-field.ps1`. The first run installs Node dependencies, creates `.env.field` with a random team token, and starts the server.
4. Open **http://localhost:8080**. Paste the token shown in the startup window into Connection. Keep the server window open. Use Field setup → Event rules to set the field origin and advanced thresholds before collecting data.

On the original development machine, configuration can reuse the existing `../.test-deps` Python modules. That installation is CPU-only. B2 and leaf extraction run there; real epoch-8 inference waits for a CUDA runtime. Set `PYTHON_BIN` and clear `PYTHON_MODULE_PATH` in `.env.field` when switching to a new CUDA environment.

`npm start` also starts the new field app. The earlier `.env` is preserved; `npm run start:legacy` uses it explicitly. Use a fresh DATA_DIR when changing storage drivers. Production defaults are real GPU classification, local B2 ground masking, v2 splitting, and local private object storage. No sample observations are installed in the production database.

## What is connected

`photo → receipt/order metadata → durable queue/private storage → SegFormer-B2 ground-ignore mask → supplied v2 heuristic → 384×384 leaf crops → supplied epoch-8 FP16 CUDA classifier → infected sample share → CV event + temporary photo cache → field map`

The app now uses a weaker blur policy for 720p input: `LEAF_BLUR_THRESHOLD=0.00025` and `MIN_CROP_SHARPNESS=0.0001` (both 10× lower than the original v2 defaults). Lower values admit softer frames/crops. The original heuristic file and classifier weights remain intact; the adapter passes configuration overrides and records the thresholds with extraction quality. Existing captures retain their old results; new captures/re-extractions use the configured policy. Restart the backend after changing environment values. See `BLUR-POLICY.md` for the extraction-only replay and rollback values.

The ground filter is **SegFormer-B2**, separate from optional Backblaze B2 storage. The classifier preserves the supplied model bytes, hash-bound metadata, saved class order and preprocessing. The resident Python worker loads it once and reuses it. It never silently substitutes a CPU model or changes precision.

The new default image rule is **10% infected leaf samples**. The overview Detection slider adjusts the required share of leaf candidates predicted diseased, initially across 10–50%; advanced rules support 1–100%. The trigger is `100 × diseased_count / leaf_count >= infected_percent`. This is not a model-confidence cutoff. A qualifying photo creates one CV event. The image rule, threshold and cache duration are snapshotted on acceptance, so changes affect new photos. Existing databases/count-only callers and old capture snapshots retain count-based behavior; an explicit slider change enables percentage mode. Samples can include duplicate fragments of one leaf. Blurry/empty/failed/unavailable-model images produce neither a healthy verdict nor an image event.

Cache defaults: **24 hours, at most 100 photos**, configurable in Event rules. Cached JPEG copies are stored in SQLite, expire automatically during worker ticks and reads, and return HTTP 410 after expiration or eviction. Event records persist after cache expiration. Original capture photos and crops are retained separately in private object storage for history and retries. SQLite can retain free pages after logical deletion; expiration is not secure disk erasure. Original capture storage requires operational capacity management.

## Sketch dashboard

The UI follows the user's paper layout and SketchPad visual reference, using the approved [public TypeUI Doodle guide](https://www.typeui.sh/design-skills/doodle), self-hosted Delius Swash Caps and JetBrains Mono, cream paper, ink outlines and peach controls. Font licenses are bundled in `app/public/fonts/` and the compiled assets. The authenticated TypeUI MCP package was not retrieved; the user approved the public-guide fallback.

Field Overview contains the recorded same-device GPS trail, photo observation points and anomaly markers, plus separate collapsible VOC/CV panels. A triangle means one signal modality; a gray skull means both CV and VOC within a displayed cell/area. Zoomed-out markers may aggregate nearby grid cells. Purple marks/labels identify simulation. These are display conventions, not a disease-severity or plant-death claim.

Use the map's +/− buttons to zoom, drag the map to pan, Fit field to reset the extent, or its corner control to expand where supported. Filters select CV, VOC or Both. Hover over a map anomaly or a VOC/CV sidebar row, or focus it with the keyboard for an anchored mini-preview with a linked photo, infected-sample share, VOC reading and time when available. Hover does not filter the panels. Click/tap pins the preview; Escape/X dismiss it, and moving into the preview keeps it visible. An anomaly without a linked photo shows its readings instead. Click a point for concise details and an Open image button; double-click an image-linked point or press Enter to open its photo in Images.

Images holds the recent-photo gallery, full source photo, leaf predictions, upload form and optional record table. The image-panel X and full-photo X/Escape return to the previous map center and zoom, signal filter and panel states. View state is held in tab session storage; map geometry changes invalidate an old map extent. Browser document-scroll restoration is best-effort, while map pan/zoom were verified exactly. Sensor ingestion and advanced rules are under Field setup.

## VOC and location streams

VOC references are fixed per device from the first accepted sample after initialization/reset. For each synchronized top/bottom pair:

```
top_change = top - top_reference
differential_change = top_change - (bottom - bottom_reference)
candidate = top_change > top_rise_threshold
            AND differential_change > differential_threshold
```

Emit one **VOC anomaly** when both conditions hold for X consecutive samples. Continued elevation produces no repeated event until a condition fails, a long gap occurs, or the device enters another grid cell. A rule change clears the references. Device states persist across restart. Readings without position can still form an anomaly; the event stays unmapped. Top and bottom must use comparable calibrated units and direction. These events are anomaly indicators, not a diagnosis of plant disease.

Authenticated ingestion:

```
POST /api/telemetry/voc
{
  "device":"rover-1", "sample_id":"sample-0001",
  "measured_at":"2026-11-08T01:00:00Z",
  "top":100, "bottom":95, "units":"raw",
  "location":{"lat":40.8075,"lon":-73.9626}
}

POST /api/telemetry/location
{
  "device":"rover-1", "measured_at":"2026-11-08T01:00:00Z",
  "location":{"lat":40.8075,"lon":-73.9626}
}
```

Example coordinates/timestamps above are illustrative. Use live UTC timestamps. VOC ingestion also accepts an array of up to 500 samples, committed atomically. Timestamps must increase per device; duplicate VOC `sample_id` with identical data replays without recounting, while changed data is rejected. Replayed location samples with identical timestamp/position are safe. Separate receipt timestamps record arrival. Raw streams retain the latest 50,000 VOC readings and latest 50,000 location readings; events persist. The dashboard shows the latest 100 readings and latest 500 events, with the grid aggregating all retained events.

Images and VOC samples can include their own explicit position or use the latest location for the same device, only if that fix predates the capture/sample and is within the configured age (default 10 seconds). No coordinates are invented. Use the same device identifier for camera and GPS if they belong to one rover. The basic grid uses a local equirectangular meter projection relative to your origin, including negative cell coordinates. It maps point events, not observed areas or camera footprint; use it for local fields rather than continental mapping. Changing grid geometry remaps historical event positions. GPS errors can exceed the default 5 m cells.

### Hardware

Set `LEAFLINK_URL` and `LEAFLINK_TOKEN` in the device environment. From the hardware directory:

```
python send_image.py photo.jpg --device rover-1 --lat 40.8075 --lon -73.9626 --wait
python stream_telemetry.py --device rover-1 --input measurements.ndjson
```

For live input, pipe your sensor reader's NDJSON output into `stream_telemetry.py --device rover-1`. Each row contains `kind:"voc"` or `kind:"location"` and the fields above; the sender adds UTC time and VOC sample IDs if missing and retries transient failures using the same payload. Persist sample IDs/timestamps at the hardware source if replaying after a device reboot. The browser Field setup → Sensor stream view also accepts manual measurements, JSON/NDJSON imports and an optional GPS watch. Browser GPS needs user permission and HTTPS or localhost. No actual sensor/GPS hardware was connected during development.

## API and deployment

All endpoints except `/api/health` require `Authorization: Bearer <API_TOKEN>`. Photos and API responses use `Cache-Control: no-store`. Credentials remain server-side except the team API token, kept in browser tab session storage. This prototype is a single-team system, not tenant-isolated accounts. Put it behind HTTPS for remote access; never put credentials in URLs.

New endpoints: `GET /api/field`, `GET /api/events`, `GET /api/events/:id/image`, `GET/PUT /api/settings`, `POST /api/telemetry/voc`, `POST /api/telemetry/location`, and `POST /api/telemetry/reset` with `{ "device":"rover-1" }`. Existing capture endpoints are retained. Image multipart accepts `image`, optional `ground_mask`, `device`, and a `metadata` JSON string with optional `captured_at` and `location`. Server order numbers survive restart and upload replay. Sequential polling updates the dashboard every three seconds; device streams continue while the page is closed.

Host the complete folder on a CUDA-enabled machine with persistent `DATA_DIR`, Node 24+, Python dependencies and local model paths. Cloudflare Workers/Sites static hosting cannot execute this Python/CUDA backend. This delivery is compiled and self-hostable; no public cloud deployment was performed. An optional `Dockerfile.gpu` / `compose.gpu.yaml` template is included for a GPU container host, but Docker/GPU deployment was not tested here. The earlier CPU `Dockerfile`/compose files remain for legacy use and are not the epoch-8 deployment.

If using Backblaze, copy the B2 variables from `.env.example` into `.env.field` and set `STORAGE_DRIVER=b2`. Otherwise local object storage is complete for local use. One Node/worker process per DATA_DIR is supported. Back up SQLite together with local objects. Use `MODEL_MODE=http` only for a separately deployed compatible model endpoint; it must return the validated per-leaf contract described in the original README. `MODEL_MODE=demo` is explicit simulation and marks image events separately.

## Verification and limits

`npm run build` compiles both dashboard and legacy capture interface. `npm test` covers metadata/order/replay, count and percentage threshold snapshots, event/cache lifecycle, persistence/restart, telemetry chronology/duplicates/atomicity, device isolation, gaps, location age, grid aggregation, blur and unavailable-CUDA handling, plus existing storage/auth/model-contract regressions.

`npm run test:models` is an additional development-machine check using local fixture photos and Python modules outside the share bundle. It ran the actual packaged B2 model and v2 heuristic on IMG_4619: **40 crops**, with a clearly simulated disease classifier. It also verified that the real epoch-8 runner refuses the installed CPU-only PyTorch.

A subsequent real Kaggle T4 smoke test ran the production queue and exact model files on ten intact, letterboxed 1280×720 scenes at nominal 1-second arrivals. Both scenes reaching classification predicted infected and triggered events, agreeing with their positive source labels (55 candidates: 52 diseased, 3 healthy). Eight scenes were rejected by the unchanged blur rule and remain unknown. Median full-image processing was 14.63 s. Event/cache invariants passed; 1 Hz capacity failed. Plant identification is not required and is not a binary acceptance metric. General infected/healthy accuracy remains unmeasured without healthy controls and more classified scenes. See `SMOKE-TEST.md` and the separately delivered full report/evidence; original model-bundle validation reports remain distinct.

Browser checks for the sketch dashboard exercised the percentage slider, both collapsible anomaly sections, signal filters, marker/photo navigation, full-photo dialog, gray skull, zoom/pan and return-to-map X/Escape. Map pan, zoom and marker transforms matched before/after image viewing. Layouts at 1280px, 764px and 390px had no page overflow; the phone marker target measured about 50px. Earlier integration also checked uploads and telemetry. All 25 backend/map tests passed. Preview records are isolated fixtures, never copied into the runtime or release. The Impeccable context/detector engine could not run; a fresh finish reviewer assessed source/screenshots instead.

Related chat context reviewed: “Build plant disease image pipeline”, “Add perspective-independent ground removal”, “Assess plant disease from VOCs”, and “Plan custom plant disease model”. Their artifacts were treated as implementation context, not as authorization to retrain models or upload data elsewhere.
