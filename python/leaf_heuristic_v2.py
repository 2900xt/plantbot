"""Post-B2 leaf proposals, v2. Dependencies: numpy, scipy, Pillow, scikit-image.

Heuristic only: no attachment inference, disease inference or leaf confidence.
Pixel thresholds operate at max_edge resolution. Defaults were inspected on
three prototype photos, not validated on an independent labeled dataset.
"""
from collections import Counter
from dataclasses import asdict, dataclass
import time

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from skimage.feature import peak_local_max
from skimage.measure import regionprops
from skimage.segmentation import watershed


@dataclass(frozen=True)
class LeafHeuristicConfig:
    max_edge: int = 1200
    output_size: int = 384
    blur_threshold: float = .0025  # smoothed grayscale Laplacian variance, [0,1]
    min_extent: int = 70
    max_extent: int = 300
    min_area: int = 800
    min_crop_sharpness: float = .001
    max_aspect: float = 4.0
    min_solidity: float = .55
    min_coverage: float = .35
    max_ground_overlap: float = .50
    seed_spacing: int = 35
    min_seed_radius: float = 8
    max_seeds: int = 200
    growth_radius: int = 6
    padding: float = .10
    duplicate_iou: float = .65
    max_candidates: int = 40
    compactness: float = .001


def extract_leaf_candidates(image, ground_mask, *, config=None):
    """Return dict with status, candidates, quality, config and debug masks.

    image: EXIF-oriented PIL image; ground_mask: aligned binary HxW PIL/numpy
    mask, 0=keep, 1/255=ignore. Never pass multiclass B2 labels as the mask.
    All candidate bounds/crop_bounds use ORIGINAL image pixels (exclusive xyxy).
    Each crop is an RGB PIL image resized from the original to output_size.
    vegetation_mask/ground_mask/labels use working_size; use that size to display
    them, not original coordinates. Inputs are unchanged; no disk I/O occurs.

    status='retake_blurry' means no crops should enter the disease model.
    status='no_candidates' does NOT mean healthy/no leaves. quality is diagnostic,
    not confidence. Fully brown leaves can be missed because seeds need green or
    yellow tissue. Overlapping leaves can still merge or split incorrectly.
    """
    cfg = config or LeafHeuristicConfig()
    if cfg.max_edge < 32 or cfg.output_size < 1 or cfg.seed_spacing < 1 or cfg.growth_radius < 1:
        raise ValueError('Invalid working size, output size, seed spacing or growth radius')
    if not (0 <= cfg.max_ground_overlap <= 1 and 0 <= cfg.duplicate_iou <= 1):
        raise ValueError('Overlap thresholds must be between 0 and 1')
    start = time.perf_counter()
    image = image.convert('RGB')
    raw_mask = np.asarray(ground_mask)
    if raw_mask.shape != (image.height, image.width) or not np.isin(raw_mask, [0, 1, 255]).all():
        raise ValueError('ground_mask must be aligned binary HxW: 0=keep, 1/255=ignore')
    work = image.copy()
    work.thumbnail((cfg.max_edge, cfg.max_edge), Image.Resampling.LANCZOS)
    ground = np.asarray(Image.fromarray((raw_mask > 0).astype(np.uint8)).resize(work.size, Image.Resampling.NEAREST)) > 0
    rgb = np.asarray(work, dtype=np.float32) / 255
    hsv = np.asarray(work.convert('HSV'), dtype=np.float32) / 255
    gray = rgb @ np.array([.299, .587, .114], dtype=np.float32)
    smooth = ndi.gaussian_filter(gray, .6)
    laplacian = ndi.laplace(smooth)
    focus = float(laplacian.var())
    rejection = Counter()
    result = dict(status='no_candidates', candidates=[], quality={'blur_score': focus},
                  working_size=work.size, original_size=image.size, config=asdict(cfg),
                  ground_mask=ground, vegetation_mask=np.zeros(ground.shape, bool),
                  labels=np.zeros(ground.shape, np.int32), rejected=rejection)
    def finish():
        result['rejected'] = dict(rejection)
        result['elapsed_seconds'] = round(time.perf_counter() - start, 3)
        return result
    if focus < cfg.blur_threshold:
        result['status'] = 'retake_blurry'
        return finish()

    gradient = np.hypot(ndi.sobel(smooth, 0), ndi.sobel(smooth, 1))
    gradient = np.clip(gradient / max(float(np.percentile(gradient, 95)), 1e-6), 0, 1)
    core = (hsv[..., 0] > .12) & (hsv[..., 0] < .45) & (hsv[..., 1] > .16) & (hsv[..., 2] > .12)
    core &= rgb[..., 1] - rgb[..., 2] > .025
    core = ndi.binary_opening(core, iterations=1)
    # Allow nearby yellow/brown tissue without letting it flood the entire scene.
    local_support = ndi.distance_transform_edt(~core) <= cfg.growth_radius
    warm = (hsv[..., 0] > .04) & (hsv[..., 0] < .45) & (hsv[..., 1] > .12) & (hsv[..., 2] > .10)
    support = core | (warm & local_support & (gradient < .75))
    vegetation = ndi.binary_propagation(core, mask=support)
    vegetation = ndi.binary_closing(vegetation, iterations=1)
    components, _ = ndi.label(vegetation)
    areas = np.bincount(components.ravel())
    keep = areas >= cfg.min_area
    keep[0] = False
    vegetation = keep[components]
    result['vegetation_mask'] = vegetation
    if not vegetation.any():
        return finish()
    distance = ndi.distance_transform_edt(vegetation)
    seed_support = vegetation & ~ground & (distance >= cfg.min_seed_radius)
    peaks = peak_local_max(distance, min_distance=cfg.seed_spacing,
                           threshold_abs=cfg.min_seed_radius, exclude_border=False,
                           labels=seed_support.astype(np.uint8), num_peaks=cfg.max_seeds)
    result['quality']['seed_count'] = len(peaks)
    if not len(peaks):
        return finish()
    markers = np.zeros(ground.shape, np.int32)
    markers[peaks[:, 0], peaks[:, 1]] = np.arange(1, len(peaks)+1)
    # Actual masked watershed: disconnected background cannot carry labels.
    elevation = .65 * gradient - .35 * np.minimum(distance / 30, 1)
    labels = watershed(elevation, markers=markers, mask=vegetation,
                       compactness=cfg.compactness, watershed_line=True)
    result['labels'] = labels
    proposals = []
    for region in regionprops(labels):
        y0, x0, y1, x1 = region.bbox
        h, w = y1-y0, x1-x0
        long, short = max(w, h), min(w, h)
        if region.area < cfg.min_area or long < cfg.min_extent or long > cfg.max_extent:
            rejection['size'] += 1
            continue
        if short < 14 or long/short > cfg.max_aspect or region.solidity < cfg.min_solidity or region.extent < cfg.min_coverage:
            rejection['shape'] += 1
            continue
        overlap = float(ground[y0:y1, x0:x1][region.image].mean())
        if overlap >= cfg.max_ground_overlap:
            rejection['ground'] += 1
            continue
        if x0 == 0 or y0 == 0 or x1 == work.width or y1 == work.height:
            rejection['border'] += 1
            continue
        side = int(np.ceil(long * (1 + 2*cfg.padding)))
        if side > min(work.size):
            rejection['oversized_square'] += 1
            continue
        x = max(0, min((x0+x1-side)//2, work.width-side))
        y = max(0, min((y0+y1-side)//2, work.height-side))
        sharpness = float(laplacian[y:y+side, x:x+side].var())
        if sharpness < cfg.min_crop_sharpness:
            rejection['crop_blur'] += 1
            continue
        proposals.append(dict(bounds=[x0,y0,x1,y1], crop_bounds=[x,y,x+side,y+side],
                              pixels=int(region.area), ground_overlap=round(overlap, 4),
                              crop_sharpness=sharpness, solidity=float(region.solidity)))
    selected = []
    for p in sorted(proposals, key=lambda p:p['pixels'], reverse=True):
        a,b,c,d = p['crop_bounds']
        duplicate = False
        for q in selected:
            x,y,z,t = q['crop_bounds']
            intersection = max(0,min(c,z)-max(a,x))*max(0,min(d,t)-max(b,y))
            if intersection / ((c-a)*(d-b)+(z-x)*(t-y)-intersection) > cfg.duplicate_iou:
                duplicate = True
                break
        if duplicate:
            rejection['duplicate'] += 1
            continue
        selected.append(p)
        if len(selected) >= cfg.max_candidates:
            break
    sx, sy = image.width/work.width, image.height/work.height
    for idx, p in enumerate(selected, 1):
        p['working_bounds'] = p['bounds'][:]
        a,b,c,d = p['bounds']
        p['bounds'] = [int(np.floor(a*sx)),int(np.floor(b*sy)),int(np.ceil(c*sx)),int(np.ceil(d*sy))]
        a,b,c,d = p['crop_bounds']
        side = min(int(np.ceil(max((c-a)*sx,(d-b)*sy))), min(image.size))
        x = max(0,min(int(round((a+c)*sx/2-side/2)),image.width-side))
        y = max(0,min(int(round((b+d)*sy/2-side/2)),image.height-side))
        p['crop_bounds'] = [x,y,x+side,y+side]
        p['id'] = idx
        p['crop'] = image.crop(p['crop_bounds']).resize((cfg.output_size,cfg.output_size),Image.Resampling.LANCZOS)
    result['candidates'] = selected
    result['status'] = 'ok' if selected else 'no_candidates'
    return finish()
