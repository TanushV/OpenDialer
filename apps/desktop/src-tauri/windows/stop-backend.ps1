param([string]$InstallDirectory)
$ErrorActionPreference = 'Stop'
# The local authority and its CPU training child; both are replaced by an update.
$Sidecars = @('opendialer-backend.exe', 'opendialer-trainer.exe', 'opendialer-codex.exe', 'codex-code-mode-host.exe')

function Select-InstalledBackend {
    param([Parameter(Mandatory=$true)][string]$InstallDirectory, [object[]]$Processes)
    $targets = @($Sidecars | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $InstallDirectory $_)) })
    return @($Processes | Where-Object {
        $executable = $_.ExecutablePath
        $executable -and @($targets | Where-Object {
            [string]::Equals([IO.Path]::GetFullPath($executable), $_, [StringComparison]::OrdinalIgnoreCase)
        }).Count -gt 0
    })
}

if ($MyInvocation.InvocationName -ne '.') {
    if (-not $InstallDirectory) { throw 'Installation directory is required' }
    $root = [IO.Path]::GetFullPath($InstallDirectory)
    $installed = @($Sidecars | ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($installed.Count -eq 0) { exit 0 }
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='opendialer-backend.exe' OR Name='opendialer-trainer.exe' OR Name='opendialer-codex.exe' OR Name='codex-code-mode-host.exe'")
    foreach ($item in @(Select-InstalledBackend -InstallDirectory $InstallDirectory -Processes $processes)) {
        Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($target in $installed) {
        $released = $false
        for ($attempt = 0; $attempt -lt 50 -and -not $released; $attempt++) {
            try {
                $file = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
                $file.Dispose()
                $released = $true
            } catch { Start-Sleep -Milliseconds 200 }
        }
        if (-not $released) {
            throw 'The existing backend is still locked; close this installation before updating. Workspace data was not modified.'
        }
    }
    exit 0
}
