from __future__ import unicode_literals
import json
import logging
import os
import shutil
from pathlib import Path
from typing import List, Tuple
import zipfile

from app.core.utils import *

logger = logging.getLogger("lpkLoder")

_ATLAS_PAGE_METADATA = (
    "size:",
    "format:",
    "filter:",
    "repeat:",
    "pma:",
    "scale:",
    "minificationfilter:",
    "magnificationfilter:",
)
_ATLAS_REGION_METADATA = (
    "rotate:",
    "xy:",
    "bounds:",
    "orig:",
    "offset:",
    "index:",
)


def _atlas_page_candidates(lines: list[str], aliases: list[str]) -> list[int]:
    """Find page header lines in old and new Spine atlas syntax."""

    def stem(value: str) -> str:
        value = value.strip().replace("\\", "/")
        return Path(value).stem.casefold()

    alias_stems = {stem(alias) for alias in aliases if stem(alias)}
    candidates: list[int] = []

    # Prefer explicit page aliases.  They remain reliable even when a page
    # omits optional size/format/filter metadata.
    if alias_stems:
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or line[:1].isspace():
                continue
            if stem(stripped) in alias_stems:
                candidates.append(index)

    # Fill missing aliases/pages from the page-level metadata layout.  Page
    # metadata is unindented; region metadata is indented beneath a region
    # name.  This avoids confusing a region's ``size:`` with a new page.
    for index, line in enumerate(lines):
        stripped = line.strip().lower()
        if (
            not stripped
            or line[:1].isspace()
            or index in candidates
            or stripped.startswith(_ATLAS_PAGE_METADATA)
        ):
            continue
        if stripped == "region" or stripped.startswith(_ATLAS_REGION_METADATA):
            continue
        lookahead = index + 1
        saw_page_metadata = False
        while lookahead < len(lines):
            next_line = lines[lookahead]
            next_stripped = next_line.strip().lower()
            if not next_stripped:
                break
            if next_line[:1].isspace():
                # An indented line immediately after the candidate is a
                # region field, so this candidate is a region name.
                break
            if next_stripped == "region" or next_stripped.startswith(_ATLAS_REGION_METADATA):
                break
            if next_stripped.startswith(_ATLAS_PAGE_METADATA):
                saw_page_metadata = True
            elif saw_page_metadata:
                # The first unindented, non-metadata line starts the first
                # region.  Keep the page candidate and stop this scan.
                break
            else:
                break
            lookahead += 1
            if lookahead - index > 12:
                break
        if saw_page_metadata:
            candidates.append(index)

    # Newer atlas writers may omit every optional page property.  Page image
    # names are still unindented and normally carry an image suffix; accepting
    # them at the start of a blank-separated block recovers every page without
    # mistaking a region's trailing ``index:`` for a page header.
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or line[:1].isspace() or index in candidates:
            continue
        if not _looks_like_atlas_image_name(stripped):
            continue
        if index == 0 or not lines[index - 1].strip():
            candidates.append(index)

    return sorted(dict.fromkeys(candidates))


def _looks_like_atlas_image_name(value: str) -> bool:
    return value.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"))


class LpkDecryptError(RuntimeError):
    """Structured, non-interactive LPK decryption failure.

    The old loader asked for a file id with ``input()`` after automatic
    recovery failed.  That blocks GUI worker threads forever.  Callers can
    catch this exception and expose the filename and attempted ids in their
    normal result/error channel instead.
    """

    def __init__(
        self,
        message: str,
        *,
        filename: str | None = None,
        attempted_file_ids: list[str] | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.filename = filename
        self.attempted_file_ids = tuple(attempted_file_ids or ())
        self.cause = cause


class LpkLoader():
    def __init__(self, lpkpath, configpath) -> None:
        self.lpkpath = lpkpath
        self.configpath = configpath
        self.lpkType = None
        self.encrypted = "true"
        self.trans = {}
        self._trans_normalized = {}
        self.entrys = {}
        self.last_decrypt_error: LpkDecryptError | None = None
        self.load_lpk()
    
    def load_lpk(self):
        self.lpkfile = zipfile.ZipFile(self.lpkpath)
        try:
            config_mlve_raw = self.lpkfile.read(hashed_filename("config.mlve")).decode()
        except KeyError:
            try:
                config_mlve_raw = self.lpkfile.read("config.mlve").decode('utf-8-sig')
            except Exception as e:
                logger.fatal(f"Failed to retrieve lpk config {self.configpath} for: {e}")
                raise e

        self.mlve_config = json.loads(config_mlve_raw)

        logger.debug(f"mlve config:\n {self.mlve_config}")
        self.lpkType = self.mlve_config.get("type")
        # only steam workshop lpk needs config.json to decrypt
        if self.lpkType == "STM_1_0":
            self.load_config()
    
    def load_config(self):
        if not self.configpath:
            raise LpkDecryptError(
                "STM_1_0 LPK requires a config.json for decryption",
                filename=None,
            )
        try:
            with open(self.configpath, "r", encoding="utf8") as config_file:
                self.config = json.load(config_file)
        except Exception as exc:
            raise LpkDecryptError(
                f"Failed to load LPK config {self.configpath}: {exc}",
                filename=None,
                cause=exc,
            ) from exc
    
    def extract(self, outputdir: str):
        subdir = ""
        created_dirs = []
        try:
            if self.lpkType in ["STD2_0", "STM_1_0"]:
                for chara in self.mlve_config["list"]:
                    if self.lpkType == "STM_1_0" and hasattr(self, 'config') and 'title' in self.config:
                        chara_name = self.config["title"]
                    else:
                        chara_name = chara["character"] if chara["character"] != "" else "character"
                    subdir =  os.path.join(outputdir, normalize(chara_name))
                    safe_mkdir(subdir)
                    created_dirs.append(subdir)

                    for i in range(len(chara["costume"])):
                        logger.info(f"extracting {chara_name}_costume_{i}")
                        self.extract_costume(chara["costume"][i], subdir)

                    # Replace encrypted references only after all costume
                    # payloads have been visited.  This also handles model
                    # commands and references encountered from nested JSON.
                    for name in self.entrys:
                        out_s: str = self.entrys[name]
                        out_s = self._replace_references_in_text(out_s)
                        output_path = Path(subdir) / name
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_text(out_s, encoding="utf8")
            else:
                logger.warning(
                    "Deprecated/unknown LPK format detected; attempting STD_1_0 format"
                )
                logger.warning(
                    "Decryption may not work for some packs even though all files are emitted"
                )
                self.encrypted = self.mlve_config.get("encrypt", "true")
                if self.encrypted == "false":
                    logger.info("LPK is not encrypted; extracting all files")
                    self.lpkfile.extractall(outputdir)
                    return [outputdir]
                # For STD_1_0 and earlier
                for file in self.lpkfile.namelist():
                    if os.path.splitext(file)[-1] == '':
                        continue
                    subdir = os.path.join(outputdir, normalize(os.path.dirname(file)))
                    outputFilePath = os.path.join(subdir, normalize(os.path.basename(file)))
                    safe_mkdir(subdir)
                    if os.path.splitext(file)[-1] in [".json", ".mlve", ".txt"]:
                        logger.debug("Extracting %s -> %s", file, outputFilePath)
                        self.lpkfile.extract(file, outputdir)
                    else:
                        logger.debug("Decrypting %s -> %s", file, outputFilePath)
                        decryptedData = self.decrypt_file(file)
                        with open(outputFilePath, "wb") as outputFile:
                            outputFile.write(decryptedData)
                return [outputdir]
        except Exception as e:
            logger.fatal(f"Failed to decrypt {self.lpkpath} for:{e}")
            try:
                # pass
                if os.path.exists(subdir) and (not os.listdir(subdir)):
                    shutil.rmtree(subdir, ignore_errors=True)
            except Exception as de:
                logging.error(f"Failed to clean up empty directories created by error unpacking: {de}")
            raise e
        return created_dirs

    def extract_costume(self, costume: dict, dir: str):
        filename = costume.get("path", "")
        if not filename:
            return

        self.check_decrypt(filename)

        self.extract_model_json(filename, dir)

    def extract_model_json(self, model_json: str, dir):
        logger.debug(f"========= extracting model {model_json} =========")
        # Already extracted.  References can contain slash/case variations,
        # so use the same normalized lookup as output rewriting.
        if self._lookup_trans(model_json):
            return

        subdir = dir
        entry_s = self.decrypt_file(model_json).decode(encoding="utf-8-sig")
        entry = json.loads(entry_s)

        out_s = json.dumps(entry, ensure_ascii=False)
        id = len(self.entrys)

        self.entrys[f"model{id}.json"] = out_s

        self._register_trans(model_json, f"model{id}.json")

        logger.debug(f"model{id}.json:\n{entry}")

        for name, val in travels_dict(entry):
            logger.debug(f"{name} -> {val}")
            # extract submodel
            if (name.lower().endswith("_command") or name.lower().endswith("_postcommand")) and val:
                commands:List[str] = val.split(";")
                for cmd in commands:
                    enc_file = find_encrypted_file(cmd)
                    if enc_file == None:
                        continue

                    if self.is_model_command(cmd):
                        enc_file = find_encrypted_file(cmd) # 是否重复赋值？Shall it be re-assigned?
                        self.extract_model_json(enc_file, dir)
                    else:
                        output_name = self.name_change(f"{name}_{id}")
                        self._recover_reference(enc_file, output_name, subdir)


            if is_encrypted_file(val):
                enc_file = val
                # already decrypted
                if self._lookup_trans(enc_file):
                    continue
                # recover regular files
                else:
                    output_name = self.name_change(f"{name}_{id}")
                    self._recover_reference(enc_file, output_name, subdir)

        # Atlas page headers refer to the original texture page name, which
        # is often not itself an encrypted filename (for example
        # ``skeleton.png``).  Restore those headers after all texture
        # references have been mapped.
        self._rewrite_atlas_pages(entry, subdir)
        
        logger.debug(f"========= end of model {model_json} =========")

    def is_model_command(self, cmd: str):
        model_commands = ["change_cos", "change_model", "add_submodel", "remove_submodel"]
        for model_cmd in model_commands:
            if cmd.startswith(model_cmd):
                return True
        return False

    def check_decrypt(self, filename: str) -> bool:
        """Check an entry without ever reading from stdin.

        Steam packs occasionally carry a stale ``fileId``.  We first try the
        configured id and then the id derived from ``lpkFile``.  If neither
        works, raise :class:`LpkDecryptError` so GUI and batch callers can
        report the failure without blocking a worker thread.
        """

        logger.info("try to decrypt entry model.json")
        config = getattr(self, "config", None)
        if not isinstance(config, dict):
            error = LpkDecryptError(
                "LPK has no decryption config",
                filename=filename,
            )
            self.last_decrypt_error = error
            raise error

        original_file_id = config.get("fileId")
        attempted: list[str] = []
        candidates: list[str] = []

        def add_candidate(value) -> None:
            if value is None:
                return
            text = str(value).strip()
            if not text:
                return
            # ``lpkFile`` is normally a filename, but some configs contain a
            # full path.  Path.stem handles both without ``str.strip``
            # accidentally removing unrelated characters.
            if text.lower().endswith(".lpk"):
                text = Path(text).stem
            if text and text not in candidates:
                candidates.append(text)

        add_candidate(original_file_id)
        add_candidate(config.get("lpkFile"))
        add_candidate(Path(str(self.lpkpath)).name)

        last_error: Exception | None = None
        for file_id in candidates:
            attempted.append(file_id)
            config["fileId"] = file_id
            try:
                self.decrypt_file(filename).decode(encoding="utf-8-sig")
                self.last_decrypt_error = None
                return True
            except (UnicodeDecodeError, KeyError, ValueError, TypeError) as exc:
                last_error = exc
                logger.info("fileId %s did not decrypt %s", file_id, filename)

        if original_file_id is None:
            config.pop("fileId", None)
        else:
            config["fileId"] = original_file_id

        error = LpkDecryptError(
            (
                f"Unable to decrypt LPK entry {filename!r}; tried fileId "
                f"values: {', '.join(attempted) or '(none)'}"
            ),
            filename=filename,
            attempted_file_ids=attempted,
            cause=last_error,
        )
        self.last_decrypt_error = error
        logger.error("decrypt error for %s: %s", self.lpkpath, error)
        raise error from last_error

    @staticmethod
    def _reference_key(value: str) -> str:
        """Normalize a model reference for slash/case-insensitive lookup."""

        return str(value).replace("\\", "/").lstrip("./").casefold()

    def _register_trans(self, source: str, target: str) -> None:
        self.trans[source] = target
        self._trans_normalized[self._reference_key(source)] = target

    def _lookup_trans(self, source: str) -> str | None:
        if source in self.trans:
            return self.trans[source]
        return self._trans_normalized.get(self._reference_key(source))

    def _recover_reference(self, filename: str, output_name: str, subdir: str) -> str:
        """Decrypt a referenced payload and register its output name."""

        existing = self._lookup_trans(filename)
        if existing:
            return existing
        _, suffix = self.recovery(filename, os.path.join(subdir, output_name))
        target = output_name + suffix
        self._register_trans(filename, target)
        return target

    def _replace_references_in_text(self, text: str) -> str:
        """Replace encrypted names in JSON and command strings.

        Values in command strings can contain several references separated by
        spaces/semicolons, so a recursive JSON-only rewrite would miss them.
        Longest-first replacement also prevents a short alias from consuming
        part of a longer path.
        """

        result = text
        for source, target in sorted(
            self.trans.items(), key=lambda item: len(item[0]), reverse=True
        ):
            result = result.replace(source, target)
            normalized = source.replace("\\", "/")
            if normalized != source:
                result = result.replace(normalized, target)
        return result

    def _rewrite_atlas_pages(self, entry: dict, subdir: str) -> None:
        atlases = entry.get("atlases") if isinstance(entry, dict) else None
        if not isinstance(atlases, list):
            return

        for atlas_info in atlases:
            if not isinstance(atlas_info, dict):
                continue
            atlas_source = atlas_info.get("atlas")
            atlas_output = self._lookup_trans(atlas_source) if isinstance(atlas_source, str) else None
            if not atlas_output:
                continue
            atlas_path = Path(subdir) / atlas_output
            if not atlas_path.is_file():
                continue
            try:
                atlas_text = atlas_path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue

            texture_outputs = []
            for texture in atlas_info.get("textures", []):
                if isinstance(texture, str):
                    mapped = self._lookup_trans(texture)
                    if mapped:
                        texture_outputs.append(Path(mapped).name)
            if not texture_outputs:
                continue

            aliases = [
                str(name)
                for name in atlas_info.get("tex_names", [])
                if isinstance(name, str)
            ]
            rewritten = self._replace_atlas_page_headers(
                atlas_text,
                texture_outputs,
                aliases=aliases,
            )
            if rewritten != atlas_text:
                atlas_path.write_text(rewritten, encoding="utf-8", newline="")

    @staticmethod
    def _replace_atlas_page_headers(
        text: str,
        page_names: list[str],
        *,
        aliases: list[str] | None = None,
    ) -> str:
        """Replace Spine atlas page headers while preserving line endings.

        ``tex_names`` from the LPK entry gives us stable page aliases in most
        Live2DViewerEX exports.  For exports without aliases, use the atlas
        page metadata layout rather than looking ahead a fixed number of
        lines; region blocks can themselves contain ``size:`` and ``index:``
        fields.
        """

        if not text or not page_names:
            return text
        lines = text.splitlines(keepends=True)
        candidates = _atlas_page_candidates(lines, aliases or [])
        if not candidates and len(page_names) == 1:
            first_nonempty = next(
                (index for index, line in enumerate(lines) if line.strip()),
                None,
            )
            if first_nonempty is not None:
                candidates = [first_nonempty]

        for page_index, line_index in enumerate(candidates[: len(page_names)]):
            original = lines[line_index]
            newline = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
            lines[line_index] = page_names[page_index] + newline
        return "".join(lines)

    def recovery(self, filename, output) -> Tuple[bytes, str]:
        ret = self.decrypt_file(filename)
        suffix = guess_type(ret)
        logger.debug("Recovering %s -> %s", filename, output + suffix)
        output_path = Path(output + suffix)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(ret)
        return ret, suffix

    def getkey(self, file: str):
        if self.lpkType == "STM_1_0" and self.mlve_config.get("encrypt") != "true":
            return 0
        if self.lpkType == "STM_1_0":
            try:
                return genkey(
                    self.mlve_config["id"]
                    + self.config["fileId"]
                    + file
                    + self.config["metaData"]
                )
            except (AttributeError, KeyError, TypeError) as exc:
                raise LpkDecryptError(
                    f"Missing STM_1_0 decryption field for {file!r}",
                    filename=file,
                    cause=exc,
                ) from exc
        elif self.lpkType == "STD2_0":
            return genkey(self.mlve_config["id"] + file)
        elif self.lpkType == "STD_1_0":
            return genkey(self.mlve_config["id"] + file)
        else:
            #return genkey("com.oukaitou.live2d.pro" + self.mlve_config["id"] + "cDaNJnUazx2B4xCYFnAPiYSyd2M=\n")
        #else:
            raise Exception(f"not support type {self.mlve_config['type']}")

    def decrypt_file(self, filename) -> bytes:
        data = self.lpkfile.read(filename)
        return self.decrypt_data(filename, data)

    def decrypt_data(self, filename: str, data: bytes) -> bytes:
        if self.lpkType == "STM_1_0" and self.mlve_config.get("encrypt") != "true":
            return data
        key = self.getkey(filename)
        return decrypt(key, data)
    
    def name_change(self, name: str) -> str:
        #去除name里面的FileReferences_
        name = name.replace("FileReferences_", "")
        return name.replace("\\", "/")
