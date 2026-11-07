"""One explainable 0-100 health score from the image term and the VOC term."""
from __future__ import annotations


def fuse(p_healthy: float, mq2_ratio: float | None, gas_ratio: float | None, cfg) -> dict:
    deltas = [r - 1.0 for r in (mq2_ratio, gas_ratio) if r is not None]
    voc_stress = min(1.0, max(0.0, max(deltas) / cfg.voc_full_scale)) if deltas else None
    if voc_stress is None:
        # No VOC data: score on the image alone rather than pretending the air is clean.
        health = 100 * p_healthy
    else:
        health = 100 * (cfg.w_img * p_healthy + cfg.w_voc * (1 - voc_stress))
    status = "healthy" if health >= cfg.healthy_min else "watch" if health >= cfg.watch_min else "unhealthy"
    if voc_stress is None:
        smell = "no VOC data"
    elif voc_stress < 0.2:
        smell = "normal VOCs"
    elif voc_stress < 0.6:
        smell = "slightly elevated VOCs"
    else:
        smell = "elevated VOCs"
    return {
        "health": round(health, 1),
        "status": status,
        "voc_stress": None if voc_stress is None else round(voc_stress, 3),
        "explain": f"Looks: {p_healthy * 100:.0f}% healthy · Smell: {smell}",
    }
