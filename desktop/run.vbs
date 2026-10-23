' Silent launcher (no cmd window)
Option Explicit

Dim fso, sh, baseDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = baseDir

If Not fso.FileExists(baseDir & "\gen.py") Then
    MsgBox "gen.py 파일을 찾을 수 없습니다." & vbCrLf & baseDir, vbCritical, "공문 생성기"
    WScript.Quit 1
End If

On Error Resume Next
sh.Run "pythonw.exe gen.py", 0, False
If Err.Number <> 0 Then
    Err.Clear
    sh.Run "python.exe gen.py", 0, False
    If Err.Number <> 0 Then
        MsgBox "Python을 찾을 수 없습니다." & vbCrLf & "Python 설치 후 다시 시도해 주세요.", vbCritical, "공문 생성기"
        WScript.Quit 1
    End If
End If