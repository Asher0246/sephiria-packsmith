function Resolve-SephiriaGameDir {
    param([string]$GameDir = '')

    if ($GameDir) {
        $resolved = [IO.Path]::GetFullPath($GameDir)
        if (Test-Path -LiteralPath (Join-Path $resolved 'Sephiria.exe') -PathType Leaf) {
            return $resolved
        }
        throw "Sephiria.exe was not found under: $resolved"
    }

    $steamRoots = [Collections.Generic.List[string]]::new()
    foreach ($registryPath in @('HKCU:\Software\Valve\Steam', 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam')) {
        try {
            $steam = Get-ItemProperty -LiteralPath $registryPath -ErrorAction Stop
            foreach ($property in @('SteamPath', 'InstallPath')) {
                if ($steam.$property) { $steamRoots.Add([string]$steam.$property) }
            }
        } catch {}
    }
    foreach ($root in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Steam'),
        'C:\Steam', 'D:\Steam', 'D:\SteamLibrary', 'E:\SteamLibrary', 'F:\SteamLibrary'
    )) {
        if ($root) { $steamRoots.Add($root) }
    }

    $libraryRoots = [Collections.Generic.List[string]]::new()
    foreach ($steamRoot in ($steamRoots | Select-Object -Unique)) {
        $libraryRoots.Add($steamRoot)
        $libraryFile = Join-Path $steamRoot 'steamapps\libraryfolders.vdf'
        if (Test-Path -LiteralPath $libraryFile -PathType Leaf) {
            foreach ($line in Get-Content -LiteralPath $libraryFile) {
                if ($line -match '^\s*"path"\s+"([^"]+)"') {
                    $libraryRoots.Add($Matches[1].Replace('\\', '\'))
                }
            }
        }
    }

    foreach ($libraryRoot in ($libraryRoots | Select-Object -Unique)) {
        $candidate = Join-Path $libraryRoot 'steamapps\common\Sephiria'
        if (Test-Path -LiteralPath (Join-Path $candidate 'Sephiria.exe') -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw 'Sephiria was not found in the detected Steam libraries. Run install.ps1 with -GameDir "X:\...\Sephiria".'
}
