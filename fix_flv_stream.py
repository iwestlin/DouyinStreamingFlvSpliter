#!/usr/bin/env python3
import argparse
import mmap
import re
import struct
import subprocess
import sys
import tempfile
import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Tag:
    type: int
    size: int
    timestamp: int
    stream_id: int
    offset: int
    payload: bytes


def parse_aac_sample_rate(payload):
    if len(payload) < 4:
        return None
    frequencies = [
        96000, 88200, 64000, 48000, 44100, 32000, 24000,
        22050, 16000, 12000, 11025, 8000, 7350,
    ]
    frequency_index = ((payload[2] & 0x07) << 1) | (payload[3] >> 7)
    if frequency_index >= len(frequencies):
        return None
    return frequencies[frequency_index]


def parse_composition_offset(payload):
    value = int.from_bytes(payload[2:5], "big")
    if value & 0x800000:
        value -= 0x1000000
    return value


def update_progress(label, position, total, last_percent):
    percent = 0 if total == 0 else min(100, position * 100 // total)
    if percent != last_percent:
        print(f"\r{label}: {percent:3d}%", end="", file=sys.stderr, flush=True)
    return percent


def finish_progress(label):
    print(f"\r{label}: 100%", file=sys.stderr, flush=True)


def iter_tags(path, progress_label=None):
    last_percent = -1
    with path.open("rb") as source_file:
        with mmap.mmap(source_file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            size_limit = len(mapped) - 15
            for match in re.finditer(rb"[\x08\x09\x12]", mapped):
                offset = match.start()
                if progress_label:
                    last_percent = update_progress(
                        progress_label, offset, len(mapped), last_percent
                    )
                if offset > size_limit:
                    continue
                tag_type = mapped[offset]
                tag_size = int.from_bytes(mapped[offset + 1:offset + 4], "big")
                if tag_size <= 0 or tag_size > 10_000_000 or offset + 15 + tag_size > len(mapped):
                    continue
                previous_size = int.from_bytes(
                    mapped[offset + 11 + tag_size:offset + 15 + tag_size],
                    "big",
                )
                if previous_size != tag_size + 11:
                    continue
                low_timestamp = int.from_bytes(mapped[offset + 4:offset + 7], "big")
                high_timestamp = int.from_bytes(mapped[offset + 7:offset + 8], "big")
                timestamp = (high_timestamp << 24) | low_timestamp
                stream_id = int.from_bytes(mapped[offset + 8:offset + 11], "big")
                payload = mapped[offset + 11:offset + 11 + tag_size]
                yield Tag(tag_type, tag_size, timestamp, stream_id, offset, payload)
            if progress_label:
                finish_progress(progress_label)


def inspect_tags(path):
    audio_packets = 0
    video_packets = 0
    sample_rate = None
    has_audio_config = False
    has_video_config = False
    audio_keys = []
    video_keys = []

    for tag in iter_tags(path, "scanning"):
        payload = tag.payload
        if tag.type == 8 and payload and payload[0] >> 4 == 10:
            if len(payload) > 1 and payload[1] == 0:
                has_audio_config = True
                sample_rate = sample_rate or parse_aac_sample_rate(payload)
            elif len(payload) > 1 and sample_rate:
                audio_packets += 1
                audio_keys.append((hashlib.sha1(payload).digest(), tag.timestamp))
        elif tag.type == 9 and payload and payload[0] & 0x0F == 7:
            if len(payload) > 1 and payload[1] == 0:
                has_video_config = True
            elif len(payload) >= 5:
                video_packets += 1
                video_keys.append((hashlib.sha1(payload).digest(), tag.timestamp))

    audio_prefix_skip = duplicate_prefix_count(audio_keys)
    video_prefix_skip = duplicate_prefix_count(video_keys)
    audio_packets -= audio_prefix_skip
    video_packets -= video_prefix_skip
    base = min(
        audio_keys[audio_prefix_skip][1],
        video_keys[video_prefix_skip][1],
    )

    return {
        "audio_packets": audio_packets,
        "video_packets": video_packets,
        "sample_rate": sample_rate,
        "has_audio_config": has_audio_config,
        "has_video_config": has_video_config,
        "audio_prefix_skip": audio_prefix_skip,
        "video_prefix_skip": video_prefix_skip,
        "base": base,
    }


def duplicate_prefix_count(keys):
    if not keys:
        return 0
    counts = Counter(keys)
    count = 0
    for key in keys:
        if counts[key] < 2:
            break
        count += 1
    return count


def write_tag(output_file, tag_type, timestamp, payload):
    size = len(payload)
    timestamp_bytes = int(timestamp).to_bytes(4, "big")
    output_file.write(
        bytes([tag_type])
        + size.to_bytes(3, "big")
        + timestamp_bytes[1:]
        + timestamp_bytes[0:1]
        + b"\x00\x00\x00"
        + payload
    )
    output_file.write((size + 11).to_bytes(4, "big"))


def write_clean_flv(source, output, info):
    audio_packets = info["audio_packets"]
    video_packets = info["video_packets"]
    sample_rate = info["sample_rate"]
    if not audio_packets or not video_packets or not sample_rate:
        raise RuntimeError("not enough valid audio/video packets")

    wrote_audio_config = False
    wrote_video_config = False
    started = False
    audio_seen = 0
    video_seen = 0
    audio_skip = info["audio_prefix_skip"]
    video_skip = info["video_prefix_skip"]

    with source.open("rb") as source_file, output.open("wb") as output_file:
        output_file.write(b"FLV\x01\x05\x00\x00\x00\t")
        output_file.write(b"\x00\x00\x00\x00")

        for tag in iter_tags(source, "rewriting"):
            payload = tag.payload
            if tag.type == 9 and payload and payload[0] & 0x0F == 7:
                if len(payload) > 1 and payload[1] == 0:
                    if not wrote_video_config:
                        write_tag(output_file, 9, 0, payload)
                        wrote_video_config = True
                elif len(payload) >= 5:
                    if video_seen < video_skip:
                        video_seen += 1
                        continue
                    if not started and payload[0] >> 4 != 1:
                        video_seen += 1
                        continue
                    started = True
                    timestamp = max(0, tag.timestamp - info["base"])
                    write_tag(output_file, 9, timestamp, payload)
                    video_seen += 1
            elif tag.type == 8 and payload and payload[0] >> 4 == 10:
                if len(payload) > 1 and payload[1] == 0:
                    if not wrote_audio_config:
                        write_tag(output_file, 8, 0, payload)
                        wrote_audio_config = True
                elif len(payload) > 1:
                    if audio_seen < audio_skip:
                        audio_seen += 1
                        continue
                    if not started:
                        audio_seen += 1
                        continue
                    timestamp = max(0, tag.timestamp - info["base"])
                    write_tag(output_file, 8, timestamp, payload)
                    audio_seen += 1


def probe_output(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return None
    import json
    try:
        streams = json.loads(result.stdout).get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if video is None or audio is None:
            return None
        return float(video.get("duration", 0)), float(audio.get("duration", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def repair(source, output):
    info = inspect_tags(source)
    print(
        f"valid audio packets: {info['audio_packets']}, "
        f"valid video packets: {info['video_packets']}, "
        f"sample rate: {info['sample_rate']}"
    )

    with tempfile.TemporaryDirectory(dir=output.parent) as temp_dir:
        clean_flv = Path(temp_dir) / f"{source.stem}.clean.flv"
        write_clean_flv(source, clean_flv, info)

        command = [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "warning",
            "-stats",
            "-i",
            str(clean_flv),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ]
        result = subprocess.run(command)
        if result.returncode != 0:
            return False

        durations = probe_output(output)
        if durations is None:
            return False
        video_duration, audio_duration = durations
        print(f"video duration: {video_duration:.3f}s, audio duration: {audio_duration:.3f}s")
        return abs(video_duration - audio_duration) < 0.5


def iter_flv_files(directory):
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() == ".flv"
    )


def repair_one(source, output, overwrite):
    if output.exists() and not overwrite:
        print(f"skip: {output.name} already exists")
        return True

    try:
        if not repair(source, output):
            if output.exists():
                output.unlink()
            return False
    except Exception as error:
        if output.exists():
            output.unlink()
        print(f"error: {error}", file=sys.stderr)
        return False
    return True


def repair_directory(source, output_root, overwrite):
    output_root.mkdir(parents=True, exist_ok=True)
    files = iter_flv_files(source)
    if not files:
        print("No FLV files found.")
        return 0

    failed = []
    for source_file in files:
        output_file = output_root / f"{source_file.stem}_streamcopy.mp4"
        print(f"\n{source_file}")
        if not repair_one(source_file, output_file, overwrite):
            failed.append(source_file)

    if failed:
        print(f"\n{len(failed)} file(s) failed:")
        for source_file in failed:
            print(f"  {source_file}")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Stream-parse an FLV, drop invalid tags, and stream-copy A/V into MP4.\n用于修复brec命令报错的flv文件"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if source.is_file():
        output = args.output.expanduser().resolve() if args.output else source.with_name(
            f"{source.stem}_streamcopy.mp4"
        )
        return 0 if repair_one(source, output, args.overwrite) else 1

    if source.is_dir():
        output_root = args.output.expanduser().resolve() if args.output else source
        return repair_directory(source, output_root, args.overwrite)

    parser.error(f"not a file or directory: {source}")


if __name__ == "__main__":
    raise SystemExit(main())
