"""Private CPU process boundary for PSD tasks and detached snapshot preparation."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import fields
import faulthandler
import json
import logging
import os
from pathlib import Path
import pickle
from queue import Empty, Queue
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable


class PsdWorkerError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int | None = None):
        super().__init__(message)
        self.exit_code = exit_code


def _stop_child(child):
    if child.poll() is not None:
        return
    try:
        child.terminate()
    except OSError:
        pass
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=2)


def _archive_failure(job_dir: Path, code: int | None) -> Path | None:
    from app.paths import RUNTIME_LOG_DIR
    try:
        destination = RUNTIME_LOG_DIR / ("psd-" + job_dir.name)
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("worker.log", "faulthandler.log", "result.json"):
            path = job_dir / name
            if path.is_file():
                shutil.copy2(path, destination / name)
        (destination / "exit.json").write_text(json.dumps({"exit_code": code}), encoding="utf-8")
        return destination
    except OSError:
        logging.getLogger(__name__).exception("Could not retain PSD worker failure diagnostics")
        return None


def worker_command(job_file: Path) -> list[str]:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return [sys.executable, "--psd-worker", "--job-file", str(job_file)]
    return [sys.executable, "-u", "-m", "app.main", "--psd-worker", "--job-file", str(job_file)]


def snapshot_payload(snapshot: Any, fallback_dir: Path) -> dict:
    """Serialize official detached state in the task thread, retaining its lease.

    Legacy immutable package paths need no writer. The fallback exists for
    older detached writer integrations that have not adopted the pure payload.
    """
    writer = snapshot.export_request
    if writer is not None and callable(getattr(writer, "to_worker_payload", None)):
        payload = writer.to_worker_payload()
        model_path = ""
    elif writer is not None:
        payload = None
        model_path = str(writer.write(fallback_dir))
    else:
        payload = None
        model_path = snapshot.model_path
    return {"model_path": model_path, "source_origin": snapshot.source_origin,
            "skin_source": dict(snapshot.skin_source or {}), "export_payload": payload}


def run_psd_job(job: dict, job_dir: Path, *,
                progress: Callable[[int, str], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> dict:
    """Drain child output independently so cancellation never waits on a pipe."""
    job_file = job_dir / "request.pickle"
    result_file = job_dir / "result.json"
    with job_file.open("wb") as stream:
        pickle.dump(job, stream, protocol=pickle.HIGHEST_PROTOCOL)
    if cancelled and cancelled():
        raise InterruptedError("PSD task cancelled")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    child = subprocess.Popen(worker_command(job_file), cwd=str(Path(__file__).resolve().parents[2]),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1,
                             creationflags=flags, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    messages: Queue[str | None] = Queue()
    tail = deque(maxlen=16)

    def drain():
        log = None
        try:
            try:
                log = (job_dir / "worker.log").open("w", encoding="utf-8")
            except OSError:
                logging.getLogger(__name__).exception("PSD worker log unavailable; continuing to drain output")
            for line in child.stdout:
                if log is not None:
                    try:
                        log.write(line)
                        log.flush()
                    except OSError:
                        logging.getLogger(__name__).exception("PSD worker log failed; continuing to drain output")
                        failed_log, log = log, None
                        try:
                            failed_log.close()
                        except OSError:
                            pass
                messages.put(line)
        except Exception as exc:
            messages.put(json.dumps({"psd_event": "reader_error", "error": str(exc)}))
        finally:
            try:
                if log is not None:
                    log.close()
            except OSError:
                logging.getLogger(__name__).exception("PSD worker log could not be closed")
            finally:
                messages.put(None)

    reader = threading.Thread(target=drain, name="psd-worker-output", daemon=True)
    reader.start()
    finished_output = False
    try:
        while child.poll() is None or not finished_output:
            if cancelled and cancelled():
                _stop_child(child)
                raise InterruptedError("PSD task cancelled")
            try:
                line = messages.get(timeout=.05)
            except Empty:
                continue
            if line is None:
                finished_output = True
                continue
            tail.append(line.rstrip())
            try:
                event = json.loads(line)
            except ValueError:
                logging.getLogger(__name__).info("PSD worker: %s", line.rstrip())
                continue
            if not isinstance(event, dict):
                continue
            if event.get("psd_event") == "reader_error":
                raise PsdWorkerError("PSD worker output failed: " + event["error"])
            if event.get("psd_event") == "progress" and progress:
                progress(int(event["value"]), str(event["message"]))
        code = child.wait()
        if cancelled and cancelled():
            raise InterruptedError("PSD task cancelled")
        if code:
            label = f"0x{code & 0xffffffff:08X}"
            message = f"PSD worker exited ({label})."
            if result_file.is_file():
                result = json.loads(result_file.read_text(encoding="utf-8"))
                message = str(result.get("error") or message)
            elif tail:
                message += "\n" + "\n".join(tail)
            raise PsdWorkerError(message, exit_code=code)
        if not result_file.is_file():
            raise PsdWorkerError("PSD worker returned no result.")
        result = json.loads(result_file.read_text(encoding="utf-8"))
        if "error" in result:
            raise PsdWorkerError(str(result["error"]))
        return result
    except PsdWorkerError as exc:
        _stop_child(child)
        reader.join(timeout=2)
        archive = _archive_failure(job_dir, exc.exit_code)
        if archive is not None:
            exc.args = (str(exc) + f"\nPSD diagnostics: {archive}",)
        raise
    finally:
        _stop_child(child)
        reader.join(timeout=2)
        child.stdout.close()


def _encode_result(result: Any) -> dict:
    def encode(value):
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): encode(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [encode(item) for item in value]
        return value
    return {item.name: encode(getattr(result, item.name)) for item in fields(result)
            if item.name != "region_masks"}


def decode_result(payload: dict):
    from app.core.psd_reconstructor import ReconstructionResult
    values = dict(payload)
    for key in ("psd_path", "metadata_path", "report_path"):
        values[key] = Path(values[key]) if values.get(key) else None
    values["output_paths"] = [Path(path) for path in values.get("output_paths", [])]
    values["texture_outputs"] = {int(index): Path(path) for index, path in values.get("texture_outputs", {}).items()}
    return ReconstructionResult(**values)


def decode_stage(payload: dict):
    from app.core.psd_project import PsdPoseExportStage
    values = dict(payload)
    for key in ("stage_root", "snapshot_dir", "scheme_dir", "final_snapshot_dir", "final_scheme_dir", "source_model"):
        values[key] = Path(values[key])
    return PsdPoseExportStage(**values)


def publish_output(stage: Path, target: Path, backup: Path):
    """Publish complete task files; restore every replaced entry on failure."""
    target.mkdir(parents=True, exist_ok=True)
    backup.mkdir()
    moved, previous = [], []
    try:
        for source in stage.iterdir():
            destination = target / source.name
            if destination.exists():
                os.rename(destination, backup / source.name)
                previous.append(source.name)
            os.rename(source, destination)
            moved.append(source.name)
    except Exception:
        for name in reversed(moved):
            os.rename(target / name, stage / name)
        for name in reversed(previous):
            os.rename(backup / name, target / name)
        raise


def _rebase(value: Any, old: Path, new: Path):
    if isinstance(value, dict):
        return {key: _rebase(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebase(item, old, new) for item in value]
    if isinstance(value, str) and Path(value).is_absolute():
        try:
            return str(new / Path(value).relative_to(old))
        except ValueError:
            pass
    return value


def _restore_snapshot(payload):
    from app.core.psd_project import PsdSnapshotRequest
    from app.core.live2d_editor_export import Live2DExportSnapshotRequest
    values = dict(payload)
    exported = values.pop("export_payload")
    return PsdSnapshotRequest(**values, export_request=(
        Live2DExportSnapshotRequest.from_worker_payload(exported) if exported is not None else None))


def _execute(job: dict, job_dir: Path) -> dict:
    from app.core.psd_project import (Live2DPSDProject, create_project_from_snapshot_request,
                                     finalize_pose_export_stage, stage_pose_export)
    from app.core.psd_reconstructor import (reconstruct_live2d_psd, repack_atlas_png_from_psd,
                                           repack_multiple_psds)
    def progress(value, message):
        print(json.dumps({"psd_event": "progress", "value": value, "message": str(message)}, ensure_ascii=False), flush=True)
    if job.get("operation") == "compare":
        from app.core.psd_comparison import build_comparison
        return build_comparison(job["metadata_path"], job["texture_outputs"], job_dir / "comparison",
                                mesh_data=job.get("mesh_data"), progress=progress)
    if job.get("operation") == "initialize":
        request = _restore_snapshot(job["snapshot"])
        project = create_project_from_snapshot_request(request.export_request, job["project_dir"],
                                                       job.get("project_name", "psd"),
                                                       stage_dir=job_dir / "initial-project", owner_token=job_dir.name)
        return {"project_dir": str(project.project_dir), "project_file": str(project.project_file), "data": project.data}
    source, mode = job["source_path"], job["mode"]
    pose = job.get("pose_stage_request")
    stage = None
    if pose:
        project = Live2DPSDProject(Path(pose["project_dir"]), Path(pose["project_file"]), pose["project_data"])
        stage = stage_pose_export(project, pose["name"], pose.get("priority", 0), pose.get("parameters"),
                                 _restore_snapshot(pose["snapshot"]), scheme_id=pose["scheme_id"],
                                 selection=pose.get("selection"), skin_source=pose.get("skin_source"),
                                 pose_source=pose.get("pose_source", "preview"),
                                 parameter_preset_id=pose.get("parameter_preset_id", ""),
                                 project_data=pose["project_data"], stage_dir=job_dir / "export",
                                 log=lambda message: progress(5, message))
        source, output = str(stage.source_model), str(stage.scheme_dir)
    else:
        output = str(job_dir / "output")
    common = {"progress": progress, "resource_limits": job.get("resource_limits")}
    if mode == "multi-repack":
        result = repack_multiple_psds(job["multi_psd_paths"], output, **common)
    elif mode == "repack-atlas":
        result = repack_atlas_png_from_psd(source, output, metadata_path=job.get("metadata_path") or None,
                                         allow_shared_uv=job.get("allow_shared_uv", False), **common)
    else:
        result = reconstruct_live2d_psd(source, output, mode=mode, parameter_values=job.get("parameter_values"),
                                       pose_name=job.get("pose_name"), output_name=job.get("output_name"),
                                       selected_drawable_ids=job.get("selected_drawable_ids"),
                                       selection_region=job.get("selection_region"), mesh_data=job.get("mesh_data"),
                                       atlas_layout=job.get("atlas_layout", "packed"), **common)
    encoded = _encode_result(result)
    if stage:
        finalize_pose_export_stage(stage, result)
        return {"result": encoded, "stage": _encode_result(stage)}
    old, new = Path(output), Path(job["output_dir"]).resolve()
    for path in old.rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(_rebase(data, old, new), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"result": _rebase(encoded, old, new), "output_stage_dir": str(old)}


def main(args=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-file", required=True)
    options = parser.parse_args(args)
    job_file = Path(options.job_file).resolve()
    job_dir = job_file.parent
    # This pickle is created by the current task, never supplied by an imported
    # model or project. Do not expose it as an ordinary file-open operation.
    with (job_dir / "faulthandler.log").open("w", encoding="utf-8") as fault:
        faulthandler.enable(file=fault, all_threads=True)
        try:
            with job_file.open("rb") as stream:
                job = pickle.load(stream)
            result = _execute(job, job_dir)
            (job_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return 0
        except Exception as exc:
            logging.exception("PSD task failed")
            (job_dir / "result.json").write_text(json.dumps({"error": str(exc)}, ensure_ascii=False), encoding="utf-8")
            return 1


if __name__ == "__main__":
    sys.exit(main())
