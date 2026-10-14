"""Opt-in, per-user Windows startup for this project's listener only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


MANAGED = "codex2github managed listener v1"
SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$request = $env:CODEX2GITHUB_STARTUP_REQUEST | ConvertFrom-Json
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('\')
$name = $request.name
$task = $null
try { $task = $folder.GetTask($name) } catch {
    if ($_.Exception.HResult -ne -2147024894) { throw }
}
if ($task -and $task.Definition.RegistrationInfo.Description -ne $request.managed) {
    throw 'Refusing to change a scheduled task not managed by codex2github'
}
if ($request.action -eq 'enable') {
    if (!$task) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $definition = $service.NewTask(0)
        $definition.RegistrationInfo.Description = $request.managed
        $definition.Principal.UserId = $identity.User.Value
        $definition.Principal.LogonType = 3
        $definition.Principal.RunLevel = 0
        $trigger = $definition.Triggers.Create(9)
        $trigger.UserId = $identity.User.Value
        $trigger.Enabled = $true
        $settings = $definition.Settings
        $settings.ExecutionTimeLimit = 'PT0S'
        $settings.MultipleInstances = 2
        $settings.StartWhenAvailable = $true
        $settings.DisallowStartIfOnBatteries = $false
        $settings.StopIfGoingOnBatteries = $false
        $settings.RestartCount = 999
        $settings.RestartInterval = 'PT1M'
        $settings.Hidden = $true
        $action = $definition.Actions.Create(0)
        $action.Path = $request.executable
        $action.Arguments = $request.arguments
        $action.WorkingDirectory = $request.cwd
        $task = $folder.RegisterTaskDefinition($name, $definition, 2, $identity.User.Value, $null, 3, $null)
    } else {
        $action = $task.Definition.Actions.Item(1)
        if ($action.Path -ne $request.executable -or $action.Arguments -ne $request.arguments) {
            throw 'Existing startup task uses a different executable; review its action before updating'
        }
        $task.Enabled = $true
    }
    if ($request.start) { $task.Run($null) | Out-Null }
} elseif ($request.action -eq 'disable') {
    if ($task) { $task.Enabled = $false }
} elseif ($request.action -ne 'status') { throw 'Invalid startup action' }
$result = @{ installed = [bool]$task; enabled = $false; running = $false; task_name = $name; starts_after_windows_login = $true }
if ($task) {
    $result.enabled = [bool]$task.Enabled
    $result.running = $task.State -eq 4
    $result.state = [int]$task.State
    $result.last_result = $task.LastTaskResult
}
$result | ConvertTo-Json -Compress
'''


def task_name(settings) -> str:
    identity = str(settings.path.resolve()).casefold().encode("utf-8")
    return "codex2github-" + hashlib.sha256(identity).hexdigest()[:12]


def manage(settings, action: str = "status", *, start: bool = False) -> dict:
    if action not in {"enable", "disable", "status"} or (start and action != "enable"):
        raise ValueError("Use autostart enable [--start], disable or status")
    if os.name != "nt":
        raise RuntimeError("Automatic startup currently requires Windows")
    executable = Path(sys.executable).resolve()
    windowless = executable.with_name("pythonw.exe")
    if windowless.is_file():
        executable = windowless
    arguments = subprocess.list2cmdline(["-m", "codex2github", "--config", str(settings.path),
                                        "watch", "--supervise"])
    request = {"name": task_name(settings), "managed": MANAGED, "action": action,
               "start": start, "executable": str(executable), "arguments": arguments,
               "cwd": str(settings.path.parent)}
    environment = os.environ.copy()
    environment["CODEX2GITHUB_STARTUP_REQUEST"] = json.dumps(request)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                            env=environment, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=45,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        # Do not echo command environments, machine paths, account names or tokens.
        raise RuntimeError("Windows could not apply the startup option; check Task Scheduler access")
    return json.loads(result.stdout)


def reply(status: dict) -> str:
    enabled = "已开启" if status.get("enabled") else "已关闭"
    running = "后台守护正在运行" if status.get("running") else "当前监听仍由现有进程负责；开启后守护会自动接续"
    return (f"开机自动启动：**{enabled}**（登录 Windows 后）。{running}。\n\n"
            "- `/autostart on`：开启并启动独立后台守护。\n"
            "- `/autostart off`：关闭下次自动启动；本次监听和正在执行的任务继续运行。\n"
            "- `/autostart`：查看状态。\n\n"
            "此选项只管理本项目的用户计划任务，不更改 Codex、CC Switch、路由或权限设置。")
