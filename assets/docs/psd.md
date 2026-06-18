# Live2D Pose Snapshot PSD Export 与分阶段批量回写设计

## 1. 背景

当前 Live2D PSD 导出逻辑主要基于默认姿势：

```text
load moc3
reset parameters to default
update model
read drawable snapshot
render drawable layers to PSD
write PSD + .lpkpsd.json
```

这对普通单角色默认站立姿势有效，但很多 Live2D 模型有多套动作、姿势、特殊交互场景。某些贴图块只会在特定 motion、特定参数状态、特定时间点出现。只导出默认姿势会漏掉这些区域，导致后续改图和回写 PNG 图集不完整。

目标是扩展现有 PSD 工具，使其支持：

```text
1. 针对指定 motion / 指定时间点导出 PSD
2. 将多个姿态 PSD 的修改合并回同一套 PNG atlas
3. 支持冲突检测、调整回写顺序
4. 支持分阶段工作流：正面改完后作为新 base，再继续改背面 / 特殊动作
```

---

## 2. 现有机制概括

当前核心文件：

```text
app/core/psd_reconstructor.py
app/core/cubism_core.py
app/core/psd_project.py
app/gui/PsdReconstructionPage.py
```

现有导出：

```text
model3.json / moc3 / folder
→ resolve textures
→ resolve or export drawables.json
→ render mesh layers or atlas component layers
→ write PSD
→ write .lpkpsd.json
```

现有回写：

```text
PSD + .lpkpsd.json
→ read PSD layers
→ read metadata layers
→ for mesh mode:
     use vertices / uvs / indices to project edited pixels back to texture atlas
→ for atlas-components mode:
     paste component layers back to atlas canvas
→ save PNG textures
```

关键点：

```text
PSD 是编辑界面
.lpkpsd.json 是回写地图
PNG atlas 是最终结果
```

---

## 3. 需要解决的问题

### 3.1 默认姿势覆盖不足

当前 `CubismCoreModel` 读取 drawable snapshot 前会 reset 默认参数。它只导出默认姿势中可见的 drawable。

需要改成：

```text
load moc3
reset parameters
apply motion / pose / parameter overrides
update model
read drawable snapshot
render PSD
```

### 3.2 不同姿势 PSD 图层不同

不同姿态导出的 PSD 图层可能完全不同：

```text
front_pose.psd:
  ArtMeshFace
  ArtMeshBody
  ArtMeshArmFront

back_pose.psd:
  ArtMeshHairBack
  ArtMeshBackBody
  ArtMeshArmBack
```

这不是问题。只要它们来自同一套原始 texture atlas，且各自 `.lpkpsd.json` 记录了正确的 texture_index / vertices / uvs / indices，就可以合并回同一套 PNG。

### 3.3 多 PSD 回写可能冲突

冲突定义在 PNG atlas 像素空间：

```text
PSD A 写入 texture_00.png 的某片区域
PSD B 也写入 texture_00.png 的同一片区域
=> 冲突
```

需要支持：

```text
1. 调整 patch 顺序
2. last-write-wins 默认策略
3. conflict_report.json
4. 后续可支持 per-layer enable/disable
```

---

## 4. 新增 pose_spec

给 PSD 导出增加 `pose_spec` 参数。

示例：

```python
pose_spec = {
    "type": "motion-frame",
    "motion_group": "TapBody",
    "motion_index": 0,
    "time_ms": 1200,
    "expression": "",
    "parameter_overrides": {}
}
```

字段含义：

```text
type:
  default
  motion-frame
  parameter-overrides

motion_group:
  model3.json FileReferences.Motions 中的 group

motion_index:
  group 下第几个 motion

time_ms:
  采样时间点，单位 ms

expression:
  预留，后续支持 expression3.json

parameter_overrides:
  手动参数覆盖，例如 ParamAngleX / ParamBodyAngleX
```

旧逻辑不传 `pose_spec` 时保持兼容，等价于：

```python
pose_spec = {"type": "default"}
```

---

## 5. 单姿势 PSD 导出设计

### 5.1 修改 reconstruct_live2d_psd

当前：

```python
def reconstruct_live2d_psd(source, output_dir, progress=None, mode="mesh"):
    ...
```

改为：

```python
def reconstruct_live2d_psd(
    source,
    output_dir,
    progress=None,
    mode="mesh",
    pose_spec=None,
):
    ...
```

### 5.2 修改 export_drawables_sidecar

当前逻辑：

```python
model = core.load_moc(source_info.moc3)
snapshot = model.drawable_snapshot()
```

改为：

```python
model = core.load_moc(source_info.moc3)

if pose_spec:
    apply_pose_spec(
        model=model,
        model_json=source_info.model_json,
        pose_spec=pose_spec,
    )

snapshot = model.drawable_snapshot()
snapshot["pose"] = pose_spec or {"type": "default"}
```

### 5.3 PSD 文件命名

默认姿势：

```text
model_mesh_pose.psd
model_mesh_pose.lpkpsd.json
```

指定 motion：

```text
model_TapBody_0_1200ms_mesh_pose.psd
model_TapBody_0_1200ms_mesh_pose.lpkpsd.json
```

注意对 motion group 做文件名安全化。

### 5.4 metadata 增加 pose 字段

`.lpkpsd.json` 增加：

```json
{
  "format": "LpkUnpacker.Live2DAtlasPSD",
  "version": 1,
  "mode": "mesh",
  "pose": {
    "type": "motion-frame",
    "motion_group": "TapBody",
    "motion_index": 0,
    "time_ms": 1200,
    "expression": "",
    "parameter_overrides": {}
  },
  "source_model": "...",
  "canvas": {
    "width": 4096,
    "height": 4096
  },
  "textures": [],
  "layers": []
}
```

---

## 6. motion3_evaluator 设计

新增文件：

```text
app/core/motion3_evaluator.py
```

职责：

```text
读取 .motion3.json
在指定 time_sec 采样 Parameter / PartOpacity 曲线
返回参数值
```

API 草案：

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass
class MotionEvaluationResult:
    parameters: dict[str, float]
    part_opacities: dict[str, float]
    warnings: list[str]

def evaluate_motion3(
    motion_path: str | Path,
    time_sec: float,
) -> MotionEvaluationResult:
    ...
```

第一版优先支持：

```text
Target == "Parameter"
Linear
Stepped
InverseStepped
Bezier
```

`PartOpacity` 可以第二阶段实现。

---

## 7. CubismCoreModel 参数写入

扩展 `app/core/cubism_core.py`。

需要绑定：

```text
csmGetParameterIds
csmGetParameterValues
csmGetParameterMinimumValues
csmGetParameterMaximumValues
```

新增方法：

```python
class CubismCoreModel:
    def parameter_index_map(self) -> dict[str, int]:
        ...

    def set_parameter_value(self, param_id: str, value: float) -> bool:
        ...

    def apply_parameter_values(self, values: dict[str, float]) -> list[str]:
        ...

    def update_model(self) -> None:
        self.core.dll.csmUpdateModel(self.model_pointer)
```

写入参数时需要 clamp：

```python
value = max(min_value, min(max_value, value))
```

---

## 8. apply_pose_spec 设计

新增函数，可放在 `cubism_core.py` 或新文件 `pose_snapshot.py`：

```python
def apply_pose_spec(model, model_json: Path, pose_spec: dict) -> list[str]:
    warnings = []

    if not pose_spec or pose_spec.get("type") == "default":
        model.update_model()
        return warnings

    if pose_spec.get("type") == "motion-frame":
        motion_path = resolve_motion_path(
            model_json,
            pose_spec["motion_group"],
            int(pose_spec["motion_index"]),
        )
        time_sec = float(pose_spec.get("time_ms", 0)) / 1000.0
        result = evaluate_motion3(motion_path, time_sec)
        warnings.extend(result.warnings)
        warnings.extend(model.apply_parameter_values(result.parameters))
        # PartOpacity 可后续支持
        model.update_model()

    overrides = pose_spec.get("parameter_overrides") or {}
    if overrides:
        warnings.extend(model.apply_parameter_values(overrides))
        model.update_model()

    return warnings
```

---

## 9. GUI 改造

PSD 页面增加 “姿势快照导出” 区域。

控件：

```text
[ ] 使用姿势快照导出

Motion group: ComboBox
Motion: ComboBox
Time ms: SpinBox / Slider

快捷按钮：
0%
25%
50%
75%
100%
```

motion 来源：

```text
model3.json → FileReferences → Motions
```

导出时：

```python
pose_spec = {
    "type": "motion-frame",
    "motion_group": selected_group,
    "motion_index": selected_index,
    "time_ms": selected_time_ms,
    "expression": "",
    "parameter_overrides": {},
}

reconstruct_live2d_psd(
    source,
    output_dir,
    mode="mesh",
    pose_spec=pose_spec,
)
```

---

## 10. 批量回写总体方案

不要把多个 PSD 先合成一个 PSD。

推荐方案是：

```text
base PNG atlas
+ PSD A delta patch
+ PSD B delta patch
+ PSD C delta patch
= final PNG atlas
```

每个 patch 输入是：

```text
PSD + 对应 .lpkpsd.json
```

批量回写本质是 patch stack。

---

## 11. 为什么不用多个 PNG 做差值合并

不推荐主流程用：

```text
original PNG
repack A PNG
repack B PNG
→ pixel diff merge
```

原因：

```text
1. PNG diff 丢失 drawable_id / pose / layer_name 信息
2. 无法知道差异来自哪个 PSD 图层
3. 无法解释冲突
4. 抗锯齿、透明度、插值会产生假差异
5. 很难做 per-layer enable/disable
6. 很难生成有用的 conflict_report
```

PNG diff 只能作为兜底工具，不能作为主设计。

---

## 12. 批量回写 API 拆分

把当前单次 `_repack_mesh_psd_layers()` 拆分。

建议新增：

```python
def load_base_canvases(
    textures: list[dict],
    base_mode: str,
    project=None,
    selected_repack_id: str | None = None,
    custom_texture_dir: Path | None = None,
) -> dict[int, np.ndarray]:
    ...

def load_reference_canvases(
    textures: list[dict],
) -> dict[int, np.ndarray]:
    ...

def apply_mesh_psd_delta_to_canvases(
    psd_path: Path,
    metadata_path: Path,
    reference_canvases: dict[int, np.ndarray],
    target_canvases: dict[int, np.ndarray],
    conflict_tracker,
    options,
) -> list[str]:
    ...

def save_canvases(
    canvases: dict[int, np.ndarray],
    textures: list[dict],
    output_dir: Path,
) -> list[Path]:
    ...

def batch_repack_atlas_pngs_from_psds(
    items: list[dict],
    output_dir: Path,
    base_mode: str = "original",
    selected_repack_id: str | None = None,
    custom_texture_dir: Path | None = None,
    conflict_policy: str = "last-write-wins",
) -> ReconstructionResult:
    ...
```

`items` 示例：

```python
items = [
    {
        "psd": Path("front_pose.psd"),
        "metadata": Path("front_pose.lpkpsd.json"),
        "enabled": True,
    },
    {
        "psd": Path("back_pose.psd"),
        "metadata": Path("back_pose.lpkpsd.json"),
        "enabled": True,
    },
]
```

---

## 13. reference canvas 与 target canvas

批量回写必须区分：

```text
reference_canvases:
  用于判断某个 PSD 图层相对导出时是否被修改。
  通常来自 metadata 指向的原始 texture。

target_canvases:
  实际写入的可变输出画布。
  来自 original / latest repack / selected repack / custom folder。
```

不要用已经被前一个 PSD 修改过的 target canvas 去计算后一个 PSD 的 changed mask。

正确流程：

```text
对每个 PSD：
  用 reference_canvas 计算 changed mask
  把 changed pixels 写入 shared target_canvas
```

---

## 14. mesh 批量回写伪代码

```python
def apply_mesh_psd_delta_to_canvases(
    psd_path,
    metadata_path,
    reference_canvases,
    target_canvases,
    conflict_tracker,
    options,
):
    metadata = read_json(metadata_path)
    textures = metadata_textures(metadata)
    layers_metadata = metadata_layers(metadata, {"drawable-mesh"})

    psd = PSDImage.open(str(psd_path))
    psd_layers = index_psd_layers(psd)

    for layer_info in layers_metadata:
        texture_index = int(layer_info["texture_index"])
        reference_canvas = reference_canvases[texture_index]
        target_canvas = target_canvases[texture_index]

        layer = psd_layers.get(str(layer_info["name"]))
        if not layer:
            warn(...)
            continue

        edited_image = layer.composite(force=True)
        if edited_image is None:
            warn(...)
            continue

        edited = np.asarray(edited_image.convert("RGBA"))

        vertices = np.asarray(layer_info["vertices"], dtype=np.float32)
        uvs = np.asarray(layer_info["uvs"], dtype=np.float32)
        indices = as_int_list(layer_info["indices"])

        left = int(getattr(layer, "left", layer_info.get("left", 0)))
        top = int(getattr(layer, "top", layer_info.get("top", 0)))

        local_vertices = vertices - np.asarray([left, top], dtype=np.float32)
        texture_points = uv_to_texture_points(uvs, reference_canvas)

        reference_layer = render_repack_reference_layer(
            texture=reference_canvas,
            shape=edited.shape,
            local_vertices=local_vertices,
            texture_points=texture_points,
            indices=indices,
            opacity=float(layer_info.get("opacity", 1.0)),
        )

        edit_mask = changed_pixel_mask(edited, reference_layer)
        if not np.any(edit_mask):
            continue

        write_mask = np.zeros(target_canvas.shape[:2], dtype=np.uint8)

        for tri in iter_triangles(indices):
            tri_write_mask = replace_triangle_masked(
                src=edited,
                src_mask=edit_mask,
                dst=target_canvas,
                src_tri=local_vertices[list(tri)],
                dst_tri=texture_points[list(tri)],
                return_written_mask=True,
            )
            write_mask |= tri_write_mask

        owner = build_patch_owner(psd_path, metadata, layer_info)
        conflict_tracker.record_write(texture_index, write_mask, owner)
```

---

## 15. 冲突处理

### 15.1 冲突定义

```text
两个 patch 写入同一 texture_index 的同一像素区域。
```

### 15.2 默认策略

第一版建议：

```text
last-write-wins
```

即 patch stack 里越靠后的 PSD 优先级越高。

### 15.3 支持调整顺序

GUI 里 patch stack 需要支持：

```text
Add PSD
Remove PSD
Move Up
Move Down
Enable / Disable
```

顺序示例：

```text
base original textures
→ apply front_pose.psd
→ apply back_pose.psd
→ apply special_pose.psd
```

如果 `back_pose.psd` 和 `front_pose.psd` 写入同一区域，且策略是 `last-write-wins`，则 `back_pose.psd` 覆盖前者。

### 15.4 conflict_report.json

输出：

```json
{
  "conflicts": [
    {
      "texture_index": 0,
      "texture_name": "texture_00.png",
      "bbox": [512, 1024, 128, 96],
      "previous_owner": "front_pose.psd::ArtMeshArmL",
      "current_owner": "back_pose.psd::ArtMeshArmL",
      "overlap_pixels": 3812,
      "policy": "last-write-wins"
    }
  ]
}
```

### 15.5 后续策略

后续可支持：

```text
first-write-wins
fail-on-conflict
manual
per-layer enable/disable
conflict overlay PNG
```

---

## 16. 分阶段工作流

推荐支持工程化分阶段修改，而不是一次性把所有姿势塞进一个批量任务里。

推荐流程：

```text
1. 创建原始 Live2D PSD Patch Project
2. 导出默认 / 正面 / 常用 idle 姿势 PSD
3. 修改正面区域
4. Batch Repack，生成 front_done
5. 预览 front_done
6. 将 front_done 设为下一阶段 base
7. 导出背面 / 侧面 / 特殊动作 PSD
8. 修改背面与特殊区域
9. Batch Repack，生成 back_done
10. 将 back_done 设为下一阶段 base
11. 继续处理特效 / 表情 / 特殊交互
12. 输出 final textures
```

### 16.1 不建议真的强制复制整个工程

软件内部建议用同工程多版本：

```text
project/
  live2d/
  psd/
  repacks/
    20260618_front_done/
    20260618_back_done/
    20260618_special_done/
  preview/current/
  project.lpkpsd_project.json
```

### 16.2 Base 选择

Batch Repack 增加 base selector：

```text
Base textures:
  original
  latest_repack
  selected_repack
  custom_texture_folder
```

这样用户可以做到：

```text
正面改完 → front_done
背面修改时以 front_done 为 base
特殊动作修改时以 back_done 为 base
```

这比每次都从原始 texture 开始回写安全。

---

## 17. Project 数据结构扩展

### 17.1 repack 版本

现有 `repack_history` 可继续使用，建议增加 base 信息：

```json
{
  "id": "20260618_front_done",
  "source_psd": "...",
  "metadata": "...",
  "textures_dir": "repacks/20260618_front_done",
  "output_paths": [
    "repacks/20260618_front_done/texture_00.png"
  ],
  "base": {
    "mode": "original",
    "repack_id": "",
    "custom_texture_dir": ""
  },
  "created_at": "2026-06-18T22:00:00"
}
```

### 17.2 batch repack 记录

新增：

```json
{
  "batch_repacks": [
    {
      "id": "20260618_back_done",
      "base": {
        "mode": "selected_repack",
        "repack_id": "20260618_front_done"
      },
      "items": [
        {
          "psd": "psd/back_pose.psd",
          "metadata": "psd/back_pose.lpkpsd.json",
          "enabled": true
        },
        {
          "psd": "psd/side_pose.psd",
          "metadata": "psd/side_pose.lpkpsd.json",
          "enabled": true
        }
      ],
      "output_paths": [
        "repacks/20260618_back_done/texture_00.png"
      ],
      "conflict_report": "repacks/20260618_back_done/conflict_report.json",
      "created_at": "2026-06-18T22:00:00"
    }
  ]
}
```

---

## 18. GUI 批量回写设计

新增 Batch Repack 模式：

```text
Batch Repack

Base textures:
  ( ) Original
  ( ) Latest repack
  ( ) Selected repack: [front_done ▼]
  ( ) Custom folder

Patch stack:
  [x] front_pose.psd       pose: default/front
  [x] back_pose.psd        pose: TapBody[0]@1200ms
  [x] special_pose.psd     pose: Special[1]@900ms

Buttons:
  Add PSD
  Remove
  Move Up
  Move Down

Conflict policy:
  last-write-wins

Output:
  repacks/{version_id}/
```

完成后显示：

```text
Written textures: N
Conflicts: M
Warnings: K

Buttons:
  Open output
  Open conflict report
  Preview repack
  Use as next base
```

---

## 19. Coverage Report，可选后续功能

用于找出哪些贴图区域只在特殊动作出现。

扫描方式：

```text
选择 motion group / all motions
每隔 200ms 采样一次
读取每个 pose 的 visible drawables 和 UV 覆盖区域
生成 coverage_report.json
生成 coverage debug PNG
```

输出示例：

```json
{
  "model": "xxx.model3.json",
  "sample_interval_ms": 200,
  "poses": [
    {
      "pose_id": "TapBody_0_1200ms",
      "motion_group": "TapBody",
      "motion_index": 0,
      "time_ms": 1200,
      "visible_drawables": [
        "ArtMesh123"
      ]
    }
  ],
  "textures": [
    {
      "index": 0,
      "name": "texture_00.png",
      "coverage_ratio": 0.23,
      "regions": [
        {
          "drawable_id": "ArtMesh123",
          "pose_id": "TapBody_0_1200ms",
          "uv_bbox": [512, 1024, 128, 96]
        }
      ]
    }
  ]
}
```

---

## 20. 实施顺序

推荐 Codex 按以下顺序实现。

### Task 1: motion3_evaluator

```text
新增 app/core/motion3_evaluator.py
实现 evaluate_motion3(path, time_sec)
第一版支持 Parameter 曲线
支持 Linear / Stepped / Bezier
无法识别的 segment 记录 warning
```

### Task 2: CubismCore 参数写入

```text
扩展 app/core/cubism_core.py
绑定 csmGetParameterIds / Values / Min / Max
实现 set_parameter_value()
实现 apply_parameter_values()
实现 update_model()
```

### Task 3: pose-aware drawable snapshot

```text
新增 apply_pose_spec()
修改 export_drawables_sidecar()
支持 pose_spec
导出 snapshot 时写入 pose 字段
```

### Task 4: pose-aware PSD export

```text
修改 reconstruct_live2d_psd()
新增 pose_spec 参数
输出文件名包含 pose 信息
_build_export_metadata() 写入 pose 字段
保持旧逻辑兼容
```

### Task 5: GUI 单姿势导出

```text
PSD 页面增加姿势快照导出区域
读取 model3.json motions
支持 group / index / time_ms
调用 reconstruct_live2d_psd(..., pose_spec=...)
```

### Task 6: 批量回写 API 拆分

```text
拆分 _repack_mesh_psd_layers()
新增 apply_mesh_psd_delta_to_canvases()
新增 batch_repack_atlas_pngs_from_psds()
支持 base_mode
支持 conflict_tracker
```

### Task 7: GUI 批量回写

```text
新增 Batch Repack 模式
支持添加多个 PSD
自动匹配 metadata
支持 Move Up / Move Down
支持 base selector
支持 conflict_report
支持 Use as next base
```

### Task 8: Coverage Report

```text
扫描 motions
按时间采样 pose snapshot
统计 UV coverage
输出 coverage_report.json 和 debug PNG
```

---

## 21. 测试用例

### 21.1 兼容旧逻辑

```text
不传 pose_spec
导出结果与旧版本一致
旧 .lpkpsd.json 可继续回写
```

### 21.2 单 motion 姿势导出

```text
选择 TapBody[0] @ 1200ms
导出 PSD
metadata.pose 正确
部分 drawable vertices 与默认姿势不同
```

### 21.3 单姿势回写

```text
修改一个图层颜色
回写 PNG
对应 atlas 区域变化
未修改区域保持不变
```

### 21.4 多 PSD 批量回写

```text
front_pose.psd 修改脸
back_pose.psd 修改背部头发
批量回写
最终 PNG 同时包含两处修改
```

### 21.5 冲突检测

```text
两个 PSD 修改同一 texture 区域
生成 conflict_report.json
默认 last-write-wins
调整顺序后最终输出不同
```

### 21.6 分阶段 base

```text
生成 front_done
以 front_done 为 base 生成 back_done
back_done 保留 front_done 修改
```

---

## 22. 最终设计原则

```text
1. PSD 不是真正资产源，PNG atlas 才是最终资产。
2. .lpkpsd.json 是回写地图，必须保留。
3. 多姿态 PSD 可以合并，但应在 atlas-space 合并。
4. 主流程使用 PSD + metadata delta merge，不使用 PNG diff merge。
5. 冲突允许调整 patch 顺序，默认 last-write-wins。
6. 大工程建议分阶段固化 repack 版本。
7. 每个 repack 版本都应可作为下一阶段 base。
```
