# 外部资料核验

核验日期：2026-09-16。本次检查公开文档，没有登录小红书或检验真实抓取成功率；仓库默认分支会变化，实际采用时须固定版本并保存 LICENSE 和能力测试结果。

| 原方案主张 | 本次结果 | 对项目的影响 |
|---|---|---|
| Pydantic AI 支持延期工具和人工批准，但批准不替代授权 | 官方文档明确说明 | 授权、审核与执行校验放应用服务，不能信任客户端批准标记 |
| xiaohongshu-mcp 能搜索、取详情/互动/评论，也有写工具 | 项目 README 列出这些操作 | 只作为候选；网关明确映射只读工具 |
| 评论默认只取 10 条一级评论 | README 对 get_feed_detail 明确给出 | 必须保存真实读取数量、回复和截断信息 |
| RedInk 有非商业限制 | README 写 CC BY-NC-SA 4.0，商用需另行许可 | MVP 不引入其代码，模板自行实现 |
| 生成合成内容发布标识要求及 2025-09-01 生效日 | 网信办官方文本第十条与第十四条可核验 | 导出附发布声明提示；上线时按实际场景复核 |
| 2026-03-10 小红书 AI 托管公告的具体措辞/处罚 | 本轮未找到可直接核验的官方原文 | 保留为待核实，不把具体日期与处罚写死进规则引擎 |
| 2026-01《社区公约 2.0》的具体 AI 条款 | 本轮未取得官方原文 | 首次真实发布前补官方原文或应用内规则快照 |
| 千瓜合同允许哪些存储、模型处理和再分发 | 未取得实际套餐合同 | 默认不接入，不以会员资格代替处理许可 |
| MediaCrawler 当前具体许可证与用途限制 | 本轮页面未提取到足够许可文本 | 保留待逐版本复核，不纳入商业依赖 |
| 普通开发者可任意检索全站的官方通用 API | 本轮没有证据证明其存在 | 不按“必有官方 API”做架构承诺，也不声称证明不存在 |
| CanDo 两项功能与效果已上线 | 没有访问当前产品版本验证 | 事实台账先 unverified |

## 原始来源

- [Pydantic AI Deferred Tools 官方文档](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/)：延期执行与授权边界。
- [xiaohongshu-mcp README](https://github.com/xpzouying/xiaohongshu-mcp/blob/main/README_EN.md)：能力与评论参数；项目描述不是平台授权证明。
- [RedInk 项目与许可说明](https://github.com/HisMax/RedInk)：非商业与商业许可区分。
- [MediaCrawler 项目](https://github.com/NanmiCoder/MediaCrawler)：后续许可核验入口，本轮不做许可结论。
- [网信办：人工智能生成合成内容标识办法](https://www.cac.gov.cn/2025-03/14/c_1743654684782215.htm)：第十条规定发布声明与标识，第十四条规定施行日期。

技术选择、预算、采样规模、权重、实验窗口和验收阈值为项目设计判断，不来自上述来源。平台规则与合同适用性在真实接入/发布前按当时条款确认。

## 本地实现参考（2026-09-17）

- [Pydantic 官方校验器文档](https://pydantic.dev/docs/validation/latest/concepts/validators/)：输入字段与跨字段校验的实现参考。
- [Python 3.12 sqlite3 文档](https://docs.python.org/3.12/library/sqlite3.html)：连接事务、回滚和参数绑定的实现参考。

具体安装版本以本仓库 uv.lock 为准；库本身不替代项目的许可或真实性判断。
