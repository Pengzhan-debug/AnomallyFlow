# QualityFlow 的 DSH 控制台

登录 React 工作台后，在左侧打开 **DSH 控制台**。调查会话按现有任务权限展示巡检任务、报告版本和只读工具轨迹。巡检调查仍由 QualityFlow 的 LangGraph 流程执行。管理员可以从同一入口管理独立的 DSH 配置。

## 本地安装环境

需要 Node.js 24.9.0 或更高版本。Windows 本地验证使用 DSH `0.2.2-rc.1` 和 pnpm `11.25.0`。从仓库根目录运行以下命令，再按项目说明启动 API 和 React 工作台。

```powershell
npm install --prefix storage/dsh-runtime --no-audit --no-fund --ignore-scripts --save-exact '@deepseek-ai/dsh@0.2.2-rc.1' 'pnpm@11.25.0'
```

API 默认从 `STORAGE_ROOT/dsh-runtime` 读取 CLI 和 pnpm。若 CLI 装在别处，可通过服务端的 `DSH_CONSOLE_CLI` 指定官方包的 `lib/bin.js` 绝对路径，并让 API 进程在 `PATH` 上找到 Node.js 和 pnpm。默认 Docker 镜像需要另行配置 Node.js、pnpm、CLI 和项目插件目录，才可在容器内安装。

## 安装与卸载

1. 用管理员账号打开 **DSH 控制台 → 插件管理**。
2. 在 **QualityFlow 健康检查** 卡片上点击 **安装插件**，然后确认。
3. 出现 **已安装** 表示包文件已落盘并写入租户的 `qualityflow` 配置。配置在 DSH 下次启动时读取。页面将加载状态显示为 **待启动或重启验证**。
4. 点击 **卸载插件** 可从配置中移除包。API 记录安装和卸载结果，便于审计。

当前目录提供项目内置的 `@qualityflow/dsh-connector`。它注册 `qualityflow_health`，通过固定的本机 `GET http://127.0.0.1:8000/healthz` 检查 API，无需模型密钥。插件安装只接受此目录中的固定包，服务端调用官方 `dsh plugin`，安装环境与 QualityFlow 的账号、数据库和模型凭据隔离。

命令行可用 `dsh --profile qualityflow --dump-config` 核对配置中的 `qualityflow-connector` 条目。运行该命令前，将 `DSH_HOME` 指向对应的 `STORAGE_ROOT/dsh-console/<tenant-key>`，其中 `tenant-key` 是租户 ID 的 SHA-256 十六进制字符串前 24 位。CLI 管理命令和加载行为见 [DeepSeek Harness CLI 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/reference/README.md#plugin-management)。

## 测试

```powershell
$env:PYTHONPATH='backend'
.venv\Scripts\python.exe -m pytest backend/tests/test_dsh_console.py -q
npm --prefix frontend run build
```

API 测试覆盖管理员授权、固定插件目录、租户隔离、重复安装、安装结果验证、审计和命令环境。浏览器验收可用管理员实际安装、卸载并重新安装，再切换到工程师确认只显示调查会话。
