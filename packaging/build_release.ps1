param(
    [string]$ReleaseName = 'SephiriaPacksmith-v1.0.0-win-x64',
    [string]$PortableBaseName = 'SephiriaPacksmith-portable-base-win-x64',
    [string]$BepInExPackage = '',
    [string]$BepInExLicense = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$dependencyRoot = Join-Path $repoRoot 'artifacts\dependencies'
if (-not $BepInExPackage) {
    $BepInExPackage = Join-Path $dependencyRoot 'BepInEx_win_x64_5.4.23.5.zip'
}
if (-not $BepInExLicense) {
    $BepInExLicense = Join-Path $dependencyRoot 'BepInEx-5.4.23.5-LICENSE.txt'
}
$releaseRoot = Join-Path $repoRoot 'artifacts\release'
$target = Join-Path $releaseRoot $ReleaseName
$zipPath = "$target.zip"
$portableBase = Join-Path $releaseRoot $PortableBaseName
$pluginDll = Join-Path $repoRoot 'artifacts\game_plugin\SephiriaInventoryBridge.dll'
$pluginInstaller = Join-Path $repoRoot 'game_plugin\install.ps1'

if (Test-Path -LiteralPath $target) {
    throw "Release directory already exists: $target"
}
if (Test-Path -LiteralPath $zipPath) {
    throw "Release ZIP already exists: $zipPath"
}
foreach ($required in @(
    (Join-Path $portableBase 'runtime'),
    $pluginDll,
    $pluginInstaller,
    $BepInExPackage,
    $BepInExLicense
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing release input: $required"
    }
}

$installerText = Get-Content -LiteralPath $pluginInstaller -Raw
$expectedHashMatch = [regex]::Match($installerText, '\$expectedHash\s*=\s*''([0-9A-Fa-f]{64})''')
if (-not $expectedHashMatch.Success) {
    throw "Plugin installer does not contain a valid expectedHash: $pluginInstaller"
}
$pluginHash = (Get-FileHash -LiteralPath $pluginDll -Algorithm SHA256).Hash
if ($pluginHash -ne $expectedHashMatch.Groups[1].Value.ToUpperInvariant()) {
    throw "Plugin DLL hash does not match game_plugin/install.ps1: $pluginHash"
}

New-Item -ItemType Directory -Path $target | Out-Null
New-Item -ItemType Directory -Path (Join-Path $target 'app\static') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $target 'assets') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $target 'game_plugin') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $target 'THIRD_PARTY_LICENSES') -Force | Out-Null

$appFiles = @(
    '__init__.py', 'catalog.py', 'custom_tablets.py', 'game_bridge.py',
    'models.py', 'server.py', 'solver.py', 'validation.py'
)
foreach ($file in $appFiles) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "app\$file") -Destination (Join-Path $target 'app')
}
Copy-Item -Path (Join-Path $repoRoot 'app\static\*') -Destination (Join-Path $target 'app\static') -Recurse
Copy-Item -LiteralPath (Join-Path $repoRoot 'assets\wiki_artifacts.json') -Destination (Join-Path $target 'assets')
Copy-Item -LiteralPath (Join-Path $repoRoot 'assets\wiki_tablets.json.gz') -Destination (Join-Path $target 'assets')
Copy-Item -LiteralPath (Join-Path $repoRoot 'assets\wiki_zh_cn.json') -Destination (Join-Path $target 'assets')
Copy-Item -LiteralPath (Join-Path $repoRoot 'assets\images') -Destination (Join-Path $target 'assets') -Recurse
Copy-Item -LiteralPath (Join-Path $portableBase 'runtime') -Destination $target -Recurse

$pluginFiles = @('find_game.ps1', 'install.ps1', 'README.md', 'uninstall.ps1')
foreach ($file in $pluginFiles) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "game_plugin\$file") -Destination (Join-Path $target 'game_plugin')
}
Get-ChildItem -LiteralPath (Join-Path $repoRoot 'game_plugin') -File -Filter '*.bat' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $target 'game_plugin')
}
Copy-Item -LiteralPath $pluginDll -Destination (Join-Path $target 'game_plugin')
Copy-Item -LiteralPath $BepInExPackage -Destination (Join-Path $target 'game_plugin\BepInEx_win_x64_5.4.23.5.zip')
Copy-Item -LiteralPath $BepInExLicense -Destination (Join-Path $target 'THIRD_PARTY_LICENSES\BepInEx-5.4.23.5-LICENSE.txt')

foreach ($file in @('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $file) -Destination $target
}
Get-ChildItem -LiteralPath $repoRoot -File | Where-Object { $_.Extension -in @('.txt', '.bat') } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $target
}

$hashLines = Get-ChildItem -LiteralPath $target -File -Recurse | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($target.Length + 1).Replace('\', '/')
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $relative"
}
[IO.File]::WriteAllLines((Join-Path $target 'SHA256SUMS.txt'), $hashLines, [Text.UTF8Encoding]::new($false))

Compress-Archive -LiteralPath $target -DestinationPath $zipPath -CompressionLevel Optimal
$zip = Get-Item -LiteralPath $zipPath
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
Write-Output "Release: $($zip.FullName)"
Write-Output "Bytes: $($zip.Length)"
Write-Output "SHA256: $zipHash"
