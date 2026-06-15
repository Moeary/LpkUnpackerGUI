from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.extract.detector import UNITY_EXTENSIONS, scan_package_folder
from app.core.extract.models import ExtractSourceType
from app.core.extract.package_classifier import (
    PackageContentKind,
    classify_lpk_content,
    classify_wpk_content,
)


LIVE2D_JSON_NAMES = {"model.json"}
LIVE2D_JSON_SUFFIXES = (".model3.json",)
UNITY_LIVE2D_HINTS = ("live2d", "cubism", "moc", "model")


@dataclass(frozen=True)
class ExtractTaskPlan:
    path: Path
    source_type: ExtractSourceType
    display_type: str
    live2d_status: str
    config_path: Path | None = None
    runnable: bool = True
    note: str = ""
    content_kind: str = "unknown"


def analyze_sources(paths: list[str | Path]) -> tuple[list[ExtractTaskPlan], list[Path]]:
    tasks: list[ExtractTaskPlan] = []
    configs: list[Path] = []
    seen_tasks: set[Path] = set()
    seen_configs: set[Path] = set()

    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not path.exists():
            continue

        if path.is_dir():
            folder_tasks, folder_configs = analyze_folder(path)
            for config in folder_configs:
                if config not in seen_configs:
                    configs.append(config)
                    seen_configs.add(config)
            for task in folder_tasks:
                if task.path not in seen_tasks:
                    tasks.append(task)
                    seen_tasks.add(task.path)
            if not folder_tasks:
                task = _task_for_folder(path)
                if task.path not in seen_tasks:
                    tasks.append(task)
                    seen_tasks.add(task.path)
            continue

        if path.name.lower() == "config.json":
            if path not in seen_configs:
                configs.append(path)
                seen_configs.add(path)
            continue

        task = analyze_file(path)
        if task.path not in seen_tasks:
            tasks.append(task)
            seen_tasks.add(task.path)

        config = _config_for_path(path)
        if config and config not in seen_configs:
            configs.append(config)
            seen_configs.add(config)

    configs = _attach_configs(tasks, configs)
    return tasks, configs


def analyze_folder(folder: Path) -> tuple[list[ExtractTaskPlan], list[Path]]:
    files, configs = scan_package_folder(folder)
    tasks = [analyze_file(Path(path)) for path in files]
    config_paths = [Path(path).resolve() for path in configs]

    if not tasks and _folder_has_live2d_model(folder):
        tasks.append(_task_for_folder(folder))

    return tasks, config_paths


def analyze_file(path: str | Path) -> ExtractTaskPlan:
    source = Path(path).resolve()
    suffix = source.suffix.lower()
    config = _config_for_path(source)

    if suffix == ".lpk":
        info = classify_lpk_content(source, [config] if config else None)
        return ExtractTaskPlan(
            path=source,
            source_type=ExtractSourceType.LPK,
            display_type=f"{info.label} LPK" if info.kind != PackageContentKind.UNKNOWN else "LPK",
            live2d_status=_package_status(info.kind),
            config_path=config,
            note=f"{info.note}；也可仅导出贴图",
            content_kind=info.kind.value,
        )

    if suffix == ".wpk":
        info = classify_wpk_content(source)
        return ExtractTaskPlan(
            path=source,
            source_type=ExtractSourceType.WPK,
            display_type=f"{info.label} WPK" if info.kind != PackageContentKind.UNKNOWN else "WPK",
            live2d_status=_package_status(info.kind),
            config_path=config,
            note=f"{info.note}；会先拆出内部 LPK 再处理",
            content_kind=info.kind.value,
        )

    if _is_live2d_model_json(source):
        return ExtractTaskPlan(
            path=source,
            source_type=ExtractSourceType.UNKNOWN,
            display_type="Live2D 模型 JSON",
            live2d_status="已解包模型",
            runnable=False,
            note="当前解包页不处理模型 JSON，可去预览/PSD 页面使用",
        )

    if suffix in UNITY_EXTENSIONS:
        probable_live2d = _path_has_live2d_hint(source)
        return ExtractTaskPlan(
            path=source,
            source_type=ExtractSourceType.UNITY,
            display_type="Unity Live2D 候选" if probable_live2d else "Unity 资源",
            live2d_status="疑似 Live2D" if probable_live2d else "执行时验证 Live2D",
            runnable=True,
            note="非 Live2D Unity 来源会被拒绝，贴图模式可单独导出图片",
        )

    return ExtractTaskPlan(
        path=source,
        source_type=ExtractSourceType.UNKNOWN,
        display_type="未知",
        live2d_status="不可处理",
        runnable=False,
        note="不支持的输入类型",
    )


def _task_for_folder(path: Path) -> ExtractTaskPlan:
    if _folder_has_live2d_model(path):
        return ExtractTaskPlan(
            path=path,
            source_type=ExtractSourceType.FOLDER,
            display_type="Live2D 文件夹",
            live2d_status="已解包模型",
            runnable=False,
            note="当前解包页不重复解包模型文件夹，可去预览/PSD 页面使用",
        )
    return ExtractTaskPlan(
        path=path,
        source_type=ExtractSourceType.FOLDER,
        display_type="文件夹",
        live2d_status="未发现来源",
        runnable=False,
        note="未发现 LPK/WPK/Unity 文件或已解包 Live2D 模型",
    )


def _attach_configs(tasks: list[ExtractTaskPlan], configs: list[Path]) -> list[Path]:
    all_configs = list(configs)
    seen = set(all_configs)
    for task in tasks:
        if task.config_path and task.config_path not in seen:
            all_configs.append(task.config_path)
            seen.add(task.config_path)
    return all_configs


def _config_for_path(path: Path) -> Path | None:
    config = path.parent / "config.json"
    return config.resolve() if config.exists() else None


def _is_live2d_model_json(path: Path) -> bool:
    name = path.name.lower()
    return name in LIVE2D_JSON_NAMES or name.endswith(LIVE2D_JSON_SUFFIXES)


def _folder_has_live2d_model(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any(_is_live2d_model_json(child) for child in path.rglob("*.json"))


def _path_has_live2d_hint(path: Path) -> bool:
    text = str(path).lower()
    return any(hint in text for hint in UNITY_LIVE2D_HINTS)


def _package_status(kind: PackageContentKind) -> str:
    if kind == PackageContentKind.LIVE2D:
        return "Live2D 包"
    if kind == PackageContentKind.SPINE:
        return "Spine 包"
    if kind == PackageContentKind.MIXED:
        return "混合包"
    return "待执行验证"
