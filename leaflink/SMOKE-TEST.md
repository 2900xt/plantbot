# Full-scene Kaggle smoke test — November 7, 2026 local

Executed the actual production `createPipeline.submit/tick` path on a Kaggle Tesla T4 (PyTorch 2.11.0+cu130, CUDA 13.0, four logical CPUs, Node 24.11.0). Exact supplied B2/v2/epoch-8 files verified by SHA256. B2 currently runs on CPU and reloads per full image; epoch-8 classification uses a resident CUDA worker. No model/threshold changes or simulations.

Ten unique intact PlantDoc detection TEST scenes: seven tomato, two potato, one apple. Proportional resize with neutral letterbox to 1280 wide × 720 high; no crop/stretch. Nominal one-second submissions; first actual arrival gap 0.914 s, others about 1 s. Physical camera mode remains unverified. Local storage and synthetic locations exercised event mapping/cache; network and camera transport were excluded.

| Scene | Plant | Outcome | Candidates / infected | Processing s | Queue s | Arrival to finish s |
|---|---|---|---:|---:|---:|---:|
| 01 | Tomato | Retake blurry | 0 / unknown | 57.32 | 0.01 | 57.37 |
| 02 | Tomato | Retake blurry | 0 / unknown | 14.78 | 56.36 | 71.23 |
| 03 | Tomato | Complete | 38 / 36 | 29.10 | 70.20 | 99.34 |
| 04 | Tomato | Complete | 17 / 16 | 15.38 | 98.30 | 113.72 |
| 05 | Tomato | Retake blurry | 0 / unknown | 14.46 | 112.70 | 127.18 |
| 06 | Tomato | Retake blurry | 0 / unknown | 14.50 | 126.14 | 140.68 |
| 07 | Tomato | Retake blurry | 0 / unknown | 14.60 | 139.65 | 154.29 |
| 08 | Potato | Retake blurry | 0 / unknown | 14.57 | 153.26 | 167.86 |
| 09 | Potato | Retake blurry | 0 / unknown | 14.47 | 166.83 | 181.33 |
| 10 | Apple | Retake blurry | 0 / unknown | 14.65 | 180.30 | 194.98 |

Median processing **14.63 s/frame**, mean 20.38 s; finite-batch service rate 0.049 frames/s. Cold startup is included in the affected image; one-time environment setup (57.9 s) is excluded. First classified image includes classifier loading/first call. This implementation does not sustain 1 Hz.

Two CV events at the 10% threshold were correctly created, mapped and cached. Both classified images predicted infected, agreeing with their positive source labels. All 55 saved candidates were classified: **52 diseased, 3 healthy**, with no simulation, one attempt per image and consistent binary counts. Plant identification is not required; different internal species labels are not binary errors. Eight blur rejections carry unknown infection status. With only two classified positive scenes and no healthy controls, expert crop labels or training-overlap audit, general binary accuracy remains unmeasured. Source annotations are not exhaustive; partial/mixed-leaf proposals should be reviewed. Winning-class scores are not calibrated infection probabilities.

Full report, per-image and per-candidate CSVs, proposal overlays, crop montage, exact inputs/provenance and raw GPU evidence are delivered separately under `output/reports/LeafLink-Full-Scene-Smoke/` and its report ZIP. Reproduce with workspace `scripts/build_full_scene_kaggle.py`, `pipeline/test/full-scene-smoke.js` and `scripts/report_full_scene_smoke.py` using the same private inputs. Preserve this baseline before changing the B2 adapter, blur rule or extraction.

Sources: [PlantDoc detection dataset](https://github.com/pratikkayal/PlantDoc-Object-Detection-Dataset), [dataset paper and authors](https://arxiv.org/abs/1911.10317). Dataset declares CC BY 4.0; source watermarks retained. [Private GPU run](https://www.kaggle.com/code/narwhalll/leaflink-full-scene-gpu-smoke-nov8), version 2. Version 1 failed during input discovery before processing; version 2 corrected Kaggle's automatic archive extraction. This setup correction did not alter the models, images or processing algorithm.
