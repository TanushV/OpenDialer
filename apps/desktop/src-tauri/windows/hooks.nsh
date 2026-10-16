!define OPENDIALER_HOOK_DIR "${__FILEDIR__}"
!macro NSIS_HOOK_PREINSTALL
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=opendialer-stop-backend.ps1 "${OPENDIALER_HOOK_DIR}\stop-backend.ps1"
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\opendialer-stop-backend.ps1" -InstallDirectory "$INSTDIR"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "OpenDialer could not stop this installation's old backend. Close it and retry the update. Your workspace has been preserved."
    Abort
  ${EndIf}
  SetOutPath "$INSTDIR"
!macroend
