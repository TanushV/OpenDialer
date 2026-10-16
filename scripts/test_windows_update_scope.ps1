$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../apps/desktop/src-tauri/windows/stop-backend.ps1"
$directory = 'C:\Apps\OpenDialer'
$items = @(
    [pscustomobject]@{ProcessId=101;ExecutablePath='C:\Apps\OpenDialer\opendialer-backend.exe'},
    [pscustomobject]@{ProcessId=102;ExecutablePath='c:\apps\opendialer\opendialer-backend.exe'},
    [pscustomobject]@{ProcessId=103;ExecutablePath='C:\Other\opendialer-backend.exe'},
    [pscustomobject]@{ProcessId=104;ExecutablePath='C:\Apps\OpenDialer\other.exe'},
    [pscustomobject]@{ProcessId=105;ExecutablePath=$null}
)
$selected = @(Select-InstalledBackend -InstallDirectory $directory -Processes $items)
if ($selected.Count -ne 2 -or $selected[0].ProcessId -ne 101 -or $selected[1].ProcessId -ne 102) { throw 'Process cleanup escaped the exact installation path' }
if (@(Select-InstalledBackend -InstallDirectory 'C:\Unrelated' -Processes $items).Count -ne 0) { throw 'Unrelated installation was selected' }
Write-Host 'PASS: legacy update cleanup selects only the exact installed backend path; unrelated/unknown executables are excluded.'
