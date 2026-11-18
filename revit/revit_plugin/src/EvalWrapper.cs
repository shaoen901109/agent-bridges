// Revit Agent Bridge — eval_csharp 的「片段包裝」邏輯（純 C#，刻意不依賴 Revit API）
//
// 把 agent 給的程式片段包進：
//   public static object Run(UIApplication uiapp, UIDocument uidoc, Document doc, Action<object> print)
// 包裝只佔固定的 3 行（見 LineOffset），所以編譯錯誤的行號 = 產生碼行號 - 3，就是 agent 片段裡的行號。
// 這支檔案獨立於 Revit，tests\EvalWrapperTests.cs 會單獨編譯它做驗證。
//
// 只用 C# 5 語法。

using System;
using System.CodeDom.Compiler;
using System.Collections.Generic;
using System.Text;
using System.Text.RegularExpressions;

namespace RevitAgentBridge
{
    public class WrappedSnippet
    {
        /// <summary>要交給編譯器的完整原始碼。</summary>
        public string Source;
        /// <summary>agent 片段的行數（含補上的空行）。</summary>
        public int SnippetLineCount;
    }

    public static class EvalWrapper
    {
        public const string Namespace = "AgentEval";
        public const string ClassName = "Snippet";
        public const string MethodName = "Run";

        /// <summary>包裝在片段前面佔的行數（第 1 行 using、第 2 行 namespace/class、第 3 行 Run 簽章）。</summary>
        public const int LineOffset = 3;

        // 預設 using（全部放在第 1 行）。刻意不放 System.IO，避免和 Autodesk.Revit.DB 內的名稱撞名；
        // 要用請在片段最上面自己寫 `using System.IO;`（會被自動搬到第 1 行）。
        private static readonly string[] DefaultUsings = new string[]
        {
            "System",
            "System.Collections.Generic",
            "System.Linq",
            "Autodesk.Revit.DB",
            "Autodesk.Revit.UI"
        };

        // 片段最前面獨立成行的 using（含別名），會被搬到第 1 行；原行改成空行以保留行號。
        private static readonly Regex UsingLine = new Regex(
            @"^\s*using\s+(?:[A-Za-z_]\w*\s*=\s*)?[A-Za-z_][\w\.]*\s*;\s*(?://.*)?$");

        public static WrappedSnippet Wrap(string snippet)
        {
            if (snippet == null) snippet = "";
            string normalized = snippet.Replace("\r\n", "\n").Replace("\r", "\n");
            string[] lines = normalized.Split('\n');
            List<string> hoisted = new List<string>();
            for (int i = 0; i < lines.Length; i++)
            {
                if (UsingLine.IsMatch(lines[i]))
                {
                    string u = lines[i].Trim();
                    int cmt = u.IndexOf("//", StringComparison.Ordinal);
                    if (cmt >= 0) u = u.Substring(0, cmt).Trim();
                    hoisted.Add(u);
                    lines[i] = "";
                }
            }

            StringBuilder sb = new StringBuilder();
            foreach (string u in DefaultUsings) sb.Append("using ").Append(u).Append("; ");
            foreach (string u in hoisted) sb.Append(u).Append(" ");
            sb.Append("\n");                                                           // 第 1 行
            sb.Append("namespace ").Append(Namespace).Append(" { public static class ").Append(ClassName).Append(" {\n"); // 第 2 行
            sb.Append("public static object ").Append(MethodName)
              .Append("(UIApplication uiapp, UIDocument uidoc, Document doc, Action<object> print) {\n");                  // 第 3 行
            sb.Append(string.Join("\n", lines));                                       // 從第 4 行開始
            sb.Append("\nreturn null; } } }\n");                                       // 片段沒 return 就補 return null

            WrappedSnippet w = new WrappedSnippet();
            w.Source = sb.ToString();
            w.SnippetLineCount = lines.Length;
            return w;
        }

        /// <summary>產生碼行號 → 片段行號；落在包裝本身（片段開頭前）回傳 0，落在片段結尾後回傳 snippetLineCount + 1。</summary>
        public static int MapLine(int generatedLine, int snippetLineCount)
        {
            int mapped = generatedLine - LineOffset;
            if (mapped < 1) return 0;
            if (mapped > snippetLineCount) return snippetLineCount + 1;
            return mapped;
        }

        /// <summary>把一個編譯錯誤轉成給 agent 看的一行文字（行號已換算成片段行號）。</summary>
        public static string FormatError(CompilerError e, int snippetLineCount)
        {
            int line = MapLine(e.Line, snippetLineCount);
            string where;
            if (e.Line <= 0) where = "（無行號）";
            else if (line == 0) where = "（包裝程式碼，通常是 using 或簽章問題）";
            else if (line > snippetLineCount) where = "（片段結尾，常見原因：括號 / 大括號沒有成對）";
            else where = "第 " + line + " 行第 " + e.Column + " 欄";
            return (e.IsWarning ? "警告 " : "錯誤 ") + e.ErrorNumber + " " + where + "：" + e.ErrorText;
        }
    }
}
