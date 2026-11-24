# 过程检查、改型与返修

硬表面建模最怕一路闷头做到收尾才发现早期母面塌了、接缝歪了。把检查拆成「过程随手看线框、阶段扫条光做改型、送审前跑商业红线」三道轻快动作，问题冒头当场消化。

## 一、过程内自检标准

### 1. 原创建模与教程复刻的分层对照

- **原创或看参考图制作**：聚焦四件事——多视角轮廓比例吻合参考单、零件拆分符合制造与装配逻辑、控制笼全四边面流顺滑无波纹、局部改型不塌面不连累邻件。
- **复刻既有教程或拆解实物**：在满足上述四项的同时，按对应工序的原画面逐段核对关键造型环、孔口边、卡线与翻边的起止位置和连接方式；改变关键接法前先说明原因与对改型的影响。

### 2. 过程看视口线框，收尾再渲染

- **实体叠加线框常开**：建模过程中始终在 3D 视口开启实体（Solid）叠加 Wireframe；查源笼进入编辑模式看 L0，查实际修改器结果在对象模式关闭 `Optimal Display`、隔离目标件并放大局部。
- **离线同机位多视角图**：检查已保存 `.blend` 时，用侧栏 **Hard Surface → 多视角检查** 或公共 `model inspect --review-grid`，成对输出同一机位、同一正交比例下的 `CONTROL_CAGE` 与 `EVALUATED_WIRE` 拼图；整件高密求值边数超出 100,000 预算时按总成或局部拆批输出。
- **两向条形高光（Zebra / Strip MatCap）**：在 L1 和 L2 细分下分别扫横、纵两个方向的条形反射（或高反差车漆 MatCap），把顺滑主面上的凹坑、极点夹痕、渐消尾突和跨缝折光一眼揪出来。正式材质与光影渲染留到验收末端进行。
- **源笼、视口档与渲染档分层核对**：打开保存栈核对 `Subdivision` 的 `levels` 与 `render_levels` 一致，避免视口看着平整、渲染级高一档后在窄缝或翻边处穿模。

### 3. 流形与几何装配检查

在 L0 源笼和完整求值层分别确认：

- **流形与绕序**：闭合实体每条边恰连两张面、面法线一致朝外、无三面共边、无内部夹面与重合分离边；有意开放的薄片（如单层外皮阶段件）保持边界连续、顶点面扇连贯。
- **自交与跨件配合**：用 **Hard Surface → 几何与装配审计**（或 `model inspect --geometry-audit`）查单件严格三角自交、指定零件对的交叉与共面重叠、通孔沿轴净径（`BORE`）和实测壁厚（`WALL`）；活动件在起点、终点和最窄中段分别检查。

## 二、白模局部改型验证

白模交付前，在工作副本上针对每个主要结构类型实做一次小改型，验证这份控制笼真正「好改、改不坏」：

1. **挑一个真实规格尺寸**：如扩大一个孔径、拉长一截滑槽、微调一道分缝位置或收紧一段圆角卡线。
2. **只动规划内的边环或控制参数**：调整后同机位对比 L0 线框、L2 条形高光与关键截面，确认目标面带平顺跟随。
3. **核对保护区与邻件**：确认固定地标、相邻分件的缝宽净空和非目标曲面保持原状；确认后记录改型对照图，交付工程保持定稿尺寸。

## 三、保存点位与精准回退

每条活动工作线只维护三个清晰点位，保持目录轻便：

1. **只读源文件**：上游交来或上阶段锁定的母版，算好 SHA-256，原样留作对照。
2. **当前工作副本**：日常编辑与多步 `model prepare` 批处理的唯一工作文件。
3. **最近通过的结构锚点**：每次做破坏性大改动（应用修改器、大改母面、进入新阶段）前，确认当前工作副本能独立冷重开并通过本阶段检查后另存为锚点；只有新里程碑通过检查再推进锚点。

### 出错时的分级返修与双脚本回退

发现高光折痕或穿插时，按 **连接拓扑 → 截面走势 → 支撑卡线距 → 修改器栈序** 由里向外排查。小面积错接优先在单对象编辑模式清理选区、关闭细分重选修边；遇到整段母面塌陷或连环错位，退回到首次出错前的最近结构锚点重做该段。

使用公共 CLI 跑连续工序或精确调点出错时，可用本技能自带的两个只读规划脚本生成安全回退请求，再交给 `blenderctl` 执行：

- **按工序回退（`scripts/plan_step_rewind.py`）**：把一次多步 `model.prepare` 作业回退到第 `N` 步（从 0 起算）之前，从父锚点干净重放前半段。
  ```powershell
  $ctl     = './blenderctl.ps1'
  $blender = (& $ctl discover --summary | ConvertFrom-Json).data.selected_blender.file
  $py      = (Get-ChildItem -Path (Split-Path $blender -Parent) -Recurse -Filter 'python.exe' | Select-Object -First 1).FullName
  $script  = './.agents/skills/blender-hard-surface-modeling\scripts\plan_step_rewind.py'

  & $py -X utf8 $script $jobDir
  & $py -X utf8 $script $jobDir --before-index 2 --current $currentBlend --output $rewindReqJson
  & $ctl --jobs-dir $newJobsDir request $rewindReqJson
  ```
- **按指定顶点回退（`scripts/plan_vertex_rewind.py`）**：后续已经做了其他手调、只想撤回某次单步 `mesh.positions` 作业里的几个点时，用三方坐标比对生成反向坐标请求，自动保留后来的其他手调，遇冲突点自动停下提示。
  ```powershell
  $script = './.agents/skills/blender-hard-surface-modeling\scripts\plan_vertex_rewind.py'
  & $blender --background --factory-startup --disable-autoexec --python-exit-code 1 `
      --python $script -- --job $jobDir --current $currentBlend --output $vertexRewindJson --indices 0
  ```
  当返回 `request_created` 且退出码为 0 时，把 `$vertexRewindJson` 交给 `& $ctl --jobs-dir $newJobsDir request $vertexRewindJson` 执行并冷开复核。

涉及已发布文件覆盖或资产库事务回滚，统一按 [blender-cli](https://github.com/tsist/blenderctl/blob/main/skills/blender-cli/SKILL.md) 走 `transaction rollback`。

## 四、送审前的 G1–G4 自检清单

准备提交 [blender-modeling-review](https://github.com/tsist/blender-modeling-review/blob/main/SKILL.md) 独立盲审和用户审看①之前，对着下面四道门逐项过一遍，省去来回打回：

```powershell
$ctl    = './blenderctl.ps1'
$blend  = (& $ctl discover --summary | ConvertFrom-Json).data.selected_blender.file
$probe  = './.agents/skills/blender-modeling-review\scripts\commercial_qa_scorecard.py'

& $ctl --jobs-dir $jobs --compact model inspect $targetBlend --expected-sha256 $sha --qa-profile render
& $blend -b --factory-startup -P $probe -- probe --blend $targetBlend --stage white_model --target-level L2 --output "$batch\commercial-probe.json"
```

- **G1 工程生死门**：新进程冷重开零报错；无负缩放、无非有限坐标；参考图已打包或路径干净；`REF_` 与 `CUTTER_` 集合内对象全部关掉 `hide_render`；细分修改器 `levels` 与 `render_levels` 一致。
- **G2 拓扑与大纲**：公制 `1 unit = 1 m`、正面朝 `-Y`、底面原点落零、静态网格缩放归 `(1,1,1)`；全部件归入单一根集合、清零 `Cube.001` 默认名与零引用孤立数据块；全四边面零 N-gon、可见曲面零 ≥6 边星形极点、零非流形与零面积退化面。
- **G3 造型还原**：正、侧、背、顶轮廓与关键尺寸、分缝、开口位置全部对上参考单和 `plan.md`。
- **G4 法线与改型**：外表面法线一致朝外，修改器与法线栈符合黄金顺序，横纵条形高光顺滑无夹捏，活动件全行程无穿插，至少完成一次局部改型复验。
