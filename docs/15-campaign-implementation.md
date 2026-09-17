# 内容策划实施与使用

日期：2026-09-17，版本 v0.7.0。依据[方案14](14-campaign-design-proposal.md)实施。新功能在合成临时数据库验证；原有真实研究与浏览器状态不用于本轮验收。

## 已实现的流程

- 独立的ContentBrief允许作者材料创作，研究运行列表可为空，不伪造research run。硬件、每个项目和具体测试分别记录。
- 创作上下文保存作者原始目标、假设、素材元数据、选定研究观察、支持/反例、反馈及缺口。模型实际收到该上下文；请求、上下文、契约和提示哈希以及预算消耗可追溯。
- campaign_plan与post_draft分别面向作者和读者。新发布稿支持1–20页；旧研究摘要仍保持六页及原审核行为。
- 模型生成草稿先保存，再单独做语义审阅；后一步失败返回saved_bundle_id和saved_version。无工具调用，不具备发布或账号互动能力。
- 检查区分program/model/author。引用存在不代表支持结论，模型疑问不自动当事实错误；无CTA、基线或发布时间不阻断普通创作。
- 作者确认产生新草稿版本，随后由原review命令批准确切版本及哈希；改Brief、文案或素材均需重新确认和批准。
- 可选系列上下文区分内部候选和明确承诺；通过修订Brief更新兑现状态并保留历史。历史查询能查到无来源草稿及series_id。
- 可选Outcome支持带分类定义和出处的具体读者反馈、作者记录，也可沿用指标输入。真实时间和缺失值规则不变；没有指标保持空列表，不补零或推断需求强度。

## 开始创作

仓库示例全部标为synthetic，不代表真实设备、作者经历或成果：

```bash
uv sync --locked
uv run rednotebook schema content-brief
uv run rednotebook --db data/campaign-demo.sqlite campaign create --file examples/campaign.synthetic.json
```

记录返回的id、version和content_hash。create只生成可编辑框架；生成成稿需显式调用本地模型：

```bash
uv run rednotebook --db <数据库> campaign generate <id> --version <n>
uv run rednotebook --db <数据库> bundle <id> --version <返回版本>
uv run rednotebook --db <数据库> campaign check <id> --version <返回版本>
```

默认预算沿用60次请求、4,000,000输入预算、524,288总输出上限，每次输出受本地配置约束。生成和审阅共享账本，无云端回退。可用`--budget budget.json`覆盖选定字段；默认不是要求一定耗尽预算。

修订和审阅：

```bash
uv run rednotebook --db <数据库> campaign revise-brief <id> --version <n> --file content-brief.json
uv run rednotebook schema campaign-output
uv run rednotebook --db <数据库> campaign revise-output <id> --version <n> --file campaign-output.json
uv run rednotebook --db <数据库> campaign assess <id> --version <n>
uv run rednotebook --db <数据库> preview <id> --version <n>
```

只改正文也可沿用revise命令，程序按bundle种类选择严格契约。减小max_pages不会静默删页，需自行修订至范围内。原图使用原asset add命令快照保存；作者素材仅以元数据进入文字模型，不能声称模型已看过图片。

## 确认和导出

作者实际核对事实、支持材料、权利和当前模型疑问后，准备确认JSON：

```json
{
  "reviewer": "实际审阅者",
  "note": "具体复核依据及对疑问的处理说明",
  "facts_and_rights_checked": true,
  "resolved_issue_ids": []
}
```

resolved_issue_ids需覆盖当前所有needs_review问题。删改或降级表达应先保存新稿，再针对该版本确认。没有运行模型审阅也仍需作者核对，程序不把确认称为独立研究评估。

```bash
uv run rednotebook --db <数据库> campaign confirm <id> --version <n> --hash <hash> --file confirmation.json
uv run rednotebook --db <数据库> review <id> --version <新版本> --hash <新hash> --reviewer <审阅者>
uv run rednotebook --db <数据库> export <id> --version <新版本>
```

本轮开发没有批准或发布任何真实内容。正式导出包含可变数量的1080×1620 PNG、正文、来源和manifest；预览有水印。旧研究摘要的六页批准与导出行为保持兼容。

## MCP与Skill

新增create_campaign、revise_campaign_brief、revise_campaign_output、generate_campaign、check_campaign及content-brief schema资源。复用read_draft、list_history、revise_draft、preview_draft和export_draft。generate_campaign支持assess_only，仅做当前稿语义审阅。

新模型调用通过原model_lock串行，并保留草稿检查点；它是等待完成的MCP调用，不是新增耐久任务队列。客户端需给模型足够等待时间；中断后用历史查询读取已保存版本。外层Agent的对话不会自动传给本地模型，先保存Brief。

配套[Campaign Skill](../skills/rednotebook-campaign/SKILL.md)保存在仓库，现有[Research Skill](../skills/rednotebook-research/SKILL.md)保持独立。重新启动使用本仓库的MCP服务后才能发现新增工具；无需恢复浏览器访问。

工具直接执行失败现在返回原生isError=true与安全错误元数据。成功读取failed任务仍为isError=false。read_diagnostic只返回代码、已知工具名与时间，不保存输入、路径、签名URL或异常原文。诊断记录在读写时清除30天前记录；离线无进程运行时不会后台定时删除。

## SQLite迁移与保留/撤销审查

首次打开旧数据库时执行7→8事务迁移：允许bundles.run_id为空，并新增bundle_sources、bundle_runs、diagnostic_events。迁移关闭外键执行表重建，提交前执行foreign_key_check，再恢复外键；失败回滚。旧payload字节、哈希、版本、批准及导出登记不重写。

每版本的来源与研究依赖单独保存。修改Brief移除研究列表不会解除旧版本链的来源依赖，防止遗留文案或素材脱离清理。来源撤销/到期清除依赖草稿的上下文、策划、文案、素材、审阅、批准信息、相关Outcome和受管导出；读取和生成每次请求仍检查权限。

无外部来源的作者原创默认随本地项目保留。可用以下命令显式清除整个草稿版本链、素材、确认信息、Outcome和受管导出，仅保留不含正文的版本墓碑：

```bash
uv run rednotebook --db <数据库> campaign delete <id> --version <最新版本> --hash <最新hash>
```

作者主动复制到新Brief的外部材料仍必须声明对应source_ids；文本系统无法自动识别隐藏来源。输入文件、手工另存内容、备份和模型服务端请求留存不属于数据库清理范围。Outcome新增字段为向后兼容可选字段，旧JSON不改写，沿用来源撤销策略。

## 验证与尚未完成的验收

工程回归覆盖无研究创作、1/2/5/7/20页渲染、精确版本批准、无CTA/基线、独立事实状态、上下文实际传递、未知依赖拒绝、模型预算与审阅失败恢复、来源撤销/到期、生成中途撤销、旧批准/导出/Outcome迁移、MCP错误标志、纯反馈复盘与本地删除。

[20个合成代表任务](../examples/campaign-evaluation.synthetic.json)附人工审阅标准，可先验证输入，再明确选择任务运行本地模型：

```bash
uv run python scripts/evaluate_campaign.py
uv run python scripts/evaluate_campaign.py --run-model --ids C01-multi-project C02-tutorial --out private/campaign-eval-new
```

评估脚本输出始终标注author_evaluation=pending；不能用模型自评替代作者盲评。源码测试不证明文章好看、事实可靠或传播有效。真实作者任务、旧/新稿盲评、审改用时、长期表现仍待采集。


### 本轮实际验证记录

- `uv sync --locked`、完整pytest、ruff检查/格式、check_scaffold和verify_foundation均已执行；最终结果见下表。
- Campaign Skill通过skill-creator的quick_validate；20个合成评估任务全部通过输入校验。
- 本地qwen3.8-27b完成C01多项目开场、C02教程的生成与语义审阅。首轮因plan/post被接口再次JSON编码失败，修复严格解码兼容后两例均完成；后续精简了把通过项重复标为needs_review的审阅提示。
- 最近两例分别生成4页，实际消耗2次/3次模型请求。曾对4页开场和7页教程渲染预览，并目视检查开场第一页与教程第四页，文字可读、无裁切、水印可见。没有人工批准或正式导出真实内容。
- 模型质量仍有已知问题：可能把“未提供成果材料”扩写为“没有成果”，或将建议用途写得像既定功能；辅助模型审阅可能漏报。已针对这类未知/否定混淆补充提示，但不据此宣称消除问题。所有生成稿仍需独立作者确认。

实跑材料和报告仅保存在Git忽略的`private/campaign-eval-20260917*`，均为synthetic；其中报告带case/上下文/提示哈希及消耗账本，可区分各次修订，不属于真实笔记验收。20个任务并未全部运行模型，作者盲评和质量基线仍为pending。

| 检查 | 本轮结果 | 边界 |
|---|---|---|
| uv sync --locked | 通过，项目版本0.7.0 | 未更换锁定的第三方依赖版本 |
| uv run pytest -q | 151 passed，12.90秒 | 离线临时数据库/合成材料，包含工作区既有浏览器回归 |
| uv run ruff check . | 通过 | 静态检查 |
| uv run ruff format --check . | 88文件格式通过 | 不等于行为验收 |
| uv run python scripts/check_scaffold.py | 通过，53个本地链接 | 使用临时数据库，不迁移真实研究库 |
| uv run python scripts/verify_foundation.py | 通过，0.616秒，峰值123.9MB | 700条合成证据，重复导入新增0，撤销清理700 |
| Skill quick_validate | 通过 | 检查Skill结构，不代表独立行为盲评 |
| evaluate_campaign默认模式 | 20个输入契约通过 | 未调用模型 |
| qwen3.8-27b本地实跑 | 2个代表任务完成生成和审阅 | 合成冒烟；作者评价pending，未自动批准 |

最终基础验证时间为2026-09-17 19:26:46 UTC，源码哈希`36ff1d9c78718b143c8cfc633745b4463b63a5069ba5a1cf2a80d5f4e7b643b3`，锁文件哈希`964f6975eae2c023e46a2b540866f8e3abf90d5c0394352f58cf3ec37728560a`。工作区同时存在浏览器方向的既有修改，本记录不将其全部归为本次策划实现。
