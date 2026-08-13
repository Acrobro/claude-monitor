<#
.SYNOPSIS
  Installs Claude Monitor as a launchable app.

.DESCRIPTION
  Creates a "Claude Monitor" shortcut in the Start Menu so it can be searched,
  pinned to Start or the taskbar, and launched like any other app.

.PARAMETER Startup
  Also launch it automatically when Windows starts.

.PARAMETER Remove
  Delete both shortcuts.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Startup
#>
param([switch]$Startup, [switch]$Remove)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $here "Claude Monitor.vbs"

$menuLink = Join-Path ([Environment]::GetFolderPath('Programs')) "Claude Monitor.lnk"
$bootLink = Join-Path ([Environment]::GetFolderPath('Startup')) "Claude Monitor.lnk"

if ($Remove) {
    foreach ($l in @($menuLink, $bootLink)) {
        if (Test-Path $l) { Remove-Item $l; "removed $l" }
    }
    return
}

if (-not (Test-Path $vbs)) { throw "launcher not found: $vbs" }

# Fail early and clearly rather than installing a shortcut that won't start.
$python = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command pyw.exe -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Warning @"
Python 3 was not found on your PATH, so the shortcut will not start yet.
Install it from https://www.python.org/downloads/ and tick
"Add python.exe to PATH" in the installer, then run this script again.
"@
} else {
    "found $($python.Source)"
}

# Borrow Claude's own icon when it's installed, so the entry is recognisable.
$icon = "$env:SystemRoot\System32\wscript.exe,0"
$app = Get-ChildItem (Join-Path $env:LOCALAPPDATA "AnthropicClaude") -Filter "app-*" -Directory -ErrorAction SilentlyContinue |
       Sort-Object Name | Select-Object -Last 1
if ($app) {
    $exe = Join-Path $app.FullName "claude.exe"
    if (Test-Path $exe) { $icon = "$exe,0" }
}

$shell = New-Object -ComObject WScript.Shell
function New-Link($path) {
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $sc.Arguments = "`"$vbs`""
    $sc.WorkingDirectory = $here
    $sc.IconLocation = $icon
    $sc.Description = "Shows which Claude Code sessions are working and which are done"
    $sc.Save()
    "installed $path"
}

New-Link $menuLink
if ($Startup) { New-Link $bootLink }

"`nLaunch it from the Start Menu (search 'Claude Monitor')."
"Right-click the Start Menu entry to pin it to Start or the taskbar."
