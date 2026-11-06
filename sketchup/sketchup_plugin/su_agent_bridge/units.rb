# encoding: UTF-8
#
# 單位轉換工具
#
# 重要：SketchUp 內部一律使用「英吋(inch)」作為長度單位（Length 物件）。
# 本橋接器對外的預設單位是「公釐(mm)」，所有 params 中的長度若未指定 units 即視為 mm。

module SUAgentBridge
  module Units

    # 每個單位對應多少英吋
    TO_INCH = {
      'mm' => 1.0 / 25.4,
      'cm' => 1.0 / 2.54,
      'm'  => 1000.0 / 25.4,
      'in' => 1.0,
      'inch' => 1.0,
      'ft' => 12.0,
      'feet' => 12.0
    }.freeze

    DEFAULT_UNIT = 'mm'.freeze

    # 正規化單位字串
    def self.normalize(unit)
      u = (unit || DEFAULT_UNIT).to_s.strip.downcase
      raise ArgumentError, "不支援的單位：#{unit.inspect}（可用：mm, cm, m, in, ft）" unless TO_INCH.key?(u)
      u
    end

    # 把外部數值轉成 SketchUp 內部英吋數值（Float）
    def self.to_inch(value, unit = DEFAULT_UNIT)
      raise ArgumentError, "長度必須是數值，收到 #{value.inspect}" unless value.is_a?(Numeric)
      value.to_f * TO_INCH[normalize(unit)]
    end

    # 把 SketchUp 內部英吋數值轉回外部單位
    def self.from_inch(value, unit = DEFAULT_UNIT)
      value.to_f / TO_INCH[normalize(unit)]
    end

    # 將 [x, y, z] 陣列（外部單位）轉成 Geom::Point3d（英吋）
    def self.to_point(array, unit = DEFAULT_UNIT)
      unless array.is_a?(Array) && array.size == 3
        raise ArgumentError, "點座標必須是 [x, y, z] 三元素陣列，收到 #{array.inspect}"
      end
      Geom::Point3d.new(*array.map { |v| to_inch(v, unit) })
    end

    # 將 [x, y, z] 陣列（外部單位）轉成 Geom::Vector3d（英吋）
    def self.to_vector(array, unit = DEFAULT_UNIT)
      unless array.is_a?(Array) && array.size == 3
        raise ArgumentError, "向量必須是 [x, y, z] 三元素陣列，收到 #{array.inspect}"
      end
      Geom::Vector3d.new(*array.map { |v| to_inch(v, unit) })
    end

    # 將 Geom::Point3d / Vector3d 轉成外部單位的陣列（四捨五入到 6 位小數）
    def self.point_to_a(point, unit = DEFAULT_UNIT)
      return nil if point.nil?
      [point.x, point.y, point.z].map { |v| (from_inch(v, unit)).round(6) }
    end

  end
end
