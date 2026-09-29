# Spine 编辑器与图集能力：调研结论

核查日期：2026-09-29。结论：已有可用 Spine 编辑器时，角色编辑优先交给编辑器。本程序已移除独立的 Spine 图集工作台入口；`app/core/spine_atlas.py` 的解析、区域导出和副本回写能力继续保留，供后续编辑器与图像工作流复用。

## 官方支持的流程

1. 使用 **Texture Unpacker** 拆开 `.atlas` 与 PNG 页面。
2. 使用 **Import Data** 导入真实骨骼 `.json` / `.skel`，而不是 Live2DViewerEX 的 `model0.json` 描述文件。
3. 设置 Images 路径指向拆出的图片，保存 `.spine` 工程，再编辑和重新导出。

官方拆图会恢复旋转与裁边留白；PMA 需正确解除预乘。导入骨骼应使用对应版本，运行时文件缺失的编辑器信息不能凭空恢复。来源：[官方导入教程](https://esotericsoftware.com/blog/Importing-skeleton-data)、[Texture Unpacker](https://en.esotericsoftware.com/spine-texture-packer#Texture-Unpacker)。

本机 `D:/Programs/Spine pro 3.8.75/Spine.com --help` 已确认提供 `--import` 和 `--unpack` 参数；本次只核对帮助，没有自动操作编辑器或声称编辑器导入已验收。

## 与本程序的差别

| 需求 | 更合适的入口 |
| --- | --- |
| 修改骨骼、网格、动画、皮肤，看到完整角色 | Spine 编辑器 |
| 拆出 atlas 部件图片 | 优先官方 Texture Unpacker |
| 保留原页尺寸、region 坐标与骨骼，只改 PNG 像素 | 复用本程序的 `app/core/spine_atlas.py` 能力；当前没有独立导航页 |
| 不使用编辑器，批量 PSD 分层编辑及差异回填 | 由后续图像工作流接入共享 core；当前没有独立图集工作台 |

“Spine 已有骨骼与 attachment 映射”不等于“图集 PNG 本身就是可直接编辑的完整角色工程”。重新打包也不保证维持原页面布局。以上入口选择是对本项目用途的判断。

多边形打包尤其需要注意：官方 CLI 支持以项目网格为上下文清理轮廓外像素；缺少项目上下文时，拆出的矩形图片可能包含相邻图片片段。不能把只按 atlas 矩形裁切理解为完整无损还原。[官方 CLI](https://en.esotericsoftware.com/spine-command-line-interface#Unpack)

后续若整合编辑器，建议独立提供“发送到 Spine”流程，检测编辑器路径与版本，先拆图再导入骨骼，并复用保留的 atlas core；不把跨版本转换当作无损恢复作者工程。
