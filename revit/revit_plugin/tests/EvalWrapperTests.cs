// EvalWrapper 的獨立測試程式（不需要 Revit）
//
// 做法：在本檔宣告「假的」Autodesk.Revit.DB / Autodesk.Revit.UI 型別，讓包裝後的程式碼真的能編譯，
// 再用和 Eval.cs 相同的 CodeDom 流程驗證：片段包裝、using 搬運、沒 return 補 return null、
// 編譯錯誤的行號換算（片段行號 = 產生碼行號 - 3）。
// 由 build.ps1 -RunTests 編譯並執行；結束碼 0 = 全部通過。
// 只用 C# 5 語法。

using System;
using System.CodeDom.Compiler;
using System.Collections.Generic;
using System.Reflection;
using Microsoft.CSharp;
using RevitAgentBridge;

namespace Autodesk.Revit.DB { public class Document { public string Title = "測試文件"; } }
namespace Autodesk.Revit.UI
{
    public class UIDocument { }
    public class UIApplication { }
}

public static class EvalWrapperTests
{
    private static int failures;
    private static int passed;

    private static void Check(bool cond, string name, string detail)
    {
        if (cond) { passed++; Console.WriteLine("  通過  " + name); }
        else { failures++; Console.WriteLine("  失敗  " + name + (detail == null ? "" : "  -> " + detail)); }
    }

    private static CompilerResults Compile(WrappedSnippet w)
    {
        CompilerParameters cp = new CompilerParameters();
        cp.GenerateInMemory = true;
        cp.TreatWarningsAsErrors = false;
        cp.CompilerOptions = "/nowarn:0162,0168,0219";
        cp.ReferencedAssemblies.Add("System.dll");
        cp.ReferencedAssemblies.Add("System.Core.dll");
        cp.ReferencedAssemblies.Add(Assembly.GetExecutingAssembly().Location); // 假的 Revit 型別在這裡
        using (CSharpCodeProvider provider = new CSharpCodeProvider())
        {
            return provider.CompileAssemblyFromSource(cp, w.Source);
        }
    }

    private static object RunSnippet(CompilerResults r, List<object> printed)
    {
        Type t = r.CompiledAssembly.GetType(EvalWrapper.Namespace + "." + EvalWrapper.ClassName);
        MethodInfo m = t.GetMethod(EvalWrapper.MethodName);
        Action<object> print = delegate (object o) { printed.Add(o); };
        return m.Invoke(null, new object[] { new Autodesk.Revit.UI.UIApplication(), new Autodesk.Revit.UI.UIDocument(),
            new Autodesk.Revit.DB.Document(), print });
    }

    private static string FirstErrorText(CompilerResults r, int lineCount)
    {
        foreach (CompilerError e in r.Errors)
            if (!e.IsWarning) return EvalWrapper.FormatError(e, lineCount);
        return null;
    }

    private static int FirstErrorMappedLine(CompilerResults r, int lineCount)
    {
        foreach (CompilerError e in r.Errors)
            if (!e.IsWarning) return EvalWrapper.MapLine(e.Line, lineCount);
        return -1;
    }

    public static int Main()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.WriteLine("EvalWrapper 測試");

        // 1. 包裝：片段第 1 行必須落在產生碼第 4 行
        WrappedSnippet w1 = EvalWrapper.Wrap("int a = 1;\nreturn a;");
        string[] gen = w1.Source.Split('\n');
        Check(gen[3] == "int a = 1;", "片段第 1 行位於產生碼第 4 行", "實際第 4 行：" + gen[3]);
        Check(gen[2].Contains("public static object Run(UIApplication uiapp, UIDocument uidoc, Document doc, Action<object> print)"),
            "第 3 行是 Run 簽章", gen[2]);
        Check(w1.SnippetLineCount == 2, "SnippetLineCount = 2", w1.SnippetLineCount.ToString());

        // 2. 一般片段編譯 + 執行 + print
        WrappedSnippet w2 = EvalWrapper.Wrap("print(\"你好\");\nprint(doc.Title);\nreturn 40 + 2;");
        CompilerResults r2 = Compile(w2);
        Check(!r2.Errors.HasErrors, "有 return 的片段可編譯", FirstErrorText(r2, w2.SnippetLineCount));
        if (!r2.Errors.HasErrors)
        {
            List<object> printed = new List<object>();
            object v = RunSnippet(r2, printed);
            Check(v is int && (int)v == 42, "回傳值 42", Convert.ToString(v));
            Check(printed.Count == 2 && (string)printed[0] == "你好" && (string)printed[1] == "測試文件", "print 兩筆輸出", printed.Count.ToString());
        }

        // 3. 沒 return → 補 return null
        WrappedSnippet w3 = EvalWrapper.Wrap("int a = 1;\nprint(a);");
        CompilerResults r3 = Compile(w3);
        Check(!r3.Errors.HasErrors, "沒有 return 的片段可編譯（自動補 return null）", FirstErrorText(r3, w3.SnippetLineCount));
        if (!r3.Errors.HasErrors)
        {
            object v = RunSnippet(r3, new List<object>());
            Check(v == null, "沒有 return 時回傳 null", Convert.ToString(v));
        }

        // 4. 預設 using：System.Linq 可用（不用自己寫 using）
        WrappedSnippet w4 = EvalWrapper.Wrap("var xs = new List<int> { 3, 1, 2 };\nreturn xs.OrderBy(x => x).First();");
        CompilerResults r4 = Compile(w4);
        Check(!r4.Errors.HasErrors, "預設 using 含 Generic 與 Linq", FirstErrorText(r4, w4.SnippetLineCount));
        if (!r4.Errors.HasErrors) Check((int)RunSnippet(r4, new List<object>()) == 1, "Linq 結果正確", null);

        // 5. 片段自帶 using 會被搬到第 1 行，且行號不位移
        WrappedSnippet w5 = EvalWrapper.Wrap("using System.IO;\nusing System.Text; // 註解\nstring p = Path.Combine(\"a\", \"b\");\nint bad = ;\nreturn p;");
        CompilerResults r5 = Compile(w5);
        Check(r5.Errors.HasErrors, "有語法錯誤的片段編譯失敗", null);
        Check(FirstErrorMappedLine(r5, w5.SnippetLineCount) == 4, "自帶 using 時錯誤行號仍是片段第 4 行",
            "換算後行號 = " + FirstErrorMappedLine(r5, w5.SnippetLineCount));
        WrappedSnippet w5b = EvalWrapper.Wrap("using System.IO;\nreturn Path.Combine(\"a\", \"b\");");
        CompilerResults r5b = Compile(w5b);
        Check(!r5b.Errors.HasErrors, "自帶 using System.IO 可編譯", FirstErrorText(r5b, w5b.SnippetLineCount));
        if (!r5b.Errors.HasErrors) Check(((string)RunSnippet(r5b, new List<object>())).Length == 3, "Path.Combine 可用", null);

        // 6. 編譯錯誤的行號換算（錯在第 3 行）
        WrappedSnippet w6 = EvalWrapper.Wrap("int a = 1;\nint b = 2;\nint c = a + undefinedName;\nreturn c;");
        CompilerResults r6 = Compile(w6);
        Check(r6.Errors.HasErrors, "未定義名稱 → 編譯失敗", null);
        Check(FirstErrorMappedLine(r6, w6.SnippetLineCount) == 3, "錯誤行號換算為片段第 3 行", "換算後 = " + FirstErrorMappedLine(r6, w6.SnippetLineCount));
        string text6 = FirstErrorText(r6, w6.SnippetLineCount);
        Check(text6 != null && text6.Contains("第 3 行") && text6.Contains("CS0103"), "錯誤訊息含「第 3 行」與 CS0103", text6);

        // 7. CRLF 換行不影響行號
        WrappedSnippet w7 = EvalWrapper.Wrap("int a = 1;\r\nint b = ;\r\nreturn a;");
        CompilerResults r7 = Compile(w7);
        Check(FirstErrorMappedLine(r7, w7.SnippetLineCount) == 2, "CRLF 片段的錯誤行號 = 2", "換算後 = " + FirstErrorMappedLine(r7, w7.SnippetLineCount));

        // 8. 括號不成對 → 錯誤落在片段結尾後，給出提示
        WrappedSnippet w8 = EvalWrapper.Wrap("if (true) {\nint a = 1;");
        CompilerResults r8 = Compile(w8);
        Check(r8.Errors.HasErrors, "大括號沒關 → 編譯失敗", null);
        string text8 = FirstErrorText(r8, w8.SnippetLineCount);
        Check(text8 != null && (text8.Contains("片段結尾") || text8.Contains("第 ")), "大括號沒關有可讀的位置說明", text8);

        // 9. MapLine 邊界
        Check(EvalWrapper.MapLine(3, 5) == 0, "MapLine：包裝行回傳 0", null);
        Check(EvalWrapper.MapLine(4, 5) == 1, "MapLine：產生碼第 4 行 = 片段第 1 行", null);
        Check(EvalWrapper.MapLine(9, 5) == 6, "MapLine：超過片段回傳行數 + 1", null);

        Console.WriteLine();
        Console.WriteLine("通過 " + passed + " 項，失敗 " + failures + " 項。");
        return failures == 0 ? 0 : 1;
    }
}
