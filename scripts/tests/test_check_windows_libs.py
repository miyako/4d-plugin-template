"""Synthetic ar archives for check_windows_libs; no real MSVC libraries."""

import io
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_windows_libs as cwl

MOVE_ASSIGN = "??4Value@Json@@QEAAAEAV01@$$QEAV01@@Z"


def member(name, body):
    header = name.ljust(16).encode() + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
    header += b"644".ljust(8) + str(len(body)).encode().ljust(10) + b"`\n"
    assert len(header) == 60
    return header + body + (b"\n" if len(body) & 1 else b"")


def coff(directives=b"", payload=b""):
    # a plain COFF object: Machine=AMD64 (0x8664), then arbitrary bytes
    return b"\x64\x86" + b"\x00" * 18 + directives + payload


def anon(version):
    # ANON_OBJECT_HEADER / IMPORT_OBJECT_HEADER prefix: Sig1=0, Sig2=0xFFFF, Version
    return b"\x00\x00\xff\xff" + version.to_bytes(2, "little") + b"\x64\x86" + b"\x00" * 24


def archive(*bodies, linker=True):
    data = cwl.AR_MAGIC
    if linker:
        data += member("/", b"\x00\x00\x00\x00")
        data += member("//", b"long_object_name.obj/\n")
    for i, body in enumerate(bodies):
        data += member(f"obj{i}.obj/", body)
    return data


MT = b" /FAILIFMISMATCH:\"_MSC_VER=1900\" /FAILIFMISMATCH:\"RuntimeLibrary=MT_StaticRelease\" "
MD = b" /FAILIFMISMATCH:\"_MSC_VER=1900\" /FAILIFMISMATCH:\"RuntimeLibrary=MD_DynamicRelease\" "


class CheckWindowsLibsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / (".cwl-fixture-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def run_main(self, data, *args):
        lib = self.root / "x.lib"
        lib.write_bytes(data)
        out = io.StringIO()
        with redirect_stdout(out):
            status = cwl.main([str(lib), *args])
        return status, out.getvalue()

    def test_static_mt_library_passes(self):
        data = archive(coff(MT, MOVE_ASSIGN.encode()), coff(MT))
        info = cwl.inspect(data)
        self.assertEqual((info["members"], info["ltcg"], info["import"]), (2, 0, 0))
        self.assertEqual(info["crt"], ["MT_StaticRelease"])
        self.assertEqual(info["msc"], ["1900"])
        status, out = self.run_main(data, "--crt", "MT_StaticRelease", "--symbol", MOVE_ASSIGN)
        self.assertEqual(status, 0, out)
        self.assertNotIn("FAIL", out)

    def test_ltcg_objects_fail(self):
        status, out = self.run_main(archive(anon(1), anon(2), coff(MT)))
        self.assertEqual(status, 1)
        self.assertIn("ltcg=2", out)
        self.assertIn("LTCG", out)

    def test_import_library_fails_unless_allowed(self):
        data = archive(anon(0), anon(0))
        status, out = self.run_main(data)
        self.assertEqual(status, 1)
        self.assertIn("import=2", out)
        status, out = self.run_main(data, "--allow-import")
        self.assertEqual(status, 0, out)

    def test_crt_mismatch_and_missing_directive_fail(self):
        status, out = self.run_main(archive(coff(MD)), "--crt", "MT_StaticRelease")
        self.assertEqual(status, 1)
        self.assertIn("does not match", out)
        status, out = self.run_main(archive(coff(MT), coff(MD)), "--crt", "MT_StaticRelease")
        self.assertEqual(status, 1)
        status, out = self.run_main(archive(coff()), "--crt", "MT_StaticRelease")
        self.assertEqual(status, 1)
        self.assertIn("no RuntimeLibrary directive", out)

    def test_header_version_symbol_missing_fails(self):
        # 1.8.x-style archive: by-value assignment only, right CRT
        status, out = self.run_main(archive(coff(MT, b"??4Value@Json@@QEAAAEAV01@V01@@Z")),
                                    "--crt", "MT_StaticRelease", "--symbol", MOVE_ASSIGN)
        self.assertEqual(status, 1)
        self.assertIn("missing symbol", out)

    def test_malformed_archives_fail_closed(self):
        for data in (b"not an archive",
                     cwl.AR_MAGIC + b"short",
                     archive(coff())[:-5],
                     cwl.AR_MAGIC + member("a.obj/", b"xx")[:58] + b"XX" + b"xx"):
            with self.subTest(data=data[:24]):
                status, out = self.run_main(data)
                self.assertEqual(status, 1)
                self.assertIn("ERROR", out)

    def test_missing_file_fails(self):
        out = io.StringIO()
        with redirect_stdout(out):
            status = cwl.main([str(self.root / "absent.lib")])
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
