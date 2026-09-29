from hashlib import md5
import json
import os
import re
import filetype
from filetype.types import Type

def hashed_filename(s: str) -> str:
    t = md5()
    t.update(s.encode())
    return t.hexdigest()

def normalize(s: str) -> str:
    s = ''.join(c for c in s if ord(c) >= 32 or c == ' ')
    s = re.sub(r'[\\/<>:"|?*]', '', s)
    if not s.strip():
        s = "unnamed"
    return s

def safe_mkdir(s: str):
    """
    Safely create a directory
    """
    # Create the directory
    os.makedirs(s, exist_ok=True)
    print(f"Created directory: {s}")

def genkey(s: str) -> int:
    ret = 0
    for i in s:
        ret = (ret * 31 + ord(i)) & 0xffffffff
    if ret & 0x80000000:
        ret = ret | 0xffffffff00000000
    return ret

def decrypt(key: int, data: bytes) -> bytes:
    ret = []
    for slice in [data[i:i+1024] for i in range(0, len(data), 1024)]:
        tmpkey = key
        for i in slice:
            tmpkey = (65535 & 2531011 + 214013 * tmpkey >> 16) & 0xffffffff
            ret.append((tmpkey & 0xff) ^ i)
    return bytes(ret)

match_rule = re.compile(r"[0-9a-f]{32}\.bin3?", re.IGNORECASE)
def is_encrypted_file(s: str) -> bool:
    if type(s) != str:
        return False
    if match_rule.fullmatch(s) != None:
        return True
    return False

# find all enc_file in s
def find_encrypted_file(s: str) -> str:
    files = re.findall(match_rule, s)
    if files == []:
        return None
    return files[0]

def get_encrypted_file(s: str):
    if type(s) != str:
        return None
    if s.startswith("change_cos"):
        filename = s[len("change_cos "):]
    else:
        filename = s
    if not is_encrypted_file(filename):
        return None
    return filename


def travels_dict(dic: dict):
    for k in dic:
        if type(dic[k]) == dict:
            for p, v in travels_dict(dic[k]):
                yield f"{k}_{p}", v
        elif type(dic[k]) == list:
            for p, v in travels_list(dic[k]):
                yield f"{k}_{p}", v
        else:
            yield str(k), dic[k]
        
def travels_list(vals: list):
    for i in range(len(vals)):
        if type(vals[i]) == dict:
            for p, v in travels_dict(vals[i]):
                yield f"{i}_{p}", v
        elif type(vals[i]) == list:
            for p, v in travels_list(vals[i]):
                yield f"{i}_{p}", v
        else:
            yield str(i), vals[i]


class Moc3(Type):
    MIME = "application/moc3"
    EXTENSION = "moc3"
    def __init__(self):
        super(Moc3, self).__init__(mime=Moc3.MIME, extension=Moc3.EXTENSION)
    
    def match(self, buf):
        return len(buf) > 3 and buf.startswith(b"MOC3")

class Moc(Type):
    MIME = "application/moc"
    EXTENSION = "moc"
    def __init__(self):
        super(Moc, self).__init__(mime=Moc.MIME, extension=Moc.EXTENSION)
    
    def match(self, buf):
        return len(buf) > 3 and buf.startswith(b"moc")

filetype.add_type(Moc3())
filetype.add_type(Moc())

def guess_type(data: bytes):
    """Return a useful extension for an extracted encrypted payload.

    ``filetype`` knows about common images and audio, but Live2DViewerEX
    stores Spine atlas and skeleton files without their original extension.
    Keep the normal detector first and use small, conservative content
    signatures for those two text/binary formats afterwards.
    """
    ftype = filetype.guess(data)
    if ftype != None:
        return "." + ftype.extension
    try:
        json.loads(data.decode("utf8"))
        return ".json"
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        pass
    if is_spine_atlas_data(data):
        return ".atlas"
    if is_spine_skeleton_data(data):
        return ".skel"
    return ""


def is_spine_atlas_data(data: bytes) -> bool:
    """Return whether *data* looks like a text Spine ``.atlas`` file.

    A page header is followed by metadata such as ``size:`` and
    ``format:``.  Requiring those fields avoids treating arbitrary text
    payloads (motion files, captions, etc.) as atlases.
    """

    if not isinstance(data, (bytes, bytearray)) or not data:
        return False
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    raw_lines = text.splitlines()
    lines = [line.strip().lower() for line in raw_lines]
    if len(lines) < 2:
        return False
    page_metadata = (
        "size:",
        "format:",
        "filter:",
        "repeat:",
        "pma:",
        "scale:",
        "minificationfilter:",
        "magnificationfilter:",
    )
    region_metadata = (
        "rotate:",
        "xy:",
        "bounds:",
        "orig:",
        "offset:",
        "index:",
    )
    has_page_metadata = any(line.startswith(page_metadata) for line in lines)
    has_region_metadata = any(
        line.startswith(region_metadata)
        for line in lines
    )
    # Keep at least one unindented page/region name.  JSON and ordinary text
    # files use quoted keys or free-form prose, while atlas files have a
    # page name followed by the metadata vocabulary above.
    has_unindented_name = any(
        raw_line and not raw_line[0].isspace() and stripped
        and not stripped.startswith(page_metadata)
        and not stripped.startswith(region_metadata)
        for raw_line, stripped in zip(raw_lines, lines)
    )
    return has_unindented_name and (has_page_metadata or has_region_metadata)


def is_spine_skeleton_data(data: bytes) -> bool:
    """Return whether *data* has the characteristic Spine binary header.

    Spine binary files begin with a variable-length hash and an ASCII
    runtime version (for example ``2.1.27`` or ``3.8.96``).  The version is
    bounded to the first 128 bytes and is accompanied by a later ``root`` or
    path/string marker, which keeps this heuristic conservative for generic
    binary assets.
    """

    if not isinstance(data, (bytes, bytearray)) or len(data) < 8:
        return False
    head = bytes(data[:128])
    version = re.search(rb"(?<![0-9])(?:[0-9]+\.){1,3}[0-9]+(?![0-9])", head)
    if not version:
        return False
    tail = bytes(data[:1024])
    # Spine's binary string writer places a length marker (0x07 for the
    # usual seven-byte version string) immediately before this version.  A
    # few exports omit an obvious ``root``/``/spine/`` marker, so accept that
    # canonical prefix as well.
    marker_before_version = version.start() > 0 and head[version.start() - 1] == 0x07
    return marker_before_version or b"root" in tail or b"/spine/" in tail or b"./spine" in tail
