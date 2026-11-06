# 給 AI agent 的使用說明（CLI 版）

這份文件是給**沒有 MCP 支援、或偏好直接跑腳本**的 agent 看的。
有 MCP 的 agent 請直接用 `rhino_*` / `gh_*` 那組 tool（工具對照見 `README.md` §5）。

---

## 0. 前置條件（每次開工先確認）

1. Rhino 7 已開啟，且已安裝 RhinoAgentBridge 外掛（Rhino 啟動時自動開伺服器）
2. 沒開 Rhino 時可以執行 `launch`：它會啟動 Rhino 並等到橋接器就緒

連不上時（離開碼 `2`，訊息以 `RHINO_NOT_AVAILABLE` 開頭）：先試 `launch`；
若 `launch` 回報 `ready: false` 並列出 `rhino_windows`，多半是**授權 / 登入 / 更新對話框**擋住——
**停下來請使用者處理**。不要結束 Rhino 行程、不要試圖繞過對話框。

---

## 1. 指令基本形式

```powershell
$RH = "$env:USERPROFILE\RhinoAgentBridge\venv\Scripts\python.exe"
& $RH -m rhino_bridge <子指令> [選項]
```

- 所有輸出都是 **JSON**（截圖類輸出存檔路徑）
- 長度單位**預設是模型單位**（先看 `info` 的 `document.model_units`；這台是公分），可用 `--units mm|cm|m|km|in|ft` 明確指定
- 物件一律用 **GUID**（建立指令回傳的 `ids`；`objects` 可列出全部）
- 離開碼：`0` 成功 / `1` 指令失敗 / `2` 連不上 Rhino / `3` 截圖失敗

---

## 2. 建議的建模工作流程

```powershell
# ① 先了解現況——不要在不知道模型內容的情況下動手
& $RH -m rhino_bridge info
& $RH -m rhino_bridge scene --limit 50

# ② 一次只做一個可理解的動作，記下回傳的 GUID
& $RH -m rhino_bridge box --width 20 --depth 20 --height 10 --layer "Agent::Test"
#   → "ids": ["<GUID>"]

# ③ 一定要看結果，不要假設幾何長對了
& $RH -m rhino_bridge view --view iso --zoom-extents
& $RH -m rhino_bridge screenshot --out C:\temp\v.png
#   → 然後真的去「看」這張圖

# ④ 不對就退回，不要在錯誤幾何上疊加
& $RH -m rhino_bridge undo
```

每個修改指令都是**一個 undo record**，一次 `undo` 剛好回復一步。

---

## 3. 何時用捷徑、何時用 eval

- 方塊 / 球 / 圓柱 / 圓 / 多段線 / 曲線 / 擠出 / 布林 / 放樣 / 移動旋轉縮放 / 圖層 / 顏色：用捷徑子指令（`box`、`extrude`、`boolean`…）
- 其他一切（曲面操作、網格、區塊、自訂邏輯）：用 `eval` 寫 **IronPython 2.7**
- 只想下 Rhino 指令：`run "_-Box 0,0,0 10,10,10 5"`（用 `-_` 開頭的無對話框形式；停在提示時用 `cancel`）

### eval 的輸入方式

```powershell
# 方式 A：寫檔（建議，中文不會被命令列吃掉）
& $RH -m rhino_bridge eval --file C:\temp\job.py

# 方式 B：stdin
"result = doc.Objects.Count" | & $RH -m rhino_bridge eval -
```

### eval 的程式碼守則

```python
# encoding: utf-8
# 這是 Python 2.7（IronPython）：沒有 f-string；用 u'中文'；整數除法會截斷
# 預設可用：Rhino, rg(Rhino.Geometry), rdo, rs(rhinoscriptsyntax), sc, System, math, json, doc
box = rg.Box(rg.Plane.WorldXY, rg.Interval(0, 10), rg.Interval(0, 10), rg.Interval(0, 5))
gid = doc.Objects.AddBrep(box.ToBrep())
result = {"id": str(gid)}        # 回傳值放在 result；print 的輸出放在 stdout
```

- 單位是**文件單位**，不是公釐
- 整段是一個 undo record，不用自己包
- 例外會回傳訊息與 traceback；`BridgeError`（預期內的錯誤）沒有 traceback

---

## 4. Grasshopper 工作流程

```powershell
& $RH -m rhino_bridge gh-new --show                     # 新增文件並顯示編輯器
& $RH -m rhino_bridge gh-slider --min 1 --max 20 --value 6 --nickname R
& $RH -m rhino_bridge gh-search "extrude" --category Surface   # 查元件與埠名稱
& $RH -m rhino_bridge gh-add Circle                      # 回傳 instance id 與埠清單
& $RH -m rhino_bridge gh-add Extrude
& $RH -m rhino_bridge gh-connect <slider id> <circle id> --target-input R
& $RH -m rhino_bridge gh-connect <circle id> <extrude id> --source-output C --target-input B
& $RH -m rhino_bridge gh-set <extrude id> "[0,0,10]" --input D   # 沒連線的輸入用 persistent data
& $RH -m rhino_bridge gh-solve                           # 看 objects_with_errors / messages
& $RH -m rhino_bridge gh-output <extrude id> --output 0  # 讀結果摘要
& $RH -m rhino_bridge gh-bake <extrude id> --layer GH_Test
& $RH -m rhino_bridge view --view iso --zoom-extents
& $RH -m rhino_bridge screenshot --out C:\temp\gh.png    # Rhino 視埠
& $RH -m rhino_bridge gh-shot --out C:\temp\canvas.png   # GH 畫布
```

重點：

- 埠用 **名稱 / 暱稱 / 索引**（`R`、`Radius`、`1`）；物件用 **instance GUID**
- 求解後一定看 `gh-solve` 的 `objects_with_errors` / `objects_with_warnings`，紅 / 橘元件的原因在 `messages`
- 搜尋結果同名多個時，加 `--category` / `--subcategory` 或直接給 `--guid`
- GH 的 `Box` / `Circle` / `Line` 值型別，`gh-bake` 會自動轉成 Brep / 曲線
- 沒有 SDK 涵蓋的操作用 `gh-eval`（可用 `G`、`ghdoc`、`canvas`、`gh`、`find`）

---

## 5. 安全守則

- **不可未經使用者明確同意儲存、覆蓋或開啟檔案**。
  `model save` 需要 `--confirm`；`model save_as` / `export` 覆寫既有檔案需要 `--overwrite`；
  `model new` / `open` 需要 `--confirm`（會丟掉未存變更，`info` 的 `document.modified` 為 `true` 時先問使用者）；
  `gh-save` 需要 `--confirm`。這些旗標只有在使用者親口要求時才加。
- 不要動使用者既有的幾何、圖層，除非他明確要求。
- **不要關閉或強制結束 Rhino**；沒有這類工具。
- Rhino / GH 跳出模態對話框時，請求會逾時——用 `window-shot` 看內容，請使用者處理。
- 指令停在提示（`info` 的 `in_command` 為 `true`）時用 `cancel`（`--how escape|enter`）。

---

## 6. 常見回應欄位

| 欄位 | 意義 |
|---|---|
| `ids` / `objects[].id` | Rhino 物件 GUID——**後續操作都用它** |
| `objects[].bbox` | 外框 `min` / `max`，單位 = 文件單位 |
| `new_object_ids`（`run`） | 指令新增的物件 |
| `output`（`run`） | 命令列輸出（中文介面是中文） |
| `messages`（`gh-solve`） | 每個有錯誤 / 警告的 GH 物件與訊息 |
| `notes`（`window-shot`） | 說明實際用了哪種擷取方式 |
| `total`（`objects`） | 符合條件的總數；用 `--limit` / `--offset` 分頁 |

---

## 7. 完整子指令清單

執行 `& $RH -m rhino_bridge --help` 取得最新清單，或
`& $RH -m rhino_bridge commands` 取得伺服器端支援的全部協定指令。
