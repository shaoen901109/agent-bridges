# encoding: UTF-8
#
# Agent Bridge for SketchUp — 主進入點
#
# 這個檔案由 su_agent_bridge.rb（SketchupExtension）載入。
# 載入時「只」建立選單、定義 API，不會自動開啟任何網路連接埠。

require 'sketchup.rb'

module SUAgentBridge

  SOURCE_DIR = File.dirname(__FILE__).freeze unless defined?(SOURCE_DIR)

  require File.join(SOURCE_DIR, 'units')
  require File.join(SOURCE_DIR, 'serialize')
  require File.join(SOURCE_DIR, 'commands')
  require File.join(SOURCE_DIR, 'server')

  # 預設連接埠。可用 Sketchup.write_default('SUAgentBridge', 'port', 9876) 修改。
  DEFAULT_PORT = 9876

  # 讀取設定的連接埠
  def self.configured_port
    value = Sketchup.read_default('SUAgentBridge', 'port', DEFAULT_PORT)
    value.to_i
  rescue StandardError
    DEFAULT_PORT
  end

  # 寫入設定的連接埠
  def self.configured_port=(port)
    Sketchup.write_default('SUAgentBridge', 'port', port.to_i)
  end

  # 取得（必要時建立）伺服器單例
  def self.server
    @server ||= Server.new
  end

  # 啟動伺服器。供選單與 -RubyStartup 自動化測試腳本呼叫。
  # 回傳 true 表示成功；失敗會丟出例外（自動化腳本可自行 rescue）。
  def self.start_server(port = nil)
    port ||= configured_port
    server.start(port)
  end

  # 停止伺服器。
  def self.stop_server
    server.stop
  end

  # 伺服器是否正在監聽
  def self.server_running?
    @server && @server.running?
  end

  # 狀態摘要（Hash）
  def self.server_status
    {
      'running'     => server_running? ? true : false,
      'port'        => server_running? ? server.port : configured_port,
      'connections' => server_running? ? server.connection_count : 0,
      'version'     => VERSION
    }
  end

  # 寫入記錄到 Ruby 主控台與狀態列
  def self.log(message)
    text = "[Agent Bridge] #{message}"
    puts text
    begin
      Sketchup.set_status_text(text)
    rescue StandardError
      nil
    end
    nil
  end

  # --- 選單 -----------------------------------------------------------------

  unless defined?(@menu_loaded) && @menu_loaded
    menu = UI.menu('Plugins').add_submenu('Agent Bridge')

    start_item = menu.add_item('啟動伺服器') do
      begin
        start_server
        UI.messagebox("Agent Bridge 伺服器已啟動\n位址：127.0.0.1:#{server.port}")
      rescue StandardError => e
        UI.messagebox("無法啟動 Agent Bridge 伺服器：\n\n#{e.message}")
      end
    end
    menu.set_validation_proc(start_item) { server_running? ? MF_GRAYED : MF_ENABLED }

    stop_item = menu.add_item('停止伺服器') do
      begin
        stop_server
        UI.messagebox('Agent Bridge 伺服器已停止')
      rescue StandardError => e
        UI.messagebox("停止伺服器時發生錯誤：\n\n#{e.message}")
      end
    end
    menu.set_validation_proc(stop_item) { server_running? ? MF_ENABLED : MF_GRAYED }

    menu.add_separator

    menu.add_item('狀態…') do
      status = server_status
      state  = status['running'] ? '執行中' : '已停止'
      UI.messagebox(
        "Agent Bridge v#{status['version']}\n\n" \
        "狀態：#{state}\n" \
        "位址：127.0.0.1:#{status['port']}\n" \
        "目前連線數：#{status['connections']}"
      )
    end

    menu.add_item('設定連接埠…') do
      current = configured_port
      result = UI.inputbox(['連接埠（1024-65535）'], [current.to_s], 'Agent Bridge 連接埠設定')
      if result
        port = result[0].to_i
        if port < 1024 || port > 65535
          UI.messagebox('連接埠必須介於 1024 與 65535 之間。')
        else
          self.configured_port = port
          UI.messagebox("連接埠已設為 #{port}。\n若伺服器正在執行，請先停止再重新啟動。")
        end
      end
    end

    @menu_loaded = true
  end

  log("已載入 v#{VERSION}（伺服器預設關閉，請由 延伸程式 → Agent Bridge → 啟動伺服器 開啟）")

end
