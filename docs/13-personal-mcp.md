# 个人使用优化（v0.6.0）

2026-09-17。目标是减少重复操作、保留失败前的材料、找回历史及降低复核成本。本轮全部验证使用离线临时数据库和明确标注的合成材料；没有解除真实浏览器暂停，没有新增真实采集或模型准确率验收。

## 日常入口

- `workspace_status`：查看 Chromium 是否安装、模型配置是否有效、来源到期时间、暂停原因及下一次导航等待时间。不发起页面或模型请求。
- `list_history(kind="jobs|notes|research|drafts", query="", limit=20, offset=0)`：分页找回历史。笔记按最新观察去重，支持本地文字查找。列表不返回签名采集链接。
- `read_note(evidence_id, revision)`、`read_research(run_id)`、`read_draft(bundle_id, version)`：读取已有材料、研究和精确版本草稿。每次读取重新检查来源许可。
- `get_job(job_id, include_result=true, wait_seconds=30)`：最多等30秒；等待超时不取消任务。返回当前阶段、阶段计数、来源绑定的已保存采集和恢复建议。
- CLI `rednotebook --db <数据库> doctor --profile <专用配置> --config <模型配置>` 提供相同本地诊断。模型连通性仍需显式执行 `model probe`。

## 一篇笔记的整合流程

先通过 `search_notes` 选定候选，再调用 `research_workflow`：

```json
{
  "source_id": "已登记且有效的来源ID",
  "collect_url": "搜索返回的完整采集链接，仅作为工具参数",
  "brief": {
    "id": "my-topic",
    "synthetic": false,
    "product": "个人研究",
    "audience": "研究对象",
    "research_question": "本次具体想了解什么？",
    "objective": "整理可核对的样本观察",
    "keywords": ["检索词"]
  }
}
```

任务依次采集、逐图分析、导入及研究。不会替你注册来源许可，不自动搜索下一篇。Brief契约和预算契约可读取 `rednotebook://schemas/brief`、`rednotebook://schemas/research-budget`。

- `capture_images=false`：只采集文字和限定评论，不点击图片分页，不推断图片总数；`analyse_images=false` 则仅跳过模型图片分析。
- 正文读取后、评论更新后、每张图片下载后，都原子保存当前证据文件。异常、超时、取消或服务重启后可找回已保存部分；保留缺页和未知总数。
- 新采集出现 partial 后停止整合流程，不自动将不完整材料送入研究。详情DOM、搜索DOM或分页等任务局部失败只结束当前任务。总任务超时、契约错误和未知本地异常产生失败暂停；下一次用户明确发起的新检索可在 `access.new_search_reset_available=true` 时复位并保留访问历史。登录、验证码、限流、访问拒绝、意外页面、人工及未知暂停仍须操作者恢复。失败后只检查一次 `browser_status`，不自动重试；`retry_job`、采集和工作流续跑都不会复位。
- 查看结果后，可使用 `import_capture(capture_id, enriched=false, allow_partial=true)` 显式导入文字；或调用 `research_workflow(capture_id=..., brief=..., source_id=..., allow_partial=true)` 继续本地处理。续跑不需要解除浏览器暂停。
- 使用结果中的真实 `capture_id`，而非后续分析任务ID。传入的Brief ID必须与采集时一致。
- 工作流失败重试优先使用已保存的capture，避免重新访问网页。缺页补采仍需操作者明确恢复后的新采集；没有跨网页自动断点续采。
- 已入库观察保持不可变。先只导入文字、再改变同一观察的图片提取内容，会按原有冲突规则拒绝；优先在首次导入前完成想要的图片分析。

浏览器操作串行，模型操作独立串行；模型等待期间可做独立浏览器任务。SQLite仍在同一事件循环线程中操作，不跨await持有数据库事务。队列总计最多4个活动任务，同一数据库仅启动一个服务。

## 本地模型预算

默认深度研究保持较宽裕的预算，而非为了省token截短证据：

| 参数 | 默认 |
|---|---:|
| 笔记/评论 | 200 / 500 |
| 单字段字符 | 50,000 |
| 模型请求 | 60次，修复重试也计入 |
| 输入预算 | 4,000,000；请求前按UTF-8序列化字节保守预留，并另记服务报告token |
| 输出预留预算 | 524,288 tokens |
| 单次模型输出 | 示例配置8,192 tokens |
| 单次模型超时 | 示例配置180秒 |
| MCP模型任务总超时 | 7,200秒，可配60–28,800秒 |

`research_notes` 和 `research_workflow` 可传 `budget` 覆盖预算及 `model_timeout_seconds`。`mode="quick"` 只将选样上限改为20篇/100评论，不降低单字段文字保留额度；默认 `deep`。模型实际上下文容量仍取决于本地服务，可通过 `batch_notes` 调小每批材料量。没有云端回退。

引用将长字段切成最多1,200字的连续片段，保留原文偏移与哈希；这不是删除中间文本。正文、评论、图片识别文字带程序生成的来源标签。报告中的图片分析状态也与实际导入信息一致。

## 用户复核

`read_research` 返回原始结论、引用原文、来源类别及独立反馈记录。

只有用户明确给出某条结论的反馈时，才调用 `record_finding_feedback(run_id, finding_id, feedback)`：

```json
{
  "decision": "correct",
  "corrected_claim": "用户修正后的观察",
  "note": "原结论把陈述误认为问题",
  "user_instruction": "这里记录用户明确给出的反馈指令"
}
```

decision为accept/correct/exclude；仅correct可填写corrected_claim。反馈追加记录，不覆盖原研究结果；接受不等于独立语义验收。新草稿排除被exclude的发现，并采用correct文本，仍保留待核对标记。旧草稿、版本哈希和正式审核不变。现有草稿要应用后来的反馈，需创建新草稿或显式修订。

## SQLite 6 → 7 迁移与保留复核

显式事务迁移只新增 `browser_jobs.progress_json` 及 `finding_reviews` 表和索引，不改写旧证据或观察。新进度为阶段/计数/运行标识；反馈文本包含用户修正和说明，按源派生内容对待。

来源撤销或到期sweep会：清除任务请求、结果和进度；删除关联研究的全部反馈（任一依赖来源失效即删除）；继续清理受管采集文件、媒体缓存、研究、草稿及导出。所有新增读取都复查来源权限，到期但尚未sweep的内容也不能读取。独立浏览器profile及外部手工备份仍不纳入单一来源清理。

## 验证与剩余限制

验证命令：`uv sync --locked`、`uv run pytest -q`、`uv run ruff check .`、`uv run ruff format --check .`、`scripts/check_scaffold.py` 与 `scripts/verify_foundation.py`。脚本在临时工作目录执行，使用项目Python环境，避免读取真实账户数据库。

125项离线测试通过，包括真实Playwright访问本地合成DOM的两图采集→图片分析替身→入库→研究替身、MCP stdio发现/诊断/脱敏、故障检查点、暂停状态下本地续跑、锁分离、反馈追加/旧版本不变、到期清理、v6迁移和完整原文分段引用。

这些测试不能替代真实页面采集与本地模型语义质量验证。真实多图翻页仍按[实测记录](12-mcp-research-test.md)待验收；未知图片数量仍保持partial，单图/原生纯文字形态没有新增真实验收。修复提示与来源标签不能保证结论正确，正式人工支持关系评估及长期使用价值仍待完成。
