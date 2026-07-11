param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]] $Args
)

$ErrorActionPreference = 'Stop'
$AidebugDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $AidebugDir
$Runner = Join-Path $AidebugDir 'runner\aidebug_health.py'
$ProjectlingAutoRunner = Join-Path $AidebugDir 'runner\projectling_auto.py'
$MotdRunner = Join-Path $AidebugDir 'runner\motd_zshrc_smoke.py'

function Find-Python {
  if ($env:PROJECTLING_PYTHON) {
    $candidate = @($env:PROJECTLING_PYTHON)
    if (Test-PythonCandidate $candidate) {
      return $candidate
    }
    throw "PROJECTLING_PYTHON is set but is not runnable: $env:PROJECTLING_PYTHON"
  }
  $python = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($python) {
    $candidate = @($python.Source)
    if (Test-PythonCandidate $candidate) {
      return $candidate
    }
  }
  $py = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($py) {
    $candidate = @($py.Source, '-3')
    if (Test-PythonCandidate $candidate) {
      return $candidate
    }
  }
  throw 'Python 3 not found. Install Python or set PROJECTLING_PYTHON.'
}

function Test-PythonCandidate {
  param([string[]] $Candidate)

  if (-not $Candidate -or $Candidate.Count -eq 0) {
    return $false
  }
  $exe = $Candidate[0]
  $prefix = @()
  if ($Candidate.Count -gt 1) {
    $prefix = $Candidate[1..($Candidate.Count - 1)]
  }
  try {
    & $exe @prefix --version *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

if (-not $Args -or $Args.Count -eq 0) {
  $Args = @('windows')
}

$cmd = $Args[0]
$rest = @()
if ($Args.Count -gt 1) {
  $rest = $Args[1..($Args.Count - 1)]
}

$python = @(Find-Python)
$pythonExe = $python[0]
$pythonPrefix = @()
if ($python.Count -gt 1) {
  $pythonPrefix = $python[1..($python.Count - 1)]
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PROJECTLING_DIR = $ProjectRoot
$env:AITERMUX_HOME = (Split-Path -Parent $ProjectRoot)
$env:AITERMUX_AIDEBUG_DIR = $AidebugDir

switch ($cmd) {
  'windows' {
    & $pythonExe @pythonPrefix $Runner --windows @rest
    exit $LASTEXITCODE
  }
  'health' {
    & $pythonExe @pythonPrefix $Runner @rest
    exit $LASTEXITCODE
  }
  'projectling-auto' {
    & $pythonExe @pythonPrefix $ProjectlingAutoRunner @rest
    exit $LASTEXITCODE
  }
  'motd-zshrc-smoke' {
    & $pythonExe @pythonPrefix $MotdRunner @rest
    exit $LASTEXITCODE
  }
  'status' {
    Write-Output "aidebug_dir=$AidebugDir"
    Write-Output "projectling_dir=$ProjectRoot"
    Get-ChildItem -LiteralPath $AidebugDir -Recurse -File -ErrorAction SilentlyContinue |
      Where-Object { $_.Extension -in '.log','.json','.jsonl','.md' } |
      ForEach-Object {
        $rel = $_.FullName.Substring($AidebugDir.Length).TrimStart('\')
        "file=$rel bytes=$($_.Length) mtime=$($_.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
      }
  }
  default {
    Write-Error "unknown aidebug command: $cmd"
    exit 2
  }
}
