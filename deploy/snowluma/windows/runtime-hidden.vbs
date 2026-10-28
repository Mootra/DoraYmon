Option Explicit
Dim files, shell, root, quote, command, action
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = files.GetParentFolderName(WScript.ScriptFullName)
quote = Chr(34)
action = "Check"
If WScript.Arguments.Count > 0 Then action = WScript.Arguments(0)
Select Case action
    Case "Start", "Check", "Stop"
    Case Else
        WScript.Quit 2
End Select
command = quote & shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe") & quote & " -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & quote & files.BuildPath(root, "runtime.ps1") & quote & " -Action " & action
shell.Run command, 0, False
