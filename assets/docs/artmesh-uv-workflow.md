# Live2D ArtMesh UV atlas PSD 验收

`atlas-artmesh` 是面向 Live2D 纹理 atlas 的 PSD 工作流。它把 Cubism Core 提供的每个 drawable 的 UV 三角形裁成一个 PSD 编辑单元，并在 repack 时把发生变化的 RGBA 像素写回原 atlas。

这条路径的目的是验证纹理 atlas 的编辑闭环：导出 PSD、原样 repack、在 PSD 中实际改色、擦除 alpha、增加 Paint 层，再检查 atlas。它不是姿态渲染器，也不以当前姿态的屏幕投影作为覆盖依据。UV 和三角形 index 才是 atlas 覆盖的来源，因此隐藏、透明、很小的 ArtMesh 仍会得到自己的元数据和编辑层。

## 运行真实样本

下面的命令使用仓库已有的嘉然模型、Steam 原始 LPK 和 Steam 中的 Cubism Core。输出目录必须是新的或空目录；命令只读取模型、纹理、MOC3 和 LPK，并在输出目录写入所有 PSD、PNG、JSON。

```powershell
pixi run python scripts/validate_artmesh_acceptance.py `
  --model "runtime/validation/native_live2d/lpk_preview_model_tchsfuhh/live2d/A-Soul嘉然Diana 原创服饰桌宠/model0.json" `
  --lpk "E:\SteamLibrary\steamapps\common\Live2DViewerEX\shared\workshop\2754242023\2754242023.lpk" `
  --cubism-core "E:\SteamLibrary\steamapps\common\Live2DViewerEX\bin\exstudio\exstudio_Data\Plugins\x86_64\Live2DCubismCore.dll" `
  --output "runtime/validation/artmesh_acceptance"
```

不传 `--cubism-core` 时，程序使用应用已有的自动发现路径。真实模型没有 drawable sidecar 时，Cubism Core 会先把 sidecar 写入本次输出目录；原始模型目录不会被补写。

如果已经有一次完整的 `export` 目录，可跳过昂贵的 PSD 导出，只复核 no-op 和真实 PSD 编辑：

```powershell
pixi run python scripts/validate_artmesh_acceptance.py `
  --model "runtime/validation/native_live2d/lpk_preview_model_tchsfuhh/live2d/A-Soul嘉然Diana 原创服饰桌宠/model0.json" `
  --lpk "E:\SteamLibrary\steamapps\common\Live2DViewerEX\shared\workshop\2754242023\2754242023.lpk" `
  --reuse-export "runtime/validation/artmesh_acceptance_final/export" `
  --output "runtime/validation/artmesh_acceptance_revalidated"
```

验收报告位于 `artmesh_acceptance_report.json`。`metrics` 中的字段含义如下：

- `raw_drawables` 是 Cubism Core 读到的 drawable 数量。
- `exported_artmesh_layers` 是实际写入 PSD 的 ArtMesh 层数量；`skipped_drawables` 必须为 0。
- `hidden_drawables` 按 `visible=false` 或 `dynamic_flags` 的最低位为 0 统计。
- `tiny_drawables` 使用固定的 bbox 面积阈值 2048 像素统计，阈值也会写在报告中，便于不同样本复核。
- `shared_uv_regions` 和 `shared_uv_pairwise_pixels` 来自 UV 三角形 mask 的实际相交。覆盖率是相交像素之和除以 atlas 总像素；多个 drawable 同时相交时按 drawable 对计数，不把它误称为连通区域面积。
- `texture_sizes`、`atlas_pixels` 证明验证使用了原始 atlas 尺寸。程序不会为了通过验收缩小纹理。

本次真实样本的报告为 43 个 raw drawable、43 个 atlas-ArtMesh 层、0 个跳过项、7 个 tiny bbox、0 个 hidden drawable、0 个 shared UV region，以及一张 4096×4096 的纹理。`hidden=0` 和 `shared=0` 是这个样本的真实限制，并不表示该路径会跳过这类 drawable；隐藏/tiny 与非零偏移共享区域由合成单测覆盖，真实报告会在遇到它们时列出实际 ID 和像素数。

## 验收内容

脚本先把 `atlas-artmesh` PSD 原样 repack 到 `no_edit` 目录，逐像素比较 RGBA 四个通道，`changed_pixels` 必须为 0。随后它打开刚导出的 PSD，在真实层结构中完成三种编辑：

1. 修改一个 Original 像素的颜色；
2. 将另一个 Original 像素设为 `(0, 0, 0, 0)`，模拟橡皮擦造成的 alpha 擦除；
3. 在同一个 ArtMesh 的 `Paint` 组增加一个新的不透明 overlay 层。

本次 4096×4096 真实复核的 no-op 为 0 个变化；颜色、擦除和新 overlay 恰好改变 3 个像素，其余 16,777,213 个像素 RGBA 完全不变。编辑后的 atlas 必须恰好改变这三个像素；颜色和 overlay 的 RGBA 值必须符合报告中的 `edited.edit.after`，擦除点的 alpha 必须为 0。PSD 对完全透明像素的 RGB 字节没有可见语义，验收会把解码器实际返回的透明 RGB 写入 `actual_changed_rgba`，而不把它误报为失败。其余像素逐字节保持不变；额外改变、alpha 擦除被吞掉、尺寸变化或源文件 hash 变化都会使命令失败。

PSD 使用每个 ArtMesh 的 `Original`、`Paint`、`AI_Edit` 锚点组。验收实际改动 PSD 层并经由同一 repacker 回写，因此只构造一个导出 PNG 或只在 atlas 外部改图，不能替代这项测试。

## 为什么不用姿态 PSD 或连通域

姿态 PSD 把 drawable 的顶点投影到当前显示姿态；同一 ArtMesh 在另一姿态下可能移动、缩放或变形，不能作为固定 atlas 像素区域的覆盖证明。`atlas-artmesh` 只使用 UV 与 index 在原纹理尺寸上栅格化，所以编辑位置与模型姿态无关。

连通域只能从当前 atlas 的颜色或 alpha 反推出若干不透明色块。纹理可能有透明边界、相邻部件共用 texel、隐藏部件或只有很小 UV 三角形；这些信息无法可靠地从连通域恢复。Cubism Core 的 drawable 列表和 UV 三角形保留了模型的真实绑定关系，故报告会单独列出 hidden、tiny 和 shared UV，而不会把它们合并或静默丢弃。

## 输出与资源注意

本次最终报告在 `runtime/validation/artmesh_acceptance_final/artmesh_acceptance_report.json`，原始 LPK、模型 JSON、MOC3 和纹理前后 SHA-256 均一致。首次完整流程在最终断言中要求擦除点的透明 RGB 也为零，因 PSD 合成器返回白色 RGB、零 alpha 而失败。修正擦除断言后，重新读取已有回写产物验证通过，未重新导出；报告明确标记 `revalidated-existing-artifacts`，原失败记录另行保留。无编辑往返和所有未修改像素仍严格比较 RGBA 四个通道。

输出目录包含导出的 PSD、`.lpkpsd.json` 元数据、不可变 layer baseline、无编辑 repack、实际编辑后的 PSD、编辑 repack 和验收报告。真实 4096×4096 atlas 的 PSD 可能占用较多内存并需要数分钟；这是保留原分辨率和逐层 baseline 的代价。`runtime/validation/` 已被忽略，验收产物不应提交到 Git。

