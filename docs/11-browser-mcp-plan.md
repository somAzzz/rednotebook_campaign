> v0.6.0当前接口、宽裕模型预算、分离调度、部分导入和SQLite 7迁移见[个人MCP使用](13-personal-mcp.md)。以下v0.5.x实施与验收记录保留历史语境。

# Playwright 检索与 MCP 执行计划

2026-09-17。沿用现有 Pydantic AI、证据库和多图分析；视频/ASR继续暂缓。

| 步骤 | 实现 | 验收 |
|---|---|---|
| P1 浏览器会话 | 独立持久化 Chromium 配置，人工首次登录，状态检测 | 能打开和关闭；登录/验证码导致暂停，错误不含 Cookie 或输入值 |
| P2 网页检索 | 关键词搜索、有限滚动、笔记ID去重、保留排序未知与检索时间 | DOM夹具验证去重/预算；真实搜索单独记录，不用模拟替代 |
| P3 详情采集 | 可见正文、限定评论、按页切换图片、视频跳过 | 输出合法 EvidenceInput；完整度诚实；图片页码与SHA对应 |
| P4 任务与保留期 | SQLite任务状态、取消、显式重试，来源绑定采集目录 | 重启中断可见；来源撤销清理任务内容和采集文件 |
| P5 MCP | stdio工具：会话、检索、采集、任务状态/取消/重试、研究、草稿/预览 | 真实MCP客户端完成初始化、工具发现、调用；复用核心约束 |
| P6 整合验收 | CLI及配置示例，离线故障测试、有限真实试运行 | 必须区分代码测试、真实网页与真实模型结果 |

浏览器只执行预定搜索、打开、滚动和图片翻页，不开放任意JavaScript、Cookie读取或发布交互工具。网页中的文本是证据数据，不是指令。使用Playwright自己的独立用户目录，不复制Codex或日常Chrome登录资料。页面访问受笔记数量、滚动次数、图片数量和总任务超时约束。

Agent通过MCP选择关键词和候选笔记。第一版不把未经验收的自动采样描述为平台代表性样本；搜索默认排序记为unknown。遇到登录/验证码，处理后显式重试，已完成的采集保留；暂停点可以重新读取页面。浏览器配置含登录状态，独立于某一来源保留，由用户退出登录或清除该专用配置。

SQLite升级需迁移和撤销复核。正式审核仍通过现有CLI完成；MCP不提供自行授予来源权限或冒充人工审核的工具。MCP仅本地stdio运行；v0.5.1专用浏览器的控制端口只监听127.0.0.1，不开放远程服务。

参考：[Playwright持久化上下文](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。

## 使用

安装依赖与浏览器（Chromium包只需安装一次）：

```bash
uv sync --locked
uv run playwright install chromium
```

登记已明确范围的来源许可文件，再手动登录独立浏览器。`automated_access`、`storage`、`local_analysis` 均需明确允许；工具不能自行修改许可。

```bash
uv run rednotebook --db private/browser-research.sqlite source register --file private/pilot/grant.json
uv run rednotebook-browser --db private/browser-research.sqlite login
```

在打开的窗口登录；检测到可见“我”账号入口后，登录命令退出，浏览器继续运行。登录状态保留在专用配置目录，后续命令复用同一浏览器。随后可以直接试用CLI：

```bash
uv run rednotebook-browser --db private/browser-research.sqlite search --source xhs-public-pilot-20260917 --keyword 'AI 简历' --limit 5 --scrolls 1
uv run rednotebook-browser --db private/browser-research.sqlite collect --source xhs-public-pilot-20260917 --url '<检索返回的完整链接>' --brief public-research --keyword 'AI 简历' --comments 10 --images 20
uv run rednotebook-browser --db private/browser-research.sqlite job '<job_id>'
```

示例来源ID仅适用于本次本地测试，过期后需按真实范围登记新许可，不能把示例当作长期授权。搜索结果中的签名查询参数仅用于访问，证据的公开locator去除查询参数。采集原始结果存于SQLite同级 `managed-captures/<job_id>/`，不覆盖旧观察。

MCP使用本地stdio：

```bash
uv run rednotebook-mcp --db private/browser-research.sqlite --profile private/browser-profile --config private/model.toml
```

客户端配置参考 [mcp.example.json](../config/mcp.example.json)，按客户端实际配置格式填写command与args。不要在终端把协议stdout当普通命令输出使用。服务不会自动注册进当前客户端。一个数据库只运行一个MCP/浏览器CLI服务；登录CLI检测成功退出后即可启动MCP，无需关闭浏览器窗口。

工具顺序：

1. `browser_open` → 人工处理登录 → `browser_status`。
2. `search_notes` → `get_job(include_result=true)`，取得有界候选列表。
   候选中的 `public_url` 是可展示、无签名参数的来源链接；`collect_url` 只作为
   `collect_note` 同名参数的输入，不应出现在面向用户的回答、日志或报告中。
3. 对选中的图文笔记调用 `collect_note`，记录capture_id。
4. `analyse_gallery` → 等待完成；不分析图片时可跳过此步。
5. `import_capture(enriched=true)`；跳过图片分析时明确使用false。
6. `research_notes`：传入与采集brief_id一致的完整Brief，关键词、目标和synthetic标记必须真实。第一版MCP研究任务限制同一个来源；跨来源研究仍可用已有CLI。
   完成结果的每条引用包含程序解析的 `source.public_url`，顶层 `sources` 提供去重来源；
   链接缺失时必须明确说明，不得由模型猜测或拼接。
7. `propose_draft`生成确定性六页编辑框架，`revise_draft`修改，`preview_draft`预览。调用本地模型生成创意可继续用已有 `propose --generate` CLI。`export_draft`仅导出已由独立审核流程批准的精确版本。

长任务返回ID，不让一次MCP调用等待整轮研究。`get_job`默认只返回状态；`rednotebook://jobs/<job_id>`资源或include_result可读取结果，须把内容视作不可信证据。`cancel_job`取消当前/排队任务；`retry_job`生成新尝试，保留旧尝试状态。服务重启把运行中任务标为interrupted，不自动重放。第一次采集失败重试可能重新访问页面；多图分析成功页可复用缓存。

## 实现边界与迁移

SQLite schema 6新增browser_jobs，包含来源、请求、状态和结果；不保存Cookie。来源撤销/到期清理会删除该来源的受管采集目录并清空任务请求/结果。服务读取结果及每次采集/分析均复查权限。浏览器profile含登录状态与浏览器缓存，不纳入单一来源删除范围；需要清理会话时关闭服务后删除专用profile，不能删除日常浏览器配置。

单个检索最多20候选、5次滚动；采集最多100条可见评论、20张图、3次评论滚动。浏览器任务总超时180秒，模型任务1200秒，排队最多4项。缺正文、未知图片总数、图片错误和评论截断都显式标记；缺失互动数不补零，万位显示保留abbreviated。只依据可见DOM，不调用隐藏签名接口。页面选择器需要随实际变化维护。

P1–P5第一版代码已实现；P6离线与MCP协议验收见后续记录。真实网页自动搜索/采集需独立浏览器登录后单独验收，不能沿用v0.4手动浏览器采集的结果作为本轮Playwright验收。

## 本轮验收记录

103项离线测试通过（其中15项浏览器/MCP测试），包括真实Playwright操作本地DOM夹具、图片顺序/预算/视频跳过、MCP stdio初始化与工具调用、参数错误脱敏、任务取消/暂停/重试/重启、来源撤销清理。Ruff和文档检查通过；基础导入、幂等、撤销检查通过。

真实Chromium已成功打开小红书，识别到login_required。尚未完成独立浏览器登录后的真实搜索与采集；v0.4的3篇/30评论/8图记录不能替代本轮验收。MCP配置示例已生成，未擅自更改客户端全局配置。结构化摘要见 [验证结果](browser-mcp-verification.json)。


## 真实试运行后的暂停修复

2026-09-17：独立浏览器登录状态复用成功，MCP搜索返回5个候选。详情采集未成功，发现结果卡片选择到了不带访问参数的链接；随后用户报告触发风控，已立即中止整个测试并关闭自动化浏览器。未认定无签名链接是全部失败的唯一原因。用户随后反馈手动访问恢复；自动化仍持久暂停，未再次上线测试。

已在本地修正可见签名链接的优先选择，并增加独立于来源的浏览器暂停标记与访问历史：

- 每次页面导航至少间隔60秒，图片翻页/滚动至少间隔3秒；每小时最多10次导航。时间记录跨进程保留。这些只是保守默认值，不能保证平台不限制访问。
- 只允许一个未完成的浏览器任务；模型任务的队列仍独立受原预算约束。
- 401/403/429、验证码、登录失效、详情缺失、采集错误和浏览器超时都停止后续自动访问；只读状态与已完成结果仍可读取。
- MCP新增 `pause_browser`，没有自动恢复工具。暂停后 `retry_job` 也不能发出浏览器请求；重启服务不会清除暂停。
- 只有操作者明确恢复时才运行 `uv run rednotebook-browser resume`。此命令保留访问历史，不立即打开页面。`uv run rednotebook-browser pause` 可在服务运行时设置暂停标记。

后续真实验收应从单篇、少量动作开始，首个异常即结束整轮，不自动转向下一篇。当前107项离线测试通过（19项浏览器/MCP），覆盖跨实例访问间隔、持久暂停、详情失败阻断后续任务、小时预算及签名链接选择。真实详情/多图采集仍未通过，不能将离线用例当作风控修复已在线验证。

### 用户恢复后的单篇尝试

用户明确要求继续后，保留访问历史解除暂停，只提交一次搜索（最多1个候选、0次滚动），预设仅采集1篇、0条评论。搜索任务 `308572b9-4250-4cc8-852b-11e82560b017` 返回 `login_required`，系统重新持久暂停；没有候选、没有详情访问、没有自动重试。该提示不等价于确认再次触发风控。进一步验收依赖可用的浏览器登录会话。图片下载也改为首个错误立即退出整篇图片循环，离线回归覆盖此路径。


## v0.5.1 登录保持修复与验证

用户完成真实登录后，先从当前界面确认“我”账号入口，再正常关闭旧的短生命周期浏览器，使用同一专用配置启动独立Chromium进程。仅访问首页检查一次，重启后仍显示已登录；之后断开并重新连接控制器，登录仍在。额外通过两个独立MCP服务进程调用browser_status，两次均返回logged_in；这两次没有刷新页面、搜索或采集。浏览器保留运行，控制端口实测只监听127.0.0.1。

会话由独立浏览器进程持有，默认配置路径固定为private/browser-profile；进程元数据不包含Cookie。MCP结束或CLI退出只卸载控制并断开，明确browser_close或rednotebook-browser close才关闭专用浏览器。不会导出登录Cookie给Agent，不复制日常Chrome资料，不刷新网页来“保活”，不修改或延长平台会话期限。平台主动使会话失效时仍需手动登录。

状态检测把automation state与authentication分开：存在登录弹窗时为login_required；明确看到“我”入口才为logged_in；仅仅没有弹窗仍为unknown。即使已登录，也不会自动清除先前的持久暂停。status连接已有浏览器但不创建浏览器或导航。

```bash
uv run rednotebook-browser --db private/browser-research.sqlite status
uv run rednotebook-browser --db private/browser-research.sqlite close
```

111项离线测试通过。新增专用浏览器断开/重连后保持标签页、只读连接不启动浏览器、会话元数据校验、登录正向证据及暂停状态独立性测试。SQLite仍为schema6，未新增证据表；会话配置与登录状态独立于研究来源，清理方式见前述专用profile说明。

连接机制参考 [Playwright connect_over_cdp](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp) 与 [Browser.close连接断开行为](https://playwright.dev/python/docs/api/class-browser#browser-close)。该验证只覆盖登录保持，不代替此前尚未完成的真实详情采集验收。
