# -*- coding: utf-8 -*-
"""Build an opkg IPK for JellyDream Modern.

The package data root inside the IPK is
usr/lib/enigma2/python/Plugins/Extensions/JellyDreamModern.
The filename follows
enigma2-plugin-extensions-jellydreammodern_<version>_all.ipk.
"""

import io
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
