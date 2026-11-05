# Agent 接入与恢复

当前支持 `AGENT_MODE=mock` 和 `AGENT_MODE=llm`。任务创建时将模式写入队列，修改配置只影响后续新任务。真实模式使用 DeepSeek 的 Anthropic Messages 兼容接口。视觉检测独立配置为 `DETECTOR_MODE=mock/http`；当前环境仍使用 Mock，HTTP 适配器已加入，真实模型尚未部署。

## 模型配置

本地 `.env` 使用 `LLM_PROVIDER=anthropic`、`LLM_BASE_URL=https://api.deepseek.com/anthropic`、`LLM_MODEL=deepseek-flash` 和 `LLM_API_KEY`。密钥只进入请求头，不进入报告、checkpoint、错误响应或源码。模型配置依据 [DeepSeek 官方兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)。

Agent 根据任务自主选择只读工具。服务端只允许批次、检测历史、案例与 SOP 四类查询；模型的 `submit_report` 只是提交草稿，不能批准报告或创建工单。单张模拟图像的报告必须列出缺失证据并标注证据不足；服务端执行 Schema、引用来源、租户/产品/文档有效性及风险下限校验。

历史检测通过独立 `inspection_history` 证据引用，并标记为 `HISTORICAL`；服务端核对来源模型版本及结果快照。该校验不等于对自然语言结论做语义蕴含验证，报告仍需工程师复核。

## 持久状态

图为 `agent → tools → agent → publish → approval`。每个模型/工具节点结束后，Worker 将 checkpoint、工具记录和队列进度一起提交；进入审批节点时使用动态 `interrupt()` 暂停。恢复使用相同服务端 thread ID，并重新读取数据库正式审批状态；不把客户端恢复值当作授权。实现遵循 [LangGraph 中断/恢复说明](https://docs.langchain.com/oss/python/langgraph/interrupts)。

同步、单分支图使用事务绑定的数据库 saver，存储在 PostgreSQL/SQLite 的 `graph_checkpoints` 与 `graph_writes` 中。状态序列化使用 LangGraph typed serializer；没有将任意 Python 对象 pickle 进数据库。checkpoint 仅供 Worker 内部使用，没有开放客户端读取或修改原始图状态的接口。

高风险工程师批准后仍等待主管；最终批准/驳回/退回会重新激活队列。正式工单仍由审批业务事务创建，图恢复只读取正式决定并结束，不重新调查或重复建工单。

## 退回后的新版本调查

退回后由原任务质检员提交补证图像和说明。API 先结束旧图的退回暂停点，再在同一业务事务中保存不可变材料、增加报告目标版本并重新入队。每一轮使用 `tenant_id:task_id:vN` 独立 thread，证据 ID 与工具日志也按版本区分。原始检测结果、原图、历史报告和审批不被覆盖；新报告从工程师阶段重新审批，高风险仍需另一账户的主管共同批准。

新图收到上版报告摘要、假设、缺失证据、退回意见及本轮材料作为背景。这些自然语言均按不可信输入处理，不能改变工具权限或审批规则。服务端要求新报告引用本轮 `supplement` 来源，核对材料哈希、任务和版本，风险不得低于上版。补证图像目前保存供人工查看；Agent 接收的是说明与元数据，不能声称已经用视觉模型复核补证像素。

提交需要 `Idempotency-Key` 和父报告/任务版本。同键同内容重试返回原材料，换内容返回冲突；新一轮入队后旧审批立即失效，即使新报告还没发布。重复退回可追加 v3 等版本。新一轮模型或证据核验失败会转人工处理，当前没有通用的失败后重试界面。

## 预算和边界

- 至少选择三个不同只读工具才能提交报告，最多八次只读调用、十轮模型请求。
- 报告 Schema 不符时向模型返回字段错误，最多修正两次；仍不合格则转人工处理。
- 累计执行预算 120 秒（不包含人工审批等待），单次模型 HTTP 请求超时 30 秒。
- 完成节点不因 Worker 重建而重复执行；当前尚未提交的外部模型调用在故障重试时可能再次发生。
- 当前实现只覆盖同步单分支图，不支持子图、并行分支或任意 checkpoint 编辑。
- 新版调查支持节点恢复；未提交的外部模型或检测请求仍可能重发，检测服务须遵守请求幂等键。
- 检索仍为本地字符向量，真实视觉模型、语义检索质量与真实场景调查正确率尚未验收。

## 验证入口

`backend/tests/test_agent.py` 使用可控模型响应验证节点间重建、数据库审批恢复、非法写工具、Schema、引用、风险下限、调用预算和节点事务回滚。该类测试验证流程与安全边界，不测真实模型正确率。

`backend/model_probe.py` 进行一次真实服务连接检查，仅输出状态与选择的工具名。`backend/agent_smoke.py` 在运行中的 API/Worker 上新增合成高风险任务，检查真实模型选择至少三个工具、报告通过校验、共同审批后图完成，且报告及工具记录未被重复生成。两者会调用外部模型；普通 pytest 回归不调用外部模型。

`backend/tests/test_supplements.py` 覆盖新版本隔离、幂等/并发、重复退回、材料篡改和 Worker 重建。`agent_smoke.py --return-only` 准备真实模型的退回任务；`frontend/qa_supplements.cjs <task_id>` 从浏览器完成补证到新版共同审批，需本地服务与演示账户。已通过的合成任务为 `QI-04fdac989dd1`，不代表真实视觉检测或调查准确率验收。
