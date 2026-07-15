# LogPilot 技术方案

## 架构摘要

LogPilot MVP 采用 Python CLI 核心加本地 Web 分析工作台。CLI 负责扫描仓库、识别日志、执行规则分析、调用本机运行时、生成报告和 Patch；Web 工作台支持选择仓库与 Codex/Claude 运行时，并展示当前与历史结果。

```mermaid
flowchart LR
  A["目标代码仓库"] --> B["CLI 扫描入口"]
  G["Web 分析工作台"] --> B
  B --> C["解析与规则核心"]
  C --> D["运行时注册表"]
  D --> H["Codex / Claude CLI"]
  H --> I["结构化 AI 分析结果"]
  C --> E["报告与 Patch 生成"]
  I --> E
  E --> J["用户应用数据目录"]
  J --> F["报告、问题与 Patch 展示"]
  F --> K["校验并采纳精确修改"]
  K --> A
```

图示说明：核心扫描能力不依赖 UI；规则分析留在本地进程，AI 分析批量交给选定的本机运行时。所有产物进入用户应用数据目录，只有用户确认采纳后才校验并修改目标源码。

## 模块划分

- `scanner` 遍历仓库并过滤无关目录。
- `parsers` 识别 Python、Java、JavaScript、TypeScript 中的常见日志调用。
- `rules` 检测禁用日志、低价值日志、重复日志、敏感字段和异常缺失日志。
- `runtime` 发现并锁定 Codex/Claude 可执行文件，检测版本、健康状态并受控执行命令。
- `ai` 构建批量日志 Prompt，校验结构化返回并转换为问题；配置默认关闭，UI 显式选择运行时后启用。
- `reporting` 输出最新 `report.json` 和 `report.md`。
- `storage` 按仓库规范化路径的 SHA-256 将产物隔离到用户应用数据目录。
- `settings` 保存仓库级语言选择与固定日志模板，并根据文件及现有日志生成语言画像和模板推荐。
- `fixes` 将规则问题转换为统一的删除、替换或插入修复；Python 异常日志会在 AST 语法校验通过后才成为可采纳项。
- `history` 将每次扫描保存到 `repositories/<repository_id>/runs/<run_id>/`。
- `patching` 根据统一修复模型生成可审查 Diff，`remediation` 负责精确校验、跨文件原子采纳、备份和回滚。
- `web` 提供目录与运行时选择、一键扫描和历史记录，并以按文件分组的纵向结果流就地展示原因、源码与 Diff，支持单项或文件级批量采纳及回滚。

## 运行时安全边界

- Codex 使用 `exec --ephemeral --sandbox read-only`，Claude 使用 `--tools "" --permission-mode plan`。
- 所有命令使用参数数组直接启动，不经过 Shell 拼接；分析 Prompt 通过标准输入传递。
- 单次扫描批量提交日志并设置超时，返回值必须符合预设 JSON Schema。
- 可通过 `LOGPILOT_CODEX_PATH`、`LOGPILOT_CLAUDE_PATH` 固定可执行文件路径。
- 扫描不写入目标仓库；`.logpilot.yaml` 仅作为可选的用户配置读取。
- 语言选择、模板与仓库风格画像保存在用户数据目录；模板按“用户固定、仓库推荐、内置安全模板”顺序解析。
- 采纳前校验原始行及上下文，事务备份统一保存在用户目录，源码变化时拒绝写入或回滚。

## MVP 约束

当前实现优先保证本地可运行和可测试，因此核心路径不强依赖第三方库。后续可在现有边界后替换为 Typer、FastAPI、Pydantic 和 Tree-sitter，而不改变用户命令、Web 操作流程、最新报告产物和历史 run 结构。
