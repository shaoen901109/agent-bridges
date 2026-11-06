---
name: blender-control
description: 透過 Agent Bridge 操控本機執行中的 Blender 3.4——查詢場景、用 bpy Python 建模與修改物件、切換視埠視角、擷取視埠與全視窗截圖來驗證結果、算圖、開檔存檔。當使用者要求操作 Blender、在 Blender 裡建模／改模型／調整物件、查看或描述 Blender 目前畫面、處理 .blend 檔案內容、算一張圖，或提到「幫我在 Blender…」時使用本技能。內容為工作流程與 bpy 寫法守則，不重複 MCP tool 的參數說明。
---

# Blender 代理操控

## 前置條件（每次開工先確認）

1. Blender 3.4 必須**已經由使用者開啟**（GUI 模式）。
2. 橋接伺服器是 **startup 腳本**（`blender_bridge.py`），Blender 一啟動就自動監聽
   `127.0.0.1:9877`。**不需要、也不能**從外部手動啟動——這點和 SketchUp 不同
   （SketchUp 要使用者從選單啟動伺服器；Blender 只要開著就在）。
   剛安裝或更新腳本後，要**重啟一次 Blender** 才生效。
3. 先呼叫 `blender_ping` 確認連線，順便看 `file`（目前檔案）與 `is_dirty`（有無未存變更）。

連不上時（回應開頭是 `BLENDER_NOT_AVAILABLE:`）：**停下來請使用者處理**，明確說出：
「請確認 Blender 已開啟；若剛安裝/更新過橋接腳本，請重新啟動 Blender。」
**不要**自行啟動 Blender、不要結束或重啟 Blender 行程、不要嘗試改設定繞過。
埠被改過時可設環境變數 `BLENDER_BRIDGE_PORT`（Blender 端與 client 端都讀它）。

## 標準工作流程

```
ping → get_scene_info → 小步操作 → set_view + viewport_screenshot 驗證 → 不對就 undo → 下一步
```

- **先看清楚再動手**：`blender_get_scene_info` 看單位、物件統計、collection 樹、選取、
  active、目前引擎與相機。要逐一列物件用 `blender_list_objects`（可用 `name_contains`、
  `types`、`limit`/`offset` 分頁）。**物件名稱**就是後續操作的識別碼。
- **小步前進**：一次只做一個可理解的動作，不要一口氣送出上百行 bpy。
- **每次改完就驗證**：`blender_set_view(view="iso", zoom_extents=True, shading="solid")`
  之後 `blender_viewport_screenshot()`。眼見為憑，不要假設幾何長對了。
- **錯了就退回**：`blender_undo_redo(direction="undo")`。橋接器的每個修改指令
  （`eval_python` / `transform_objects` / `delete_objects`）在改完後都 `undo_push` 一步，
  一次 undo 剛好回復一步。回應含 `before`/`after` 的物件清單，可直接確認。
  不要在錯誤幾何上繼續疊加。
- **算圖驗證外觀**：要看材質/燈光時用 `blender_render(engine="WORKBENCH")`（快）；
  EEVEE/CYCLES 慢很多，只在使用者要求時用。場景必須有相機，沒有就先用 eval 建一支。

### 視埠截圖 vs 全視窗截圖

| 情況 | 用哪個 |
|---|---|
| 驗證幾何、看模型長怎樣（**九成情況**） | `blender_viewport_screenshot` |
| 要看 Outliner / Properties / 工具列 / 目前模式 | `blender_window_screenshot` |
| 懷疑有對話框擋住（指令逾時、沒有回應） | `blender_window_screenshot` |
| 使用者說「看一下我的畫面」 | `blender_window_screenshot` |

兩種都是 Blender 自己的 `screen.screenshot(_area)`，**只能看，不能點**——本橋接器不模擬滑鼠鍵盤。
需要使用者點的東西，請直接請使用者點。截圖抓的是第一個（最大的）3D Viewport；
使用者若正在 Edit Mode 或用了別的 workspace，畫面就是那個狀態。

## 捷徑 tool vs `blender_eval_python`

先用捷徑 tool（語意清楚、參數會被檢查、自動 undo_push）：
`blender_select_objects`、`blender_transform_objects`、`blender_delete_objects`、
`blender_set_view`、`blender_model_file`、`blender_render`。

捷徑做不到時才用 `blender_eval_python`：建 mesh、加 modifier、材質、collection、
armature／vertex group、相機燈光、匯出等。常用片段見
[references/bpy-snippets.md](references/bpy-snippets.md)。

## `eval_python` 寫法守則

1. **程式碼在 timer context 執行**，不是在 operator 裡：
   - `bpy.context.object`、`bpy.context.selected_objects`、`bpy.context.active_object`
     **可能是 None 或不可用**。一律改用 `bpy.data.objects["名稱"]`、
     `bpy.context.view_layer.objects.active`、`[o for o in scene.objects if o.select_get()]`。
   - 呼叫需要 UI 的 operator（`bpy.ops.object.*`、`bpy.ops.mesh.*`、`bpy.ops.view3d.*`）前，
     要用 `bpy.context.temp_override(window=…, area=…, region=…)` 提供 context；
     **能用 `bpy.data` / `bmesh` 直接做的就不要用 `bpy.ops`**（更穩、不依賴選取狀態）。
   - 需要選取狀態的 ops：先 `ob.select_set(True)`、`view_layer.objects.active = ob`。
2. **回傳資料**：把要看的值指定給 `result`（JSON 可序列化；Vector/Matrix/ID 會自動轉）。
   `print()` 的輸出在 `stdout`。globals 跨呼叫持久（`bpy`、`bmesh`、`mathutils`、`math`、
   `Vector`、`Matrix`、`Euler`、`Quaternion` 已預載）。
3. **單位**：Blender 內部長度是**公尺**（預設 Metric、scale 1.0）。旋轉在 `rotation_euler`
   是**弧度**，寫 `math.radians(45)`；捷徑 tool 的 `rotation_deg` 已經是度。
4. **命名**：建立的物件與 data 取有意義的名稱（`"牆-A"`、`"Wall_L"`）；左右對稱用 `.L` / `.R`
   後綴（Blender 的鏡像與 armature 對稱都靠這個）。不要依賴 `Cube.001` 這種自動名稱。
5. **Collection**：新物件要 `link` 到某個 collection 才會出現在場景；自己建的東西放進
   專屬 collection（例如 `"Bridge"`），不要混進使用者的 collection。
6. **不要清場**：絕不執行 `bpy.ops.wm.read_factory_settings`、`read_homefile`、
   `bpy.data.objects.remove` 迴圈全刪、或 `bpy.ops.object.delete` 全選刪除。
   刪除前先 `blender_list_objects` 確認名稱，只刪自己建立的東西。
7. **大場景不要傾印**：不要 `result = [o.name for o in bpy.data.objects]` 或印出所有頂點。
   用計數、篩選、`[:20]`，或用 `blender_list_objects` 的 `limit`/`offset` 分頁。
8. **不要 block 主執行緒**：不要在 eval 裡 `time.sleep`、跑長迴圈或等待輸入；
   Blender 的 UI 在指令執行期間會凍結，逾時 600 秒。算圖走 `blender_render`。
9. **模式**：改 mesh 資料前確認物件不在 Edit Mode（`ob.mode == 'OBJECT'`），否則 `bmesh`
   寫入不會生效；需要時用 `bpy.ops.object.mode_set(mode='OBJECT')`（要 temp_override）。
10. **只做 Blender 相關的事**：`eval_python` 技術上能執行任何 Python（含檔案系統、網路）。
    只用它呼叫 bpy。需要動檔案請走一般的檔案工具並讓使用者看得見。

## 安全守則

- **不可未經使用者明確同意儲存或覆蓋 .blend**。`blender_model_file` 的 `action="save"`
  存回原路徑需要 `confirm=True`、存到既有檔案需要 `overwrite=True`——
  這些旗標只有在使用者親口要求時才加。另存副本用 `copy=True`（不改目前檔案路徑）。
- **開新檔/開別的檔前先確認未存變更**：`blender_ping` 的 `is_dirty` 為 true 時，
  `action="open"` / `"new"` 會被拒絕；先問使用者要不要存，再決定是否加 `confirm=True`。
- 不要動使用者既有的物件、collection、材質、modifier，除非他明確要求。
- 不要嘗試啟動/關閉/重啟 Blender，不要改它的偏好設定或 startup 檔。
- `blender_render` 會暫時改引擎/解析度/相機，算完自動還原；但仍不要在使用者算圖中途呼叫。

## 備援：MCP 不可用時改用 CLI

同一份 Python 函式庫也提供 CLI，輸出 JSON：

```powershell
$BL = "$env:USERPROFILE\BlenderAgentBridge\venv\Scripts\python.exe"
& $BL -m blender_bridge ping
& $BL -m blender_bridge scene --limit 20
& $BL -m blender_bridge objects --types MESH --contains wall --limit 50
& $BL -m blender_bridge select Cube Camera
& $BL -m blender_bridge transform Cube --location 1,2,0 --rotation 0,0,45 --scale 2
& $BL -m blender_bridge delete Cube.001
& $BL -m blender_bridge view --view iso --zoom-extents --shading solid
& $BL -m blender_bridge screenshot --out C:\temp\view.png --max-edge 1280
& $BL -m blender_bridge window-shot --out C:\temp\window.png
& $BL -m blender_bridge undo
& $BL -m blender_bridge model save --path C:\temp\a.blend          # 既有檔案要加 --overwrite
& $BL -m blender_bridge render --out C:\temp\r.png --engine WORKBENCH --resolution 1280x720
```

Python 程式碼含引號時，**一律用檔案或 stdin**，不要塞進命令列（Windows 引號地獄）：

```powershell
& $BL -m blender_bridge eval --file C:\temp\code.py
Get-Content C:\temp\code.py -Raw | & $BL -m blender_bridge eval -
```

離開碼：`0` 成功、`1` 指令執行失敗（含安全旗標拒絕）、`2` 連不上 Blender、`3` 截圖/算圖失敗。
連不上時 stderr 開頭是 `BLENDER_NOT_AVAILABLE:`。

更多 bpy 片段：[references/bpy-snippets.md](references/bpy-snippets.md)
