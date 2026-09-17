# RedNotebook

本地证据研究与创作助手，当前 v0.6.0。已实现文字/多图证据导入、本地模型分析、带引用研究、创意分镜、版本审核、六页图片导出及复盘输入。

已用小红书真实公开笔记测试：3篇笔记、30条评论，其中一篇8张文字图片全部逐页分析。当前是小样本功能验收，尚未通过大样本研究准确率与长期效果验证。项目不依赖CanDo；视频与ASR按用户要求暂缓。

个人使用新增历史查询、单篇整合研究、阶段进度、失败材料恢复和用户逐条反馈。默认本地研究预算已放宽；使用与迁移说明见 [个人MCP使用](docs/13-personal-mcp.md)。本轮125项离线测试通过，不代表新增真实网页验收。

## 安装与检查

需要uv和Python 3.12：

```bash
uv sync --locked
uv run rednotebook doctor
uv run rednotebook status
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_scaffold.py
uv run python scripts/verify_foundation.py
```

## 导入与研究

```bash
uv run rednotebook fixtures --out data/demo
uv run rednotebook --db data/demo.sqlite import --file data/demo/evidence.json --grant data/demo/grant.json
uv run rednotebook --db data/demo.sqlite quality --brief demo-cando-001
uv run rednotebook --db data/demo.sqlite metrics rank --brief demo-cando-001
uv run rednotebook model probe
uv run rednotebook --db data/demo.sqlite analyse --brief examples/brief.cando.synthetic.json
```

合成夹具保留历史CanDo示例名，不代表当前项目必须推广该产品。F0故意包含3条坏行，导入返回partial及退出码2是预期。可自行提供通用Brief，facts与assets可以为空。来源许可未明确允许对应处理时，拒绝执行。

`--db` 放在子命令前，默认为 `data/rednotebook.sqlite`。配置在Git忽略的 `private/model.toml`；新环境参考 `config/model.example.toml`。只有显式研究/视觉/创意命令调用本地端点，无云端回退。数据库带权限、修订与预算校验；研究工具只开放获取笔记、获取评论样本与比较已计算指标。

```bash
uv run rednotebook --db data/demo.sqlite research show <run_id> --format markdown
uv run rednotebook schema evidence
uv run rednotebook evidence inspect <evidence_id> --revision 1
uv run rednotebook source sweep
```

研究结果为hypothesis，引用位置匹配不等于结论被语义支持。小样本、截断评论、未知指标保留原状态，不补零或编造CTR。同口径完整分组不足20条时不输出排序分数。

## 单图与整篇多图

```bash
uv run rednotebook --db <数据库> media-analyse --source <来源ID> --file <本地图片> --kind image
uv run rednotebook --db <数据库> gallery-analyse --manifest <图片清单.json>
```

图片清单示例：

```json
{
  "source_id": "source-001",
  "note_external_id": "note-001",
  "declared_total": 2,
  "images": [
    {"position": 1, "path": "01.webp", "sha256": "填入64位SHA-256"},
    {"position": 2, "path": "02.webp", "sha256": "填入64位SHA-256"}
  ]
}
```

路径相对清单目录，越界、缺文件和哈希错误均拒绝相应页面。默认最多20张，输出完整页码与缺页列表。成功图片按来源/哈希/模型端点/配置/提示缓存，重复页保留位置但可复用结果；失败页可重试。图片只送往已配置的本地模型，识别结果始终标记待核验。

在首次导入观察之前，可合并机器提取结果：

```bash
uv run rednotebook enrich --file evidence.json --gallery gallery-result.json --out evidence-enriched.json
uv run rednotebook --db <数据库> import --file evidence-enriched.json --grant grant.json
uv run rednotebook --db <数据库> analyse --brief brief.json --max-chars 10000
```

`enrich` 写新文件，不覆盖旧文件；必须唯一匹配来源与笔记ID。`media_text` 作为独立机器来源字段引用，不冒充作者原话。长图可以使用更高的 `--max-chars`（上限50000），超出仍明确截断。

## 创意、修订、审核与导出

```bash
uv run rednotebook --db <数据库> propose --research <run_id> --generate
uv run rednotebook --db <数据库> bundle <bundle_id> --version 3
uv run rednotebook --db <数据库> revise <bundle_id> --version 3 --file editorial.json
uv run rednotebook --db <数据库> preview <bundle_id> --version 4
uv run rednotebook --db <数据库> review <bundle_id> --version 4 --hash <content_hash> --reviewer <审阅者>
uv run rednotebook --db <数据库> export <bundle_id> --version 4
```

不加 `--generate` 时生成确定性的编辑框架；加此参数才调用本地模型。输出最多3个研究方向和一套6页分镜，均需人工核对。`editorial.json` 包含title、body、6项pages（各有title/body，可选asset_id）及disclosure；可从bundle的payload.editorial取出修改。

预览带未批准水印。正式导出必须通过确切版本与哈希审核；修改正文或素材生成新draft，不继承批准。六张PNG均为1080×1620，附正文、来源说明和manifest。渲染采用锁定Pillow及系统中文字体；没有字体或文字溢出时失败，不静默裁字。

自有素材使用 `asset add <bundle_id> --version … --file … --owner … --rights-ref …`，生成新版本并返回素材哈希；在分镜asset_id中引用该哈希。素材字节固定入库，后续修改源文件不影响版本，原图不裁切。不要把第三方公开图片声明为自有素材。

受管导出位于数据库同级 `managed-exports/`，同版本重试复用文件并校验manifest/文件哈希。来源撤销/到期会清空依赖的研究和创作内容、媒体缓存、相关outcome及受管导出。输入文件、手工保存报告、外部备份和模型服务器请求留存不在该清理范围内。

## Playwright 检索与 MCP

已增加独立持久化浏览器、关键词检索、图文采集、来源绑定任务与本地stdio MCP。先运行 `uv run playwright install chromium`，再通过 `rednotebook-browser login` 手动登录。

登录检测完成后浏览器保持打开，MCP或CLI退出只断开控制连接。`rednotebook-browser status` 只读取当前页面登录状态，`rednotebook-browser close` 才显式关闭专用浏览器。

完整步骤、工具顺序和限制见 [Playwright/MCP计划与使用](docs/11-browser-mcp-plan.md)，客户端配置见 [MCP配置示例](config/mcp.example.json)。真实搜索已返回候选；最近详情打开成功，但整篇多图采集未完成，分页错误后持久暂停。登录保持已验证；后续分页修复尚未重跑线上采集。异常后恢复需明确操作者命令，不能自动重试。与此前手动浏览器小样本测试分别记录。离线测试中的浏览器DOM用例需要已安装Chromium，不访问外网。

## 复盘与只读接入

```bash
uv run rednotebook --db <数据库> outcome import --file outcome.json
uv run rednotebook --db <数据库> retrospective <bundle_id> --version 4
uv run rednotebook --db <数据库> evaluate --file labels.json
uv run rednotebook --db <数据库> adapter fetch --source <来源ID> --path /explore/<24位笔记ID>
```

Outcome保留真实发布时间、观察时间及24h/72h/7d容差窗口，区分公开与自有数据；迟到不改时间、缺指标不补造。`evaluate`只计算审阅者标签，不生成“人工标注”。

HTTP只读适配器目前仅取得公开元数据，明确返回partial。完整笔记和图片的真实测试使用浏览器只读研究；没有稳定批量官方接口、自动发布或账号交互功能。401/403暂停、429有界重试、验证码停止，不绕过平台访问限制。

ASR代码为暂缓实验模块，默认不安装其可选依赖、不下载模型、不运行视频测试。

## 验收与文档

- [当前进度](docs/07-status.md)
- [MCP调研实测](docs/12-mcp-research-test.md)
- [真实公开笔记与多图验收](docs/10-public-note-pilot.md)
- [原始分步计划](docs/04-execution.md)与[验收矩阵](docs/05-acceptance.md)
- [可行性](docs/01-feasibility.md)、[产品范围](docs/02-product.md)、[架构](docs/03-architecture.md)
- [数据契约](docs/08-data-contract.md)、[历史基础验收](docs/foundation-verification.md)、[S4历史记录](docs/09-local-research.md)

真实样本、图片、模型配置与产物位于Git忽略目录。退出码0成功、2部分完成、1结构化错误、130中断；测试使用离线临时数据库，真实模型验收另行显式运行。
