; AverQel Kinesis NSIS Installer Script
; This script is included by electron-builder for Windows NSIS installer customization

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"

; Basic installer settings
Name "AverQel Kinesis"
OutFile "AverQel-Kinesis-Setup-${PRODUCT_VERSION}-${PRODUCT_ARCH}.exe"
InstallDir "$PROGRAMFILES\AverQel Kinesis"
InstallDirRegKey HKCU "Software\AverQel\Kinesis" ""
ShowInstDetails show
ShowUninstDetails show
RequestExecutionLevel admin
Unicode true

; Compression
SetCompressor lzma
SetCompressor /SOLID lzma
SetCompress /auto

; Modern UI
!define MUI_ICON "${NSISDIR}\assets\installer.ico"
!define MUI_UNICON "${NSISDIR}\assets\installer.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${NSISDIR}\assets\nsis-header.bmp"
!define MUI_WELCOMEPAGE_TEXT "Welcome to the AverQel Kinesis Setup Wizard. This will install AverQel Kinesis on your computer."
!define MUI_FINISHPAGE_RUN "$INSTDIR\AverQel Kinesis.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch AverQel Kinesis"
!define MUI_FINISHPAGE_TEXT "AverQel Kinesis has been installed on your computer. Click Finish to close this wizard."

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "assets/LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

; Language strings
LangString LICENSE_TITLE ${LANG_ENGLISH} "License Agreement"
LangString LICENSE_TEXT ${LANG_ENGLISH} "Please read the following license agreement carefully. You must accept the terms of this agreement before continuing with the installation."
LangString DIR_SUBTEXT ${LANG_ENGLISH} "Destination Folder"
LangString DIR_BROWSE_TEXT ${LANG_ENGLISH} "Browse..."
LangString UNCONFIRM ${LANG_ENGLISH} "Are you sure you want to uninstall $(^NameDA)?"
LangString UNINSTALLING ${LANG_ENGLISH} "Uninstalling $(^NameDA)..."
LangString REMOVE_COMPLETED ${LANG_ENGLISH} "$(^NameDA) has been successfully removed from your computer."
LangString REBOOT_REQUIRED ${LANG_ENGLISH} "A system restart is required to complete the installation."
LangString REBOOT_ASK ${LANG_ENGLISH} "Do you want to restart your computer now?"

; Custom pages
Var PREVIOUS_VERSION
Var DOES_VERSION_EXIST

Function .onInit
    ; Check if already installed
    ReadRegStr $R0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\{${PRODUCT_GUID}}" "DisplayVersion"
    StrCmp $R0 "" 0 +2
    StrCpy $PREVIOUS_VERSION $R0
    StrCpy $DOES_VERSION_EXIST "1"
FunctionEnd

Function onInitCompleted
    ; Check if we need to close running instances
    ${If} $DOES_VERSION_EXIST == "1"
        ; Check if AverQel Kinesis is running
        FindWindow $0 "AverQel Kinesis"
        ${If} $0 != 0
            MessageBox MB_YESNO|MB_ICONQUESTION "AverQel Kinesis is currently running. Do you want to close it before continuing?" IDYES +2
            Abort
            SendMessage $0 ${WM_CLOSE} 0 0
            Sleep 1000
        ${EndIf}
    ${EndIf}
FunctionEnd

Function un.onInit
    ; Check if app is running before uninstall
    FindWindow $0 "AverQel Kinesis"
    ${If} $0 != 0
        MessageBox MB_YESNO|MB_ICONQUESTION "AverQel Kinesis is currently running. Please close it before uninstalling." IDYES +2
        Abort
        SendMessage $0 ${WM_CLOSE} 0 0
        Sleep 1000
    ${EndIf}
FunctionEnd

Function un.onUninstSuccess
    HideWindow
    MessageBox MB_OK|MB_ICONINFORMATION "$(^NameDA) has been successfully removed from your computer."
FunctionEnd

Section "AverQel Kinesis" SecMain
    SetOutPath $INSTDIR

    ; Main executable
    File "AverQel Kinesis.exe"

    ; Backend runtime
    File /r "backend-runtime\averqel-backend*"
    File /r "backend-runtime\alembic*"
    File "backend-runtime\alembic.ini"

    ; Frontend
    File /r "frontend\*"

    ; Playwright browsers
    File /r "playwright-browsers\*"

    ; Assets
    File "assets\icon.ico"
    File "assets\installer.ico"

    ; License
    File "assets\LICENSE"

    ; Create uninstaller
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Write registry keys for uninstaller
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "DisplayName" "AverQel Kinesis"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "DisplayVersion" "${PRODUCT_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "Publisher" "AverQel"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "QuietUninstallString" "$INSTDIR\uninstall.exe /S"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "DisplayIcon" "$INSTDIR\AverQel Kinesis.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "URLInfoAbout" "https://averqel.com"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "URLUpdateInfo" "https://averqel.com/download"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "NoModify" "1"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "NoRepair" "1"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "EstimatedSize" 0
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}" "InstallDate" "$(GetDate)"

    ; Create start menu shortcuts
    CreateDirectory "$SMPROGRAMS\AverQel Kinesis"
    CreateShortcut "$SMPROGRAMS\AverQel Kinesis\AverQel Kinesis.lnk" "$INSTDIR\AverQel Kinesis.exe" "" "$INSTDIR\icon.ico" 0
    CreateShortcut "$SMPROGRAMS\AverQel Kinesis\Uninstall.lnk" "$INSTDIR\uninstall.exe" "" "$INSTDIR\icon.ico" 0
    CreateShortcut "$SMPROGRAMS\AverQel Kinesis\Documentation.lnk" "https://averqel.com/docs" "" "$INSTDIR\icon.ico" 0

    ; Desktop shortcut
    CreateShortcut "$DESKTOP\AverQel Kinesis.lnk" "$INSTDIR\AverQel Kinesis.exe" "" "$INSTDIR\icon.ico" 0

    ; Register file associations if needed
    ; WriteRegStr HKCR ".akinesis" "" "AverQelKinesis.Project"
    ; WriteRegStr HKCR "AverQelKinesis.Project" "" "AverQel Kinesis Project"
    ; WriteRegStr HKCR "AverQelKinesis.Project\DefaultIcon" "" "$INSTDIR\icon.ico"
    ; WriteRegStr HKCR "AverQelKinesis.Project\shell\open\command" "" '"$INSTDIR\AverQel Kinesis.exe" "%1"'
SectionEnd

Section -AdditionalIcons
    CreateShortcut "$SMPROGRAMS\AverQel Kinesis\Documentation.lnk" "https://averqel.com/docs"
    CreateShortcut "$SMPROGRAMS\AverQel Kinesis\Uninstall.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

Section -Post
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Register file association
    WriteRegStr HKCR ".akinesis" "" "AverQelKinesis.Project"
    WriteRegStr HKCR "AverQelKinesis.Project" "" "AverQel Kinesis Project"
    WriteRegStr HKCR "AverQelKinesis.Project\DefaultIcon" "" "$INSTDIR\icon.ico"
    WriteRegStr HKCR "AverQelKinesis.Project\shell\open\command" "" '"$INSTDIR\AverQel Kinesis.exe" "%1"'

    ; Set up firewall rule for backend
    ${If} ${RunningX64}
        ExecWait 'netsh advfirewall firewall add rule name="AverQel Kinesis" dir=in action=allow program="$INSTDIR\backend-runtime\averqel-backend\averqel-backend.exe" enable=yes profile=any'
    ${Else}
        ExecWait 'netsh advfirewall firewall add rule name="AverQel Kinesis" dir=in action=allow program="$INSTDIR\backend-runtime\averqel-backend\averqel-backend.exe" enable=yes profile=any'
    ${EndIf}
SectionEnd

UninstallSection "Uninstall" SecUninstall
    ; Remove firewall rule
    ExecWait 'netsh advfirewall firewall delete rule name="AverQel Kinesis"'

    ; Remove registry entries
    DeleteRegKey HKCR ".akinesis"
    DeleteRegKey HKCR "AverQelKinesis.Project"
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_GUID}"

    ; Remove shortcuts
    Delete "$SMPROGRAMS\AverQel Kinesis\*.*"
    RMDir "$SMPROGRAMS\AverQel Kinesis"
    Delete "$DESKTOP\AverQel Kinesis.lnk"

    ; Remove files
    Delete "$INSTDIR\*.*"
    RMDir /r "$INSTDIR\backend-runtime"
    RMDir /r "$INSTDIR\frontend"
    RMDir /r "$INSTDIR\playwright-browsers"
    RMDir /r "$INSTDIR\assets"

    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"
SectionEnd

Function GetDate
    System::Call 'KERNEL32::GetLocalTime(i .r0)'
    Pop $0
    System::Call 'KERNEL32::GetLocalTime(i .r1)'
    Pop $1
    System::Call 'KERNEL32::GetLocalTime(i .r2)'
    Pop $2
    System::Call 'KERNEL32::GetLocalTime(i .r3)'
    Pop $3
    System::Call 'KERNEL32::GetLocalTime(i .r4)'
    Pop $4
    System::Call 'KERNEL32::GetLocalTime(i .r5)'
    Pop $5
    System::Call 'KERNEL32::GetLocalTime(i .r6)'
    Pop $6
    System::Call 'KERNEL32::GetLocalTime(i .r7)'
    Pop $7
    System::Call 'KERNEL32::GetLocalTime(i .r8)'
    Pop $8
    StrCpy $0 $3
    StrCpy $1 $4
    StrCpy $2 $5
    StrCpy $3 $6
    StrCpy $4 $7
    StrCpy $5 $8
FunctionEnd
