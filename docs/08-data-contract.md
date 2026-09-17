# v0.2 数据契约与操作语义

契约唯一代码来源为 `src/rednotebook/domain/models.py`；可用 `rednotebook schema brief|grant|evidence` 输出 JSON Schema。当前 schema_version 属于初版契约；未知字段拒绝，字段名不会默默忽略。

## 许可与产品事实

SourceGrant 必须有 id、source_name、basis_ref、synthetic、带时区的 valid_from/valid_until，以及 permissions。存储必须显式 allowed；读取/计算还需 local_analysis=allowed。云处理、图片复用、摘录输出与自动访问是独立字段，默认 unknown，当前都不调用。

来源的处理有效期取 `valid_until` 和“首次登记时间 + retention_days”的较早值，默认 30 天；重复导入不会延长保留期。这是保守的来源级保留策略。许可记录一旦登记不可被 import 悄悄改写；新许可评估使用新的 source_id，撤销来源不会被同 ID 的文件重新启用。本地操作者是授权信息的信任主体，不具备多用户服务端身份系统。

Brief 的 audience、research_question、objective 和 keywords 不能为空。产品事实 verified 必须有 product_version、verified_at 及绑定素材 ID；素材必须声明所有者、许可依据、路径、SHA-256。`brief validate` 校验素材存在和哈希，无法判断录屏是否真的证明功能，后者仍需人工审核。相对素材路径以 brief 文件目录为基准。

v0.3 在研究开始时持久化 Brief 的不可变版本，research_runs 以外键绑定确切版本。导入行中的 brief_id 仍是采样标签；研究时按同 ID 取资料并强制检查合成标记一致。

## JSON 与 CSV

JSON 顶层是 EvidenceInput 数组。CSV 顶层表头与 EvidenceInput 一致，metrics、sampling、coverage 使用 JSON 单元格；synthetic/is_reply/is_author_reply 只接受小写 `true`/`false`，followers 为非负整数。空单元格表示字段省略，不能用字符串 `null` 代替实际空值。支持 UTF-8 BOM。单文件上限 20MiB、5,000 行；这些是导入防护上限，不代表研究 Agent 预算已实现。

必要字段：source_id、kind、text、locator、observed_at、synthetic、至少一条 sampling。comment 还需 parent_external_id，指向同来源根笔记。导入器先处理 note，再处理 comment；父笔记不存在时拒绝该评论。弱身份笔记无稳定 external_id，因此当前不能作为评论关联目标。

所有时间必须带时区，不接受无时区日期或 Unix 数字时间。存储转 UTC；不允许未来观察时间或观察早于发布时间。

Sampling 包含 run_id、brief_id、keyword、sort、group 和 truncated。当前将采样信息随观察记录保存。覆盖统计只代表已导入的评论，只有热评或发生截断时明确标记；同一评论跨多个采样词只计一次。每个 run_id 只描述一个固定的采样条件；实现会拒绝同来源内复用 run_id 却改变 brief/keyword/排序/群体，也会拒绝同观察时刻悄悄更改截断标记。

## 身份与历史

稳定身份优先 `(source_id, kind, external_id)`，映射成 SHA-256 证据 ID。缺 external_id 时用 locator 去首尾空格 + 正文构成弱身份并标记；弱身份正文变化会形成新证据，不保证跨 URL 变体去重。作者 ID 以数据库内随机盐 HMAC 伪名化，不保存原始 author_id；作者昵称若包含在原文中不会自动识别脱敏。

不同正文/标题/分组元数据生成不可变修订；相同修订内容重用原 revision。每个观察时间绑定其修订，当前记录按最新观察时间选择，不能被后来补导的旧资料覆盖。

相同来源、证据、指标名和观察时刻必须有完全一致的指标载荷，否则报 `metric_snapshot_conflict` 并回滚该行。因此不允许在同一次观察中将不同口径当作可混用的同名指标。新时间的快照保留历史。正文或覆盖元数据的同时间冲突也拒绝。

## 指标

Metric 的 name 支持 likes/saves/comments/shares/views/impressions/clicks。value 为非负有限数值或 null；null 必须有 missing_reason。exact 数量必须为整数，数值范围不超过 2^53-1，避免浮点整数失真。raw_display 纯数字时与 exact value 必须一致。

`1.2万` 可由导入器转换为 12000，但 precision 必须是 abbreviated；仍保存原字符串。未支持自动解析 `1万+`、`1.2k` 等表示，适配器应先明确口径和近似数值，不能猜成精确数。

排名使用每篇最新一次观察的三项完整指标，不用旧快照补最新缺失值。按来源、合成标记、话题、形式、发布龄、账号规模、采样群体及三个指标各自的来源/精度/口径分组；默认 n≥20。输出同时包含分组键、组内数量、分位、权重与算法版本。分享单列，不改变三指标权重。缺少 followers 表示 unknown，不能当作小账号；author_id 未知与 followers 未知是不同问题。

快照差值仅限同证据、同指标、同源、同定义且时间递增；负增长保留并标异常；近似输入输出 approximate。当前不生成 CTR 或其他业务比率；`safe_ratio` 仅是内部数值函数，不构成业务归因。

## 异常和删除

每个输入行设 SQLite savepoint；坏行不影响其他有效行，最终为 partial 或 failed。磁盘/SQLite 异常和可捕获中断使本批所有证据变更回滚，job 记录 failed。可用原文件安全重跑。进程被强制终止时 SQLite 回滚，job 可能仍显示 importing，此时需要操作者确认进程已停止后重跑；暂不自动认领并发作业。

撤销/过期清理删除数据库内原文、修订、采样与快照，以 tombstone 标记失效引用。SQLite 开启 foreign_keys 和 secure_delete，未启用 WAL。只保留不含原文的作业摘要/错误字段、来源记录与审计。输入原文件、终端记录、用户保存的报告和外部备份由操作者另行管理；当前不会假装已经清除这些外部副本。

JSON 输出是本地诊断结果，不是已审核的发布内容。v0.3 已为研究报告建立来源依赖：来源撤销/到期清理时清空数据库内报告，读取时也重新检查许可。图文包及模型服务端留存不在当前本地管理范围内，A16 的未来对象仍需补全。

## v0.4 多图与交付契约补充（2026-09-17）

数据库已迁移至 schema 5，新增不可变创作版本、审核哈希、受管理导出清单、复盘观察和来源绑定的图片缓存。来源撤销同时清理关联缓存和受管理导出；外部手工副本不属于自动清理范围。

笔记 `format` 支持 `text`、`image_text`、`video`；视频处理本轮暂缓。新增 `media_text` 与必需的 `media_provenance`，机器识别始终标记 `human_verified=false`。图片清单以来源ID、笔记ID、总页数、唯一页码、相对路径和SHA-256标识图片；结果保留页码、覆盖率和缺失页，不将部分完成视作整篇完成。相同图片在不同页码仍保留位置，只复用分析结果。

完整命令和边界见 [README](../README.md) 与 [真实验收记录](10-public-note-pilot.md)。
