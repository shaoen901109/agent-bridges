# Blender bpy 常用片段

給 `blender_eval_python`（或 CLI `eval --file`）使用。
程式碼在 **timer context** 執行：`bpy.context.object` / `selected_objects` 不可靠，
一律用 `bpy.data` 與 `view_layer`；需要 `bpy.ops` 時用 temp_override（見第 1 節）。
`bpy`、`bmesh`、`mathutils`、`math`、`Vector`、`Matrix`、`Euler`、`Quaternion` 已預載。
要回傳的值指定給 `result`。

單位提醒：長度**公尺**、`rotation_euler` **弧度**（`math.radians(45)`）。

---

## 0. 取得常用物件

```python
scene = bpy.context.scene or bpy.data.scenes[0]
vl    = bpy.context.view_layer or scene.view_layers[0]
ob    = bpy.data.objects.get("Cube")          # 找不到回 None，不會丟例外
active = vl.objects.active
selected = [o for o in scene.objects if o.select_get(view_layer=vl)]
result = {"active": active.name if active else None, "selected": [o.name for o in selected]}
```

## 1. temp_override 樣板（呼叫需要 UI context 的 operator）

```python
def view3d_override():
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                region = next(r for r in area.regions if r.type == 'WINDOW')
                return dict(window=window, screen=window.screen, area=area, region=region)
    return dict(window=wm.windows[0], screen=wm.windows[0].screen)

ob = bpy.data.objects["Cube"]
vl = bpy.context.view_layer
for o in vl.objects: o.select_set(False)
ob.select_set(True); vl.objects.active = ob
with bpy.context.temp_override(**view3d_override()):
    bpy.ops.object.shade_smooth()          # 任何 bpy.ops.object.* / mesh.* / view3d.*
result = ob.name
```

## 2. 用 bmesh 建 mesh（不依賴 ops、不依賴選取）

```python
import bmesh
def make_box(name, size=(1, 1, 1), location=(0, 0, 0), collection="Bridge"):
    mesh = bpy.data.meshes.new(name + "_mesh")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=size, verts=bm.verts)
    bm.to_mesh(mesh); bm.free()
    ob = bpy.data.objects.new(name, mesh)
    ob.location = location
    coll = bpy.data.collections.get(collection)
    if coll is None:
        coll = bpy.data.collections.new(collection)
        bpy.context.scene.collection.children.link(coll)
    coll.objects.link(ob)
    return ob

wall = make_box("Wall_A", size=(4.0, 0.2, 2.8), location=(0, 0, 1.4))
result = {"name": wall.name, "dims": list(wall.dimensions)}
```

多邊形擠出（樓板／量體）：

```python
import bmesh
pts = [(0, 0), (6, 0), (6, 4), (0, 4)]          # 平面輪廓（公尺）
height = 3.0
mesh = bpy.data.meshes.new("Slab_mesh"); bm = bmesh.new()
verts = [bm.verts.new((x, y, 0)) for x, y in pts]
face = bm.faces.new(verts)
res = bmesh.ops.extrude_face_region(bm, geom=[face])
up = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
bmesh.ops.translate(bm, vec=(0, 0, height), verts=up)
bm.normal_update(); bm.to_mesh(mesh); bm.free()
ob = bpy.data.objects.new("Slab", mesh); bpy.context.scene.collection.objects.link(ob)
result = ob.name
```

## 3. Modifier

```python
ob = bpy.data.objects["Wall_A"]
m = ob.modifiers.new("Bevel", type='BEVEL'); m.width = 0.02; m.segments = 3
a = ob.modifiers.new("Array", type='ARRAY'); a.count = 5; a.relative_offset_displace = (1.2, 0, 0)
s = ob.modifiers.new("Solidify", type='SOLIDIFY'); s.thickness = 0.1
result = [mod.name for mod in ob.modifiers]
```

套用 modifier（需要 ops + override）：

```python
ob = bpy.data.objects["Wall_A"]
vl = bpy.context.view_layer; vl.objects.active = ob; ob.select_set(True)
with bpy.context.temp_override(object=ob, active_object=ob, selected_objects=[ob]):
    bpy.ops.object.modifier_apply(modifier="Solidify")
```

## 4. 材質與顏色

```python
def get_material(name, rgba=(0.8, 0.8, 0.8, 1.0), roughness=0.5, metallic=0.0):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name); mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = rgba
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    mat.diffuse_color = rgba                     # Solid 著色模式也看得到顏色
    return mat

ob = bpy.data.objects["Wall_A"]
mat = get_material("清水混凝土", (0.55, 0.54, 0.52, 1.0), roughness=0.9)
if ob.data.materials: ob.data.materials[0] = mat
else: ob.data.materials.append(mat)
result = mat.name
```

Solid 模式要看到物件顏色：`blender_set_view(shading="solid")` 後
`space.shading.color_type = 'MATERIAL'`（在 eval 裡：找 VIEW_3D area → `area.spaces.active.shading.color_type = 'MATERIAL'`）。

## 5. Collection

```python
scene = bpy.context.scene
coll = bpy.data.collections.get("Bridge") or bpy.data.collections.new("Bridge")
if coll.name not in scene.collection.children:
    scene.collection.children.link(coll)
ob = bpy.data.objects["Wall_A"]
for c in list(ob.users_collection): c.objects.unlink(ob)   # 從原 collection 移出
coll.objects.link(ob)
coll.hide_viewport = False
result = {c.name: len(c.objects) for c in bpy.data.collections}
```

## 6. Armature 與 vertex group 綁定

```python
# 建 armature
arm_data = bpy.data.armatures.new("Rig")
arm = bpy.data.objects.new("Rig", arm_data)
bpy.context.scene.collection.objects.link(arm)
vl = bpy.context.view_layer; vl.objects.active = arm
with bpy.context.temp_override(object=arm, active_object=arm):
    bpy.ops.object.mode_set(mode='EDIT')
    b1 = arm_data.edit_bones.new("Spine"); b1.head = (0, 0, 0); b1.tail = (0, 0, 1)
    b2 = arm_data.edit_bones.new("Arm.L"); b2.head = (0, 0, 1); b2.tail = (0.6, 0, 1); b2.parent = b1
    b3 = arm_data.edit_bones.new("Arm.R"); b3.head = (0, 0, 1); b3.tail = (-0.6, 0, 1); b3.parent = b1
    bpy.ops.object.mode_set(mode='OBJECT')

# 綁 mesh：Armature modifier + 同名 vertex group
mesh_ob = bpy.data.objects["Body"]
mod = mesh_ob.modifiers.new("Armature", type='ARMATURE'); mod.object = arm
mesh_ob.parent = arm
vg = mesh_ob.vertex_groups.get("Spine") or mesh_ob.vertex_groups.new(name="Spine")
vg.add([v.index for v in mesh_ob.data.vertices], 1.0, 'REPLACE')
result = [b.name for b in arm_data.bones]
```

自動權重（需要 ops + 兩者都選取、armature 為 active）：

```python
mesh_ob = bpy.data.objects["Body"]; arm = bpy.data.objects["Rig"]
vl = bpy.context.view_layer
for o in vl.objects: o.select_set(False)
mesh_ob.select_set(True); arm.select_set(True); vl.objects.active = arm
with bpy.context.temp_override(object=arm, active_object=arm, selected_objects=[mesh_ob, arm],
                               selected_editable_objects=[mesh_ob, arm]):
    bpy.ops.object.parent_set(type='ARMATURE_AUTO')
```

## 7. 相機與燈光

```python
scene = bpy.context.scene
cam_data = bpy.data.cameras.new("Cam"); cam = bpy.data.objects.new("Cam", cam_data)
scene.collection.objects.link(cam)
cam.location = (8, -8, 5)
cam.rotation_euler = (math.radians(63), 0, math.radians(45))
cam_data.lens = 35
scene.camera = cam                                  # blender_render 會用它

light_data = bpy.data.lights.new("Sun", type='SUN'); light_data.energy = 3.0
sun = bpy.data.objects.new("Sun", light_data); scene.collection.objects.link(sun)
sun.rotation_euler = (math.radians(50), 0, math.radians(30))
result = {"camera": scene.camera.name}
```

讓相機對準目標（Track To）：

```python
cam = bpy.data.objects["Cam"]; target = bpy.data.objects["Wall_A"]
c = cam.constraints.new(type='TRACK_TO'); c.target = target
c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'
```

## 8. Workbench 算圖（比 blender_render 更細的控制）

```python
scene = bpy.context.scene
r = scene.render
r.engine = 'BLENDER_WORKBENCH'
scene.display.shading.light = 'STUDIO'
scene.display.shading.color_type = 'MATERIAL'      # 或 'OBJECT' / 'RANDOM'
scene.display.shading.show_shadows = True
scene.display.shading.show_cavity = True
r.resolution_x, r.resolution_y, r.resolution_percentage = 1280, 720, 100
r.filepath = r"C:\temp\wb.png"; r.image_settings.file_format = 'PNG'
bpy.ops.render.render(write_still=True)
result = r.filepath
```

（一般情況直接用 `blender_render(engine="WORKBENCH")`，它會自動還原設定。）

## 9. 匯出 glTF / OBJ / FBX

```python
import os
out = r"C:\temp\export"
os.makedirs(out, exist_ok=True)
vl = bpy.context.view_layer
for o in vl.objects: o.select_set(False)
for name in ["Wall_A", "Slab"]:
    bpy.data.objects[name].select_set(True)
with bpy.context.temp_override(selected_objects=[bpy.data.objects[n] for n in ["Wall_A", "Slab"]]):
    bpy.ops.export_scene.gltf(filepath=os.path.join(out, "model.glb"), use_selection=True, export_format='GLB')
    bpy.ops.export_scene.fbx(filepath=os.path.join(out, "model.fbx"), use_selection=True)
    bpy.ops.wm.obj_export(filepath=os.path.join(out, "model.obj"), export_selected_objects=True)  # 3.4 新版 OBJ 匯出
result = os.listdir(out)
```

匯出**前**先問使用者路徑；不要覆蓋既有檔案。

## 10. 批次改名 / 批次查詢

```python
import re
renamed = []
for ob in bpy.data.objects:
    if ob.type == 'MESH' and re.match(r"^Cube\.\d+$", ob.name):
        new = "Box_%02d" % len(renamed); ob.name = new; renamed.append(new)
result = renamed
```

安全查詢（不會爆 context）：

```python
scene = bpy.context.scene
from collections import Counter
result = {
    "counts": dict(Counter(o.type for o in scene.objects)),
    "meshes_top20": [(o.name, len(o.data.vertices)) for o in scene.objects if o.type == 'MESH'][:20],
    "bbox_m": [round(v, 3) for v in bpy.data.objects["Wall_A"].dimensions],
}
```

## 11. 常見陷阱

| 症狀 | 原因與處理 |
|---|---|
| `bpy.context.object` 是 None / `AttributeError: 'Context' object has no attribute` | timer context；改用 `bpy.data.objects[...]`、`view_layer.objects.active` |
| `RuntimeError: Operator bpy.ops.xxx.poll() failed, context is incorrect` | 缺 override；用第 1 節樣板，或改用 `bpy.data`/`bmesh` 做同樣的事 |
| 新物件看不到 | 沒有 `collection.objects.link(ob)`，或 collection 被 `hide_viewport` |
| 旋轉數字怪 | `rotation_euler` 是弧度；捷徑 tool 用度 |
| 改了 mesh 沒反應 | 物件在 Edit Mode；先切回 Object Mode |
| bmesh 寫入後法線怪 | `bm.normal_update()` 再 `to_mesh`，或 `mesh.update()` |
| 使用者要按很多次 Ctrl+Z | 用 `undo_push=False` 跑了很多次 eval；預設每次 eval 一步 |
| 指令逾時沒回應 | Blender 跳了對話框或在 modal operator（例如算圖視窗）——用全視窗截圖確認 |
| `is_dirty` 一直是 True | 只在 --background 測試實例會這樣；GUI 正常 |
| 截圖是舊畫面 | 橋接會先 tag_redraw 再延遲截圖；仍舊就先 `blender_set_view(zoom_extents=True)` 再截 |
