---
name: rhino-control
description: 透過 Agent Bridge 操控本機的 Rhino 7 與 Grasshopper——查詢模型、用 IronPython（RhinoCommon）建模與修改幾何、設定視角、擷取視埠與全視窗截圖來驗證結果，並能建立、求解、烘焙 Grasshopper 定義。當使用者要求操作 Rhino／Rhino 7／犀牛、在 Rhino 裡建模／改模型、查看或描述 Rhino 目前畫面、處理 .3dm 檔案內容，或提到 Grasshopper／GH／參數化建模／節點／電池時使用本技能。內容為工作流程與 IronPython 守則，不重複 MCP tool 的參數說明。
---

# Rhino 7 + Grasshopper 代理操控

MCP server 名 `rhino`，工具前綴 `rhino_`（Rhino）與 `gh_`（Grasshopper）。
（部分 MCP 客戶端可能顯示成 `mcp_rhino_<tool_name>`，照你看到的名稱呼叫即可。）

## 前置條件（每次開工先確認）

1. 先呼叫 `rhino_get_info` 確認連線，**並記下模型單位**（這台目前是公分）。
2. 外掛隨 Rhino 啟動自動開伺服器（127.0.0.1:9878），使用者不需手動啟動。

連不上時（回應開頭是 `RHINO_NOT_AVAILABLE`）：
- Rhino 沒開 → 呼叫 `rhino_launch`（可帶 `path` 開 .3dm；它會等橋接器就緒）。
- `rhino_launch` 回 `ready=false` 並列出視窗標題 → 多半是授權／登入／更新對話框擋住，
  **停下來請使用者處理**。
- **不要**強制結束 Rhino 行程（也沒有這類工具），不要試圖繞過對話框。

## 標準工作流程

```
get_info → get_scene_info → 小步操作 → set_view + 視埠截圖驗證 → 不對就 undo → 下一步
```

- **先看清楚再動手**：`rhino_get_scene_info` 看物件統計、圖層、邊界；要 GUID 用 `rhino_list_objects`（可依 types／layer／name 過濾、分頁）。
- **小步前進**：一次只做一個可理解的動作，記下回傳的 GUID。
- **每次改完就驗證**：`rhino_set_view(view="iso", zoom_extents=True)` 然後
  `rhino_viewport_screenshot()`。眼見為憑。
- **錯了就退回**：`rhino_undo_redo(direction="undo")`。每個橋接器指令（含整段 eval）是一個
  undo record，一次 undo 剛好回復一步。redo 用 `rhino_undo_redo(direction="redo")`（內部已改走 `_Redo` 指令）。
- 指令停在提示（`rhino_get_info` 的 `in_command` 為 true）→ `rhino_cancel_command`（`how`: escape｜enter）。

### 視埠截圖 vs 全視窗截圖

| 情況 | 用哪個 |
|---|---|
| 驗證幾何、看模型長怎樣（**九成情況**） | `rhino_viewport_screenshot` |
| 看命令列提示、面板、工具列 | `rhino_window_screenshot` |
| 懷疑有對話框擋住（請求逾時、沒有回應） | `rhino_window_screenshot` |
| 看 Grasshopper 整個視窗 | `rhino_window_screenshot(target="grasshopper")` |
| 看 GH 畫布（元件、連線、紅橘錯誤） | `gh_canvas_screenshot` |

全視窗截圖是 OS 層擷取，**只能看，不能點**。需要點的東西請使用者點。

## 單位、GUID、命名

- 長度一律是**文件單位**（不是公釐）。先 `rhino_get_info` 看單位；捷徑 tool 可傳
  `units`（mm/cm/m/km/in/ft）明確指定，CLI 用 `--units`。`eval_python` 內沒有換算，自己算。
- Rhino 物件用 **GUID**；Grasshopper 物件用 **instance GUID**（`gh_add_*`／`gh_list_document` 回傳的 `id`，不是元件的 component GUID）。
- 圖層用完整路徑 `Parent::Child`，不存在會自動建立。新東西放自己的圖層（如 `Agent::XXX`），不混進使用者圖層。

## 捷徑 tool vs `rhino_eval_python`

先用捷徑 tool（語意清楚、參數會被檢查、自帶 undo）：
`rhino_create_box`／`_sphere`／`_cylinder`／`_circle`／`_polyline`／`_curve`、`rhino_extrude`、
`rhino_boolean`、`rhino_loft`、`rhino_transform_objects`、`rhino_set_layer`、`rhino_set_material`、
`rhino_select_objects`、`rhino_set_view`、`rhino_run_command`。

做不到才用 `rhino_eval_python`：曲面／網格操作、區塊、迴圈批次、遍歷既有物件、自訂邏輯等。
片段見 [references/ironpython-snippets.md](references/ironpython-snippets.md)。

## `rhino_eval_python` 寫法守則（IronPython 2.7）

1. **是 Python 2.7**：沒有 f-string、沒有型別註記；用 `'%s' % x` 或 `.format`。
2. **中文**：字串寫 `u'中文'`；檔案開頭加 `# encoding: utf-8`。
3. **整數除法會截斷**：`7 / 2 == 3`；要小數寫 `1.0 * a / b`。
4. **用 RhinoCommon／rhinoscriptsyntax**：預設已有 `Rhino`、`rg`（Rhino.Geometry）、`rdo`（Rhino.DocObjects）、
   `rs`（rhinoscriptsyntax）、`sc`、`System`、`math`、`json`、`doc`（目前文件）。
5. **回傳值放變數 `result`**（要能 JSON 序列化；Point3d、BoundingBox 會自動轉）；`print` 輸出也會一併回傳。
6. **整段是一個 undo record**，不必自己包；新增物件用 `doc.Objects.Add...`，不必手動 `Redraw()`。
7. **單位 = 文件單位**，不要把公釐數字直接塞進公分文件。
8. **不要大量傾印**：回傳計數、前 N 筆、bbox，不要 `result = [全部物件]`。
9. **不要一次刪光**：絕不寫「刪除所有物件」這類程式碼。刪除前先 `rhino_list_objects` 確認範圍，只刪自己建立的。
10. **只做 Rhino 相關的事**：eval 技術上能跑任何 IronPython（含檔案系統、網路）。只用它呼叫 RhinoCommon／rhinoscriptsyntax；
    需要動檔案請走一般檔案工具並讓使用者看得見。

## Grasshopper 工作流程

```
gh_status → gh_open_editor → gh_new_document → gh_search_components → gh_add_* → gh_connect
  → gh_solve（看錯誤）→ gh_get_output（讀摘要）→ gh_bake → Rhino 視埠截圖驗證
```

要點：
- 先 `gh_status`；編輯器沒載入就 `gh_open_editor`（第一次要數秒）。`gh_new_document` 會讓新文件成為作用中（舊的留著、不存檔）。
- **不要憑記憶猜元件與埠名**：`gh_search_components` 查 guid、分類、輸入輸出埠（名稱／暱稱／型別／是否選用）。
  `gh_add_component` 回傳也會列出完整埠清單。
- 埠用**名稱、暱稱（不分大小寫）或索引**指定；slider／panel／toggle 輸出只有一個，`source_output` 可省略。
- **同名元件有多個**（如 `Circle` 有 Curve 分類的元件，也有 Params 的參數）→ 加 `category`／`subcategory`，或改用 `guid` 放置。
- 沒連線的輸入用 `gh_set_input(id, value, input=埠)` 設 persistent data（數字、字串、點 `[x,y,z]`、區間 `[a,b]`、`{"rhino_id": GUID}`…）。
  例如 Extrude 的 `Direction` 是必填，沒連線要設 `[0,0,10]`。
- `gh_solve` 後**一定看** `objects_with_errors`／`objects_with_warnings` 與 `messages`；紅／橘元件原因在那裡。
  錯了先 `gh_disconnect`／`gh_delete_objects` 修，不要疊加。
- `gh_get_output` 只回摘要（筆數、bbox、長度、是否實體），不會傾印幾何。
- `gh_bake` 把輸出放進 Rhino（一個 undo record，可指定 `layer`／`name`／`color`）。GH 值型別（Box／Circle／Line）會自動轉 Brep／曲線。
- GH SDK 沒涵蓋的才用 `gh_eval_python`（有 `G`、`ghdoc`、`canvas`、`gh`、`find`）。
- GH 文件本身不等於 Rhino 模型：烘焙前 Rhino 裡什麼都沒有。

### 完整範例：Slider → Circle → Extrude → 烘焙

MCP 呼叫序列（id 以實際回傳為準）：

```
gh_status()
gh_open_editor(show=True)
gh_new_document(show=True)
gh_add_slider(min=1, max=20, value=6, nickname="R")             → slider id
gh_search_components(query="Circle", category="Curve")          → 確認埠：Plane(P) / Radius(R)；輸出 Circle(C)
gh_add_component(name="Circle", category="Curve")               → circle id
gh_search_components(query="Extrude")                           → 確認 Base(B) / Direction(D)
gh_add_component(name="Extrude", category="Surface")            → extrude id
gh_connect(source=<slider>, target=<circle>, target_input="R")
gh_connect(source=<circle>, target=<extrude>, source_output="C", target_input="B")
gh_set_input(id=<extrude>, input="D", value=[0,0,10])
gh_solve()                                  → objects_with_errors 應為空
gh_get_output(id=<extrude>, output=0)       → 確認是實體
gh_bake(id=<extrude>, output=0, layer="Agent::GH_Test")
rhino_set_view(view="iso", zoom_extents=True)
rhino_viewport_screenshot()
```

等效 CLI（`$RH` 定義見下節）：

```powershell
& $RH -m rhino_bridge gh-new --show
& $RH -m rhino_bridge gh-slider --min 1 --max 20 --value 6 --nickname R
& $RH -m rhino_bridge gh-search "Circle" --category Curve
& $RH -m rhino_bridge gh-add Circle --category Curve
& $RH -m rhino_bridge gh-add Extrude --category Surface
& $RH -m rhino_bridge gh-connect <slider id> <circle id> --target-input R
& $RH -m rhino_bridge gh-connect <circle id> <extrude id> --source-output C --target-input B
& $RH -m rhino_bridge gh-set <extrude id> "[0,0,10]" --input D
& $RH -m rhino_bridge gh-solve
& $RH -m rhino_bridge gh-output <extrude id> --output 0
& $RH -m rhino_bridge gh-bake <extrude id> --output 0 --layer Agent::GH_Test
& $RH -m rhino_bridge view --view iso --zoom-extents
& $RH -m rhino_bridge screenshot --out C:\temp\gh.png
```

調參數只要 `gh_set_input(id=<slider>, value=12)` 再 `gh_solve`。常用元件與埠名見
[references/gh-components.md](references/gh-components.md)。

## 已知的坑

- **模態對話框會讓請求逾時**（UI 執行緒被擋）：用 `rhino_window_screenshot` 看內容，請使用者處理；橋接器無法關閉對話框。
  常見例子：Rhino 結束時的「Grasshopper multi-save」存檔詢問、授權／更新框。
- **GH 畫布截圖要先顯示編輯器**：`gh_canvas_screenshot` 預設會先顯示；失敗時先 `gh_open_editor`。
- 長運算期間後續請求可能回 `BusyError`；等一下再試，或 `rhino_cancel_command`。
- 需要檔案對話框的 Rhino 指令（如 `_Open`）用 `-_Open "路徑"` 形式；`rhino_run_command` 優先用 `-_` 開頭的無對話框形式。
- 改了外掛的 `py\*.py` 沒生效 → CLI `raw reload_scripts`。

## 安全守則

- **不可未經使用者明確同意儲存、覆蓋、新建或開啟檔案**。`rhino_model_file`：`save` 需 `confirm=True`；
  `save_as`／`export` 覆寫既有檔需 `overwrite=True`；`new`／`open` 需 `confirm=True`。`gh_save_document` 需 `confirm=True`（覆寫另加 `overwrite`）；
  `gh_open_document` 也要使用者同意。這些旗標**只有使用者親口要求時才加**。
- **開新檔／開別的檔前先確認未存變更**：`rhino_get_info` 顯示有未存變更時先問使用者——`new`／`open` 會丟掉未存變更。
- 不要動使用者既有的幾何、圖層、材質，除非他明確要求。刪除前先列出確認。
- **不要關閉或強制結束 Rhino**；不要改它的授權設定。

## 備援：MCP 不可用時改用 CLI

同一份 Python 函式庫也提供 CLI，輸出 JSON：

```powershell
$RH = "$env:USERPROFILE\RhinoAgentBridge\venv\Scripts\python.exe"
& $RH -m rhino_bridge launch                 # 沒開就啟動 Rhino
& $RH -m rhino_bridge ping
& $RH -m rhino_bridge info
& $RH -m rhino_bridge scene --limit 50
& $RH -m rhino_bridge objects --types brep curve --limit 20
& $RH -m rhino_bridge box --width 20 --depth 20 --height 10 --layer Agent::Test [--units mm]
& $RH -m rhino_bridge view --view iso --zoom-extents
& $RH -m rhino_bridge screenshot --out C:\temp\view.png --max-edge 1280
& $RH -m rhino_bridge window-shot --out C:\temp\window.png [--target grasshopper]
& $RH -m rhino_bridge undo
& $RH -m rhino_bridge run "_-Box 0,0,0 10,10,10 5"
```

IronPython 程式碼含引號或中文時，**一律用檔案或 stdin**，不要塞進命令列（Windows 引號地獄）：

```powershell
& $RH -m rhino_bridge eval --file C:\temp\code.py
Get-Content C:\temp\code.py -Raw | & $RH -m rhino_bridge eval -
& $RH -m rhino_bridge gh-eval --file C:\temp\gh_code.py
```

離開碼：`0` 成功、`1` 指令執行失敗、`2` 連不上 Rhino、`3` 截圖失敗。
完整子指令：`& $RH -m rhino_bridge --help`。

更多片段：[references/ironpython-snippets.md](references/ironpython-snippets.md)、[references/gh-components.md](references/gh-components.md)
