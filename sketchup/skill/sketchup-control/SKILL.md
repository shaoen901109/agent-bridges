---
name: sketchup-control
description: 透過 Agent Bridge 操控本機執行中的 SketchUp 2024——查詢模型、用 Ruby API 建模與修改幾何、切換工具、擷取視埠與全視窗截圖來驗證結果。當使用者要求操作 SketchUp、在 SketchUp 裡建模／改模型／畫圖、查看或描述 SketchUp 目前畫面、處理 .skp 檔案內容，或提到「幫我在 SketchUp…」時使用本技能。內容為工作流程與 Ruby API 守則，不重複 MCP tool 的參數說明。
---

# SketchUp 代理操控

## 前置條件（每次開工先確認）

1. SketchUp 2024 必須**已經由使用者開啟**。
2. 使用者必須在 SketchUp 內手動啟動橋接伺服器：
   **延伸程式(Extensions) → Agent Bridge → 啟動伺服器**。伺服器預設關閉。
3. 先呼叫 `sketchup_get_info` 確認連線。

連不上時（回應開頭是 `SKETCHUP_NOT_AVAILABLE`）：**停下來請使用者處理**，明確說出上面第 1、2 步。
**不要**自行啟動 SketchUp、不要結束 SketchUp 行程、不要嘗試改設定繞過。
（你沒有辦法從外部啟動伺服器——這是刻意的安全設計。）

## 標準工作流程

```
get_info → get_scene_info → 小步操作 → 視埠截圖驗證 → 不對就 undo → 下一步
```

- **先看清楚再動手**：`sketchup_get_scene_info` 了解實體統計、圖層/標籤、材質、元件、
  選取集與整體 bounding box。要拿實體的 `persistent_id` 用 `sketchup_list_entities`。
- **小步前進**：一次只做一個可理解的動作，不要一口氣送出上百行 Ruby。
- **每次改完就驗證**：`sketchup_set_camera(view="iso", zoom_extents=True)` 之後
  `sketchup_viewport_screenshot()`。眼見為憑，不要假設幾何長對了。
- **錯了就退回**：`sketchup_undo_redo(direction="undo")`。每個橋接器指令都是一個
  operation，一次 undo 剛好回復一步。不要在錯誤幾何上繼續疊加。
- **注意非同步**：`sketchup_send_action` 與 `sketchup_undo_redo` 走 SketchUp 自己的
  訊息佇列，回應中的狀態是**送出前**的（欄位叫 `active_tool_before`）。
  要確認結果請下一個 tool 呼叫再查一次（例如 `sketchup_list_entities`），
  不要把回應裡的數字當成已完成的結果。

### 視埠截圖 vs 全視窗截圖

| 情況 | 用哪個 |
|---|---|
| 驗證幾何、看模型長怎樣（**九成情況**） | `sketchup_viewport_screenshot` |
| 要看工具列/面板狀態、目前啟用的工具 | `sketchup_window_screenshot` |
| 懷疑有對話框擋住（指令逾時、沒有回應） | `sketchup_window_screenshot` |
| 使用者說「看一下我的畫面」 | `sketchup_window_screenshot` |

全視窗截圖是 OS 層擷取，**只能看，不能點**——本橋接器不模擬滑鼠鍵盤。
需要使用者點的東西，請直接請使用者點。

## 捷徑 tool vs `sketchup_eval_ruby`

先用捷徑 tool（語意清楚、參數會被檢查、自動包 undo operation）：
`sketchup_create_box`、`sketchup_create_face`、`sketchup_push_pull`、
`sketchup_transform_entities`、`sketchup_set_material`、`sketchup_delete_entities`、
`sketchup_select_entities`、`sketchup_set_camera`、`sketchup_send_action`。

捷徑做不到時才用 `sketchup_eval_ruby`：陣列複製、布林運算、掃掠、
遍歷既有幾何、建立元件定義、場景頁面、標籤操作等。
常用片段見 [references/ruby-snippets.md](references/ruby-snippets.md)。

## `eval_ruby` 寫法守則

1. **包 operation**：會改模型的程式碼一定要寫
   `model.start_operation("動作名稱", true)` … `model.commit_operation`，
   否則使用者要按很多次 Ctrl+Z 才能復原。
2. **單位**：SketchUp 內部長度是**英吋**。一律明確轉換——寫 `3000.mm`、`2.5.m`、
   `10.cm`，不要寫裸數字（裸數字 = 英吋）。捷徑 tool 的 `units` 參數預設就是 `mm`。
3. **用 persistent_id 追蹤實體**：`entityID` 會因存檔重開而改變，`persistent_id` 不會。
   從 id 取回實體：`model.find_entity_by_persistent_id(id)`。
4. **面的方向**：`add_face` 的法線方向不保證；要確定朝向時檢查 `face.normal.z` 並視需要
   `face.reverse!`。SketchUp 的「反面」（背面）呈現藍灰色，渲染與出圖都會有問題。
5. **push_pull 正負**：`face.pushpull(d)` 沿**法線方向**擠出，`d` 為負則反向。
   擠出方向不如預期時，先看 `face.normal`，不要盲目改符號。
6. **群組／元件內部**：`group.entities` 與 `instance.definition.entities` 才是內部幾何；
   直接在 `model.entities` 加東西會和使用者既有幾何黏在一起。新建幾何預設包成 Group
   （捷徑 tool 的 `group=True`）。元件是共用定義——改一個定義等於改全部實例。
7. **不要大規模破壞**：絕不執行 `model.entities.clear!`、`entities.to_a.each(&:erase!)`
   這類一次刪光的程式碼。刪除前先 `sketchup_list_entities` 確認範圍，只刪自己建立的東西。
8. **大模型不要全部傾印**：不要 `puts model.entities.to_a.inspect`。用計數、篩選、
   `first(n)`，或用 `sketchup_list_entities` 的 `limit` / `offset` 分頁。
9. **只做 SketchUp 相關的事**：`eval_ruby` 技術上能執行任何 Ruby（含檔案系統、網路）。
   只用它呼叫 SketchUp API。需要動檔案請走一般的檔案工具並讓使用者看得見。

## 安全守則

- **不可未經使用者明確同意儲存或覆蓋 .skp**。`sketchup_model_file` 的
  `action="save"` 需要 `confirm=True`（存回原路徑）或 `overwrite=True`（覆寫既有檔案）——
  這些旗標只有在使用者親口要求時才加。
- **開新檔/開別的檔前先確認未存變更**：`sketchup_get_info` 的 `model_modified` 為 true 時，
  先問使用者要不要存，再決定是否 `action="new"` / `"open"`（兩者都需要 `confirm=True`）。
- 不要動使用者既有的幾何、圖層、材質，除非他明確要求。
- 不要嘗試啟動/關閉 SketchUp 或改它的授權設定。

## 備援：MCP 不可用時改用 CLI

同一份 Python 函式庫也提供 CLI，輸出 JSON：

```powershell
$SK = "$env:USERPROFILE\SketchUpAgentBridge\venv\Scripts\python.exe"
& $SK -m sketchup_bridge ping
& $SK -m sketchup_bridge info
& $SK -m sketchup_bridge scene --limit 50
& $SK -m sketchup_bridge entities --scope all --types Face --limit 20
& $SK -m sketchup_bridge tool pushpull            # 短名稱：select/line/rectangle/pushpull/move/…
& $SK -m sketchup_bridge box --width 3000 --depth 2000 --height 2800 --name "測試量體"
& $SK -m sketchup_bridge set-camera --view iso --zoom-extents
& $SK -m sketchup_bridge screenshot --out C:\temp\view.png --max-edge 1280
& $SK -m sketchup_bridge window-shot --out C:\temp\window.png
& $SK -m sketchup_bridge undo
```

Ruby 程式碼含引號時，**一律用檔案或 stdin**，不要塞進命令列（Windows 引號地獄）：

```powershell
& $SK -m sketchup_bridge eval --file C:\temp\code.rb
Get-Content C:\temp\code.rb -Raw | & $SK -m sketchup_bridge eval -
```

離開碼：`0` 成功、`1` 指令執行失敗、`2` 連不上伺服器、`3` 截圖失敗。

更多 Ruby 片段：[references/ruby-snippets.md](references/ruby-snippets.md)
