# encoding: UTF-8
#
# 指令實作（method 名稱 → 處理程式）
#
# 所有指令都在 SketchUp 主執行緒（UI.start_timer 的 callback）內被呼叫，
# 因此可以安全地使用 SketchUp API。
#
# 會修改模型的指令一律包在 model.start_operation / commit_operation 內，
# 讓使用者可以用一次 Ctrl+Z 復原。

require 'json'
require 'base64'
require 'stringio'
require 'tmpdir'
require 'fileutils'

module SUAgentBridge
  module Commands

    module_function

    # 常用的 Sketchup.send_action 字串
    COMMON_ACTIONS = {
      'selectSelectionTool:'  => '選取工具 (Select)',
      'selectLineTool:'       => '線條工具 (Line)',
      'selectRectangleTool:'  => '矩形工具 (Rectangle)',
      'selectCircleTool:'     => '圓形工具 (Circle)',
      'selectArcTool:'        => '弧形工具 (Arc)',
      'selectPushPullTool:'   => '推拉工具 (Push/Pull)',
      'selectMoveTool:'       => '移動工具 (Move)',
      'selectRotateTool:'     => '旋轉工具 (Rotate)',
      'selectScaleTool:'      => '比例工具 (Scale)',
      'selectOffsetTool:'     => '偏移工具 (Offset)',
      'selectEraseTool:'      => '橡皮擦 (Eraser)',
      'selectPaintTool:'      => '油漆桶 (Paint Bucket)',
      'selectMeasureTool:'    => '捲尺 (Tape Measure)',
      'selectDimensionTool:'  => '標註 (Dimension)',
      'selectTextTool:'       => '文字 (Text)',
      'selectOrbitTool:'      => '環繞檢視 (Orbit)',
      'selectPanTool:'        => '平移 (Pan)',
      'selectZoomTool:'       => '縮放 (Zoom)',
      'selectFollowMeTool:'   => '路徑跟隨 (Follow Me)',
      'viewTop:'              => '俯視圖',
      'viewBottom:'           => '仰視圖',
      'viewFront:'            => '前視圖',
      'viewBack:'             => '後視圖',
      'viewLeft:'             => '左視圖',
      'viewRight:'            => '右視圖',
      'viewIso:'              => '等角視圖',
      'viewZoomExtents:'      => '縮放至全部範圍',
      'viewPerspective:'      => '切換透視',
      'renderWireframe:'      => '線框顯示',
      'renderHiddenLine:'     => '隱藏線顯示',
      'renderShaded:'         => '著色顯示',
      'renderTextured:'       => '材質貼圖顯示',
      'renderMonochrome:'     => '單色顯示',
      'editUndo:'             => '復原',
      'editRedo:'             => '重做',
      'editDelete:'           => '刪除選取',
      'editHide:'             => '隱藏選取',
      'editUnhide:'           => '取消隱藏'
    }.freeze

    # 好記的短名稱 → send_action 字串
    TOOL_ALIASES = {
      'select'     => 'selectSelectionTool:',
      'selection'  => 'selectSelectionTool:',
      'line'       => 'selectLineTool:',
      'pencil'     => 'selectLineTool:',
      'rectangle'  => 'selectRectangleTool:',
      'rect'       => 'selectRectangleTool:',
      'circle'     => 'selectCircleTool:',
      'arc'        => 'selectArcTool:',
      'pushpull'   => 'selectPushPullTool:',
      'push_pull'  => 'selectPushPullTool:',
      'move'       => 'selectMoveTool:',
      'rotate'     => 'selectRotateTool:',
      'scale'      => 'selectScaleTool:',
      'offset'     => 'selectOffsetTool:',
      'erase'      => 'selectEraseTool:',
      'eraser'     => 'selectEraseTool:',
      'paint'      => 'selectPaintTool:',
      'material'   => 'selectPaintTool:',
      'measure'    => 'selectMeasureTool:',
      'tape'       => 'selectMeasureTool:',
      'dimension'  => 'selectDimensionTool:',
      'text'       => 'selectTextTool:',
      'orbit'      => 'selectOrbitTool:',
      'pan'        => 'selectPanTool:',
      'zoom'       => 'selectZoomTool:',
      'followme'   => 'selectFollowMeTool:',
      'follow_me'  => 'selectFollowMeTool:'
    }.freeze

    # send_action 是非同步的（SketchUp 會排進自己的訊息佇列），
    # 因此回應中的 active_tool 可能還沒更新。
    ASYNC_NOTE = 'send_action 由 SketchUp 非同步處理，回應中的狀態可能尚未更新；' \
                 '需要確認結果請稍後另外查詢（例如 eval_ruby ' \
                 '"Sketchup.active_model.tools.active_tool_name"）。'.freeze

    STANDARD_VIEWS = {
      'top'    => 'viewTop:',
      'bottom' => 'viewBottom:',
      'front'  => 'viewFront:',
      'back'   => 'viewBack:',
      'left'   => 'viewLeft:',
      'right'  => 'viewRight:',
      'iso'    => 'viewIso:'
    }.freeze

    # 分派表。key 為 method 字串。
    def dispatch(method_name, params)
      handler = "cmd_#{method_name}"
      unless respond_to?(handler)
        raise ArgumentError, "未知的指令：#{method_name}（可用指令請呼叫 list_commands）"
      end
      send(handler, params || {})
    end

    def command_names
      methods.map(&:to_s).select { |m| m.start_with?('cmd_') }.map { |m| m.sub('cmd_', '') }.sort
    end

    # --- 共用小工具 -----------------------------------------------------------

    def model
      m = Sketchup.active_model
      raise RuntimeError, '目前沒有開啟的 SketchUp 模型。' if m.nil?
      m
    end

    def unit_of(params)
      Units.normalize(params['units'] || params['unit'] || Units::DEFAULT_UNIT)
    end

    # 包一層 undo operation
    def with_operation(name)
      m = model
      m.start_operation(name, true)
      begin
        result = yield(m)
        m.commit_operation
        result
      rescue StandardError, ScriptError => e
        begin
          m.abort_operation
        rescue StandardError
          nil
        end
        raise e
      end
    end

    # 依 persistent_id 或 entityID 找實體。ids 可為單一值或陣列。
    # 預設在整個模型（含群組/元件內部）遞迴尋找。
    def find_entities(ids, container = nil)
      ids = [ids] unless ids.is_a?(Array)
      wanted = ids.map(&:to_i)
      found  = {}
      scan_entities(container || model.entities) do |entity|
        pid = Serialize.safe { entity.persistent_id }
        eid = Serialize.safe { entity.entityID }
        if wanted.include?(pid)
          found[pid] ||= entity
        elsif wanted.include?(eid)
          found[eid] ||= entity
        end
      end
      missing = wanted.reject { |id| found.key?(id) }
      unless missing.empty?
        raise ArgumentError, "找不到下列實體 id：#{missing.join(', ')}（請先用 list_entities 取得 persistent_id）"
      end
      wanted.map { |id| found[id] }
    end

    # 遞迴走訪實體
    def scan_entities(entities, depth = 0, &block)
      return if depth > 12
      entities.each do |entity|
        block.call(entity)
        case entity
        when Sketchup::Group
          scan_entities(entity.entities, depth + 1, &block)
        when Sketchup::ComponentInstance
          scan_entities(entity.definition.entities, depth + 1, &block)
        end
      end
    end

    # --- 基本資訊 -------------------------------------------------------------

    def cmd_ping(_params)
      { 'pong' => true, 'time' => Time.now.to_f, 'version' => SUAgentBridge::VERSION }
    end

    def cmd_list_commands(_params)
      { 'commands' => command_names }
    end

    def cmd_get_info(params)
      unit = unit_of(params)
      m = model
      opts = m.options['UnitsOptions']
      {
        'plugin_version'   => SUAgentBridge::VERSION,
        'sketchup_version' => Sketchup.version,
        'sketchup_version_number' => Sketchup.version_number,
        'is_pro'           => Sketchup.is_pro?,
        'ruby_version'     => RUBY_VERSION,
        'platform'         => (Sketchup.platform.to_s rescue RUBY_PLATFORM),
        'locale'           => (Sketchup.get_locale rescue nil),
        'model_title'      => m.title,
        'model_path'       => (m.path.nil? || m.path.empty? ? nil : m.path),
        'model_saved'      => !(m.path.nil? || m.path.empty?),
        'model_modified'   => Serialize.safe { m.modified? },
        'entity_count_top_level' => m.entities.size,
        'model_units'      => {
          'length_format' => Serialize.safe { opts['LengthFormat'] },
          'length_unit'   => Serialize.safe { opts['LengthUnit'] },
          'length_precision' => Serialize.safe { opts['LengthPrecision'] }
        },
        'bridge_units'     => unit,
        'active_tool'      => Serialize.safe { m.tools.active_tool_name },
        'selection_count'  => m.selection.size,
        'server'           => SUAgentBridge.server_status
      }
    end

    def cmd_get_scene_info(params)
      unit  = unit_of(params)
      limit = (params['limit'] || 200).to_i
      m = model

      counts = Hash.new(0)
      total  = 0
      scan_entities(m.entities) do |entity|
        counts[entity.class.name.to_s.split('::').last] += 1
        total += 1
      end

      layers = m.layers.to_a.first(limit).map do |layer|
        { 'name' => layer.name, 'visible' => layer.visible?,
          'folder' => Serialize.safe { layer.folder && layer.folder.display_name } }
      end

      materials = m.materials.to_a.first(limit).map do |mat|
        { 'name' => mat.name, 'display_name' => Serialize.safe { mat.display_name },
          'color' => Serialize.safe { c = mat.color; c && [c.red, c.green, c.blue] },
          'alpha' => Serialize.safe { mat.alpha },
          'has_texture' => Serialize.safe { !mat.texture.nil? } }
      end

      definitions = m.definitions.to_a.reject { |d| d.image? }.first(limit).map do |defn|
        { 'name' => defn.name, 'instances' => Serialize.safe { defn.count_instances },
          'is_group' => defn.group?, 'entity_count' => defn.entities.size }
      end

      pages = m.pages.to_a.first(limit).map { |p| { 'name' => p.name } }

      {
        'entity_counts'      => counts,
        'entity_total'       => total,
        'top_level_count'    => m.entities.size,
        'bounds'             => Serialize.bounds_hash(m.bounds, unit),
        'layers'             => layers,
        'layer_total'        => m.layers.size,
        'materials'          => materials,
        'material_total'     => m.materials.size,
        'definitions'        => definitions,
        'definition_total'   => m.definitions.size,
        'pages'              => pages,
        'page_total'         => m.pages.size,
        'selection'          => {
          'count' => m.selection.size,
          'items' => m.selection.to_a.first([limit, 50].min).map { |e| Serialize.entity_summary(e, unit) }
        },
        'truncated'          => (m.layers.size > limit || m.materials.size > limit ||
                                 m.definitions.size > limit || m.selection.size > limit),
        'limit'              => limit,
        'units'              => unit
      }
    end

    # --- 實體查詢 / 選取 -------------------------------------------------------

    # params:
    #   scope: 'top' (預設，僅最上層) | 'all' (遞迴) | 'selection'
    #   types: ['Face','Edge',...]  篩選型別
    #   limit: 預設 200
    #   offset: 預設 0
    def cmd_list_entities(params)
      unit   = unit_of(params)
      scope  = (params['scope'] || 'top').to_s
      limit  = (params['limit'] || 200).to_i
      offset = (params['offset'] || 0).to_i
      types  = params['types']
      types  = [types] if types.is_a?(String)

      collected = []
      case scope
      when 'selection'
        collected = model.selection.to_a
      when 'all'
        scan_entities(model.entities) { |e| collected << e }
      when 'top'
        collected = model.entities.to_a
      else
        raise ArgumentError, "scope 必須是 'top'、'all' 或 'selection'，收到 #{scope.inspect}"
      end

      if types && !types.empty?
        wanted = types.map { |t| t.to_s.downcase }
        collected = collected.select { |e| wanted.include?(e.class.name.to_s.split('::').last.downcase) }
      end

      total = collected.size
      page  = collected[offset, limit] || []

      {
        'total'     => total,
        'offset'    => offset,
        'limit'     => limit,
        'returned'  => page.size,
        'truncated' => (offset + page.size) < total,
        'scope'     => scope,
        'units'     => unit,
        'entities'  => page.map { |e| Serialize.entity_summary(e, unit) }
      }
    end

    def cmd_get_selection(params)
      unit  = unit_of(params)
      limit = (params['limit'] || 200).to_i
      sel   = model.selection
      {
        'count'     => sel.size,
        'truncated' => sel.size > limit,
        'units'     => unit,
        'entities'  => sel.to_a.first(limit).map { |e| Serialize.entity_summary(e, unit) }
      }
    end

    # params: ids (陣列) / clear (true 清空) / add (true 為加入，預設取代)
    def cmd_select_entities(params)
      sel = model.selection
      if params['clear']
        sel.clear
        return { 'count' => 0, 'cleared' => true }
      end
      ids = params['ids'] || params['persistent_ids']
      raise ArgumentError, '必須提供 ids（persistent_id 陣列）或 clear=true。' if ids.nil?
      entities = find_entities(ids)
      sel.clear unless params['add']
      sel.add(entities)
      { 'count' => sel.size, 'selected_ids' => entities.map { |e| Serialize.safe { e.persistent_id } } }
    end

    def cmd_delete_entities(params)
      ids = params['ids'] || params['persistent_ids']
      raise ArgumentError, '必須提供 ids（persistent_id 陣列）。' if ids.nil?
      entities = find_entities(ids)
      with_operation('Agent: 刪除實體') do |_m|
        entities.each { |e| e.erase! if e.valid? }
      end
      { 'deleted' => entities.size }
    end

    # --- 截圖 -----------------------------------------------------------------

    # params: width, height, antialias, transparent, path（不給則存到暫存資料夾）,
    #         return_base64（預設 true）
    def cmd_screenshot(params)
      view   = model.active_view
      width  = (params['width']  || view.vpwidth).to_i
      height = (params['height'] || view.vpheight).to_i
      width  = 32  if width  < 32
      height = 32  if height < 32
      width  = 4096 if width  > 4096
      height = 4096 if height > 4096

      path = params['path']
      if path.nil? || path.to_s.empty?
        dir = File.join(Dir.tmpdir, 'su_agent_bridge')
        FileUtils.mkdir_p(dir)
        path = File.join(dir, "viewport_#{Time.now.strftime('%Y%m%d_%H%M%S')}_#{rand(9999)}.png")
      else
        path = path.to_s
        FileUtils.mkdir_p(File.dirname(path))
      end

      options = {
        :filename    => path,
        :width       => width,
        :height      => height,
        :antialias   => params.key?('antialias') ? !!params['antialias'] : true,
        :compression => (params['compression'] || 0.9).to_f,
        :transparent => !!params['transparent']
      }

      ok = view.write_image(options)
      raise RuntimeError, "view.write_image 失敗（路徑：#{path}）" unless ok && File.exist?(path)

      result = {
        'path'   => path,
        'width'  => width,
        'height' => height,
        'bytes'  => File.size(path)
      }

      return_base64 = params.key?('return_base64') ? !!params['return_base64'] : true
      if return_base64
        data = File.open(path, 'rb') { |f| f.read }
        result['image_base64'] = Base64.strict_encode64(data)
        result['mime_type']    = 'image/png'
      end
      result
    end

    # --- 相機 -----------------------------------------------------------------

    def cmd_get_camera(params)
      Serialize.camera_hash(model.active_view.camera, unit_of(params))
    end

    # params: eye, target, up（陣列，外部單位）, perspective(bool), fov(度),
    #         view（標準視角名稱）, zoom_extents(bool)
    def cmd_set_camera(params)
      unit = unit_of(params)
      view = model.active_view

      if params['view']
        name   = params['view'].to_s.downcase
        action = STANDARD_VIEWS[name]
        raise ArgumentError, "未知的標準視角：#{name}（可用：#{STANDARD_VIEWS.keys.join(', ')}）" if action.nil?
        Sketchup.send_action(action)
      end

      if params['eye'] || params['target'] || params['up']
        camera = view.camera
        eye    = params['eye']    ? Units.to_point(params['eye'], unit)    : camera.eye
        target = params['target'] ? Units.to_point(params['target'], unit) : camera.target
        up     = params['up']     ? Geom::Vector3d.new(*params['up'].map(&:to_f)) : camera.up
        view.camera.set(eye, target, up)
      end

      unless params['perspective'].nil?
        view.camera.perspective = !!params['perspective']
      end

      if params['fov'] && view.camera.perspective?
        view.camera.fov = params['fov'].to_f
      end

      if params['zoom_extents']
        view.zoom_extents
      end

      view.invalidate
      Serialize.camera_hash(view.camera, unit)
    end

    def cmd_zoom_extents(params)
      view = model.active_view
      view.zoom_extents
      view.invalidate
      Serialize.camera_hash(view.camera, unit_of(params))
    end

    # --- 工具 / send_action ---------------------------------------------------

    def cmd_list_actions(_params)
      { 'actions' => COMMON_ACTIONS, 'tool_aliases' => TOOL_ALIASES, 'note' => ASYNC_NOTE }
    end

    def cmd_send_action(params)
      action = params['action']
      raise ArgumentError, '必須提供 action 字串，例如 "selectLineTool:"。' if action.nil?
      ok = Sketchup.send_action(action.to_s)
      { 'action' => action.to_s, 'sent' => !!ok,
        'active_tool_before' => Serialize.safe { model.tools.active_tool_name },
        'note' => ASYNC_NOTE }
    end

    # select_tool 是 send_action 的友善包裝，
    # 接受 'line'、'pushpull'、'selectLineTool:' 等寫法。
    def cmd_select_tool(params)
      tool = params['tool'].to_s
      raise ArgumentError, '必須提供 tool。' if tool.empty?
      key = tool.downcase
      action = if COMMON_ACTIONS.key?(tool)
                 tool
               elsif TOOL_ALIASES.key?(key)
                 TOOL_ALIASES[key]
               else
                 candidate = "select#{tool[0].upcase}#{tool[1..-1]}Tool:"
                 COMMON_ACTIONS.key?(candidate) ? candidate : nil
               end
      if action.nil?
        raise ArgumentError,
              "未知的工具：#{tool}（可用短名稱：#{TOOL_ALIASES.keys.join(', ')}；" \
              '完整清單請呼叫 list_actions）'
      end
      cmd_send_action('action' => action)
    end

    # --- 建模捷徑 -------------------------------------------------------------

    # params: origin([x,y,z] 預設 [0,0,0]), width, depth, height, units, name, group(預設 true)
    def cmd_create_box(params)
      unit   = unit_of(params)
      origin = Units.to_point(params['origin'] || [0, 0, 0], unit)
      w = Units.to_inch(params['width']  || raise(ArgumentError, '必須提供 width'),  unit)
      d = Units.to_inch(params['depth']  || raise(ArgumentError, '必須提供 depth'),  unit)
      h = Units.to_inch(params['height'] || raise(ArgumentError, '必須提供 height'), unit)
      make_group = params.key?('group') ? !!params['group'] : true

      result = with_operation('Agent: 建立方塊') do |m|
        container = make_group ? m.entities.add_group : m
        entities  = make_group ? container.entities : m.entities

        pts = [
          origin,
          origin + Geom::Vector3d.new(w, 0, 0),
          origin + Geom::Vector3d.new(w, d, 0),
          origin + Geom::Vector3d.new(0, d, 0)
        ]
        face = entities.add_face(pts)
        # add_face 的法線方向不保證朝上；統一讓 push_pull 往 +Z 長高
        face.reverse! if face.normal.z < 0
        face.pushpull(h)

        target = make_group ? container : face
        container.name = params['name'].to_s if make_group && params['name']
        Serialize.entity_summary(target, unit)
      end
      model.active_view.invalidate
      result
    end

    # params: points（[[x,y,z],...] 至少 3 點）, units, group(預設 true), name,
    #         push_pull（可選，擠出距離）
    def cmd_create_face(params)
      unit   = unit_of(params)
      points = params['points']
      unless points.is_a?(Array) && points.size >= 3
        raise ArgumentError, 'points 必須是至少 3 個 [x, y, z] 點的陣列。'
      end
      make_group = params.key?('group') ? !!params['group'] : true
      pts = points.map { |p| Units.to_point(p, unit) }

      result = with_operation('Agent: 建立面') do |m|
        container = make_group ? m.entities.add_group : m
        entities  = make_group ? container.entities : m.entities
        face = entities.add_face(pts)
        raise RuntimeError, '無法以這些點建立面（可能共線或不共面）。' if face.nil?
        if params['push_pull']
          distance = Units.to_inch(params['push_pull'], unit)
          face.pushpull(distance)
        end
        container.name = params['name'].to_s if make_group && params['name']
        Serialize.entity_summary(make_group ? container : face, unit)
      end
      model.active_view.invalidate
      result
    end

    # params: id（face 的 persistent_id）, distance, units, copy(bool)
    def cmd_push_pull(params)
      unit = unit_of(params)
      id   = params['id'] || params['persistent_id']
      raise ArgumentError, '必須提供 id（face 的 persistent_id）。' if id.nil?
      distance = Units.to_inch(params['distance'] || raise(ArgumentError, '必須提供 distance'), unit)
      copy = !!params['copy']

      face = find_entities(id).first
      raise ArgumentError, "id #{id} 不是 Face，而是 #{face.class.name}。" unless face.is_a?(Sketchup::Face)

      result = with_operation('Agent: 推拉') do |_m|
        face.pushpull(distance, copy)
        Serialize.entity_summary(face, unit)
      end
      model.active_view.invalidate
      result
    end

    # params: ids（陣列）, name
    def cmd_create_group(params)
      unit = unit_of(params)
      ids  = params['ids'] || params['persistent_ids']
      raise ArgumentError, '必須提供 ids（persistent_id 陣列）。' if ids.nil?
      entities = find_entities(ids)

      result = with_operation('Agent: 建立群組') do |m|
        group = m.entities.add_group(entities)
        group.name = params['name'].to_s if params['name']
        Serialize.entity_summary(group, unit)
      end
      model.active_view.invalidate
      result
    end

    # 通用變換：params: ids, translate([dx,dy,dz]), rotate({axis, origin, angle_deg}),
    #                  scale(數值或 [sx,sy,sz]), units, copy(bool)
    def cmd_transform_entities(params)
      unit = unit_of(params)
      ids  = params['ids'] || params['persistent_ids']
      raise ArgumentError, '必須提供 ids（persistent_id 陣列）。' if ids.nil?
      entities = find_entities(ids)

      transform = Geom::Transformation.new
      applied = []

      if params['translate']
        vector = Units.to_vector(params['translate'], unit)
        transform = Geom::Transformation.translation(vector) * transform
        applied << 'translate'
      end

      if params['rotate']
        r = params['rotate']
        origin = Units.to_point(r['origin'] || [0, 0, 0], unit)
        axis   = Geom::Vector3d.new(*(r['axis'] || [0, 0, 1]).map(&:to_f))
        angle  = (r['angle_deg'] || r['angle'] || 0).to_f * Math::PI / 180.0
        transform = Geom::Transformation.rotation(origin, axis, angle) * transform
        applied << 'rotate'
      end

      if params['scale']
        s = params['scale']
        origin = Units.to_point(params['scale_origin'] || [0, 0, 0], unit)
        transform = if s.is_a?(Array)
                      Geom::Transformation.scaling(origin, s[0].to_f, s[1].to_f, s[2].to_f) * transform
                    else
                      Geom::Transformation.scaling(origin, s.to_f) * transform
                    end
        applied << 'scale'
      end

      raise ArgumentError, '必須提供 translate、rotate 或 scale 其中至少一項。' if applied.empty?

      copy = !!params['copy']
      if copy
        bad = entities.reject { |e| e.is_a?(Sketchup::Group) || e.is_a?(Sketchup::ComponentInstance) }
        unless bad.empty?
          raise ArgumentError,
                'copy=true 僅支援 Group / ComponentInstance；' \
                "收到 #{bad.map { |e| e.class.name }.uniq.join(', ')}。" \
                '其他實體請改用 eval_ruby（例如 entities.add_instance 或先 add_group）。'
        end
      end

      result = with_operation('Agent: 變換實體') do |m|
        if copy
          copies = entities.map { |e| m.entities.add_instance(e.definition, e.transformation) }
          m.entities.transform_entities(transform, copies)
          copies.map { |e| Serialize.entity_summary(e, unit) }
        else
          m.entities.transform_entities(transform, entities)
          entities.map { |e| Serialize.entity_summary(e, unit) }
        end
      end
      model.active_view.invalidate
      { 'applied' => applied, 'copy' => copy, 'entities' => result }
    end

    # params: ids, material（材質名稱）或 color（'#RRGGBB' 或 [r,g,b]）, alpha
    def cmd_set_material(params)
      unit = unit_of(params)
      ids  = params['ids'] || params['persistent_ids']
      raise ArgumentError, '必須提供 ids（persistent_id 陣列）。' if ids.nil?
      entities = find_entities(ids)

      result = with_operation('Agent: 設定材質') do |m|
        material = nil
        if params['material']
          name = params['material'].to_s
          material = m.materials[name] || m.materials.add(name)
        end
        if params['color']
          material ||= m.materials.add(params['material_name'] || "Agent_#{m.materials.size + 1}")
          material.color = params['color'].is_a?(Array) ? Sketchup::Color.new(*params['color']) : params['color'].to_s
        end
        material.alpha = params['alpha'].to_f if material && params['alpha']

        entities.each do |e|
          raise ArgumentError, "#{e.class.name} 不支援設定材質。" unless e.respond_to?(:material=)
          e.material = material
        end
        { 'material' => material && material.display_name,
          'entities' => entities.map { |e| Serialize.entity_summary(e, unit) } }
      end
      model.active_view.invalidate
      result
    end

    # --- eval_ruby ------------------------------------------------------------

    # params: code（字串）, timeout 無（同步執行）
    def cmd_eval_ruby(params)
      code = params['code']
      raise ArgumentError, '必須提供 code 字串。' if code.nil? || code.to_s.empty?

      captured   = StringIO.new
      old_stdout = $stdout
      value      = nil
      begin
        $stdout = captured
        value = eval(code.to_s, TOPLEVEL_BINDING, '(agent_bridge_eval)')
      ensure
        $stdout = old_stdout
      end

      {
        'class'   => value.class.name,
        'inspect' => Serialize.truncate(value.inspect),
        'value'   => Serialize.jsonable(value),
        'stdout'  => Serialize.truncate(captured.string)
      }
    end

    # --- 復原 / 重做 ----------------------------------------------------------

    def cmd_undo(params)
      times = (params['times'] || 1).to_i
      times = 1 if times < 1
      times = 20 if times > 20
      times.times { Sketchup.send_action('editUndo:') }
      model.active_view.invalidate
      { 'undone' => times, 'note' => ASYNC_NOTE + '（復原完成後請重新查詢 list_entities 確認結果）' }
    end

    def cmd_redo(params)
      times = (params['times'] || 1).to_i
      times = 1 if times < 1
      times = 20 if times > 20
      times.times { Sketchup.send_action('editRedo:') }
      model.active_view.invalidate
      { 'redone' => times, 'note' => ASYNC_NOTE + '（重做完成後請重新查詢 list_entities 確認結果）' }
    end

    # --- 模型檔案 -------------------------------------------------------------

    def cmd_new_model(params)
      unless params['confirm']
        raise ArgumentError, '為避免遺失未存檔的變更，new_model 必須帶 confirm=true。'
      end
      Sketchup.file_new
      { 'ok' => true, 'model_title' => model.title }
    end

    def cmd_open_model(params)
      path = params['path'].to_s
      raise ArgumentError, '必須提供 path。' if path.empty?
      raise ArgumentError, "找不到檔案：#{path}" unless File.exist?(path)
      unless params['confirm']
        raise ArgumentError, '為避免遺失未存檔的變更，open_model 必須帶 confirm=true。'
      end
      ok = Sketchup.open_file(path)
      { 'ok' => !!ok, 'path' => path, 'model_title' => model.title }
    end

    # 安全規則：
    #   - 不帶 path 時只能存回既有 model.path，且必須 confirm=true
    #   - 帶 path 且該檔案已存在時，必須 overwrite=true
    def cmd_save_model(params)
      m = model
      path = params['path'].to_s
      if path.empty?
        current = m.path
        raise ArgumentError, '這個模型還沒有存檔路徑，請提供 path。' if current.nil? || current.empty?
        unless params['confirm']
          raise ArgumentError, "覆寫現有檔案需要 confirm=true（目標：#{current}）。"
        end
        ok = m.save
        return { 'ok' => !!ok, 'path' => current }
      end

      if File.exist?(path) && !params['overwrite']
        raise ArgumentError, "檔案已存在，若確定要覆寫請帶 overwrite=true：#{path}"
      end
      FileUtils.mkdir_p(File.dirname(path))
      ok = m.save(path)
      { 'ok' => !!ok, 'path' => path }
    end

  end
end
