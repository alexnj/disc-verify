# disc-verify

Check that every sector of an optical disc can be read: data CDs, audio CDs,
DVDs and Blu-rays. Useful for checking second-hand discs, old backups or a box
set before you rely on them. Runs on macOS and Linux.

disc-verify reads the whole disc, start to finish, straight from the drive,
including menus, extras and unused space. It retries any sector that fails and
tells you exactly which parts of the disc can't be read. Areas the drive can
only crawl through are skipped and reported, so a copy-protected or badly
damaged disc doesn't take all night.

```
$ sudo disc-verify --eject
Verifying /dev/rdisk4 (DVD-ROM)
  3,791,488 sectors, 7.8 GB, reading via libdvdcss
   43.7%    3.4 GB /    7.8 GB   11.2 MB/s  ETA 0:06:43  problem sectors: 0

OK: all 3,791,488 sectors read successfully in 0:11:52.
```

## Install

On macOS:

```sh
brew install alexnj/tap/disc-verify
```

This also installs [libdvdcss](https://www.videolan.org/developers/libdvdcss.html),
which is needed to read most movie DVDs (see [DVDs](#dvds)).

On Linux, copy the `disc-verify` script somewhere on your `PATH` (it needs
only Python 3). For movie DVDs, also install your distribution's libdvdcss
package (`libdvdcss2` on Debian/Ubuntu, `libdvdcss` on Arch and Fedora).

You need an optical drive that can read the disc: a DVD drive for
CDs and DVDs, a Blu-ray drive for Blu-rays.

## Usage

```sh
sudo disc-verify              # verify the disc in the drive
sudo disc-verify --eject      # ...and eject it when done
disc-verify --device /dev/sr1 # Linux: pick a drive (default: first with a disc)
disc-verify --device disc.iso # verify an image file
```

| Option | |
|---|---|
| `-e`, `--eject` | Eject the disc when finished (handy when checking a stack of discs) |
| `-r N`, `--retries N` | Extra attempts for each failing sector (default 1) |
| `--end N` | Stop after sector N, e.g. to re-read just one skipped area with `--thorough` |
| `-s N`, `--start N` | Start at sector N, e.g. to continue a run that stopped. A stopped run prints the command to use |
| `-t`, `--thorough` | Don't skip areas the drive reads very slowly; read every sector however long it takes |
| `-d PATH`, `--device PATH` | Device such as `/dev/disk4` (macOS) or `/dev/sr0` (Linux), or an image file. Default: the disc in the optical drive |
| `--no-css` | Don't use libdvdcss for DVDs |
| `-q`, `--quiet` | No progress bar |

On macOS, `sudo` is needed because only root can read a disc directly. On
Linux, members of the group that owns the drive (usually `optical` or
`cdrom`) can run it without `sudo`. Without access, disc-verify tells you
what to do and exits.

## Reading the results

- **OK**: every sector was read. The disc is physically fine.
- **FAILED: unreadable**: these sectors couldn't be read even after retries.
  Each region is listed with its position: a track and time for CDs, or how
  far into the disc for DVDs and Blu-rays.
- **DAMAGED: read with C2 errors** (audio CDs only): the drive read these
  sectors but reported that it had to patch over damaged audio. This can be
  heard as clicks or dropouts.
- **SKIPPED**: the drive read these areas so slowly (seconds per read instead
  of a fraction of a second) that disc-verify skipped ahead to where reading
  was quick again. See [Slow areas](#slow-areas).
- **STOPPED: the drive stopped responding**: sectors that read fine earlier in
  the run started failing too, so the drive, not the disc, has stopped
  working. Unplug the drive (or power-cycle it) to reset it.

Press Ctrl-C at any time to stop. You still get a report for the part that
was read.

Exit codes: `0` all OK, `1` unreadable, damaged or skipped sectors found, `2`
setup problem (no disc, no permission), `3` the drive stopped responding,
`130` interrupted.

**Where the damage is matters.** Discs are read from the centre outward, so
damage late in a single-layer disc means the outer edge, where scratches and
wear are most common. On a dual-layer DVD or Blu-ray, the second half of the
sectors is the second layer.

## Disc types

### Audio CDs

A CD player never reports a read error. When audio is damaged, the drive fills
the gap with a best guess. So disc-verify also asks the drive for its **C2
error flags**, which mark sectors where that happened. Not every drive reports
C2; if yours doesn't, disc-verify says so and checks read errors only.

Audio CDs are read track by track. Problems are shown as `track 3 at 02:15`.

### Data CDs

CD-ROM, CD-R, Video CD and mixed-mode or Enhanced CDs are read track by track
too, in the right format for each track.

### DVDs

Most movie DVDs are encrypted (CSS). Many drives refuse to read the encrypted
sectors until the computer goes through an unlock step, which disc-verify does
with libdvdcss. Without it, a perfectly good disc can show hundreds of false
errors.

Most DVD drives also have a **region** setting. If the disc's region doesn't
match the drive's, the drive refuses to read the encrypted parts. On macOS
you can set the drive's region by inserting a disc and opening the DVD Player
app; on Linux, with the `regionset` tool. Drives only allow about five region changes, and the last one is
permanent.

### Blu-rays

Standard Blu-rays usually read fine. The drive's region doesn't matter for
Blu-ray. 4K UHD discs, and a few others, use an extra layer of copy
protection (AACS bus encryption) that disc-verify can't unlock, so the drive
may refuse some reads. disc-verify warns when a disc looks like a UHD disc.

### False alarms

Copy protection can look like damage:

- The drive refusing protected content shows up as **huge unbroken runs** of
  bad sectors, often covering most of the disc. Real damage shows up as
  scattered sectors or small clusters. disc-verify points it out when it sees
  this pattern.
- Some commercial DVDs and copy-protected audio CDs contain **deliberately
  unreadable sectors** that players skip. If a disc has a small bad region but
  plays through fine in a player, this may be why.

### Slow areas

Some DVD copy protections go further: they fill whole cells of the video
files with deliberately damaged sectors and use the menus to steer players
around them. A drive reading through such an area slows to a crawl (a few
KB/s), and some drives lock up after a while and fail every read, even of
sectors they read fine a minute earlier, until they are unplugged.

So when several reads in a row are very slow, disc-verify skips ahead and
reports the skipped area. On a DVD-Video disc it skips the rest of the slow
cell, the unit these decoys come in; elsewhere it probes further and further
ahead until reading is quick again. Either way the drive spends as little
time as possible in the slow area. It also notices when the drive stops
responding, and stops rather than reporting the rest of the disc as bad. On a
disc you burned yourself, a slow area is an early sign of damage. Use
`--thorough` to read slow areas anyway.

## How long it takes

It depends on the drive and the disc. Rough guide:

| Disc | Time |
|---|---|
| CD (700 MB / 80 min) | 5–10 min |
| DVD single-layer (4.7 GB) | 10–15 min |
| DVD dual-layer (8.5 GB) | 15–25 min |
| Blu-ray 25 GB | 30–60 min |
| Blu-ray 50 GB | 1–2 h |

Damaged areas take longer: each failed read can take several seconds while
the drive tries to recover.

## Development

disc-verify is a single Python 3 script with no dependencies outside the
standard library.

```sh
python3 -m unittest discover tests
```

## License

Apache 2.0. See [LICENSE](LICENSE).
