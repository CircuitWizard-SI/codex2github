# 部署裸项目

本源码模板用于分发程序。它没有维护者的会话、任务、运行配置、数据库或凭据。每位用户在自己的电脑上部署，再创建自己的私有任务仓库；不要将源代码模板设为任务仓库。

## 部署前

- 当前实际监听功能要求 Windows、Python 3.11+、Git、已登录的 GitHub CLI，以及已经可以正常执行任务的 Codex CLI。
- 本机 Codex 模型路由与凭据由用户自行登录或配置，程序只读取已有设置。
- 用户需要登录或授权 GitHub；若源码仓库尚未公开，还需要该仓库的读取权限。
- 保持电脑开机、联网。继续 Desktop 已占用的会话时保持 Desktop 打开；项目不安装服务或开机启动项。
- 启动监听后，当前版本会同步部署者本机全部可读会话历史（包括归档和子代理）到其私有任务仓库。会话中的代码和路径也可能上传，部署者应接受此范围。

## 安装与启动

在准备放置项目的目录打开 PowerShell，逐行执行。把 `YOUR_USER` 换成自己的 GitHub 用户名。

```powershell
gh auth login
gh repo clone CircuitWizard-SI/codex2github
cd codex2github
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
gh repo create YOUR_USER/codex-tasks --private
.\.venv\Scripts\python.exe -m codex2github init --repo YOUR_USER/codex-tasks
.\.venv\Scripts\python.exe -m codex2github doctor
.\.venv\Scripts\python.exe -m codex2github setup
.\.venv\Scripts\python.exe -m codex2github watch
```

已有自己的专用私有任务仓库时，跳过 `gh repo create`。`doctor` 应显示 `ok: true`；`setup` 打印手机端聊天入口链接。第一次可以前台观察，确认正常后按 Ctrl+C 停止监听，再后台运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

前台与后台选择一种即可。安装和 `init` 不会上传会话；启动 `watch` 才会持续检查任务和发布历史。电脑重启后重新运行启动命令。

## 手机创建新会话

打开自己的私有任务仓库，在 Issues 中新建 Issue。标题填会话名，正文填任务，提交后本机自动创建 Codex 会话，不需要先在桌面端新建。以后在原 Issue 评论继续对话。

想先指定模型和档位时，正文只填 `/model 模型ID 档位`，收到设置确认后再评论实际任务。`/models` 查看当前路由模型与档位，`/effort` 查看当前档位，`/effort auto` 恢复默认。

## 交给 Codex 自动部署

可以将下面的要求和仓库链接发给本机 Codex：

> 请部署 https://github.com/CircuitWizard-SI/codex2github 。优先使用非 C 盘目录，在项目虚拟环境安装；允许创建我的专用私有任务仓库，生成项目配置，检查后启动后台监听器。部署前说明当前版本会将我的全部可读 Codex 会话历史上传到我的私有任务仓库。不得修改本机 Codex、CC Switch 的路由、密钥或权限配置；不安装系统服务或开机启动项。不要将源码模板配置为任务仓库。需要我登录时给出具体操作，完成后继续，最后提供手机聊天与新建会话链接。

通常只有登录、浏览器授权和实际出现的系统安装确认需要用户亲自操作。不需要用户把密钥发到聊天里。
