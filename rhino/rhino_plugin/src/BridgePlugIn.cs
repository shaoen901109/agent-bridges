// Rhino Agent Bridge — Rhino 端外掛（.rhp）
//
// 設計重點（為什麼是「薄 C# 外殼 + IronPython 指令」）：
//   * C# 負責「一定要穩」的部分：Rhino 啟動時自動載入（AtStartup）、背景執行緒 TCP server、
//     把每一行 JSON 請求派到 Rhino UI 執行緒、逾時回錯誤、Start/Stop/Status 指令。
//   * 所有建模 / Grasshopper 指令用 IronPython 2.7 寫（rhino_plugin/py/*.py），
//     由 Rhino.Runtime.PythonScript 在 UI 執行緒執行：寫起來短、可用 rhinoscriptsyntax，
//     且 rhino_eval_python 與內建指令共用同一個 Python 環境。
//   * 刻意只用 C# 5 語法，讓 .NET Framework 內建的 csc.exe 也能編譯（install.ps1 優先用
//     Roslyn csc，找不到就退回 Framework csc）。
//
// 協定：換行分隔 JSON，每行一個請求 / 回應；本檔只做「行」的搬運，JSON 由 Python 解析。

using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Rhino;
using Rhino.Commands;
using Rhino.PlugIns;

[assembly: System.Runtime.InteropServices.Guid("4bc68b53-2390-428e-a045-42bd3604fdab")]
[assembly: PlugInDescription(DescriptionType.Organization, "Rhino Agent Bridge")]
[assembly: PlugInDescription(DescriptionType.Country, "Taiwan")]
[assembly: PlugInDescription(DescriptionType.Email, "none@example.invalid")]
[assembly: PlugInDescription(DescriptionType.Phone, "0")]
[assembly: PlugInDescription(DescriptionType.Fax, "0")]
[assembly: PlugInDescription(DescriptionType.Address, "local")]
[assembly: PlugInDescription(DescriptionType.UpdateUrl, "")]
[assembly: PlugInDescription(DescriptionType.WebSite, "")]
[assembly: AssemblyTitle("Rhino Agent Bridge")]
[assembly: AssemblyDescription("讓本機 AI agent 透過 127.0.0.1 操控 Rhino 與 Grasshopper")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace RhinoAgentBridge
{
    public class BridgePlugIn : PlugIn
    {
        public const int DefaultPort = 9878;
        public static BridgePlugIn Instance { get; private set; }
        public BridgeServer Server { get; private set; }

        public BridgePlugIn()
        {
            Instance = this;
        }

        public override PlugInLoadTime LoadTime
        {
            get { return PlugInLoadTime.AtStartup; }
        }

        /// <summary>目前設定的連接埠：環境變數 RHINO_BRIDGE_PORT 優先，其次外掛設定，最後預設值。</summary>
        public int ConfiguredPort
        {
            get
            {
                string env = Environment.GetEnvironmentVariable("RHINO_BRIDGE_PORT");
                int p;
                if (!string.IsNullOrEmpty(env) && int.TryParse(env, out p) && p >= 1024 && p <= 65535) return p;
                return Settings.GetInteger("Port", DefaultPort);
            }
            set { Settings.SetInteger("Port", value); }
        }

        public bool AutoStart
        {
            get { return Settings.GetBool("AutoStart", true); }
            set { Settings.SetBool("AutoStart", value); }
        }

        public string PyDir
        {
            get
            {
                string here = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                return Path.Combine(here, "py");
            }
        }

        protected override LoadReturnCode OnLoad(ref string errorMessage)
        {
            Server = new BridgeServer();
            // 等 Rhino 第一次進入 Idle 才開伺服器：此時 UI 執行緒與主視窗都已就緒。
            RhinoApp.Idle += OnFirstIdle;
            return LoadReturnCode.Success;
        }

        private void OnFirstIdle(object sender, EventArgs e)
        {
            RhinoApp.Idle -= OnFirstIdle;
            if (!AutoStart) return;
            StartServer(true);
        }

        /// <summary>啟動伺服器；失敗時把原因印在 Rhino 指令列，不丟例外。</summary>
        public bool StartServer(bool quiet)
        {
            try
            {
                if (Server.IsRunning)
                {
                    if (!quiet) RhinoApp.WriteLine("[Agent Bridge] 伺服器已在 127.0.0.1:{0} 執行中。", Server.Port);
                    return true;
                }
                Server.Start(ConfiguredPort);
                RhinoApp.WriteLine("[Agent Bridge] 伺服器已啟動：127.0.0.1:{0}（僅接受本機連線）", Server.Port);
                return true;
            }
            catch (Exception ex)
            {
                RhinoApp.WriteLine("[Agent Bridge] 無法啟動伺服器：{0}", ex.Message);
                BridgeLog.Write("start failed: " + ex);
                return false;
            }
        }

        public void StopServer()
        {
            if (Server != null && Server.IsRunning)
            {
                Server.Stop();
                RhinoApp.WriteLine("[Agent Bridge] 伺服器已停止。");
            }
            else
            {
                RhinoApp.WriteLine("[Agent Bridge] 伺服器本來就沒有在執行。");
            }
        }

        public string StatusText()
        {
            if (Server != null && Server.IsRunning)
                return string.Format("執行中 127.0.0.1:{0}，已處理 {1} 個請求，目前 {2} 條連線。",
                    Server.Port, Server.RequestCount, Server.ClientCount);
            return string.Format("未啟動（設定連接埠 {0}，Rhino 啟動時自動啟動：{1}）", ConfiguredPort, AutoStart ? "是" : "否");
        }
    }

    // ---------------------------------------------------------------------
    // 指令：RhinoAgentBridge（選項式）＋ 三個一鍵別名
    // ---------------------------------------------------------------------

    public class RhinoAgentBridgeCommand : Command
    {
        public override string EnglishName { get { return "RhinoAgentBridge"; } }

        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            BridgePlugIn plugin = BridgePlugIn.Instance;
            while (true)
            {
                var go = new Rhino.Input.Custom.GetOption();
                go.SetCommandPrompt("Agent Bridge：" + plugin.StatusText());
                int optStart = go.AddOption("Start");
                int optStop = go.AddOption("Stop");
                int optStatus = go.AddOption("Status");
                int optReload = go.AddOption("ReloadScripts");
                var autoToggle = new Rhino.Input.Custom.OptionToggle(plugin.AutoStart, "Off", "On");
                int optAuto = go.AddOptionToggle("AutoStart", ref autoToggle);
                int optPort = go.AddOption("Port");
                go.AcceptNothing(true);
                var res = go.Get();
                if (res != Rhino.Input.GetResult.Option) break;
                int idx = go.Option().Index;
                if (idx == optStart) plugin.StartServer(false);
                else if (idx == optStop) plugin.StopServer();
                else if (idx == optStatus) RhinoApp.WriteLine("[Agent Bridge] " + plugin.StatusText());
                else if (idx == optReload) { PythonHost.Reset(); RhinoApp.WriteLine("[Agent Bridge] Python 指令腳本將於下次請求重新載入。"); }
                else if (idx == optAuto) plugin.AutoStart = autoToggle.CurrentValue;
                else if (idx == optPort)
                {
                    int port = plugin.ConfiguredPort;
                    if (Rhino.Input.RhinoGet.GetInteger("連接埠（1024-65535，重新啟動伺服器後生效）", false, ref port, 1024, 65535)
                        == Result.Success)
                    {
                        plugin.ConfiguredPort = port;
                        RhinoApp.WriteLine("[Agent Bridge] 連接埠已設為 {0}。", port);
                    }
                }
            }
            return Result.Success;
        }
    }

    public class RhinoBridgeStartCommand : Command
    {
        public override string EnglishName { get { return "RhinoBridgeStart"; } }
        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            return BridgePlugIn.Instance.StartServer(false) ? Result.Success : Result.Failure;
        }
    }

    public class RhinoBridgeStopCommand : Command
    {
        public override string EnglishName { get { return "RhinoBridgeStop"; } }
        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            BridgePlugIn.Instance.StopServer();
            return Result.Success;
        }
    }

    public class RhinoBridgeStatusCommand : Command
    {
        public override string EnglishName { get { return "RhinoBridgeStatus"; } }
        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            RhinoApp.WriteLine("[Agent Bridge] " + BridgePlugIn.Instance.StatusText());
            return Result.Success;
        }
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
                string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RhinoAgentBridge");
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
    // Python 主機：在 UI 執行緒用同一個 PythonScript 實例處理所有請求
    // ---------------------------------------------------------------------
    internal static class PythonHost
    {
        private static Rhino.Runtime.PythonScript script;
        private static bool busy;

        public static void Reset()
        {
            script = null;
        }

        private static void EnsureInit()
        {
            if (script != null) return;
            // 確保內建的 IronPython 外掛已載入
            try { PlugIn.LoadPlugIn(new Guid("814d908a-e25c-493d-97e9-ee3861957f49")); } catch (Exception) { }
            Rhino.Runtime.PythonScript ps = Rhino.Runtime.PythonScript.Create();
            if (ps == null) throw new InvalidOperationException("無法建立 Rhino 的 IronPython 執行環境（PythonScript.Create 回傳 null）。");
            string pyDir = BridgePlugIn.Instance.PyDir;
            string boot = "import sys\n" +
                          "_d = r'" + pyDir.Replace("'", "") + "'\n" +
                          "if _d not in sys.path: sys.path.insert(0, _d)\n" +
                          "import rhino_bridge_main\n";
            ps.ExecuteScript(boot);
            script = ps;
        }

        // 這些指令只讀狀態或用來「救」卡在指令提示的 Rhino，允許在另一個指令執行期間重入
        // （Rhino 在等指令列輸入時仍會處理訊息，所以重入請求真的會被執行到）。
        private static readonly string[] ReentrantOk = { "ping", "get_info", "cancel_command", "get_status" };

        public static bool IsReentrantOk(string method)
        {
            return method != null && Array.IndexOf(ReentrantOk, method) >= 0;
        }

        /// <summary>必須在 UI 執行緒呼叫。回傳一行 JSON 回應；回傳 null 代表忙碌中且不允許重入。</summary>
        public static string Handle(string line, string statusJson, string method)
        {
            bool outer = !busy;
            if (!outer && !IsReentrantOk(method))
                return null; // 呼叫端會轉成「橋接器忙碌（重入）」錯誤
            busy = true;
            try
            {
                EnsureInit();
                script.SetVariable("__rb_line", line);
                script.SetVariable("__rb_status", statusJson);
                script.ExecuteScript("__rb_resp = rhino_bridge_main.handle_line(__rb_line, __rb_status)");
                object resp = script.GetVariable("__rb_resp");
                return resp as string;
            }
            finally
            {
                if (outer) busy = false;
            }
        }
    }

    // ---------------------------------------------------------------------
    // TCP server（背景執行緒）
    // ---------------------------------------------------------------------
    public class BridgeServer
    {
        private const int MaxRequestBytes = 16 * 1024 * 1024;
        private const int MaxClients = 8;
        private const double DefaultTimeoutSeconds = 120.0;

        private TcpListener listener;
        private Thread acceptThread;
        private volatile bool running;
        private readonly List<TcpClient> clients = new List<TcpClient>();
        private long requestCount;
        private DateTime startedAt;

        public int Port { get; private set; }
        public bool IsRunning { get { return running; } }
        public long RequestCount { get { return Interlocked.Read(ref requestCount); } }
        public int ClientCount { get { lock (clients) { return clients.Count; } } }

        public void Start(int port)
        {
            if (running) throw new InvalidOperationException("伺服器已在執行中。");
            if (port < 1024 || port > 65535) throw new ArgumentException("連接埠必須介於 1024 與 65535 之間。");
            TcpListener l = new TcpListener(IPAddress.Loopback, port); // 只綁 127.0.0.1
            try
            {
                l.ExclusiveAddressUse = true;
                l.Start();
            }
            catch (SocketException ex)
            {
                throw new InvalidOperationException(
                    "連接埠 " + port + " 無法使用（" + ex.SocketErrorCode + "）。可能被其他程式佔用，" +
                    "請用 `netstat -ano | findstr :" + port + "` 查詢，或以 RhinoAgentBridge → Port 改連接埠。");
            }
            listener = l;
            Port = port;
            running = true;
            startedAt = DateTime.Now;
            Interlocked.Exchange(ref requestCount, 0);
            acceptThread = new Thread(AcceptLoop);
            acceptThread.IsBackground = true;
            acceptThread.Name = "RhinoAgentBridge-accept";
            acceptThread.Start();
            BridgeLog.Write("server started on 127.0.0.1:" + port);
        }

        public void Stop()
        {
            running = false;
            try { if (listener != null) listener.Stop(); } catch (Exception) { }
            listener = null;
            lock (clients)
            {
                foreach (TcpClient c in clients)
                {
                    try { c.Close(); } catch (Exception) { }
                }
                clients.Clear();
            }
            BridgeLog.Write("server stopped");
        }

        private void AcceptLoop()
        {
            while (running)
            {
                TcpClient client;
                try { client = listener.AcceptTcpClient(); }
                catch (Exception) { break; }

                bool tooMany;
                lock (clients) { tooMany = clients.Count >= MaxClients; if (!tooMany) clients.Add(client); }
                if (tooMany)
                {
                    try { client.Close(); } catch (Exception) { }
                    continue;
                }
                Thread t = new Thread(delegate () { ClientLoop(client); });
                t.IsBackground = true;
                t.Name = "RhinoAgentBridge-client";
                t.Start();
            }
        }

        private void ClientLoop(TcpClient client)
        {
            try
            {
                client.NoDelay = true;
                NetworkStream stream = client.GetStream();
                byte[] chunk = new byte[65536];
                MemoryStream pending = new MemoryStream();
                while (running)
                {
                    int n = stream.Read(chunk, 0, chunk.Length);
                    if (n <= 0) break;
                    pending.Write(chunk, 0, n);
                    if (pending.Length > MaxRequestBytes)
                    {
                        WriteLine(stream, ErrorJson(null, "請求超過 16MB 上限。", "ProtocolError"));
                        break;
                    }
                    // 從緩衝取出所有完整的行
                    byte[] all = pending.ToArray();
                    int start = 0;
                    for (int i = 0; i < all.Length; i++)
                    {
                        if (all[i] != (byte)'\n') continue;
                        string line = Encoding.UTF8.GetString(all, start, i - start).Trim();
                        start = i + 1;
                        if (line.Length == 0) continue;
                        string resp = Dispatch(line);
                        WriteLine(stream, resp);
                    }
                    pending = new MemoryStream();
                    if (start < all.Length) pending.Write(all, start, all.Length - start);
                }
            }
            catch (Exception ex)
            {
                BridgeLog.Write("client loop ended: " + ex.Message);
            }
            finally
            {
                lock (clients) { clients.Remove(client); }
                try { client.Close(); } catch (Exception) { }
            }
        }

        private static void WriteLine(NetworkStream stream, string json)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(json + "\n");
            stream.Write(bytes, 0, bytes.Length);
            stream.Flush();
        }

        private class Pending
        {
            public string Response;
            public bool Started;
            public bool Abandoned;
            public readonly ManualResetEvent Done = new ManualResetEvent(false);
        }

        private string Dispatch(string line)
        {
            Interlocked.Increment(ref requestCount);

            // 只為了拿 id / timeout，真正的解析在 Python
            object id = null;
            string method = null;
            double timeout = DefaultTimeoutSeconds;
            try
            {
                var ser = new JavaScriptSerializer();
                ser.MaxJsonLength = int.MaxValue;
                Dictionary<string, object> req = ser.DeserializeObject(line) as Dictionary<string, object>;
                if (req == null) return ErrorJson(null, "請求必須是 JSON 物件。", "ProtocolError");
                if (req.ContainsKey("id")) id = req["id"];
                if (req.ContainsKey("method")) method = req["method"] as string;
                if (req.ContainsKey("timeout"))
                {
                    double t;
                    if (double.TryParse(Convert.ToString(req["timeout"], System.Globalization.CultureInfo.InvariantCulture),
                        System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out t) && t > 0)
                        timeout = Math.Max(1.0, t - 1.0); // 比 client 的逾時早一點，讓 client 收得到伺服器端的錯誤
                }
            }
            catch (Exception ex)
            {
                return ErrorJson(null, "請求不是合法 JSON：" + ex.Message, "ProtocolError");
            }

            string status = StatusJson();
            Pending p = new Pending();
            try
            {
                RhinoApp.InvokeOnUiThread(new Action(delegate ()
                {
                    lock (p)
                    {
                        if (p.Abandoned) return;
                        p.Started = true;
                    }
                    try
                    {
                        p.Response = PythonHost.Handle(line, status, method);
                        if (p.Response == null)
                            p.Response = ErrorJson(id, "橋接器正忙於處理另一個指令（Rhino 可能停在指令提示等輸入）。可先用 cancel_command 解除，或稍後重試。", "BusyError");
                    }
                    catch (Exception ex)
                    {
                        BridgeLog.Write("handler exception: " + ex);
                        p.Response = ErrorJson(id, "橋接器內部錯誤：" + ex.Message, ex.GetType().Name);
                    }
                    finally
                    {
                        p.Done.Set();
                    }
                }));
            }
            catch (Exception ex)
            {
                return ErrorJson(id, "無法派送到 Rhino UI 執行緒：" + ex.Message, ex.GetType().Name);
            }

            if (!p.Done.WaitOne((int)(timeout * 1000)))
            {
                bool started;
                lock (p) { started = p.Started; if (!started) p.Abandoned = true; }
                return ErrorJson(id,
                    started
                        ? "指令在 Rhino 內執行超過 " + timeout.ToString("0.#") + " 秒仍未完成（可能仍在背景跑，或 Rhino 卡在對話框）。"
                        : "Rhino UI 執行緒在 " + timeout.ToString("0.#") + " 秒內沒有空檔處理這個指令（Rhino 可能正忙或停在對話框）；指令已取消。",
                    "TimeoutError");
            }
            return p.Response;
        }

        private string StatusJson()
        {
            return "{\"running\":true,\"host\":\"127.0.0.1\",\"port\":" + Port +
                   ",\"clients\":" + ClientCount +
                   ",\"requests\":" + RequestCount +
                   ",\"started_at\":\"" + startedAt.ToString("yyyy-MM-dd HH:mm:ss") + "\"}";
        }

        internal static string ErrorJson(object id, string message, string errorClass)
        {
            StringBuilder sb = new StringBuilder();
            sb.Append("{\"id\":");
            if (id == null) sb.Append("null");
            else if (id is string) sb.Append("\"").Append(Escape((string)id)).Append("\"");
            else sb.Append(Convert.ToString(id, System.Globalization.CultureInfo.InvariantCulture));
            sb.Append(",\"ok\":false,\"error\":{\"message\":\"").Append(Escape(message));
            sb.Append("\",\"class\":\"").Append(Escape(errorClass)).Append("\"}}");
            return sb.ToString();
        }

        private static string Escape(string s)
        {
            StringBuilder sb = new StringBuilder();
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20 || c > 0x7e) sb.AppendFormat("\\u{0:x4}", (int)c);
                        else sb.Append(c);
                        break;
                }
            }
            return sb.ToString();
        }
    }
}
