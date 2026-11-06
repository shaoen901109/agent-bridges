# encoding: UTF-8
#
# 把 SketchUp 物件轉成可 JSON 化的 Hash / Array / 純量

module SUAgentBridge
  module Serialize

    module_function

    # 實體的簡短摘要
    # entity :: Sketchup::Entity
    # unit   :: 輸出長度用的單位字串
    def entity_summary(entity, unit = Units::DEFAULT_UNIT)
      return nil if entity.nil?
      info = {
        'persistent_id' => safe { entity.persistent_id },
        'entity_id'     => safe { entity.entityID },
        'type'          => entity.class.name.to_s.split('::').last,
        'valid'         => safe { entity.valid? }
      }

      info['name']  = safe { entity.name } if entity.respond_to?(:name)
      info['layer'] = safe { entity.layer && entity.layer.name } if entity.respond_to?(:layer)
      info['hidden'] = safe { entity.hidden? } if entity.respond_to?(:hidden?)

      if entity.respond_to?(:material)
        material = safe { entity.material }
        info['material'] = material ? safe { material.display_name } : nil
      end

      if entity.respond_to?(:bounds)
        info['bounds'] = bounds_hash(safe { entity.bounds }, unit)
      end

      case entity
      when Sketchup::Face
        info['area_mm2'] = safe { (entity.area / (Units::TO_INCH['mm']**2)).round(3) }
        info['vertex_count'] = safe { entity.vertices.size }
        info['normal'] = safe { [entity.normal.x, entity.normal.y, entity.normal.z].map { |v| v.to_f.round(6) } }
      when Sketchup::Edge
        info['length'] = safe { Units.from_inch(entity.length, unit).round(6) }
      when Sketchup::Group
        info['definition'] = safe { entity.definition.name }
        info['entity_count'] = safe { entity.entities.size }
      when Sketchup::ComponentInstance
        info['definition'] = safe { entity.definition.name }
        info['entity_count'] = safe { entity.definition.entities.size }
      when Sketchup::Text, Sketchup::DimensionLinear
        info['text'] = safe { entity.text }
      end

      info
    end

    # BoundingBox → Hash（外部單位）
    def bounds_hash(bounds, unit = Units::DEFAULT_UNIT)
      return nil if bounds.nil? || (bounds.respond_to?(:empty?) && bounds.empty?)
      {
        'min'  => Units.point_to_a(bounds.min, unit),
        'max'  => Units.point_to_a(bounds.max, unit),
        'size' => [bounds.width, bounds.height, bounds.depth].map { |v| Units.from_inch(v, unit).round(6) },
        'units' => unit
      }
    rescue StandardError
      nil
    end

    # 相機 → Hash
    def camera_hash(camera, unit = Units::DEFAULT_UNIT)
      {
        'eye'         => Units.point_to_a(camera.eye, unit),
        'target'      => Units.point_to_a(camera.target, unit),
        'up'          => [camera.up.x, camera.up.y, camera.up.z].map { |v| v.to_f.round(6) },
        'perspective' => camera.perspective?,
        'fov'         => (camera.perspective? ? camera.fov.to_f.round(4) : nil),
        'height'      => (camera.perspective? ? nil : Units.from_inch(camera.height, unit).round(6)),
        'aspect_ratio' => safe { camera.aspect_ratio.to_f },
        'units'       => unit
      }
    end

    # 把任意 Ruby 物件轉成可 JSON 化的東西（給 eval_ruby 用）
    # depth 用來避免無限遞迴 / 爆量輸出
    def jsonable(value, depth = 0)
      return '…（巢狀過深，已截斷）' if depth > 6

      case value
      when nil, true, false, Integer, String
        value.is_a?(String) ? truncate(value) : value
      when Float
        value.nan? || value.infinite? ? value.to_s : value
      when Symbol
        value.to_s
      when Length
        { 'inch' => value.to_f.round(9), 'mm' => Units.from_inch(value, 'mm').round(6) }
      when Geom::Point3d
        { 'point_mm' => Units.point_to_a(value, 'mm') }
      when Geom::Vector3d
        { 'vector' => [value.x.to_f, value.y.to_f, value.z.to_f].map { |v| v.round(9) } }
      when Geom::Transformation
        { 'transformation' => value.to_a.map { |v| v.to_f.round(9) } }
      when Sketchup::Color
        { 'color' => [value.red, value.green, value.blue, value.alpha] }
      when Array
        value.first(500).map { |v| jsonable(v, depth + 1) }
      when Hash
        result = {}
        value.first(200).each { |k, v| result[k.to_s] = jsonable(v, depth + 1) }
        result
      when Sketchup::Entity
        entity_summary(value)
      when Sketchup::Material
        { 'material' => safe { value.display_name } }
      when Sketchup::Layer
        { 'layer' => safe { value.name } }
      when Sketchup::ComponentDefinition
        { 'definition' => safe { value.name }, 'instances' => safe { value.count_instances } }
      else
        if value.respond_to?(:to_a) && value.class.name.to_s.start_with?('Sketchup::')
          # Entities / Selection 之類的集合
          arr = safe { value.to_a } || []
          { 'collection' => value.class.name, 'size' => arr.size,
            'items' => arr.first(200).map { |v| jsonable(v, depth + 1) } }
        else
          truncate(value.inspect)
        end
      end
    rescue StandardError => e
      "（無法序列化：#{e.class}: #{e.message}）"
    end

    MAX_STRING = 20_000

    def truncate(string, limit = MAX_STRING)
      s = string.to_s
      s.length > limit ? "#{s[0, limit]}…（已截斷，原長度 #{s.length}）" : s
    end

    # 執行 block，任何例外都回傳 nil
    def safe
      yield
    rescue StandardError
      nil
    end

  end
end
