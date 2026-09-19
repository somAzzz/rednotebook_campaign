# 浏览器恢复卡顿核查与修复

日期：2026-09-18。针对recover-ui、attach和视频保护注入的等待问题。

## 核查结论

确认：open缺少整体期限；逐标签页evaluate没有独立外层期限；旧HTTP探活只验证浏览器端点身份，不能证明renderer响应；清理阶段也可能等待；recover-ui的局部导航超时没有覆盖所有DOM调用。

纠正：page.evaluate不能依靠set_default_timeout获得通用的30秒执行上限。因此不能把各局部超时相加，就断言正常情况“不可能超过30秒”。asyncio截止时间是协作式超时，主机睡眠或事件循环不运行期间不能保证墙钟响应时间。

用户提供的pmset时间线与系统挂起解释相符，但本次未独立取得当时的完整命令起止、历史电源日志和传输记录，不能认定睡眠是唯一根因或排除代码问题。唤醒后CDP正常也不能证明历史renderer始终正常。

当前仓库及本机已安装研究Skill均未发现`reference/browser-research.md`引用；已有`references/topic-workflow.md`存在。浏览器生命周期说明确实缺失，已补充并链接。本次没有复现一个当前不存在的断链。

MCP当前不暴露recover-ui；它属于独立CLI。CLI返回结构化错误但不自动写MCP诊断表，所以空诊断表不能否定失败。

## 修复

- open整体60秒；Playwright驱动启动也在期限内。
- 选择目标页后用真实evaluate探活并安装禁播保护，共5秒。返回browser_renderer_unresponsive。
- 其他已有标签页并行安装禁播保护，每页5秒；失败返回browser_video_guard_timeout，不跳过保护后继续执行。
- state整体15秒；dismiss_note_overlay的DOM检查、返回上一页和隐藏等待整体20秒。可选pacing独立于恢复期限；CLI恢复跳过pacing。
- CLI status/close/recover-ui整体90秒，包括attach及后续动作。错误清理阶段有独立有界等待，因此不承诺绝对90秒墙钟上限。
- disconnect和driver stop分别最多等待5秒，不关闭用户专用浏览器进程、不清除登录资料、不解除访问暂停。
- 成功和失败路径均检查墙钟与monotonic的耗时差。绝对差超过10秒返回system_suspend_or_clock_change_suspected；外部取消仍保持取消语义。

时钟差仅为启发式信号：调时也会触发；某些平台monotonic包含睡眠时长，则可能无法识别睡眠。这个错误码不表示已精确确定睡眠时间。超时只在主机及事件循环能运行时生效，没有引入会杀死浏览器或绕过访问限制的外部看门狗。

## 验证

离线测试覆盖事件循环可运行时的无限等待截止、外部取消、成功/异常路径的模拟时钟跳变、open超时清理、选中页renderer无响应、其他标签页无响应及恢复时DOM查询无响应。现有本地Chromium恢复、禁播和专用会话断开后保留测试继续执行。

不操作真实账号，不让真实机器睡眠，不声称复现了报告中的历史13分钟事件。Skill同步至本机安装目录，并补充[生命周期说明](../skills/rednotebook-research/references/browser-lifecycle.md)。长时间任务可以由操作者自行用caffeinate抑制空闲睡眠，本次不修改系统电源设置。

本轮验证：`uv sync --locked`成功；193项离线测试通过（14.85秒）；ruff检查与100文件格式检查通过；55个文档链接检查通过；基础验证0.724秒、峰值124.6MB、700条合成记录撤销清理通过。研究Skill验证通过。
