// Revit Agent Bridge — 共用小工具：記錄檔、錯誤型別、JSON 轉換、參數讀取、單位換算
// 只用 C# 5 語法。

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using Autodesk.Revit.DB;

namespace RevitAgentBridge
{
    /// <summary>預期內的錯誤（參數不對、找不到物件…）。訊息會原樣給 agent 看，不附 backtrace。</summary>
    public class BridgeError : Exception
    {
        public string ErrorClass;
        public BridgeError(string message) : base(message) { ErrorClass = "BridgeError"; }
        public BridgeError(string message, string errorClass) : base(message) { ErrorClass = errorClass; }
    }

    // ---------------------------------------------------------------------
    // 簡單的檔案記錄（排錯用），失敗一律吞掉
    // ---------------------------------------------------------------------
    internal static class BridgeLog
    {
        private static readonly object Gate = new object();
        public static void Write(string text)
        {
            try
            {
                string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RevitAgentBridge");
                Directory.CreateDirectory(dir);
                lock (Gate)
                {
                    string file = Path.Combine(dir, "bridge.log");
                    try { if (File.Exists(file) && new FileInfo(file).Length > 512 * 1024) File.Delete(file); } catch (Exception) { }
                    File.AppendAllText(file, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss ") + text + Environment.NewLine, Encoding.UTF8);
                }
            }
            catch (Exception) { }
        }
    }

    // ---------------------------------------------------------------------
    // 單位：Revit API 內部長度是英尺；對外一律公釐
    // ---------------------------------------------------------------------
    internal static class Units
    {
        public const double MmPerFoot = 304.8;
        public static double MmToFt(double mm) { return mm / MmPerFoot; }
        public static double FtToMm(double ft) { return ft * MmPerFoot; }
        public static double Round(double v, int digits) { return Math.Round(v, digits); }
        public static double[] XyzToMm(XYZ p)
        {
            return new double[] { Round(FtToMm(p.X), 3), Round(FtToMm(p.Y), 3), Round(FtToMm(p.Z), 3) };
        }
    }

    // ---------------------------------------------------------------------
    // 請求參數讀取
    // ---------------------------------------------------------------------
    internal static class P
    {
        public static bool Has(Dictionary<string, object> p, string key)
        {
            return p != null && p.ContainsKey(key) && p[key] != null;
        }

        public static string Str(Dictionary<string, object> p, string key)
        {
            if (!Has(p, key)) return null;
            string s = Convert.ToString(p[key], CultureInfo.InvariantCulture);
            if (s != null) s = s.Trim();
            return string.IsNullOrEmpty(s) ? null : s;
        }

        public static string RequireStr(Dictionary<string, object> p, string key)
        {
            string s = Str(p, key);
            if (s == null) throw new BridgeError("必須提供參數 " + key + "。");
            return s;
        }

        public static double ToDouble(object o, string what)
        {
            try { return Convert.ToDouble(o, CultureInfo.InvariantCulture); }
            catch (Exception) { throw new BridgeError(what + " 必須是數字，收到：" + Convert.ToString(o) + "。"); }
        }

        public static double Num(Dictionary<string, object> p, string key, double defaultValue)
        {
            if (!Has(p, key)) return defaultValue;
            return ToDouble(p[key], key);
        }

        public static double RequireNum(Dictionary<string, object> p, string key)
        {
            if (!Has(p, key)) throw new BridgeError("必須提供參數 " + key + "（數字）。");
            return ToDouble(p[key], key);
        }

        public static int Int(Dictionary<string, object> p, string key, int defaultValue)
        {
            return (int)Math.Round(Num(p, key, defaultValue));
        }

        public static bool Bool(Dictionary<string, object> p, string key, bool defaultValue)
        {
            if (!Has(p, key)) return defaultValue;
            object o = p[key];
            if (o is bool) return (bool)o;
            string s = Convert.ToString(o, CultureInfo.InvariantCulture).Trim().ToLowerInvariant();
            if (s == "true" || s == "1" || s == "yes") return true;
            if (s == "false" || s == "0" || s == "no") return false;
            return defaultValue;
        }

        /// <summary>JSON 陣列在 JavaScriptSerializer 可能是 object[] 或 ArrayList，一律轉成 List&lt;object&gt;。</summary>
        public static List<object> List(object o)
        {
            if (o == null || o is string) return null;
            IEnumerable e = o as IEnumerable;
            if (e == null || o is IDictionary) return null;
            List<object> list = new List<object>();
            foreach (object x in e) list.Add(x);
            return list;
        }

        public static List<object> List(Dictionary<string, object> p, string key)
        {
            if (!Has(p, key)) return null;
            return List(p[key]);
        }

        /// <summary>[x,y] 或 [x,y,z]（公釐）→ double[3]（缺 z 補 0）。</summary>
        public static double[] Point(object o, string what)
        {
            List<object> l = List(o);
            if (l == null || l.Count < 2 || l.Count > 3)
                throw new BridgeError(what + " 必須是 [x, y] 或 [x, y, z]（公釐），收到：" + Convert.ToString(o) + "。");
            double[] r = new double[3];
            for (int i = 0; i < l.Count; i++) r[i] = ToDouble(l[i], what);
            return r;
        }

        public static List<ElementId> Ids(Dictionary<string, object> p, string key)
        {
            List<object> l = List(p, key);
            if (l == null) throw new BridgeError("必須提供 " + key + "（元件 id 的整數陣列）。");
            List<ElementId> ids = new List<ElementId>();
            foreach (object o in l)
            {
                long v;
                try { v = Convert.ToInt64(Convert.ToDouble(o, CultureInfo.InvariantCulture)); }
                catch (Exception) { throw new BridgeError(key + " 內含不是整數的項目：" + Convert.ToString(o) + "。"); }
                ids.Add(new ElementId(v));
            }
            return ids;
        }
    }

    // ---------------------------------------------------------------------
    // JSON：解析請求、把任意物件正規化成可序列化結構、組回應
    // ---------------------------------------------------------------------
    internal static class Json
    {
        private static JavaScriptSerializer NewSerializer()
        {
            JavaScriptSerializer s = new JavaScriptSerializer();
            s.MaxJsonLength = int.MaxValue;
            s.RecursionLimit = 200;
            return s;
        }

        public static object Parse(string text) { return NewSerializer().DeserializeObject(text); }
        public static string Serialize(object value) { return NewSerializer().Serialize(value); }

        public static long IdValue(ElementId id)
        {
            return id == null ? -1 : id.Value;
        }

        private static string CategoryName(Element e)
        {
            try { return e.Category == null ? null : e.Category.Name; } catch (Exception) { return null; }
        }

        private static string ElementName(Element e)
        {
            try { return e.Name; } catch (Exception) { return null; }
        }

        /// <summary>
        /// 把任意物件轉成 JavaScriptSerializer 能吐出合法 JSON 的結構。
        /// ElementId→整數、XYZ→[x,y,z]（英尺原值）、Element→{id,name,category}、IEnumerable→陣列、其餘 ToString。
        /// </summary>
        public static object Normalize(object v) { return Normalize(v, 0); }

        private static object Normalize(object v, int depth)
        {
            if (v == null) return null;
            if (depth > 12) return v.ToString();
            if (v is string || v is bool) return v;
            if (v is char) return v.ToString();
            if (v is int || v is long || v is short || v is byte || v is sbyte || v is uint || v is ushort || v is ulong)
                return Convert.ToInt64(v, CultureInfo.InvariantCulture);
            if (v is double || v is float || v is decimal)
            {
                double d = Convert.ToDouble(v, CultureInfo.InvariantCulture);
                if (double.IsNaN(d) || double.IsInfinity(d)) return null;
                return d;
            }
            if (v is Enum) return v.ToString();
            if (v is Guid) return v.ToString();
            if (v is DateTime) return ((DateTime)v).ToString("yyyy-MM-dd HH:mm:ss");
            if (v is ElementId) return ((ElementId)v).Value;
            if (v is XYZ)
            {
                XYZ p = (XYZ)v;
                return new List<object> { Normalize(p.X, depth + 1), Normalize(p.Y, depth + 1), Normalize(p.Z, depth + 1) };
            }
            if (v is UV)
            {
                UV p = (UV)v;
                return new List<object> { Normalize(p.U, depth + 1), Normalize(p.V, depth + 1) };
            }
            if (v is BoundingBoxXYZ)
            {
                BoundingBoxXYZ b = (BoundingBoxXYZ)v;
                Dictionary<string, object> bd = new Dictionary<string, object>();
                bd["min"] = Normalize(b.Min, depth + 1);
                bd["max"] = Normalize(b.Max, depth + 1);
                return bd;
            }
            if (v is Element)
            {
                Element e = (Element)v;
                Dictionary<string, object> ed = new Dictionary<string, object>();
                ed["id"] = e.Id.Value;
                ed["name"] = ElementName(e);
                ed["category"] = CategoryName(e);
                return ed;
            }
            IDictionary dict = v as IDictionary;
            if (dict != null)
            {
                Dictionary<string, object> r = new Dictionary<string, object>();
                foreach (DictionaryEntry de in dict) r[Convert.ToString(de.Key, CultureInfo.InvariantCulture)] = Normalize(de.Value, depth + 1);
                return r;
            }
            IEnumerable en = v as IEnumerable;
            if (en != null)
            {
                List<object> list = new List<object>();
                foreach (object x in en)
                {
                    list.Add(Normalize(x, depth + 1));
                    if (list.Count >= 20000) break; // 防止一次吐出整個模型
                }
                return list;
            }
            return v.ToString();
        }

        public static string Ok(object id, object result)
        {
            Dictionary<string, object> resp = new Dictionary<string, object>();
            resp["id"] = id;
            resp["ok"] = true;
            resp["result"] = Normalize(result);
            return Serialize(resp);
        }

        public static string Err(object id, string message, string errorClass, IList<string> backtrace)
        {
            Dictionary<string, object> err = new Dictionary<string, object>();
            err["message"] = message;
            err["class"] = errorClass;
            if (backtrace != null && backtrace.Count > 0) err["backtrace"] = new List<string>(backtrace);
            Dictionary<string, object> resp = new Dictionary<string, object>();
            resp["id"] = id;
            resp["ok"] = false;
            resp["error"] = err;
            return Serialize(resp);
        }

        public static string FromException(object id, Exception ex)
        {
            BridgeError be = ex as BridgeError;
            if (be != null) return Err(id, be.Message, be.ErrorClass, null);
            List<string> trace = new List<string>();
            if (ex.StackTrace != null)
            {
                foreach (string line in ex.StackTrace.Split('\n'))
                {
                    string t = line.Trim();
                    if (t.Length > 0) trace.Add(t);
                    if (trace.Count >= 8) break;
                }
            }
            BridgeLog.Write("handler exception: " + ex);
            return Err(id, ex.Message, ex.GetType().Name, trace);
        }
    }
}
