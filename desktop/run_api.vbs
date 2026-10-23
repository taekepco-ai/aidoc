' API 모드 무음 런처 - HF Space (taeaiai/gongmun-api) 호출
' Space 이름을 다르게 지었으면 아래 URL만 수정

Option Explicit

Dim fso, sh, baseDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = baseDir

sh.Environment("PROCESS").Item("SLM_API_URL") = "https://taeaiai-gongmun-api.hf.space"

If Not fso.FileExists(baseDir & "\gen.py") Then
    MsgBox "gen.py 파일을 찾을 수 없습니다." & vbCrLf & baseDir, vbCritical, "공문 생성기"
    WScript.Quit 1
End If

On Error Resume Next
sh.Run "pythonw.exe gen.py", 0, False
If Err.Number <> 0 Then
    sh.Run "python.exe gen.py", 0, False
End If