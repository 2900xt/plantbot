# Relaxed blur policy

The user requested a weaker filter after the baseline GPU test rejected eight of ten full-scene 720p photos.

| Setting | Original v2 | New app default |
|---|---:|---:|
| LEAF_BLUR_THRESHOLD | 0.0025 | 0.00025 |
| MIN_CROP_SHARPNESS | 0.001 | 0.0001 |

Both minimum sharpness thresholds are 10× lower. The adapter passes these overrides to the supplied v2 configuration; the supplied heuristic/model files and classifier weights are unchanged. Extraction quality records the effective thresholds. Set the original values and restart the backend to roll back. Values must be finite and within 0–1; zero explicitly disables the corresponding sharpness floor.

Validation replay used the exact normalized images and saved real B2 masks from the original Kaggle test. **All ten original frames now produce candidates**, compared with two before. Candidate counts by scene: 26, 9, 39, 17, 11, 20, 34, 17, 17, 6 (196 total). All twenty deliberately blurred controls (each frame with Gaussian radius 3 and 8) still return `retake_blurry` and no candidates. The backend regression suite passes 27 tests, including adapter threshold propagation, defocus rejection and rollback/invalid configuration.

This is a local extraction-only replay, not a new GPU classification/timing run. These ten scenes informed the adjustment, so the result is not held-out validation. It establishes acceptance behavior and preservation of an obvious-defocus guard, not binary disease accuracy. Original GPU timings and predictions remain the original baseline. Native camera captures and healthy controls are still needed for broader validation.

Existing finished captures retain their results. New processing uses the new defaults; a `retake_blurry` capture already has a persisted empty extraction manifest, so simply retrying that old record does not re-extract it under a new policy. Re-upload it as a new capture for this change. Config overrides also apply when a new mask resets extraction. This preserves original evidence rather than silently rewriting prior results.

Reproduce in the source workspace with `py scripts/check_relaxed_blur.py`; observations are in `outputs/smoke-full-images/relaxed-blur-check.json` and included in the report package.
