"""Load benchmark for the detector ``/inspect`` endpoint.

Sends real images at increasing concurrency, each request with a unique
``Idempotency-Key`` so the response cache cannot hide queueing. Reports
client-observed latency (p50/p95/p99), server-reported inference time,
throughput and error rate. This measures the service, not the quality of the
model: pair it with ``evaluate_patchcore.py`` for accuracy.

    DETECTOR_DEVICE=cuda TRUST_REMOTE_CODE=1 ... uvicorn detector.app:app --port 8080
    python -m detector.benchmark_service --image-dir /data/mvtec/bottle/test/good \
        --product-code VALVE-A --levels 1,4,8,16 --requests-per-level 32

The bundled detector is single-process with an in-process model lock, so GPU
inference is serialized by design. The table is expected to show flat
throughput and rising tail latency once concurrency exceeds 1; that is the
bottleneck this script exists to quantify.
"""
from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import time
import uuid

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/inspect")
    parser.add_argument("--health-url", default="http://127.0.0.1:8080/healthz")
    parser.add_argument("--image-dir", required=True)
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--profiles-path", default="data/detector_profiles.json")
    parser.add_argument("--levels", default="1,4,8,16",
                        help="comma-separated concurrency levels")
    parser.add_argument("--requests-per-level", type=int, default=32)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--api-key", help="Bearer token if the service requires one")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--report-dir", default="detector/models/bench_reports")
    return parser.parse_args(argv)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _summary(values: list[float]) -> dict:
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 4),
        "p50": round(_percentile(values, 0.50), 4),
        "p95": round(_percentile(values, 0.95), 4),
        "p99": round(_percentile(values, 0.99), 4),
        "max": round(max(values), 4),
    }


def load_images(directory: Path) -> list[tuple[bytes, str]]:
    images = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            data = path.read_bytes()
            images.append((data, sha256(data).hexdigest()))
    if not images:
        raise SystemExit(f"{directory} 下没有可用图像。")
    return images


def load_profile(path: Path, product_code: str) -> dict:
    profiles = json.loads(path.read_text(encoding="utf-8"))
    if product_code not in profiles:
        raise SystemExit(f"{path} 中没有产品 {product_code} 的配置。")
    return profiles[product_code]


async def run_level(client, url, images, profile, product_code, level, count, headers) -> dict:
    import httpx

    semaphore = asyncio.Semaphore(level)

    async def one(index: int) -> dict:
        image, digest = images[index % len(images)]
        fields = {
            "request_id": f"BENCH-{level}-{index}",
            "tenant_id": "demo",
            "product_code": product_code,
            "image_sha256": digest,
            "model_version": profile["model_version"],
            "threshold_version": profile["threshold_version"],
        }
        request_headers = {**headers,
                           "Idempotency-Key": f"bench:{level}:{index}:{uuid.uuid4().hex}"}
        try:
            async with semaphore:
                # Start the clock after the client-side concurrency slot is free so
                # the number reflects service response time, not our own queue.
                started = time.perf_counter()
                response = await client.post(
                    url, data=fields,
                    files={"image": ("inspection.png", image, "image/png")},
                    headers=request_headers)
            elapsed = time.perf_counter() - started
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            return {"ok": response.status_code == 200, "elapsed": elapsed,
                    "status": response.status_code, "result": body.get("status"),
                    "inference_ms": body.get("inference_ms"), "error": None}
        except Exception as exc:  # noqa: BLE001 - benchmarking must survive failures
            return {"ok": False, "elapsed": time.perf_counter() - started,
                    "status": None, "result": None, "inference_ms": None, "error": str(exc)[:200]}

    wall_start = time.perf_counter()
    results = await asyncio.gather(*(one(i) for i in range(count)))
    wall = time.perf_counter() - wall_start

    latencies = [r["elapsed"] for r in results]
    inference = [r["inference_ms"] / 1000 for r in results if r["inference_ms"] is not None]
    errors = [r for r in results if not r["ok"]]
    return {
        "concurrency": level,
        "requests": count,
        "wall_seconds": round(wall, 3),
        "throughput_per_second": round(count / wall, 3),
        "error_rate": round(len(errors) / count, 4),
        "latency_seconds": _summary(latencies),
        "server_inference_seconds": _summary(inference) if inference else None,
        "result_counts": {key: sum(1 for r in results if r["result"] == key)
                          for key in sorted({r["result"] for r in results if r["result"]})},
        "sample_errors": sorted({str(r["error"]) for r in errors})[:3],
    }


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    profile = load_profile(Path(args.profiles_path), args.product_code)
    images = load_images(Path(args.image_dir))
    levels = [int(item) for item in args.levels.split(",") if item.strip()]
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}

    async with httpx.AsyncClient(timeout=args.timeout,
                                 limits=httpx.Limits(max_connections=max(levels) + 2),
                                 trust_env=False) as client:
        try:
            health = await client.get(args.health_url)
            health.raise_for_status()
            print(f"healthz: {health.json()}")
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"检测服务不可用（{args.health_url}）：{exc}") from None

        for _ in range(args.warmup):
            await run_level(client, args.url, images, profile, args.product_code, 1, 1, headers)
        print(f"预热完成（{args.warmup} 次），开始压测")

        reports = []
        for level in levels:
            report = await run_level(client, args.url, images, profile, args.product_code, level,
                                     args.requests_per_level, headers)
            reports.append(report)
            lat, inf = report["latency_seconds"], report["server_inference_seconds"] or {}
            print(f"并发 {level:>2}: {report['throughput_per_second']:.2f} req/s | "
                  f"p50 {lat['p50']:.3f}s p95 {lat['p95']:.3f}s p99 {lat['p99']:.3f}s | "
                  f"服务端推理 p50 {inf.get('p50', 0):.3f}s p95 {inf.get('p95', 0):.3f}s | "
                  f"错误率 {report['error_rate']:.1%}")

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"detector_bench_{stamp}.json"
    report_path.write_text(json.dumps({"url": args.url, "product_code": args.product_code,
                                       "profile": profile, "levels": reports},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
