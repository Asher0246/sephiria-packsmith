param(
    [string]$GameDir = '',
    [string]$PackagePath = ''
)

$ErrorActionPreference = 'Stop'
$expectedHash = '82F9878551030F54657792C0740D9D51A09500EEAE1FBA21106B0C441E6732C4'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$resolver = Join-Path $scriptDir 'find_game.ps1'
. $resolver
$GameDir = Resolve-SephiriaGameDir -GameDir $GameDir
$gameExe = Join-Path $GameDir 'Sephiria.exe'
$runningGame = Get-Process -Name 'Sephiria' -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -eq $gameExe } catch { $false }
}
if ($runningGame) {
    Write-Warning 'Sephiria is running. The plugin will be updated now, but the game must be restarted before the new version takes effect.'
}
$pluginSource = Join-Path $scriptDir 'SephiriaInventoryBridge.dll'
if (-not (Test-Path -LiteralPath $pluginSource -PathType Leaf)) {
    $pluginSource = Join-Path $repoRoot 'artifacts\game_plugin\SephiriaInventoryBridge.dll'
}
if (-not (Test-Path -LiteralPath $pluginSource -PathType Leaf)) {
    throw 'Precompiled SephiriaInventoryBridge.dll is missing.'
}

$bepInExInstalled = Test-Path -LiteralPath (Join-Path $GameDir 'BepInEx\core\BepInEx.dll') -PathType Leaf
if (-not $bepInExInstalled) {
    if (-not $PackagePath) {
        $PackagePath = Join-Path $scriptDir 'BepInEx_win_x64_5.4.23.5.zip'
    }
    if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) {
        throw "BepInEx package not found: $PackagePath"
    }
    $actualHash = (Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256).Hash
    if ($actualHash -ne $expectedHash) {
        throw 'BepInEx package hash mismatch.'
    }
    $extractDir = Join-Path ([IO.Path]::GetTempPath()) ('SephiriaInventoryBridge-' + [guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
        Expand-Archive -LiteralPath $PackagePath -DestinationPath $extractDir -Force
        Copy-Item -LiteralPath (Join-Path $extractDir 'BepInEx') -Destination $GameDir -Recurse -Force
        foreach ($file in @('doorstop_config.ini', 'winhttp.dll')) {
            Copy-Item -LiteralPath (Join-Path $extractDir $file) -Destination (Join-Path $GameDir $file) -Force
        }
    } finally {
        if (Test-Path -LiteralPath $extractDir) {
            Remove-Item -LiteralPath $extractDir -Recurse -Force
        }
    }
}
$pluginDir = Join-Path $GameDir 'BepInEx\plugins'
New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
$pluginTarget = Join-Path $pluginDir 'SephiriaInventoryBridge.dll'
if (-not $runningGame) {
    Get-ChildItem -LiteralPath $pluginDir -Filter 'SephiriaInventoryBridge.dll.loaded-*' -File `
        -ErrorAction SilentlyContinue | Remove-Item -Force
}
try {
    Copy-Item -LiteralPath $pluginSource -Destination $pluginTarget -Force
} catch [System.IO.IOException] {
    if (-not $runningGame -or -not (Test-Path -LiteralPath $pluginTarget -PathType Leaf)) {
        throw
    }
    $processId = ($runningGame | Select-Object -First 1).Id
    $loadedTarget = "$pluginTarget.loaded-$processId-$(Get-Date -Format 'yyyyMMddHHmmss')"
    Move-Item -LiteralPath $pluginTarget -Destination $loadedTarget
    Copy-Item -LiteralPath $pluginSource -Destination $pluginTarget
    Write-Output "Preserved the currently loaded plugin as: $loadedTarget"
}
Write-Output "Installed the inventory read/apply bridge under: $GameDir"
if ($runningGame) {
    Write-Output 'Restart Sephiria, enter a run, then click Read Game in the solver.'
} else {
    Write-Output 'Start Sephiria, enter a run, then click Read Game in the solver.'
}
