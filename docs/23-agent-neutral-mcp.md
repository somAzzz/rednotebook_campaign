# 本地模型与调用方 agent 的双路径 MCP

## 路径选择

先调用 `analysis_capabilities`。两个路径共用采集、来源许可、证据校验、撤销和策划门槛。

| 阶段 | caller_analysis（Codex 等调用方） | local_model_analysis |
| --- | --- | --- |
| 检索计划 | plan_search(use_model=false) | 可选 use_model=true |
| 搜索 | search_with_plan / search_notes | 相同 |
| 选择和采集 | prepare_topic_evidence；单篇 collect_note 或 research_workflow(analysis_mode="caller_analysis") | research_topic / research_workflow，默认模式保持不变 |
| 正文、评论、指标 | read_evidence_bundle，遍历 next_offset | 工作流内部读取 |
| 图片 | read_capture_image 返回原始 MCP ImageContent，由调用方视觉模型阅读 | analyse_gallery 使用配置的本地视觉模型 |
| 导入 | prepare_capture_review，提交逐页摘记后一次导入 | analyse_gallery → import_capture |
| 引用 | read_agent_catalog，分页固定 revision/span/hash | 内部 EvidenceGateway |
| 研究 | validate_agent_findings → submit_capture_review | research_notes / 本地工作流 |
| 主题完成 | assemble_reviewed_topic | research_topic 完成门槛 |
| 策划 | create_campaign → revise_campaign_output → submit_campaign_review | create_campaign → generate_campaign |
| 审核与导出 | check_campaign / preview_draft；正式批准仍为独立操作 | 相同 |

调用方路径不读取 model.toml、不启动本地模型，也不自动调用远端模型 API。MCP 只把许可内的证据返回当前客户端；推理由调用方完成。无视觉能力的客户端不能宣称图片已读完。`research_topic` 和 `research_workflow` 也接受 `analysis_mode="caller_analysis"`；默认仍是本地模型路径，避免改变旧客户端行为。

## 调用方顺序和数据约定

1. 搜索完成后提交 TopicSelection（选取理由、每篇问题、覆盖、缺口）。`prepare_topic_evidence` 返回持久 job_id，`get_job` 返回每篇的 capture_id 和 research_brief。采集完成只表示 evidence_prepared，research_complete/campaign_ready 保持 false。
2. 按 capture_id 读取 `read_evidence_bundle`。offset 指向文本切片，不是评论序号；每片最多 1200 字符，limit 默认20、上限50。遍历直到 next_offset=null。后续页传首响应 bundle_sha256 为 expected_sha256，阻止混读变化快照。正文、标题、评论和 media_text 都不静默截断；字段空字符串单独保留。metadata 保留原指标值、precision、sampling、coverage；公共链接已移除查询和 fragment。这里只返回已采样评论，绝不代表平台全部评论。
3. 对 images 的每个 position/sha256 调用 `read_capture_image(capture_id, position, expected_sha256)`。响应包含原始图片字节、MIME、尺寸、哈希，不暴露本地路径、不重采样。单张上限20 MiB，支持 PNG/JPEG/WebP/GIF；超限或不支持的格式明确失败。
4. 阅读图片后调用 `prepare_capture_review(capture_id, brief, pages)`。每页 PageReading 包含 position、sha256、text、key_content_readable；必须覆盖全部页，关键内容不可读时不能放行。没有图片时 pages=[]。这里才导入带图文摘记的证据，因此不要先 import_capture 再补写摘记（同一观察时间的原始证据不可覆盖）。返回 input_sha256。
5. `read_agent_catalog(capture_id, input_sha256, offset, limit)` 返回引用 ref_id 及确切 evidence_id/revision/field/start/end/excerpt/sha256，遍历全部页。`compare_capture_metrics` 使用确定性指标计算。原始 capture 切片不能直接用作正式 ref_id。
6. 按 `rednotebook://schemas/agent-findings` 构造 draft。最多5条发现，各有 support_ids、counter_ids、counter_search、limitation。`validate_agent_findings` 只读验证；`submit_capture_review` 再次校验并保存标准 research run 和 workflow job。引用正确不等于结论真实；结果保留 semantic_quality_verified=false、human_accuracy_review=not_run。
7. 将 note_id → review job_id 映射交给 `assemble_reviewed_topic`，原选择、来源、每篇问题、采集门槛和阅读门槛一致才可得到 campaign_ready。采集失败仍先恢复子任务，不能绕过门槛用 allow_partial。
8. `create_campaign` 绑定 topic_job_id。调用方用 `rednotebook://schemas/campaign-output` 生成文案，`revise_campaign_output` 保存；再按 semantic-review schema 提交审阅。`submit_campaign_review` 必须提供 `read_draft` 返回的版本和 content_hash，新版本不继承作者确认或批准。预览可用；正式导出仍需要独立批准。

相关资源：`rednotebook://schemas/brief`、`topic-selection`、`page-reading`、`agent-findings`、`campaign-output`、`semantic-review`。

## 许可与处理方身份

新的调用方证据/图片/提交接口要求来源 `cloud_processing=allowed`，或操作者此前记录的精确 capture、processor、文件哈希、有效期授权。无自动云端回退，也没有 MCP 放宽许可工具。`--caller-processor` 是启动时由操作者选择的审计身份，不是 agent 在每次调用中自报的绕过选项；它不是网络用户认证。

既有7篇授权仍仅属于 `codex-assistant`。启动该授权对应的服务须明确 `--caller-processor codex-assistant`；其他名称不能继承。新的广泛来源授权由原有 source-grant 流程管理。用户明确授权单次材料后，停服务并由操作者执行：

```sh
uv run rednotebook-browser --db private/browser-research.sqlite authorize-caller \
  --capture-id CAPTURE_UUID --processor codex-assistant \
  --statement-file /absolute/path/explicit-user-consent.txt
```

不将授权正文写入终端日志。此操作不改变 source grant、其他权限或保留期限。同一 capture 的现有授权不可覆盖。旧的本地读取/研究工具仍遵循原有 local_analysis 许可；云端客户端应使用调用方路径，不能用本地接口规避 cloud_processing 限制。

## 单客户端与多个客户端

旧 STDIO 用法继续可用，模型配置在调用方路径可以不存在：

```sh
uv run rednotebook-mcp --db private/browser-research.sqlite \
  --caller-processor codex-assistant
```

多个受信任的本机 MCP 客户端共用一个服务进程：

```sh
uv run rednotebook-mcp --db private/browser-research.sqlite \
  --caller-processor codex-assistant --transport streamable-http --port 8000
```

客户端连接 `http://127.0.0.1:8000/mcp`，不要各自再启动 STDIO 服务。服务固定绑定 loopback，并保留 MCP SDK 的 Host/Origin 防护。共享一个 BrowserService、数据库和有界任务管理；浏览器占用时仍明确返回 browser_job_already_active，而非并发导航。客户端断开不关闭其他会话；服务退出才清理。独占数据库锁仍保留，防止第二个进程破坏恢复状态。

这不是公网托管或多租户账户隔离。所有连接共享操作者配置的 caller 身份和本地信任范围；不同许可范围应使用独立工作区，不能用同一服务伪装成不同受权方。

## 迁移、保留与验收

schema 12 → 13 只激活 caller_analysis_contract_version。历史 grant、证据、job JSON 和哈希不重写；旧 JSON 默认读取为 local_model_analysis。新模式和通用处理方元数据仍存于现有 source-bound jobs/research/managed-captures，来源到期或撤销沿用既有清理，并使依赖策划不可用。没有新永久图片副本或独立缓存。

离线验收覆盖：完整正文和评论分页、原始 MCP 图片字节、跨 agent 授权隔离、未知引用/错误哈希拒绝、无图笔记、无本地模型采集准备、主题门槛、未批准导出阻断、撤销清理、双 MCP HTTP 客户端的共享任务及断连生命周期。本地模型既有离线回归保留。本轮不启动真实本地模型、不新增真实账号采集；这不替代 docs/18 的真实材料和人工精度验收。

验证记录（2026-09-19）：`uv sync --locked`、`uv run pytest -q`（230 passed）、`uv run ruff check .`、`uv run ruff format --check .`、`check_scaffold.py`、`verify_foundation.py` 通过。最后补充的 HTTP 原图协议断言与调用方模式审计字段通过相关10项测试。仓库与已安装 Research Skill 已同步。
