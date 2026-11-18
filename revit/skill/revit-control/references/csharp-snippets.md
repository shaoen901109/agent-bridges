# C# 片段範例（revit_eval_csharp）

> 全部是 **C# 5** 語法，單位是**英呎**（1 ft = 304.8 mm）。片段可直接使用 `uiapp`、`uidoc`、`doc`、`print`。
> 預設 using：System、System.Collections.Generic、System.Linq、Autodesk.Revit.DB、Autodesk.Revit.UI。
> 預設 `transaction=true`（片段內不要自己開 Transaction）；唯讀查詢也可以用預設值。
> 這些範例沒有在實機驗證過；第一次用請先在測試專案跑，並以編譯錯誤訊息為準微調。

## 單位換算小工具

```csharp
double mm = 3000;
double ft = UnitUtils.ConvertToInternalUnits(mm, UnitTypeId.Millimeters);   // mm -> ft
double back = UnitUtils.ConvertFromInternalUnits(ft, UnitTypeId.Millimeters); // ft -> mm
return new object[] { ft, back };
```

## FilteredElementCollector：查詢

### 依類別列出（類別 + 實例）

```csharp
var walls = new FilteredElementCollector(doc)
    .OfCategory(BuiltInCategory.OST_Walls)
    .WhereElementIsNotElementType()
    .ToElements();
print("牆數量：" + walls.Count);
return walls.Take(20).Select(delegate (Element e) {
    return new { id = e.Id, name = e.Name, type = doc.GetElement(e.GetTypeId()).Name };
}).ToList();
```

### 依 .NET 類別找（Wall、Floor、Level…）

```csharp
var levels = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>()
    .OrderBy(delegate (Level l) { return l.Elevation; }).ToList();
return levels.Select(delegate (Level l) {
    return new { id = l.Id, name = l.Name, elevation_mm = UnitUtils.ConvertFromInternalUnits(l.Elevation, UnitTypeId.Millimeters) };
}).ToList();
```

### 只看目前視圖可見的元件

```csharp
var inView = new FilteredElementCollector(doc, doc.ActiveView.Id)
    .WhereElementIsNotElementType().ToElements();
return inView.GroupBy(delegate (Element e) { return e.Category == null ? "(無類別)" : e.Category.Name; })
    .OrderByDescending(delegate (IGrouping<string, Element> g) { return g.Count(); })
    .Select(delegate (IGrouping<string, Element> g) { return new { category = g.Key, count = g.Count() }; })
    .ToList();
```

### 快速過濾器（比事後 LINQ 快）：依樓層過濾

```csharp
Level lv = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>()
    .First(delegate (Level l) { return l.Name == "Level 1"; });
var filter = new ElementLevelFilter(lv.Id);
var items = new FilteredElementCollector(doc).WherePasses(filter).WhereElementIsNotElementType().ToElements();
return items.Count;
```

### 依參數值過濾（慢過濾器，用 LINQ）

```csharp
var tall = new FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Walls)
    .WhereElementIsNotElementType().ToElements()
    .Where(delegate (Element e) {
        Parameter p = e.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM);
        return p != null && p.AsDouble() > 3000 / 304.8;
    }).ToList();
return tall;
```

## 讀取參數

### 讀單一元件的全部參數

```csharp
Element e = doc.GetElement(new ElementId(123456L));   // 2024：ElementId(long)
var list = new List<object>();
foreach (Parameter p in e.Parameters)
{
    list.Add(new { name = p.Definition.Name, storage = p.StorageType.ToString(), value = p.AsValueString() });
}
return list;
```

### 依名稱或 BuiltInParameter 讀取

```csharp
Element e = doc.GetElement(new ElementId(123456L));
Parameter byName = e.LookupParameter("Mark");                               // 依名稱（找不到回 null）
Parameter byBip = e.get_Parameter(BuiltInParameter.ALL_MODEL_MARK);          // 依內建參數
return new object[] { byName == null ? null : byName.AsString(), byBip == null ? null : byBip.AsString() };
```

### 統計：每種牆類型的總長度（mm）與面積（m²）

```csharp
var walls = new FilteredElementCollector(doc).OfClass(typeof(Wall)).Cast<Wall>().ToList();
return walls.GroupBy(delegate (Wall w) { return w.WallType.Name; })
    .Select(delegate (IGrouping<string, Wall> g) {
        double len = g.Sum(delegate (Wall w) { return w.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH).AsDouble(); });
        double area = g.Sum(delegate (Wall w) { Parameter a = w.get_Parameter(BuiltInParameter.HOST_AREA_COMPUTED); return a == null ? 0 : a.AsDouble(); });
        return new {
            type = g.Key, count = g.Count(),
            length_mm = Math.Round(len * 304.8),
            area_m2 = Math.Round(area * 0.09290304, 2)      // 1 平方英呎 = 0.09290304 平方公尺
        };
    }).ToList();
```

## 寫入參數

```csharp
// 設定備註（Comments）＋ 長度參數（Unconnected Height，要用英呎）
Element e = doc.GetElement(new ElementId(123456L));
Parameter c = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
if (c != null && !c.IsReadOnly) c.Set("由 agent 標註");

Parameter h = e.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM);
if (h != null && !h.IsReadOnly) h.Set(3500 / 304.8);
return e.Id;
```

> 單純改一個參數用 `revit_set_parameter` 更省事（它會做單位與型別處理）；要批次改很多元件才用片段。

## 牆

### 沿著一條線建牆

```csharp
Level lv = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>()
    .OrderBy(delegate (Level l) { return l.Elevation; }).First();
WallType wt = new FilteredElementCollector(doc).OfClass(typeof(WallType)).Cast<WallType>().First();

double s = 1 / 304.8;   // mm -> ft 的倍率
Line line = Line.CreateBound(new XYZ(0, 0, 0), new XYZ(6000 * s, 0, 0));
Wall w = Wall.Create(doc, line, wt.Id, lv.Id, 3000 * s, 0, false, false);   // 高度、底部偏移、翻轉、結構
return w;
```

### 用四條牆圍出矩形房間

```csharp
Level lv = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>()
    .OrderBy(delegate (Level l) { return l.Elevation; }).First();
double s = 1 / 304.8;
double w = 5000 * s, d = 4000 * s, h = 3000 * s;
XYZ[] pts = new XYZ[] { new XYZ(0, 0, 0), new XYZ(w, 0, 0), new XYZ(w, d, 0), new XYZ(0, d, 0) };
var ids = new List<ElementId>();
for (int i = 0; i < 4; i++)
{
    Line ln = Line.CreateBound(pts[i], pts[(i + 1) % 4]);
    ids.Add(Wall.Create(doc, ln, lv.Id, false).Id);   // 預設牆類型
}
foreach (ElementId id in ids) { ((Wall)doc.GetElement(id)).get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM).Set(h); }
return ids;
```

## 樓板（Revit 2024：`Floor.Create`）

```csharp
Level lv = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>()
    .OrderBy(delegate (Level l) { return l.Elevation; }).First();
ElementId floorTypeId = new FilteredElementCollector(doc).OfClass(typeof(FloorType)).FirstElementId();
double s = 1 / 304.8;
double w = 5000 * s, d = 4000 * s;
CurveLoop loop = new CurveLoop();
XYZ[] p = new XYZ[] { new XYZ(0, 0, 0), new XYZ(w, 0, 0), new XYZ(w, d, 0), new XYZ(0, d, 0) };
for (int i = 0; i < 4; i++) loop.Append(Line.CreateBound(p[i], p[(i + 1) % 4]));
Floor f = Floor.Create(doc, new List<CurveLoop> { loop }, floorTypeId, lv.Id);
return f;
```

> 舊版 API `doc.Create.NewFloor` 在 2024 已移除，不要用。

## 門窗（族群實例）

### 在牆上放門

```csharp
// 前提：族群已載入專案。先找類型，再找一道牆，放在牆的中點。
FamilySymbol sym = new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol))
    .OfCategory(BuiltInCategory.OST_Doors).Cast<FamilySymbol>().FirstOrDefault();
if (sym == null) throw new Exception("專案中沒有門族群，請先載入");
if (!sym.IsActive) sym.Activate();        // 第一次使用類型必須啟用

Wall wall = new FilteredElementCollector(doc).OfClass(typeof(Wall)).Cast<Wall>().First();
LocationCurve lc = (LocationCurve)wall.Location;
XYZ mid = lc.Curve.Evaluate(0.5, true);
Level lv = (Level)doc.GetElement(wall.LevelId);
FamilyInstance door = doc.Create.NewFamilyInstance(mid, sym, wall, lv, Autodesk.Revit.DB.Structure.StructuralType.NonStructural);
return door;
```

### 放窗並設定窗台高

```csharp
FamilySymbol sym = new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol))
    .OfCategory(BuiltInCategory.OST_Windows).Cast<FamilySymbol>().FirstOrDefault();
if (sym == null) throw new Exception("專案中沒有窗族群");
if (!sym.IsActive) sym.Activate();
Wall wall = new FilteredElementCollector(doc).OfClass(typeof(Wall)).Cast<Wall>().First();
LocationCurve lc = (LocationCurve)wall.Location;
Level lv = (Level)doc.GetElement(wall.LevelId);
FamilyInstance win = doc.Create.NewFamilyInstance(lc.Curve.Evaluate(0.3, true), sym, wall, lv,
    Autodesk.Revit.DB.Structure.StructuralType.NonStructural);
win.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM).Set(900 / 304.8);   // 窗台高 900 mm
return win;
```

## 樓層與視圖

### 建立樓層（並建立平面視圖）

```csharp
Level nl = Level.Create(doc, 3000 / 304.8);
nl.Name = "Level 2 (agent)";
ElementId vftId = new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
    .First(delegate (ViewFamilyType v) { return v.ViewFamily == ViewFamily.FloorPlan; }).Id;
ViewPlan.Create(doc, vftId, nl.Id);
return nl;
```

### 切換作用中視圖（要 transaction=false 以外的情況通常用 revit_set_view）

```csharp
View v = new FilteredElementCollector(doc).OfClass(typeof(View)).Cast<View>()
    .First(delegate (View x) { return !x.IsTemplate && x.Name == "Level 1"; });
uidoc.ActiveView = v;     // 實機驗證點：不同版本可能要改用 uidoc.RequestViewChange(v)
return v.Name;
```

## 其他實用片段

### 刪除（先確認 id！）

```csharp
ICollection<ElementId> gone = doc.Delete(new ElementId(123456L));
return gone.Count;   // 連帶刪除的元件數
```

### 改選取

```csharp
uidoc.Selection.SetElementIds(new List<ElementId> { new ElementId(123456L) });
return uidoc.Selection.GetElementIds().Count;
```

### 幫牆上色（取代目前視圖的圖形）

```csharp
OverrideGraphicSettings ogs = new OverrideGraphicSettings();
ogs.SetProjectionLineColor(new Color(255, 0, 0));
doc.ActiveView.SetElementOverrides(new ElementId(123456L), ogs);
return "ok";
```
