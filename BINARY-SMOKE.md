# LeafLink infected-versus-healthy smoke test

Real Kaggle Tesla T4, CUDA 13.0, PyTorch 2.11.0+cu130. Twenty intact plant/leaf scenes: ten source-labeled infected and ten source-labeled healthy. Inputs and labels were frozen before this run. Full frames retain the entire source scene, proportionally resized/padded to 1280×720. No retraining, model replacement, species scoring or result-based threshold changes.

## Accuracy results

**Full pipeline, current 10% event rule: 63.2% accuracy on 19/20 classified photos.** Correct decisions per all submitted photos: 60.0%; unknown/rejected: 1. This evaluates B2 → leaf proposals → binary leaf verdicts → photo event threshold.

**Given an annotated leaf region, classifier-only: 68.8% accuracy (44/64).** These crops use the source annotation boxes with square20% context from the same720p frames, resized384, and bypass B2/heuristic. They test the supplied classifier's infected/healthy mapping separately from extraction. They are 64 regions within20 photos, not independent plants. This is not the accuracy of the automatically extracted proposals, whose individual binary labels are unavailable.

| Measurement | Full pipeline photos | Annotated leaf regions |
|---|---:|---:|
| True infected → infected | 10 | 34 |
| True healthy → healthy | 2 | 10 |
| Healthy → infected (false positive) | 7 | 17 |
| Infected → healthy (false negative) | 0 | 3 |
| Unknown infected / healthy | 0 / 1 | 0 / 0 |
| Sensitivity on classified | 100.0% | 91.9% |
| Specificity on classified | 22.2% | 37.0% |
| Precision | 58.8% | 66.7% |
| Balanced accuracy on classified | 61.1% | 64.5% |

For transparency, the alternative rule 'at least one predicted infected candidate' yields 63.2% accuracy on classified photos (TP 10, TN 2, FP 7, FN 0). The main result above uses the existing app's preselected 10% rule; this alternative was not used to select a better result. An internal healthy class maps to healthy and all disease classes map to infected; plant identification is not required.

## Every photo and its timing

| Photo | Expected | Predicted | Outcome | Candidates / infected | Process s | Queue s | Total s |
|---|---|---|---|---:|---:|---:|---:|
| infected-01 | infected | infected | correct | 26 / 19 | 50.80 | 0.01 | 50.85 |
| healthy-01 | healthy | infected | false_positive | 8 / 7 | 15.19 | 49.82 | 65.04 |
| infected-02 | infected | infected | correct | 9 / 8 | 15.38 | 64.00 | 79.43 |
| healthy-02 | healthy | healthy | correct | 1 / 0 | 15.17 | 78.39 | 93.60 |
| infected-03 | infected | infected | correct | 39 / 37 | 16.36 | 92.56 | 108.97 |
| healthy-03 | healthy | infected | false_positive | 32 / 24 | 16.02 | 107.94 | 123.99 |
| infected-04 | infected | infected | correct | 17 / 16 | 15.46 | 122.95 | 138.45 |
| healthy-04 | healthy | infected | false_positive | 14 / 13 | 15.44 | 137.41 | 152.89 |
| infected-05 | infected | infected | correct | 11 / 11 | 15.31 | 151.87 | 167.21 |
| healthy-05 | healthy | infected | false_positive | 14 / 7 | 15.61 | 166.18 | 181.82 |
| infected-06 | infected | infected | correct | 20 / 11 | 15.74 | 180.79 | 196.56 |
| healthy-06 | healthy | infected | false_positive | 3 / 2 | 15.47 | 195.53 | 211.03 |
| infected-07 | infected | infected | correct | 34 / 22 | 16.24 | 210.01 | 226.28 |
| healthy-07 | healthy | healthy | correct | 1 / 0 | 15.37 | 225.25 | 240.65 |
| infected-08 | infected | infected | correct | 17 / 17 | 16.76 | 239.63 | 256.41 |
| healthy-08 | healthy | unknown | unknown | 0 / — | 15.70 | 255.39 | 271.12 |
| infected-09 | infected | infected | correct | 17 / 17 | 15.74 | 270.09 | 285.86 |
| healthy-09 | healthy | infected | false_positive | 5 / 2 | 15.35 | 284.84 | 300.22 |
| infected-10 | infected | infected | correct | 6 / 4 | 15.66 | 299.19 | 314.89 |
| healthy-10 | healthy | infected | false_positive | 2 / 2 | 15.45 | 313.86 | 329.34 |

Median full-image processing: **15.54 s**, range 15.17–50.80 s. One-photo-per-second capacity: FAIL for this serial deployment. Processing includes Python/B2 startup and inference, proposals, crop storage, CUDA classification, SQLite/event/cache work. The first classified frame includes resident classifier startup. Queue and arrival-to-finish are separate. B2 currently executes on CPU and reloads per frame; classification runs on GPU. HTTP/camera/network transport is excluded. Locations are synthetic; images/predictions are real. The classifier-only leaf pass is additional and excluded from these full-pipeline image timings.

Relaxed blur settings: image 0.00025, crop 0.0001. No further tuning occurred during this run. No-candidate/blurred/failed images stay unknown, never healthy. 17 CV events and 17 cached photos were recorded.

## Interpretation and limits

These percentages are measured agreement with the supplied source labels on this small convenience smoke set. Healthy/generic leaf categories are treated as healthy; they are not an independent expert diagnosis of every leaf in the photo. Positive scenes were reused from the blur-policy acceptance check, so this is not held-out validation. Healthy scenes were selected and visually checked before prediction; detached-leaf/tabletop examples were excluded. Dataset categories, source resolutions and backgrounds differ across scenes. Watermarks are retained. No training-overlap audit was possible. Healthy leaves in infected photos must not be assigned infected ground truth just because their source photo is positive; therefore automatically extracted candidates are not given unsupported accuracy labels.

The full-scene and annotated-region tests answer different questions. A healthy/photo false positive can arise from proposals, crop context or classification; a cropped-leaf pass cannot certify the full pipeline. No species mismatch is counted as a binary error. Saved winning-class softmax scores are not calibrated binary infection probabilities. Use a larger independently labeled native-camera set for a general accuracy claim.


Source photos and labels: [PlantDoc detection dataset](https://github.com/pratikkayal/PlantDoc-Object-Detection-Dataset). Full local report includes image overlays and every annotated leaf prediction.
