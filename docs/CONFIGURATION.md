# 配置与启动

`codex2github --config path/to/config.json ...` 为一个实例指定配置文件。相对路径相对配置文件目录解析；子进程沿用同一绝对配置路径。每个实例应使用独立的运行目录和任务仓库。

| 字段 | 默认值 | 用途 |
|---|---|---|
| `repo` | 必填 | `用户名/专用私有任务仓库` |
| `model` | `auto` | 兼容旧配置的字段；实际默认模型始终读取本机当前 Codex/CC Switch 路由 |
| `workspace` | `workspace` | 默认任务输出目录 |
| `runtime_dir` | `.runtime` | 会话绑定、队列、日志、任务结果和历史镜像 |
| `codex_home` | `CODEX_HOME` 或 `~/.codex` | 只读访问原会话索引及历史数据；CLI 本身按正常方式写入会话 |
| `poll_seconds` | `15` | GitHub 留言检查间隔 |
| `sync_seconds` | `30` | 事件触发的历史同步最短间隔 |
| `recovery_seconds` | `900` | 文件通知漏报／离线变化的恢复检查间隔 |

运行目录是私有数据目录，不应加入 Git。`config.json` 不保存 API 密钥：GitHub 登录由 `gh auth login` 负责，模型和凭据由 Codex／用户已有的供应商配置负责。

本源码仓库是空白部署项目。每位部署者通过 `init --repo 自己的用户名/自己的私有任务仓库` 建立自己的实例；不需要复制维护者的配置或数据。不要把本源码仓库设成 `repo`，即使它是私有仓库：`doctor`、任务处理和历史发布都会拒绝本源码仓库及 GitHub 模板仓库。已经部署的私人实例保留原有 `config.json`、任务仓库和会话绑定，更新分发源码不要求迁移。

安装及 `init` 不同步历史。启动 `watch` 后，当前版本同步部署者本机全部可读会话（含归档和子代理），目标是其专用私有任务仓库。部署者需要事先接受上传范围；用户对话中的源码、路径和其它敏感文字可能被包含。

## 启动方式

```powershell
# 生成配置，不创建模型会话、不写入 GitHub
.\.venv\Scripts\python.exe -m codex2github init --repo YOUR_USER/codex-tasks

# 只读检查
.\.venv\Scripts\python.exe -m codex2github doctor

# 前台持续运行；Ctrl+C 停止监听，不关闭 Issue
.\.venv\Scripts\python.exe -m codex2github watch

# 后台运行
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

修改配置后重启监听器生效。用不同配置文件运行不同实例时，指定 `--config`：

```powershell
.\.venv\Scripts\python.exe -m codex2github --config .\config.local-second.json doctor
```

## 数据与权限

同步器只读 SQLite／JSONL，模型执行器续接同一个原会话 ID。输出位置通过提示和沙箱设置控制；续接原会话时可能使用原工作目录，实际可写范围取决于 Codex 的沙箱实现。不要把提示文字当成权限隔离机制。

`/model` 保存桥接器的会话模型覆盖，只影响对应 Issue 绑定的会话。每次处理请求都会读取本机路由指纹；如果 Codex 配置或 CC Switch 的活动路由标识发生变化，全部旧覆盖自动失效，排队消息也回到 `auto`，随后使用新路由默认模型。

`/models` 和带模型 ID 的 `/model` 会重新读取当前 Codex 配置（含选中的 profile），对该活动端点发送只读 `GET /models`，过滤图片、音频等非工作模型。接口不可用时，回退到当前配置指定的本机模型目录、与当前端点匹配的 CC Switch 当前 provider 目录及默认模型。保存的其它 provider、堆栈成员和历史用量不作为直连路由的可用模型来源。目录查询不执行生成，不增加任务提示前缀，也不会在空闲监听时轮询端点。

`/effort`（别名 `/reasoning`）显示当前对话的思考档位及模型支持范围。使用 `/effort high`、`/effort xhigh` 等进行单会话覆盖，或 `/model 模型ID xhigh` 同时选择；`/effort auto` 清除档位覆盖。可选值由匹配本机路由的模型目录读取；未标注能力时明确说明，不能把客户端枚举当成模型已支持的档位。换模型清除旧档位覆盖，换路由清除所有旧模型和档位覆盖，等待消息随新默认值执行。

已有 Windows Desktop 会话执行前，桥接器只调用会话所有者的下一轮模型／档位接口，读回确认后才调用官方 CLI 入队。发送字段严格限定为目标会话 ID、`model` 和 `effort`，不发送权限字段或当前轮次 ID；watcher 只读本机状态。接口不可用则尝试 CLI app-server，原会话仍被占用时等待释放，不假装参数已生效。独立 app-server 直接在 `turn/start` 中传入档位。所有路径都保持任务原文，无附加提示前缀。

`/steer` 直接引导已有执行轮次。对原生会话，先只读原始日志中的最新开始／完成事件，再连接原会话所有者的 `thread-follower-steer-turn`；所有者实际发送 `turn/steer`，响应中的轮次必须与目标一致。引导不改变模型、路由或权限，也不新建会话。入队回执不能作为“当前空闲”的证据；活动轮次不明或引导回执丢失时保持未确认状态，不自动排队、重发或中断。确认空闲时才作为下一轮消息处理。

认证沿用本机当前路由，凭据只用于该端点的读取且不跟随重定向；桥接器不保存密钥、不写入 Codex、CC Switch、provider、endpoint、API key 或路由文件。目录中列出模型并不表示桥接器逐一执行过生成测试。

当前留言授权规则是专用私有任务仓库的拥有者账号；组织仓库和多用户授权尚未实现。程序在处理评论、发布结果和上传历史前检查仓库仍为私有。不要把公开源码仓库配置成任务仓库。

## 常见状态

- `doctor` 显示 `private_task_repository: false`：确认配置指向自己的私有任务仓库，且 `gh` 已登录有访问权限的账号。
- 模型不在 `/models`：确认 CC Switch 已切换到目标路由，端点的 `/models` 支持当前凭据。接口不支持或不可达时，列表会回退到匹配的本机目录和默认模型；由你在 CC Switch 调整路由或目录，桥接器不会自行修改它们。
- 送达卡为橙色：请求结果未确认，监听器会继续核对消息唯一标识；先查看任务反馈，避免重复发送。
- 已提交但没有最终回复：保持监听器运行；提交回执不代表任务完成，程序会只读核对对应本机轮次，结果发布失败时自动重试。旧入队记录会恢复追踪，不需要重发任务。
- 完整历史只显示早期内容：同步器会合并数据库和原始会话文件；监听器启动后先恢复漏掉的变化，再按文件通知更新。没有可读的原始记录时，保留能够读取的历史。
- 手机列表没有变化：下拉刷新页面。电脑关闭或监听器停止时，仓库显示最后一次快照。
