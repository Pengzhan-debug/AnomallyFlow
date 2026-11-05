"""Held-out evaluation for the PatchCore detector (MVTec AD layout).

Unlike ``train_patchcore.py --calibrate-on val`` this script enforces a clean
protocol: normal images are split into memory bank / calibration / test sets,
and anomaly images into calibration / test sets. The threshold band is chosen
on the calibration split only; every reported number is computed on images
that were never seen by fit, export post-processing or threshold selection.

    python -m detector.evaluate_patchcore \
        --dataset-root /data/mvtec --category bottle \
        --product-code VALVE-A --model-version patchcore-bottle-heldout-1

The checkpoint is written under ``<output-dir>/_eval/<category>/`` so the
service registry and ``detector_profiles.json`` are never touched. This is an
evaluation tool, not a deployment step; no weights are registered.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import shutil
import sys
from typing import Iterable

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--output-dir", default="detector/models")
    parser.add_argument("--report-dir", help="defaults to <output-dir>/eval_reports")
    parser.add_argument("--split-seed", type=int, default=42)
    parser.add_argument("--memory-frac", type=float, default=0.65)
    parser.add_argument("--cal-frac", type=float, default=0.175)
    parser.add_argument("--anomaly-cal-frac", type=float, default=0.32)
    parser.add_argument("--normal-quantile", type=float, default=0.99)
    parser.add_argument("--abnormal-quantile", type=float, default=0.01)
    parser.add_argument("--backbone", default="wide_resnet50_2")
    parser.add_argument("--layers", default="layer2,layer3")
    parser.add_argument("--pretrained-source", default="timm", choices=["timm", "torchvision"])
    parser.add_argument("--image-size", help="H,W (default: model default 256)")
    parser.add_argument("--coreset-sampling-ratio", type=float, default=0.1)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--accelerator", default="auto", choices=["auto", "cpu", "gpu"])
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    args = parser.parse_args(argv)
    if not 0 < args.memory_frac < 1 or not 0 <= args.cal_frac < 1:
        parser.error("--memory-frac/--cal-frac 取值不合理")
    if args.memory_frac + args.cal_frac >= 1:
        parser.error("--memory-frac + --cal-frac 必须小于 1（其余留作测试）")
    return args


def _images(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(src.resolve(), dst)


def build_split(dataset_root: Path, category: str, workdir: Path, args: argparse.Namespace) -> dict:
    """Create disjoint memory/calibration/test directories with symlinks."""
    category_dir = dataset_root / category
    train_good = _images(category_dir / "train" / "good")
    test_good = _images(category_dir / "test" / "good")
    anomaly_dirs = sorted(p for p in (category_dir / "test").iterdir()
                          if p.is_dir() and p.name != "good")
    anomalies: dict[str, list[Path]] = {p.name: _images(p) for p in anomaly_dirs}
    if not train_good or not test_good or not anomalies:
        raise SystemExit(f"{category_dir} 缺少 train/good、test/good 或 test/<defect> 目录。")

    rng = random.Random(args.split_seed)
    normals = train_good + test_good
    rng.shuffle(normals)
    memory_n = max(1, round(len(normals) * args.memory_frac))
    cal_n = max(2, round(len(normals) * args.cal_frac))
    if memory_n + cal_n >= len(normals):
        raise SystemExit("正常图太少，无法同时留出校准集与测试集。")
    memory, calibration = normals[:memory_n], normals[memory_n:memory_n + cal_n]
    test_normals = normals[memory_n + cal_n:]

    cal_by_type: dict[str, list[Path]] = {}
    test_by_type: dict[str, list[Path]] = {}
    per_type = {}
    for defect, files in anomalies.items():
        shuffled = list(files)
        rng.shuffle(shuffled)
        n_cal = max(1, round(len(shuffled) * args.anomaly_cal_frac)) if len(shuffled) > 1 else 0
        cal_by_type[defect] = shuffled[:n_cal]
        test_by_type[defect] = shuffled[n_cal:]
        per_type[defect] = {"calibration": len(shuffled[:n_cal]), "test": len(shuffled[n_cal:])}

    cal_anomalies = [p for files in cal_by_type.values() for p in files]
    test_anomalies = [p for files in test_by_type.values() for p in files]
    if not test_anomalies or len(test_normals) < 2:
        raise SystemExit("留出测试集为空。")

    if workdir.exists():
        shutil.rmtree(workdir)
    flat_groups = {
        "memory": memory,
        "cal_normals": calibration,
        "test_normals": test_normals,
    }
    for group, files in flat_groups.items():
        for index, src in enumerate(files):
            _link(src, workdir / group / f"{index:05d}_{src.name}")
    # Keep the defect type in the path so per-type metrics stay separable.
    for group, by_type in (("cal_anomalies", cal_by_type), ("test_anomalies", test_by_type)):
        for defect, files in by_type.items():
            for index, src in enumerate(files):
                _link(src, workdir / group / defect / f"{index:05d}_{src.name}")

    groups = {**flat_groups, "cal_anomalies": cal_anomalies, "test_anomalies": test_anomalies}
    seen = {name: {p.resolve() for p in files} for name, files in groups.items()}
    names = list(seen)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if seen[a] & seen[b]:
                raise SystemExit(f"数据划分重叠：{a} 与 {b}")

    return {
        "counts": {name: len(files) for name, files in groups.items()},
        "defect_breakdown": per_type,
        "directories": {name: str(workdir / name) for name in groups},
    }


def _to_tensor(path: Path):
    import numpy as np
    import torch
    from PIL import Image

    with Image.open(path) as opened:
        array = np.asarray(opened.convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(array).permute(2, 0, 1)


def score_files(model, files: Iterable[Path], device, batch_size: int) -> list[float]:
    import torch

    files = list(files)
    scores: list[float] = []
    with torch.no_grad():
        for start in range(0, len(files), batch_size):
            batch = torch.stack([_to_tensor(p) for p in files[start:start + batch_size]]).to(device)
            predictions = model(batch)
            scores += [float(s) for s in predictions.pred_score.detach().float().cpu().reshape(-1)]
    return scores


def auroc(scores: list[float], labels: list[int]) -> float:
    """Rank-based AUROC with tie-aware average ranks (no sklearn dependency)."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    rank_sum = sum(ranks[i] for i, label in enumerate(labels) if label == 1)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def auroc_ci(scores: list[float], labels: list[int], n_resamples: int = 2000,
             seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap 95% CI for AUROC over test images."""
    rng = random.Random(seed)
    values = []
    indices = list(range(len(scores)))
    for _ in range(n_resamples):
        sample = [rng.choice(indices) for _ in indices]
        value = auroc([scores[i] for i in sample], [labels[i] for i in sample])
        if not math.isnan(value):
            values.append(value)
    values.sort()
    if not values:
        return (float("nan"), float("nan"))
    return (_quantile(values, 0.025), _quantile(values, 0.975))


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def metrics_at_band(normal_scores: list[float], anomaly_scores: list[float],
                    normal_max: float, abnormal_min: float) -> dict:
    tn = sum(1 for s in normal_scores if s <= normal_max)
    fp = sum(1 for s in normal_scores if s >= abnormal_min)
    normal_uncertain = len(normal_scores) - tn - fp
    tp = sum(1 for s in anomaly_scores if s >= abnormal_min)
    fn = sum(1 for s in anomaly_scores if s <= normal_max)
    anomaly_uncertain = len(anomaly_scores) - tp - fn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / len(anomaly_scores) if anomaly_scores else 0.0
    specificity = tn / len(normal_scores) if normal_scores else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "normals": len(normal_scores), "anomalies": len(anomaly_scores),
        "normal_decided_normal": tn, "normal_uncertain": normal_uncertain,
        "normal_decided_abnormal": fp,
        "anomaly_decided_abnormal": tp, "anomaly_uncertain": anomaly_uncertain,
        "anomaly_decided_normal": fn,
        "specificity": round(specificity, 4), "false_positive_rate": round(1 - specificity, 4),
        "recall": round(recall, 4), "precision": round(precision, 4), "f1": round(f1, 4),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dataset_root = Path(args.dataset_root)
    if not (dataset_root / args.category).is_dir():
        raise SystemExit(f"--dataset-root 下没有类别目录 {args.category}。")

    workdir = Path(args.output_dir) / "_eval" / args.category / "split"
    split = build_split(dataset_root, args.category, workdir, args)
    print(f"划分（{args.category}）：{split['counts']}")
    print(f"缺陷类型拆分：{split['defect_breakdown']}")

    try:
        from anomalib.data import Folder
        from anomalib.engine import Engine
        from anomalib.models import Patchcore
        import torch
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("未安装 anomalib/torch；请在 detector 环境运行。") from exc

    layers = [item.strip() for item in args.layers.split(",") if item.strip()]
    pre_processor = True
    if args.image_size:
        height, width = (int(v) for v in args.image_size.split(","))
        pre_processor = Patchcore.configure_pre_processor(image_size=(height, width))

    model = Patchcore(backbone=args.backbone, layers=layers,
                      pre_trained=(args.pretrained_source == "timm"),
                      pre_processor=pre_processor,
                      coreset_sampling_ratio=args.coreset_sampling_ratio)
    if args.pretrained_source == "torchvision":
        from .train_patchcore import _use_torchvision_backbone
        _use_torchvision_backbone(model, args.backbone, layers)

    directories = split["directories"]
    datamodule = Folder(
        name=f"{args.category}-heldout",
        normal_dir=directories["memory"],
        abnormal_dir=directories["cal_anomalies"],
        normal_test_dir=directories["cal_normals"],
        val_split_mode="same_as_test",
        train_batch_size=args.batch_size,
        eval_batch_size=args.batch_size,
        num_workers=0,
        seed=args.split_seed,
    )

    run_dir = Path(args.output_dir) / "_eval" / args.category / "runs"
    export_root = Path(args.output_dir) / "_eval" / args.category / "export"
    checkpoint = Path(args.output_dir) / "_eval" / args.category / f"{args.model_version}.pt"
    for path in (run_dir, export_root):
        shutil.rmtree(path, ignore_errors=True)
    engine = Engine(default_root_dir=str(run_dir), max_epochs=1, logger=False,
                    accelerator=args.accelerator, devices=1)
    engine.fit(model=model, datamodule=datamodule)
    exported = engine.export(model=model, export_type="torch", export_root=str(export_root),
                             model_file_name="model")
    if exported is None or not Path(exported).is_file():
        raise SystemExit("导出失败：未生成 torch checkpoint。")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(exported, checkpoint)
    shutil.rmtree(export_root, ignore_errors=True)
    shutil.rmtree(run_dir, ignore_errors=True)  # Lightning intermediates are not used.

    if args.device == "cpu":
        device = torch.device("cpu")
    elif args.device == "cuda" and torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    served = torch.load(checkpoint, map_location="cpu", weights_only=False)["model"]
    served.eval().to(device)

    cal_normal_scores = score_files(served, _images(Path(directories["cal_normals"])),
                                    device, args.batch_size)
    cal_anomaly_scores = score_files(served, _images(Path(directories["cal_anomalies"])),
                                     device, args.batch_size)
    test_normal_scores = score_files(served, _images(Path(directories["test_normals"])),
                                     device, args.batch_size)
    test_anomaly_scores = score_files(served, _images(Path(directories["test_anomalies"])),
                                      device, args.batch_size)

    normal_max = _quantile(cal_normal_scores, args.normal_quantile)
    abnormal_min = _quantile(cal_anomaly_scores, args.abnormal_quantile)
    band_valid = normal_max < abnormal_min
    if not band_valid:
        print(f"警告：校准集分数不可分（normal_max={normal_max:.6f}, "
              f"abnormal_min={abnormal_min:.6f}）；不产生阈值带，也不在测试集上调参。"
              "以下只报告与阈值无关的 AUROC。")

    labels = [0] * len(test_normal_scores) + [1] * len(test_anomaly_scores)
    scores = test_normal_scores + test_anomaly_scores
    ci_low, ci_high = auroc_ci(scores, labels)

    per_defect = {}
    anomaly_root = Path(directories["test_anomalies"])
    for defect_dir in sorted(p for p in anomaly_root.iterdir() if p.is_dir()):
        defect_scores = score_files(served, _images(defect_dir), device, args.batch_size)
        defect_labels = [0] * len(test_normal_scores) + [1] * len(defect_scores)
        entry = {
            "count": len(defect_scores),
            "score_min": round(min(defect_scores), 6),
            "score_max": round(max(defect_scores), 6),
            "auroc_vs_test_normals": round(auroc(test_normal_scores + defect_scores, defect_labels), 4),
        }
        if band_valid:
            entry["recall_at_band"] = round(
                sum(1 for s in defect_scores if s >= abnormal_min) / len(defect_scores), 4)
            entry["uncertain_at_band"] = sum(1 for s in defect_scores if normal_max < s < abnormal_min)
        per_defect[defect_dir.name] = entry

    report = {
        "category": args.category, "product_code": args.product_code,
        "model_version": args.model_version, "split_seed": args.split_seed,
        "split": split,
        "calibration": {
            "normal_scores": {"min": round(min(cal_normal_scores), 6),
                              "p50": round(_quantile(cal_normal_scores, 0.5), 6),
                              "max": round(max(cal_normal_scores), 6)},
            "anomaly_scores": {"min": round(min(cal_anomaly_scores), 6),
                               "p50": round(_quantile(cal_anomaly_scores, 0.5), 6),
                               "max": round(max(cal_anomaly_scores), 6)},
            "quantiles": {"normal": args.normal_quantile, "abnormal": args.abnormal_quantile},
            "band_valid": band_valid,
            "normal_max": round(normal_max, 6), "abnormal_min": round(abnormal_min, 6),
        },
        "test": {
            "normal_scores": {"min": round(min(test_normal_scores), 6),
                              "p50": round(_quantile(test_normal_scores, 0.5), 6),
                              "max": round(max(test_normal_scores), 6)},
            "anomaly_scores": {"min": round(min(test_anomaly_scores), 6),
                               "p50": round(_quantile(test_anomaly_scores, 0.5), 6),
                               "max": round(max(test_anomaly_scores), 6)},
            "auroc": round(auroc(scores, labels), 4),
            "auroc_95ci": [round(ci_low, 4), round(ci_high, 4)],
            "at_calibrated_band": (metrics_at_band(test_normal_scores, test_anomaly_scores,
                                                   normal_max, abnormal_min)
                                   if band_valid else None),
            "per_defect": per_defect,
            "raw_scores": {
                "test_normals": [round(s, 6) for s in test_normal_scores],
                "test_anomalies": [round(s, 6) for s in test_anomaly_scores],
            },
        },
    }

    report_dir = Path(args.report_dir) if args.report_dir else Path(args.output_dir) / "eval_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{args.category}_{args.model_version}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(workdir, ignore_errors=True)

    cal, test = report["calibration"], report["test"]
    band = test["at_calibrated_band"]
    print(f"\n校准集 {cal['normal_scores']['min']}–{cal['normal_scores']['max']} (normal) / "
          f"{cal['anomaly_scores']['min']}–{cal['anomaly_scores']['max']} (anomaly)")
    if cal["band_valid"]:
        print(f"阈值带 [{cal['normal_max']}, {cal['abnormal_min']}]")
    else:
        print("阈值带：无（校准集不可分）")
    print(f"留出测试集：AUROC {test['auroc']} (95% CI {test['auroc_95ci'][0]}–{test['auroc_95ci'][1]}), "
          f"正常 {len(test['raw_scores']['test_normals'])} 张 / "
          f"异常 {len(test['raw_scores']['test_anomalies'])} 张")
    if band is not None:
        print(f"  正常判 NORMAL {band['normal_decided_normal']} / UNCERTAIN {band['normal_uncertain']} / "
              f"ABNORMAL {band['normal_decided_abnormal']}")
        print(f"  异常判 ABNORMAL {band['anomaly_decided_abnormal']} / UNCERTAIN {band['anomaly_uncertain']} / "
              f"NORMAL {band['anomaly_decided_normal']}")
        print(f"  specificity {band['specificity']}, recall {band['recall']}, "
              f"precision {band['precision']}, F1 {band['f1']}")
    for defect, values in test["per_defect"].items():
        extra = (f" recall {values['recall_at_band']} uncertain {values['uncertain_at_band']}"
                 if "recall_at_band" in values else "")
        print(f"  {defect}: n={values['count']}{extra} AUROC {values['auroc_vs_test_normals']}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
