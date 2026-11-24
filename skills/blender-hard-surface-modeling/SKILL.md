---
name: blender-hard-surface-modeling
description: 在 Blender 中规划硬表面建模方案、按制造与装配逻辑拆分零件，并通过全四边控制笼或加权法线中模完成可编辑硬表面白模。接到工业产品、家电、工具、机械装备、载具或精确装配壳体的从零建模、外部候选重建与局部返修时使用。
metadata:
  version: "1.0.0"
---

# Blender 硬表面建模

本技能属于 [建模部门](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/modeling.md)，负责把参考图和结构需求变成比例准确、分件合理、面流顺滑、留足改型余地的可编辑硬表面白模。硬表面好不好改、高光顺不顺，八成在动第一条边之前就已经决定：先把零件怎么拆、每件走哪条路线、怎么起坯定死，再上控制笼和修改器栈。

所有已保存 `.blend` 的后台读写、几何审计与冷重开都通过公共入口 `blenderctl` 完成，查当前工具入口先运行：

```powershell
$ctl = './blenderctl.ps1'
& $ctl discover --product hard-surface
& $ctl model flow-tools
```

## 怎么做

### 第一步：配准参考，按制造逻辑分好件

开工前先把米制尺度（`1 unit = 1 m`，Z 向上，正面朝 `-Y`）和正交参考图锁死。用轴距、总长宽高或核心圆心对齐正、侧、顶三视图，再配一张无遮挡斜视图认清曲面凹凸与翻边深度；参考图统一收进隐藏的 `REF_` 集合并锁定选择。

接着按真实产品的制造与装配顺序拆分零件，前三条确定物理归属，后两条在不破坏物理结构的前提下优化布线和复用：

1. **制造工艺与分型**：注塑外壳按合模分型线拆上下壳或左右壳，冲压钣金、铸件、机加工件、包胶嵌件、透明罩片各自独立成件。
2. **运动关系**：旋钮、按键、翻盖、门板、滑块、轮组各为独立对象，原点直接落在真实转轴或滑轨基准上，标明允许的单一自由度与行程。
3. **材质与表面分区**：高光烤漆、磨砂塑料、阳极铝、橡胶握套、透明玻璃按真实边界拆件；纯平面印刷标识留在同件交材质处理。
4. **可见分缝与拆装方向**：明确每道缝两侧归谁、拆下外盖后底下露出的是承台、内壁还是通孔，避免两件在缝底打架。
5. **建模与复用便利**：把密集孔阵、散热格栅、饰条拆出独立件隔离局部密度；左右对称或圆周重复结构缩成 1/2、1/4 或单扇区再镜像/阵列。

### 第二步：选定路线、起坯方式与建模计划

硬表面三条正式路线按零件特点混用：**Sub-D 全四边控制笼**负责带自由曲面、圆润过渡和特写高光的主壳；**非细分中模（倒角 + 加权法线）**负责机械支架、液压筒、工业箱体和规则切面件；**混合装配**在同一整机中让流线外皮走 Sub-D、内部承力机构走中模。布尔操作用于平面精准开孔或在副本上求复杂相贯交线，曲面上的正式开口一律回归显式四边面带。

按零件特征选定起坯方式，开工前把交接结构和第一次试配检查一次定清：

| 起坯方式 | 适用部件 | 交接结构与关键做法 | 首次装配检查 |
|---|---|---|---|
| **同源母面切分** | 共享连贯高光的相邻外板（汽车翼子板与机盖、飞机蒙皮、手柄/鼠标上下壳） | 先建光顺无孔的连续母面并另存备份；按真实缝线切分（非破坏用引导母面 `Y` 拆 + 缩裹栈，特写实翻边用 `P` 分离后挤内翻边加卡线），留足等宽缝隙与隐藏止口 | 隐藏母面后同机位查跨缝条形高光连续度、实剖缝宽与翻边净空 |
| **独立起坯装配** | 独立功能件、刚性机构、活动件、模块化壳体（阀门、夹爪、键盘、灯组内构、起落架） | 共用同一世界装配基准与中心轴；各件独立规划控制笼密度与细分预算，先做轴孔、承座与定位止挡再细化外观 | 以共同轴心或装配平面切剖面，核对贴座、侧隙、轴向净空与活动行程 |
| **平面展开或代理成形** | 单轴恒曲率多孔圆筒/滚花环、双曲壳上的凸筋格栅与百叶 | 恒曲率件在平面布好全四边孔环与支撑带，先细分再 `Bend` 卷圆并限组 `Weld(ALL)`；双曲凸筋在平态绑定平面代理（`Surface Deform`），由代理缩裹贴合母面 | 核对卷圆背缝无内部夹墙、孔径按目标轴线贯通、凸筋保高无压扁 |

- **写建模计划**：一二级消费小件与简单家电在开工说明里列清零件清单、路线和起坯方式即可动工；三级及以上产品（或零件超 10 件、含运动机构、有跨缝共享母面）先写一页 `plan.md`（零件表、大纲装配树、关键截面与最小公倍数分段、高风险局部小样、白模停点）。遇到陌生结构或原创设计先请用户确认计划再建模，按已定参考图或教程施工直接推进。
- **搭好大纲骨架**：整件资产收在单一根集合下，内部按功能总成建子集合，参考图放 `REF_`、布尔切刀放 `CUTTER_`（同步关 `hide_render`）。零件按「部位 + 角色」语义命名，对称件加 `_L` / `_R`，静件原点归底面中心 `[0,0,0]` 或总成基准，动件原点落转轴。完整方法与 `plan.md` 模板见 [建模计划与资产分件](references/planning-and-part-breakdown.md)。

### 第三步：低密搭控制笼，全场景保持全四边面

先搭大形体量（Blockout）核对多视角剪影和运动包络，再把主壳细化为稀疏、可读的四边面控制笼：

1. **先顺母面再落特征**：用尽量少的截面站锁定最高点、拐点、肩线和收尖，调顺两条互相垂直方向的条形高光后再开孔、起槽和拉翻边。开孔前复制一份无孔母面留作缩裹或法线传递基准。
2. **段数按最小公倍数前置配平**：圆柱相贯、多瓣对称或孔阵先算清匹配段数（如 6 棱接 4 槽用 24 段、8 瓣手轮用 24 段、5 辐轮毂用 40 段、高光漆面柱面开孔常用 32 段配 16 边孔），让孔周保留均匀四边缓冲圈。
3. **全场景保持全四边面**：无论 Sub-D 曲面还是静态中模的纯平面封口，一律收成规整四边面（平口优先 `Grid Fill` 或同心内插四边环），清零 N-gon；把 3 价与 5 价极点推离圆角和高光脊线，落在平坦过渡区，可见曲面上清零 6 边及以上的星形极点。
4. **控制线各司其职**：分清决定轮廓的造型环、收紧圆角的支撑环（卡线）和收束密度的过渡环；加线前先明确一条环的起点、走向和归宿。详见 [控制笼布线与卡线](references/topology-and-support-loops.md) 与 [曲面开孔、相贯与翻边](references/cutouts-and-flanges.md)。

### 第四步：按黄金栈序装配修改器与分件接口

所有物体上修改器前先应用缩放（`Ctrl+A → Scale`，含负缩放时勾选 `Corrective Flip Normals`），统一外表面法线（`Shift+N`），并在进 `Subdivision` 前清理 `Mark Sharp` 以免退回 CPU 细分。按固定次序堆叠修改器，避免层间互相破坏：

- **对称与合并**：`Mirror(Clipping ✓, Merge)` 始终排在 `Subdivision` 之上；阵列或卷圆合缝用 `Weld(Mode=ALL, Vertex Group=接缝组)` 并排在 `Solidify` 之前。
- **曲率贴合与弯曲**：`Subdivision(Boundary Smooth=Keep Corners)` 排在 `Shrinkwrap` 或 `Simple Deform(Bend)` 之前，贴合时用顶点组排除孔壁、翻边和凸台。
- **起厚与倒角**：先在单层面上开孔理顺布线，再向内 `Solidify(Offset=-1, Even Thickness ✓)`，最后对需要收圆的棱加 `Bevel(Limit=Weight, Miter Outer=Arc)`；同一零件多组不同半径圆角拆成各自命名的边权属性（`bevel_weight_edge`），开 `Clamp Overlap` 后如产生重合顶点接 `Weld` 焊清。
- **法线与高光**：非细分中模严守 `Bevel → Smooth by Angle(关 Pin to Last) → Weighted Normal(Face Area, Keep Sharp ✓)`；用 `Data Transfer` 从无孔母面传递自定义法线时放最底端，起厚后经 `Solidify Shell VG + VertexWeightMix(SUB)` 扣除生成内壁，保住内壁法线朝向。详见 [修改器栈规范](references/modifier-stacks.md) 与 [跨件装配与结构接口](references/assembly-and-interfaces.md)。

### 第五步：过程看线框与条光，改型验证后送审

建模过程一律在 **3D 视口实体叠加线框** 中边做边查，把渲染留给最终审看。原创或按参考图制作时，以轮廓比例、分件合理、面流顺滑与改型可控为准；复刻既有教程或拆解实物时，同步按原工序画面逐段核对关键边环的起止与接法。

1. **分层核对**：分别查看 L0 控制笼、保存的视口求值与渲染求值（两者级别保持一致），关键 Sub-D 曲面在 L1 与 L2 下扫横纵两向条形高光，查清凹陷、极点夹痕与跨缝断光。
2. **用插件做精准定位**：查单条边环/面带归宿走侧栏 **Hard Surface → 面流检查**（离线用 `model inspect --face-flow`）；出同机位白模/控制笼/求值线框拼图用 **多视角检查**（`--review-grid`）；量穿插、缝宽、孔径与壁厚走 **几何与装配审计**（`--geometry-audit`）。操作入口见 [硬表面工具速查](references/tool-workbench.md)。
3. **实做一次局部改型**：每个主装配在副本上改一次代表性尺寸（如改大一个孔径、拉长一截翻边或移一道分缝），确认目标面带连续跟随、保护基准与邻件不跑偏，验证通过后退回定稿态。
4. **维护三个文件点位**：每条工作线只留「只读源文件 + 当前工作副本 + 最近通过检查的结构锚点」；出错优先定位第一道坏工序回退重做，局部误调可用随包 `plan_step_rewind.py` / `plan_vertex_rewind.py` 精准恢复。详见 [过程检查、改型与返修](references/inspection-and-rework.md)。

## 交给下游

白模定稿前，先按 [blender-modeling-review](https://github.com/tsist/blender-modeling-review/SKILL.md) 的 G1–G4 跑一遍只读体检（`model inspect --qa-profile render` 或 `game` + `commercial_qa_scorecard.py`），确保冷重开零报错、切刀全关 `hide_render`、米制 `-Y` 朝向、缩放归 `1,1,1`、单一根集合零默认名、全四边零 N-gon 且高光顺滑，再送独立审核签认并呈交用户做 **审看①（白模）**。

交付物固定带齐四项：

1. **可编辑白模工作副本**：路径与 SHA-256（保留核心非破坏修改器栈、打包参考图或清理外部依赖）。
2. **零件与装配清单**：对象名、所属子集合、路线与修改器说明；含运动件时附枢轴位置、允许轴向与行程范围。
3. **白模看图包**：同机位控制笼线框、细分白模、双方向条形高光拼图，以及关键接口剖面和局部改型前后对照图。
4. **体检与退路**：自检与独立审核回执路径，以及最近一次结构锚点路径。

白模经用户拍板后，直接交 [blender-uv](https://github.com/tsist/blender-uv-workflow/blob/main/skills/blender-uv/SKILL.md) 展 UV、[blender-material-authoring](https://github.com/tsist/blender-material-workflow/blob/main/skills/blender-material-authoring/SKILL.md) 做材质，或交 [blender-rigging](https://github.com/tsist/blender-rigging-workflow/blob/main/skills/blender-rigging/SKILL.md) 接机械/角色绑定。

## 例子：从零规划并做出一台高速电吹风可编辑白模

接到委托：「按正、侧、斜三张产品参考图做一台高速电吹风（二级家电），要求磁吸集风嘴和进风滤网可拆、手柄开关滑槽好调尺寸，按 L2 商业标准交白模审看。」

1. **配准参考与分件**：按真实毫米尺寸（筒长 96 mm、筒径 76 mm、柄长 148 mm）校准三视图收进 `REF_Hairdryer`。按制造与装配拆为 6 个主件并收入单一根集合 `Hairdryer_Pro` 下的三个子集合：`Housing`（一体 T 形主机壳 `Body_Shell`、手柄底盖 `Handle_Cap`）、`Airflow`（前出风双环格栅 `Front_Grille`、可拆磁吸进风滤网 `Intake_Filter`、圆转扁磁吸集风嘴 `Nozzle_Flat`）、`Controls`（推拉开关滑键 `Switch_Slider` 与档位按键 `Mode_Button`）。开工短清单直接列明：主机壳与集风嘴走 Sub-D 控制笼，前格栅与磁吸环走径向阵列 + Sub-D。
2. **选起坯与定段数**：主机壳采用 24 段主筒 + 12 段手柄 T 形相贯，保留无孔母面 `REF_Body_Guide` 作缩裹参考；前出风 7 槽格栅按 `7×7` 预留 49 段基环，进风滤网用 23 对错排渐变孔按 46 段单周期径向阵列；集风嘴锁定圆端 24 段对接主筒，扁端沿对称中线用奇数分段保中线过渡。
3. **搭控制笼与修改器栈**：在主机壳先顺好 T 形肩部四边过渡带，再开手柄滑槽与按键孔（孔周留 3 圈均匀四边缓冲带），挂载 `SubD(Keep Corners) → Shrinkwrap(VG_OuterSkin，排除槽壁) → Solidify(Offset=-1, 1.8 mm) → Bevel(Weight, Arc)`；集风嘴的扁口用 `F` 封口后做均等偏移内插拉出内外双唇并转全四边，确保长短轴壁厚均匀；所有平面封口均用 `Grid Fill` 或同心四边环清零 N-gon。
4. **自检与改型验证**：过程中全程开实体叠加线框检查 L0 与 L2，用横纵条形高光核对 T 形相贯肩和滑槽外周，通过 `model inspect --geometry-audit` 抽测进风孔逐孔贯通、壁厚与磁吸唇间隙（保留 0.3 mm 装配缝）。在副本上把开关滑槽加长 4 mm、把集风嘴扁口收窄 2 mm，确认母面高光和圆端装配环纹丝不动。
5. **体检与交付**：跑 `model inspect --qa-profile render` 与 `commercial_qa_scorecard.py` 确认 G1–G4 全绿，生成同机位线框/白模/高光拼图与改型对照图，送独立审核签认后呈用户完成审看①，连同工作副本、SHA-256 与对象清单交下游展 UV。

## 参考

- [建模计划与资产分件](references/planning-and-part-breakdown.md) — 分级计划门槛、`plan.md` 模板、五级分件判据、起坯三路线、大纲装配树与公倍数分段
- [控制笼布线与卡线](references/topology-and-support-loops.md) — 全四边面流、两族控制线、卡线距与变宽圆角、极点退让、减线与渐消棱
- [曲面开孔、相贯与翻边](references/cutouts-and-flanges.md) — 柱面/双曲开孔、斜交相贯、翻边内偏净空、跨折凹槽与展开/代理成形
- [修改器栈规范](references/modifier-stacks.md) — 黄金修改器次序、倒角权重与防钳制、Solidify 内壁隔离、焊接与法线传递
- [跨件装配与结构接口](references/assembly-and-interfaces.md) — 三类分缝与隐藏止口、间隙公差、活动副行程、卡扣/搭接/拔模与螺纹收口
- [过程检查、改型与返修](references/inspection-and-rework.md) — 线框与条光自检、改型测试、锚点管理、双回退脚本用法与送审交接
- [硬表面工具速查](references/tool-workbench.md) — 原生工具与 Hard Surface Workflow 按工序速查、面流/多视角/几何审计入口
- [来源、实证与归档索引](references/sources.md) — 教程库导航、定量实证依据与旧版资料归档位置
- 精准回退脚本：`scripts/plan_step_rewind.py` · `scripts/plan_vertex_rewind.py`

维护：唯一维护源是 `./.agents/skills/blender-hard-surface-modeling/`，改完运行 `tools/sync_skills.ps1` 更新双端镜像。
