// Revit Agent Bridge — 交易包裝（Tx）與 eval_csharp（即時編譯 C# 片段）
// 只用 C# 5 語法。

using System;
using System.CodeDom.Compiler;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.ExceptionServices;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Microsoft.CSharp;

namespace RevitAgentBridge
{
    /// <summary>交易失敗處理：警告一律記下來並刪除（避免跳出會卡住 ExternalEvent 的警告對話框）；錯誤則回復交易。</summary>
    internal class WarningSwallower : IFailuresPreprocessor
    {
        public FailureProcessingResult PreprocessFailures(FailuresAccessor a)
        {
            bool hasError = false;
            foreach (FailureMessageAccessor m in a.GetFailureMessages())
            {
                string text = m.GetDescriptionText();
                if (m.GetSeverity() == FailureSeverity.Warning)
                {
                    if (Tx.Warnings.Count < 50) Tx.Warnings.Add(text);
                }
                else
                {
                    hasError = true;
                    if (Tx.Errors.Count < 50) Tx.Errors.Add(text);
                }
            }
            a.DeleteAllWarnings();
            return hasError ? FailureProcessingResult.ProceedWithRollBack : FailureProcessingResult.Continue;
        }
    }

    internal static class Tx
    {
        /// <summary>本次請求期間 Revit 回報的警告 / 錯誤（每個請求開始時由 Handlers.Invoke 清空）。</summary>
        public static readonly List<string> Warnings = new List<string>();
        public static readonly List<string> Errors = new List<string>();

        public static void Reset() { Warnings.Clear(); Errors.Clear(); }

        /// <summary>在一個 Transaction 內執行 body；例外或提交失敗都會 RollBack 並丟出錯誤。</summary>
        public static T Run<T>(Document doc, string name, Func<T> body)
        {
            using (Transaction t = new Transaction(doc, name))
            {
                t.Start();
                FailureHandlingOptions fho = t.GetFailureHandlingOptions();
                fho.SetFailuresPreprocessor(new WarningSwallower());
                fho.SetClearAfterRollback(true);
                t.SetFailureHandlingOptions(fho);

                T result = body(); // 例外 → using 結束時自動 RollBack
                TransactionStatus status = t.Commit();
                if (status != TransactionStatus.Committed)
                {
                    string detail = Errors.Count > 0 ? "：" + string.Join("；", Errors.ToArray()) : "";
                    throw new BridgeError("交易沒有提交（狀態 " + status + "），已復原" + detail + "。", "TransactionError");
                }
                return result;
            }
        }
    }

    internal static class Evaluator
    {
        private const int MaxPrintItems = 2000;

        private static readonly string[] ReferenceNames = new string[]
        {
            "System.dll", "System.Core.dll", "System.Xml.dll", "System.Xml.Linq.dll",
            "System.Data.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "Microsoft.CSharp.dll"
        };

        /// <summary>編譯並執行一段 C# 片段。回傳 {result, output, warnings...}。</summary>
        public static object Run(UIApplication uiapp, Dictionary<string, object> p)
        {
            string code = P.Str(p, "code");
            if (code == null) throw new BridgeError("必須提供參數 code（C# 程式片段）。");
            bool useTransaction = P.Bool(p, "transaction", true);

            WrappedSnippet wrapped = EvalWrapper.Wrap(code);
            Assembly asm = Compile(wrapped);

            Type type = asm.GetType(EvalWrapper.Namespace + "." + EvalWrapper.ClassName);
            MethodInfo method = type == null ? null : type.GetMethod(EvalWrapper.MethodName);
            if (method == null) throw new BridgeError("編譯後找不到進入點 " + EvalWrapper.ClassName + "." + EvalWrapper.MethodName + "。", "CompileError");

            UIDocument uidoc = uiapp.ActiveUIDocument;
            Document doc = uidoc == null ? null : uidoc.Document;
            List<object> output = new List<object>();
            Action<object> print = delegate (object o)
            {
                if (output.Count < MaxPrintItems) output.Add(Json.Normalize(o));
            };
            object[] args = new object[] { uiapp, uidoc, doc, print };

            Func<object> invoke = delegate ()
            {
                try { return method.Invoke(null, args); }
                catch (TargetInvocationException tie)
                {
                    if (tie.InnerException != null) ExceptionDispatchInfo.Capture(tie.InnerException).Throw();
                    throw;
                }
            };

            object value;
            bool inTransaction = useTransaction && doc != null;
            if (inTransaction)
            {
                try { value = Tx.Run<object>(doc, "Agent eval_csharp", invoke); }
                catch (Exception ex)
                {
                    if (ex.GetType().Name != "InvalidOperationException") throw;
                    throw new BridgeError(ex.Message +
                        "\n提示：本次已自動包在 Transaction 裡（transaction=true）。片段若自己 new Transaction、"
                        + "或呼叫 doc.Save／SaveAs／Regenerate 以外需要無交易狀態的 API，請改傳 transaction=false。",
                        "InvalidOperationException");
                }
            }
            else value = invoke();

            Dictionary<string, object> r = new Dictionary<string, object>();
            r["result"] = value;
            r["output"] = output;
            r["transaction"] = inTransaction;
            if (output.Count >= MaxPrintItems) r["output_truncated"] = true;
            return r;
        }

        // 載入的組件無法卸載：同一段程式碼重跑時重用，避免每次都多佔一份記憶體。
        private static readonly Dictionary<string, Assembly> CompileCache = new Dictionary<string, Assembly>();
        private const int MaxCacheEntries = 64;

        private static Assembly Compile(WrappedSnippet wrapped)
        {
            Assembly cached;
            if (CompileCache.TryGetValue(wrapped.Source, out cached)) return cached;
            Assembly asm = CompileUncached(wrapped);
            if (CompileCache.Count >= MaxCacheEntries) CompileCache.Clear();
            CompileCache[wrapped.Source] = asm;
            return asm;
        }

        private static Assembly CompileUncached(WrappedSnippet wrapped)
        {
            CompilerParameters cp = new CompilerParameters();
            cp.GenerateInMemory = true;
            cp.GenerateExecutable = false;
            cp.IncludeDebugInformation = false;
            cp.TreatWarningsAsErrors = false;
            cp.CompilerOptions = "/optimize+ /nowarn:0162,0168,0219,0618,0414,0649";
            cp.ReferencedAssemblies.Add(typeof(Document).Assembly.Location);   // RevitAPI.dll
            cp.ReferencedAssemblies.Add(typeof(UIApplication).Assembly.Location); // RevitAPIUI.dll
            foreach (string n in ReferenceNames) cp.ReferencedAssemblies.Add(n);

            CompilerResults results;
            try
            {
                using (CSharpCodeProvider provider = new CSharpCodeProvider())
                {
                    results = provider.CompileAssemblyFromSource(cp, wrapped.Source);
                }
            }
            finally
            {
                try { cp.TempFiles.Delete(); } catch (Exception) { }
            }

            if (results.Errors.HasErrors)
            {
                List<string> lines = new List<string>();
                foreach (CompilerError e in results.Errors)
                {
                    if (e.IsWarning) continue;
                    lines.Add(EvalWrapper.FormatError(e, wrapped.SnippetLineCount));
                    if (lines.Count >= 10) break;
                }
                throw new BridgeError(
                    "C# 編譯失敗（行號是你的片段內的行號；本橋接器的編譯器只支援 C# 5 語法，沒有 ?. / $\"\" / nameof / 運算式主體成員）：\n"
                    + string.Join("\n", lines.ToArray()), "CompileError");
            }
            return results.CompiledAssembly;
        }
    }
}
