# 意图分析与查询扩展

v0.8.0。新增计划生成与计划执行两个步骤，原search_notes继续作为明确的单查询工具，不会悄悄扩大旧调用。

## 行为

原始primary_query始终是第一条查询，并原样保存。例如`rtx pro 6000`默认先执行主体检索，再补充专业显卡、高价显卡用途、大显存显卡应用场景。这些是探索方向，不是关于该型号的已核实价格、显存或性能结论。

计划记录用户目标、各查询的类型、理由、假设和预算。类别/角度/场景分为category/angle/scenario；暂不自动生成产品别名或代际等价关系，防止混淆不同型号。高价值的价格、性价比与用途价值歧义会保留为假设。

默认规则不调用模型：依据显式类别、GPU词形和目标生成小规模扩展；陌生主体没有依据时仅保留主体。复杂意图可显式use_model=true，交给本地模型提出扩展，程序仍强制原词优先、去重和预算上限。用户提供expansions时优先采用，可通过新建计划修改查询。

exact_only=true或目标明确“只看这个型号/不要扩展”时只保留主体，不调用模型。默认4个查询，主体最多20条，扩展每词最多5条、每词2次滚动。扩展总候选槽位不超过主体槽位；不保证实际返回条数或相关性比例。最多6个查询、每词20条、5次滚动。保留原浏览器导航节奏、小时额度和访问暂停机制。

## MCP使用

1. 读取`rednotebook://schemas/search-intent`或运行`rednotebook schema search-intent`。
2. `plan_search(source_id, intent, use_model=false)`创建来源绑定的规划任务。用get_job(include_result=true)取得计划；规划不访问网页，浏览器暂停时也可进行。
3. `search_with_plan(source_id, plan_job_id)`执行已完成的同来源计划。通过get_job读取进度与结果。用户已请求扩展检索时，查看计划后即可继续，无需重复索取许可。
4. 按需要从候选中选择笔记，再使用原collect_note流程。扩展搜索不自动采集详情，也不推断正文或评论。

可将作者明确给出的Campaign creator_goal/reader_task整理到user_goal；本地模型不会自动读取外层对话。不要把来源不明的研究摘录伪装成作者目标。

结果中的groups只标识命中哪个查询层，不是已经确认的相关性。核心词返回的候选也可能不相关。按note_id去重；hits保存所有命中查询、观察时间、标题及原始metric_display，null与“0”保持区分，未知指标不改成点赞数。候选列表按核心查询优先顺序保留，不按不明指标排序。

public_url可展示；collect_url仅用作后续采集输入。结果明确标注bounded_search_not_representative、ranking_unverified、expansion_not_subject_evidence。其他显卡的经验不能作为该型号性能或兼容性的证据。

## CLI

先使用已登记且有相应权限的source：

```bash
uv run rednotebook schema search-intent
uv run rednotebook-browser --db <数据库> plan-search --source <来源ID> --file examples/search-intent.json
uv run rednotebook-browser --db <数据库> plan-search --source <来源ID> --file examples/search-intent.json --generate
uv run rednotebook-browser --db <数据库> search-plan --source <来源ID> --plan-job <已完成规划任务ID>
```

上面两个plan-search命令分别演示规则规划和本地模型规划，实际选择其一。CLI与MCP使用同一服务锁；已运行MCP时通过其工具操作，不能并发启动另一个服务抢占数据库。

## 失败、保留与迁移

每完成一个查询保存检查点。任一查询失败即停止，不自动重试、不继续下一个扩展；get_job保留已经完成的query_results和候选。有界搜索通常为partial，全部查询执行完以execution_complete=true单独表达，不能将执行完毕称作完整平台采样。显式retry_job创建新尝试并从主体重新执行，不是假装无损续传。

权限在每个查询前后检查。来源撤销/到期使规划、任务输入、结果及检查点不可读，并由原清理流程删除其内容。规划先保存规则版本；模型规划失败时可以读取该草案，但不能直接执行失败的规划任务，应修正后创建新计划。

SQLite 8→9是显式向前迁移，登记search_plan_contract_version=1并提高数据库版本。不重写旧任务JSON、旧草稿、哈希或批准；新增JobRequest字段提供默认值，旧任务继续可读。旧二进制应拒绝v9，不能降级解释新增操作。新计划存放于原browser_jobs受控JSON内，无新增内容存储位置。

## 验证边界

新增离线测试覆盖原词优先、限制扩展、预算、词形去重、模型无法替换主查询、多查询笔记去重及命中来源、暂停期间规划、失败停止与检查点、生成中撤销、MCP入口及旧任务迁移。

本地qwen3.8-27b已用示例目标实际生成一个计划：1次请求，主查询不变，并补充类别、用途场景和内容角度，假设单独记录。此次未执行真实网页搜索；搜索召回率、扩展相关性和型号混淆率尚待真实样本评估，不因工程测试通过就称作检索质量已验证。

本轮最终验证：`uv sync --locked`通过；`uv run pytest -q`为166 passed（13.75秒）；ruff检查与格式通过；check_scaffold验证55个本地链接；verify_foundation通过（0.632秒、峰值124.1MB）；Research/Campaign两个Skill结构校验通过。基础验证时间2026-09-17 19:37:32 UTC，源码哈希`42ba75e836f905f7d10019265275fe441cb1523c0905eea3c06a4d21cd7d9352`。模型规划实跑仅传入示例意图，不读取真实笔记或访问浏览器。
