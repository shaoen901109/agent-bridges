# 給 AI agent 的使用說明（CLI 版）

這份文件是給**沒有 MCP 支援、或偏好直接跑腳本**的 agent 看的。
有 MCP 的 agent 請直接用 `revit_*` 那組 tool（工具對照見 `README.md` §5）。

---

## 0. 前置條件（每次開工先確認）

1. Revit 2024 已開啟，且已安裝 RevitAgentBridge 外掛（Revit 啟動時自動開伺服器；第一次載入要選「永遠載入」）
2. 沒開 Revit 時可以執行 `launch`：它會啟動 Revit 並等到橋接器就緒（Revit 啟動很慢，預設等 180 秒）

連不上時（離開碼 `2`，訊息以 `REVIT_NOT_AVAILABLE` 開頭）：先試 `launch`；
若 `launch` 回報 `ready: false` 並列出 `revit_windows`，多半是**授權 / 登入 / 更新對話框**或
**「是否載入此外掛」的安全性對話框**擋住——**停下來請使用者處理**。
不要結束 Revit 行程、不要試圖繞過對話框。
請求逾時也一樣：**Revit 可能停在對話框，請使用者處理**（用 `window-shot` 看內容再轉告）。

---

## 1. 指令基本形式

```powershell
$RV = "$env:USERPROFILE\RevitAgentBridge\venv\Scripts\python.exe"
& $RV -m revit_bridge <子指令> [選項]
```

- 所有輸出都是 **JSON**（截圖類輸出存檔路徑）
- **長度單位一律公釐 mm、角度用度**（`eval` 例外：C# 片段內是英呎，見 §3）
- 元件一律用 **ElementId 整數**（`elements` 可列出、建立指令會回傳）
- 離開碼：`0` 成功 / `1` 指令失敗 / `2` 連不上 Revit / `3` 截圖失敗

---

## 2. 建議的建模工作流程

```powershell
# ① 先了解現況——不要在不知道專案內容的情況下動手
& $RV -m revit_bridge info
& $RV -m revit_bridge scene

# ② 一次只做一個可理解的動作，記下回傳的 id
& $RV -m revit_bridge wall --start 0,0 --end 5000,0 --level "Level 1" --height 3000
#   → 回傳新牆的 id

# ③ 一定要看結果，不要假設建對了
& $RV -m revit_bridge view --view 3d --zoom-extents
& $RV -m revit_bridge screenshot --out C:\temp\v.png
#   → 然後真的去「看」這張圖

# ④ 不對就退回，不要在錯誤的結果上疊加
& $RV -m revit_bridge undo       # 非同步：等一下再用 info / screenshot 確認
```

每個修改指令都是**一個 Transaction**，一次 `undo` 剛好回復一步。

---

## 3. 何時用捷徑、何時用 eval

- 牆 / 樓板 / 樓層 / 放族群 / 改參數 / 選取 / 刪除 / 視圖：用捷徑子指令
  （`wall`、`floor`、`level`、`place`、`set-param`、`select`、`delete`、`view`）
- 查詢：`elements`（可依 `--category`、`--type-name`、`--level`、`--name-contains` 過濾，`--types-only` 列類型）、
  `element <id>`（單一元件全部參數）
- 其他一切（統計、批次改參數、標註、過濾器、幾何運算）：用 `eval` 寫 **C#（Revit API）**

### eval 的輸入方式

```powershell
# 方式 A：寫檔（建議，中文不會被命令列吃掉）
& $RV -m revit_bridge eval --file C:\temp\job.cs

# 方式 B：stdin
'return doc.Title;' | & $RV -m revit_bridge eval -

# 不要自動包 Transaction（自己管理交易、存檔、ExportImage 時）
& $RV -m revit_bridge eval --file C:\temp\job.cs --no-transaction
```

### eval 的程式碼守則

```csharp
// 這是 C# 5：沒有 ?. / $"" / nameof / => 成員 / out var
// 可直接用：uiapp, uidoc, doc, print(x)
// 預設 using：System, System.Collections.Generic, System.Linq, Autodesk.Revit.DB, Autodesk.Revit.UI
var walls = new FilteredElementCollector(doc).OfClass(typeof(Wall)).ToElements();
print("牆數量：" + walls.Count);
return walls.Count;      // 沒 return 會自動補 return null;
```

- **單位是 Revit 內部的英呎**：1 ft = 304.8 mm。`UnitUtils.ConvertToInternalUnits(3000, UnitTypeId.Millimeters)` 轉進去，
  `ConvertFromInternalUnits(v, UnitTypeId.Millimeters)` 轉出來。
- 預設整段包在一個 Transaction 內（失敗自動 RollBack），片段內不要自己 `new Transaction`
- 編譯錯誤回「片段內的行號」；執行期錯誤沒有行號，請用 `print` 除錯
- 常見範例見 `skill\revit-control\references\csharp-snippets.md`

---

## 4. 安全守則

- **不可未經使用者明確同意儲存、覆蓋或開啟檔案**。
  `model save` 需要 `--confirm`；`model save_as` 覆寫既有檔案需要 `--overwrite`；
  `model new` / `open` 需要 `--confirm`（有未存變更時先問使用者，`info` 的 `document` 會告知）。
  這些旗標只有在使用者親口要求時才加。
- 不要動使用者既有的元件，除非他明確要求；`delete` 前先用 `element <id>` 確認，並注意 Revit 會連帶刪除相依元件。
- **不要關閉或強制結束 Revit**；沒有這類工具。也不要試圖自動關閉對話框。
- Revit 跳出模態對話框時，請求會逾時——用 `window-shot` 看內容，請使用者處理。

---

## 5. 常見回應欄位

| 欄位 | 意義 |
|---|---|
| `id` / `ids` | Revit ElementId 整數——**後續操作都用它** |
| `elements[].bbox_mm` | 外框，單位公釐（`elements --bbox`） |
| `total`（`elements`） | 符合條件的總數；用 `--limit` / `--offset` 分頁 |
| `warnings` | 交易中被自動略過的 Revit 警告 |
| `stale`（`info`） | `true` 表示 Revit 忙碌或停在對話框，這是快取資訊 |
| `output`（`eval`） | `print` 的輸出 |
| `notes`（`window-shot`） | 說明實際用了哪種擷取方式 |

---

## 6. 完整子指令清單

執行 `& $RV -m revit_bridge --help` 取得最新清單，或
`& $RV -m revit_bridge commands` 取得伺服器端支援的全部協定指令；
`raw <method> --params '{...}'` 可直接送任意協定指令。
