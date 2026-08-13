' Launches the monitor with no console window.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

script = fso.GetParentFolderName(WScript.ScriptFullName) & "\claude_monitor.py"

On Error Resume Next
sh.Run """pythonw.exe"" """ & script & """", 0, False
If Err.Number <> 0 Then
    Err.Clear
    ' fall back to the py launcher's windowed variant
    sh.Run """pyw.exe"" """ & script & """", 0, False
    If Err.Number <> 0 Then
        MsgBox "Claude Monitor needs Python 3 available on your PATH." & vbCrLf & _
               "Install it from python.org, then try again.", 48, "Claude Monitor"
    End If
End If
