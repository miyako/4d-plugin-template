#!/usr/bin/env python3
"""Inspect vendored MSVC static libraries (.lib) without MSVC; fail closed.

Reads the ar archive directly and reports, per library:

  members  archive members (excluding the linker and long-name members)
  ltcg     LTCG (/GL) objects: ANON_OBJECT_HEADER with Sig1=0, Sig2=0xFFFF and
           Version>=1. They are bound to the exact compiler that produced them.
  import   short import headers (Version 0): the .lib is a DLL import library.
  crt      RuntimeLibrary= values from /FAILIFMISMATCH linker directives.
  msc      _MSC_VER= values from the same directives (1900 = v140).

Checks (each failure is reported and makes the exit status 1):

  default            ltcg == 0 and import == 0 (static, toolset-independent objects)
  --allow-import     accept import libraries (the DLL must then ship separately)
  --crt VALUE        every RuntimeLibrary= directive must equal VALUE
                     (MT_StaticRelease for /MT, MD_DynamicRelease for /MD)
  --symbol NAME      the mangled NAME must occur in the archive (repeatable);
                     use a symbol only the vendored header version declares

This is static inspection evidence only: it does not link, load or run anything.
"""

import argparse
import re
import sys
from pathlib import Path

AR_MAGIC = b"!<arch>\n"
HEADER_SIZE = 60


class ArchiveError(ValueError):
    pass


def inspect(data):
    if not data.startswith(AR_MAGIC):
        raise ArchiveError("not an ar archive")
    pos, members, ltcg, imports = len(AR_MAGIC), 0, 0, 0
    while pos < len(data):
        if pos + HEADER_SIZE > len(data):
            raise ArchiveError(f"truncated member header at offset {pos}")
        header = data[pos:pos + HEADER_SIZE]
        if header[58:60] != b"`\n":
            raise ArchiveError(f"bad member header at offset {pos}")
        try:
            size = int(header[48:58].strip() or b"0")
        except ValueError as exc:
            raise ArchiveError(f"bad member size at offset {pos}") from exc
        body = data[pos + HEADER_SIZE:pos + HEADER_SIZE + size]
        if len(body) != size:
            raise ArchiveError(f"truncated member at offset {pos}")
        name = header[:16]
        if not (name.startswith(b"/ ") or name.startswith(b"// ")):
            members += 1
            if len(body) >= 6 and body[0:2] == b"\x00\x00" and body[2:4] == b"\xff\xff":
                if int.from_bytes(body[4:6], "little") == 0:
                    imports += 1
                else:
                    ltcg += 1
        pos += HEADER_SIZE + size + (size & 1)
    text = data.decode("latin-1")
    return {
        "members": members,
        "ltcg": ltcg,
        "import": imports,
        "crt": sorted(set(re.findall(r"RuntimeLibrary=([A-Za-z_]+)", text))),
        "msc": sorted(set(re.findall(r"_MSC_VER=(\d+)", text))),
    }


def problems(data, info, allow_import=False, crt=None, symbols=()):
    found = []
    if info["ltcg"]:
        found.append(f"{info['ltcg']} LTCG (/GL) objects: rebuild without /GL for the plugin's toolset")
    if info["import"] and not allow_import:
        found.append(f"{info['import']} import members: this is a DLL import library, not a static library")
    if crt is not None:
        if not info["crt"]:
            found.append(f"no RuntimeLibrary directive found; cannot confirm {crt}")
        elif info["crt"] != [crt]:
            found.append(f"RuntimeLibrary {','.join(info['crt'])} does not match {crt}")
    for symbol in symbols:
        if symbol.encode("latin-1") not in data:
            found.append(f"missing symbol {symbol}")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("libs", nargs="+", type=Path)
    parser.add_argument("--allow-import", action="store_true")
    parser.add_argument("--crt")
    parser.add_argument("--symbol", action="append", default=[])
    args = parser.parse_args(argv)
    status = 0
    for lib in args.libs:
        try:
            data = lib.read_bytes()
            info = inspect(data)
        except (OSError, ArchiveError) as exc:
            print(f"{lib}: ERROR {exc}")
            status = 1
            continue
        print(f"{lib}: members={info['members']} ltcg={info['ltcg']} import={info['import']} "
              f"crt={','.join(info['crt']) or '-'} msc={','.join(info['msc']) or '-'}")
        for problem in problems(data, info, args.allow_import, args.crt, args.symbol):
            print(f"  FAIL {problem}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
