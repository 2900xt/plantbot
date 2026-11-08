"""JSON stdin/stdout bridge: SegFormer-B2 ignore mask -> supplied leaf heuristic.

No network downloads. Ground masks are binary, aligned with oriented RGB input.
"""
import base64
import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from leaf_heuristic import extract_leaf_candidates
from leaf_heuristic_v2 import extract_leaf_candidates as extract_v2, LeafHeuristicConfig


def encode(image, fmt):
    output = io.BytesIO()
    image.save(output, format=fmt, **({"quality": 95} if fmt == "JPEG" else {}))
    return base64.b64encode(output.getvalue()).decode("ascii")


def ground_mask(image, model_path):
    # Matches scripts/mask_ground.py, with the B2 checkpoint selected explicitly.
    import torch
    from transformers import SegformerForSemanticSegmentation
    torch.set_num_threads(4)
    directory = Path(model_path)
    model = SegformerForSemanticSegmentation.from_pretrained(directory, local_files_only=True).eval()
    config = json.loads((directory / "preprocessor_config.json").read_text())
    mean = np.array(config["image_mean"], dtype=np.float32)
    std = np.array(config["image_std"], dtype=np.float32)
    names = {"earth", "grass", "road", "sidewalk", "floor", "sand", "path", "dirt track", "land"}
    ground_ids = [int(i) for i, name in model.config.id2label.items() if name.strip() in names]
    if not ground_ids:
        raise ValueError("Ground model has no supported ground classes")
    pixels = np.asarray(image.resize((512, 512), Image.Resampling.BILINEAR), dtype=np.float32) / 255
    tensor = torch.from_numpy(((pixels - mean) / std).transpose(2, 0, 1).copy()).unsqueeze(0)
    with torch.inference_mode():
        logits = model(pixel_values=tensor).logits
        labels = torch.nn.functional.interpolate(logits, size=(512, 512), mode="bilinear", align_corners=False).argmax(1)[0].numpy()
    raw = np.isin(labels, ground_ids)
    broad = ndi.binary_closing(np.pad(raw, 8, mode="edge"), structure=np.ones((17, 17), bool))[8:-8, 8:-8] | raw
    holes = ndi.binary_fill_holes(broad) & ~broad
    components, _ = ndi.label(holes)
    sizes = np.bincount(components.ravel())
    small = sizes <= round(raw.size * .02)
    small[0] = False
    broad |= small[components]
    return Image.fromarray(broad.astype(np.uint8) * 255).resize(image.size, Image.Resampling.NEAREST)


def process(payload):
    def progress(stage, message):
        print('LEAFLINK_PROGRESS:' + json.dumps({"stage":stage,"message":message}), file=sys.stderr, flush=True)
    image = Image.open(io.BytesIO(base64.b64decode(payload["image"], validate=True))).convert("RGB")
    if payload.get("mask"):
        mask = Image.open(io.BytesIO(base64.b64decode(payload["mask"], validate=True)))
        if mask.mode not in ("1", "L"):
            raise ValueError("Ground mask must be single-channel binary, not a multiclass label image")
        mask = mask.convert("L")
        mask_source = "provided"
    elif payload["ground_mode"] == "local":
        progress("ground_mask", "SegFormer-B2 is identifying ground regions")
        mask = ground_mask(image, payload["ground_model_dir"])
        mask_source = "segformer-b2-local"
    else:
        raise ValueError("An aligned binary ground_mask upload is required in provided mode")
    progress("leaf_separation", "Separating foliage into individual leaf candidates")
    if payload.get('heuristic_version','v2') == 'v2':
        cfg=LeafHeuristicConfig(max_candidates=payload['max_candidates'],
            blur_threshold=payload.get('blur_threshold',.00025),
            min_crop_sharpness=payload.get('crop_sharpness_threshold',.0001))
        result=extract_v2(image,mask,config=cfg)
        result['quality'].update(blur_threshold=cfg.blur_threshold,crop_sharpness_threshold=cfg.min_crop_sharpness)
    else:
        result = extract_leaf_candidates(image, mask, min_leaf_extent=payload["min_leaf_extent"], crop_mode=payload["crop_mode"])
    all_candidates = result["candidates"]
    candidates = []
    for item in all_candidates[:payload["max_candidates"]]:
        item = dict(item)
        item["image"] = encode(item.pop("crop"), "JPEG")
        candidates.append(item)
    return {"candidates": candidates, "ground_mask": encode(mask, "PNG"), "mask_source": mask_source,
            "source_size": [image.width, image.height], "crop_size": [384, 384],
            "candidate_count": len(candidates), "total_candidates": len(all_candidates),
            "truncated": len(all_candidates) >= payload['max_candidates'], "rejected_ground": result.get("rejected_ground",result.get('rejected',{}).get('ground',0)),
            "status":result.get('status','ok' if candidates else 'no_candidates'),"quality":result.get('quality',{}),
            "heuristic": "supplied-hsv-watershed-"+payload.get('heuristic_version','v2'), "crop_mode": payload["crop_mode"],
            "min_leaf_extent": payload["min_leaf_extent"]}


if __name__ == "__main__":
    try:
        print(json.dumps(process(json.load(sys.stdin))))
    except Exception as error:
        # No image bytes or credential-bearing environment is logged.
        print(f"Leaf extraction failed ({type(error).__name__}): {error}", file=sys.stderr)
        raise SystemExit(1)
