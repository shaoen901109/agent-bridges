# encoding: UTF-8
#
# Agent Bridge for SketchUp — 載入器（extension registration）
#
# 這個檔案只負責向 SketchUp 註冊擴充功能，真正的程式碼在 su_agent_bridge/ 資料夾內。
# 預設「不會」啟動伺服器；使用者必須從 延伸程式(Extensions) → Agent Bridge → 啟動伺服器 手動開啟。

require 'sketchup.rb'
require 'extensions.rb'

module SUAgentBridge

  # 插件版本
  VERSION = '1.0.0'.freeze

  # 這個載入器檔案所在的資料夾（通常是 Plugins 資料夾）
  PLUGIN_DIR  = File.dirname(__FILE__).freeze
  # 實作程式碼資料夾
  SOURCE_DIR  = File.join(PLUGIN_DIR, 'su_agent_bridge').freeze

  unless defined?(@extension_registered) && @extension_registered
    loader = File.join(SOURCE_DIR, 'main')

    @extension = SketchupExtension.new('Agent Bridge', loader)
    @extension.version     = VERSION
    @extension.creator     = 'SketchUp 代理操控插件'
    @extension.copyright   = '2026'
    @extension.description =
      '讓本機 AI agent（Claude Code 等支援 MCP 的客戶端）透過 127.0.0.1 的 TCP ' \
      '介面操控 SketchUp：查詢模型、執行 Ruby API、切換工具、擷取視埠截圖。' \
      '伺服器預設關閉，需從 延伸程式 → Agent Bridge 手動啟動。'

    Sketchup.register_extension(@extension, true)
    @extension_registered = true
  end

  # 提供給外部查詢的 extension 物件
  def self.extension
    @extension
  end

end
