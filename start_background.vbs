Set FSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

' Lay duong dan thu muc chua script nay
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

' Chay truc tiep python trong .venv o che do an hoan toan (window style 0, khong hien man hinh den)
pythonExe = scriptDir & "\.venv\Scripts\python.exe"
If FSO.FileExists(pythonExe) Then
    cmd = """" & pythonExe & """ -m src.main"
Else
    cmd = "python -m src.main"
End If

WshShell.Run cmd, 0, False
