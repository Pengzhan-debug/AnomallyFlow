"""Small explicit connectivity probe; prints only sanitized codes and tool names."""
from qualityflow import config  # loads local .env
from qualityflow.model import AnthropicModel, ModelError

try:
    calls = AnthropicModel().complete([{"role": "user", "content":
        '合成联调：产品 VALVE-A，任务 probe，风险 HIGH。先通过只读工具查询批次和历史，不要提交报告。'}])
    print("Model connection passed; selected tools: " + ", ".join(call["name"] for call in calls))
except ModelError as exc:
    print("Model connection failed: " + exc.code)
    raise SystemExit(1)
