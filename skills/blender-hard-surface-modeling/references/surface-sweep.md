# 在 Blender 中复现贴面扫掠

本示例使用 Hard Surface Workflow 0.3.7 和 Windows Blender 5.3.0（验收 build `02f0899d78e3`）。需要一个 Blender 平面和一份文本配方。 [扩展发行页](https://github.com/tsist/blender-hard-surface-workflow/releases/tag/v0.3.7)

1. 在新场景的单位设置中选 Metric，Unit Scale 设为 1，长度可显示为 Millimeters。在世界原点添加 Plane，把默认的 2 m 平面缩放到 200 mm × 200 mm（`S` → `0.1`），在 Object Mode 应用缩放（`Ctrl+A → Scale`）。平面朝 +Z。
2. 在 Text Editor 新建 Text，将示例 JSON 的完整内容粘进去。保持平面为活动对象，在 3D 视图打开 **N → Hard Surface → 贴面扫掠**，选中该 Text。 [矩形扫掠配方](surface-sweep-example.json)
3. 点 **预览**。预览线框应沿平面形成一段带圆弯的 4 × 2 mm 胶条，胶条底部目标间隙为 0.2 mm，净空下限为 0.1 mm。核对宽高、净空和相交读数，确认后点 **提交**。
4. 在 Outliner 中找到名称末尾为 `Bend` 的 Empty，沿世界 +Y 移动 3 mm（`G` → `Y` → `3mm`）。看候选线框是否只在 `Left lock` 和 `Right lock` 之间改形。点 **提交** 保存新形，或点 **取消** 恢复上次提交的站位。保存后独立重开，以同机位对比锁区外、弯道和两端的轮廓与截面。

## 把示例用于零件

选择一个连续的安装面，在配方中按路径顺序写站位。位置采用**世界毫米**，射线从站位沿 `ray_direction` 打向求值后的母面。用 `outward_normal` 指定预期安装侧，首尾锁住。局改两边也设锁站，并在锁站内外保留同直线上的引导站，让局改后的路径在锁站处保持原切向。每次只提交一个未锁站的位置变化。

`STRAIGHT` 适用于没有转折的站，`ROUND` 以 `radius_mm` 控制圆弯，`MITER` 以 `miter_limit_deg` 限制斜接角。可把示例中心 `Bend` 改为 `MITER`，删除该站的 `radius_mm` 并填写 `miter_limit_deg`。预检拒绝漏射、错面、跨相离区域、圆弯空间不足，以及尺寸、净空或相交检查不合格的配方。按回执中的站名复查射线命中和转角前后空间。

在独立副本中重建空心管时，可将名称换成 `Example Hollow Pipe`，将 `profile` 换成下面一段。检查两端端口环面、通孔、弯中的壁厚和最近的安装件。

```json
{"type": "PIPE", "outer_diameter_mm": 4.6, "wall_mm": 0.8, "sides": 32}
```

`gap_mm` 是目标间隙，`min_clearance_mm` 是最低净空，`gap_tolerance_mm` 是允许的间隙偏差。保存求值后的成品，在同样位置实切直段、弯前后和弯内，分别量宽高或内外径与壁厚。装配复核时把相邻固定件放回原位，检查扫掠件与其余零件的净空。镜面或亮面件用横纵两向条光检查折面和轮廓。
