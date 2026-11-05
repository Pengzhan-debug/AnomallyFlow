"""Opt-in real-model check against running API/worker, using only a synthetic demo task."""
import os
import argparse
import time
from uuid import uuid4

import httpx
from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--return-only", action="store_true")
    args = parser.parse_args()
    with httpx.Client(base_url=os.environ.get("QUALITYFLOW_URL", "http://localhost:8000"), timeout=15) as client:
        def auth(role):
            response = client.post("/api/v1/auth/login", json={"username": role.lower() + ".demo",
                                   "password": os.environ[f"DEMO_{role}_PASSWORD"]})
            response.raise_for_status()
            return {"Authorization": "Bearer " + response.json()["access_token"]}
        from qualityflow.smoke_support import upload_demo_task
        task_id = args.task_id or upload_demo_task(client, "agent-live-" + uuid4().hex)
        engineer, manager = auth("ENGINEER"), auth("MANAGER")
        deadline = time.monotonic() + 180
        task = {}
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/inspections/{task_id}", headers=engineer)
            response.raise_for_status()
            task = response.json()
            assert task["execution"]["mode"] == "llm", "Enable AGENT_MODE=llm on the API"
            if task["status"] in {"WAITING_APPROVAL", "NEEDS_REVIEW"}:
                break
            time.sleep(1)
        assert task.get("status") == "WAITING_APPROVAL", "Agent stopped: " + str(task.get("error_code"))
        report = client.get(f"/api/v1/inspections/{task_id}/report", headers=engineer).json()
        assert report["analysis_mode"] == "llm"
        assert len({item["tool_name"] for item in report["tool_trace"]}) >= 3
        if args.prepare_only:
            print(f"Prepared Agent task: {task_id}; {len(report['tool_trace'])} read-only calls; awaiting human approval.")
            return
        approval_id = task["approval_id"]
        if args.return_only:
            response = client.post(f"/api/v1/approvals/{approval_id}/decision", headers=engineer,
                json={"decision": "RETURN_FOR_MORE_EVIDENCE", "reason": "补拍同位置图像并说明取样与光照变化",
                      "expected_version": 0, "expected_report_version": 1})
            response.raise_for_status()
            print(f"Returned Agent task: {task_id}; ready for supplementary material.")
            return
        for headers, version in [(engineer, 0), (manager, 1), (manager, 1)]:
            response = client.post(f"/api/v1/approvals/{approval_id}/decision", headers=headers,
                json={"decision": "APPROVE", "reason": "合成 Agent 联调审批", "expected_version": version,
                      "expected_report_version": 1})
            response.raise_for_status()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            detail = client.get(f"/api/v1/inspections/{task_id}", headers=engineer).json()
            if detail["execution"]["status"] == "SUCCEEDED":
                break
            time.sleep(0.5)
        assert detail["execution"]["status"] == "SUCCEEDED"
        assert detail["work_order_id"] == response.json()["work_order_id"]
        unchanged = client.get(f"/api/v1/inspections/{task_id}/report", headers=engineer).json()
        assert unchanged["report_id"] == report["report_id"] and unchanged["tool_trace"] == report["tool_trace"]
        print(f"Real Agent check passed: {task_id}; {len(report['tool_trace'])} read-only calls; report validated; co-sign and checkpoint resume completed.")


if __name__ == "__main__":
    main()
