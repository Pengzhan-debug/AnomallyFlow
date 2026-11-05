"""Tests for the held-out evaluation helpers; no torch/anomalib needed."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from detector.evaluate_patchcore import (
    auroc,
    auroc_ci,
    build_split,
    metrics_at_band,
    _quantile,
)


def make_args(**overrides) -> argparse.Namespace:
    values = dict(memory_frac=0.5, cal_frac=0.25, anomaly_cal_frac=0.34,
                  split_seed=7, normal_quantile=0.99, abnormal_quantile=0.01)
    values.update(overrides)
    return argparse.Namespace(**values)


def make_image(path: Path, value: int, size=(32, 32)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = np.full((*size, 3), value, dtype=np.uint8)
    Image.fromarray(pixels, mode="RGB").save(path)


def make_dataset(root: Path, category: str = "bottle", normals: int = 20,
                 anomalies=(5, 5)) -> Path:
    category_root = root / category
    for index in range(normals):
        make_image(category_root / "train" / "good" / f"{index:03d}.png", 40 + index)
    for index in range(4):
        make_image(category_root / "test" / "good" / f"{index:03d}.png", 200 + index)
    for group, count in zip(("scratch", "dent"), anomalies):
        for index in range(count):
            make_image(category_root / "test" / group / f"{index:03d}.png", 255)
    return root


def test_split_is_disjoint_and_keeps_defect_groups(tmp_path):
    root = make_dataset(tmp_path)
    args = make_args()
    split = build_split(root, "bottle", tmp_path / "work", args)

    counts = split["counts"]
    assert counts["memory"] == 12
    assert counts["cal_normals"] == 6
    assert counts["test_normals"] == 6
    assert counts["cal_anomalies"] + counts["test_anomalies"] == 10
    assert sum(v["test"] for v in split["defect_breakdown"].values()) == counts["test_anomalies"]

    resolved = {}
    for name, directory in split["directories"].items():
        resolved[name] = {p.resolve() for p in Path(directory).rglob("*.png")}
        assert len(resolved[name]) == counts[name]
    names = list(resolved)
    for index, first in enumerate(names):
        for second in names[index + 1:]:
            assert resolved[first].isdisjoint(resolved[second])

    test_types = {p.parent.name for p in Path(split["directories"]["test_anomalies"]).rglob("*.png")}
    assert test_types == {"scratch", "dent"}


def test_auroc_handles_separation_ties_and_single_class():
    assert auroc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert auroc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert auroc([0.5, 0.5, 0.5, 0.5], [0, 0, 1, 1]) == 0.5
    assert np.isnan(auroc([0.1, 0.2], [0, 0]))


def test_bootstrap_ci_brackets_point_estimate():
    rng = np.random.default_rng(0)
    normals = rng.normal(0.3, 0.05, 60).tolist()
    anomalies = rng.normal(0.7, 0.08, 40).tolist()
    scores = normals + anomalies
    labels = [0] * 60 + [1] * 40
    low, high = auroc_ci(scores, labels, n_resamples=400, seed=3)
    assert 0.0 <= low <= auroc(scores, labels) <= high <= 1.0


def test_band_metrics_count_uncertain_separately():
    result = metrics_at_band([0.2, 0.5, 0.8], [0.3, 0.6, 0.9], normal_max=0.4, abnormal_min=0.7)
    assert result["normal_decided_normal"] == 1
    assert result["normal_uncertain"] == 1
    assert result["normal_decided_abnormal"] == 1
    assert result["anomaly_decided_normal"] == 1
    assert result["anomaly_uncertain"] == 1
    assert result["anomaly_decided_abnormal"] == 1
    assert result["specificity"] == pytest.approx(1 / 3, abs=5e-4)
    assert result["recall"] == pytest.approx(1 / 3, abs=5e-4)


def test_quantile_matches_numpy():
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert _quantile(values, 0.5) == pytest.approx(np.quantile(values, 0.5))
    assert _quantile(values, 0.99) == pytest.approx(np.quantile(values, 0.99))
