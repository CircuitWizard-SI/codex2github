# 贡献指南

先创建 Issue 描述问题或建议，再提交改动。公开 Issue 只用于项目反馈；自己的远程任务应放在自己的私有任务仓库。

提交作者与提交者使用 GitHub 匿名邮箱（`ID+用户名@users.noreply.github.com`）。提交前运行 `python scripts/audit_public.py --history`，检查旧文件版本及提交元数据；不使用个人邮箱，不在提交说明中粘贴会话、机器路径或密钥。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

运行文件通知相关测试需要 Windows；其余核心逻辑可以在 Linux 上验证。测试使用合成会话与模拟执行器，禁止通过默认测试访问真实模型、真实任务或开发者个人会话。运行数据保存在 `.test-runtime/` 并由 Git 忽略。提交前运行 `python scripts/audit_public.py` 检查 Git 文件清单与明显敏感数据；该检查使用项目范围，不读取系统凭据。

## 改动约定

- 保持原会话 UUID、队列顺序与消息去重语义。
- 引导未确认时保留未知状态，不因超时自动重发或悄悄排队。
- 只在用户明确要求时关闭 GitHub 任务。
- 不将个人目录、供应商密钥、原会话或日志放入源码。
- 为影响行为的改动增加有实际区分能力的定向检查。
- 避免让 GUI 或第三方客户端直接写内部 JSON 状态；先扩展服务接口。

PR 说明包含问题、改变后的行为和验证结果。桌面 IPC 依赖内部协议，相关改动应写明所测版本及未验证边界。

## 版本与发布

当前公开版本为 `0.2.2`。后续每次对外推送项目更新时必须递增版本号，同时更新 `pyproject.toml`、包内 `__version__`、CHANGELOG 和对应 GitHub 标签／Release；不能覆盖已有版本标签。默认修复使用补丁版本（下一版 `0.2.3`），新增功能使用次版本。发布前完成完整历史和发行包隐私审计，遵循 [版本发布规则](docs/RELEASING.md)。
