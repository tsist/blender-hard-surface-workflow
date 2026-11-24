# 硬表面工具速查

日常建模优先用 Blender 原生编辑工具和原生修改器；需要精准选环、批量理线、可参数化刀体/承座，或者做面流、线框与装配审计时，再调用 **Hard Surface Workflow** 插件侧栏（`3D 视图 → N → Hard Surface`）或公共 CLI。

所有工具的最新 ID、字段和 Schema 都用命令实时查询，免去猜参数名：

```powershell
$ctl = './blenderctl.ps1'

# 查看插件与 CLI 当前支持版本和入口
& $ctl discover --product hard-surface

# 列出全部网格工具与对象/修改器工具 ID，或查单个工具的完整字段
& $ctl model flow-tools
& $ctl model flow-tools --tool native.bevel

# 分别查看三大只读检查入口的请求 JSON 规范
& $ctl model flow-tools --face-flow
& $ctl model flow-tools --review-grid
& $ctl model flow-tools --geometry-audit
```

## 一、按工序选工具对照表

| 建模工序 | 首选原生操作 | Hard Surface Workflow 对应模块与代表工具 ID | 实操提醒 |
|---|---|---|---|
| **配准参考与建基准** | Image Empty 正交锁轴、3D 游标、自定义变换坐标系 | **Reference Contours / 可编辑工作平面**：`reference_contour`（提取正交外轮廓）、`profile_work_plane_create/update`（面/边正交工作平面）、`section_surface`（具名截面搭四边母面）、`surface_project_path`（向求值母面投射 3D 路径） | 工作平面保持无缩放、无剪切正交标架；修改控制笼后同步核对依赖面/边索引 |
| **精准选区与保护区** | 原生环选（`Alt+左键`）、边环（`Ctrl+Alt+左键`）、原生选区历史 | **编辑模式 → 选择 → 硬表面选取**：支持 `edge_loop`、`edge_ring`、`vertex_loop`、`face_loop`、坐标范围 `box` 与显式索引；批处理用 `protect` 选择器锁点/边/面 | 先在单对象编辑模式切到目标域并转视角看背面高亮；GUI 环选优先用原生环选或「硬表面选取」菜单确认实选结果；需要同时锁定边连接或面归属时，在磁盘请求里明示 `EDGE`/`FACE` 保护选择器 |
| **对齐、圆化与理线** | `S+轴+0` 压平、`G+G` Edge Slide、原生 Circularize / Space Evenly / Flatten | **工作台对齐与理线**：`align.axis/plane/reference`、`mesh.snap_symmetry`、内置 `looptools.circle/space/curve/flatten/relax`、内置 `edgeflow.set_flow/set_curve/set_linear/set_vertex_curve`、`looptools.gstretch`（沿标线拉伸） | 曲面整圆关闭 `Flatten`；`Set Flow` 与 `Relax` 选中拓扑干净的一小段并固定两端，避免压薄壁厚或拉扁孔径 |
| **切面、卡线、翻边与开孔** | 环切（`Ctrl+R`）、刀切（`K`）、内插（`I`）、倒角（`Ctrl+B`）、区域挤出（`Alt+E`）、`Grid Fill`、`Bridge Edge Loops` | **工作台网格成形**：`native.loop_cut_slide`、`native.split_face`（凸平面单面拆面）、`native.inset`、`native.bevel`、`native.flange`（沿开边逐站挤翻边）、`edit_mesh_tools.offset_edges`、`native.grid_fill`、`native.bridge`、`patch.quad_grid`、`form.hole/recess/push_pull/lathe` | 跨多面斜切直接用原生 Knife；`vertex.connect` 用于建开放连边，切既有面用 `native.split_face` 或原生 `J`/`K`；跨折面凹槽与翻边分段沿局部方向挤出，不用平均法向整段压入；`form.recess` 逐面沉孔走区域模式或原生内插+挤出 |
| **可换刀体、分件与承座** | 原生 `P` 分离、实时 `Boolean` 修改器 | **刀体、分件与局部返修**：`profile_dimension_create/resize`（圆/槽/多边形尺寸刀体）、`surface_path_cutter`（闭合 3D 轮廓棱柱刀）、`boolean_batch/rebind/slice/consumers`、`shell_extract`、`shell_panel_seat/resize`（缓弧面共边盖板与承座）、`fillet_strip_refit`（重定已应用圆角宽）、`boolean_seam_cleanup` | 换刀或改共用刀前先跑 `boolean_consumers` 核对所有受影响零件；`shell_panel_seat` 适用于无折叠的局部高度场开口，跨折深翻边直接手建四边返边 |
| **修改器栈与阵列回转** | 属性面板原生修改器栈（Mirror / Solidify / Bevel / SubD / Array / Screw / Shrinkwrap / Weighted Normal / Data Transfer） | **Editable Modifier Stack**：`stack_snapshot/add/update/toggle/reorder`、`array_linear`、`array_radial`、`revolve` | 改径向阵列份数时同步更新 Empty 旋转步角（`360° / Count`）；用 `stack_snapshot` 在大改前记录当前栈序与开关状态 |

## 二、三大只读检查入口

日常建模在视口里直接开叠加线框；需要精确定位布线归宿、出同机位多视角对比图或量毫米级穿插与缝宽时，调用插件的三个只读入口（不改动源工程）。

### 1. 面流检查（Face Flow）

追踪真实控制笼（`CONTROL_CAGE`）或实际求值层（`EVALUATED`）上的边循环、边环和连续面带，核对每条线的起点、走向和归宿，并可一键预扫重合分离边、断开顶点扇、极点和间距突变。

- **界面操作**：在 **3D 视图 → N → Hard Surface → 面流检查** 填写部位与共同地标（复刻参考帧时，在 **图像编辑器 → N → Hard Surface → Face Flow Reference** 加载原帧点标）；编辑模式选活动边或面带种子，打开实体线框追踪高亮并核对每处停点。
- **后台离线命令**：
  ```powershell
  & $ctl --jobs-dir $jobs --compact model inspect $targetBlend --expected-sha256 $sha --context $ctxJson --face-flow $flowReqJson
  ```
  读取作业目录下的 `face-flow-report.json`，按预扫给出的候选坐标逐条回到视口核实。

### 2. 多视角检查（Review Grid）

从保存工程按锁定正交机位、中心点与缩放比例成对输出白模、控制笼线框与求值线框拼图，用于阶段自检与白模审看。

- **界面操作**：对象模式 **3D 视图 → N → Hard Surface → 多视角检查**，选择视图组合并导出 PNG。
- **后台离线命令**：
  ```powershell
  & $ctl --jobs-dir $jobs --compact model inspect $targetBlend --expected-sha256 $sha --context $ctxJson --review-grid $gridReqJson
  ```
  核对布线时成对提交 `display = "CONTROL_CAGE"` 与 `display = "EVALUATED_WIRE"`，保持相同的 `views`、`projection = "ORTHO"` 与 `framing`；若整机求值边数超出 100,000 线框预算，按子总成或局部对象分批出图，整机另外保留一张实体白模图。

### 3. 几何与装配审计（Geometry and Fit Audit）

以世界毫米量化单件自交、零件对穿插、间隙测线、通孔净径与壁厚。

- **界面操作**：**3D 视图 → N → Hard Surface → Product Surfaces and Fit → Inspect Geometry and Fit**。
- **后台离线命令**：
  ```powershell
  & $ctl --jobs-dir $jobs --compact model inspect $targetBlend --expected-sha256 $sha --geometry-audit $auditReqJson
  ```
- **四类核心查询与读数要点**：
  - **单件自交与双件相穿（`self_intersection` / `pairs`）**：分别查看 `strict_hits`（严格横穿三角对）和 `coplanar.overlapping_pairs`（共面正面积重叠）；零件超 16 件或配对超 32 组时固定场景拆批运行。
  - **间隙与包容测线（`probes`）**：给定世界毫米起点与方向，读取两闭合件的材料占据区间，查清缝宽与内部包裹侵入；开放壳另辅以剖面和首命中检查；射线贴边报 `ambiguous_boundary` 时加一个小偏移邻站复测。
  - **通孔净径（`BORE`）**：在孔口、腰部、出口取站位并给足角采样分辨率（正多边孔取边数的两倍以覆盖平边弦距），核对实测拟合中心、最小净径与轴向贯通；超射线预算时按站拆批。
  - **实测壁厚（`WALL`）**：给近表面世界毫米测点，沿向内法向读取对面距离，重点抽查折角和最薄残壁。

## 三、批处理与现场面板交接

- **连续工序批处理（`model prepare`）**：把同一目标的一组已确认网格操作（`mesh.flow`）或对象/修改器操作（`hard_surface.stack`）写入一份清单 JSON，一次提交生成一个可冷重开的工作候选，原文件保持完好：
  ```powershell
  & $ctl --jobs-dir $jobs --compact model prepare --file $srcBlend --expected-sha256 $sha --manifest $manifestJson
  ```
- **接手界面已保存的预览（`model panel-controls`）**：用户在界面里配好选区预览并存盘后，先用 `inspect-preview` 读回保存的执行请求，确认工具、参数与保护区后再执行生成新工作副本。完整交接字段先跑 `& $ctl model panel-controls --help` 核对。
- **跨进程选区纪律**：网格拓扑一经拆面、焊点或倒角，旧元素索引即失效；跨步骤或重开工程后按真实几何地标与最新拓扑指纹重新解析选区。
