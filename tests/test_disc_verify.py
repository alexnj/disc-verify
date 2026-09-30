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
    """Fails on `bad`, flags `c2`, fails `flaky` sectors a few times.

    Time is simulated: each read advances `now` by 0.01 s, or by `slow_time`
    if it touches a `slow` sector. After reading a `stuck_at` sector every
    read fails, like a drive that has locked up."""
    name = "fake"

    def __init__(self, count, bad=(), c2=(), flaky=None, stop_at=None,
                 slow=(), slow_time=5.0, stuck_at=None,
                 sector_size=dv.DATA_SECTOR):
        self.count = count
        self.bad, self.c2, self.slow = set(bad), set(c2), set(slow)
        self.flaky = dict(flaky or {})
        self.stop_at = stop_at
        self.slow_time = slow_time
        self.stuck_at, self.stuck = stuck_at, False
        self.sector_size = sector_size
        self.now = 0.0
        self.reads = 0
        self.slow_reads = 0
        self.log = []

    def clock(self):
        return self.now

    def read(self, lba, count):
        if self.stop_at is not None and lba >= self.stop_at:
            raise KeyboardInterrupt
        sectors = range(lba, lba + count)
        self.reads += 1
        self.log.append((lba, count))
        slow = any(s in self.slow for s in sectors)
        self.slow_reads += slow
        self.now += self.slow_time if slow or self.stuck else 0.01
        if self.stuck:
            raise OSError("timeout")
        if self.stuck_at is not None and self.stuck_at in sectors:
            self.stuck = True
        for s in sectors:
            if s in self.bad:
                raise OSError("EIO")
            if self.flaky.get(s, 0) > 0:
                if count == 1:
                    self.flaky[s] -= 1
                raise OSError("EIO")
        return [s for s in sectors if s in self.c2]


def run_verify(reader, retries=3, thorough=False, cells=()):
    out = io.StringIO()
    with redirect_stdout(out):
        r = dv.verify(reader, retries, quiet=True, thorough=thorough,
                      cells=cells, clock=reader.clock)
    reader.output = out.getvalue()
    return r


class VerifyTest(unittest.TestCase):
    def test_clean(self):
        r = run_verify(FakeReader(1000))
        self.assertEqual((r.bad, r.c2, r.skipped, r.done, r.stopped),
                         ([], [], [], 1000, None))

    def test_bad_sectors_found_exactly(self):
        bad = [5, 100, 101, 102, 999]
        r = run_verify(FakeReader(1000, bad=bad))
        self.assertEqual((r.bad, r.c2, r.done), (bad, [], 1000))

    def test_flaky_sector_recovers_with_retries(self):
        self.assertEqual(run_verify(FakeReader(200, flaky={70: 2})).bad, [])

    def test_flaky_sector_fails_without_enough_retries(self):
        self.assertEqual(run_verify(FakeReader(200, flaky={70: 2}), 1).bad,
                         [70])

    def test_c2_sectors_reported_separately(self):
        r = run_verify(FakeReader(500, bad=[10], c2=[300, 301]))
        self.assertEqual((r.bad, r.c2, r.done), ([10], [300, 301], 500))

    def test_interrupted(self):
        r = run_verify(FakeReader(1000, stop_at=128))
        self.assertEqual(r.stopped, "interrupted")
        self.assertEqual(r.done, 128)

    def test_extents_with_gap(self):
        r = FakeReader(1000, bad=[500])
        r.extents = lambda: [(0, 99), (600, 999)]   # 500 is between sessions
        res = run_verify(r)
        self.assertEqual((res.bad, res.done), ([], 500))

    def test_slow_region_skipped(self):
        slow = range(20000, 50000)
        r = FakeReader(200000, slow=slow, bad=[60000])
        res = run_verify(r)
        self.assertIsNone(res.stopped)
        self.assertEqual(res.bad, [60000])
        (first, last), = res.skipped
        # The skip starts after the three slow chunks that detect the region
        # and ends at most one probe step past it.
        self.assertTrue(20000 < first <= 20000 + 3 * r.chunk, first)
        self.assertTrue(50000 <= last + 1 <= 50000 + dv.MAX_JUMP, last)
        self.assertEqual(res.done, 200000)
        self.assertLess(r.slow_reads, 10)   # vs ~470 to read it all

    def test_patchy_slow_region(self):
        # Quick patches inside the slow region mustn't lure it back in.
        slow = set(range(20000, 50000)) - {s for p in range(21000, 50000, 3000)
                                          for s in range(p, p + 200)}
        r = FakeReader(200000, slow=slow)
        res = run_verify(r)
        self.assertEqual(len(res.skipped), 1)
        self.assertTrue(res.skipped[0][1] >= 49999)
        self.assertLess(r.slow_reads, 12)

    def test_slow_cell_skipped_whole(self):
        cells = [(0, 9999), (10000, 39999), (40000, 99999)]
        r = FakeReader(100000, slow=range(15000, 40000))
        res = run_verify(r, cells=cells)
        (first, last), = res.skipped
        self.assertTrue(15000 < first <= 15000 + 4 * r.chunk, first)
        self.assertEqual(last, 39999)
        self.assertEqual(r.slow_reads, 3)

    def test_huge_slow_cell_skipped_in_steps(self):
        cells = [(0, 9999), (10000, 399999)]
        r = FakeReader(400000, slow=range(15000, 150000))
        res = run_verify(r, cells=cells)
        self.assertLessEqual(res.skipped[0][1] - res.skipped[0][0],
                             dv.MAX_JUMP)
        self.assertTrue(149999 <= res.skipped[-1][1] <= 150000 + dv.MAX_JUMP)
        self.assertLess(r.slow_reads, 10)

    def test_slow_failures_join_the_skip(self):
        # Chunks that failed slowly in the streak that triggered a skip
        # aren't rechecked sector by sector (each would take seconds).
        r = FakeReader(20000, bad=range(5000, 9000), slow=range(5000, 9000))
        res = run_verify(r)
        self.assertEqual(res.bad, [])
        (first, last), = res.skipped
        self.assertTrue(4900 < first <= 5000, first)
        self.assertTrue(last >= 8999, last)
        self.assertEqual(res.done, 20000)
        self.assertLess(r.slow_reads, 12)

    def test_stuck_check_reads_latest_good_sector(self):
        r = FakeReader(20000, bad=[10000], slow=[10000])
        run_verify(r)
        # The read right after each (slow) failure is the stuck check.
        checks = [r.log[i + 1][0] for i, (lba, n) in enumerate(r.log[:-1])
                  if lba <= 10000 < lba + n]
        self.assertTrue(checks)
        self.assertTrue(all(abs(lba - 10000) < 128 for lba in checks), checks)

    def test_skip_messages_grouped(self):
        # Many small slow cells with good cells between them (like a decoy
        # title): one message for the stretch, but the report keeps the
        # separate areas. A later, separate slow area gets its own message.
        cells = [(i, i + 999) for i in range(0, 200000, 1000)]
        slow = {s for i in range(50000, 70000, 2000) for s in range(i, i + 1000)}
        slow |= set(range(150000, 151000))
        r = FakeReader(200000, slow=slow)
        res = run_verify(r, cells=cells)
        notes = [l for l in r.output.splitlines() if "skipped" in l]
        self.assertEqual(len(notes), 2, notes)
        self.assertIn("in 10 slow areas between sectors", notes[0])
        self.assertIn("skipped sectors 150", notes[1])
        self.assertEqual(len(res.skipped), 11)

    def test_adjacent_slow_cells(self):
        # Two slow cells in a row: the second is skipped on its first slow
        # read.
        cells = [(0, 9999), (10000, 19999), (20000, 29999), (30000, 99999)]
        r = FakeReader(100000, slow=range(12000, 30000))
        res = run_verify(r, cells=cells)
        self.assertEqual([b for a, b in res.skipped], [19999, 29999])
        self.assertEqual(r.slow_reads, 4)

    def test_slow_region_read_when_thorough(self):
        res = run_verify(FakeReader(20000, slow=range(5000, 6000)),
                         thorough=True)
        self.assertEqual((res.bad, res.skipped, res.done), ([], [], 20000))

    def test_slow_bad_sectors_skipped_in_recheck(self):
        # A chunk whose every sector fails slowly: stop rechecking it after
        # a few sectors rather than waiting out every one.
        bad = range(640, 704)
        r = FakeReader(2000, bad=bad, slow=bad)
        res = run_verify(r, retries=0)
        self.assertEqual(res.bad, [640, 641, 642])
        self.assertEqual(res.skipped, [(643, 703)])

    def test_stuck_drive_stops(self):
        r = FakeReader(100000, stuck_at=5000)
        res = run_verify(r)
        self.assertEqual(res.stopped, "stuck")
        self.assertEqual(res.bad, [])       # not blamed on the disc
        self.assertLess(r.reads, 100)
        self.assertEqual(res.resume, 5056)   # the first read that failed

    def test_stop_reports_unchecked_failures(self):
        # Chunks that failed but weren't rechecked yet are reported as
        # unreadable, and resuming continues where pass 1 got to rather
        # than going back into them.
        r = FakeReader(1000, bad=[100], stop_at=640)
        res = run_verify(r)
        self.assertEqual(res.resume, 640)
        self.assertEqual(res.bad, list(range(64, 128)))

    def test_stuck_in_recheck_has_no_resume_point(self):
        # Stuck while rechecking failures (pass 1 finished): nothing is
        # left to resume, and the unchecked rest counts as unreadable.
        r = FakeReader(1000, bad=[100, 700], stuck_at=None)
        r.slow = {700}
        orig = r.read

        def read(lba, count):
            if count == 1 and lba == 700:
                r.stuck = True
            return orig(lba, count)
        r.read = read
        res = run_verify(r)
        self.assertEqual(res.stopped, "stuck")
        self.assertIsNone(res.resume)
        self.assertEqual(res.bad, [100] + list(range(700, 704)))

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


def iso_image(files):
    """A minimal ISO 9660 image with a VIDEO_TS directory holding `files`
    ({name: bytes}); each file starts on its own sector. Returns (image,
    {name: lba})."""
    S = dv.DATA_SECTOR

    def record(name, lba, size, is_dir=False):
        name = name.encode()
        n = 33 + len(name) + (len(name) + 1) % 2
        rec = bytearray(n)
        rec[0] = n
        rec[2:6] = lba.to_bytes(4, "little")
        rec[10:14] = size.to_bytes(4, "little")
        rec[25] = 2 if is_dir else 0
        rec[32] = len(name)
        rec[33:33 + len(name)] = name
        return bytes(rec)

    lbas, lba = {}, 20
    for name, data in files.items():
        lbas[name] = lba
        lba += (len(data) + S - 1) // S
    img = bytearray(lba * S)
    for name, data in files.items():
        img[lbas[name] * S:lbas[name] * S + len(data)] = data
    ts = b"".join(record(f"{n};1", lbas[n], len(d)) for n, d in files.items())
    img[19 * S:19 * S + len(ts)] = ts
    root = record("\0", 18, S, True) + record("VIDEO_TS", 19, S, True)
    img[18 * S:18 * S + len(root)] = root
    img[16 * S:16 * S + 6] = b"\x01CD001"
    img[16 * S + 156:16 * S + 190] = record("\0", 18, S, True)
    return bytes(img), lbas


def vts_ifo(menu_vobs, menu_cells, title_vobs, title_cells):
    """A VTS IFO with just the fields ifo_cells reads: VOBS offsets (in
    sectors from the IFO) and cell address tables at sectors 1 and 2."""
    S = dv.DATA_SECTOR
    ifo = bytearray(3 * S)
    ifo[0:12] = b"DVDVIDEO-VTS"
    ifo[0xC0:0xC4] = menu_vobs.to_bytes(4, "big")
    ifo[0xC4:0xC8] = title_vobs.to_bytes(4, "big")
    for at, sector, cells in ((0xD8, 1, menu_cells), (0xE0, 2, title_cells)):
        ifo[at:at + 4] = sector.to_bytes(4, "big")
        base = sector * S
        ifo[base + 4:base + 8] = (8 + 12 * len(cells) - 1).to_bytes(4, "big")
        for i, (a, b) in enumerate(cells):
            off = base + 8 + 12 * i
            ifo[off + 4:off + 8] = a.to_bytes(4, "big")
            ifo[off + 8:off + 12] = b.to_bytes(4, "big")
    return bytes(ifo)


class DvdCellsTest(unittest.TestCase):
    def test_cells_from_image(self):
        ifo = vts_ifo(3, [(0, 9)], 100, [(0, 499), (500, 999)])
        img, lbas = iso_image({"VTS_01_0.IFO": ifo, "VTS_01_0.BUP": ifo})
        with tempfile.NamedTemporaryFile(suffix=".iso") as f:
            f.write(img)
            f.flush()
            cells = dv.dvd_cells(f.name)
        base = lbas["VTS_01_0.IFO"]
        self.assertEqual(cells, [(base + 3, base + 12), (base + 100, base + 599),
                                 (base + 600, base + 1099)])

    def test_not_dvd_video(self):
        with tempfile.NamedTemporaryFile(suffix=".iso") as f:
            f.write(bytes(dv.DATA_SECTOR * 40))
            f.flush()
            self.assertEqual(dv.dvd_cells(f.name), [])


class SgReaderTest(unittest.TestCase):
    def reader(self, results, unlock):
        """An SgReader whose drive answers with `results` in turn: True for
        a good read, else the sense (key, ASC, ASCQ) of a failure."""
        r = dv.SgReader.__new__(dv.SgReader)
        r.fd, r.unlock, r.may_unlock = None, unlock, True
        r.buf = bytearray(dv.DATA_SECTOR)
        answers = iter(results)

        def fake_scsi_read(fd, cdb, buf, n):
            a = next(answers)
            if a is True:
                return n
            e = dv.ScsiError(b"")
            e.sense = a
            raise e
        patcher = mock.patch.object(dv, "scsi_read", fake_scsi_read)
        patcher.start()
        self.addCleanup(patcher.stop)
        return r

    def test_unlocks_again_when_refused(self):
        unlock = mock.Mock()
        r = self.reader([dv.SENSE_CSS_LOCKED, True], unlock)
        r.read(100, 1)
        unlock.assert_called_once()

    def test_unlocks_once_per_run_of_refusals(self):
        unlock = mock.Mock()
        locked = dv.SENSE_CSS_LOCKED
        r = self.reader([locked, locked, locked, True, locked, True],
                        unlock)
        for _ in range(2):
            with self.assertRaises(OSError):
                r.read(100, 1)
        r.read(200, 1)                      # a good read re-arms it
        r.read(300, 1)
        self.assertEqual(unlock.call_count, 2)

    def test_other_errors_dont_unlock(self):
        unlock = mock.Mock()
        r = self.reader([(0x3, 0x11, 0x05)], unlock)
        with self.assertRaises(OSError):
            r.read(100, 1)
        unlock.assert_not_called()


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

    def test_start(self):
        with tempfile.NamedTemporaryFile(suffix=".iso") as f:
            f.write(os.urandom(dv.DATA_SECTOR * 300))
            f.flush()
            code, out = self.run_main("--device", f.name, "-q",
                                      "--start", "100")
        self.assertEqual(code, 0)
        self.assertIn("OK: all 200 sectors", out)

    def test_start_and_end(self):
        with tempfile.NamedTemporaryFile(suffix=".iso") as f:
            f.write(os.urandom(dv.DATA_SECTOR * 300))
            f.flush()
            code, out = self.run_main("--device", f.name, "-q",
                                      "--start", "100", "--end", "149")
        self.assertEqual(code, 0)
        self.assertIn("OK: all 50 sectors", out)

    def test_resume_command(self):
        with mock.patch.object(sys, "argv", ["disc-verify", "-e", "--start",
                                             "5", "--retries", "2"]):
            cmd = dv.resume_command(123)
        self.assertTrue(cmd.endswith("disc-verify -e --retries 2 --start 123"))

    def test_no_disc(self):
        with mock.patch.object(dv, "find_disc", return_value=(None, None)):
            code, _ = self.run_main()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
