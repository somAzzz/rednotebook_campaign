# 从候选扫描到精读与策划

2026-09-17，v0.9.0，SQLite 10。

## 默认流程

用户要求“先研究再策划”时，先用 `plan_search` 和 `search_with_plan` 扫描约30–40篇去重候选，再由调用模型结合用户目标选择3–5篇，调用 `research_topic` 逐篇打开正文、采集图片/评论、分析图片、导入及研究。选篇理由是待验证判断，不能仅按搜索卡片显示数字取前五。

搜索数量配置保持原样：核心查询默认20，最多3个默认扩展各5，总计35个候选槽位；去重后实际数量可能不足30。报告实际数量与覆盖缺口，不循环搜索凑数。精读默认3–5篇；可提供 `sample_exception` 解释为何只有1–2篇。每篇保存 `reason`、`question` 和 `coverage`，单篇问题追加到研究问题中。不同笔记使用独立研究 brief，避免后一篇研究混入前面已导入的样本。

只搜索时可以停在候选列表；只用作者材料创作时不需要浏览器或研究。搜索阶段保留 `metric_display` 原值及每次查询命中，不自动解释成赞藏量或稳定热度排名。

## MCP 接口

1. 读取 `rednotebook://schemas/topic-selection` 和 `rednotebook://schemas/brief`。
2. 调用 `research_topic(source_id, selection, brief)`。`selection` 指向同一来源的已执行完毕 `search_plan` 任务，并选择其中的笔记ID。程序从保存结果解析采集地址，不接受任意替换地址。
3. 用 `get_job(include_result=true)` 读取进度。结果分别报告 `search_complete`、`candidate_count`、`selected_count`、`completed_count`、`research_complete`、`campaign_ready` 和实际研究记录ID。`notes` 保留入选理由、问题、子任务ID及 `research_brief`，完成子任务包含 `reading_scope`、图片分析和研究结果。
4. 完成后读取各篇研究及引用，比较共同点、分歧、限制和证据空白。若内容与选择时的判断不符，可解释原因并重新选择，不把标题匹配当作正文相关性证明。
5. 创建 ContentBrief 时设置 `research_mode="research_then_plan"` 和 `topic_job_id`。程序自动接入各篇研究、来源和 bundle 依赖。缺少或未完成精读的此模式会被拒绝。
6. 生成并审阅策划，核对作者目标、研究支持、重复免责声明和面向读者的表达。创作提示词要求跨篇比较，但语义质量仍需人工复核；不会因为结构通过就宣称研究准确或文案有效。

`capture_images`、`analyse_images` 默认 true，`max_images` 默认20，评论抽样目标默认5。按目标调整读取范围：图片包含主要正文时保留图片分析，研究读者问题时采集评论。读取范围会保存，关闭图片分析不能声称已读完图片。所有笔记都沿用本地 deep 预算，默认每篇60次模型请求、4,000,000输入预算，不因批量精读压缩单篇预算。

独立的搜索辅助创作使用 `research_mode="search_only"` 和 `search_job_id`，搜索标题、原始显示指标及命中信息进入有来源依赖的上下文，不包含签名采集地址。作者材料模式使用 `author_only`。为兼容旧 brief，缺省 `optional` 保留旧行为；新 Skill 和 MCP 说明要求根据用户任务显式选模式，程序不能仅凭自然语言判定用户原本要求什么。

## 停止与恢复

明确识别的视频返回 `skipped/video_deferred`，不采集或研究视频，继续余下图文候选；记录 `skipped_video_count`，不计入精读完成数，需补选时新建选择。其他子任务失败、暂停或部分完成时，批量任务立即停止，不继续后续笔记，也不自动重复采集。已完成任务保留；父任务取消会取消当前子任务，已保存材料保留。浏览器暂停处理规则与现有单篇流程一致，topic 操作和续跑不会复位暂停。

读取结果的 `stopped_job_id`。可以用该条 `research_brief` 和已保存 `capture_id` 显式恢复单篇 `research_workflow`，或在必要时由用户明确要求重新采集。允许部分材料生成的研究仍可保持partial，它可以支持受限报告，但不能把缺失阅读算作完整样本。单篇恢复完成后，调用：

```text
resume_topic_research(
  job_id="停止的主题任务ID",
  replacement_jobs={"笔记ID": "完成的恢复子任务ID"}
)
```

续跑验证来源、原研究 brief 与笔记身份；重用已完成子任务，只执行尚未开始的条目。服务重启后的父任务也可按此方式恢复。不能通过 `retry_job` 从头重跑主题任务。新选择创建新主题任务；目前不同选择之间不自动复用子任务，避免把不匹配的问题或阅读范围当成已完成。

## 存储、迁移与撤销审查

显式9→10迁移只登记新JSON契约版本，不重写旧任务、旧bundle payload、hash和批准状态；旧程序会拒绝新数据库版本。主题任务与子任务仍保存在来源绑定的 `browser_jobs` 中，选择理由、问题、读取范围和进度随来源撤销/到期清除。策划从主题任务解析实际研究ID并写入既有 `bundle_runs`/`bundle_sources`，依赖旧机制清理所有版本及受管导出。搜索辅助上下文也登记来源。

本次没有创建额外不受管理的研究副本。签名链接仅在已有来源任务请求/搜索结果中用于采集，不进入主题结果或策划上下文。手工另存报告和模型端点自身的请求留存仍不属于数据库清理范围。

## 验证与限制

离线临时数据库测试覆盖：35篇合成候选、3篇真实代码路径采集/导入/研究（使用确定性模型替身）、策划研究依赖、来源撤销、第二篇失败停止、显式恢复不重复第一篇、部分材料不能冒充精读完成、跨来源与候选身份校验、暂停不复位、父子取消及重启读取、9→10旧payload不变，以及MCP工具实际调用。

这不是在线选样质量、真实图片阅读质量或文案传播效果验收。本轮不访问真实账号、不读取真实研究数据库。部署后需重启MCP服务以发现新工具；首次用新版打开数据库会执行迁移。搜索、正文与评论始终是有界样本；已读原文仍可能只是作者未经核实的自述。

验证命令：`uv sync --locked`、`uv run pytest -q`（183 passed）、`uv run ruff check .`、`uv run ruff format --check .`（95 files）、`uv run python scripts/check_scaffold.py`（57个链接）、`uv run python scripts/verify_foundation.py` 均通过；研究与策划两个Skill通过quick_validate。测试使用临时库和合成材料，不代表真实内容质量通过。

后续完整精读按[审核与验收标准](18-mcp-review-acceptance.md)执行：正文与全部图片完整采集、逐图读取独立验收，评论允许少量高讨论度样本。该标准列出的差距尚待实现，不能将本页历史工程测试视为完整采集验收。

v0.10.0新增独立完整性门槛、评论采样与resume_collect，参见[采集审计修复](20-capture-audit-fixes.md)。
