"""Smoke check a running API without printing tokens or local demo passwords."""

from io import BytesIO
import os
import time

import httpx
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ.get("QUALITYFLOW_URL", "http://localhost:8000")


def image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, format="PNG")
    return output.getvalue()


def main() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=15) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        login = client.post("/api/v1/auth/login", json={
            "username": "inspector.demo",
            "password": os.environ["DEMO_INSPECTOR_PASSWORD"],
        })
        login.raise_for_status()
        auth = {"Authorization": "Bearer " + login.json()["access_token"]}
        batches = client.get("/api/v1/batches", headers=auth)
        batches.raise_for_status()
        assert len(batches.json()["items"]) >= 12
        results = []
        for suffix, batch, expected in [
            ("normal", "BA-2026-1001", "NORMAL"),
            ("abnormal", "BA-2026-1003", "ABNORMAL"),
        ]:
            response = client.post("/api/v1/inspections", headers={**auth,
                "Idempotency-Key": "smoke-" + suffix + "-v4"},
                data={"batch_id": batch, "product_code": "VALVE-A"},
                files={"image": ("sample.png", image_bytes(), "image/png")})
            response.raise_for_status()
            task_id = response.json()["task_id"]
            for _ in range(60):
                detail = client.get(f"/api/v1/inspections/{task_id}", headers=auth)
                detail.raise_for_status()
                if detail.json()["detection_status"] is not None:
                    break
                time.sleep(0.25)
            assert detail.json()["detection_status"] == expected, detail.json()
            assert detail.json()["execution"]["status"] == "SUCCEEDED"
            assert detail.json()["status"] == ("COMPLETED" if expected == "NORMAL" else "WAITING_APPROVAL")
            if expected == "ABNORMAL":
                report = client.get(f"/api/v1/inspections/{task_id}/report", headers=auth)
                report.raise_for_status()
                assert report.json()["risk_level"] == "HIGH"
                assert len(report.json()["tool_trace"]) == 4
                assert {item["source_type"] for item in report.json()["evidence_refs"]} == {"detection", "batch", "case", "sop"}
                trace = client.get(f"/api/v1/inspections/{task_id}/tools", headers=auth)
                trace.raise_for_status()
                assert len(trace.json()["items"]) == 4
                assert detail.json()["approval_id"]
            results.append((batch, task_id, detail.json()["status"]))
        print("API smoke passed: health, login, 12 batches, Mock detection, report, and approval entry.")
        for batch, task_id, status in results:
            print(f"{batch}: {task_id} -> {status}")


if __name__ == "__main__":
    main()
