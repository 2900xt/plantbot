"""Importable post-B2 leaf-crop heuristic. Requires numpy, scipy and Pillow.

No model downloads or filesystem writes. This proposes regions; it is not a
trained leaf detector. It may miss brown leaves and merge/fragment foliage.
Defaults reproduce the two crops from the tomato-soil prototype image.
"""
import numpy as np
from PIL import Image
from scipy import ndimage as ndi


def extract_leaf_candidates(image, ground_mask, *, min_leaf_extent=80, crop_mode="tight"):
    """Return candidate RGB crops plus debug masks and rejection count.

    image: already EXIF-oriented PIL image, aligned with ground_mask.
    ground_mask: same-size PIL mask or HxW numpy array; 0=keep, 1/255=ignore.
        Supply B2's binary ground ignore mask, not its multiclass label image.
    min_leaf_extent: minimum source-region long edge in pixels (default 80).
    crop_mode: 'tight' uses padded square candidate bounds; 'fixed' uses 384px
        source windows. Both return 384x384 RGB PIL images.

    Result keys:
      candidates: list of dictionaries with crop (PIL), bounds (region xyxy),
        crop_bounds (square source xyxy), center, pixels and ground_overlap.
        Bounds have exclusive right/bottom coordinates. ground_overlap is a
        region fraction, not a detection confidence. Input image is unchanged.
      vegetation_mask: boolean HxW mask before ground rejection.
      rejected_ground: count rejected by ground overlap AFTER size/shape filters.

    Pixel thresholds depend on source resolution. Tight mode skips border
    regions. If a square extends beyond a small image, Pillow pads with black.
    Resizing adds no image detail. Apply your classifier's normalization later.
    """
    if crop_mode not in {"tight", "fixed"}:
        raise ValueError("crop_mode must be 'tight' or 'fixed'")
    if min_leaf_extent < 1:
        raise ValueError("min_leaf_extent must be positive")
    image = image.convert("RGB")
    mask = np.asarray(ground_mask)
    if mask.ndim != 2 or mask.shape != (image.height, image.width):
        raise ValueError("ground_mask must be HxW and aligned with the image")
    if not np.isin(mask, [0, 1, 255]).all():
        raise ValueError("ground_mask must be binary: 0=keep, 1 or 255=ignore")
    ground = mask > 0
    rgb = np.asarray(image, dtype=np.float32) / 255
    hsv = np.asarray(image.convert('HSV'), dtype=np.float32) / 255
    # Green/yellow vegetation gate: intentionally broad enough for yellow leaves.
    vegetation = (hsv[:, :, 0] > .12) & (hsv[:, :, 0] < .43) & (hsv[:, :, 1] > .22) & (hsv[:, :, 2] > .16)
    vegetation &= (rgb[:, :, 1] - rgb[:, :, 2] > .035)
    vegetation = ndi.binary_opening(vegetation, iterations=2)
    # Keep the unmasked support for checking how much a proposal overlaps ground.
    components, count = ndi.label(vegetation)
    sizes = np.bincount(components.ravel())
    keep = sizes >= 250
    keep[0] = False
    vegetation &= keep[components]
    distance = ndi.distance_transform_edt(vegetation)
    peaks = (distance == ndi.maximum_filter(distance, size=45)) & (distance >= 6)
    seed_labels, count = ndi.label(peaks)
    markers = np.zeros(vegetation.shape, np.int32)
    markers[~vegetation] = 1
    markers[peaks] = seed_labels[peaks] + 1
    height = np.uint8(255 * (1 - distance / max(float(distance.max()), 1)))
    regions = ndi.watershed_ift(height, markers)
    regions[~vegetation] = 0
    proposals = []
    rejected_ground = 0
    sharpness = ndi.laplace(rgb.mean(axis=2)) ** 2
    for label in range(2, count + 2):
        # IFT may give disconnected islands the same label. Keep only the
        # largest connected island so a proposal cannot span unrelated leaves.
        islands, island_count = ndi.label(regions == label)
        island_sizes = np.bincount(islands.ravel())
        island_sizes[0] = 0
        if island_count == 0:
            continue
        ys, xs = np.where(islands == int(island_sizes.argmax()))
        if len(xs) < 300:
            continue
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        width, height = x1 - x0, y1 - y0
        if min(width, height) < 14 or max(width, height) < min_leaf_extent or max(width, height) > 300 or max(width / height, height / width) > 5:
            continue
        if float(sharpness[ys, xs].mean()) < .001:
            continue
        overlap = float(ground[ys, xs].mean())
        if overlap >= .5:
            rejected_ground += 1
            continue
        proposals.append(dict(region=label, bounds=[x0, y0, x1, y1], center=[int(xs.mean()), int(ys.mean())], pixels=len(xs), ground_overlap=round(overlap, 3)))
    # Square bounds with 10% margin on each side preserve leaf proportions.
    selected = []
    for p in sorted(proposals, key=lambda p: p['pixels'], reverse=True):
        x0, y0, x1, y1 = p['bounds']
        if crop_mode == 'tight':
            # Border-truncated regions cannot supply a complete leaf crop.
            if x0 == 0 or y0 == 0 or x1 == image.width or y1 == image.height:
                continue
            side = int(np.ceil(max(x1-x0, y1-y0) * 1.2))
            cx, cy = (x0+x1)//2, (y0+y1)//2
        else:
            side = 384
            cx, cy = p['center']
        x = max(0, min(cx - side//2, max(0, image.width - side)))
        y = max(0, min(cy - side//2, max(0, image.height - side)))
        box = [x, y, x + side, y + side]
        duplicate = False
        for q in selected:
            a, b, c, d = q['crop_bounds']
            intersection = max(0, min(c, box[2]) - max(a, x)) * max(0, min(d, box[3]) - max(b, y))
            union = side**2 + (c-a)*(d-b) - intersection
            if intersection / union > .8:
                duplicate = True
                break
        if not duplicate:
            selected.append(dict(p, crop_bounds=box))
    for candidate in selected:
        candidate["crop"] = image.crop(candidate["crop_bounds"]).resize(
            (384, 384), Image.Resampling.BILINEAR)
    return {"candidates": selected, "vegetation_mask": vegetation,
            "rejected_ground": rejected_ground}
