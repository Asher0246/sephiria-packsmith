param(
    [string]$GameDir = 'D:\SteamLibrary\steamapps\common\Sephiria',
    [string]$BepInExRoot = ''
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$outputDir = Join-Path $repoRoot 'artifacts\game_plugin'
if (-not $BepInExRoot) {
    $BepInExRoot = $GameDir
}
$bepInExDll = Join-Path $BepInExRoot 'BepInEx\core\BepInEx.dll'
$unityFacadeDll = Join-Path $GameDir 'Sephiria_Data\Managed\UnityEngine.dll'
$unityDll = Join-Path $GameDir 'Sephiria_Data\Managed\UnityEngine.CoreModule.dll'
$netstandardDll = Join-Path $GameDir 'Sephiria_Data\Managed\netstandard.dll'
$compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
foreach ($required in @($bepInExDll, $unityFacadeDll, $unityDll, $netstandardDll, $compiler)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Missing build dependency: $required"
    }
}
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$output = Join-Path $outputDir 'SephiriaInventoryBridge.dll'
& $compiler /nologo /target:library /optimize+ /langversion:5 `
    "/reference:$bepInExDll" "/reference:$unityFacadeDll" "/reference:$unityDll" "/reference:$netstandardDll" `
    "/out:$output" (Join-Path $scriptDir 'SephiriaInventoryBridge.cs')
if ($LASTEXITCODE -ne 0) {
    throw "C# compiler failed with exit code $LASTEXITCODE"
}
Get-FileHash -LiteralPath $output -Algorithm SHA256
