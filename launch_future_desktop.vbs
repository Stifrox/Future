Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1Path = fso.BuildPath(scriptDir, "start_future_server.ps1")

cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File """ & ps1Path & """"
shell.Run cmd, 0, False
