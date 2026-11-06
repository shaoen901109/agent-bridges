# encoding: UTF-8
#
# 非阻塞式 TCP 伺服器
#
# 設計重點：
#   * SketchUp 內使用 Ruby Thread 並不可靠（API 必須在主執行緒呼叫），
#     因此這裡用 UI.start_timer 定時輪詢 accept_nonblock / read_nonblock，
#     所有 SketchUp API 呼叫都發生在主執行緒的 timer callback 內。
#   * 絕不使用會卡住 UI 的阻塞式 accept / read / write。
#   * 單一指令拋出例外只會回傳錯誤給該 client，不會讓伺服器停止。
#
# 協定：換行分隔的 JSON（newline-delimited JSON）
#   request : {"id": 1, "method": "ping", "params": {}}\n
#   response: {"id": 1, "ok": true,  "result": {...}}\n
#           : {"id": 1, "ok": false, "error": {"message": "...", "class": "...", "backtrace": [...]}}\n

require 'socket'
require 'json'

module SUAgentBridge

  class Server

    # 輪詢間隔（秒）
    POLL_INTERVAL = 0.05
    # 單次讀取大小
    READ_CHUNK = 64 * 1024
    # 單一 request 行的長度上限（防止惡意/異常輸入吃光記憶體）
    MAX_REQUEST_BYTES = 8 * 1024 * 1024
    # 同時連線數上限
    MAX_CLIENTS = 8

    attr_reader :port

    # 單一 client 連線的緩衝狀態
    class Connection
      attr_reader :socket
      attr_accessor :inbuf, :outbuf, :closing

      def initialize(socket)
        @socket  = socket
        @inbuf   = String.new('', :encoding => Encoding::BINARY)
        @outbuf  = String.new('', :encoding => Encoding::BINARY)
        @closing = false
      end

      def close
        @socket.close
      rescue StandardError
        nil
      end
    end

    def initialize
      @tcp_server  = nil
      @connections = []
      @timer_id    = nil
      @port        = nil
      @ticking     = false
      @request_count = 0
    end

    def running?
      !@tcp_server.nil?
    end

    def connection_count
      @connections.size
    end

    def request_count
      @request_count
    end

    # 啟動監聽。失敗時丟出帶有清楚訊息的例外。
    def start(port)
      if running?
        raise RuntimeError, "Agent Bridge 伺服器已經在 127.0.0.1:#{@port} 執行中。請先停止再重新啟動。"
      end

      port = port.to_i
      if port < 1024 || port > 65535
        raise ArgumentError, "連接埠必須介於 1024 與 65535 之間，收到 #{port}。"
      end

      begin
        @tcp_server = TCPServer.new('127.0.0.1', port)
      rescue Errno::EADDRINUSE
        @tcp_server = nil
        raise RuntimeError,
              "連接埠 #{port} 已被其他程式佔用。\n" \
              "請改用其他連接埠（延伸程式 → Agent Bridge → 設定連接埠…），\n" \
              "或在命令提示字元執行 `netstat -ano | findstr :#{port}` 找出佔用的程式。"
      rescue Errno::EACCES
        @tcp_server = nil
        raise RuntimeError, "沒有權限綁定連接埠 #{port}。請改用 1024 以上且未被系統保留的連接埠。"
      rescue StandardError => e
        @tcp_server = nil
        raise RuntimeError, "無法在 127.0.0.1:#{port} 建立監聽：#{e.class}: #{e.message}"
      end

      @port = port
      @connections = []
      @request_count = 0
      @timer_id = UI.start_timer(POLL_INTERVAL, true) { tick }

      SUAgentBridge.log("伺服器已啟動：127.0.0.1:#{@port}（僅接受本機連線）")
      true
    end

    # 停止監聽並關閉所有連線。
    def stop
      if @timer_id
        begin
          UI.stop_timer(@timer_id)
        rescue StandardError
          nil
        end
        @timer_id = nil
      end

      @connections.each(&:close)
      @connections = []

      if @tcp_server
        begin
          @tcp_server.close
        rescue StandardError
          nil
        end
        @tcp_server = nil
      end

      SUAgentBridge.log('伺服器已停止。')
      true
    end

    # --- 內部：主迴圈 ---------------------------------------------------------

    def tick
      return if @ticking || @tcp_server.nil?
      @ticking = true
      begin
        accept_pending
        @connections.each { |conn| pump(conn) }
        @connections.reject! do |conn|
          if conn.closing && conn.outbuf.empty?
            conn.close
            true
          else
            false
          end
        end
      rescue StandardError, ScriptError => e
        # 主迴圈本身出錯絕不能讓 timer 停掉
        SUAgentBridge.log("主迴圈例外（已忽略）：#{e.class}: #{e.message}")
      ensure
        @ticking = false
      end
      nil
    end

    def accept_pending
      loop do
        socket = @tcp_server.accept_nonblock(:exception => false)
        break if socket.nil? || socket == :wait_readable

        if @connections.size >= MAX_CLIENTS
          begin
            socket.write(JSON.generate(
              'id' => nil, 'ok' => false,
              'error' => { 'message' => "連線數已達上限 #{MAX_CLIENTS}。" }
            ) + "\n")
            socket.close
          rescue StandardError
            nil
          end
          next
        end

        begin
          socket.sync = true
        rescue StandardError
          nil
        end
        @connections << Connection.new(socket)
        SUAgentBridge.log("新連線（目前 #{@connections.size} 條）")
      end
    rescue IO::WaitReadable, Errno::EWOULDBLOCK, Errno::EAGAIN
      nil
    rescue StandardError => e
      SUAgentBridge.log("accept 失敗：#{e.class}: #{e.message}")
    end

    # 讀 → 處理 → 寫
    def pump(conn)
      read_available(conn)
      process_lines(conn)
      flush(conn)
    end

    def read_available(conn)
      return if conn.closing
      loop do
        data = conn.socket.read_nonblock(READ_CHUNK, :exception => false)
        if data.nil?
          # 對方關閉連線（EOF）
          conn.closing = true
          break
        end
        break if data == :wait_readable
        conn.inbuf << data
        if conn.inbuf.bytesize > MAX_REQUEST_BYTES
          respond(conn, nil, false, nil,
                  { 'message' => "單一請求超過 #{MAX_REQUEST_BYTES} bytes 上限，連線將關閉。" })
          conn.inbuf.clear
          conn.closing = true
          break
        end
      end
    rescue EOFError, Errno::ECONNRESET, Errno::ECONNABORTED, IOError
      conn.closing = true
    rescue StandardError => e
      SUAgentBridge.log("讀取失敗：#{e.class}: #{e.message}")
      conn.closing = true
    end

    def process_lines(conn)
      while (index = conn.inbuf.index("\n".b))
        line = conn.inbuf.slice!(0, index + 1)
        line = line.chomp
        next if line.strip.empty?
        handle_line(conn, line)
      end
    end

    def handle_line(conn, raw_line)
      line = raw_line.dup.force_encoding(Encoding::UTF_8)
      request_id = nil
      begin
        request = JSON.parse(line)
        unless request.is_a?(Hash)
          raise ArgumentError, '請求必須是 JSON 物件，例如 {"id":1,"method":"ping"}。'
        end
        request_id = request['id']
        method_name = request['method']
        if method_name.nil? || method_name.to_s.empty?
          raise ArgumentError, '請求缺少 method 欄位。'
        end

        @request_count += 1
        result = Commands.dispatch(method_name.to_s, request['params'])
        respond(conn, request_id, true, result, nil)
      rescue JSON::ParserError => e
        respond(conn, request_id, false, nil,
                { 'class' => 'JSON::ParserError',
                  'message' => "無法解析 JSON：#{e.message}" })
      rescue StandardError, ScriptError => e
        respond(conn, request_id, false, nil,
                { 'class'     => e.class.name,
                  'message'   => e.message.to_s,
                  'backtrace' => (e.backtrace || []).first(20) })
        SUAgentBridge.log("指令錯誤：#{e.class}: #{e.message}")
      end
    end

    def respond(conn, request_id, ok, result, error)
      payload = { 'id' => request_id, 'ok' => ok }
      payload['result'] = result if ok
      payload['error']  = error unless ok

      json = begin
        JSON.generate(payload)
      rescue StandardError => e
        JSON.generate('id' => request_id, 'ok' => false,
                      'error' => { 'class' => e.class.name,
                                   'message' => "結果無法轉成 JSON：#{e.message}" })
      end

      conn.outbuf << json.b << "\n".b
      nil
    end

    def flush(conn)
      until conn.outbuf.empty?
        written = conn.socket.write_nonblock(conn.outbuf, :exception => false)
        break if written == :wait_writable
        break if written.nil? || written <= 0
        conn.outbuf.slice!(0, written)
      end
    rescue Errno::EPIPE, Errno::ECONNRESET, Errno::ECONNABORTED, IOError
      conn.outbuf.clear
      conn.closing = true
    rescue StandardError => e
      SUAgentBridge.log("寫入失敗：#{e.class}: #{e.message}")
      conn.outbuf.clear
      conn.closing = true
    end

  end
end
