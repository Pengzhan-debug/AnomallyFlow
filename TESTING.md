# 测试方法与当前验证范围

本轮记录：2026-11-02。测试使用合成图像、演示批次和演示 SOP/案例。通过结果验证当前功能行为，不能视为生产质量或真实模型准确率结论。

## 分层方法

| 层级 | 方法 | 检查内容 | 本轮结果 |
| --- | --- | --- | --- |
| 业务规则 | pytest / unittest，直接调用领域对象 | 高风险分阶段与不同账户审批、旧版本冲突、重复决定、复检补拍/返工/关闭、账户权限 | 纳入 74 项后端测试，全部通过 |
| 工具与检索 | 独立临时数据库，预设预期来源及干扰资料 | 已知问题召回 SOP/案例；排除跨租户、跨产品、过期、未生效、停用资料；空结果、参数错误、调用预算、篡改引用；只读工具不创建工单 | 纳入 74 项后端测试，全部通过 |
| API 集成 | FastAPI TestClient + 临时 SQLite + 开启外键约束 | 真实 HTTP 路由、multipart 图像校验、上传幂等、报告引用、工具调用记录、审批事务、工单复检、建号与首次改密 | 纳入 74 项后端测试，全部通过；另有 2 个子用例通过 |
| Worker 与入队恢复 | 并发领取、错误注入、真实进程强制终止 | API 重建后排队仍在、过期凭证不能写入、重试上限、部分写入回滚与恢复 | 既有 7 项通过；PostgreSQL 故障注入为前轮独立验证 |
| Agent 与 checkpoint | 脚本化模型响应＋真实数据库 checkpoint | 自主工具边界、非法写操作、Schema 与有限修正、引用、风险下限、预算、节点事务回滚、重建 Worker 后审批恢复 | 既有用例通过 |
| 补证与新版本 | multipart API、并发提交、材料篡改、脚本化模型、节点间重建 | 退回→v2/v3、旧报告/原图保留、独立证据和图、重试/并发去重、旧审批拦截 | 新增 7 项通过 |
| 检测协议与质量门 | httpx MockTransport + Worker/API 集成，独立于调查模式 | 四类结果、质量不合格、阈值/模型/图像不匹配、超时、异常 JSON、定位图校验、排队后配置固定与结果恢复 | 新增 25 项通过；没有实际视觉模型调用 |
| 视觉样本库 | 临时数据库、图像哈希、清单重复导出和角色/租户检查 | 人工标签与划分、跨集冲突、源图读取、文件错误、只读角色、跨租户隐藏、无批次时先准备样本类别 | 新增 3 项通过 |
| Anomalib 适配服务 | FastAPI TestClient + 假 Runner + 后端 `ServiceResult` 交叉校验 | 阈值带四态、质量门、字段集合、哈希/版本/模型缺失错误、幂等重放与冲突、错误可重试、定位图尺寸、Bearer 鉴权 | 新增 14 项通过；不含 torch/真实模型 |
| 数据库迁移与服务冒烟 | Alembic + Docker API / PostgreSQL + httpx 客户端 | 本地 SQLite 迁移；容器启动应用迁移；登录后上传新图，查到四种证据来源、四个工具日志及审批请求 | 通过 |
| 浏览器端到端 | Playwright 驱动本机 Edge，连接真实前端和容器 API | 本轮：补证→真实 DeepSeek v2→共同审批；样本导入、预览、清单下载、只读质检员、检测原图/模式标识、390 px 窄屏 | 通过，截图已人工查看；既有主链路与账户端到端为前轮验证 |
| 前端构建 | Vite production build | JSX/CSS 与模块依赖可构建 | 通过 |

后端最近一次完整回归输出为 `74 passed, 2 subtests passed`（164.24 秒）。原有 39 项全部保留，本轮增加 7 项补证、25 项检测适配器、3 项样本用例。普通 pytest 使用脚本化模型和模拟 HTTP 响应，不访问外部模型或视觉服务。SQLite 迁移与 Alembic check 通过，Docker 更新应用迁移后完成本轮浏览器检查。

## 独立 Worker 与恢复测试

本轮已经移除 API 的进程内 BackgroundTasks。API 测试先确认任务与队列一起落库，再显式调用 Worker 驱动函数检查输出；另有用例在 API 应用重新创建之后执行队列，确认排队状态不依赖 API 内存。

`backend/tests/test_worker.py` 测试两线程同时领取只产生一个执行凭证、超时后的旧凭证不能提交、执行期间凭证失效时回滚全部结果、三次错误或中断后转人工处理。真实进程用例会启动子进程，等它执行管线并 flush 报告/审批数据、但还没提交时强制终止进程，再检查数据库回滚，等待领取期限结束，由替代执行者完成任务。

同一故障注入也已在 Docker 的 PostgreSQL 上通过：提交前终止子进程 → 未提交结果回滚 → 领取期限结束后接手 → 仅一份报告和审批请求 → 两级人工批准创建一张工单 → 重放任务仍是一张工单。测试后独立 Worker 已恢复运行。

复现这个演示环境检查时，先暂停消费者，运行检查，再恢复（即使检查失败也应执行恢复）：

```powershell
docker compose stop worker
docker compose exec -T api python -m qualityflow.recovery_check
docker compose start worker
```

检查只允许 `DEMO_MODE=1`，会新增合成任务与审批工单，并终止它自己启动的测试子进程。默认正式 Worker 领取期限为 180 秒，测试子进程使用 3 秒；此故障脚本验证整段 Mock 事务回滚与重跑。Agent 节点 checkpoint 由独立用例验证，长任务 heartbeat 尚未实现。

## 怎样判断测试是否通过

自动化脚本会断言状态、数量、ID 和错误码，例如工程师批准高风险后 `work_order_id` 仍为空；主管批准后才出现工单，重复提交返回同一工单。检索用例明确断言预期文档 ID，并放入相似但无权或无效的资料验证过滤。失败路径必须返回明确错误或缺失证据，不得虚构正常结果。

API 测试每次创建独立数据库和临时文件目录，结束后清理。SQLite 开启外键，用于发现写入顺序问题；容器冒烟使用 PostgreSQL，补充验证真实数据库差异。浏览器脚本用真实角色账号操作，等待服务端正式状态和异步页面内容加载完成，再断言结果。临时密码不会输出到测试日志，截图前先隐藏密码。

## 入口与复现

在项目根目录运行后端测试：

```powershell
$env:PYTHONPATH='backend'
.venv\Scripts\python.exe -m pytest backend\tests -q
```

启动 Docker API 后运行服务冒烟：

```powershell
.venv\Scripts\python.exe backend\smoke.py
```

前端构建使用 `cd frontend` 后运行 `npm run build`。浏览器脚本为 `frontend/qa.cjs`，需要前端在 5173、API 在 8000 运行；当前环境通过 Playwright 运行时的 `NODE_PATH` 加载 Playwright，并使用已安装的 Edge。该脚本会在演示数据库新增合成任务、审批与一个测试账户，不应对生产库运行。

## 尚未验证的范围

- 真实视觉模型准确率、召回率、真实语义检索 Recall/MRR，以及真实 LLM 调查质量。
- Agent 在真实生产问题上的调查质量、长任务 heartbeat、Outbox 重放，以及更广泛的网络/数据库故障组合。
- 真实视觉模型推理、归一化与阈值校准、HTTP 服务的真实网络/硬件性能。HTTP 协议用模拟传输验证；环境仍是 Mock，不把接口通过视为自动异常检测已上线。
- 真实产品样本质量、工件级/批次级划分、近似图像泄漏和像素掩膜；目前无真实样本或视觉权重。
- 并发压力、长时稳定性、生产容量和 p95 延迟；工具记录里的耗时只是单次观测。
- 生产对象存储、生产部署、完整渗透测试和完整 PRD 发布验收。

检索仍是本地字符 n-gram TF-IDF 余弦相似度。mock 模式按固定计划调用工具，llm 模式由模型选择；最多 8 次只读调用、10 轮模型请求，报告格式最多修正两次，模型 HTTP 请求超时 30 秒。尚无独立工具进程或可中止的工具硬超时。

## 真实 DeepSeek 调查与审批恢复

已使用 DeepSeek Anthropic 兼容接口执行合成高风险任务：`QI-74e9e572dfde`。模型实际执行 6 次只读调用（至少三个不同工具），报告通过 Schema 与引用校验后进入待审批；随后重启 Docker Worker，完成工程师和主管审批，图从持久 checkpoint 恢复并完成。复核报告 ID、工具记录与工单 ID 均未重复。首次真实联调发现报告格式不符，已加入最多两次格式修正；严格校验仍然保留。

复现时需本地模型配置、运行中的 API/Worker 和 `AGENT_MODE=llm`；以下命令会真实调用模型服务并新增合成数据：

```powershell
$env:PYTHONPATH='backend'
.venv\Scripts\python.exe backend\agent_smoke.py --prepare-only
# 记下输出的 task_id，重启 Worker 后替换下一条命令的任务 ID
docker compose restart worker
.venv\Scripts\python.exe backend\agent_smoke.py --task-id <task_id>
```

单条合成任务成功不能代表真实模型准确率或生产验收通过。实现与适配依据见 [Agent 接入与恢复](design/Agent接入与恢复.md)。

历史查询现在返回独立的 inspection_history 证据 ID、来源版本与 HISTORICAL 标记；补充用例验证历史来源篡改和跨租户引用会被拒绝。来源身份与版本校验不等于自然语言事实蕴含校验，语义引用质量仍属于待评测范围。

## Anomalib 适配服务测试

`detector/tests/test_service.py` 用注入的假 Runner 验证服务协议，不需要安装 torch/anomalib：响应键与后端 `ServiceResult` 完全一致，状态由固定阈值带计算，空白/过小图像转 `UNCERTAIN`，哈希、版本、模型缺失和推理异常返回 `ERROR`，定位图缩放到原图尺寸，同一 `Idempotency-Key` 重放一致、换内容返回 409，配置密钥后要求 Bearer。另有一次独立检查把服务响应直接交给后端 `qualityflow.detector.ServiceResult` 解析并按同一阈值重算状态，结果一致。

本轮没有训练或加载真实模型，因此不构成本文档前面“尚未验证范围”中视觉准确率的证据；训练脚本与阈值校准需要在有样本和 GPU/CPU 资源的 detector 环境执行。

## 补证与样本浏览器检查

本轮合成任务 `QI-04fdac989dd1`：真实 DeepSeek 生成 v1 后退回，浏览器提交补证图像与说明，再生成真实 v2，由工程师与主管批准。脚本断言 v1 报告未变、原图与补证图可读、旧版只读、旧决定 409、同键补证重放、新版证据包含 supplement、队列完成和手机无页面横向溢出。本轮没有在 v2 入队时额外暂停 Worker；新版恢复由本地重建用例验证，前轮 v1 待审批重启检查仍保留。

`frontend/qa_samples.cjs` 在演示库登记两张明确标注“不用于模型训练”的 8×8 合成图，验证正常训练样本和异常验证样本、划分控件、原图、导出清单、质检员只读、Mock 检测元数据及 390 px 页面。截图为 `design/preview-sample-library.png`、`preview-sample-library-mobile.png`、`preview-detection-interface.png`；原有补证截图为 `preview-supplements-v1.png`、`preview-supplements-v2.png`。截图不包含密码或服务密钥。

启动本地 API/Worker 和前端后复现（补证命令会真实调用模型，普通样本脚本不调用模型）：

```powershell
$env:PYTHONPATH='backend'
.venv\Scripts\python.exe backend\agent_smoke.py --return-only
# 取得上一步的新 task_id；Playwright 需通过本机运行时 NODE_PATH 可解析
node frontend\qa_supplements.cjs <task_id>
node frontend\qa_samples.cjs
```

样本与视觉接口配置详见 [视觉检测接入](design/视觉检测接入.md)。

## MVTec bottle 真实 PatchCore 联调（演示）

2026-11-04 用 MVTec AD bottle（209 张正常训练图；测试 20 张正常 + 63 张异常）走通训练、导出、HTTP 服务与业务链路。产品码映射为 `VALVE-A`，仅作演示，不代表真实阀门样本。

- 训练：PatchCore + torchvision `wide_resnet50_2`（`layer2,layer3`），256 输入，coreset 0.1，RTX 3090；导出 `detector/models/VALVE-A/patchcore-mvtec-bottle-1.pt`（约 389 MB，registry 含 SHA256）。
- 推理分数：正常 `0.311–0.493`，异常 `0.551–1.000`，测试集 AUROC 1.0。演示阈值带固定为 `[0.50, 0.545]`（`--normal-max/--abnormal-min`），20/20 正常判 `NORMAL`，63/63 异常判 `ABNORMAL`。该阈值带取自同一测试集分数包络，属于演示设置，不能当作独立校准或准确率结论。
- 服务延迟：RTX 3090、900×900 输入、`DETECTOR_DEVICE=cuda`，首个请求含懒加载约 21 秒，预热后约 `263–362 ms`（含定位图编码）。
- 业务闭环：正常图 `QI-79d4736bb9a8` → `COMPLETED`；异常图 `QI-6272d677efc4` → `WAITING_APPROVAL`（报告 `RPT-2076545a2482`，证据含真实模型版本与分数）→ 工程师批准 → 工单 `WO-2ced2525f420`。两次任务均记录 `detector_mode=http`，异常定位图 900×900 与原图一致。
- 调查使用 `AGENT_MODE=mock`，未调用真实模型；本次结果只验证检测接入与业务状态，不包含调查质量结论。
- 小样本校准问题：`--calibrate-on val` 仅 10 张正常验证图，0.995 分位数会把其余正常图判为 `UNCERTAIN`；同时修复了 `engine.predict` 返回未归一化分数、以及校准前重复 `setup()` 可能重切验证集的问题（现与导出 checkpoint 的预处理/后处理路径一致）。

复现命令：

```bash
CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 python -m detector.train_patchcore \
  --data-kind mvtec --dataset-root /media/lenovo/sda7/pz/data/mvtec --category bottle \
  --product-code VALVE-A --calibrate-on val --normal-max 0.50 --abnormal-min 0.545 \
  --model-version patchcore-mvtec-bottle-1 --threshold-version valve-a-demo-1 \
  --output-dir detector/models --profiles-path data/detector_profiles.json \
  --batch-size 16 --num-workers 2 --accelerator gpu --pretrained-source torchvision
```

启动服务与 Worker 时设置 `DETECTOR_MODE=http`、`DETECTOR_URL=http://127.0.0.1:8080/inspect`、`DETECTOR_PROFILES_PATH=data/detector_profiles.json`，detector 侧设置 `TRUST_REMOTE_CODE=1`（pickle checkpoint）与 `DETECTOR_DEVICE=cuda`。

## 真实 LLM 调查 × 真实 PatchCore 联合联调（演示）

2026-11-04 首次把两条真实路径放在同一次业务闭环里：`AGENT_MODE=llm`（DeepSeek Anthropic 兼容接口，`deepseek-flash`）+ `DETECTOR_MODE=http`（MVTec bottle PatchCore，`VALVE-A`）。

- 正常图 `good/000.png` → `QI-4877c32747b0`：真实检测 `NORMAL`，分数 `0.3263`，直接 `COMPLETED`，未触发调查。首次请求含懒加载约 19.5 秒，定位图可读。
- 异常图 `broken_large/000.png` → `QI-dc27425581ad`：真实检测 `ABNORMAL`，分数 `0.9597`（预热后 313 ms），随后真实模型完成调查：3 次模型请求、6 次只读工具调用（`get_batch_info` 1 条、`get_inspection_history` 3 条、SOP/案例检索各 2 次均为 `EMPTY`），未触发格式修正。
- 报告 `RPT-eed61a451af2`：`analysis_mode=llm`、`model_version=deepseek-flash`；`observed_facts` 与 `hypotheses` 引用 `EV-QI-dc27425581ad-D`（真实检测证据，含模型版本与 0.9597 分数）与批次/历史证据；明确把图像文件名备注标为“待核验数据”，未当作缺陷结论；`evidence_sufficient=false`、风险 `MEDIUM`。
- 审批 `APR-40de677772bf`（一级工程师）批准后创建工单 `WO-f404b9cf1045`，任务 `ORDER_CREATED`；无重复报告、证据或工单。
- 局限：SOP/案例检索为空是演示种子库未覆盖该产品，不是检索故障；`evidence_sufficient=false` 表示缺少人工复核与实物复检，属预期行为。单次成功不构成调查质量或视觉准确率结论。

复现（会真实调用模型与 GPU 推理）：

```bash
DETECTOR_DEVICE=cuda TRUST_REMOTE_CODE=1 HF_HUB_OFFLINE=1 \
  DETECTOR_PROFILES_PATH=data/detector_profiles.json \
  python -m uvicorn detector.app:app --port 8080 &
PYTHONPATH=backend AGENT_MODE=llm DETECTOR_MODE=http \
  DETECTOR_URL=http://127.0.0.1:8080/inspect DETECTOR_PROFILES_PATH=data/detector_profiles.json \
  python -m uvicorn qualityflow.api:app --port 8000 &
PYTHONPATH=backend AGENT_MODE=llm DETECTOR_MODE=http \
  DETECTOR_URL=http://127.0.0.1:8080/inspect DETECTOR_PROFILES_PATH=data/detector_profiles.json \
  python -m qualityflow.worker &
```

## PatchCore 留出集评测（消除阈值泄漏）

`detector/evaluate_patchcore.py` 用独立的 memory / 校准 / 测试三段划分重新训练与评测：正常图按 65% / 17.5% / 其余拆成 memory bank、校准集、测试集，异常图按缺陷类型分层，32% 进校准、其余进测试。阈值带只由校准集决定（正常图 0.99 分位与异常图 0.01 分位）；测试集不参与训练、归一化统计或阈值选择。校准集不可分时脚本拒绝生成阈值带，但仍报告与阈值无关的 AUROC，不在测试集上调参。

2026-11-05 以 seed 42 在三类 MVTec 上执行：

| 类别 | memory | 校准（正常+异常） | 测试（正常+异常） | 阈值带 | 测试 AUROC [95% CI] | 阈值带下 specificity / recall / F1 |
| --- | --- | --- | --- | --- | --- | --- |
| bottle | 149 | 40 + 20 | 40 + 43 | 0.495 / 0.528 | 1.000 [1.000, 1.000] | 1.000 / 0.954 / 0.976 |
| metal_nut | 157 | 42 + 29 | 43 + 64 | 0.466 / 0.546 | 0.997 [0.990, 1.000] | 0.954 / 0.938 / 0.968 |
| cable | 183 | 49 + 29 | 50 + 63 | 无（校准集重叠） | 0.972 [0.942, 0.993] | —（拒绝生成阈值带） |

- bottle 的分类错误集中在 1 张 `contamination`（分数落在阈值带内判 `UNCERTAIN`）；`broken_large`、`broken_small` 在测试集上全部判对。
- cable 的校准正常图分数最高到 0.635，而部分校准异常图低至 0.509，两者重叠；脚本按设计不产生阈值带，也不使用测试集分数补偿。按缺陷类型看，`cable_swap`（AUROC 0.88）与 `poke_insulation`（0.90）是主要弱点。
- 这些是单次划分结果，不含像素级掩膜指标、多个随机种子的均值或真实产线数据；仍然不能当作生产准确率结论。但阈值选择已与测试集完全隔离，报告数字可直接引用为“留出测试集”结果。

复现（每类约 1–2 分钟，GPU）：

```bash
python -m detector.evaluate_patchcore --dataset-root /media/lenovo/sda7/pz/data/mvtec \
  --category bottle --product-code VALVE-A --model-version patchcore-bottle-heldout-1 \
  --pretrained-source torchvision --accelerator gpu
```

结果写入 `detector/models/eval_reports/<category>_<model-version>.json`（含原始分数、校准带、按缺陷类型拆分与 bootstrap 置信区间）。评测 checkpoint 写在 `detector/models/_eval/`，不会修改服务 registry 或 `detector_profiles.json`。评测辅助函数（AUROC、bootstrap、划分不重叠、阈值带统计）有 5 项无 torch 单元测试。

## 并发与尾延迟基线

2026-11-05 在单卡 RTX 3090 上测两组：`detector/benchmark_service.py` 直接压检测接口，`backend/benchmark_chain.py` 压“上传 → Worker → 完成”业务链。客户端计时在取得并发槽之后开始，不含客户端自身排队；每个请求使用唯一 `Idempotency-Key`，不会命中响应缓存。检测使用真实 PatchCore 与本地 900×900 PNG。

检测接口（每档 32 个请求）：

| 并发 | 吞吐 | p50 | p95 | p99 | 错误率 |
| --- | --- | --- | --- | --- | --- |
| 1 | 2.79 req/s | 0.342 s | 0.438 s | 0.468 s | 0% |
| 4 | 10.29 req/s | 0.362 s | 0.463 s | 0.524 s | 0% |
| 8 | 17.38 req/s | 0.380 s | 0.638 s | 0.684 s | 0% |
| 16 | 21.19 req/s | 0.656 s | 1.028 s | 1.095 s | 0% |

业务链（每档 16 个任务，正常图，`AGENT_MODE=mock` 不触发调查，客户端每 150 ms 轮询）：

| Worker 轮询 | 并发 | 吞吐 | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- |
| 默认 1 s | 1 | 0.47 task/s | 1.55 s | 4.94 s | 6.44 s |
| 默认 1 s | 4 | 1.13 task/s | 2.09 s | 5.40 s | 6.78 s |
| 默认 1 s | 8 | 1.14 task/s | 5.54 s | 9.63 s | 9.96 s |
| 默认 1 s | 16 | 1.60 task/s | 5.95 s | 9.61 s | 9.90 s |
| 0.1 s | 1 | 1.28 task/s | 0.63 s | 1.56 s | 2.24 s |
| 0.1 s | 4 | 1.90 task/s | 2.05 s | 2.21 s | 2.23 s |
| 0.1 s | 8 | 1.94 task/s | 3.92 s | 4.25 s | 4.32 s |
| 0.1 s | 16 | 1.94 task/s | 4.59 s | 7.86 s | 8.17 s |

结论与边界：

- 检测服务在 16 并发下仍无错误，吞吐约 21 req/s，p95 约 1 s；单进程内的模型锁没有把吞吐压死，但 p50 从 0.34 s 升到 0.66 s，说明开始排队。
- 业务链的瓶颈不在检测服务，而在单个 Worker 的领取节奏：默认 1 s 轮询时 1.1–1.6 task/s，降到 0.1 s 后约 1.9 task/s，c=1 的 p50 从 1.55 s 降到 0.63 s。继续扩容需要多 Worker 或多实例，而不是加 GPU 并发。
- 未覆盖：PostgreSQL、异常图像触发调查的路径、多 Worker、长时间稳定性与内存增长；客户端轮询会给 p50 带来不超过 150 ms 的附加延迟。压测会在演示库写入合成任务与图像，不应指向生产库。

复现：

```bash
# 检测接口（需要 detector 服务在 8080）
PYTHONPATH=. python -m detector.benchmark_service \
  --image-dir /media/lenovo/sda7/pz/data/mvtec/bottle/test/good \
  --product-code VALVE-A --levels 1,4,8,16 --requests-per-level 32

# 业务链（需要 API 在 8000、Worker 在运行）
PYTHONPATH=backend python backend/benchmark_chain.py \
  --image-dir /media/lenovo/sda7/pz/data/mvtec/bottle/test/good \
  --product-code VALVE-A --levels 1,4,8,16 --requests-per-level 16
```
