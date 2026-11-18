// Revit Agent Bridge — Revit 端外掛（IExternalApplication，RevitAgentBridge.dll）
//
// 設計重點：
//   * OnStartup：建立 ExternalEvent、訂閱 DialogBoxShowing（只記錄，不自動按掉）、建立 Ribbon「Agent Bridge」分頁、
//     自動啟動 TCP 伺服器（環境變數 REVIT_BRIDGE_AUTOSTART=0 可關閉自動啟動）。
//   * 所有指令邏輯都在 C#（Handlers.cs）；任意程式碼用 eval_csharp 即時編譯執行（Eval.cs）。
//   * 只用 C# 5 語法，讓 .NET Framework 內建的 csc.exe 也能編譯。

using System;
using System.Reflection;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Events;

[assembly: AssemblyTitle("Revit Agent Bridge")]
[assembly: AssemblyDescription("讓本機 AI agent 透過 127.0.0.1 操控 Revit")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace RevitAgentBridge
{
    public class App : IExternalApplication
    {
        public const int DefaultPort = 9879;
        public const string TabName = "Agent Bridge";

        public static BridgeServer Server = new BridgeServer();

        /// <summary>連接埠：環境變數 REVIT_BRIDGE_PORT 優先，否則預設 9879。</summary>
        public static int ConfiguredPort
        {
            get
            {
                string env = Environment.GetEnvironmentVariable("REVIT_BRIDGE_PORT");
                int p;
                if (!string.IsNullOrEmpty(env) && int.TryParse(env, out p) && p >= 1024 && p <= 65535) return p;
                return DefaultPort;
            }
        }

        public static bool AutoStart
        {
            get
            {
                string env = Environment.GetEnvironmentVariable("REVIT_BRIDGE_AUTOSTART");
                return !(env != null && env.Trim() == "0");
            }
        }

        public Result OnStartup(UIControlledApplication application)
        {
            BridgeLog.Write("OnStartup");
            try
            {
                StateCache.RevitVersion = application.ControlledApplication.VersionNumber;
                StateCache.RevitVersionName = application.ControlledApplication.VersionName;
                StateCache.RevitBuild = application.ControlledApplication.VersionBuild;
            }
            catch (Exception ex) { BridgeLog.Write("version read failed: " + ex.Message); }

            try
            {
                ApiBridge.Event = ExternalEvent.Create(new ApiHandler());
            }
            catch (Exception ex)
            {
                BridgeLog.Write("ExternalEvent.Create failed: " + ex);
            }

            try { application.DialogBoxShowing += OnDialogBoxShowing; }
            catch (Exception ex) { BridgeLog.Write("DialogBoxShowing subscribe failed: " + ex.Message); }

            try { CreateRibbon(application); }
            catch (Exception ex) { BridgeLog.Write("ribbon failed: " + ex); }

            if (AutoStart) StartServer();
            return Result.Succeeded;
        }

        public Result OnShutdown(UIControlledApplication application)
        {
            try { application.DialogBoxShowing -= OnDialogBoxShowing; } catch (Exception) { }
            try { if (Server.IsRunning) Server.Stop(); } catch (Exception) { }
            BridgeLog.Write("OnShutdown");
            return Result.Succeeded;
        }

        /// <summary>只記錄對話框（不設定 OverrideResult，不自動按掉）。</summary>
        private static void OnDialogBoxShowing(object sender, DialogBoxShowingEventArgs e)
        {
            try
            {
                TaskDialogShowingEventArgs td = e as TaskDialogShowingEventArgs;
                MessageBoxShowingEventArgs mb = e as MessageBoxShowingEventArgs;
                if (td != null) StateCache.RecordDialog("TaskDialog", td.DialogId, td.Message);
                else if (mb != null) StateCache.RecordDialog("MessageBox", "MessageBox", mb.Message);
                else StateCache.RecordDialog("Dialog", e.DialogId, "");
            }
            catch (Exception) { }
        }

        public static bool StartServer()
        {
            try
            {
                if (Server.IsRunning) return true;
                Server.Start(ConfiguredPort);
                return true;
            }
            catch (Exception ex)
            {
                BridgeLog.Write("start failed: " + ex);
                LastStartError = ex.Message;
                return false;
            }
        }

        public static string LastStartError = "";

        public static string StatusText()
        {
            if (Server.IsRunning)
                return string.Format("執行中 127.0.0.1:{0}，已處理 {1} 個請求，目前 {2} 條連線。",
                    Server.Port, Server.RequestCount, Server.ClientCount);
            string s = string.Format("未啟動（設定連接埠 {0}；啟動 Revit 時自動啟動：{1}）", ConfiguredPort, AutoStart ? "是" : "否");
            if (!string.IsNullOrEmpty(LastStartError)) s += "\n上次啟動失敗原因：" + LastStartError;
            return s;
        }

        private static void CreateRibbon(UIControlledApplication app)
        {
            try { app.CreateRibbonTab(TabName); } catch (Exception) { } // 分頁已存在就略過
            RibbonPanel panel = app.CreateRibbonPanel(TabName, "橋接器");
            string asm = Assembly.GetExecutingAssembly().Location;

            PushButtonData start = new PushButtonData("AgentBridgeStart", "啟動" + Environment.NewLine + "伺服器", asm, "RevitAgentBridge.StartCommand");
            start.ToolTip = "啟動 Agent Bridge 伺服器（127.0.0.1:" + ConfiguredPort + "，僅接受本機連線）";
            PushButtonData stop = new PushButtonData("AgentBridgeStop", "停止" + Environment.NewLine + "伺服器", asm, "RevitAgentBridge.StopCommand");
            stop.ToolTip = "停止 Agent Bridge 伺服器，agent 將無法操控 Revit";
            PushButtonData status = new PushButtonData("AgentBridgeStatus", "狀態", asm, "RevitAgentBridge.StatusCommand");
            status.ToolTip = "顯示伺服器狀態與最近一次對話框紀錄";

            panel.AddItem(start);
            panel.AddItem(stop);
            panel.AddItem(status);
        }
    }

    [Transaction(TransactionMode.Manual)]
    public class StartCommand : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            bool ok = App.StartServer();
            TaskDialog.Show("Agent Bridge", ok ? "伺服器已啟動。\n" + App.StatusText() : "無法啟動伺服器。\n" + App.StatusText());
            return ok ? Result.Succeeded : Result.Failed;
        }
    }

    [Transaction(TransactionMode.Manual)]
    public class StopCommand : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            if (App.Server.IsRunning)
            {
                App.Server.Stop();
                TaskDialog.Show("Agent Bridge", "伺服器已停止。");
            }
            else
            {
                TaskDialog.Show("Agent Bridge", "伺服器本來就沒有在執行。");
            }
            return Result.Succeeded;
        }
    }

    [Transaction(TransactionMode.Manual)]
    public class StatusCommand : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            string text = App.StatusText();
            string dialog = StateCache.DialogHint();
            if (dialog.Length > 0) text += "\n" + dialog;
            TaskDialog.Show("Agent Bridge", text + "\n記錄檔：%LOCALAPPDATA%\\RevitAgentBridge\\bridge.log");
            return Result.Succeeded;
        }
    }
}
