param(
    [string]$GameDir = ''
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'find_game.ps1')
$GameDir = Resolve-SephiriaGameDir -GameDir $GameDir
$gameExe = Join-Path $GameDir 'Sephiria.exe'
$runningGame = Get-Process -Name 'Sephiria' -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -eq $gameExe } catch { $false }
}
if ($runningGame) {
    throw 'Close Sephiria before uninstalling the inventory bridge.'
}
$plugin = Join-Path $GameDir 'BepInEx\plugins\SephiriaInventoryBridge.dll'
if (Test-Path -LiteralPath $plugin -PathType Leaf) {
    Remove-Item -LiteralPath $plugin -Force
    Write-Output "Removed SephiriaInventoryBridge.dll. BepInEx was left in place for other plugins."
} else {
    Write-Output "Inventory bridge plugin is not installed."
}
