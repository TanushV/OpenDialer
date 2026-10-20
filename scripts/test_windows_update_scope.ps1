$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../apps/desktop/src-tauri/windows/stop-backend.ps1"
$directory = 'C:\Apps\OpenDialer'
$items = @(
    [pscustomobject]@{ProcessId=101;ExecutablePath='C:\Apps\OpenDialer\opendialer-backend.exe'},
    [pscustomobject]@{ProcessId=102;ExecutablePath='c:\apps\opendialer\opendialer-backend.exe'},
    [pscustomobject]@{ProcessId=103;ExecutablePath='C:\Other\opendialer-backend.exe'},
    [pscustomobject]@{ProcessId=104;ExecutablePath='C:\Apps\OpenDialer\other.exe'},
    [pscustomobject]@{ProcessId=105;ExecutablePath=$null},
    [pscustomobject]@{ProcessId=106;ExecutablePath='C:\Apps\OpenDialer\opendialer-trainer.exe'},
    [pscustomobject]@{ProcessId=107;ExecutablePath='C:\Other\opendialer-trainer.exe'}
)
$selected = @(Select-InstalledBackend -InstallDirectory $directory -Processes $items)
$ids = @($selected | ForEach-Object { $_.ProcessId })
if (($ids -join ',') -ne '101,102,106') { throw "Process cleanup escaped the exact installation path: $($ids -join ',')" }
if (@(Select-InstalledBackend -InstallDirectory 'C:\Unrelated' -Processes $items).Count -ne 0) { throw 'Unrelated installation was selected' }
Write-Host 'PASS: update cleanup selects only this installation''s backend and trainer paths; unrelated/unknown executables are excluded.'
