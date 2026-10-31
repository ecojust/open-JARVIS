# Session、run 与工具设计参考

核对日期：2026-10-31。本记录基于公开的官方仓库源码与文档，用于说明本轮设计取舍；不是功能完成或生产模型验收报告。

OpenCode 的官方文档站，以及 Claude Code / Claude 平台文档站，在本次环境返回 HTTP 403，因此以下引用使用可实际读取的官方 GitHub 固定提交。Claude Code 部分仅讨论其官方 Agent SDK 公开接口和示例，不推测未公开的产品内部实现；也不将 SDK 的行为自动推广到 Claude Code 的所有客户端。

## 可核对的设计

| 主题 | 已读取的公开证据 | 对 JARVIS 的启发 |
| --- | --- | --- |
| 会话与轮次 | Claude SDK 的 `ClaudeSDKClient` 支持同一连接反复 `query()`，维护多轮上下文；`receive_response()` 遇到 `ResultMessage` 结束本轮。[client.py][claude-client] | session 管理持续对话；run 管理一次用户输入到本轮最终回复。不能每次发送都重建 session。 |
| 恢复与新分支 | SDK `resume` 按 session ID 载入历史，`continue_conversation` 延续最近会话，`fork_session` 才在恢复时生成新会话。[types.py][claude-types] | 持久化当前 session，重启和任务结束不自动换会话；新建按钮和明确的新建语音指令负责创建新的 session。 |
| 执行与取消 | OpenCode 的 `SessionRunCoordinator` 按 key 串行执行、合并执行期间的唤醒信号；`interrupt` 只等待接受中断，`awaitIdle` 才等待无活动执行。[run-coordinator.ts][opencode-run] | 请求已接受、取消已请求、执行已结束是不同事实；保留现有队列、步骤记录和真实操作结果，不让语音重发造成重复任务。 |
| 工具统一注册 | OpenCode 注册表维护名称、说明、输入/输出 schema 和执行方法，提供 `list` 与按可用范围取得的 definitions；schema 在注册时转换。[tool.ts][opencode-tool] | 工具面板与模型收到的工具定义应来自同一个注册表，不能额外手写一份会过期的“能力宣传”。 |
| 工具扩展 | OpenCode 自定义工具文档使用 `description`、参数 schema、`execute`，执行上下文含 session/message/agent；Claude SDK 支持声明工具名称、说明、输入类型的进程内 MCP 工具。[OpenCode 文档][opencode-custom]、[SDK README][claude-readme] | 当前内置工具继续用轻量函数注册，记录所属 Agent、参数与执行范围；以后增加 MCP 可接同一目录，但本轮不必引入新常驻子进程。 |
| WebFetch | OpenCode `webfetch` 默认 Markdown，也支持 text/html；有可配置但有上限的超时、随格式变化的 Accept、逐跳重定向检查及明确的类型错误。[webfetch.ts][opencode-fetch] | 给模型结构化正文、来源和明确失败原因，让它知道何时换来源、何时继续读取，避免把网页导航当正文。 |
| 结果大小 | OpenCode 的 HTTP 读取同时检查声明长度与实际流式字节；统一工具输出层超过上限时返回预览并保存全文引用。[http-body.ts][opencode-body]、[tool-output.ts][opencode-output] | 网络读取、解析、返回模型的内容都要有界。截断时必须提供继续读取办法，不能默默丢掉后半篇材料。 |
| 上下文管理 | OpenCode 的 compaction 按模型窗口预留空间，保留最近对话片段并管理摘要；摘要与完整会话存储是不同层。[compaction.ts][opencode-compaction] | 长 session 不等于每轮发送全部历史。先实现按 session 有界读取、保留当前指令和真实产物引用；自动摘要需要另行设计和验证，不能冒充已实现。 |

术语需要按本产品定义使用：OpenCode 的 `execution/run` 在上述代码中是一次忙碌期间，可能继续处理新到输入，**不等于**用户要求的“一问一答”。JARVIS 的 run 应以一次用户提交为边界，工具调用、委派和步骤都归于该 run；同一个 session 可以包含多个 run。Claude SDK 的 `max_turns` 文档将 turn 描述为用户消息及 assistant 回复，但本产品无需复刻 SDK 的类型或存储结构。

## 本轮采用的约定

以下是实现约定，完成情况以本仓库测试与验证记录为准。

1. **会话数据**：session 保存标题、时间和当前会话身份；现有 task 作为 run 保留 `task_id` 兼容标识，同时暴露 `run_id` 与 `session_id`。旧消息与任务需要迁移，不能因改名丢失。每次补充问题的回答也是新 run，关联之前的 run，并保留其已有工具事实。
2. **上下文隔离**：模型只能读取当前 session 的既往轮次和有界产物引用，不能把其他对话或尚未执行的未来排队输入混进当前 run。文件工具仍沿用现有工作目录边界；会话历史隔离不等同于为每个 session 创建独立文件沙箱。
3. **语音生命周期**：partial transcript 仅用于输入框预览，final transcript 自动提交一次。使用 utterance ID 和代际标识去重、丢弃暂停或切换会话后的旧事件。离线 STT 的预览须限频并合并积压，最终识别优先；不能为每个音频帧启动推理。只对完整匹配的“新建对话”等短命令执行会话操作，不误判包含这几个字的普通任务。
4. **工具可发现性**：右上工具入口展示当前注册的真实工具、中文说明、Agent 归属与参数；不加载 MAF 或模型才能打开。目录展示不扩展工具执行权限。Claude SDK 的 `allowed_tools` 是权限允许列表，官方文档明确它不等于工具可见列表，这两个概念不能混用。
5. **网页读取**：保留 `fetch_page` 的兼容入口，优先输出适合模型的 Markdown 或纯文本，正确识别中文编码，去除脚本和明显导航噪声，保留有用标题、段落与来源链接；返回最终 URL、类型、截断与继续读取信息。网络失败、HTTP 错误、非文本和超限结果应让模型据实调整，不把空正文包装成成功。
6. **资源和执行边界**：继续使用单个任务服务、按需 Agent 和有界工具预算；新网页缓存需限制总容量与寿命。保留现有公网地址校验、每次跳转重新检查、凭据不跨站转发及工作目录限制。网页和工具返回的文本始终是资料，不是新的执行授权。

本轮参考这些产品的接口分层与执行事实记录，不因此改用 Claude SDK、移除 MAF、增加浏览器运行时、自动安装插件或开放任意 shell。网页需要 JavaScript、登录或验证码时，应报告读取限制并选择可用公开来源。

## 固定来源

- OpenCode 官方仓库：[`anomalyco/opencode@bf544cd419c5f17535c0d5af929f7ecd41188320`](https://github.com/anomalyco/opencode/tree/bf544cd419c5f17535c0d5af929f7ecd41188320)。上述 `packages/core` 路径为本次实际读取的版本，可能与旧文章中的 `packages/opencode` 路径不同。
- Claude 官方 Python Agent SDK：[`anthropics/claude-agent-sdk-python@2146f9d8985513053e904b13200c8f744b36785a`](https://github.com/anthropics/claude-agent-sdk-python/tree/2146f9d8985513053e904b13200c8f744b36785a)。本轮使用公开 `client.py`、`types.py`、README 及 `examples/streaming_mode.py` 核对行为。
- Claude Code 官方产品仓库：[`anthropics/claude-code@80c6959d28c4c30ecd14fc1cdf172f19d9adad81/README.md`](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/README.md)。其 README 指向官方产品文档，但不提供这里可据以证明完整 agent 内核的源码。

[claude-client]: https://github.com/anthropics/claude-agent-sdk-python/blob/2146f9d8985513053e904b13200c8f744b36785a/src/claude_agent_sdk/client.py
[claude-types]: https://github.com/anthropics/claude-agent-sdk-python/blob/2146f9d8985513053e904b13200c8f744b36785a/src/claude_agent_sdk/types.py
[claude-readme]: https://github.com/anthropics/claude-agent-sdk-python/blob/2146f9d8985513053e904b13200c8f744b36785a/README.md
[opencode-run]: https://github.com/anomalyco/opencode/blob/bf544cd419c5f17535c0d5af929f7ecd41188320/packages/core/src/session/run-coordinator.ts
[opencode-tool]: https://github.com/anomalyco/opencode/blob/bf544cd419c5f17535c0d5af929f7ecd41188320/packages/core/src/tool.ts
[opencode-custom]: https://github.com/anomalyco/opencode/blob/bf544cd419c5f17535c0d5af929f7ecd41188320/packages/web/src/content/docs/custom-tools.mdx
[opencode-fetch]: https://github.com/anomalyco/opencode/blob/bf544cd419c5f17535c0d5af929f7ecd41188320/packages/core/src/tool/plugin/webfetch.ts
[opencode-body]: https://github.com/anomalyco/opencode/blob/bf544cd419c5f17535c0d5af929f7ecd41188320/packages/core/src/tool/http-body.ts
[opencode-output]: https://github.com/anomalyco/opencode/blob/bf544cd419c5f17535c0d5af929f7ecd41188320/packages/core/src/tool-output.ts
[opencode-compaction]: https://github.com/anomalyco/opencode/blob/bf544cd419c5f17535c0d5af929f7ecd41188320/packages/core/src/session/compaction.ts
