"""Run an isolated DSH review over a server-selected inspection snapshot."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock

from .dsh_console import DshConsole, DshConsoleError, isolated_environment


TOOL = "qualityflow_inspection_context"
OVERLAY = Path(__file__).resolve().parents[2] / "integrations/dsh/inspection/cordis.patch.yml"
PROMPT = ("调用 qualityflow_inspection_context 一次。用中文给出简短巡检简报，"
          "包括本次检测、批次和相关历史，再列出证据缺口。历史为空时说明暂无记录。")
MAX_TRANSCRIPT = 512_000


def parse_brief(transcript: bytes) -> dict:
    if len(transcript) > MAX_TRANSCRIPT:
        raise DshConsoleError(502, "DSH_BRIEF_TOO_LARGE", "DSH 简报输出超出限制。")
    call_id = None
    called = False
    final = {}
    try:
        for line in transcript.splitlines():
            event = json.loads(line)
            if event["type"] == "tool_call":
                if event.get("tool") != TOOL or call_id is not None:
                    raise ValueError("Unexpected tool call")
                call_id = event["callId"]
            elif event["type"] == "tool_result":
                called = bool(call_id and event.get("callId") == call_id and event.get("status") == "completed")
            elif event["type"] == "final":
                final = event
        answer = final.get("text", "").strip()
        if called and (final.get("reason") or {}).get("kind") == "completed" and 0 < len(answer) <= 6000:
            return {"brief": answer, "tool": TOOL, "engine": "deepseek_harness"}
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    raise DshConsoleError(502, "DSH_BRIEF_UNVERIFIED", "DSH 未返回已完成且包含有效取证的简报。")


class DshInspection:
    def __init__(self, console: DshConsole):
        self.console = console
        self.running = Lock()

    def run(self, snapshot: dict) -> dict:
        node = shutil.which("node")
        if not node or not self.console.cli.is_file() or not OVERLAY.is_file():
            raise DshConsoleError(503, "DSH_BRIEF_RUNTIME_MISSING", "请先配置本地 DSH CLI 和巡检插件。")
        key = os.environ.get("DSH_INSPECTION_API_KEY", "").strip()
        if not key and os.environ.get("LLM_BASE_URL", "").rstrip("/") == "https://api.deepseek.com/anthropic":
            key = os.environ.get("LLM_API_KEY", "").strip()
        if not key:
            raise DshConsoleError(503, "DSH_BRIEF_KEY_MISSING", "请先在服务端配置 DSH_INSPECTION_API_KEY。")
        if not self.running.acquire(blocking=False):
            raise DshConsoleError(429, "DSH_BRIEF_BUSY", "已有 DSH 简报正在生成，请稍后重试。")
        try:
            root = self.console.storage_root / "dsh-inspection"
            root.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="run-", dir=root) as directory:
                home = Path(directory)
                env = isolated_environment(home, self.console.path)
                env.update(
                    DEEPSEEK_API_KEY=key,
                    DSH_INSPECTION_MODEL=os.environ.get("DSH_INSPECTION_MODEL", "deepseek-v4-flash"),
                    QUALITYFLOW_DSH_CONTEXT=json.dumps(snapshot, ensure_ascii=True),
                    QUALITYFLOW_DSH_CLI=str(self.console.cli),
                )
                with (home / "output.jsonl").open("w+b") as output:
                    subprocess.run(
                        [node, str(self.console.cli), "--profile", "headless", "--patch", str(OVERLAY),
                         "--permission-mode", "read-only", "--approval-policy", "never", "--json", "-"],
                        cwd=home, env=env, input=PROMPT.encode(), stdout=output, stderr=subprocess.DEVNULL,
                        timeout=60, check=True,
                    )
                    output.seek(0)
                    return parse_brief(output.read(MAX_TRANSCRIPT + 1))
        except subprocess.TimeoutExpired:
            raise DshConsoleError(504, "DSH_BRIEF_TIMEOUT", "DSH 简报生成超时，请稍后重试。") from None
        except (OSError, subprocess.CalledProcessError):
            raise DshConsoleError(502, "DSH_BRIEF_FAILED", "DSH 执行失败，请检查运行环境和模型配置。") from None
        finally:
            self.running.release()
