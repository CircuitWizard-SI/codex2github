param([string]$ConfigPath = '')
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
if (-not $ConfigPath) { $ConfigPath = Join-Path $ProjectRoot 'config.json' }
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$settings = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$runtimePath = $settings.runtime_dir
if (-not $runtimePath) { $runtimePath = '.runtime' }
if (-not [IO.Path]::IsPathRooted($runtimePath)) { $runtimePath = Join-Path (Split-Path $ConfigPath -Parent) $runtimePath }
New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath)) { $PythonPath = (Get-Command python.exe -ErrorAction Stop).Source }
$process = Start-Process -FilePath $PythonPath -ArgumentList @('-m','codex2github','--config',('"' + $ConfigPath + '"'),'watch') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'launcher.log') -RedirectStandardError (Join-Path $runtimePath 'launcher-error.log')
Write-Output "codex2github listener started: $($process.Id)"
