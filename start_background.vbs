Set FSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

' Lay duong dan thu muc chua script nay
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

' Chay start.bat o che do an hoan toan (window style 0, false = khong can doi)
WshShell.Run "cmd /c start.bat", 0, False
