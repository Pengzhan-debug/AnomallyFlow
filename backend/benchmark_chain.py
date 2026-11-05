"""End-to-end load benchmark for the upload -> worker -> completed chain.

Uploads real images through the business API at increasing client concurrency
and waits for each task to reach a terminal state. Reports client-observed
latency percentiles and throughput. Use normal images so the investigation
agent is not involved; this measures queueing, detection via HTTP and DB
commits, not model quality.

    PYTHONPATH=backend python backend/benchmark_chain.py \
        --image-dir /data/mvtec/bottle/test/good --product-code VALVE-A \
        --levels 1,4,8,16 --requests-per-level 16

Requires a running API and at least one worker. The worker's ``--poll-interval``
(default 1s) is a large part of the observed end-to-end latency; run it with a
smaller interval to compare. Benchmark tasks are written to the demo database.
"""
from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import math
from pathlib import Path
import time
import uuid

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="inspector.demo")
    parser.add_argument("--product-code", default="VALVE-A")
    parser.add_argument("--image-dir", required=True)
    parser.add_argument("--levels", default="1,4,8,16")
    parser.add_argument("--requests-per-level", type=int, default=16)
    parser.add_argument("--poll-seconds", type=float, default=0.15)
    parser.add_argument("--task-timeout", type=float, default=170.0)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    return parser.parse_args(argv)


def _credentials(env_file: Path, username: str) -> str:
    key = "DEMO_" + username.split(".")[0].upper() + "_PASSWORD"
    values = {}
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    if key not in values:
        raise SystemExit(f"{env_file} 中没有 {key}")
    return values[key]


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _images(directory: Path) -> list[tuple[bytes, str]]:
    result = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            data = path.read_bytes()
            result.append((data, sha256(data).hexdigest()))
    if not result:
        raise SystemExit(f"{directory} 下没有可用图像。")
    return result


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    password = _credentials(args.env_file, args.username)
    images = _images(Path(args.image_dir))
    levels = [int(item) for item in args.levels.split(",") if item.strip()]

    async with httpx.AsyncClient(timeout=args.task_timeout + 30,
                                 limits=httpx.Limits(max_connections=max(levels) + 16),
                                 trust_env=False) as client:
        login = await client.post(f"{args.base_url}/api/v1/auth/login",
                                  json={"username": args.username, "password": password})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        batches = (await client.get(f"{args.base_url}/api/v1/batches", headers=headers)).json()["items"]
        batch = next((b for b in batches if b["product_code"] == args.product_code), None)
        if batch is None:
            raise SystemExit(f"没有产品 {args.product_code} 的批次。")
        print(f"批次 {batch['id']}，{len(images)} 张图，并发 {levels}")
        terminal = {"COMPLETED", "NEEDS_REVIEW", "WAITING_APPROVAL", "ORDER_CREATED"}

        async def one(level: int, index: int, semaphore: asyncio.Semaphore) -> dict:
            image, _digest = images[index % len(images)]
            async with semaphore:
                started = time.perf_counter()
                response = await client.post(
                    f"{args.base_url}/api/v1/inspections",
                    data={"batch_id": batch["id"], "product_code": args.product_code,
                          "remark": f"benchmark {level}/{index}"},
                    files={"image": ("inspection.png", image, "image/png")},
                    headers={**headers,
                             "Idempotency-Key": f"bench:{level}:{index}:{uuid.uuid4().hex}"})
                if response.status_code != 202:
                    return {"ok": False, "elapsed": time.perf_counter() - started,
                            "error": f"upload {response.status_code}: {response.text[:120]}"}
                task_id = response.json()["task_id"]
                deadline = time.perf_counter() + args.task_timeout
                while time.perf_counter() < deadline:
                    detail = (await client.get(f"{args.base_url}/api/v1/inspections/{task_id}",
                                               headers=headers)).json()
                    if detail["status"] in terminal:
                        return {"ok": detail["status"] == "COMPLETED",
                                "elapsed": time.perf_counter() - started,
                                "status": detail["status"],
                                "detector_mode": detail["detector_mode"]}
                    await asyncio.sleep(args.poll_seconds)
                return {"ok": False, "elapsed": time.perf_counter() - started, "error": "timeout"}

        for level in levels:
            semaphore = asyncio.Semaphore(level)
            wall_start = time.perf_counter()
            results = await asyncio.gather(*(one(level, index, semaphore)
                                             for index in range(args.requests_per_level)))
            wall = time.perf_counter() - wall_start
            latencies = [r["elapsed"] for r in results]
            errors = [r for r in results if not r["ok"]]
            statuses: dict[str, int] = {}
            for result in results:
                status = result.get("status", "ERROR")
                statuses[status] = statuses.get(status, 0) + 1
            print(f"并发 {level:>2}: {len(results) / wall:.2f} task/s | "
                  f"p50 {_percentile(latencies, 0.5):.2f}s "
                  f"p95 {_percentile(latencies, 0.95):.2f}s "
                  f"p99 {_percentile(latencies, 0.99):.2f}s | "
                  f"错误 {len(errors)} | {statuses}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
