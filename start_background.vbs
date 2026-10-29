Set FSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

' Lay duong dan thu muc chua script nay
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

' Chay python trong .venv qua cmd an (style 0: an hoan toan khong hien man hinh den)
pythonExe = scriptDir & "\.venv\Scripts\python.exe"
If FSO.FileExists(pythonExe) Then
    cmd = "cmd.exe /c """"" & pythonExe & """ -m src.main"""
Else
    cmd = "cmd.exe /c python -m src.main"
End If

WshShell.Run cmd, 0, False
