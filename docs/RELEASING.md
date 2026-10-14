# 版本发布

当前发布基线为 **0.2.0**，GitHub 标签为 `v0.2.0`，Release 名称为 `0.2.0`。首次公开版本为 `0.1.0`。标签指向审计和验证通过的源码提交，作为该次对外推送状态的固定记录。

## 后续更新规则

每次对外推送新的项目更新时，版本号必须高于上一公开版本。修复、文档和兼容性调整默认递增补丁版本，例如 `0.1.0 → 0.1.1`；新增功能可递增次版本，例如 `0.1.0 → 0.2.0`。开发分支可以多次提交，合并到对外发布的 `main` 时必须完成版本更新。已有版本标签和发行附件不能覆盖或移动。

同步修改这些位置：

1. `pyproject.toml` 的 `project.version`。
2. `src/codex2github/__init__.py` 的 `__version__`。
3. CHANGELOG 的版本条目和 README 的当前发布链接。
4. 相同版本的 GitHub `v版本号` 标签及 Release。

`codex2github --version` 显示安装的版本。隐私审计同时检查包版本与模块版本一致。

## 发布检查

只从裸源码目录生成源码和安装包。运行实例的配置、会话、任务结果、日志、截图和凭据不属于发行内容。提交作者与提交者均使用 GitHub 匿名邮箱。

```powershell
python -m unittest discover -s tests -q
python scripts/audit_public.py --history
python -m codex2github --version
python -m pip wheel --no-deps -w dist .
python scripts/normalize_release.py dist/codex2github-版本号-py3-none-any.whl
python scripts/audit_public.py dist/codex2github-版本号-py3-none-any.whl
```

发布前核对远端全部分支／标签的可达提交、提交元数据、旧文件版本，以及 Issues／评论、发行附件和 CI 可公开内容；仅扫描当前工作树不足以确认旧内容已清理。凭据和私人标识检查需要结合部署者的只读本地比对，检查报告不得记录秘密原值。

CI 通过后创建指向已验证提交的版本标签，再建立同版本 Release。发行附件只允许审计过的 wheel／源码包与校验值。确认仓库可见性、标签对应提交及附件后，再告知用户发布完成。源码仓库的公开状态不改变用户的私有任务仓库和监听器连接。

上传前，对每份 wheel／源码 ZIP 运行 `normalize_release.py`：全部 ZIP 条目使用固定时间 `2000-01-01 00:00:00`，文件权限固定为常规 `0644`／目录 `0755`，移除 ZIP extra 字段和全部注释。这些值是通用发行元数据，不能作为部署者本机权限或时间的证据。规范化后重新计算校验值并审计，再上传。GitHub 自动生成的源码归档及提交／发布记录仍可能展示公开项目时间。

自动规则扫描与人工复核不能证明任意文本绝对没有隐私；发布结果应说明实际检查范围与发现，不能声称不可实现的绝对保证。
