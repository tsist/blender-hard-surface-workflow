# 网格工具操作指南

适用于 Hard Surface Workflow **0.3.5**、公共 CLI **0.91.0** 和 Blender **5.3.0**。本页覆盖工作台目录中 62 项 `mesh_tools`。修改器、快照和求值测量见[工具工作台](tool-workbench.md)。

```powershell
& './blenderctl.ps1' --blender-version 5.3.0 model flow-tools --tool native.split_face
```

在工作台执行时，进入**单一目标网格的编辑模式**，在源笼上使用工具规定的点、边或面域。先预览选择及保护区，从侧视确认隐藏侧或镜像显示。批量可用 `model prepare` 在工作副本中提交 `mesh.flow`。用 `model panel-controls` 重放已保存的面板请求时写 `"edition": "0.3.5"`。数组里的索引均来自当前源笼。拓扑指纹可检查索引所对应的连接结构，同一指纹下点还可能移动。重开或拓扑变化后按实体地标复核选区和参数点。

`protect` 接受一个或一组选择器。默认 `allow_expansion=false` 时，选区外点的保留和位置受保护，拓扑工具还检查选区外面的连接与绕序。允许工具经邻环扩展前，另圈定必保的点、边和面。属性命令和只选取命令各有相应检查。下文除标注世界坐标或角度外，长度采用对象局部 Blender 单位。对象有非均匀缩放时，局部圆或等距需要在成品坐标中再量一遍。`入口和版本` · [选取步骤](mesh-flow.md) · `完整参数目录`

## 选取

| 工具 ID | 输入、参数与验收 |
| --- | --- |
| `native.select_loop` | 边模式选可辨的种子边。`delimit_edge_loop` 可组合 `SEAM`、`SHARP`、`NGONS`、`INNER_CORNERS`、`OUTER_CORNERS`。检查新增高亮及每条链的终点。0.3.5 工作台执行器可能在操作后重新套用旧预览。本版在 GUI 优先使用原生环选或「硬表面选取」菜单，并在编辑模式查实际选区。`复现` |

## 对齐与保形

| 工具 ID | 输入、参数与验收 |
| --- | --- |
| `align.axis` | 选点、边或面，将所含顶点向局部 `X/Y/Z` 的 `MIN/MAX/CENTER` 对齐。`offset` 加到目标坐标，`factor` 混合。量选点目标坐标，查看邻面朝向和面积。会拒绝导致相邻面翻向或退化的压齐。 |
| `align.plane` | 为面区选 `source=FACES`，散置采样点用 `BEST_FIT`，`AUTO` 按有效选区推断。`offset` 沿拟合面法向，`factor` 为投影混合。复测平面残差、原面朝向和局部折线。有方向歧义时，改用带世界原点和法向的精确平面。 |
| `align.reference` | 选要整体移动的点、边或面。`source_world` 与 `target_world` 给世界基准，`POINT` 对点，`PLANE` 以 `target_direction` 为法向并可加 `plane_offset`，`EDGE` 再给 `target_end_world` 并投到有限线段，`DIRECTION` 沿指定方向投影位移。前四种只平移。`FRAME` 补齐 `source_normal`、`source_tangent`、目标法向 `target_direction` 和 `target_tangent`，执行刚体旋转和平移。复核世界基准、刚体边长和坐标存储误差（`1e-5 BU`），同时排除退化方向或无效框架。180° 可以是合法旋转。 |
| `mesh.snap_symmetry` | 选接收侧和希望吸附的中线点。`direction` 指固定源侧，例如 `-X`，从该侧可见点找镜像对应。`threshold` 控制候选距离，`use_center` 和 `center_tolerance` 控制中线，`factor` 混合。查 `unmatched_indices` 和 `ambiguous_indices`。同一点有多个候选或多个点争用一个源点都列为歧义。最后量镜像点对距离。 |
| `native.flatten` | 选至少可定义平面的点、边或面，用 `BEST_FIT` 或 `NORMAL`，以 `factor` 混合，`lock` 依次冻结局部 X、Y、Z。量目标平面的残差。有锁轴或部分混合时，剩余残差可能来自约束本身。 |
| `looptools.flatten` | 选同一待压平区域，`plane=best_fit/normal/view`；用 `influence` 和 `lock_x/y/z` 控制。`view` 和视图坐标系锁轴需要真实 `VIEW_3D`。内置 LoopTools 4.7.7 的 `none` 与 `bounding_box` 采用同一投影，需另查极值及边界。 |

## 滑环、圆化、理线与平滑

| 工具 ID | 输入、参数与验收 |
| --- | --- |
| `native.edge_slide` | 边模式选有可追踪邻边的环或链，`value` 控制滑移方向与幅度。`use_even`、`flipped` 决定均匀参考侧，`single_side`、`use_clamp` 决定单侧与限位。核站位、跨边、相邻面和细分高光。公共工具关闭 `correct_uv`，带纹理的网格同时查看 UV 拉伸。 |
| `native.circularize` | 选附在真实面上的点环或边环。`fit_method=LEAST_SQUARE/CONTRACT`，可用 `use_custom_radius` 与 `custom_radius`；`angle` 以弧度旋转，`flatten` 和 `factor` 为数值混合，另可设 `regular` 及局部 `lock`。量圆心、半径、点距和离面高度。孤立点不满足原生求解条件。 |
| `looptools.circle` | 对圆口优先实选完整闭环；点、边、面域均可输入。`fit=best/inside`，定半径用 `custom_radius=true` 和 `radius`，`flatten` 为布尔，`angle` 为弧度。看半径、相位和边长。孤立选点可能扩到邻面，局部窄壁需核实际影响区。 |
| `native.space_evenly` | 选要重排的真实边链或边环，`interpolation=LINEAR/CUBIC`，`factor` 控制混合，`lock` 依次锁局部 X、Y、Z。查点距、端点、轮廓和高光。曲线采样与约束可改变局部截面，尖角和换向地标应单独保住。 |
| `looptools.space` | 选目标边链或边环以明确走向。`input=selected/all`，`interpolation=linear/cubic`，可设 `influence` 与锁轴。复查端点、边长和实际移动范围。`all` 可能作用到平行环；遇默认保护拒绝，先确认这些环在计划内。 |
| `edgeflow.set_linear` | 边模式选至少两条有面支撑、无分支的开放边链。`mix` 混合，`space_evenly` 使点沿端点连线等距。量到直线的距离和实际端点，检查邻面长宽。闭环或散边应改用相应工作流。 |
| `edgeflow.set_curve` | 边模式选至少两条有面支撑、无分支的开放链。`tension` 和 `mix` 管拟合，`use_rail` 按端部边作导轨，`rail_mode=ABSOLUTE/FACTOR` 配 `rail_start/end`。看端点、端部切向和条光，检查导轨是否选到了所需邻边。 |
| `edgeflow.set_flow` | 选两侧均有四边面的目标边。`tension`、`iterations`、`mix` 控制拟合，`min_angle` 是度数，端部 `blend_mode=ABSOLUTE/FACTOR` 分别填 `blend_start/end_int` 或 `blend_start/end_float`，`blend_type` 选 `LINEAR/SMOOTH`。比较截面和两级细分，留意孔径、厚度与支撑线距离。三角、边界或极点附近优先拆开处理。 |
| `edgeflow.set_vertex_curve` | 点模式选择锚点，`anchors` 按顺序给端点和可选导向点。`path` 显式给 3–512 个不重复、逐边相接的点，空路径只接受唯一最短路。`use_topology_distance` 可按边数找路；路径超出选区需顶层 `allow_expansion=true`。`flip/rotate` 只用于双锚点圆弧。复核路径、固定端点、切向和邻面，保护内部点时工具会拒绝。 |
| `looptools.curve` | 选路径上的引导点并观察其间实际要移动的网格点。`boundaries`、`interpolation`、`regular`、`restriction=none/extrude/indent`、`influence` 与锁轴共同决定结果。只在 `boundaries=true`、满影响、`regular=true` 及兼容开放路径等条件下走原生曲线。核两端、实际移点和选区边界，工具可能涉及未选中间点。 |
| `looptools.relax` | 选择已有正确走向的一段链或环。`iterations` 是字符串 `"1"/"3"/"5"/"10"/"25"`，另有 `input`、`interpolation`、`regular`。当前由内置兼容实现执行；`input=all` 可能带动平行环。从少量迭代开始，对比轮廓、圆口、板厚和条光。 |
| `native.smooth` | 选需缓和的点或所含顶点的边、面。`factor`、`repeat` 控制程度，`xaxis/yaxis/zaxis` 选择局部轴。比较截面与端点，检查缩小和边缘漂移。应保护安装面和折根。 |
| `native.smooth_normal` | 选需要沿法向缓和的局部点、边或面。用 `factor` 和 `repeat`，复测表面高度、最薄截面与相邻折线。尖折、薄壁或法向混乱处先小范围试一次。 |

## 引导线与单帧清理

| 工具 ID | 输入、参数与验收 |
| --- | --- |
| `looptools.gstretch` | 对一条已选开放链，可沿其首尾连线拉伸。需指定路线时给 `strokes` 或 `guide` 之一。显式笔划为开放 XYZ 折线，`stroke_space=WORLD/LOCAL`。具名项形如 `{"source":"annotation","name":"Guide","layer":"Line","frame":1}`，也可用 `grease_pencil`；严格按此帧读。 | 在所需坐标系锁定应保护的轴。0.3.5 的 5.3.0 短链小样用 `regular`、锁 Y/Z 实测 X 四段等距，Y/Z、邻线和面连接保持。已有选中链按 `method=project/irregular/regular` 求位置，空选区有引导时才按 `conversion` 生成新边。查轨迹、端点和段数。`delete_strokes` 固定为 `false`。具名 Annotation 须为 3D，带父级的 Grease Pencil 图层应改用显式世界折线。 `实际短链复验` |
| `looptools.remove_annotation` | 以辅助网格建立公共 `mesh.flow` 上下文，按 Annotation 数据块名 `name`、精确 `layer/frame` 和现有 `expected_stroke_count` 定位。获得清理授权后填 `confirm=true`。图层须解锁、数据须未共享。读回该帧笔划数，并抽查相邻帧。 |
| `looptools.remove_gpencil` | 同样使用辅助网格上下文，`name` 填当前场景的 Grease Pencil **对象名**。给准确图层、帧和现有笔划数，获授权后 `confirm=true`。图层须解锁，数据块和 Drawing 的共享都会阻止清理。读回目标帧和邻帧，确认仅清理指定 Drawing。 |

## 切面、内插、卡线及翻边

| 工具 ID | 输入、参数与验收 |
| --- | --- |
| `vertex.connect` | 点模式按路径依次提供点。`close=false` 建开放散边，`true` 在近共面的闭合路径上造面，`tolerance` 为平面容差。查连边顺序、面绕序和是否重叠原面。需要切分既有平面时用 `native.split_face` 或原生 J/K。 |
| `native.split_face` | 面模式只选**一张凸平面多边形**，`vertices` 给同面两个互不相邻的源点。结果应沿直线产生两个子面，新刀边恰由两面共享。检查面朝向及旧周界。凹面、非平面、跨多个面或没有两个既有端点时回到原生 Knife。 |
| `native.loop_cut_slide` | 选一张种子四边面，`direction_vertices=[a,b]` 指向该面的一条边，`factor` 从 a 到 b 量站位。单次调用沿整条四边面带走，无需设置 `allow_expansion`。核贯穿路径、新环站位和两侧截面。隐藏面、三角或非流形连接、与显式保护冲突都会阻断。 |
| `native.subdivide` | 选要局部加密的边或面，`number_cuts` 为每边增切数，`smoothness` 控新增点，`quadcorner` 与 `ngon` 管四边面切角及 N 边面。核面型、相邻面是否被改接、关键原点位置。只需一条贯通支撑环时优先核对 Loop Cut 路径。 |
| `native.inset` | 选面区，`thickness` 设边宽、`depth` 设法向高差；`use_individual` 区分逐面与区域，`use_edge_rail` 用于区域内插。可配 `use_even_offset`、`use_boundary`、`use_outset`、`use_relative_offset`。 | 需要内插区接到镜像中线时，原半笼的中线边须属于真实开边，再试关闭 `use_boundary`。同一 3 mm 小样关闭时内插选面到 X=0，开启时退至 X=3 mm，完整镜像多两面。检查斜视、净宽、接线和完整栈，跨窄面时查自交。 `开边内插实测` |
| `support.face` | 选需加卡线的连通面区或明确的逐面选区。`width` 是每圈局部内插距离，`count` 为圈数，`individual` 决定逐面。读一圈实际宽度，看转角和细分反光。连续内插在窄角可能挤压或折回。 |
| `support.edge` | 边模式选实际承托折线，`width` 定局部偏移，`clamp_overlap` 控重叠。重点比对倒角两侧、交汇的三条折线及求值轮廓。开启限宽后应量实际宽度。 |
| `native.bevel` | 选折边或角点，`affect` 省略时公共核心按点或边域推断；`offset_type` 定义宽度，`PERCENT` 使用 `offset_pct`，其他长度型用 `offset`。再设 `segments`、`profile`、内外角接法及法线、缝或锐边选项。量受 `clamp_overlap` 限制后的实际宽度，看三角会合、面序与阴影。 |
| `edit_mesh_tools.offset_edges` | 选方向一致的开放边链或闭边界，`geometry_mode=offset/extrude/move`，`width/depth` 为有符号局部距离，`follow_face`、`edge_rail` 和 `edge_rail_only_end` 决定方向和端点，按需要核对 `mirror_modifier` 与 `threshold`。查自由边、接缝、拐角自交以及每个截面的实际偏移。 |
| `native.flange` | 选一条自由开放边链，每条边恰连一张可见原面。`start_vertex` 取链的一端，`offsets` 填扁平数组 `[dx0,dy0,dz0,dx1,dy1,dz1,…]`，每站一个非零局部位移。逐站确认面带共用原边与横边，并追踪剩余自由边。检查错位、自交及新面的 UV。 |

量产车外板倒角先核对象的非均匀缩放，再按世界毫米量有效偏移。Model Y 的 90° 返边小样用 `OFFSET`、四段和 3 mm，单位缩放时两向均为 3 mm。对象缩放 `(2, 0.5, 1)` 时，同一输入得到 6 和 1.5 mm。返边只留 1.8 mm 时，开启 `clamp_overlap` 后回执仍为 `FINISHED`，实际两向均约 1.8 mm。把偏移、缝净宽和板厚分开测，在尖角和三件交汇另取截面。 `Blender 5.3.0 四段倒角实测`

## 补片、配对、开孔与生成

| 工具 ID | 输入、参数与验收 |
| --- | --- |
| `native.grid_fill` | 选单个可配对的空洞边界，设 `span`（0 为自动）、`offset` 决定排格，`use_interp_simple` 为简单插值。追两族四边线、角点配位和剖面，查是否翻面。奇数周界、内孔或对边分段失配时先规划分区。 |
| `native.bridge` | 选待接的两条边链或两个边环。调整 `twist_offset`、`number_cuts`、插值与中间截面。需要角点约束时用 `corner_vertices` 给两源点，两链须等边数且同为开链或闭环，`type=SINGLE`、`use_merge=false`。此约束会读回实际共边链，仍须查源点配相、绕向和扭转。 |
| `looptools.bridge` | 选成对边环或清晰面区周边。`mode=basic/shortest`，配 `segments`（0 自动）、`interpolation`、`twist`、`reverse`。`remove_faces` 可能删除桥接后内部面。查两端配对和面序，异段数时量过渡单元。 |
| `looptools.loft` | 选多个有明确次序的截面环。`loft_loop` 可连末环与首环，`segments`、`interpolation`、`cubic_strength` 控段间造型。测各截面相位和实际连接顺序；`remove_faces` 会删除内部面，接口内皮须检查。 |
| `patch.quad_grid` | 只选允许替换的单个盘形面区，`corners` 是四个源周界角点。对边段数应相等，边界固定，单次执行。查边界坐标、网格走向和接近边缘的法线。目标区有 UV、自定义法线或其他自定义属性、内部权重/缝/锐边、混合材质或平滑状态时会拒绝。 |
| `form.hole` | 选平面且周界有余量的目标面。`center_mode=VERTEX_MEDIAN/AREA_CENTROID/EXPLICIT`，显式 `center_local` 仅单面。`radius_mode=CLEAR_RADIUS` 按孔缘边段净空，`VERTEX_RADIUS` 量到顶点；设 `segments` 与 `tolerance`。量最小净孔、环带余量、绕序和实际穿通。环带可能产生多边面，细分用另查转接高光。 |
| `form.lathe` | 边模式选截面轮廓，`axis` 为局部轴，`center=ORIGIN/CURSOR/SELECTION`，`angle` 用度，`steps` 控密度。截面保留，整圈接缝需查焊接，端口需另外验。轴上点会被拒绝，截面错序或回转自交须先修正。 |
| `form.push_pull` | 选面区，`distance` 正值沿面法线，`individual` 决定逐面。检查新底和真实壁深，观察原位置是否有残盖、侧壁是否重叠。需要保住相邻薄壳时以剖面确认材料余量。 |
| `form.recess` | 选要压入的面区，`thickness` 为内插宽度，`depth` 为下沉，`mode=RECESS/COUNTERSINK`。区域模式用平均法向，`individual=true` 按每个原面法向。用回执 `requested_depth`、`measured_depth_min/max` 和实剖核实际壁深、底厚及角。旧 0.3.2 的逐面沉台深度结果应以当前版本重新测量。 |
| `array.linear` | 选要实际复制的源点、边或面，`axis` 指局部轴，`distance` 是相邻项有符号间距，`count` 含原件。核数量、每段实距、占用空间与新旧面是否叠合。后续需频繁编辑间距时可从修改器入口处理。 |
| `array.radial` | 选要复制的网格元素，`axis` 与 `center=ORIGIN/CURSOR/SELECTION` 定轴，`angle` 是度数，`count` 含原件。整圈角步 `360/count`，部分弧为 `angle/(count-1)`。查零点、尾端对应和 360° 接合处重叠。 |

## 焊接、减线与改接

| 工具 ID | 输入、参数与验收 |
| --- | --- |
| `vertex.merge` | 选确实要焊成一个点的顶点。`mode=CENTER` 到选区中心，`ACTIVE` 到有序索引末个活动点。查存活位置、邻面连接和该点权重。合并后仍须判断是否产生细长面或重叠边。 |
| `native.merge_by_distance` | 选接缝候选顶点，或用边、面选区圈定其所含顶点。`threshold` 按局部距离，`use_centroid` 控幸存位置，`use_sharp_edge_from_normals` 可利用法线差生成锐边。报合并数并查两层薄壳是否误焊、缝是否闭合。 |
| `native.dissolve_degenerate` | 选含短边、退化面的局部点、边或面区，`threshold` 按局部长度。查重复点、面积和邻边连通。阈值接近真实小倒角尺寸时先缩小选区和容差。 |
| `native.dissolve_edges` | 只选计划删去并合并相邻面的边。`angle_threshold` 用弧度，按需设 `use_preserve_quads`、`use_face_split`、`use_verts`。复核残余面序、边界、转折与细分轮廓。 |
| `native.dissolve_faces` | 选要并成较大面的面片，`use_verts` 决定是否清中间点。查外周界、生成 N 边面的平面性、隐藏折线和求值三角剖分。 |
| `native.dissolve_limited` | 选可减线的局部点、边或面，`angle_limit` 用弧度，`delimit` 可选 `NORMAL/MATERIAL/SEAM/SHARP/UV`。需要动边界才用 `use_dissolve_boundaries`。比较删线前后的接口、属性界线及截面。 |
| `native.dissolve_verts` | 点模式选冗余点。`use_boundary_tear` 允许拆开边界，`use_face_split` 允许拆面。确认哪些边接续、哪些面重组，量原有界线和开口是否保形。 |
| `native.rotate_edge` | 选内部共享边，`use_ccw` 改方向。检查新对角线、共享面与局部极点的去向。扭曲四边区域应从侧视检查新面折向和高光。 |
| `native.triangulate_faces` | 选要定剖分的面。四边面用 `quad_method` 选固定或几何方向，N 边面用 `ngon_method=BEAUTY/CLIP`。核对对角线、面朝向及剖面，特别留意非平面面片。 |
| `native.tris_to_quads` | 选预计能成对的相邻三角。设 `face_threshold/shape_threshold`（弧度）、`topology_influence`，并按需求显式启用 `materials/seam/sharp/uvs/vcols`。查接合对、残留三角和属性界线，深折或不成对处保留局部重布线。 |

## 绕序与网格属性

| 工具 ID | 输入、参数与验收 |
| --- | --- |
| `native.edge_bevel_weight` | 选倒角修改器应读取的边。`value` 是 `0–1`，0 清除。回读 `bevel_weight_edge`，并在修改器用 Weight 限制时观察求值轮廓。W975 冷开将同一组选边改成仅有 Mark Sharp 后，倒角求值面数回到源笼值。用实际面数、真线框和局部条光确认作用范围。 `边权对照` |
| `native.edge_crease` | 选细分应加折痕的边。`value` 是 `0–1`，0 清除。回读 `crease_edge`，核细分级别下角、端点和高光。 |
| `native.flip_normals` | 选方向需要反转的面。查选中面绕序、邻面相反共享边方向及其内外可见面。 |
| `native.recalc_normals` | 选要统一朝向的一组面，`inside` 指朝内。闭壳查面朝向和剖面；开壳、断片或混合组件还需按成品的正面方向逐段核对。 |
| `native.mark_seam` | 选 UV 分界边，`clear=true` 清现有缝。回读边缝标志，若目标是 UV 岛，再对实际展开结果复核。 |
| `native.mark_sharp` | 选着色断开处的边，`clear=true` 清标记。回读锐边标志，在当前平滑/自定义法线设置下看成品阴影。 |
| `native.shade_flat` | 选应逐面着色的面，回读平滑标记并观察面界高光。效果还受网格绕序和求值链影响。 |
| `native.shade_smooth` | 选应平滑着色的面，回读标记。看锐边和大平面阴影是否符合设计，随后用真实材质和光照核查接缝。 |

## 本地复验入口和案例

- 0.3.5 的 Blender 5.3.0 冷开回执覆盖安装包、拆面和 GStretch。本页其它工具应按实际选区、回执和成品剖面核查。`本版复验`
- 上一版 0.3.4 在 Blender 5.3.0 的拓扑、源选择器、精度工具记录可用来重建试件。`拓扑结果` · `CLI 冷开` · `定位复验`
- 以源笼、实际细分和截面一起判断可编辑性。下面的案例给出网格和对照图。`基础连接` · `偏位台肩` · `单环插线` · `分段翻边` · `翻边厚度` · `减线`
- LoopTools、引导线的清理范围、孔与沉台的已有试件可用于构造自己的失败样本。部分为旧版历史记录。`LoopTools` · `引导线` · `成形工具` · `交接流程`
