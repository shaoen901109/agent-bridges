# 給 AI agent 的使用說明（CLI 版）

這份文件是給**沒有 MCP 支援、或偏好直接跑腳本**的 agent 看的。
有 MCP 的 agent 請直接用 `sketchup_*` 那組 tool，並參考 `skill/sketchup-control/SKILL.md`。

---

## 0. 前置條件（每次開工先確認）

1. SketchUp 2024 已由**使用者**開啟
2. 使用者已在 SketchUp 內點選 **延伸程式(Extensions) → Agent Bridge → 啟動伺服器**

連不上時（離開碼 `2`）：**停下來請使用者做上面兩步**。
不要自行啟動 SketchUp、不要結束 SketchUp 行程、不要試圖繞過。

---

## 1. 指令基本形式

```powershell
$SK = "$env:USERPROFILE\SketchUpAgentBridge\venv\Scripts\python.exe"
& $SK -m sketchup_bridge <子指令> [選項]
```

- 所有輸出都是 **JSON**（截圖類輸出存檔路徑）
- 長度單位**預設公釐(mm)**，可用 `--units cm|m|in|ft` 改
- 離開碼：`0` 成功 / `1` 指令失敗 / `2` 連不上 / `3` 截圖失敗

---

## 2. 建議的建模工作流程

```
① 先看                 ② 小步做              ③ 驗證              ④ 錯了退回
   info / scene    →     box / face /    →    set-camera    →     undo
   entities              eval                 + screenshot
```

具體來說：

```powershell
# ① 先了解現況——不要在不知道模型內容的情況下動手
& $SK -m sketchup_bridge info
& $SK -m sketchup_bridge scene --limit 50

# ② 一次只做一個可理解的動作
& $SK -m sketchup_bridge box --width 3000 --depth 2000 --height 2800 --name "主量體"
#   → 回傳的 persistent_id 記下來，後續都用它指涉這個物件

# ③ 一定要看結果，不要假設幾何長對了
& $SK -m sketchup_bridge set-camera --view iso --zoom-extents
& $SK -m sketchup_bridge screenshot --out C:\temp\step1.png --max-edge 1280
#   → 然後真的去「看」這張圖

# ④ 不對就退回，不要在錯誤幾何上疊加
& $SK -m sketchup_bridge undo
```

**視埠截圖 vs 全視窗截圖**

- `screenshot`：看模型幾何——九成情況用這個
- `window-shot`：看工具列、面板、對話框；指令逾時時用它確認是不是跳了對話框

全視窗截圖**只能看不能點**。需要使用者點的東西，請直接請使用者點。

---

## 3. 何時用捷徑、何時用 eval

先用捷徑子指令（參數會被檢查、自動包 undo operation）：
`box`、`face`、`pushpull`、`group`、`transform`、`material`、`delete`、`select`、
`set-camera`、`tool`、`action`。

捷徑做不到的（陣列複製、布林運算、遍歷既有幾何、元件定義、場景頁面、標籤）
才用 `eval`。常用片段見 `skill/sketchup-control/references/ruby-snippets.md`。

### eval 的輸入方式

Ruby 程式碼幾乎一定含引號，**請用檔案或 stdin**，不要塞進命令列：

```powershell
# 方式 A：寫檔（建議）
Set-Content -Path C:\temp\code.rb -Value $code -Encoding UTF8
& $SK -m sketchup_bridge eval --file C:\temp\code.rb

# 方式 B：stdin
Get-Content C:\temp\code.rb -Raw | & $SK -m sketchup_bridge eval -
```

### eval 的程式碼守則

```ruby
# encoding: UTF-8
model = Sketchup.active_model
model.start_operation('動作名稱', true)      # ← 一定要包，否則使用者要按很多次 Ctrl+Z
begin
  # …你的程式碼…
  # 長度一律寫單位：3000.mm / 2.5.m / 10.cm（裸數字 = 英吋！）
  model.commit_operation
  # 回傳小而具體的東西，不要傾印整個模型
  { 'pid' => group.persistent_id }
rescue => e
  model.abort_operation
  raise e
end
```

其他守則：

1. **用 `persistent_id` 追蹤實體**（`entityID` 存檔後會變）。
   取回：`model.find_entity_by_persistent_id(id)`
2. **面的方向**不保證，需要時檢查 `face.normal` 並 `face.reverse!`
3. **`pushpull(d)` 沿法線方向**，`d` 為負則反向；方向不對先讀 normal，不要盲改符號
4. **新幾何包進 Group**（`model.entities.add_group`），否則會和使用者既有幾何黏在一起
5. **絕不大規模破壞**：不要 `entities.clear!`、不要 `to_a.each(&:erase!)`
6. **大模型不要全部傾印**：用計數、`first(n)`、篩選
7. **只做 SketchUp 相關的事**：eval 技術上能碰檔案系統與網路，但只用它呼叫 SketchUp API

---

## 4. 安全守則

- **不可未經使用者明確同意儲存或覆蓋 .skp**。
  `model save` 需要 `--confirm`（存回原路徑）或 `--overwrite`（覆寫既有檔案）——
  這些旗標只有在使用者親口要求時才加。
- **開新檔/開別的檔前先確認未存變更**：`info` 的 `model_modified` 為 `true` 時先問使用者。
- 不要動使用者既有的幾何、圖層、材質，除非他明確要求。
- 不要嘗試啟動/關閉 SketchUp，也不要改它的授權設定。

---

## 5. 常見回應欄位

| 欄位 | 意義 |
|---|---|
| `persistent_id` | 實體的長期識別碼——**後續操作都用它** |
| `entity_id` | 只在本次 session 內有效，不要拿來跨存檔使用 |
| `bounds.min/max/size` | 外框，單位同 `units` 欄位（預設 mm） |
| `note`（`send_action`/`undo`/`redo`） | 提醒該動作是非同步的，狀態要稍後重查 |
| `notes`（`window-shot`） | 說明實際用了哪種擷取方式、有沒有退回 ImageGrab |
| `truncated` | `true` 表示清單被截斷，請用 `--limit` / `--offset` 分頁 |

---

## 6. 完整子指令清單

執行 `& $SK -m sketchup_bridge --help` 取得最新清單，或
`& $SK -m sketchup_bridge commands` 取得伺服器端支援的全部 27 個協定指令。
