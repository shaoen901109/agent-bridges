---
name: revit-control
description: 透過 Agent Bridge 操控本機的 Revit 2024——查詢專案與元件、建牆／樓板／樓層、放置門窗與族群、讀寫參數、切換視圖、匯出視圖截圖與全視窗截圖來驗證結果，並能用 C#（Revit API）片段處理工具涵蓋不到的事。當使用者要求操作 Revit、在 Revit 裡建模／改模型、查看或描述 Revit 目前畫面、處理 .rvt 檔案內容、查詢元件或參數、統計數量，或提到「幫我在 Revit…」「BIM」「族群」「牆」「樓板」時使用本技能。內容為工作流程與 C# 寫法守則，不重複 MCP tool 的參數說明。
---

# Revit 2024 代理操控

MCP server 名 `revit`，工具前綴 `revit_`。
（部分 MCP 客戶端可能顯示成 `mcp_revit_<tool_name>`，照你看到的名稱呼叫即可。）

## 前置條件（每次開工先確認）

1. 先呼叫 `revit_get_info` 確認連線，並記下：目前文件（有沒有開專案）、作用中視圖、有無未存變更。
2. 外掛隨 Revit 啟動自動開伺服器（127.0.0.1:9879）；也可在功能區「Agent Bridge」頁籤手動啟動／停止／看狀態。

連不上時（回應開頭是 `REVIT_NOT_AVAILABLE`）：
- Revit 沒開 → 呼叫 `revit_launch`（可帶 `path` 開 .rvt；它會等橋接器就緒，Revit 啟動很慢，要一兩分鐘）。
- 回 `ready=false` 並列出視窗標題 → 多半是授權／登入／更新對話框，或第一次載入外掛的安全性對話框
  （請使用者選「永遠載入」）擋住，**停下來請使用者處理**。
- 請求逾時、`get_info` 回 `stale=true` → **Revit 可能停在對話框，請使用者處理**。可用 `revit_window_screenshot`
  看對話框內容，再把內容轉告使用者。
- **不要**強制結束 Revit 行程（也沒有這類工具），不要試圖關閉或繞過對話框。

## 標準工作流程

```
get_info → get_scene_info → 小步操作 → set_view + view_screenshot 驗證 → 不對就 undo → 下一步
```

- **先看清楚再動手**：`revit_get_scene_info` 看樓層（含標高）、視圖、各類別元件數、牆／樓板類型；
  要找元件 id 用 `revit_list_elements`（可依 category／type_name／level／name_contains 過濾、分頁）；
  `revit_get_element` 看單一元件的全部參數。
- **小步前進**：一次只做一個可理解的動作，記下回傳的元件 id。
- **每次改完就驗證**：`revit_set_view(view="3d", zoom_extents=True)` 然後 `revit_view_screenshot()`。眼見為憑。
- **錯了就退回**：`revit_undo`。每個橋接器的修改指令（含整段 eval）是一個 Transaction，一次 undo 剛好回復一步。
  undo 是非同步投遞（PostCommand），回傳時可能還沒完成，退完用 `revit_get_info`／截圖確認。
- 工具涵蓋不到的 → `revit_eval_csharp`（見下）。

### 視圖截圖 vs 全視窗截圖

| 情況 | 用哪個 |
|---|---|
| 驗證模型長怎樣（**九成情況**） | `revit_view_screenshot`（匯出作用中視圖，只含繪圖區） |
| 看功能區、專案瀏覽器、開著的對話框、懷疑 Revit 卡住 | `revit_window_screenshot` |

視圖截圖只能匯出圖形視圖（平面／立面／剖面／3D／圖紙），明細表不行。

## 單位（最容易出錯）

- **所有外部工具（`revit_create_wall`、座標、高度、`revit_set_parameter` 的長度值…）一律公釐 mm、角度用度。**
- **`revit_eval_csharp` 裡是 Revit 內部單位：長度為英呎（ft）。1 ft = 304.8 mm。**
  - mm → ft：`UnitUtils.ConvertToInternalUnits(3000, UnitTypeId.Millimeters)`，或直接 `3000 / 304.8`。
  - ft → mm：`UnitUtils.ConvertFromInternalUnits(v, UnitTypeId.Millimeters)`。
  - 角度內部是弧度；面積是平方英呎、體積是立方英呎。回傳給使用者之前先換算單位並標明。
- 專案顯示單位（可能是公分或公尺）和內部單位無關；使用者講「3 公尺」就是 3000 mm。

## revit_eval_csharp 寫法守則

片段會被包進 `public static object Run(UIApplication uiapp, UIDocument uidoc, Document doc, Action<object> print)`，
可直接使用這四個名稱。預設 using：System、System.Collections.Generic、System.Linq、Autodesk.Revit.DB、Autodesk.Revit.UI。
需要其他命名空間，把 `using System.IO;` 寫在片段最上面。

1. **只能 C# 5 語法**：沒有 `?.`、`??=`、字串插值 `$"..."`、`nameof`、運算式主體成員（`=> x`）、`out var`、`is` 模式比對、
   區域函式、tuple。字串用 `string.Format` 或 `+` 串接；`out` 變數要先宣告。
2. **要回傳資料就 `return` 它**；沒 return 會自動補 `return null;`。結果自動序列化：ElementId→整數、XYZ→[x,y,z]、
   Element→{id,name,category}、集合→陣列，其餘用 ToString。中間過程用 `print(x)` 輸出。
3. **交易**：預設 `transaction=true`，整段包在一個 Transaction 內、失敗自動 RollBack，片段內**不要**自己 `new Transaction`。
   純查詢也沒關係（空交易會被丟棄）。需要自己管理交易（TransactionGroup、`doc.Save`、`ExportImage`、開關文件）才傳
   `transaction=false`。
4. **一個片段做一件事**，先小後大：先用查詢片段確認選到的元件對不對，再做修改。大量修改前先 `print` 筆數。
5. **編譯錯誤**會回「片段內的行號」與訊息，照著修；**執行期錯誤沒有行號**，用 `print` 逐段除錯。
   錯誤訊息裡的 `AgentEval.Snippet.Run` 是包裝，不是你寫的。
6. 型別／名稱要查證，不要憑印象：用 `FilteredElementCollector` 找出實際存在的類型名稱再用。
7. **ElementId**：Revit 2024 的 `ElementId.Value` 是 `long`（`IntegerValue` 是舊寫法，已過時）。
8. 參數寫入前先檢查 `param.IsReadOnly`；`Parameter.Set` 型別要對（長度用 double ft、文字用 string、是非用 int 0/1）。
9. 不要在片段裡 `doc.Delete` 使用者既有的元件、不要存檔或關檔，除非使用者明確要求。
10. 需要常見範例（牆、樓板、門窗、過濾器、參數）→ 讀 `references/csharp-snippets.md`。

## 常見建模注意事項

- 牆：`revit_create_wall(start, end, level, height, wall_type)`，座標 mm、z 由樓層決定。
  牆類型名稱可用「類型名」或「族群名 : 類型名」；先 `revit_list_elements(category="Walls", types_only=True)` 查。
- 樓板：`revit_create_floor(points, level, floor_type)`，多邊形頂點依序給（不用重複首點）。
- 門窗：先確認族群已載入（`revit_list_elements(category="Doors", types_only=True)`）；
  `revit_place_family_instance` 的點要落在牆線 600 mm 內，會自動找最近的牆當宿主，也可用 `host_id` 指定；
  窗台高度之後用 `revit_set_parameter` 設定。
- 新樓層：`revit_create_level(elevation, name)`；API 建的樓層預設沒有平面視圖，工具預設會一併建立。
- 型別參數（Type Parameter）會影響所有同型別元件，`revit_set_parameter` 的回應會註明，改之前先想清楚。

## 安全規則

- **未經使用者明確同意，不要儲存、覆蓋、開新檔或開啟別的檔案。** `revit_model_file` 的 new／open／save 需要
  `confirm=true`、另存覆蓋需要 `overwrite=true`；有未存變更時先問使用者。
- 刪除（`revit_delete_elements`）是破壞性操作：先用 `revit_get_element` 確認 id，只刪自己剛建立的，
  並注意 Revit 會連帶刪除相依元件（牆上的門窗）。
- 只綁 127.0.0.1；不要把橋接器埠號對外開放。

## 常見錯誤對照

| 症狀 | 可能原因與做法 |
|---|---|
| 開頭 `REVIT_NOT_AVAILABLE` | Revit 沒開或外掛沒載入：`revit_launch`；仍不行請使用者確認 Agent Bridge 頁籤是否存在、是否已選「永遠載入」 |
| 逾時／`stale=true` | Revit 停在對話框（或正在重生成很久）。`revit_window_screenshot` 看一下，請使用者處理 |
| `REVIT_COMMAND_FAILED` 找不到樓層／類型 | 名稱要完全一致（含空格大小寫）；先用 `get_scene_info`／`list_elements(types_only=True)` 查 |
| 門窗放不上去 | 點離牆太遠（>600 mm）、族群未載入、或該族群需要宿主；改用 `host_id` |
| 編譯錯誤 `CS1056`／`CS1002`… | 多半是用了 C# 6+ 語法（`$""`、`?.`、`=>`），改成 C# 5 寫法 |
| 長度數值差 304.8 倍 | eval 裡誤把 mm 當成英呎，或反過來；見上方「單位」 |
| 截圖是空白或不是想看的視圖 | 先 `revit_set_view` 切視圖並 `zoom_extents`；明細表不能匯出 |
| 修改沒有出現在畫面 | 視圖尚未重繪或視圖範圍／可見性設定隱藏了它；用 `get_element` 確認元件存在，再截圖 |
| 警告被吞掉 | 橋接器的交易會自動略過警告並在回應 `warnings` 列出；有錯誤則整筆 RollBack |
