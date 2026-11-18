// Revit Agent Bridge — TCP server 與「派進 Revit API 執行緒」的機制
//
// 設計重點：
//   * 背景執行緒跑 TcpListener（只綁 127.0.0.1），每個連線一條讀取執行緒；協定是換行分隔 JSON。
//   * Revit API 只能在「API context」呼叫：請求先放進佇列（ConcurrentQueue），再 ExternalEvent.Raise()，
//     等 Revit 閒下來呼叫 ApiHandler.Execute 時，在主執行緒把佇列一次處理完。
//   * 逾時邏輯照 Rhino 版：Pending.Started / Abandoned——還沒開始執行的請求逾時就標成放棄，不會事後才跑。
//   * ping / get_status / list_commands 不需要 API context，直接在 socket 執行緒回答。
//   * Revit 停在模態對話框時 ExternalEvent 永遠不會被呼叫：逾時錯誤會提示「請使用者處理對話框」，不強制結束 Revit。
//   * 只用 C# 5 語法。

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Autodesk.Revit.UI;

namespace RevitAgentBridge
{
    internal class Pending
    {
        public object Id;
        public string Method;
        public Dictionary<string, object> Params;
        public string Response;
        public bool Started;
        public bool Abandoned;
        public readonly ManualResetEvent Done = new ManualResetEvent(false);
    }

    /// <summary>Revit 主執行緒端：把佇列裡的請求逐一執行。</summary>
    internal class ApiHandler : IExternalEventHandler
    {
        private bool busy;

        public void Execute(UIApplication app)
        {
            if (busy) return; // 防止 Revit 在處理中途（例如開檔）抽空訊息而重入
            busy = true;
            try
            {
                StateCache.Refresh(app);
                Pending p;
                while (ApiBridge.Queue.TryDequeue(out p))
                {
                    lock (p)
                    {
                        if (p.Abandoned) continue;
                        p.Started = true;
                    }
                    try
                    {
                        object result = Handlers.Invoke(app, p.Method, p.Params);
                        p.Response = Json.Ok(p.Id, result);
                    }
                    catch (Exception ex)
                    {
                        try { p.Response = Json.FromException(p.Id, ex); }
                        catch (Exception ex2) { p.Response = Json.Err(p.Id, "橋接器內部錯誤：" + ex2.Message, ex2.GetType().Name, null); }
                    }
                    finally
                    {
                        p.Done.Set();
                    }
                }
            }
            finally
            {
                busy = false;
            }
            // 處理期間若又有新請求進來而 Raise 被合併掉，補一次
            if (!ApiBridge.Queue.IsEmpty) ApiBridge.Raise();
        }

        public string GetName() { return "RevitAgentBridge"; }
    }

    internal static class ApiBridge
    {
        public static readonly ConcurrentQueue<Pending> Queue = new ConcurrentQueue<Pending>();
        public static ExternalEvent Event;

        public static void Raise()
        {
            try { if (Event != null) Event.Raise(); }
            catch (Exception ex) { BridgeLog.Write("raise failed: " + ex.Message); }
        }
    }

    /// <summary>不需要 API context 就能回答的狀態快取（對話框、最近一次的文件資訊）。</summary>
    internal static class StateCache
    {
        public static string RevitVersion = "";
        public static string RevitVersionName = "";
        public static string RevitBuild = "";
        public static volatile Dictionary<string, object> LastInfo;
        public static volatile Dictionary<string, object> LastDialog;

        public static void Refresh(UIApplication app)
        {
            try { LastInfo = Handlers.BuildInfo(app, false); }
            catch (Exception) { }
        }

        public static void RecordDialog(string kind, string id, string message)
        {
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["kind"] = kind;
            d["dialog_id"] = id;
            d["message"] = message;
            d["time"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
            LastDialog = d;
            BridgeLog.Write("dialog showing: " + kind + " id=" + id + " msg=" + message);
        }

        public static string DialogHint()
        {
            Dictionary<string, object> d = LastDialog;
            if (d == null) return "";
            return "最近一次偵測到的對話框：" + Convert.ToString(d["dialog_id"]) + "（" + Convert.ToString(d["time"]) + "）。";
        }
    }

    public class BridgeServer
    {
        private const int MaxRequestBytes = 16 * 1024 * 1024;
        private const int MaxClients = 8;
        private const double DefaultTimeoutSeconds = 120.0;
        private const double InfoTimeoutSeconds = 4.0;

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
                    "請用 `netstat -ano | findstr :" + port + "` 查詢，或設定環境變數 REVIT_BRIDGE_PORT 後重新啟動 Revit。");
            }
            listener = l;
            Port = port;
            running = true;
            startedAt = DateTime.Now;
            Interlocked.Exchange(ref requestCount, 0);
            acceptThread = new Thread(AcceptLoop);
            acceptThread.IsBackground = true;
            acceptThread.Name = "RevitAgentBridge-accept";
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
                t.Name = "RevitAgentBridge-client";
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
                        WriteLine(stream, Json.Err(null, "請求超過 16MB 上限。", "ProtocolError", null));
                        break;
                    }
                    // 從緩衝取出所有完整的行
                    byte[] all = pending.ToArray();
                    // 防瀏覽器跨站：網頁可以 fetch('http://127.0.0.1:9879', {body: '{...}\n'})，
                    // HTTP 請求行／標頭不是 JSON。只要連線送來的內容不是以 { 開頭就立刻斷線，
                    // 夾在 body 裡的 JSON 永遠不會被執行。
                    if (!IsJsonStart(all))
                    {
                        BridgeLog.Write("rejected non-JSON client (possible HTTP/browser request)");
                        break;
                    }
                    int start = 0;
                    bool reject = false;
                    for (int i = 0; i < all.Length; i++)
                    {
                        if (all[i] != (byte)'\n') continue;
                        string line = Encoding.UTF8.GetString(all, start, i - start).Trim();
                        start = i + 1;
                        if (line.Length == 0) continue;
                        if (line[0] != '{') { reject = true; break; }
                        string resp = Dispatch(line);
                        WriteLine(stream, resp);
                    }
                    if (reject)
                    {
                        BridgeLog.Write("rejected non-JSON line; connection closed");
                        break;
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

        /// <summary>緩衝內第一個非空白位元組是 {（或還沒有非空白內容）才算合法。</summary>
        private static bool IsJsonStart(byte[] data)
        {
            for (int i = 0; i < data.Length; i++)
            {
                byte b = data[i];
                if (b == (byte)' ' || b == (byte)'\t' || b == (byte)'\r' || b == (byte)'\n') continue;
                return b == (byte)'{';
            }
            return true;
        }

        private static void WriteLine(NetworkStream stream, string json)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(json + "\n");
            stream.Write(bytes, 0, bytes.Length);
            stream.Flush();
        }

        public Dictionary<string, object> StatusDict()
        {
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["running"] = running;
            d["host"] = "127.0.0.1";
            d["port"] = Port;
            d["clients"] = ClientCount;
            d["requests"] = RequestCount;
            d["started_at"] = startedAt.ToString("yyyy-MM-dd HH:mm:ss");
            d["pending_queue"] = ApiBridge.Queue.Count;
            d["last_dialog"] = StateCache.LastDialog;
            return d;
        }

        private string Dispatch(string line)
        {
            Interlocked.Increment(ref requestCount);

            object id = null;
            string method = null;
            Dictionary<string, object> prm = null;
            double timeout = DefaultTimeoutSeconds;
            try
            {
                Dictionary<string, object> req = Json.Parse(line) as Dictionary<string, object>;
                if (req == null) return Json.Err(null, "請求必須是 JSON 物件。", "ProtocolError", null);
                if (req.ContainsKey("id")) id = req["id"];
                if (req.ContainsKey("method")) method = req["method"] as string;
                if (req.ContainsKey("params")) prm = req["params"] as Dictionary<string, object>;
                if (req.ContainsKey("timeout") && req["timeout"] != null)
                {
                    double t;
                    if (double.TryParse(Convert.ToString(req["timeout"], System.Globalization.CultureInfo.InvariantCulture),
                        System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out t) && t > 0)
                        timeout = Math.Max(1.0, t - 1.0); // 比 client 的逾時早一點，讓 client 收得到伺服器端的錯誤
                }
            }
            catch (Exception ex)
            {
                return Json.Err(null, "請求不是合法 JSON：" + ex.Message, "ProtocolError", null);
            }
            if (string.IsNullOrEmpty(method)) return Json.Err(id, "請求缺少 method。", "ProtocolError", null);
            if (prm == null) prm = new Dictionary<string, object>();

            // ---- 不需要 API context 的指令 ----
            try
            {
                if (method == "ping") return Json.Ok(id, PingInfo());
                if (method == "get_status") return Json.Ok(id, StatusDict());
                if (method == "list_commands") return Json.Ok(id, Handlers.CommandNames());
            }
            catch (Exception ex)
            {
                return Json.FromException(id, ex);
            }

            // ---- 派進 Revit API 執行緒 ----
            bool isInfo = method == "get_info";
            double effective = isInfo ? Math.Min(timeout, InfoTimeoutSeconds) : timeout;

            Pending p = new Pending();
            p.Id = id;
            p.Method = method;
            p.Params = prm;
            ApiBridge.Queue.Enqueue(p);
            ApiBridge.Raise();

            DateTime deadline = DateTime.Now.AddSeconds(effective);
            while (!p.Done.WaitOne(500))
            {
                bool started;
                lock (p) { started = p.Started; }
                if (!started) ApiBridge.Raise(); // ExternalEvent 偶爾會被合併，重複 Raise 是安全的
                if (DateTime.Now >= deadline)
                {
                    lock (p) { started = p.Started; if (!started) p.Abandoned = true; }
                    return TimeoutResponse(id, method, effective, started, isInfo);
                }
            }
            return p.Response;
        }

        private string TimeoutResponse(object id, string method, double seconds, bool started, bool isInfo)
        {
            string dialog = StateCache.DialogHint();
            if (isInfo)
            {
                // get_info 逾時：回傳快取的資訊，並標明 Revit 目前沒有回應
                Dictionary<string, object> info = StateCache.LastInfo;
                Dictionary<string, object> r = info == null ? new Dictionary<string, object>() : new Dictionary<string, object>(info);
                r["stale"] = true;
                r["api_context"] = false;
                r["warning"] = "Revit 的 API 執行緒在 " + seconds.ToString("0.#") + " 秒內沒有回應，以下是最近一次快取的資訊。"
                               + "Revit 可能停在對話框，請使用者處理。" + dialog;
                r["bridge"] = StatusDict();
                return Json.Ok(id, r);
            }
            string msg = started
                ? "指令在 Revit 內執行超過 " + seconds.ToString("0.#") + " 秒仍未完成（可能仍在背景跑，或 Revit 卡在對話框）。"
                : "Revit 的 API 執行緒在 " + seconds.ToString("0.#") + " 秒內沒有空檔處理這個指令；指令已取消。";
            msg += "Revit 可能停在對話框，請使用者處理（可用 revit_window_screenshot 查看畫面）；不要強制結束 Revit。" + dialog;
            return Json.Err(id, msg, "TimeoutError", null);
        }

        private Dictionary<string, object> PingInfo()
        {
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["pong"] = true;
            d["bridge"] = "revit-agent-bridge";
            d["bridge_version"] = "1.0.0";
            d["revit_version"] = StateCache.RevitVersionName;
            d["port"] = Port;
            return d;
        }
    }
}
