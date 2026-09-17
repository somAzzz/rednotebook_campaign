# S4 本地模型接入与验收

日期：2026-09-17，版本 v0.3.0。已连接用户指定的 `qwen3.8-27b` 服务，连接配置保存在 Git 忽略的 `private/model.toml`，API Key 不进入提示词、研究元数据或错误报告。

## 已实现

- `model probe`：检查 `/v1/models`，真实调用一个只读测试工具，再验证中文结构化输出。
- `analyse --brief <file>`：验证 Brief/素材、按不可变版本保存 Brief，读取获准资料并按来源/采样组轮转选样，分批调用 Pydantic AI。
- 只注册三个研究工具；仅允许读取当前批次的证据。指标在整个选定研究样本中确定性计算，再只返回当前批次笔记的分数，避免每批不足 20 条导致所有分数失效。
- 模型返回 ref_id；代码将其解析为 evidence_id、revision、字段、字符区间、原文摘录和 SHA-256，拒绝不存在、越界、错版本或不匹配的引用。
- 结构与引用错误最多修复两次。SDK 网络自动重试关闭；连接/HTTP/超时错误立即停止本轮后续调用，保留已完成批次。
- 报告保存在 SQLite。`research show <id> --format json|markdown` 会重新检查来源许可；Markdown 转义模型提供的 HTML、图片和链接语法。

## 配置与命令

配置模板见 `config/model.example.toml`；当前连接已配置，可直接运行：

```bash
uv run rednotebook model probe
uv run rednotebook analyse --brief examples/brief.cando.synthetic.json --max-notes 10 --max-comments 30 --max-model-calls 8
uv run rednotebook research show <run_id> --format markdown
```

以上 analyse 需要同 brief_id 的资料已导入所选数据库。使用其他数据库时在子命令前指定 `--db`。没有获准笔记时不调用模型，返回材料缺口。CLI 中的 brief 路径与导入数据中的 brief_id 是不同参数，前者内容的 id 必须与后者一致。

连接仅接受明确的回环或 RFC1918/ULA 局域网字面 IP；不接受公共域名、凭据 URL 或元数据链路本地地址。HTTP 客户端只允许已配置端点上的 `/v1/models` 与 `/v1/chat/completions`，禁用环境代理和重定向。不使用云端回退。局域网服务属于操作者配置的受信模型处理环境，数据用途仍需 local_analysis 许可。

## 预算与状态

默认最多 200 篇笔记、500 条评论、30 次模型请求，每批 10 篇。每字段最多向模型提供 1,800 字符，截断会明确计数。默认单请求最多 2,048 输出 token、45 秒（可配置，上限 60 秒）；每批最多 8 次只读工具执行。

调用前以序列化消息/工具结构的 UTF-8 字节数加 1,024 预留输入预算，总上限 200,000；每次调用按输出上限预留，总上限 20,000。它是保守估计与预留机制，并非 Qwen tokenizer 的精确计数。失败和纠错都消耗预留，不回收未用输出，因此可能在实际用量较少时提前停止。报告同时保留服务端实际返回的 token 用量；超过声明预算即停止。未知服务端开销不能由客户端预先证明。

`complete` 表示选定批次完成；`partial` 表示选择被预算缩减、字段截断、后续失败或预算耗尽；`failed` 表示没有批次完成即失败。任一状态都不意味着语义准确性通过。没有证据时不生成结论。运行中断保存已有记录；进程强制终止可能留下 running，当前无自动恢复，重新运行将创建新 run_id。

## SQLite v1 → v2 迁移与保留策略

新增 briefs、research_runs、research_sources，旧证据表及历史记录不改写；打开 v1 数据库自动以事务升级 user_version=2。研究绑定 Brief 的确切版本及所有贡献覆盖统计/研究输入的来源。来源撤销或到期清理时将相关研究状态置 revoked 并清空 report_json；查询立即拒绝过期或撤销来源。模型运行途中撤销许可也不能重新提交报告。

只保留不含原文的模型配置摘要、预算、输入哈希、提示版本和来源关联。研究原文引用仅存在于受控数据库报告中，未保存模型完整对话日志。本地操作者主动保存的 Markdown/JSON 副本、模型服务端可能留存的请求与外部备份不在清理范围内；后续图文包要另行纳入生命周期。

## 实际验收

- `uv run pytest -q`：71 个离线测试通过（含原有 48 个）。
- Ruff 检查和格式检查通过；S1–S3 合成端到端回归通过。
- 本地模型探针：HTTP 200，模型名匹配，中文结构化输出和带参数的只读工具调用通过。
- 真实服务处理 3 篇合成笔记、6 条合成评论：1 批 complete，3 条待核验发现，9 处引用匹配，未执行恶意评论中的指令。
- 最新研究运行使用 2 次请求；服务端报告输入 9,794、输出 526 token；调用前预留输入 34,408、输出 4,096。探针另用 2 次请求。这些数值只描述这一轮合成测试。

机器记录：[S4 验收摘要](research-verification.json)。可复跑的显式在线脚本为 `uv run python scripts/verify_research_live.py`，结果写入 Git 忽略的 `artifacts/s4-live/`，包含研究草案及机器报告。该脚本不属于默认 pytest，不会在普通测试中偷偷连接模型。

适用用例：A10（假引用/错片段）、A12（工具白名单/禁止外联）、A15（坏 JSON/超时/预算）、A16 当前研究报告的撤销清理已做自动验证；v1 迁移、Brief 版本、同源授权、无资料不调用模型也有测试。

## 未通过或未执行的部分

**A11 真实研究准确率尚未验收，不能宣称达到 90% 支持关系精度。** 尚无获准 F1 真实留出集及 30 条人工标注。合成草案人工查看已发现：把个别反馈写成“主要障碍”、把任务延期的反例扩大到任务拆分等风险。词面引用匹配不能发现全部推论错误，所以所有发现固定为 hypothesis / semantic_review=pending，不能自动视为已审核事实。

模型端对无参数工具曾生成不合法的额外参数；探针及无参数统计工具已改用明确的受限参数并实测通过。此兼容处理不放宽白名单，也不证明所有工具序列都稳定。

没有测试图片能力、没有真实小红书采集、没有执行创意发布或业务效果试点。进入 S5 前仍应完成真实样本质量门槛及产品事实核验。

实现参考：[Pydantic AI 结构化输出](https://pydantic.dev/docs/ai/core-concepts/output/)、[兼容模型提供者](https://pydantic.dev/docs/ai/models/openai/)、[用量限制](https://pydantic.dev/docs/ai/api/pydantic-ai/usage/)。实际依赖版本以 uv.lock 为准，客户端锁定 openai 2.x，避免与当前 Pydantic AI 的 HTTP 客户端类型不匹配。
