' Gemini API 모드 실행 런처
' API 키는 GEMINI_API_KEY 환경변수 또는 desktop\gemini_key.txt 파일에서 읽음
' (둘 다 없으면 실행 시 입력 창이 뜨고, 입력한 키는 파일로 저장됨)

Option Explicit

Dim fso, sh, baseDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = baseDir

sh.Environment("PROCESS").Item("SLM_BACKEND") = "gemini"
' 모델을 바꾸려면 아래 주석을 해제하고 수정하세요
' sh.Environment("PROCESS").Item("GEMINI_MODEL") = "gemini-2.5-pro"

If Not fso.FileExists(baseDir & "\gen.py") Then
    MsgBox "gen.py 파일을 찾을 수 없습니다." & vbCrLf & baseDir, vbCritical, "실행 오류"
    WScript.Quit 1
End If

On Error Resume Next
sh.Run "pythonw.exe gen.py", 0, False
If Err.Number <> 0 Then
    Err.Clear
    sh.Run "python.exe gen.py", 0, False
    If Err.Number <> 0 Then
        MsgBox "Python을 찾을 수 없습니다." & vbCrLf & "Python 설치 후 다시 시도해 주세요.", vbCritical, "실행 오류"
        WScript.Quit 1
    End If
End If