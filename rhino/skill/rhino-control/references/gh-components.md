# Grasshopper 常用元件與埠名

**以 `gh_search_components`／`gh_add_component` 回傳的埠清單為準**——下表是常見預設，版本或過時版本可能不同。
埠可用名稱、暱稱（不分大小寫）或索引（0 起算）指定。

物件一律用 **instance GUID**。slider／panel／toggle 只有一個輸出，`gh_connect` 可省略 `source_output`。

---

## 參數與輸入類

| 元件 | 工具 | 備註 |
|---|---|---|
| Number Slider | `gh_add_slider(min, max, value, integer, decimals, nickname)` | 設值用 `gh_set_input(id, value=…)`，超出範圍會報錯；整數滑桿 `integer=True` |
| Panel | `gh_add_panel(text, nickname)` | 輸出就是文字，多行 = 多筆資料；也可接在輸出後面看資料 |
| Boolean Toggle | `gh_add_toggle(value, nickname)` | |
| Point／Vector／Curve… 參數 | `gh_add_component(name="Point", category="Params")` | 同名常有元件與參數兩種，加 category；可用 `gh_set_input(value={"rhino_id": GUID})` 引用 Rhino 物件 |

## 向量／曲線／曲面（預設埠，需自行用 search 確認）

| 元件 | 常見輸入 | 常見輸出 | 備註 |
|---|---|---|---|
| Construct Point | X, Y, Z | Pt | 有過時版本，注意 category／guid |
| Series | Start(S), Step(N), Count(C) | Series(S) | 產生等差數列 |
| Range | Domain(D), Steps(N) | Range(R) | |
| Circle | Plane(P), Radius(R) | Circle(C) | Plane 未連線預設世界 XY |
| Rectangle | Plane(P), X Size(X), Y Size(Y), Radius(R) | Rectangle(R), Length(L) | |
| Line | Start Point(A), End Point(B) | Line(L) | |
| Polyline | Vertices(V), Closed(C) | Polyline(P), Length(L) | |
| Interpolate Curve | Vertices(V), Degree(D), Periodic(P) | Curve(C) | |
| Extrude | Base(B), Direction(D) | Extrusion(E) | **Direction 必填**，沒連線用 `gh_set_input(input="D", value=[0,0,10])` |
| Loft | Curves(C), Options(O) | Loft(L) | |
| Boundary Surfaces | Edges(E) | Surfaces(S) | 平面封閉曲線轉面 |
| Move | Geometry(G), Motion(T) | Geometry(G), Transform(X) | Motion 接 Vector／Unit Z |
| Rotate | Geometry(G), Angle(A), Plane(P) | Geometry(G) | 角度為**弧度** |
| Unit Z | Factor(F) | Unit vector(V) | 可接 slider 當高度 |
| Linear Array | Geometry(G), Direction(D), Count(N) | Geometry(G) | 線性陣列 |

## 資料處理

| 元件 | 用途 |
|---|---|
| Flatten／Graft | 資料樹處理也可直接設在埠上：`gh_set_input(id, value, input=埠, flatten=True)` 或 `graft=True`（另有 `reverse`、`simplify`） |
| List Item | 取第 i 筆（List(L), Index(i), Wrap(W)） |
| Merge／Cull Pattern／Sort List | 合併、篩選、排序 |
| Expression | 寫簡單運算式（變數 x, y…） |

## 常用流程小抄

- **數字 → 幾何**：Slider → 元件輸入；多個 slider 對應多個埠。
- **多個 slider 控制一個點**：Construct Point 的 X／Y／Z 各接一個 slider。
- **讀 Rhino 物件進 GH**：Curve／Geometry 參數 → `gh_set_input(id, value={"rhino_id": "<GUID>"})`。
- **看中間資料**：`gh_get_output(id, output=埠)`（只回摘要）或接 Panel；整體狀況用 `gh_list_document`。
- **烘焙**：`gh_bake(id, output=埠, layer="Agent::GH", name="…")`；烘焙後 Rhino 裡才有實體物件。

## 排錯

| 症狀 | 處理 |
|---|---|
| 元件紅色（Error） | `gh_solve` 的 `messages` 看原因；多半是必填輸入沒連線或型別不符 |
| 元件橘色（Warning） | 常是輸入為空、資料樹層級不符；檢查 flatten／graft |
| `gh_add_component` 回候選清單 | 名稱有歧義 → 加 `category`／`subcategory` 或給 `guid` |
| `gh_connect` 找不到埠 | 先看 `gh_list_document` 或 add 回傳的埠清單，改用索引 |
| 烘焙後沒東西 | 輸出為空或元件有錯；先 `gh_get_output` 確認有幾何 |
| 請求逾時 | 可能有對話框擋住（如 GH 存檔詢問），`rhino_window_screenshot(target="grasshopper")` 看，請使用者處理 |
