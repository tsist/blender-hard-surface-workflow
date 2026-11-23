# 硬表面工具使用规范

命令实例需要匹配的工作区版 Blender CLI 0.91.0。可先使用 Blender 5.3.0 原生操作和已安装插件的面板。

使用 Windows Blender **5.3.0**、Hard Surface Workflow **0.3.5** 与公共 CLI **0.91.0**。按具体工序查询工具。每项指南列出适用输入、关键配置和实测验收方式。

| 任务 | 操作指南 |
| --- | --- |
| 编辑源笼，选方向、滑环、拆面、倒角、开孔、返边、修形 | [62 项网格工具逐项规则](tool-mesh-guide.md) |
| 组织母面、工作平面、刀体、分件、阵列和修改器栈 | [27 项对象工具及 16 类原生修改器](tool-object-guide.md) |
| 确认选区、量孔壁及缝宽、查装配穿插、出同机位证据 | [选区、测量、图证与回退](tool-inspection-guide.md) |
| 中断恢复、局部回退、候选与磁盘预算 | [保存与精准回退](save-and-rewind.md) |
| 现有案例的实际失效、复现入口、修正或当前边界 | 实证中的工具问题记录 |

## 从目标倒推工具

先画出母面、宽跨距与接收边。用原生编辑工具建立可解释的面流。每条卡线只承担一个明确的折角、口沿或短过渡。环切以实际经过的面带和最终高光验收。独立接口先定义配合与测量方向，再使用布尔、提取、承座或给厚。开放曲壳上的局部孔槽先安排边流、墙脚和邻件的可用空间。带闭合重复的零件分别核验源扇区可编辑性、周期合缝与最终绘制成本。[源笼与接线](mesh-flow.md) · [修改器原理与次序](modifier-basics.md) · [组合实证](topology-modifier-casework.md)

## 发现和交接

```powershell
& '<配套工作区中的 blenderctl.ps1>' --blender-version 5.3.0 model flow-tools
& '<配套工作区中的 blenderctl.ps1>' --blender-version 5.3.0 model flow-tools --tool native.loop_cut_slide
& '<配套工作区中的 blenderctl.ps1>' --blender-version 5.3.0 model flow-tools --geometry-audit
```

GUI 的网格编辑在 **3D 视图 → N → Hard Surface**。磁盘入口在 `model inspect` 完成只读检查，在 `model prepare` 将连续工序写成一个候选。已保存的面板预览用 `model panel-controls`，在请求中显式指定 `"edition": "0.3.5"`。交接记录实际使用的选区、保护区、外部参照、源 SHA 和运行版本。拓扑变化后按几何地标重新取种子和路径。

## 历史取证

逐面沉位深度、活动点身份、阵列节距失效、窄颈内插和圆化尺寸误读均有已保存的反例与版本说明。按报告里的版本重查历史结论，当前动作的规则以本页对应指南和运行回执为准。
