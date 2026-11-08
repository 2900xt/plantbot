# LeafLink

A field-monitoring web app prototype for **infected versus healthy leaf screening**,
VOC anomaly streams and GPS observations. Plant species identification is not a
product requirement.

The image path records receipt/order/time/location, removes ground with SegFormer B2,
extracts leaf candidates with the supplied v2 heuristic, classifies each candidate
with the epoch-8 CUDA FP16 model, and creates a mapped event/cache when the configured
infected-candidate share is reached. The dashboard has Field Overview and Images tabs,
map pan/zoom, separate collapsible VOC/CV panels, map/sidebar hover previews and image
viewing that restores the previous map position.

## Current measured status

Real Kaggle Tesla T4 smoke test, 20 full-scene photos: **63.2% accuracy on 19 classified
photos**, with 10 true positives, 2 true negatives, 7 false positives and 1 unknown
(no candidates). Correct decisions across all20 submitted photos:60%. On64 separately
source-annotated leaf regions, classifier-only binary accuracy was **68.75% (44/64)**:
34 true positives,10 true negatives,17 false positives,3 false negatives.

Median full-image processing: **15.54 seconds**; this serial version does not sustain
one photo per second. The classifier runs on GPU; the current B2 adapter reloads on CPU
per image. This convenience test uses source annotations, includes images used during
blur-policy adjustment, and is not a general field-accuracy claim. False positives
remain a substantial limitation. [Results, per-photo timings and methodology](BINARY-SMOKE.md).

## Run locally

1. Install Node.js24+ and Python. Install a matching CUDA-enabled PyTorch/torchvision
   pair using the [official PyTorch selector](https://pytorch.org/get-started/locally/).
2. Supply the two weight files described in [models/README.md](models/README.md).
3. Install dependencies and build:

```sh
npm ci
python -m pip install -r requirements-field.txt
npm run build
npm start
```

Open http://localhost:8080 and enter the locally displayed API token in Connection.
`npm start` creates `.env.field` and private runtime storage on first launch. Keep
credentials on the backend. On Windows, `Start-LeafLink.cmd` starts the configured app
after installation/build. Set `PYTHON_BIN` if the default Python launcher differs.
Real inference requires the supplied weights and an accessible CUDA GPU.

The default blur floors are `LEAF_BLUR_THRESHOLD=0.00025` and
`MIN_CROP_SHARPNESS=0.0001`; [blur-policy replay and rollback](BLUR-POLICY.md).
Image Detection % means the share of candidate leaves predicted infected, not model
confidence. [Setup, event rules, telemetry API and hosting](FIELD-APP.md).

## Verify

```sh
npm test
npm run build
```

The27 regression tests cover auth/storage, image ingestion and retries, threshold
snapshots, cache/event lifecycle, telemetry chronology/device isolation, map grouping
and the configurable blur guard. Synthetic fixtures are marked in tests; actual GPU
smoke results are reported separately. [Binary summary](docs/binary-summary.json),
[per-image CSV](docs/binary-per-image.csv), [per-leaf CSV](docs/binary-per-leaf.csv),
[source labels and URLs](docs/binary-input-manifest.json).

`test/full-scene-smoke.js` accepts an input-manifest directory and an output directory
to exercise the real queue on a configured CUDA host. `test/annotated-leaf-binary.py`
tests the source-annotated regions separately. The source test photos and model weights
are not committed. Full local reports/evidence remain separate from this public source.

## Deployment

Run Node/Python/CUDA together with a persistent data directory. The frontend is compiled,
but static hosting alone cannot execute the inference/storage backend. A GPU Docker
template is included; it is not deployment-tested. Build `dist` and supply weights before
building that image. Physical sensor/GPS integration and public cloud deployment have
not been validated by the smoke test.

Fonts are self-hosted with their bundled OFL licenses. Smoke images/labels originate
from the [PlantDoc detection dataset](https://github.com/pratikkayal/PlantDoc-Object-Detection-Dataset),
which declares CC BY4.0; this repository records attribution/URLs without redistributing
the downloaded photographs. See the dataset's original terms and provenance.
