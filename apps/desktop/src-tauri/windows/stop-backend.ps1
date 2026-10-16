param([string]$InstallDirectory)
$ErrorActionPreference = 'Stop'

function Select-InstalledBackend {
    param([Parameter(Mandatory=$true)][string]$InstallDirectory, [object[]]$Processes)
    $target = [IO.Path]::GetFullPath((Join-Path $InstallDirectory 'opendialer-backend.exe'))
    return @($Processes | Where-Object {
        $_.ExecutablePath -and [string]::Equals([IO.Path]::GetFullPath($_.ExecutablePath), $target, [StringComparison]::OrdinalIgnoreCase)
    })
}

if ($MyInvocation.InvocationName -ne '.') {
    if (-not $InstallDirectory) { throw 'Installation directory is required' }
    $target = Join-Path ([IO.Path]::GetFullPath($InstallDirectory)) 'opendialer-backend.exe'
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { exit 0 }
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='opendialer-backend.exe'")
    foreach ($item in @(Select-InstalledBackend -InstallDirectory $InstallDirectory -Processes $processes)) {
        Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
    }
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        try {
            $file = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $file.Dispose()
            exit 0
        } catch { Start-Sleep -Milliseconds 200 }
    }
    throw 'The existing backend is still locked; close this installation before updating. Workspace data was not modified.'
}
