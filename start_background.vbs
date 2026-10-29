Set FSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

' Lay duong dan thu muc chua script nay
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

' Chay start.bat o che do an hoan toan (window style 0, false = khong can doi)
batPath = scriptDir & "\start.bat"
WshShell.Run "cmd.exe /c """ & batPath & """", 0, False
