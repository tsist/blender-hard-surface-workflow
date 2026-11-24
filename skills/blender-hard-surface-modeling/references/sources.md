# 来源、实证与归档索引

本技能全部操作规范均沉淀自行业通用硬表面建模实践、Blender 官方文档及本项目本地实测，按主题汇总出处如下，方便溯源与深度学习。

## 一、项目教程深读与总装手册

位于工作区 `tutorial-library/`，覆盖从一级消费小件到六级航空器的端到端硬表面案例（按授权级别与本地复用规则查阅）：

- **真实产品一至六级跨梯队速查与总装手册**（`tutorial-library/hard-surface-real-products/`）：
  - 一级消费小件：`消费小件Xbox后续_工程交接速查_20261122.md`（iPhone 14 Pro Max、中画幅概念相机、机械键盘、复古卡带机、Walkman）
  - 二级家电与三级手持工具：`家电与手持工具_共性构造工程交接速查_20261122.md`（米家电吹风 PRO、小风扇、搅拌机、意式咖啡机、十字螺丝刀、筋膜枪、冲击钻、老虎钳）
  - 四级工业、五级汽车与六级航空器：`工业器材汽车与民航机_跨梯队工程交接速查_20261122.md`（手轮阀、室内云台摄像头、俯仰球机、扁线电机定子、法拉利 FXX-K、保时捷 911、Tesla Model Y、Cirrus SF50、DC-10-30）
  - 五级量产车完整总装：`砚池街NoOne_ModelY全33集高精度白模总装手册_20261123.md`
  - 六级航空器完整总装：`六级航空器长流程白模总装手册_SF50_DC10_Stearman_20261123.md`
- **硬表面拓扑与高阶复合曲面专题**（`tutorial-library/hard-surface-topology/`）：
  - `专题_硬表面SubD曲面布线与边流极点方法论总谱_5.2.1实证.md`（Probes J / K / L：柱面开孔曲率贴合、极点落区、全四边端盖与折痕 Quality）
  - `专题_高阶单体硬表面复合曲面相贯与A级高光连续性_5.2.1实证.md`（八大耦合定律与 Probes M1–M7：展开卷圆焊缝、导向母面贴合、平态代理保高、三叉斜接渐消、跨缝止口、斜交穹顶与 Solidify Shell 内壁法线隔离）
  - `官方_Pixar_OpenSubdiv建模注记与Blender5.2细分曲面权威指南.md` 与 `TopologyGuides_硬表面SubD边流极点与密度缩减图谱.md`
- **修改器与工具演进专题**（`tutorial-library/hard-surface-modifiers/`）：
  - `专题_硬表面卡线倒角加权法线与布尔清理工作流_5.2.1实证.md`（实证 A/B/C：法线黄金栈序、Boolean 三求解器与 Clamp+Weld、Subsurf 折痕响应）
  - `专题_Blender5.0新阵列修改器全解与5.2.1实证.md`
  - `专题_免费开源硬表面效率工具链与5.2.1实证.md`
  - `专题_Blender5.2与5.3网格建模工具演进与硬表面无缝拓扑清理_5.2.1实证.md`（Probes G / H / I：Shrinkwrap 与 Data Transfer 栈序、布尔六类故障、LoopTools 量化）

## 二、本地插件、CLI 契约与共享 Wiki

- **Hard Surface Workflow 插件源码与目录**：`tools/hard_surface_workflow_addon/catalog.json`（完整 62 项网格工具与 27 项对象/修改器工具定义）、`deliveries/hard-surface-workflow-0.3.6/README.md`
- **公共 CLI 会话与预览契约**：`docs/cli/workflows/session-and-preview.md`
- **协作技能**：
  - [blender-work-methodology](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/SKILL.md) 与 [建模部门卡](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/modeling.md)
  - [blender-modeling-reference](https://github.com/tsist/blender-modeling-reference/blob/main/SKILL.md)（前期参考图包、尺寸表与一致性核对单）
  - [simready-hard-surface-research](https://github.com/tsist/simready-hard-surface-research/blob/main/SKILL.md)（工业机械 OpenUSD 结构拆解与枢轴参考）
  - [blender-modeling-review](https://github.com/tsist/blender-modeling-review/blob/main/SKILL.md)（L0–L3 四档商业标尺与 G1–G5 五级审核红线）
  - [blender-cli](https://github.com/tsist/blenderctl/blob/main/skills/blender-cli/SKILL.md)（公共 CLI 作业、依赖核验与事务发布）
- **共享 Wiki 对应词条**：
  - `procedures/blender-hard-surface-planning-and-part-breakdown.md`（建模计划与资产分件方法论）
  - `procedures/blender-subd-curved-surface-routing-and-pole-methodology.md`（Sub-D 曲面布线与极点迁移）

## 三、历史实证与旧版技能归档

- **本地试件与多级产品白模实证目录**：`work/hard-surface-topology-foundations/`、`work/hard-surface-modifier-basics/`、`work/hard-surface-modifier-verification/`、`work/hard-surface-combined-cases/`、`work/real-hard-surface-practice-20261120/`
- **重构前旧版技能完整归档**：`archives/blender-hard-surface-modeling-legacy-20261124/`（保留重构前的 20 篇旧文档、全部 63 张实图及停用的 `measure_pair.py` 脚本，供随时回溯查阅）
