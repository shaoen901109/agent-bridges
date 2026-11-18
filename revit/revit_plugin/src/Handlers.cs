// Revit Agent Bridge — 全部指令的實作（在 Revit API 執行緒內執行，由 ApiHandler 呼叫）
//
// 約定：
//   * 對外長度一律公釐、角度一律度；Revit 內部是英尺 / 弧度，在這裡換算（見 Units）。
//   * 修改模型的指令各自包成一個 Transaction（Tx.Run），一次 Revit undo 剛好回復一步。
//   * 預期內的錯誤丟 BridgeError（訊息原樣給 agent）；其他例外由 Json.FromException 附上 backtrace。
//   * 只用 C# 5 語法。

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using Autodesk.Revit.UI;

namespace RevitAgentBridge
{
    internal static class Handlers
    {
        private static readonly string[] Names = new string[]
        {
            "ping", "get_status", "list_commands", "get_info", "get_scene_info", "list_elements", "get_element",
            "set_parameter", "create_wall", "create_floor", "create_level", "place_family_instance",
            "delete_elements", "select_elements", "set_view", "screenshot", "eval_csharp",
            "new_model", "open_model", "save_model", "undo", "redo"
        };

        public static List<object> CommandNames()
        {
            List<object> l = new List<object>();
            foreach (string n in Names) l.Add(n);
            return l;
        }

        public static object Invoke(UIApplication app, string method, Dictionary<string, object> p)
        {
            Tx.Reset();
            object result;
            switch (method)
            {
                case "get_info": result = BuildInfo(app, true); break;
                case "get_scene_info": result = GetSceneInfo(app, p); break;
                case "list_elements": result = ListElements(app, p); break;
                case "get_element": result = GetElement(app, p); break;
                case "set_parameter": result = SetParameter(app, p); break;
                case "create_wall": result = CreateWall(app, p); break;
                case "create_floor": result = CreateFloor(app, p); break;
                case "create_level": result = CreateLevel(app, p); break;
                case "place_family_instance": result = PlaceFamilyInstance(app, p); break;
                case "delete_elements": result = DeleteElements(app, p); break;
                case "select_elements": result = SelectElements(app, p); break;
                case "set_view": result = SetView(app, p); break;
                case "screenshot": result = Screenshot(app, p); break;
                case "eval_csharp": result = Evaluator.Run(app, p); break;
                case "new_model": result = NewModel(app, p); break;
                case "open_model": result = OpenModel(app, p); break;
                case "save_model": result = SaveModel(app, p); break;
                case "undo": result = PostUndoRedo(app, false); break;
                case "redo": result = PostUndoRedo(app, true); break;
                default:
                    throw new BridgeError("未知的指令：" + method + "（可用 list_commands 查看全部指令）。", "UnknownMethod");
            }
            Dictionary<string, object> d = result as Dictionary<string, object>;
            if (d != null && Tx.Warnings.Count > 0 && !d.ContainsKey("warnings"))
                d["warnings"] = new List<string>(Tx.Warnings);
            return result;
        }

        // ------------------------------------------------------------------
        // 共用小工具
        // ------------------------------------------------------------------

        private static Dictionary<string, object> D(params object[] kv)
        {
            Dictionary<string, object> d = new Dictionary<string, object>();
            for (int i = 0; i + 1 < kv.Length; i += 2) d[(string)kv[i]] = kv[i + 1];
            return d;
        }

        private static bool Valid(ElementId id) { return id != null && id.Value != -1; }

        private static string SafeName(Element e)
        {
            try { return e.Name; } catch (Exception) { return null; }
        }

        private static string CatName(Element e)
        {
            try { return e.Category == null ? null : e.Category.Name; } catch (Exception) { return null; }
        }

        private static Document RequireDoc(UIApplication app)
        {
            UIDocument ud = app.ActiveUIDocument;
            if (ud == null)
                throw new BridgeError("Revit 目前沒有開啟任何文件。請先用 revit_model_file（action=open 或 new）開檔，或請使用者開啟專案。", "NoDocument");
            return ud.Document;
        }

        private static Document RequireProjectDoc(UIApplication app)
        {
            Document doc = RequireDoc(app);
            if (doc.IsFamilyDocument)
                throw new BridgeError("目前作用中的是族群檔（.rfa），這個指令只支援專案檔（.rvt）。族群編輯請用 revit_eval_csharp。", "FamilyDocument");
            return doc;
        }

        private static Level FindLevel(Document doc, UIDocument uidoc, string name)
        {
            List<Level> levels = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>()
                .OrderBy(l => l.Elevation).ToList();
            if (name != null)
            {
                foreach (Level l in levels)
                    if (string.Equals(l.Name, name, StringComparison.OrdinalIgnoreCase)) return l;
                string names = string.Join("、", levels.Select(l => l.Name).ToArray());
                throw new BridgeError("找不到樓層「" + name + "」。現有樓層：" + (names.Length == 0 ? "（無）" : names) + "。", "NotFound");
            }
            if (levels.Count == 0)
                throw new BridgeError("文件裡沒有任何樓層，請先用 revit_create_level 建立。", "NotFound");
            if (uidoc != null)
            {
                ViewPlan vp = doc.ActiveView as ViewPlan;
                if (vp != null && vp.GenLevel != null) return vp.GenLevel;
            }
            return levels[0];
        }

        private static string LevelNameOf(Document doc, ElementId id)
        {
            if (!Valid(id)) return null;
            Element e = doc.GetElement(id);
            return e == null ? null : SafeName(e);
        }

        /// <summary>解析類別：接受 BuiltInCategory 名稱（OST_Walls / Walls / Wall）或文件內的（本地化）類別名稱。</summary>
        private static Category ResolveCategory(Document doc, string name)
        {
            string n = name.Trim();
            string[] tries = new string[] { n, "OST_" + n, "OST_" + n + "s", "OST_" + n + "es" };
            foreach (string t in tries)
            {
                BuiltInCategory bic;
                if (Enum.TryParse<BuiltInCategory>(t, true, out bic) && Enum.IsDefined(typeof(BuiltInCategory), bic))
                {
                    Category c = Category.GetCategory(doc, bic);
                    if (c != null) return c;
                }
            }
            foreach (Category c in doc.Settings.Categories)
                if (string.Equals(c.Name, n, StringComparison.OrdinalIgnoreCase)) return c;
            throw new BridgeError("找不到類別「" + name + "」。可用 BuiltInCategory 名稱（例如 OST_Walls、OST_Doors、OST_Floors、OST_Furniture），"
                                  + "或 revit_get_scene_info 回傳的類別名稱。", "NotFound");
        }

        private static ElementType FindType(Document doc, Type cls, string name, ElementTypeGroup group, string label)
        {
            List<ElementType> all = new FilteredElementCollector(doc).OfClass(cls).Cast<ElementType>().ToList();
            if (name != null)
            {
                List<ElementType> hit = all.Where(t => string.Equals(SafeName(t), name, StringComparison.OrdinalIgnoreCase)
                    || string.Equals(t.FamilyName + " : " + SafeName(t), name, StringComparison.OrdinalIgnoreCase)
                    || string.Equals(t.FamilyName + ":" + SafeName(t), name, StringComparison.OrdinalIgnoreCase)).ToList();
                if (hit.Count == 1) return hit[0];
                if (hit.Count > 1)
                    throw new BridgeError(label + "「" + name + "」有 " + hit.Count + " 個同名項目，請改用「族群名 : 類型名」。", "Ambiguous");
                string avail = string.Join("、", all.Take(30).Select(t => SafeName(t)).ToArray());
                throw new BridgeError("找不到" + label + "「" + name + "」。現有（最多列 30 個）：" + avail + "。", "NotFound");
            }
            ElementId defId = doc.GetDefaultElementTypeId(group);
            if (Valid(defId))
            {
                ElementType d = doc.GetElement(defId) as ElementType;
                if (d != null) return d;
            }
            if (all.Count == 0) throw new BridgeError("文件裡沒有任何" + label + "。", "NotFound");
            return all[0];
        }

        private static List<ElementId> ExistingIds(Document doc, List<ElementId> ids)
        {
            List<string> missing = new List<string>();
            foreach (ElementId id in ids)
                if (doc.GetElement(id) == null) missing.Add(id.Value.ToString());
            if (missing.Count > 0)
                throw new BridgeError("找不到這些元件 id：" + string.Join(", ", missing.Take(20).ToArray()) + "。", "NotFound");
            return ids;
        }

        // ------------------------------------------------------------------
        // 元件摘要 / 參數
        // ------------------------------------------------------------------

        private static Dictionary<string, object> Summ(Document doc, Element e, bool withBbox)
        {
            Dictionary<string, object> d = D("id", e.Id.Value, "name", SafeName(e), "category", CatName(e), "class", e.GetType().Name);
            ElementType et = e as ElementType;
            if (et != null) d["family"] = et.FamilyName;
            else
            {
                ElementId tid = e.GetTypeId();
                if (Valid(tid))
                {
                    Element t = doc.GetElement(tid);
                    if (t != null)
                    {
                        d["type_name"] = SafeName(t);
                        ElementType t2 = t as ElementType;
                        if (t2 != null) d["family"] = t2.FamilyName;
                    }
                }
                string lv = LevelNameOf(doc, e.LevelId);
                if (lv != null) d["level"] = lv;
            }
            try
            {
                LocationPoint lp = e.Location as LocationPoint;
                LocationCurve lc = e.Location as LocationCurve;
                if (lp != null) d["location_mm"] = D("point", Units.XyzToMm(lp.Point));
                else if (lc != null && lc.Curve != null && lc.Curve.IsBound)
                    d["location_mm"] = D("start", Units.XyzToMm(lc.Curve.GetEndPoint(0)), "end", Units.XyzToMm(lc.Curve.GetEndPoint(1)));
            }
            catch (Exception) { }
            if (withBbox)
            {
                try
                {
                    BoundingBoxXYZ bb = e.get_BoundingBox(null);
                    if (bb != null) d["bbox_mm"] = D("min", Units.XyzToMm(bb.Min), "max", Units.XyzToMm(bb.Max));
                }
                catch (Exception) { }
            }
            return d;
        }

        private static string SpecKind(Parameter prm)
        {
            try
            {
                ForgeTypeId dt = prm.Definition.GetDataType();
                if (dt != null)
                {
                    if (dt.Equals(SpecTypeId.Length)) return "length";
                    if (dt.Equals(SpecTypeId.Angle)) return "angle";
                }
            }
            catch (Exception) { }
            return "other";
        }

        private static Dictionary<string, object> ParamInfo(Document doc, Parameter prm)
        {
            Dictionary<string, object> d = D("name", prm.Definition.Name, "storage", prm.StorageType.ToString(), "read_only", prm.IsReadOnly);
            if (!prm.HasValue) { d["value"] = null; return d; }
            switch (prm.StorageType)
            {
                case StorageType.Double:
                    {
                        double raw = prm.AsDouble();
                        string kind = SpecKind(prm);
                        if (kind == "length") { d["value"] = Units.Round(Units.FtToMm(raw), 3); d["unit"] = "mm"; }
                        else if (kind == "angle") { d["value"] = Units.Round(raw * 180.0 / Math.PI, 4); d["unit"] = "deg"; }
                        else { d["value"] = raw; d["unit"] = "internal"; }
                        break;
                    }
                case StorageType.Integer: d["value"] = prm.AsInteger(); break;
                case StorageType.String: d["value"] = prm.AsString(); break;
                case StorageType.ElementId:
                    {
                        ElementId id = prm.AsElementId();
                        d["value"] = id.Value;
                        Element ref0 = Valid(id) ? doc.GetElement(id) : null;
                        if (ref0 != null) d["value_name"] = SafeName(ref0);
                        break;
                    }
                default: d["value"] = null; break;
            }
            try { string vs = prm.AsValueString(); if (vs != null) d["value_string"] = vs; } catch (Exception) { }
            return d;
        }

        // ------------------------------------------------------------------
        // get_info / get_scene_info / list_elements / get_element
        // ------------------------------------------------------------------

        public static Dictionary<string, object> BuildInfo(UIApplication app, bool withBridge)
        {
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["pong"] = true;
            d["bridge_version"] = "1.0.0";
            d["revit_version"] = app.Application.VersionNumber;
            d["revit_version_name"] = app.Application.VersionName;
            d["revit_build"] = app.Application.VersionBuild;
            d["api_context"] = true;
            d["stale"] = false;
            int docs = 0;
            try { foreach (Document x in app.Application.Documents) docs++; } catch (Exception) { }
            d["open_documents"] = docs;

            UIDocument ud = app.ActiveUIDocument;
            if (ud == null)
            {
                d["document"] = null;
            }
            else
            {
                Document doc = ud.Document;
                Dictionary<string, object> dd = D("title", doc.Title, "path", doc.PathName, "is_family", doc.IsFamilyDocument,
                    "is_modified", doc.IsModified, "is_workshared", doc.IsWorkshared);
                try { dd["length_unit"] = doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId().TypeId; } catch (Exception) { }
                try { dd["selected_count"] = ud.Selection.GetElementIds().Count; } catch (Exception) { }
                try
                {
                    View v = doc.ActiveView;
                    if (v != null) dd["active_view"] = D("id", v.Id.Value, "name", v.Name, "type", v.ViewType.ToString());
                }
                catch (Exception) { }
                d["document"] = dd;
            }
            if (withBridge)
            {
                d["bridge"] = App.Server.StatusDict();
                d["last_dialog"] = StateCache.LastDialog;
                d["units_note"] = "對外長度一律公釐（Revit 內部是英尺）；eval_csharp 內是英尺。";
            }
            return d;
        }

        private static object GetSceneInfo(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireDoc(app);
            int limit = Math.Max(1, Math.Min(P.Int(p, "limit", 200), 2000));

            List<object> levels = new List<object>();
            foreach (Level l in new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(x => x.Elevation))
                levels.Add(D("id", l.Id.Value, "name", l.Name, "elevation_mm", Units.Round(Units.FtToMm(l.Elevation), 3)));

            List<object> views = new List<object>();
            int viewTotal = 0;
            foreach (View v in new FilteredElementCollector(doc).OfClass(typeof(View)).Cast<View>())
            {
                if (v.IsTemplate) continue;
                ViewType vt = v.ViewType;
                if (vt == ViewType.ProjectBrowser || vt == ViewType.SystemBrowser || vt == ViewType.Internal || vt == ViewType.Undefined) continue;
                viewTotal++;
                if (views.Count >= limit) continue;
                Dictionary<string, object> vd = D("id", v.Id.Value, "name", v.Name, "type", vt.ToString());
                ViewPlan vp = v as ViewPlan;
                if (vp != null && vp.GenLevel != null) vd["level"] = vp.GenLevel.Name;
                views.Add(vd);
            }

            Dictionary<string, int> counts = new Dictionary<string, int>();
            int totalElements = 0;
            foreach (Element e in new FilteredElementCollector(doc).WhereElementIsNotElementType())
            {
                Category c = e.Category;
                if (c == null || c.CategoryType != CategoryType.Model) continue;
                int n;
                counts.TryGetValue(c.Name, out n);
                counts[c.Name] = n + 1;
                totalElements++;
            }
            Dictionary<string, object> catCounts = new Dictionary<string, object>();
            foreach (KeyValuePair<string, int> kv in counts.OrderByDescending(x => x.Value).Take(limit)) catCounts[kv.Key] = kv.Value;

            Dictionary<string, object> types = new Dictionary<string, object>();
            types["wall_types"] = new FilteredElementCollector(doc).OfClass(typeof(WallType)).Cast<ElementType>()
                .Take(40).Select(t => (object)SafeName(t)).ToList();
            types["floor_types"] = new FilteredElementCollector(doc).OfClass(typeof(FloorType)).Cast<ElementType>()
                .Take(40).Select(t => (object)SafeName(t)).ToList();
            int symbolCount = new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol)).GetElementCount();
            types["family_symbol_count"] = symbolCount;
            types["note"] = "要列族群類型請用 list_elements(types_only=true, category=...)。";

            return D("document", D("title", doc.Title, "path", doc.PathName, "is_family", doc.IsFamilyDocument),
                     "levels", levels, "views", views, "view_total", viewTotal,
                     "model_element_total", totalElements, "category_counts", catCounts, "types", types,
                     "units_note", "長度一律公釐。");
        }

        private static object ListElements(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireDoc(app);
            string cat = P.Str(p, "category");
            string typeName = P.Str(p, "type_name");
            string levelName = P.Str(p, "level");
            string nameContains = P.Str(p, "name_contains");
            bool typesOnly = P.Bool(p, "types_only", false);
            bool withBbox = P.Bool(p, "include_bbox", false);
            int limit = Math.Max(1, Math.Min(P.Int(p, "limit", 100), 1000));
            int offset = Math.Max(0, P.Int(p, "offset", 0));

            FilteredElementCollector col = new FilteredElementCollector(doc);
            if (cat != null) col = col.OfCategoryId(ResolveCategory(doc, cat).Id);
            col = typesOnly ? col.WhereElementIsElementType() : col.WhereElementIsNotElementType();

            Level lv = null;
            if (levelName != null) lv = FindLevel(doc, app.ActiveUIDocument, levelName);

            List<Element> matched = new List<Element>();
            foreach (Element e in col)
            {
                if (e.Category == null) continue;
                if (lv != null && !(Valid(e.LevelId) && e.LevelId.Value == lv.Id.Value)) continue;
                if (nameContains != null)
                {
                    string n = SafeName(e);
                    if (n == null || n.IndexOf(nameContains, StringComparison.OrdinalIgnoreCase) < 0) continue;
                }
                if (typeName != null && !typesOnly)
                {
                    ElementId tid = e.GetTypeId();
                    Element t = Valid(tid) ? doc.GetElement(tid) : null;
                    string tn = t == null ? null : SafeName(t);
                    if (tn == null || tn.IndexOf(typeName, StringComparison.OrdinalIgnoreCase) < 0) continue;
                }
                matched.Add(e);
            }

            List<object> items = new List<object>();
            foreach (Element e in matched.Skip(offset).Take(limit)) items.Add(Summ(doc, e, withBbox));
            return D("total", matched.Count, "offset", offset, "returned", items.Count, "elements", items,
                     "units_note", "長度一律公釐。");
        }

        private static object GetElement(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireDoc(app);
            if (!P.Has(p, "id")) throw new BridgeError("必須提供參數 id（元件 id 整數）。");
            long id = (long)Math.Round(P.RequireNum(p, "id"));
            Element e = doc.GetElement(new ElementId(id));
            if (e == null) throw new BridgeError("找不到元件 id=" + id + "。", "NotFound");

            List<string> filter = null;
            List<object> fl = P.List(p, "parameter_names");
            if (fl != null) filter = fl.Select(x => Convert.ToString(x)).ToList();

            List<object> prms = new List<object>();
            foreach (Parameter prm in e.Parameters.Cast<Parameter>().OrderBy(x => x.Definition.Name))
            {
                if (filter != null && !filter.Any(f => string.Equals(f, prm.Definition.Name, StringComparison.OrdinalIgnoreCase))) continue;
                try { prms.Add(ParamInfo(doc, prm)); } catch (Exception) { }
            }
            Dictionary<string, object> d = Summ(doc, e, true);
            d["parameters"] = prms;
            d["units_note"] = "length 參數的 value 是公釐、angle 是度；其他類型是 Revit 內部單位，另見 value_string。";
            return d;
        }

        // ------------------------------------------------------------------
        // set_parameter
        // ------------------------------------------------------------------

        private static object SetParameter(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireDoc(app);
            long id = (long)Math.Round(P.RequireNum(p, "id"));
            Element e = doc.GetElement(new ElementId(id));
            if (e == null) throw new BridgeError("找不到元件 id=" + id + "。", "NotFound");
            if (!P.Has(p, "value")) throw new BridgeError("必須提供參數 value。");
            string name = P.Str(p, "name");
            string builtIn = P.Str(p, "built_in");
            if (name == null && builtIn == null) throw new BridgeError("必須提供 name（參數名稱）或 built_in（BuiltInParameter 名稱，如 WALL_USER_HEIGHT_PARAM）。");

            Parameter prm = null;
            string scope = "instance";
            if (builtIn != null)
            {
                BuiltInParameter bip;
                if (!Enum.TryParse<BuiltInParameter>(builtIn, true, out bip))
                    throw new BridgeError("未知的 BuiltInParameter：" + builtIn + "。", "NotFound");
                prm = e.get_Parameter(bip);
                if (prm == null)
                {
                    Element t0 = Valid(e.GetTypeId()) ? doc.GetElement(e.GetTypeId()) : null;
                    if (t0 != null) { prm = t0.get_Parameter(bip); scope = "type"; }
                }
            }
            else
            {
                prm = e.LookupParameter(name);
                if (prm == null)
                {
                    Element t1 = Valid(e.GetTypeId()) ? doc.GetElement(e.GetTypeId()) : null;
                    if (t1 != null) { prm = t1.LookupParameter(name); scope = "type"; }
                }
            }
            if (prm == null)
                throw new BridgeError("元件 id=" + id + " 找不到參數「" + (name ?? builtIn) + "」（實例與型別都找過）。可先用 revit_get_element 看參數清單。", "NotFound");
            if (prm.IsReadOnly) throw new BridgeError("參數「" + prm.Definition.Name + "」是唯讀的。", "ReadOnly");

            object value = p["value"];
            string applied = Tx.Run<string>(doc, "Agent set_parameter", delegate ()
            {
                bool ok;
                switch (prm.StorageType)
                {
                    case StorageType.String:
                        ok = prm.Set(Convert.ToString(value));
                        break;
                    case StorageType.Integer:
                        if (value is bool) ok = prm.Set((bool)value ? 1 : 0);
                        else ok = prm.Set((int)Math.Round(P.ToDouble(value, "value")));
                        break;
                    case StorageType.ElementId:
                        ok = prm.Set(new ElementId((long)Math.Round(P.ToDouble(value, "value"))));
                        break;
                    case StorageType.Double:
                        if (value is string)
                        {
                            ok = prm.SetValueString((string)value); // 照專案顯示單位解析（例如 "3000" 或 "3 m"）
                        }
                        else
                        {
                            double v = P.ToDouble(value, "value");
                            string kind = SpecKind(prm);
                            if (kind == "length") v = Units.MmToFt(v);
                            else if (kind == "angle") v = v * Math.PI / 180.0;
                            ok = prm.Set(v);
                        }
                        break;
                    default:
                        throw new BridgeError("不支援的參數儲存型別：" + prm.StorageType + "。");
                }
                if (!ok) throw new BridgeError("設定參數「" + prm.Definition.Name + "」失敗（值不合法，或受限於其他約束）。");
                string vs = null;
                try { vs = prm.AsValueString(); } catch (Exception) { }
                return vs ?? "";
            });

            return D("id", id, "parameter", prm.Definition.Name, "scope", scope, "value_string", applied,
                     "note", scope == "type" ? "設定的是型別參數，會影響所有同型別元件。" : null);
        }

        // ------------------------------------------------------------------
        // 建立：牆、樓板、樓層、族群
        // ------------------------------------------------------------------

        private static object CreateWall(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireProjectDoc(app);
            double[] a = P.Point(p.ContainsKey("start") ? p["start"] : null, "start");
            double[] b = P.Point(p.ContainsKey("end") ? p["end"] : null, "end");
            double heightMm = P.Num(p, "height", 3000);
            double offsetMm = P.Num(p, "base_offset", 0);
            bool structural = P.Bool(p, "structural", false);
            if (heightMm <= 0) throw new BridgeError("height 必須大於 0（公釐）。");
            Level level = FindLevel(doc, app.ActiveUIDocument, P.Str(p, "level"));
            WallType wt = (WallType)FindType(doc, typeof(WallType), P.Str(p, "wall_type"), ElementTypeGroup.WallType, "牆類型");

            double z = level.Elevation;
            XYZ p0 = new XYZ(Units.MmToFt(a[0]), Units.MmToFt(a[1]), z);
            XYZ p1 = new XYZ(Units.MmToFt(b[0]), Units.MmToFt(b[1]), z);
            if (p0.DistanceTo(p1) < 0.01) throw new BridgeError("牆的起點與終點太近（小於約 3 mm）。");

            Wall wall = Tx.Run<Wall>(doc, "Agent create_wall", delegate ()
            {
                return Wall.Create(doc, Line.CreateBound(p0, p1), wt.Id, level.Id, Units.MmToFt(heightMm), Units.MmToFt(offsetMm), false, structural);
            });
            return D("id", wall.Id.Value, "wall_type", SafeName(wt), "level", level.Name,
                     "length_mm", Units.Round(Units.FtToMm(p0.DistanceTo(p1)), 3), "height_mm", heightMm);
        }

        private static object CreateFloor(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireProjectDoc(app);
            List<object> pts = P.List(p, "points");
            if (pts == null || pts.Count < 3) throw new BridgeError("points 必須是至少 3 個點的陣列：[[x,y],[x,y],...]（公釐，依序繞一圈，不必重複第一點）。");
            Level level = FindLevel(doc, app.ActiveUIDocument, P.Str(p, "level"));
            FloorType ft = (FloorType)FindType(doc, typeof(FloorType), P.Str(p, "floor_type"), ElementTypeGroup.FloorType, "樓板類型");

            List<XYZ> xs = new List<XYZ>();
            foreach (object o in pts)
            {
                double[] q = P.Point(o, "points 的每個點");
                xs.Add(new XYZ(Units.MmToFt(q[0]), Units.MmToFt(q[1]), level.Elevation));
            }
            CurveLoop loop = new CurveLoop();
            for (int i = 0; i < xs.Count; i++)
            {
                XYZ s = xs[i];
                XYZ e = xs[(i + 1) % xs.Count];
                if (s.DistanceTo(e) < 0.01) throw new BridgeError("points 內有重複或太近的相鄰點（第 " + (i + 1) + " 點）。");
                loop.Append(Line.CreateBound(s, e));
            }

            Floor floor = Tx.Run<Floor>(doc, "Agent create_floor", delegate ()
            {
                return Floor.Create(doc, new List<CurveLoop> { loop }, ft.Id, level.Id);
            });
            return D("id", floor.Id.Value, "floor_type", SafeName(ft), "level", level.Name, "point_count", xs.Count);
        }

        private static object CreateLevel(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireProjectDoc(app);
            double elevationMm = P.RequireNum(p, "elevation");
            string name = P.Str(p, "name");
            bool makePlan = P.Bool(p, "create_plan_view", true);

            Dictionary<string, object> r = Tx.Run<Dictionary<string, object>>(doc, "Agent create_level", delegate ()
            {
                Level lv = Level.Create(doc, Units.MmToFt(elevationMm));
                if (name != null)
                {
                    try { lv.Name = name; }
                    catch (Exception ex) { throw new BridgeError("無法命名為「" + name + "」（名稱可能重複）：" + ex.Message); }
                }
                Dictionary<string, object> d = D("id", lv.Id.Value, "name", lv.Name, "elevation_mm", elevationMm);
                if (makePlan)
                {
                    try
                    {
                        ViewFamilyType vft = new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
                            .FirstOrDefault(v => v.ViewFamily == ViewFamily.FloorPlan);
                        if (vft != null)
                        {
                            ViewPlan vp = ViewPlan.Create(doc, vft.Id, lv.Id);
                            d["plan_view_id"] = vp.Id.Value;
                            d["plan_view_name"] = vp.Name;
                        }
                    }
                    catch (Exception ex) { d["plan_view_error"] = ex.Message; }
                }
                return d;
            });
            return r;
        }

        private static List<FamilySymbol> MatchSymbols(Document doc, string spec)
        {
            List<FamilySymbol> all = new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol)).Cast<FamilySymbol>().ToList();
            Func<FamilySymbol, string> full = s => s.FamilyName + " : " + s.Name;
            List<FamilySymbol> tier = all.Where(s => string.Equals(full(s), spec, StringComparison.OrdinalIgnoreCase)
                || string.Equals(s.FamilyName + ":" + s.Name, spec, StringComparison.OrdinalIgnoreCase)).ToList();
            if (tier.Count > 0) return tier;
            tier = all.Where(s => string.Equals(s.Name, spec, StringComparison.OrdinalIgnoreCase)).ToList();
            if (tier.Count > 0) return tier;
            tier = all.Where(s => string.Equals(s.FamilyName, spec, StringComparison.OrdinalIgnoreCase)).ToList();
            if (tier.Count > 0) return tier;
            List<string> near = all.Where(s => full(s).IndexOf(spec, StringComparison.OrdinalIgnoreCase) >= 0).Take(30).Select(full).ToList();
            throw new BridgeError("找不到族群類型「" + spec + "」。" + (near.Count > 0
                ? "名稱相近的有：" + string.Join("；", near.ToArray()) + "。"
                : "專案內可能還沒載入該族群（要載入請用 revit_eval_csharp 的 doc.LoadFamily），或用 revit_list_elements(types_only=true, category=...) 查現有類型。"), "NotFound");
        }

        private static object PlaceFamilyInstance(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireProjectDoc(app);
            string spec = P.RequireStr(p, "family_type");
            double[] pt = P.Point(p.ContainsKey("point") ? p["point"] : null, "point");
            bool hasZ = false;
            List<object> pl = P.List(p, "point");
            if (pl != null && pl.Count == 3) hasZ = true;
            double rotation = P.Num(p, "rotation_deg", 0);
            Level level = FindLevel(doc, app.ActiveUIDocument, P.Str(p, "level"));

            List<FamilySymbol> matches = MatchSymbols(doc, spec);
            if (matches.Count > 1)
                throw new BridgeError("「" + spec + "」符合 " + matches.Count + " 個族群類型，請改用「族群名 : 類型名」。符合的有："
                    + string.Join("；", matches.Take(10).Select(s => s.FamilyName + " : " + s.Name).ToArray()) + "。", "Ambiguous");
            FamilySymbol symbol = matches[0];
            FamilyPlacementType placement = symbol.Family.FamilyPlacementType;

            double zFt = hasZ ? Units.MmToFt(pt[2]) : level.Elevation;
            XYZ xyz = new XYZ(Units.MmToFt(pt[0]), Units.MmToFt(pt[1]), zFt);

            Element host = null;
            if (placement == FamilyPlacementType.OneLevelBasedHosted)
            {
                if (P.Has(p, "host_id"))
                {
                    host = doc.GetElement(new ElementId((long)Math.Round(P.RequireNum(p, "host_id"))));
                    if (host == null) throw new BridgeError("找不到 host_id 指定的宿主元件。", "NotFound");
                }
                else
                {
                    double best = double.MaxValue;
                    foreach (Wall w in new FilteredElementCollector(doc).OfClass(typeof(Wall)).Cast<Wall>())
                    {
                        LocationCurve lc = w.Location as LocationCurve;
                        if (lc == null) continue;
                        XYZ probe = new XYZ(xyz.X, xyz.Y, lc.Curve.GetEndPoint(0).Z);
                        IntersectionResult ir = lc.Curve.Project(probe);
                        if (ir == null) continue;
                        if (ir.Distance < best) { best = ir.Distance; host = w; }
                    }
                    if (host == null || best > Units.MmToFt(600))
                        throw new BridgeError("這個族群（" + symbol.FamilyName + "）需要宿主牆，但 point 附近 600 mm 內沒有牆。請把 point 放在牆線上，或用 host_id 指定宿主。", "NoHost");
                    xyz = new XYZ(xyz.X, xyz.Y, hasZ ? zFt : lcZ(host));
                }
                Level hl = Valid(host.LevelId) ? doc.GetElement(host.LevelId) as Level : null;
                if (hl != null) level = hl;
            }
            else if (placement != FamilyPlacementType.OneLevelBased)
            {
                throw new BridgeError("這個族群的放置方式是 " + placement + "，revit_place_family_instance 只支援以點放置的單樓層族群與牆上的門窗。請改用 revit_eval_csharp。", "Unsupported");
            }

            StructuralType st = StructuralType.NonStructural;
            Category cat = symbol.Category;
            if (cat != null)
            {
                long cid = cat.Id.Value;
                if (cid == (long)BuiltInCategory.OST_StructuralColumns) st = StructuralType.Column;
                else if (cid == (long)BuiltInCategory.OST_StructuralFoundation) st = StructuralType.Footing;
            }

            FamilyInstance inst = Tx.Run<FamilyInstance>(doc, "Agent place_family_instance", delegate ()
            {
                if (!symbol.IsActive) { symbol.Activate(); doc.Regenerate(); }
                FamilyInstance fi = host != null
                    ? doc.Create.NewFamilyInstance(xyz, symbol, host, level, st)
                    : doc.Create.NewFamilyInstance(xyz, symbol, level, st);
                if (host == null && Math.Abs(rotation) > 1e-9)
                    ElementTransformUtils.RotateElement(doc, fi.Id, Line.CreateBound(xyz, xyz + XYZ.BasisZ), rotation * Math.PI / 180.0);
                return fi;
            });
            return D("id", inst.Id.Value, "family", symbol.FamilyName, "type", symbol.Name, "level", level.Name,
                     "host_id", host == null ? null : (object)host.Id.Value);
        }

        private static double lcZ(Element wall)
        {
            LocationCurve lc = wall.Location as LocationCurve;
            return lc == null ? 0 : lc.Curve.GetEndPoint(0).Z;
        }

        // ------------------------------------------------------------------
        // 刪除 / 選取
        // ------------------------------------------------------------------

        private static object DeleteElements(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireDoc(app);
            List<ElementId> ids = ExistingIds(doc, P.Ids(p, "ids"));
            if (ids.Count == 0) throw new BridgeError("ids 是空的。");
            ICollection<ElementId> deleted = Tx.Run<ICollection<ElementId>>(doc, "Agent delete_elements", delegate ()
            {
                return doc.Delete(ids);
            });
            return D("requested", ids.Count, "deleted_count", deleted.Count,
                     "deleted_ids", deleted.Take(200).Select(i => (object)i.Value).ToList(),
                     "note", "deleted_count 可能大於 requested：Revit 會連帶刪除相依元件（例如牆上的門窗）。");
        }

        private static object SelectElements(UIApplication app, Dictionary<string, object> p)
        {
            UIDocument ud = app.ActiveUIDocument;
            if (ud == null) RequireDoc(app);
            bool clear = P.Bool(p, "clear", false);
            bool add = P.Bool(p, "add", false);
            List<ElementId> ids = new List<ElementId>();
            if (!clear) ids = ExistingIds(ud.Document, P.Ids(p, "ids"));
            if (add)
            {
                HashSet<long> seen = new HashSet<long>(ids.Select(i => i.Value));
                foreach (ElementId cur in ud.Selection.GetElementIds())
                    if (seen.Add(cur.Value)) ids.Add(cur);
            }
            ud.Selection.SetElementIds(ids);
            return D("selected_count", ud.Selection.GetElementIds().Count);
        }

        // ------------------------------------------------------------------
        // 視圖與截圖
        // ------------------------------------------------------------------

        private static View3D FindOrCreate3D(Document doc, out bool created)
        {
            created = false;
            List<View3D> v3 = new FilteredElementCollector(doc).OfClass(typeof(View3D)).Cast<View3D>()
                .Where(v => !v.IsTemplate && !v.IsPerspective).ToList();
            View3D pick = v3.FirstOrDefault(v => v.Name == "{3D}") ?? v3.FirstOrDefault();
            if (pick != null) return pick;
            ViewFamilyType vft = new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
                .FirstOrDefault(v => v.ViewFamily == ViewFamily.ThreeDimensional);
            if (vft == null) throw new BridgeError("文件裡沒有 3D 視圖類型，無法建立 3D 視圖。");
            created = true;
            return Tx.Run<View3D>(doc, "Agent create 3D view", delegate ()
            {
                View3D v = View3D.CreateIsometric(doc, vft.Id);
                try { v.Name = "Agent 3D"; } catch (Exception) { }
                return v;
            });
        }

        private static object SetView(UIApplication app, Dictionary<string, object> p)
        {
            UIDocument ud = app.ActiveUIDocument;
            Document doc = RequireDoc(app);
            string name = P.Str(p, "view");
            bool zoom = P.Bool(p, "zoom_extents", false);
            View target = null;
            bool created = false;

            if (name != null)
            {
                if (string.Equals(name, "3d", StringComparison.OrdinalIgnoreCase))
                {
                    target = FindOrCreate3D(doc, out created);
                    if (created) zoom = true;
                }
                else
                {
                    long vid;
                    List<View> cands = new FilteredElementCollector(doc).OfClass(typeof(View)).Cast<View>().Where(v => !v.IsTemplate).ToList();
                    if (long.TryParse(name, out vid)) target = cands.FirstOrDefault(v => v.Id.Value == vid);
                    if (target == null) target = cands.FirstOrDefault(v => string.Equals(v.Name, name, StringComparison.OrdinalIgnoreCase));
                    if (target == null)
                    {
                        string avail = string.Join("、", cands.Where(v => v.ViewType != ViewType.ProjectBrowser && v.ViewType != ViewType.SystemBrowser
                            && v.ViewType != ViewType.Internal && v.ViewType != ViewType.Undefined).Take(40).Select(v => v.Name).ToArray());
                        throw new BridgeError("找不到視圖「" + name + "」。現有視圖（最多列 40 個）：" + avail + "。可用 \"3d\" 開 3D 視圖。", "NotFound");
                    }
                }
                if (ud.ActiveView == null || ud.ActiveView.Id.Value != target.Id.Value) ud.ActiveView = target;
            }
            else
            {
                target = doc.ActiveView;
            }

            if (zoom && target != null)
            {
                foreach (UIView uv in ud.GetOpenUIViews())
                {
                    if (uv.ViewId.Value == target.Id.Value) { uv.ZoomToFit(); break; }
                }
            }
            View active = doc.ActiveView;
            return D("active_view", active == null ? null : D("id", active.Id.Value, "name", active.Name, "type", active.ViewType.ToString()),
                     "created_3d_view", created, "zoomed", zoom);
        }

        private static object Screenshot(UIApplication app, Dictionary<string, object> p)
        {
            UIDocument ud = app.ActiveUIDocument;
            Document doc = RequireDoc(app);
            int px = Math.Max(200, Math.Min(P.Int(p, "pixel_size", 1600), 4000));
            View v = doc.ActiveView;
            if (v == null) throw new BridgeError("目前沒有作用中的視圖。");

            string dir = Path.Combine(Path.GetTempPath(), "revit_agent_bridge", "shot_" + DateTime.Now.ToString("yyyyMMdd_HHmmss_fff"));
            Directory.CreateDirectory(dir);

            ImageExportOptions o = new ImageExportOptions();
            o.ExportRange = ExportRange.CurrentView;
            o.FilePath = Path.Combine(dir, "view");   // Revit 可能自行補副檔名或附加視圖名稱，所以事後掃描資料夾
            o.HLRandWFViewsFileType = ImageFileType.PNG;
            o.ShadowViewsFileType = ImageFileType.PNG;
            o.ImageResolution = ImageResolution.DPI_150;
            o.ZoomType = ZoomFitType.FitToPage;
            o.PixelSize = px;
            o.FitDirection = FitDirectionType.Horizontal;
            doc.ExportImage(o);

            string[] files = Directory.GetFiles(dir);
            string pick = files.Where(f => f.EndsWith(".png", StringComparison.OrdinalIgnoreCase))
                .OrderByDescending(f => new FileInfo(f).LastWriteTime).FirstOrDefault();
            if (pick == null) pick = files.OrderByDescending(f => new FileInfo(f).LastWriteTime).FirstOrDefault();
            if (pick == null)
                throw new BridgeError("ExportImage 沒有產生任何檔案（作用中視圖「" + v.Name + "」可能不是可匯出的圖形視圖，例如明細表）。請先用 revit_set_view 切到平面 / 立面 / 剖面 / 3D 視圖。", "ExportFailed");
            return D("path", pick, "view_name", v.Name, "view_type", v.ViewType.ToString(), "pixel_size", px);
        }

        // ------------------------------------------------------------------
        // 檔案：new / open / save / save_as
        // ------------------------------------------------------------------

        private static string FindDefaultTemplate(Autodesk.Revit.ApplicationServices.Application a)
        {
            try
            {
                string t = a.DefaultProjectTemplate;
                if (!string.IsNullOrEmpty(t) && File.Exists(t)) return t;
            }
            catch (Exception) { }
            string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "Autodesk", "RVT " + a.VersionNumber, "Templates");
            if (Directory.Exists(root))
            {
                List<string> rte = Directory.GetFiles(root, "*.rte", SearchOption.AllDirectories).OrderBy(f => f).ToList();
                string pref = rte.FirstOrDefault(f => Path.GetFileName(f).IndexOf("Architectural", StringComparison.OrdinalIgnoreCase) >= 0
                    || Path.GetFileName(f).IndexOf("建築", StringComparison.OrdinalIgnoreCase) >= 0);
                if (pref != null) return pref;
                if (rte.Count > 0) return rte[0];
            }
            return null;
        }

        private static object NewModel(UIApplication app, Dictionary<string, object> p)
        {
            if (!P.Bool(p, "confirm", false))
                throw new BridgeError("new 需要 confirm=true（請先確認目前文件的未存變更，並取得使用者同意）。", "ConfirmRequired");
            string path = P.Str(p, "path");
            if (path == null)
                throw new BridgeError("new 需要 path：Revit API 無法顯示「未存檔的新專案」，所以新專案會先存到這個 .rvt 路徑再開啟。", "BadParams");
            if (!path.EndsWith(".rvt", StringComparison.OrdinalIgnoreCase)) path += ".rvt";
            path = Path.GetFullPath(path);
            bool overwrite = P.Bool(p, "overwrite", false);
            if (File.Exists(path) && !overwrite)
                throw new BridgeError("檔案已存在：" + path + "。要覆蓋需 overwrite=true（要先取得使用者同意）。", "ConfirmRequired");

            string template = P.Str(p, "template");
            if (template == null) template = FindDefaultTemplate(app.Application);
            if (template == null || !File.Exists(template))
                throw new BridgeError("找不到專案樣板（.rte）。請用 template 參數指定樣板的完整路徑（預設位置在 C:\\ProgramData\\Autodesk\\RVT 2024\\Templates）。", "NoTemplate");

            Directory.CreateDirectory(Path.GetDirectoryName(path));
            Document nd = app.Application.NewProjectDocument(template);
            try
            {
                SaveAsOptions so = new SaveAsOptions();
                so.OverwriteExistingFile = overwrite;
                nd.SaveAs(path, so);
            }
            finally
            {
                try { nd.Close(false); } catch (Exception) { }
            }
            UIDocument ud = app.OpenAndActivateDocument(path);
            return D("path", path, "template", template, "title", ud.Document.Title);
        }

        private static object OpenModel(UIApplication app, Dictionary<string, object> p)
        {
            if (!P.Bool(p, "confirm", false))
                throw new BridgeError("open 需要 confirm=true（請先確認目前文件的未存變更，並取得使用者同意）。", "ConfirmRequired");
            string path = P.RequireStr(p, "path");
            path = Path.GetFullPath(path);
            if (!File.Exists(path)) throw new BridgeError("找不到檔案：" + path + "。", "NotFound");
            bool prevModified = app.ActiveUIDocument != null && app.ActiveUIDocument.Document.IsModified;
            UIDocument ud = app.OpenAndActivateDocument(path);
            return D("path", path, "title", ud.Document.Title, "previous_document_modified", prevModified,
                     "note", prevModified ? "先前作用中的文件有未存變更，它仍然開著（沒有被關閉）。" : null);
        }

        private static object SaveModel(UIApplication app, Dictionary<string, object> p)
        {
            Document doc = RequireDoc(app);
            string path = P.Str(p, "path");
            bool overwrite = P.Bool(p, "overwrite", false);
            if (path == null)
            {
                if (!P.Bool(p, "confirm", false))
                    throw new BridgeError("save 需要 confirm=true（覆寫目前檔案，必須使用者同意）。", "ConfirmRequired");
                if (string.IsNullOrEmpty(doc.PathName))
                    throw new BridgeError("這個文件還沒有存過檔，請改用 save_as 並提供 path。", "BadParams");
                doc.Save();
                return D("path", doc.PathName, "saved", true);
            }
            string ext = doc.IsFamilyDocument ? ".rfa" : ".rvt";
            if (!path.EndsWith(ext, StringComparison.OrdinalIgnoreCase)) path += ext;
            path = Path.GetFullPath(path);
            if (File.Exists(path) && !overwrite)
                throw new BridgeError("檔案已存在：" + path + "。要覆蓋需 overwrite=true（要先取得使用者同意）。", "ConfirmRequired");
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            SaveAsOptions so = new SaveAsOptions();
            so.OverwriteExistingFile = overwrite;
            doc.SaveAs(path, so);
            return D("path", path, "saved", true);
        }

        private static object PostUndoRedo(UIApplication app, bool redo)
        {
            RevitCommandId cid = RevitCommandId.LookupPostableCommandId(redo ? PostableCommand.Redo : PostableCommand.Undo);
            if (!app.CanPostCommand(cid))
                throw new BridgeError("目前無法投遞" + (redo ? "重做" : "復原") + "指令（可能沒有可" + (redo ? "重做" : "復原") + "的操作，或 Revit 正忙）。", "CannotPost");
            app.PostCommand(cid);
            return D("posted", true, "direction", redo ? "redo" : "undo",
                     "note", "這是非同步的：指令已投遞給 Revit，會在目前的 API 呼叫結束後才執行。請稍候再用 get_info / 截圖確認結果。");
        }
    }
}
