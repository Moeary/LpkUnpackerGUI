# Spine 原生预览

预览由 Python / PySide6 控制，官方 `spine-cpp` 负责骨骼、约束和动画求值，OpenGL 子窗口负责绘制。Live2D 与 Spine 均不启动浏览器、QtWebEngine 或 HTTP 服务。

## 使用

在设置中安装或选择匹配的 **Spine native runtime**，再向资源预览页拖入骨骼、模型目录或 LPK/WPK。左侧调整视图，中间显示模型，右侧选择皮肤和动画、暂停或拖动时间。首次播放优先选择 `normal` / `idle`。

设置中的目标版本默认是 **3.8.75**，控制导出转换；它不会通过改版本字符串强迫不兼容的骨骼载入。预览按模型版本选择匹配的 native runtime。旧的 `spine-ts` / JavaScript runtime 目录不再用于预览。

## 高 DPI 与缩放

Spine 直接在屏幕物理像素大小的 OpenGL 缓冲区绘制，缩放作用于模型坐标，不再对网页 canvas 的截图做 CSS 放大。左侧“放大抗锯齿”可控制采样；“适应预览窗口”将缩放恢复为 100%，清除旋转与偏移，不改变动画姿态。

Live2D 的离屏缓冲同时考虑屏幕像素比例、缩放和抗锯齿，设置了尺寸与像素总数上限。高 DPI / 放大可能增加 GPU 内存与渲染成本。原贴图分辨率不足时，抗锯齿只能改善边缘，不能补回不存在的纹理细节。

## 窗口状态

渲染使用 `QOpenGLWindow` 与 `QWidget.createWindowContainer`，避免动态加入 `QOpenGLWidget` 或 WebEngine 导致顶层 HWND 重建。主窗口记住正常恢复尺寸和最大化状态；预览控件不改写主窗口窗口标志。

## 验收

`scripts/verify_spine_embedded_preview.py` 在真实 Windows 桌面测试模型像素、动画变化、暂停、时间拖动、抗锯齿、模型切换及最大化/还原。脚本使用独立设置目录，不覆盖用户设置。可通过 `QT_SCALE_FACTOR=2` 验证高 DPI，报告与截图写入指定路径。

Spine runtime 使用其专有许可，来源开放不等同于 MIT；下载或分发应保留对应版本的许可证。

## 当前边界

已验证原生 3.8.75 与 4.0 模型。当前原生画布尚未提供人物姿态 PSD 导出，按钮会禁用并说明原因；atlas 部件 PSD 导出与回写仍可使用。含 two-color / dark tint 的特殊模型目前只应用主颜色，效果可能与编辑器不同。
