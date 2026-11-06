# Rhino IronPython 2.7 常用片段

給 `rhino_eval_python`（或 CLI `eval --file`）使用。整段程式是一個 undo record，不必自己包。

提醒：
- Python 2.7：沒有 f-string；中文用 `u'...'`，檔頭 `# encoding: utf-8`；整數除法會截斷（`1.0 * a / b`）。
- 長度 = **文件單位**（先 `rhino_get_info` 看；這台是公分）。
- 預設名稱：`Rhino`、`rg`、`rdo`、`rs`、`sc`、`System`、`math`、`json`、`doc`。
- 結果放 `result`（要能 JSON 序列化）。

---

## 1. 統計與查詢（不改模型）

```python
# encoding: utf-8
counts = {}
for obj in doc.Objects:
    t = str(obj.ObjectType)
    counts[t] = counts.get(t, 0) + 1
result = {'total': doc.Objects.Count, 'by_type': counts,
          'units': str(doc.ModelUnitSystem), 'tol': doc.ModelAbsoluteTolerance}
```

依名稱／圖層找物件（只回前 20 筆）：

```python
found = []
for obj in doc.Objects:
    if obj.Name and u'牆' in obj.Name:
        found.append({'id': str(obj.Id), 'name': obj.Name,
                      'layer': doc.Layers[obj.Attributes.LayerIndex].FullPath})
result = found[:20]
```

## 2. 建 Brep 方塊（rg 幾何 + 加入文件）

```python
box = rg.Box(rg.Plane.WorldXY, rg.Interval(0, 20), rg.Interval(0, 10), rg.Interval(0, 5))
gid = doc.Objects.AddBrep(box.ToBrep())
result = {'id': str(gid)}
```

## 3. 帶名稱／圖層／顏色加入物件

```python
import System.Drawing as sd
layer_path = u'Agent::Demo'
idx = doc.Layers.FindByFullPath(layer_path, -1)
if idx < 0:
    layer = Rhino.DocObjects.Layer()
    layer.Name = u'Demo'
    parent = doc.Layers.FindByFullPath(u'Agent', -1)
    if parent < 0:
        parent = doc.Layers.Add(u'Agent', sd.Color.Gray)
    layer.ParentLayerId = doc.Layers[parent].Id
    idx = doc.Layers.Add(layer)

attr = rdo.ObjectAttributes()
attr.Name = u'測試球'
attr.LayerIndex = idx
attr.ObjectColor = sd.Color.FromArgb(200, 60, 60)
attr.ColorSource = rdo.ObjectColorSource.ColorFromObject
gid = doc.Objects.AddSphere(rg.Sphere(rg.Point3d(0, 0, 0), 5), attr)
result = str(gid)
```

## 4. 曲線擠出成實體（封閉平面曲線）

```python
crv = rg.Circle(rg.Plane.WorldXY, 8).ToNurbsCurve()
srf = rg.Surface.CreateExtrusion(crv, rg.Vector3d(0, 0, 30))
brep = srf.ToBrep().CapPlanarHoles(doc.ModelAbsoluteTolerance)
result = str(doc.Objects.AddBrep(brep))
```

## 5. 陣列複製（迴圈，注意整數除法）

```python
src = rs.coerceguid('<GUID>')
ids = []
for i in range(1, 6):
    xf = rg.Transform.Translation(i * 12.0, 0, 0)
    ids.append(str(doc.Objects.Transform(src, xf, False)))   # False = 保留原物件，產生複本
result = ids
```

## 6. 布林運算（Brep）

```python
a = rs.coercebrep('<GUID_A>')
b = rs.coercebrep('<GUID_B>')
out = rg.Brep.CreateBooleanDifference([a], [b], doc.ModelAbsoluteTolerance)
if not out:
    raise Exception(u'布林失敗：確認兩者都是封閉實體且確實相交')
result = [str(doc.Objects.AddBrep(x)) for x in out]
# 確認結果正確後，才由使用者同意再刪原物件
```

## 7. 取外框與量測

```python
gid = rs.coerceguid('<GUID>')
obj = doc.Objects.FindId(gid)
bb = obj.Geometry.GetBoundingBox(True)
result = {'min': bb.Min, 'max': bb.Max, 'size': [bb.Max.X - bb.Min.X, bb.Max.Y - bb.Min.Y, bb.Max.Z - bb.Min.Z]}
brep = obj.Geometry
if isinstance(brep, rg.Brep) and brep.IsSolid:
    result['volume'] = rg.VolumeMassProperties.Compute(brep).Volume
```

## 8. 區塊（Block）

```python
# 以選取物件定義區塊並插入兩個實例（rs 版本最簡單）
ids = rs.SelectedObjects()
if not ids:
    raise Exception(u'請先選取要做成區塊的物件')
rs.AddBlock(ids, (0, 0, 0), u'AgentBlock', True)
rs.InsertBlock(u'AgentBlock', (50, 0, 0))
result = u'ok'
```

## 9. 選取／隱藏／圖層可見度

```python
rs.LayerVisible(u'Agent::Demo', False)
rs.UnselectAllObjects()
result = rs.IsLayerVisible(u'Agent::Demo')
```

---

## 常見錯誤

| 症狀 | 原因與處理 |
|---|---|
| `SyntaxError` 在 f-string／`print(..., file=)` | 這是 Python 2.7，改 `%` 或 `.format` |
| 中文變成亂碼或 `UnicodeEncodeError` | 字串前加 `u`，檔頭加 `# encoding: utf-8` |
| `a / b` 結果是整數 | 改 `1.0 * a / b` |
| 物件大小差 10 倍或 1000 倍 | 單位不是你以為的，先 `rhino_get_info` |
| 布林回傳空 | 輸入不是封閉實體，或幾何未相交；先檢查 `IsSolid` |
| `result` 回傳報序列化錯誤 | 轉成 `str()`／list／dict 再放進 `result` |
