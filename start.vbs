' Запуск игры совсем без консольного окна (двойной клик по этому файлу).
' Аргументы не передаются: для ключей используйте start.bat в командной строке.
Dim shell, fso, folder, exe
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder

exe = FindExe("pyw.exe")
If exe <> "" Then
    shell.Run """" & exe & """ -3.13 """ & folder & "\main.py""", 0, False
Else
    exe = FindExe("pythonw.exe")
    If exe <> "" Then
        shell.Run """" & exe & """ """ & folder & "\main.py""", 0, False
    Else
        MsgBox "Не найден pyw.exe или pythonw.exe. Установите Python 3.13.", 16, "Сапёр"
    End If
End If

Function FindExe(exeName)
    Dim paths, p, candidate
    paths = Split(shell.ExpandEnvironmentStrings("%PATH%"), ";")
    For Each p In paths
        If p <> "" Then
            candidate = p & "\" & exeName
            If fso.FileExists(candidate) Then
                FindExe = candidate
                Exit Function
            End If
        End If
    Next
    FindExe = ""
End Function
