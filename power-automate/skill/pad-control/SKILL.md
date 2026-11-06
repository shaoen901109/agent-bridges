---
name: pad-control
description: 透過 pad-bridge 操控本機的 Power Automate Desktop（PAD）——列出與執行桌面流程（帶輸入、取回輸出變數與失敗原因）、讀出流程的 Robin 腳本、用 Robin 建立新流程或改寫既有流程、查動作語法。當使用者要求「跑某個 Power Automate 流程」「做一個 PAD 流程自動化某件事」「改我的桌面流程」、要 agent 把一段桌面 RPA 交給 PAD 執行，或提到 Power Automate Desktop／PAD／桌面流程時使用本技能。內容為工作流程與 Robin 寫法守則，不重複 MCP tool 的參數說明。
---

# Power Automate Desktop 代理操控

## 前置條件

1. Power Automate（PAD）已安裝並以使用者帳號登入。console 沒開時 bridge 會自己喚起。
2. 先呼叫 `pad_list_flows` 確認看得到流程清單與帳號。

## 這個 bridge 怎麼運作（理解它的限制）

- **執行**走官方 run URL（`ms-powerautomate:/console/flow/run`），結果讀本機執行紀錄
  （`RunDefinition.json`／`Actions.log`），是可靠的。個人 Microsoft 帳號也能用。
- **建立／修改／讀腳本／查語法**是用 UI Automation 操作 PAD 設計工具，會用**真實鍵盤與滑鼠**、
  搶前景視窗 20–60 秒。呼叫前先告訴使用者「接下來會操作 PAD 視窗，請先別動電腦」。
- 流程是 **attended** 執行：用到滑鼠鍵盤／UI 自動化的流程會接管使用者的畫面；同時只能跑一個流程。

## 執行流程

```
pad_list_flows → pad_run_flow(flow_name, inputs) → 看 status / outputs / not_succeeded_actions
```

- `inputs` 的 key 是輸入變數的**外部名稱**，值只能是文字、數字、布林。大量資料寫成檔案，傳路徑。
- 沒給的輸入會用預設值；若輸入沒有預設值，PAD 會跳窗問使用者，流程會卡住。
- 長時間流程用 `wait=False` 拿 `run_id`，再用 `pad_get_run` 追。逾時回 `still_running` 不代表失敗。
- 失敗時看 `not_succeeded_actions`：會列出出錯動作的名稱、`Module.Action`、錯誤代碼與訊息。

### 確認對話框（`awaiting_confirmation`）

PAD 預設每次外部觸發都跳「外部來源正在嘗試執行以下流程」。這是 PAD 的安全機制：

- 回 `awaiting_confirmation` 時，**請使用者去 PAD 按「繼續」**，不要自己想辦法點掉或繞過。
- 使用者想免確認時，告訴他兩種做法讓他自己選：
  1. PAD console → 設定 → 關閉「從外部叫用流程時顯示確認對話方塊」（官方做法，所有外部觸發都不再確認）。
  2. 在 MCP 設定加環境變數 `PAD_BRIDGE_AUTO_CONFIRM=1`：bridge 只會替**自己剛觸發、名稱相符**的流程按繼續。

## 建立或修改流程

```
（不確定語法）pad_search_actions → pad_action_template
→ pad_create_flow(flow_name, script, inputs, outputs)
→ pad_run_flow 用測試輸入跑一次 → 看結果，不對就 pad_update_flow 修
```

1. **語法不要猜**。寫不熟的動作前，先 `pad_action_template("中文動作名")` 拿 PAD 產生的正確寫法，
   再把參數換掉。已驗證的常用寫法在 [references/robin-snippets.md](references/robin-snippets.md)。
2. **向使用者既有流程學**：`pad_get_flow_script` 可讀出他手做流程的 Robin 文字。
3. 輸入／輸出變數**不在 Robin 文字裡**，用 `inputs` / `outputs` 參數建立。輸出變數要在腳本裡被 `SET` 才會有值。
4. 腳本有設計階段錯誤（未知動作、參數名錯、變數不存在）時回 `PAD_SCRIPT_ERROR` 並列出行號與訊息：
   - `pad_update_flow`：**不會儲存**，流程維持原狀。
   - `pad_create_flow`：流程與變數已建立但動作是空的，修正後用 `pad_update_flow` 補上。
5. `pad_update_flow(script=...)` 會**整段取代 Main 子流程**。改既有流程前一定先 `pad_get_flow_script`，
   在原文上修改，並先跟使用者確認要改的是哪個流程。只改 Main；其他子流程不支援。
6. 預設建立傳統語法流程（`power_fx=False`）。使用者既有流程都是傳統語法，維持一致。

## Robin 寫法守則（傳統語法）

- 文字常值：`$'''內容'''`；空字串是兩個反引號（PAD 產生的範本會這樣寫）；變數插值放在文字裡：`$'''Hi %Name%'''`。
- 路徑裡的反斜線要寫兩個：`$'''C:\\temp\\a.txt'''`。
- 輸出變數用 `=>`：`... Content=> FileContents`，名稱可自訂。
- 運算式直接寫或用 `%...%`：`SET Doubled TO N * 2`、`IF Counter = 3 THEN`、`LOOP WHILE (Counter) < (3)`。
- 區塊用 `END` 結束；`ELSE IF` 可串接。動作的錯誤處理用 `ON ERROR ... END` 緊接在動作後。
- 清單索引 `TextList[0]`、屬性 `CurrentItem.Name`、`TextList.Count`、JSON 物件 `Obj['key']`。

## 安全守則

- **不要刪除流程**（bridge 也沒有提供）。需要刪除請使用者自己在 PAD 刪。
- 不要改使用者既有流程，除非他明確要求；修改前先讀出原腳本並說明要改什麼。
- 不要執行使用者沒指名的既有流程——很多流程會操作滑鼠鍵盤、開網頁、寫檔。
- 暫存流程 `_pad_bridge_scratch` 是 bridge 查語法用的畫布，內容永遠不存，別拿來放正式動作。
- 不要嘗試用 bridge 以外的方式（改登錄檔、改 PAD 設定檔）繞過確認框或授權。

## 備援：MCP 不可用時改用 CLI

```powershell
$PB = "$env:USERPROFILE\PADAgentBridge\venv\Scripts\python.exe"
& $PB -m pad_bridge flows
& $PB -m pad_bridge run 我的流程 --input Msg=你好 --input Count=3 --timeout 300
& $PB -m pad_bridge run-status <run_id>
& $PB -m pad_bridge runs --flow 我的流程 --limit 5
& $PB -m pad_bridge script 我的流程
& $PB -m pad_bridge search-actions 取代
& $PB -m pad_bridge template 取代文字
& $PB -m pad_bridge create 新流程 --script-file C:\temp\flow.robin --spec-file C:\temp\vars.json
& $PB -m pad_bridge update 舊流程 --script-file C:\temp\flow.robin
& $PB -m pad_bridge dialogs --dismiss
```

Robin 腳本一律寫成 UTF-8 檔案再傳，不要塞進命令列。`vars.json`：
`{"inputs": [{"name": "Msg", "type": "text", "default": "hi"}], "outputs": [{"name": "Result"}]}`

離開碼：`0` 成功、`1` 流程沒有成功（Failed／逾時／等待確認）、`2` UI 操作失敗。
