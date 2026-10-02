#!/usr/bin/env python3
"""Split a live FLV that mixes several AVC configurations into playable parts.

Why this exists
---------------
fix_flv_stream.py-new rebuilds one monotonic FLV, but it injects only the very
first AVC sequence header it meets and then splices pieces that may start with
non-keyframes.  Recordings that changed resolution / SPS-PPS mid-file (several
reconnects, quality switches, PK layouts, chunks stored out of order) therefore
cannot be played as a single track: the decoder gets frames whose parameter
sets do not match the ones it was configured with.  Video turns into garbage
while AAC (a single, self describing config) keeps playing.

Crucially, the chunks of such a recording are often stored out of order, so the
configuration that applies to a chunk is NOT simply the last sequence header
seen earlier in the file.  This tool therefore picks, for every chunk, the
candidate sequence header that actually decodes it (trial decode with ffprobe)
and only then groups chunks by that configuration.

What this tool does
-------------------
1. streams the file once and cuts it into pieces at onMetaData script tags, at
   timestamp regressions, and at every change of video/audio codec config;
2. collects every distinct sequence header found in the file;
3. decides per piece, by trial decode, which sequence header really matches it;
4. orders the pieces by start timestamp and groups consecutive pieces that
   share a configuration into one playable run;
5. writes one FLV per run, injecting that run's sequence header exactly once
   and starting each file on a keyframe with a monotonic timeline;
6. optionally remuxes each run to MP4 -- safe here because every run uses a
   single, constant configuration.

Usage:
  python3 fix_flv_split.py input.flv [-o OUTDIR] [--dry-run] [--mp4] [--overwrite]
"""

import argparse
import hashlib
import mmap
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

DISCONTINUITY_MS = 1000
MAX_TAG_SIZE = 10_000_000
SAMPLE_SECONDS = 6.0
SAMPLE_MAX_BYTES = 12_000_000
TAG_START = re.compile(rb"[\x08\x09\x12]")
VIDEO, AUDIO, SCRIPT = 9, 8, 18
FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09"
PREV_TAG = b"\x00\x00\x00\x00"


@dataclass
class Piece:
    start: int
    end: int = 0
    vhash: str = ""
    ahash: str = ""
    vcfg: Optional[bytes] = None
    acfg: Optional[bytes] = None
    res: str = "?"
    ts_min: Optional[int] = None
    ts_max: Optional[int] = None
    video: int = 0
    audio: int = 0
    keyframes: List[int] = field(default_factory=list)

    @property
    def codec(self):
        return "%s/%s" % (self.vhash, self.ahash)

    @property
    def media(self):
        return self.video + self.audio


class _BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def bit(self):
        value = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return value

    def bits(self, count):
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self):
        zeros = 0
        while self.bit() == 0:
            zeros += 1
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self):
        value = self.ue()
        return (value + 1) // 2 if value & 1 else -(value // 2)


def _unescape(data):
    out = bytearray()
    index = 0
    while index < len(data):
        if (
            index + 2 < len(data)
            and data[index] == 0
            and data[index + 1] == 0
            and data[index + 2] == 3
        ):
            out += data[index:index + 2]
            index += 3
        else:
            out.append(data[index])
            index += 1
    return bytes(out)


def avc_resolution(payload):
    """Return 'WxH' parsed from an FLV AVC sequence header, or '?'."""
    try:
        record = payload[5:]
        num_sps = record[5] & 0x1F
        if num_sps == 0:
            return "?"
        length = int.from_bytes(record[6:8], "big")
        sps = record[8:8 + length]
        if len(sps) < 4 or (sps[0] & 0x1F) != 7:
            return "?"
        reader = _BitReader(_unescape(sps[1:]))
        profile = reader.bits(8)
        reader.bits(8)
        reader.bits(8)
        reader.ue()
        chroma_format_idc = 1
        separate_colour_plane = 0
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma_format_idc = reader.ue()
            if chroma_format_idc == 3:
                separate_colour_plane = reader.bit()
            reader.ue()
            reader.ue()
            reader.bit()
            if reader.bit():
                count = 8 if chroma_format_idc != 3 else 12
                for index in range(count):
                    if reader.bit():
                        size = 16 if index < 6 else 64
                        last = 8
                        nxt = 8
                        for _ in range(size):
                            if nxt != 0:
                                nxt = (last + reader.se() + 256) % 256
                            last = last if nxt == 0 else nxt
        reader.ue()
        poc_type = reader.ue()
        if poc_type == 0:
            reader.ue()
        elif poc_type == 1:
            reader.bit()
            reader.se()
            reader.se()
            for _ in range(reader.ue()):
                reader.se()
        reader.ue()
        reader.bit()
        width_mbs = reader.ue() + 1
        height_units = reader.ue() + 1
        frame_mbs_only = reader.bit()
        if not frame_mbs_only:
            reader.bit()
        reader.bit()
        crop_left = crop_right = crop_top = crop_bottom = 0
        if reader.bit():
            crop_left = reader.ue()
            crop_right = reader.ue()
            crop_top = reader.ue()
            crop_bottom = reader.ue()
        width = width_mbs * 16
        height = height_units * 16 * (2 - frame_mbs_only)
        if chroma_format_idc == 0 or separate_colour_plane:
            crop_unit_x = 1
            crop_unit_y = 2 - frame_mbs_only
        else:
            crop_unit_x = {1: 2, 2: 2, 3: 1}[chroma_format_idc]
            crop_unit_y = {1: 2, 2: 1, 3: 1}[chroma_format_idc] * (2 - frame_mbs_only)
        width -= crop_unit_x * (crop_left + crop_right)
        height -= crop_unit_y * (crop_top + crop_bottom)
        return "%dx%d" % (width, height)
    except Exception:
        return "?"


def iter_tags(mapped, start, end):
    limit = end - 15
    for match in TAG_START.finditer(mapped, start, end):
        offset = match.start()
        if offset > limit:
            continue
        tag_type = mapped[offset]
        size = int.from_bytes(mapped[offset + 1:offset + 4], "big")
        if size <= 0 or size > MAX_TAG_SIZE or offset + 15 + size > end:
            continue
        if int.from_bytes(mapped[offset + 11 + size:offset + 15 + size], "big") != size + 11:
            continue
        timestamp = (mapped[offset + 7] << 24) | int.from_bytes(
            mapped[offset + 4:offset + 7], "big"
        )
        yield tag_type, timestamp, offset, size


def encode_tag(tag_type, timestamp, payload):
    size = len(payload)
    stamp = int(timestamp).to_bytes(4, "big")
    return (
        bytes([tag_type])
        + size.to_bytes(3, "big")
        + stamp[1:]
        + stamp[0:1]
        + b"\x00\x00\x00"
        + payload
        + (size + 11).to_bytes(4, "big")
    )


def update_progress(label, position, total, last_percent):
    percent = 0 if total <= 0 else min(100, position * 100 // total)
    if percent != last_percent:
        sys.stderr.write("\r%s: %3d%%" % (label, percent))
        sys.stderr.flush()
    return percent


def finish_progress(label):
    sys.stderr.write("\r%s: 100%%\n" % label)
    sys.stderr.flush()


def scan(path):
    pieces = []
    configs = {}
    audio_payload = None
    vhash = ahash = ""
    vcfg = acfg = None
    res = "?"
    with path.open("rb") as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            size = len(mapped)
            limit = size - 15
            current = Piece(start=0)
            last_timestamp = None
            last_percent = -1
            for match in TAG_START.finditer(mapped):
                offset = match.start()
                last_percent = update_progress("scanning", offset, size, last_percent)
                if offset > limit:
                    continue
                tag_type = mapped[offset]
                tag_size = int.from_bytes(mapped[offset + 1:offset + 4], "big")
                if tag_size <= 0 or tag_size > MAX_TAG_SIZE or offset + 15 + tag_size > size:
                    continue
                if int.from_bytes(
                    mapped[offset + 11 + tag_size:offset + 15 + tag_size], "big"
                ) != tag_size + 11:
                    continue
                timestamp = (mapped[offset + 7] << 24) | int.from_bytes(
                    mapped[offset + 4:offset + 7], "big"
                )
                header = mapped[offset + 11]
                packet_type = mapped[offset + 12] if tag_size >= 2 else -1
                is_vcfg = tag_type == VIDEO and (header & 0x0F) == 7 and packet_type == 0
                is_acfg = tag_type == AUDIO and (header >> 4) == 10 and packet_type == 0

                new_video = new_audio = False
                if is_vcfg:
                    payload = bytes(mapped[offset + 11:offset + 11 + tag_size])
                    digest = hashlib.sha1(payload).hexdigest()[:8]
                    if digest not in configs:
                        configs[digest] = (payload, avc_resolution(payload))
                    if digest != vhash:
                        vhash = digest
                        vcfg = payload
                        res = configs[digest][1]
                        new_video = True
                if is_acfg:
                    payload = bytes(mapped[offset + 11:offset + 11 + tag_size])
                    digest = hashlib.sha1(payload).hexdigest()[:8]
                    if digest != ahash:
                        ahash = digest
                        acfg = payload
                        audio_payload = payload
                        new_audio = True

                regressed = (
                    last_timestamp is not None
                    and timestamp < last_timestamp - DISCONTINUITY_MS
                )
                if (
                    tag_type == SCRIPT or regressed or new_video or new_audio
                ) and offset != current.start:
                    current.end = offset
                    if current.media:
                        pieces.append(current)
                    current = Piece(start=offset)

                current.vhash = vhash
                current.ahash = ahash
                current.vcfg = vcfg
                current.acfg = acfg
                current.res = res

                if is_vcfg or is_acfg:
                    continue

                if tag_type == VIDEO and (header & 0x0F) == 7:
                    current.video += 1
                    if current.ts_min is None or timestamp < current.ts_min:
                        current.ts_min = timestamp
                    if current.ts_max is None or timestamp > current.ts_max:
                        current.ts_max = timestamp
                    if (header >> 4) == 1:
                        current.keyframes.append(timestamp)
                    last_timestamp = timestamp
                elif tag_type == AUDIO and (header >> 4) == 10:
                    current.audio += 1
                    if current.ts_min is None or timestamp < current.ts_min:
                        current.ts_min = timestamp
                    if current.ts_max is None or timestamp > current.ts_max:
                        current.ts_max = timestamp
                    last_timestamp = timestamp

            finish_progress("scanning")
            current.end = size
            if current.media:
                pieces.append(current)
    return pieces, configs, audio_payload


def build_sample(mapped, piece, video_cfg, audio_cfg):
    """FLV bytes: config + keyframe + up to SAMPLE_SECONDS of the piece."""
    keyframe = None
    for tag_type, timestamp, offset, tag_size in iter_tags(mapped, piece.start, piece.end):
        if (
            tag_type == VIDEO
            and (mapped[offset + 11] & 0x0F) == 7
            and mapped[offset + 12] != 0
            and (mapped[offset + 11] >> 4) == 1
        ):
            keyframe = (timestamp, offset)
            break
    if keyframe is None:
        return None

    start_ts, start_offset = keyframe
    out = bytearray(FLV_HEADER + PREV_TAG)
    out += encode_tag(VIDEO, 0, video_cfg)
    if audio_cfg:
        out += encode_tag(AUDIO, 0, audio_cfg)

    limit_bytes = start_offset + SAMPLE_MAX_BYTES
    for tag_type, timestamp, offset, tag_size in iter_tags(mapped, start_offset, piece.end):
        if offset > limit_bytes or timestamp - start_ts > SAMPLE_SECONDS * 1000:
            break
        out += encode_tag(tag_type, timestamp - start_ts, bytes(mapped[offset + 11:offset + 11 + tag_size]))
    return bytes(out)


def trial_decode(sample, scratch):
    if sample is None:
        return None, 0
    handle, path = tempfile.mkstemp(suffix=".flv", dir=scratch)
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(sample)
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-count_frames",
                "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames",
                "-of", "csv=p=0", path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return None, 0
    errors = sum(1 for line in result.stderr.decode("utf-8", "replace").splitlines() if line.strip())
    text = result.stdout.decode("utf-8", "replace").strip()
    frames = int(text) if text.isdigit() else 0
    os.unlink(path)
    return errors, frames


def assign_configs(path, pieces, configs, audio_cfg, log):
    if not configs:
        return
    keys = list(configs.keys())
    with path.open("rb") as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            scratch = tempfile.mkdtemp(prefix="fixflv_trial_")
            try:
                targets = [piece for piece in pieces if piece.video > 0]
                last_percent = -1
                for index, piece in enumerate(targets):
                    last_percent = update_progress("probing", index, len(targets), last_percent)
                    hint = piece.vhash if piece.vhash in configs else keys[0]
                    order = [hint] + [key for key in keys if key != hint]
                    results = []
                    for key in order:
                        sample = build_sample(mapped, piece, configs[key][0], audio_cfg)
                        errors, frames = trial_decode(sample, scratch)
                        results.append((key, errors, frames))
                        if errors == 0 and frames > 0:
                            break
                    good = [item for item in results if item[1] == 0 and item[2] > 0]
                    if good:
                        best = max(good, key=lambda item: item[2])
                    else:
                        valid = [item for item in results if item[2] > 0] or results
                        best = min(valid, key=lambda item: (item[1] if item[1] is not None else 1 << 30, -item[2]))
                        if best[0] != hint:
                            for item in valid:
                                if item[0] == hint:
                                    best = item
                                    break
                    piece.vhash = best[0]
                    piece.vcfg = configs[best[0]][0]
                    piece.res = configs[best[0]][1]
                    if best[0] != hint:
                        log(
                            "  piece @%d ts %s..%s: config -> %s (%s)"
                            % (piece.start, piece.ts_min, piece.ts_max, best[0], piece.res)
                        )
                if targets:
                    finish_progress("probing")
            finally:
                shutil.rmtree(scratch, ignore_errors=True)


def plan(pieces):
    ordered = sorted(
        (piece for piece in pieces if piece.media and piece.ts_min is not None),
        key=lambda piece: (piece.ts_min, piece.ts_max, piece.start),
    )
    runs = []
    current = []
    for piece in ordered:
        if current and piece.codec != current[0].codec:
            runs.append(current)
            current = []
        current.append(piece)
    if current:
        runs.append(current)
    return ordered, runs


def run_span(pieces):
    return pieces[0].ts_min, max(piece.ts_max for piece in pieces)


def write_run(source, run, index, stem, output_dir, overwrite, log):
    config = run[0]
    name = "%s.part%02d.%s.%s.flv" % (stem, index, config.res, config.vhash)
    output = output_dir / name
    if output.exists() and not overwrite:
        log("  skip existing %s" % name)
        return output, True

    first_ts, last_ts = run_span(run)
    with source.open("rb") as source_file:
        with mmap.mmap(source_file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            with output.open("wb") as output_file:
                output_file.write(FLV_HEADER)
                output_file.write(PREV_TAG)
                if config.vcfg:
                    output_file.write(encode_tag(VIDEO, 0, config.vcfg))
                if config.acfg:
                    output_file.write(encode_tag(AUDIO, 0, config.acfg))

                label = "writing part%02d" % index
                total_bytes = sum(piece.end - piece.start for piece in run)
                done_bytes = 0
                last_percent = -1
                prev_max = None
                cursor = 0
                out_end = 0
                written = 0
                skipped = []
                for piece in run:
                    if prev_max is not None and piece.ts_max <= prev_max:
                        skipped.append("dup@%d" % piece.start)
                        continue
                    effective = piece.ts_min if prev_max is None else max(piece.ts_min, prev_max)
                    keyframe = None
                    for candidate in piece.keyframes:
                        if candidate >= effective:
                            keyframe = candidate
                            break
                    if keyframe is None:
                        skipped.append("nokf@%d" % piece.start)
                        continue

                    started = False
                    for tag_type, timestamp, offset, tag_size in iter_tags(
                        mapped, piece.start, piece.end
                    ):
                        last_percent = update_progress(
                            label, done_bytes + (offset - piece.start), total_bytes, last_percent
                        )
                        if tag_type == SCRIPT or tag_size < 2:
                            continue
                        header = mapped[offset + 11]
                        packet_type = mapped[offset + 12]
                        if tag_type == VIDEO:
                            if (header & 0x0F) != 7 or packet_type == 0:
                                continue
                            if not started:
                                if (header >> 4) != 1 or timestamp < keyframe:
                                    continue
                                started = True
                            if timestamp < effective:
                                continue
                            out_timestamp = cursor + (timestamp - keyframe)
                        elif tag_type == AUDIO:
                            if (header >> 4) != 10 or packet_type == 0:
                                continue
                            if not started or timestamp < keyframe:
                                continue
                            out_timestamp = cursor + (timestamp - keyframe)
                        else:
                            continue
                        output_file.write(
                            encode_tag(tag_type, out_timestamp, mapped[offset + 11:offset + 11 + tag_size])
                        )
                        out_end = max(out_end, out_timestamp)
                        written += 1

                    done_bytes += piece.end - piece.start
                    cursor = out_end + 1
                    prev_max = piece.ts_max if prev_max is None else max(prev_max, piece.ts_max)

    finish_progress(label)
    if written == 0:
        output.unlink()
        log("  dropped empty run (no keyframe)")
        return None, False
    log(
        "  %s  ts %s..%s  -> 0..%d (%.1fs, %d packets)%s"
        % (
            name, first_ts, last_ts, out_end, out_end / 1000.0, written,
            "" if not skipped else "  skipped: " + ",".join(skipped),
        )
    )
    return output, True


def remux(flv_path, mp4_path):
    return subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "warning", "-stats",
            "-i", str(flv_path),
            "-map", "0:v:0", "-map", "0:a:0",
            "-c", "copy", "-movflags", "+faststart",
            "-y", str(mp4_path),
        ]
    ).returncode == 0


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(
        description="Split a mixed-configuration live FLV into playable parts."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="only print the plan")
    parser.add_argument("--mp4", action="store_true", help="remux each part to MP4")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.is_file():
        parser.error("not a file: %s" % source)
    output_dir = (
        args.output_dir.expanduser().resolve() if args.output_dir else source.parent
    )

    pieces, configs, audio_cfg = scan(source)
    if not pieces:
        parser.error("no media packets found")

    print("%s: %d piece(s), %d distinct video config(s)" % (source.name, len(pieces), len(configs)))
    assign_configs(source, pieces, configs, audio_cfg, print)

    ordered, runs = plan(pieces)
    print("plan: %d part(s)" % len(runs))
    for index, run in enumerate(runs, 1):
        first_ts, last_ts = run_span(run)
        print(
            "  part%02d: %s cfg=%s  ts %s..%s (%.1fs)  [%d piece(s)]"
            % (
                index, run[0].res, run[0].vhash, first_ts, last_ts,
                (last_ts - first_ts) / 1000.0, len(run),
            )
        )

    if args.dry_run:
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    for index, run in enumerate(runs, 1):
        output, _ = write_run(
            source, run, index, source.stem, output_dir, args.overwrite, print
        )
        if output is not None and args.mp4:
            mp4 = output.with_suffix(".mp4")
            print("  remuxing %s ..." % mp4.name)
            if not remux(output, mp4):
                print("  error: ffmpeg remux failed for %s" % output.name, file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
