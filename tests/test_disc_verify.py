import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.dont_write_bytecode = True

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "disc-verify")
_loader = importlib.machinery.SourceFileLoader("disc_verify", SCRIPT)
_spec = importlib.util.spec_from_loader("disc_verify", _loader)
dv = importlib.util.module_from_spec(_spec)
_loader.exec_module(dv)


class FakeReader(dv.Reader):
    """Fails on `bad`, flags `c2`, fails `flaky` sectors a few times."""
    name = "fake"

    def __init__(self, count, bad=(), c2=(), flaky=None, stop_at=None,
                 sector_size=dv.DATA_SECTOR):
        self.count = count
        self.bad, self.c2 = set(bad), set(c2)
        self.flaky = dict(flaky or {})
        self.stop_at = stop_at
        self.sector_size = sector_size

    def read(self, lba, count):
        if self.stop_at is not None and lba >= self.stop_at:
            raise KeyboardInterrupt
        sectors = range(lba, lba + count)
        for s in sectors:
            if s in self.bad:
                raise OSError("EIO")
            if self.flaky.get(s, 0) > 0:
                if count == 1:
                    self.flaky[s] -= 1
                raise OSError("EIO")
        return [s for s in sectors if s in self.c2]


def run_verify(reader, retries=3):
    with redirect_stdout(io.StringIO()):
        return dv.verify(reader, retries, quiet=True)


class VerifyTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(run_verify(FakeReader(1000)), ([], [], 1000, False))

    def test_bad_sectors_found_exactly(self):
        bad = [5, 100, 101, 102, 999]
        result = run_verify(FakeReader(1000, bad=bad))
        self.assertEqual(result, (bad, [], 1000, False))

    def test_flaky_sector_recovers_with_retries(self):
        self.assertEqual(run_verify(FakeReader(200, flaky={70: 2}))[0], [])

    def test_flaky_sector_fails_without_enough_retries(self):
        self.assertEqual(run_verify(FakeReader(200, flaky={70: 2}), 1)[0], [70])

    def test_c2_sectors_reported_separately(self):
        bad, c2, done, _ = run_verify(FakeReader(500, bad=[10], c2=[300, 301]))
        self.assertEqual((bad, c2, done), ([10], [300, 301], 500))

    def test_interrupted(self):
        bad, c2, done, interrupted = run_verify(FakeReader(1000, stop_at=128))
        self.assertTrue(interrupted)
        self.assertEqual(done, 128)

    def test_extents_with_gap(self):
        r = FakeReader(1000, bad=[500])
        r.extents = lambda: [(0, 99), (600, 999)]   # 500 is between sessions
        self.assertEqual(run_verify(r), ([], [], 500, False))

    def test_ranges(self):
        self.assertEqual(dv.ranges([1, 2, 3, 7, 9, 10]),
                         [[1, 3], [7, 7], [9, 10]])
        self.assertEqual(dv.ranges([]), [])


def toc_entry(session, point, lba, data=False, adr=1):
    m, rest = divmod(lba + 150, 60 * 75)
    s, f = divmod(rest, 75)
    control = 0x04 if data else 0x00
    return bytes([session, adr << 4 | control, 0, point, 0, 0, 0, 0, m, s, f])


def full_toc(entries):
    body = bytes([1, 2]) + b"".join(entries)
    return len(body).to_bytes(2, "big") + body


class TocTest(unittest.TestCase):
    def test_audio_cd(self):
        toc = full_toc([
            toc_entry(1, 0xA0, 0), toc_entry(1, 0xA1, 0),
            toc_entry(1, 0xA2, 30000),
            toc_entry(1, 1, 0), toc_entry(1, 2, 10000), toc_entry(1, 3, 20000),
        ])
        tracks = dv.parse_full_toc(toc)
        self.assertEqual([(t.number, t.first, t.last, t.audio) for t in tracks],
                         [(1, 0, 9999, True), (2, 10000, 19999, True),
                          (3, 20000, 29999, True)])

    def test_enhanced_cd_with_data_session(self):
        toc = full_toc([
            toc_entry(1, 1, 0), toc_entry(1, 2, 5000), toc_entry(1, 0xA2, 9000),
            toc_entry(2, 3, 20400, data=True), toc_entry(2, 0xA2, 25000, data=True),
        ])
        tracks = dv.parse_full_toc(toc)
        self.assertEqual([(t.number, t.first, t.last, t.audio) for t in tracks],
                         [(1, 0, 4999, True), (2, 5000, 8999, True),
                          (3, 20400, 24999, False)])

    def test_ignores_non_position_entries(self):
        toc = full_toc([toc_entry(1, 1, 0), toc_entry(1, 0xA2, 100),
                        toc_entry(1, 1, 50, adr=5)])
        self.assertEqual([(t.first, t.last) for t in dv.parse_full_toc(toc)],
                         [(0, 99)])

    def test_track_at_and_locate(self):
        r = dv.CDReader.__new__(dv.CDReader)
        r.tracks = [dv.Track(1, 0, 9999, True), dv.Track(2, 10000, 19999, True)]
        r.starts = [0, 10000]
        self.assertEqual(r.track_at(10000).number, 2)
        self.assertEqual(r.locate(10000 + 75 * 125), "track 2 at 02:05")


class C2Test(unittest.TestCase):
    def test_flags(self):
        size = dv.CDDA_SECTOR + dv.C2_SIZE
        buf = bytearray(size * 3)
        buf[1 * size + dv.CDDA_SECTOR + 10] = 0x80
        buf[2 * size + 5] = 0xFF          # audio data, not a flag
        self.assertEqual(dv.c2_flagged(bytes(buf), 3), [1])


class MiscTest(unittest.TestCase):
    def test_classify(self):
        for t, kind in [("CD-ROM", "cd"), ("CD-R", "cd"), ("DVD-ROM", "dvd"),
                        ("DVD+RW", "dvd"), ("BD-RE", "bd"), (None, None),
                        ("HD-DVD", None)]:
            self.assertEqual(dv.classify(t), kind, t)

    def test_parse_drutil(self):
        out = (" Vendor   Product           Rev\n"
               " HL-DT-ST BD-RE  WH16NS40   1.05\n\n"
               "           Type: BD-ROM               Name: /dev/disk4\n"
               "       Sessions: 1                  Tracks: 1\n")
        self.assertEqual(dv.parse_drutil(out), ("/dev/disk4", "BD-ROM"))
        self.assertEqual(dv.parse_drutil(""), (None, None))

    def test_raw_path(self):
        self.assertEqual(dv.raw_path("/dev/disk4"), "/dev/rdisk4")
        self.assertEqual(dv.raw_path("/tmp/x.iso"), "/tmp/x.iso")

    def test_progress_uses_sector_size(self):
        p = dv.Progress(7500, dv.CDDA_SECTOR, quiet=True)
        line = p.line(750, 0, p.start + 1)
        self.assertIn("1.8 MB", line)          # 750 * 2352 bytes
        self.assertIn("1.8 MB/s", line)

    def test_struct_sizes(self):
        import ctypes
        self.assertEqual(ctypes.sizeof(dv.dk_cd_read_t), 32)
        self.assertEqual(ctypes.sizeof(dv.dk_cd_read_toc_t), 24)
        self.assertEqual(ctypes.sizeof(dv.sg_io_hdr), 88)


class LinuxTest(unittest.TestCase):
    def test_read_cd_cdb(self):
        cdb = dv.read_cd_cdb(0x12345, 24,
                             dv.CD_AREA_USER | dv.CD_AREA_ERROR_FLAGS,
                             dv.CD_TYPE_CDDA)
        self.assertEqual(cdb, bytes([0xBE, 0x04, 0x00, 0x01, 0x23, 0x45,
                                     0x00, 0x00, 24, 0x12, 0, 0]))
        cdb = dv.read_cd_cdb(0, 1, dv.CD_AREA_USER, dv.CD_TYPE_MODE2_FORM2)
        self.assertEqual(cdb[1], 5 << 2)
        self.assertEqual(cdb[9], 0x10)

    def test_sense_message(self):
        fixed = bytes([0x70, 0, 0x03] + [0] * 9 + [0x11, 0x05])
        self.assertIn("3/11/05", dv.sense_message(fixed))
        desc = bytes([0x72, 0x05, 0x6F, 0x03])
        self.assertIn("5/6F/03", dv.sense_message(desc))
        self.assertEqual(dv.sense_message(b""), "drive reported an error")

    def test_profiles_classify(self):
        for profile, kind in [(0x08, "cd"), (0x0A, "cd"), (0x10, "dvd"),
                              (0x2B, "dvd"), (0x40, "bd"), (0x43, "bd")]:
            self.assertEqual(dv.classify(dv.MMC_PROFILES[profile]), kind)


class MainTest(unittest.TestCase):
    def run_main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["disc-verify", *argv]), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                dv.main()
        return cm.exception.code, out.getvalue()

    def test_image_ok(self):
        with tempfile.NamedTemporaryFile(suffix=".iso") as f:
            f.write(os.urandom(dv.DATA_SECTOR * 300))
            f.flush()
            code, out = self.run_main("--device", f.name, "-q")
        self.assertEqual(code, 0)
        self.assertIn("OK: all 300 sectors", out)

    def test_no_disc(self):
        with mock.patch.object(dv, "find_disc", return_value=(None, None)):
            code, _ = self.run_main()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
