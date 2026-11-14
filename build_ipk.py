# -*- coding: utf-8 -*-
"""Build an opkg IPK for JellyDream Modern.

The package data root inside the IPK is
usr/lib/enigma2/python/Plugins/Extensions/JellyDreamModern.
The filename follows
enigma2-plugin-extensions-jellydreammodern_<version>_all.ipk.
"""

import gettext
import io
import shutil
import struct
import subprocess
import tarfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
CONTROL_DIR = REPO_ROOT / "control"
PLUGIN_DIR = (
    REPO_ROOT
    / "JellyDreamModern"
    / "usr"
    / "lib"
    / "enigma2"
    / "python"
    / "Plugins"
    / "Extensions"
    / "JellyDreamModern"
)
INSTALL_PREFIX = "usr/lib/enigma2/python/Plugins/Extensions/JellyDreamModern"
CONSTANTS_FILE = PLUGIN_DIR / "constants.py"
DIST_DIR = REPO_ROOT / "dist"

SKIP_DIR_NAMES = {"__pycache__", ".cursor", ".git"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
CONTROL_SCRIPTS = ("preinst", "postinst", "prerm", "postrm")
CATALOG_DOMAIN = "JellyDreamModern"
MIN_CATALOG_ENTRIES = 390
_PO_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
}


def unescape_po_string(value):
    """Undo PO quotes without unicode_escape, which corrupts UTF-8."""
    output = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            output.append(character)
            index += 1
            continue
        if index + 1 >= len(value):
            raise ValueError("PO string ends with a backslash")
        escaped = value[index + 1]
        if escaped in _PO_ESCAPES:
            output.append(_PO_ESCAPES[escaped])
            index += 2
            continue
        if escaped in "01234567":
            digits = escaped
            index += 2
            while index < len(value) and len(digits) < 3 and value[index] in "01234567":
                digits += value[index]
                index += 1
            output.append(chr(int(digits, 8)))
            continue
        raise ValueError("Unsupported PO escape \\%s" % escaped)
    return "".join(output)


def quoted_po_text(line):
    start = line.find('"')
    end = line.rfind('"')
    if start < 0 or end <= start:
        raise ValueError("PO line has no quoted string: %s" % line)
    return line[start + 1:end]


def parse_po(text):
    messages = {}
    msgid = None
    msgstr = None
    field = None

    def finish():
        nonlocal msgid, msgstr, field
        if msgid is not None:
            if msgstr is None:
                raise ValueError("msgid without msgstr")
            messages[msgid] = msgstr
        msgid = None
        msgstr = None
        field = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            finish()
            continue
        if line.startswith("#"):
            continue
        if line.startswith("msgid "):
            if field == "msgstr":
                finish()
            field = "msgid"
            msgid = unescape_po_string(quoted_po_text(line[len("msgid "):]))
            msgstr = None
            continue
        if line.startswith("msgstr "):
            field = "msgstr"
            msgstr = unescape_po_string(quoted_po_text(line[len("msgstr "):]))
            continue
        if line.startswith('"') and field == "msgid":
            msgid += unescape_po_string(quoted_po_text(line))
            continue
        if line.startswith('"') and field == "msgstr":
            msgstr += unescape_po_string(quoted_po_text(line))
            continue
        raise ValueError("Unrecognized PO line: %s" % raw_line)

    finish()
    header = messages.get("")
    if not header or "charset=UTF-8" not in header:
        raise ValueError("PO catalog has no UTF-8 header entry")
    if len(messages) - 1 < MIN_CATALOG_ENTRIES:
        raise ValueError(
            "PO catalog has %s entries, expected at least %s"
            % (len(messages) - 1, MIN_CATALOG_ENTRIES)
        )
    return messages


def write_mo(messages, path):
    """Write a GNU MO catalog whose first entry is the empty header."""
    items = sorted(
        ((key.encode("utf-8"), value.encode("utf-8")) for key, value in messages.items()),
        key=lambda item: item[0],
    )
    count = len(items)
    header_size = 28
    key_table = header_size
    value_table = key_table + count * 8
    cursor = value_table + count * 8
    key_index = []
    value_index = []
    key_blobs = []
    value_blobs = []
    for key, value in items:
        key_index.append((len(key), cursor))
        key_blobs.append(key + b"\x00")
        cursor += len(key) + 1
    for key, value in items:
        value_index.append((len(value), cursor))
        value_blobs.append(value + b"\x00")
        cursor += len(value) + 1

    output = bytearray()
    output += struct.pack(
        "<7I",
        0x950412DE,
        0,
        count,
        key_table,
        value_table,
        0,
        0,
    )
    for length, offset in key_index:
        output += struct.pack("<II", length, offset)
    for length, offset in value_index:
        output += struct.pack("<II", length, offset)
    for blob in key_blobs:
        output += blob
    for blob in value_blobs:
        output += blob
    if not key_blobs or key_blobs[0] != b"\x00":
        raise ValueError("MO catalog does not start with the empty msgid")
    path.write_bytes(output)


def compile_po_file(po_path, mo_path):
    msgfmt = shutil.which("msgfmt")
    if msgfmt:
        subprocess.check_call(
            [msgfmt, "-c", "-o", str(mo_path), str(po_path)],
        )
        return
    write_mo(parse_po(po_path.read_text(encoding="utf-8")), mo_path)


def verify_catalog(locale_dir, language):
    translation = gettext.translation(
        CATALOG_DOMAIN,
        localedir=str(locale_dir),
        languages=[language],
        fallback=False,
    )
    content_type = translation.info().get("content-type", "")
    if "charset=UTF-8" not in content_type:
        raise SystemExit("%s catalog has no UTF-8 charset: %s" % (language, content_type))
    entry_count = len(translation._catalog)
    if entry_count < MIN_CATALOG_ENTRIES:
        raise SystemExit(
            "%s catalog has %s entries, expected at least %s"
            % (language, entry_count, MIN_CATALOG_ENTRIES)
        )
    if language == "en" and translation.gettext("Filme") != "Movies":
        raise SystemExit("English catalog did not translate Filme")


def compile_catalogs():
    locale_dir = PLUGIN_DIR / "locale"
    po_files = sorted(locale_dir.glob("*/LC_MESSAGES/%s.po" % CATALOG_DOMAIN))
    if len(po_files) != 10:
        raise SystemExit("Expected 10 PO catalogs, found %s" % len(po_files))
    for po_path in po_files:
        mo_path = po_path.with_suffix(".mo")
        compile_po_file(po_path, mo_path)
        verify_catalog(locale_dir, po_path.parents[1].name)


def read_control_version():
    control_path = CONTROL_DIR / "control"
    text = control_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("control/control has no Version line")


def read_constants_version():
    text = CONSTANTS_FILE.read_text(encoding="utf-8")
    marker = 'JELLYDREAM_VERSION = "'
    start = text.find(marker)
    if start < 0:
        raise SystemExit("JELLYDREAM_VERSION was not found in constants.py")
    start += len(marker)
    end = text.find('"', start)
    return text[start:end]


def unix_bytes(path):
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def add_bytes(archive, arcname, data, mode):
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(time.time())
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.type = tarfile.REGTYPE
    archive.addfile(info, io.BytesIO(data))


def add_dir(archive, arcname):
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = int(time.time())
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    archive.addfile(info)


def build_control_tar():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        add_dir(archive, "./")
        control_text = unix_bytes(CONTROL_DIR / "control")
        if not control_text.endswith(b"\n"):
            control_text += b"\n"
        add_bytes(archive, "./control", control_text, 0o644)
        for script_name in CONTROL_SCRIPTS:
            script_path = CONTROL_DIR / script_name
            if script_path.is_file():
                add_bytes(archive, "./" + script_name, unix_bytes(script_path), 0o755)
    return buffer.getvalue()


def build_data_tar():
    buffer = io.BytesIO()
    directories = set()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        add_dir(archive, "./")
        for folder in Path(INSTALL_PREFIX).parts:
            directories.add(folder)
        current = ""
        for part in Path(INSTALL_PREFIX).parts:
            current = part if not current else current + "/" + part
            add_dir(archive, "./" + current)

        for path in sorted(PLUGIN_DIR.rglob("*")):
            relative = path.relative_to(PLUGIN_DIR).as_posix()
            if any(part in SKIP_DIR_NAMES for part in path.relative_to(PLUGIN_DIR).parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            arcname = "./" + INSTALL_PREFIX + "/" + relative
            if path.is_dir():
                add_dir(archive, arcname)
            elif path.is_file():
                add_bytes(archive, arcname, path.read_bytes(), 0o644)
    return buffer.getvalue()


def write_ar(path, members):
    with path.open("wb") as archive:
        archive.write(b"!<arch>\n")
        for name, data in members:
            header_name = (name + "/").encode("ascii").ljust(16, b" ")
            header = b"".join((
                header_name,
                str(int(time.time())).encode("ascii").ljust(12, b" "),
                b"0".ljust(6, b" "),
                b"0".ljust(6, b" "),
                b"100644".ljust(8, b" "),
                str(len(data)).encode("ascii").ljust(10, b" "),
                b"`\n",
            ))
            if len(header) != 60:
                raise SystemExit("Invalid ar header for %s" % name)
            archive.write(header)
            archive.write(data)
            if len(data) % 2:
                archive.write(b"\n")


def main():
    compile_catalogs()
    control_version = read_control_version()
    constants_version = read_constants_version()
    if control_version != constants_version:
        raise SystemExit(
            "Version mismatch: control=%s constants.py=%s"
            % (control_version, constants_version)
        )

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    ipk_name = "enigma2-plugin-extensions-jellydreammodern_%s_all.ipk" % control_version
    ipk_path = DIST_DIR / ipk_name

    debian_binary = b"2.0\n"
    write_ar(
        ipk_path,
        (
            ("debian-binary", debian_binary),
            ("control.tar.gz", build_control_tar()),
            ("data.tar.gz", build_data_tar()),
        ),
    )
    print(ipk_path)


if __name__ == "__main__":
    main()
