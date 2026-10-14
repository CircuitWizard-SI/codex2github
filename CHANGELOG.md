# 更新记录

## 未发布

暂无未发布改动。下一次对外发布的版本必须高于 `0.2.2`。

## [0.2.2](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.2.2)

- 包含 0.2.1 的已有会话 Desktop 专用执行路径、断连等待和提交核对修复。
- 修复非 Windows 下将 PID 0 误判为活跃进程，导致无 worker 的提交记录无法恢复核对；非正 PID 明确表示没有 worker。
- Include Desktop-only execution for existing chats and fix recovery when a worker PID is absent. Nonpositive PIDs never identify a running worker.

## 0.2.1（源码更新，未发行 / source update, unreleased）

- 已有会话仅通过 Codex Desktop 的原会话接口和官方队列执行；删除独立 app-server 接管回退，并拒绝独立执行器恢复已有会话。
- 桌面端不可用时保留原留言等待重连，限制重试频率，同一消息只提示一次；不更改任何 Codex 或 CC Switch 全局配置。
- 提交回执不明时保留日志边界核对结果，不自动重发；worker 意外退出也能从提交记录继续核对。
- 重试前清理已确认未提交的旧等待结果，防止旧结果导致重复调度。新会话暂时保留现有创建方式。
- Existing chats execute only through Desktop. Keep unsent requests pending during disconnection; reconcile uncertain submissions without replaying them. New-chat creation remains on the existing path.

## [0.2.0](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.2.0)

- 新增可选 Windows 登录自启动及独立后台守护，默认关闭；监听不会随 Codex 窗口退出。
- 私有 Issue 的拥有者可用 `/autostart on`、`/autostart off`、`/autostart` 开启、关闭下次启动及查看状态。
- 修复 `pythonw` 后台监听仍弹出 GitHub CLI 窗口；隐藏所有项目子进程窗口。
- 修复任务完成后在关闭 app-server 时卡住，导致回执缺失和后续消息一直排队；结果与回执先保存，再清理子进程。
- stdout 读取线程未退出时不再阻塞关闭同一管道；清理异常独立记录，不丢弃已完成结果、不重跑任务。
- worker 意外退出时恢复已保存结果，最终回复通过固定标记去重。
- Persist completed results before shutdown and recover durable results without replaying tasks.

## [0.1.7](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.1.7)

- 支持仅作用于项目子进程的 Codex Desktop/unknown 请求身份，从已安装 Desktop 包读取版本并验证初始化返回值。
- Add a Desktop/unknown User Agent profile without changing global Codex or CC Switch settings.

## [0.1.6](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.1.6)

- 移除独立 worker 的 30 分钟任务总时长限制，不再因运行时间主动中止 Codex；持续等待真实完成、失败或进程退出。
- 保留连接及单次 RPC 超时，避免失去响应时无限阻塞；不改变客户端身份、缓存相关调用路径、模型或任务输入。
- Remove the 30-minute task deadline. Long-running turns wait for actual completion or failure; connection timeouts remain bounded.

## [0.1.5](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.1.5)

- 新会话取得会话 ID 后自动在原 Issue 附上“查看完整会话记录”入口，正在执行时即可关联；已有新会话会自动补齐。
- 保留用户原有正文及评论，幂等更新专用链接区块；显示首次同步等待提示，链接更新失败不阻断任务。
- Add full-history links when new chats bind, repair previously created chats, and preserve task text and comments.

## [0.1.4](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.1.4)

- 修复带 `codex-session` 标签的未绑定 Issue 被误判为只能连接旧会话，导致新任务遭拒的问题；兼容已存在的未绑定 Issue，保留模型和档位选择。
- 会话入口增加“➕ 新建会话”链接，支持先选模型／档位再发送任务；已有会话绑定及目录入口语义保持不变。
- 修复不自动重放旧消息；需要恢复的已拒绝请求须先确认没有执行记录，再单独提交。

## [0.1.3](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.1.3)

- README 改为完整简体中文／English 双语，提供语言导航，覆盖部署、指令、通知、隐私边界、限制和开发说明。
- 保持任务执行与实例配置行为不变。

## [0.1.2](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.1.2)

- 修复通知工作流读取最终评论遇到 GitHub 临时 HTTP 500 后直接退出的问题；只读请求有限重试，权限拒绝直接报告。
- 发送回执不明时核对固定提醒标记，不盲目重复发送，不重跑 Codex 任务。
- 提供 `notifications enable --update`，以原文件 SHA 安全更新审查过的项目通知工作流。

## [0.1.1](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.1.1)

- 规范发行附件的 ZIP 元数据：固定文件时间、标准文件权限，清除额外字段及注释，避免携带本地修改时间和本地提交标记。
- 新增发行包规范化工具及内容完整性／可重现检查；wheel 文件内容和 RECORD 校验保持有效。
- 撤下 `0.1.0` 的原始发行附件，保留其源码标签与发布记录。GitHub 的提交和发布时间属于公开项目记录，继续保留。

## [0.1.0](https://github.com/CircuitWizard-SI/codex2github/releases/tag/v0.1.0)

首次公开源码版本，将本机桥接器整理为可安装、可配置的通用项目。

- 可选 GitHub Actions 完成提醒：保持原任务回复，单独追加拥有者提及；忽略提交回执与引导卡，重复运行去重，通知失败不重跑任务。
- 提供通知启用、停用和带标记的测试命令；正确处理 GitHub API 成功返回的空响应。

- 独立裸源码模板与部署者私有任务仓库分离；从部署者本机自动识别会话。
- 拒绝向 GitHub 模板仓库发送任务及历史，避免将源码分发仓库误用为运行仓库。
- 打包清单排除个人配置与运行数据，并在 CI 中审计发行包。
- 完成提交追踪、完整历史合并及单会话模型／思考档位切换。

- GitHub 原会话入口与会话模型覆盖。
- 引导、排队、撤回和自动更新的送达卡。
- Windows 文件事件、历史分页与主会话／子代理分类。
- Python 包、配置示例、源码与私有运行数据分离。
- 命令行入口、后台启动脚本、MIT 许可证、文档及 CI。
- 移动端和桌面客户端列入路线图，当前仍使用 GitHub 网页入口。
