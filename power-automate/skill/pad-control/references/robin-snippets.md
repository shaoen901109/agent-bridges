# Robin 常用片段（傳統語法，PAD 2.72 實測可解析）

以下每段都在 2026-10-14 用 `pad_update_flow` 整段貼進 PAD 驗證過（設計工具錯誤清單為 0），只保證語法，沒有逐一執行。
表內沒有的動作，用 `pad_action_template("中文動作名")` 取得 PAD 產生的寫法，不要猜。

## 變數、條件、迴圈

```
SET Counter TO 0
LOOP WHILE (Counter) < (3)
    Variables.IncreaseVariable Value: Counter IncrementValue: 1
END
IF Counter = 3 THEN
    SET Msg TO $'''done'''
ELSE IF Counter > 3 THEN
    SET Msg TO $'''over'''
ELSE
    SET Msg TO $'''less'''
END
```

```
LOOP LoopIndex FROM 1 TO 5 STEP 1
    WAIT 0.1
END
```

```
SET N TO 21
SET B TO True
SET A TO $'''測試'''
SET Doubled TO N * 2
IF B = True THEN
    SET Summary TO $'''%A% / %Doubled% / yes'''
END
```

## 清單與文字

```
SET Source TO $'''a-b'''
SET Raw TO $'''12'''
Variables.CreateNewList List=> List
Variables.AddItemToList Item: $'''a''' List: List
Text.SplitText.SplitWithDelimiter Text: $'''a,b''' CustomDelimiter: $''',''' IsRegEx: False Result=> TextList
SET First TO TextList[0]
SET Count TO TextList.Count
Text.Replace.ReplaceText Text: Source TextToFind: $'''-''' IgnoreCase: False ReplaceWith: $'''_''' ActivateEscapeSequences: False ComparisonType: Text.TextComparisonType.CultureSensitive Result=> Replaced
Text.ToNumber Text: Raw Number=> TextAsNumber
```

## 日期時間

```
DateTime.GetCurrentDateTime.Local DateTimeFormat: DateTime.DateTimeFormat.DateAndTime CurrentDateTime=> CurrentDateTime
Text.ConvertDateTimeToText.FromCustomDateTime DateTime: CurrentDateTime CustomFormat: $'''yyyy-MM-dd HH:mm''' Result=> FormattedDateTime
```

## 檔案與資料夾

```
File.ReadTextFromFile.ReadText File: $'''C:\\temp\\x.txt''' Encoding: File.TextFileEncoding.UTF8 Content=> FileContents
File.WriteText File: $'''C:\\temp\\y.txt''' TextToWrite: FileContents AppendNewLine: True IfFileExists: File.IfFileExists.Overwrite Encoding: File.FileEncoding.UTF8
Folder.GetFiles Folder: $'''C:\\temp''' FileFilter: $'''*.txt''' IncludeSubfolders: False FailOnAccessDenied: True SortBy1: Folder.SortBy.NoSort SortDescending1: False SortBy2: Folder.SortBy.NoSort SortDescending2: False SortBy3: Folder.SortBy.NoSort SortDescending3: False Files=> Files
LOOP FOREACH CurrentItem IN Files
    SET Name TO CurrentItem.Name
END
```

## 指令碼與程式

```
Scripting.RunPowershellScript.RunScript Script: $'''Get-Date''' ScriptOutput=> PowershellOutput
Scripting.RunDOSCommand.RunDOSCommand DOSCommandOrApplication: $'''dir''' WorkingDirectory: $'''C:\\''' StandardOutput=> CommandOutput StandardError=> CommandErrorOutput ExitCode=> CommandExitCode
Scripting.RunPythonScript.RunPythonScript PythonCode: $'''print(1)''' ScriptOutput=> PythonScriptOutput
System.RunApplication.RunApplication ApplicationPath: $'''notepad.exe''' WindowStyle: System.ProcessWindowStyle.Normal ProcessId=> AppProcessId
```

## 網路與 JSON

```
Web.InvokeWebService.InvokeWebService Url: $'''https://example.com''' Method: Web.Method.Get Accept: $'''application/json''' ContentType: $'''application/json''' ConnectionTimeout: 30 FollowRedirection: True ClearCookies: False FailOnErrorStatus: False EncodeRequestBody: False UserAgent: $'''Mozilla/5.0''' Encoding: Web.Encoding.AutoDetect AcceptUntrustedCertificates: False ResponseHeaders=> WebServiceResponseHeaders Response=> WebServiceResponse StatusCode=> StatusCode
Variables.ConvertJsonToCustomObject Json: WebServiceResponse CustomObject=> JsonAsCustomObject
SET A TO JsonAsCustomObject['a']
```

## Excel

```
Excel.LaunchExcel.LaunchAndOpenUnderExistingProcess Path: $'''C:\\temp\\a.xlsx''' Visible: True ReadOnly: False UseMachineLocale: False Instance=> ExcelInstance
Excel.ReadFromExcel.ReadCell Instance: ExcelInstance StartColumn: $'''A''' StartRow: 1 GetCellContentsMode: Excel.GetCellContentsMode.TypedValues CellValue=> ExcelData
Excel.CloseExcel.Close Instance: ExcelInstance
```

## 剪貼簿、訊息、滑鼠鍵盤

```
Clipboard.GetText Text=> ClipboardText
Clipboard.SetText Text: $'''hello'''
Display.ShowMessageDialog.ShowMessage Title: $'''標題''' Message: $'''內容''' Icon: Display.Icon.None Buttons: Display.Buttons.OK DefaultButton: Display.DefaultButton.Button1 IsTopMost: False ButtonPressed=> ButtonPressed
MouseAndKeyboard.SendKeys.FocusAndSendKeys TextToSend: $'''{Control}({A})''' DelayBetweenKeystrokes: 10 SendTextAsHardwareKeys: False
MouseAndKeyboard.SendMouseClick.ClickAt ClickType: MouseAndKeyboard.MouseClickType.LeftClick MillisecondsDelay: 0 X: 100 Y: 200 RelativeTo: MouseAndKeyboard.PositionRelativeTo.Screen MovementStyle: MouseAndKeyboard.MovementStyle.Instant
```

`ShowMessage` 會停住流程等人按，自動化流程裡不要用（agent 觸發時沒人按就一直卡著）。

## 錯誤處理

```
Text.ToNumber Text: $'''abc''' Number=> N
ON ERROR
    SET N TO 0
END
```
