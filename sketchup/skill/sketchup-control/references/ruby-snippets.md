# SketchUp Ruby API 常用片段

給 `sketchup_eval_ruby`（或 CLI `eval --file`）使用。
所有會改模型的片段都已包在 `start_operation` / `commit_operation` 內。

單位提醒：SketchUp 內部是**英吋**。一律寫 `3000.mm`、`2.5.m`、`10.cm`，不要寫裸數字。

---

## 0. 取得常用物件

```ruby
model  = Sketchup.active_model
ents   = model.entities            # 最上層幾何
sel    = model.selection
view   = model.active_view
layers = model.layers              # 即 UI 上的「標籤 Tags」
mats   = model.materials
```

由 persistent_id 取回實體（跨存檔仍有效）：

```ruby
entity = Sketchup.active_model.find_entity_by_persistent_id(12345)
```

---

## 1. 建一道牆（有厚度的量體）

```ruby
model = Sketchup.active_model
model.start_operation('建立牆體', true)
begin
  group = model.entities.add_group
  ents  = group.entities

  length    = 4000.mm
  thickness = 200.mm
  height    = 2800.mm

  face = ents.add_face(
    [0, 0, 0],
    [length, 0, 0],
    [length, thickness, 0],
    [0, thickness, 0]
  )
  face.reverse! if face.normal.z < 0   # 確保往上擠
  face.pushpull(height)

  group.name = '牆-A'
  model.commit_operation
  group.persistent_id
rescue => e
  model.abort_operation
  raise e
end
```

## 2. 在牆上開洞（門窗洞）

在**同一個 group 內**畫出洞口輪廓，再把洞口面往牆的另一側推穿。

```ruby
model = Sketchup.active_model
wall  = model.find_entity_by_persistent_id(WALL_PID)   # 換成實際 id
model.start_operation('開窗洞', true)
begin
  ents = wall.entities
  # 找出牆體朝 -Y（外側）的大面
  outer = ents.grep(Sketchup::Face).select { |f| f.normal.y < -0.9 }.max_by(&:area)

  z0, z1 = 900.mm, 2100.mm
  x0, x1 = 1000.mm, 2200.mm
  y = outer.vertices.first.position.y

  hole = ents.add_face(
    [x0, y, z0], [x1, y, z0], [x1, y, z1], [x0, y, z1]
  )
  hole.pushpull(-300.mm)      # 推穿 200mm 厚的牆，多推一點確保穿透
  model.commit_operation
rescue => e
  model.abort_operation
  raise e
end
```

## 3. 陣列複製（線性 / 矩形）

```ruby
model  = Sketchup.active_model
source = model.find_entity_by_persistent_id(SOURCE_PID)  # Group 或 ComponentInstance
model.start_operation('陣列複製', true)
begin
  ids = []
  6.times do |i|
    next if i.zero?
    t = Geom::Transformation.translation(Geom::Vector3d.new(1200.mm * i, 0, 0))
    copy = model.entities.add_instance(source.definition, source.transformation)
    copy.transform!(t)
    ids << copy.persistent_id
  end
  model.commit_operation
  ids
rescue => e
  model.abort_operation
  raise e
end
```

## 4. 材質與顏色

```ruby
model = Sketchup.active_model
model.start_operation('上材質', true)
begin
  mat = model.materials['清水混凝土'] || model.materials.add('清水混凝土')
  mat.color = Sketchup::Color.new(180, 178, 172)
  mat.alpha = 1.0

  target = model.find_entity_by_persistent_id(PID)
  target.material = mat          # Group / Instance / Face 都適用
  # 只上背面：face.back_material = mat
  model.commit_operation
rescue => e
  model.abort_operation
  raise e
end
```

## 5. 標籤（Tags / Layers）

```ruby
model = Sketchup.active_model
model.start_operation('整理標籤', true)
begin
  tag = model.layers['結構'] || model.layers.add('結構')
  tag.visible = true
  model.find_entity_by_persistent_id(PID).layer = tag
  model.commit_operation
rescue => e
  model.abort_operation
  raise e
end
```

分類統計（大模型安全寫法，不傾印全部）：

```ruby
Sketchup.active_model.entities.group_by { |e| e.class.name }
        .map { |k, v| [k, v.size] }.to_h
```

## 6. 場景頁面（Scenes）

```ruby
model = Sketchup.active_model
model.start_operation('新增場景', true)
begin
  page = model.pages.add('外觀-西南')
  page.use_camera = true
  model.commit_operation
  page.name
rescue => e
  model.abort_operation
  raise e
end
```

切換場景：`Sketchup.active_model.pages.selected_page = model.pages['外觀-西南']`

## 7. 相機（比 set_camera tool 更細的控制）

```ruby
model  = Sketchup.active_model
view   = model.active_view
camera = view.camera

eye    = Geom::Point3d.new(12.m, -15.m, 6.m)
target = Geom::Point3d.new(2.m, 2.m, 1.5.m)
camera.set(eye, target, Z_AXIS)
camera.perspective = true
camera.fov = 45.0
view.invalidate
```

人眼高度的兩點透視：

```ruby
view.camera.set(Geom::Point3d.new(0, -20.m, 1.6.m),
                Geom::Point3d.new(0, 0, 1.6.m), Z_AXIS)
Sketchup.send_action('viewTwoPoint:') rescue nil
```

## 8. 元件定義與實例

```ruby
model = Sketchup.active_model
model.start_operation('建立元件', true)
begin
  definition = model.definitions.add('椅子')
  definition.entities.add_face([0,0,0], [450.mm,0,0], [450.mm,450.mm,0], [0,450.mm,0])
                     .pushpull(450.mm)
  instance = model.entities.add_instance(
    definition, Geom::Transformation.translation(Geom::Vector3d.new(2.m, 0, 0))
  )
  model.commit_operation
  instance.persistent_id
rescue => e
  model.abort_operation
  raise e
end
```

把既有 Group 轉成元件：`group.to_component`

## 9. 安全查詢（不會爆 context）

```ruby
model = Sketchup.active_model
{
  'faces'  => model.entities.grep(Sketchup::Face).size,
  'groups' => model.entities.grep(Sketchup::Group).map { |g| [g.persistent_id, g.name] }.first(20),
  'bbox_m' => [model.bounds.width.to_m, model.bounds.height.to_m, model.bounds.depth.to_m].map { |v| v.round(3) }
}
```

## 10. 常見陷阱

| 症狀 | 原因與處理 |
|---|---|
| 新幾何跟舊幾何黏在一起、互相切割 | 沒有包 Group／沒有在 group.entities 內建立 |
| 擠出方向相反 | `face.normal` 不是你以為的方向；先讀 normal 再決定正負 |
| 面呈藍灰色（反面朝外） | `face.reverse!`，或整組 `ents.grep(Sketchup::Face).each(&:reverse!)` |
| `add_face` 回傳 nil | 點共線、不共面、或重複點 |
| 改了一個元件其他也跟著變 | 元件共用定義，這是正常行為；要獨立請先 `make_unique` |
| 使用者要按很多次 Ctrl+Z | 沒包 `start_operation` / `commit_operation` |
| 存檔後 id 全部對不上 | 用了 `entityID`；改用 `persistent_id` |
| 指令一直逾時沒回應 | SketchUp 可能跳了對話框——用全視窗截圖確認 |
