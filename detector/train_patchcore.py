"""Train a PatchCore baseline, export a torch checkpoint, calibrate thresholds.

Run inside the detector image/environment (anomalib 2.7, torch, torchvision):

    python -m detector.train_patchcore \
        --product-code VALVE-A \
        --normal-dir /data/valve-a/train/good \
        --abnormal-dir /data/valve-a/test/bad \
        --normal-test-dir /data/valve-a/test/good \
        --val-normal-dir /data/valve-a/validation/good \
        --val-abnormal-dir /data/valve-a/validation/bad \
        --model-version patchcore-wide-resnet50-2026-11-03 \
        --threshold-version valve-a-2026-11-03 \
        --output-dir detector/models \
        --profiles-path data/detector_profiles.json

For a quick demo on a public anomaly dataset (research/licence terms apply):

    python -m detector.train_patchcore --data-kind mvtec \
        --dataset-root /media/lenovo/sda7/pz/data/mvtec --category bottle \
        --product-code VALVE-A --calibrate-on val \
        --model-version patchcore-mvtec-bottle-1 --threshold-version valve-a-demo-1

The exported checkpoint is only as good as the samples: use工件/批次 independent
splits, never register the same file in train and validation, and treat the
calibrated thresholds as an evaluation result, not a guarantee.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--data-kind", default="folder", choices=["folder", "mvtec"],
                        help="folder: flat normal/abnormal dirs; mvtec: MVTecAD root/category layout")
    parser.add_argument("--normal-dir", help="normal images used to build the memory bank (folder kind)")
    parser.add_argument("--abnormal-dir", help="abnormal images for validation/test metrics (folder kind)")
    parser.add_argument("--normal-test-dir", help="normal test images (folder kind)")
    parser.add_argument("--dataset-root", help="dataset root containing category dirs (mvtec kind)")
    parser.add_argument("--category", help="category name under --dataset-root (mvtec kind)")
    parser.add_argument("--val-normal-dir", help="separate normal split used for threshold calibration")
    parser.add_argument("--val-abnormal-dir", help="separate abnormal split used for threshold calibration")
    parser.add_argument("--calibrate-on", default="folder", choices=["folder", "val"],
                        help="folder: use --val-* dirs; val: use the datamodule validation split")
    parser.add_argument("--normal-max", type=float, help="override: scores at or below this are NORMAL")
    parser.add_argument("--abnormal-min", type=float, help="override: scores at or above this are ABNORMAL")
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--threshold-version", required=True)
    parser.add_argument("--output-dir", default="detector/models")
    parser.add_argument("--registry-path", help="defaults to <output-dir>/registry.json")
    parser.add_argument("--profiles-path", default="data/detector_profiles.json")
    parser.add_argument("--backbone", default="wide_resnet50_2")
    parser.add_argument("--layers", default="layer2,layer3")
    parser.add_argument("--pretrained-source", default="timm", choices=["timm", "torchvision"],
                        help="timm downloads ImageNet weights from HuggingFace; torchvision uses "
                             "download.pytorch.org (useful when HF is unreachable)")
    parser.add_argument("--image-size", help="pre-processor size as H,W (default: model default 256)")
    parser.add_argument("--coreset-sampling-ratio", type=float, default=0.1)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--accelerator", default="auto", choices=["auto", "cpu", "gpu"])
    parser.add_argument("--max-normal-quantile", type=float, default=0.995)
    parser.add_argument("--min-abnormal-quantile", type=float, default=0.005)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--metadata-json", help="optional free-form metadata stored next to the checkpoint")
    args = parser.parse_args(argv)
    if args.data_kind == "folder" and not args.normal_dir:
        parser.error("--normal-dir is required with --data-kind folder")
    if args.data_kind == "mvtec" and not (args.dataset_root and args.category):
        parser.error("--dataset-root and --category are required with --data-kind mvtec")
    return args


TORCHVISION_BACKBONES = {
    "resnet18": ("resnet18", "ResNet18_Weights", "IMAGENET1K_V1"),
    "resnet34": ("resnet34", "ResNet34_Weights", "IMAGENET1K_V1"),
    "resnet50": ("resnet50", "ResNet50_Weights", "IMAGENET1K_V2"),
    "wide_resnet50_2": ("wide_resnet50_2", "Wide_ResNet50_2_Weights", "IMAGENET1K_V2"),
}


def _use_torchvision_backbone(model, backbone: str, layers: list[str]) -> None:
    """Swap the timm extractor for an equivalent torchvision ResNet.

    Anomalib's timm backbone downloads from HuggingFace; in offline/restricted
    environments torchvision's ImageNet weights are used instead. Feature
    normalization from ``Patchcore.configure_pre_processor`` is unchanged.
    """
    if backbone not in TORCHVISION_BACKBONES:
        raise SystemExit(f"--pretrained-source torchvision 不支持 {backbone}；可选 "
                         f"{', '.join(TORCHVISION_BACKBONES)}")
    from anomalib.models.components.feature_extractors import TimmFeatureExtractor
    import torchvision

    ctor, weights_name, weight_tag = TORCHVISION_BACKBONES[backbone]
    weights = getattr(getattr(torchvision.models, weights_name), weight_tag)
    network = getattr(torchvision.models, ctor)(weights=weights).eval()
    model.model.feature_extractor = TimmFeatureExtractor(
        backbone=network, layers=layers, pre_trained=False).eval()
    print(f"backbone: torchvision {backbone} ({weight_tag}), layers={layers}")


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _collect_scores(model, dataloaders) -> tuple[list[float], list[float]]:
    """Score the calibration set through the exported model's forward path.

    ``Engine.predict`` calls the inner model directly (``predict_step``), so it
    returns un-normalized scores. The torch checkpoint served by ``detector``
    wraps the model with its pre/post-processors, so calibrating on engine
    output produced a band that did not match serving. Calling ``model(...)``
    applies the same pre- and post-processing as the export.
    """
    import torch

    device = next(model.parameters()).device
    model.eval()
    normal, abnormal = [], []
    with torch.no_grad():
        for batch in dataloaders:
            predictions = model(batch.image.to(device))
            scores = predictions.pred_score.detach().float().cpu().reshape(-1).tolist()
            labels = (batch.gt_label.detach().cpu().reshape(-1).tolist()
                      if batch.gt_label is not None else [])
            if not labels:
                raise SystemExit("校准数据缺少标签，请分别提供正常与异常目录。")
            for score, label in zip(scores, labels):
                (abnormal if int(label) == 1 else normal).append(float(score))
    return normal, abnormal


def _build_datamodule(args: argparse.Namespace):
    from anomalib.data import Folder, MVTecAD

    if args.data_kind == "mvtec":
        return MVTecAD(
            root=args.dataset_root,
            category=args.category,
            train_batch_size=args.batch_size,
            eval_batch_size=args.batch_size,
            num_workers=args.num_workers,
            val_split_mode="from_test",
            val_split_ratio=0.5,
            seed=args.seed,
        )
    return Folder(
        name=args.product_code,
        normal_dir=args.normal_dir,
        abnormal_dir=args.abnormal_dir,
        normal_test_dir=args.normal_test_dir,
        train_batch_size=args.batch_size,
        eval_batch_size=args.batch_size,
        num_workers=args.num_workers,
        seed=args.seed,
    )


def _calibrate(model, args: argparse.Namespace, datamodule) -> tuple[float, float]:
    import numpy as np
    from anomalib.data import Folder

    if args.calibrate_on == "val":
        # Do not call setup() again: after fit it can re-split the test set with
        # a different RNG state, so the calibration images would not be the ones
        # whose scores the exported checkpoint was normalized on.
        dataloaders = datamodule.val_dataloader()
    else:
        if not (args.val_normal_dir and args.val_abnormal_dir):
            raise SystemExit("--calibrate-on folder 时必须给出 --val-normal-dir 与 --val-abnormal-dir。")
        calibration = Folder(
            name=f"{args.product_code}-calibration",
            normal_dir=args.val_normal_dir,
            abnormal_dir=args.val_abnormal_dir,
            train_batch_size=args.batch_size,
            eval_batch_size=args.batch_size,
            num_workers=args.num_workers,
            seed=args.seed,
        )
        calibration.setup()
        dataloaders = calibration.test_dataloader()
    normal, abnormal = _collect_scores(model, dataloaders)
    if not normal or not abnormal:
        raise SystemExit("校准数据中缺少正常或异常图像。")
    normal_max = float(np.quantile(normal, args.max_normal_quantile))
    abnormal_min = float(np.quantile(abnormal, args.min_abnormal_quantile))
    print(f"校准：正常 {len(normal)} 张，异常 {len(abnormal)} 张；"
          f"normal_max={normal_max:.6f}，abnormal_min={abnormal_min:.6f}")
    if not normal_max < abnormal_min:
        raise SystemExit("分数分布不可分，未写入阈值；请检查样本质量或改用其他模型/预处理。")
    return normal_max, abnormal_min


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.data_kind == "folder" and not Path(args.normal_dir).is_dir():
        raise SystemExit("--normal-dir 必须是包含正常图像的目录。")
    if args.data_kind == "mvtec" and not (Path(args.dataset_root) / args.category).is_dir():
        raise SystemExit(f"--dataset-root 下没有类别目录 {args.category}。")
    if any(ch in args.model_version for ch in ("/", "\\", "..")) or len(args.model_version) > 80:
        raise SystemExit("--model-version 只能用于文件名，不能包含路径分隔符或 '..'。")

    try:
        from anomalib.engine import Engine
        from anomalib.models import Patchcore
    except ImportError as exc:  # pragma: no cover - depends on runtime image
        raise SystemExit("未安装 anomalib/torch；请在 detector 镜像或同等环境中运行。") from exc

    layers = [item.strip() for item in args.layers.split(",") if item.strip()]
    pre_processor = True
    if args.image_size:
        try:
            height, width = (int(v) for v in args.image_size.split(","))
        except ValueError:
            raise SystemExit("--image-size 需为 H,W，例如 256,256") from None
        pre_processor = Patchcore.configure_pre_processor(image_size=(height, width))

    output_dir = Path(args.output_dir)
    registry_path = Path(args.registry_path) if args.registry_path else output_dir / "registry.json"
    run_dir = output_dir / args.product_code / "runs" / args.model_version
    model = Patchcore(backbone=args.backbone, layers=layers,
                      pre_trained=(args.pretrained_source == "timm"),
                      pre_processor=pre_processor,
                      coreset_sampling_ratio=args.coreset_sampling_ratio)
    if args.pretrained_source == "torchvision":
        _use_torchvision_backbone(model, args.backbone, layers)
    datamodule = _build_datamodule(args)
    engine = Engine(default_root_dir=str(run_dir), max_epochs=1, logger=False,
                    accelerator=args.accelerator, devices=1)
    engine.fit(model=model, datamodule=datamodule)

    export_root = output_dir / args.product_code / "export"
    if export_root.exists():
        shutil.rmtree(export_root)
    exported = engine.export(model=model, export_type="torch", export_root=str(export_root),
                             model_file_name="model")
    if exported is None or not Path(exported).is_file():
        raise SystemExit("导出失败：未生成 torch checkpoint。")
    checkpoint = output_dir / args.product_code / f"{args.model_version}.pt"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(exported, checkpoint)
    shutil.rmtree(export_root, ignore_errors=True)

    if args.normal_max is not None and args.abnormal_min is not None:
        normal_max, abnormal_min = args.normal_max, args.abnormal_min
        if not 0 <= normal_max < abnormal_min <= 1:
            raise SystemExit("阈值必须满足 0 <= normal_max < abnormal_min <= 1。")
    else:
        normal_max, abnormal_min = _calibrate(model, args, datamodule)

    digest = sha256(checkpoint.read_bytes()).hexdigest()
    registry = _load_json(registry_path) or {"schema_version": 1, "products": {}}
    registry.setdefault("products", {})[args.product_code] = {
        "checkpoint": f"{args.product_code}/{checkpoint.name}",
        "sha256": digest,
    }
    _write_json_atomic(registry_path, registry)

    profiles = _load_json(Path(args.profiles_path))
    profiles[args.product_code] = {
        "model_version": args.model_version,
        "threshold_version": args.threshold_version,
        "normal_max": round(normal_max, 6),
        "abnormal_min": round(abnormal_min, 6),
    }
    _write_json_atomic(Path(args.profiles_path), profiles)

    if args.metadata_json:
        metadata = _load_json(Path(args.metadata_json))
        metadata.update({"product_code": args.product_code, "model_version": args.model_version,
                         "threshold_version": args.threshold_version, "checkpoint_sha256": digest})
        _write_json_atomic(checkpoint.with_suffix(".meta.json"), metadata)

    print(f"checkpoint: {checkpoint}\nsha256: {digest}\nregistry: {registry_path}\nprofiles: {args.profiles_path}")
    print("提示：将 detector/models 与 profiles 文件挂载到服务后重启 detector，方可影响新任务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
