#!/usr/bin/env python3
"""
FLV 文件分割脚本
根据 onMetaData 检测结果，将包含多个片段的 FLV 文件分离
并自动修复时间戳偏移问题
"""

import os
import struct
import subprocess

def split_and_fix_flv(input_file, output_dir=None):
    """
    分割 FLV 文件并修复时间戳偏移

    Args:
        input_file: 输入 FLV 文件路径
        output_dir: 输出目录
    """
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(input_file), 'split_output')

    os.makedirs(output_dir, exist_ok=True)

    with open(input_file, 'rb') as f:
        flv_header = f.read(9)
        f.read(4)
        all_data = f.read()

    # 查找所有 Script Tag (type=18) 的位置
    scripts = []
    pos = 0
    tag_count = 0

    while pos + 11 <= len(all_data):
        tag_type = all_data[pos]
        data_size = struct.unpack('>I', bytes([0]) + all_data[pos+1:pos+4])[0]

        if tag_type == 18:
            scripts.append({'tag': tag_count, 'offset': pos, 'size': data_size})

        if pos + 11 + data_size + 4 > len(all_data):
            break
        pos += 11 + data_size + 4
        tag_count += 1

    print(f"找到 {len(scripts)} 个 Script Tag")

    if len(scripts) < 2:
        print("只有一个片段，无需分割")
        return

    base_name = os.path.splitext(os.path.basename(input_file))[0]

    for i, script in enumerate(scripts):
        # 片段范围：Script N 到 Script N+1（最后一个到文件结尾）
        start = script['offset']
        if i < len(scripts) - 1:
            end = scripts[i + 1]['offset']
        else:
            end = len(all_data)

        segment_data = bytearray(all_data[start:end])

        # 后续片段需要添加 AVC 和 AAC Sequence Header 才能被 ffmpeg 解析
        if i > 0:
            search_start = scripts[0]['offset'] + 11 + scripts[0]['size'] + 4
            search_end = scripts[1]['offset']
            avc_seq_header = find_avc_sequence_header(all_data, search_start, search_end)
            aac_seq_header = find_aac_sequence_header(all_data, search_start, search_end)
            if avc_seq_header:
                print(f"    包含 AVC Sequence Header")
                segment_data = bytearray(avc_seq_header) + segment_data
            if aac_seq_header:
                print(f"    包含 AAC Audio Sequence Header")
                segment_data = bytearray(aac_seq_header) + segment_data

        # 修复时间戳偏移（跳过非关键帧，重写时间戳）
        segment_data = fix_timestamps(segment_data, is_first_segment=(i == 0))

        # 添加 FLV 头
        segment_data = flv_header + struct.pack('>I', 0) + segment_data

        output_file = os.path.join(output_dir, f"{base_name}_part{i+1}.flv")

        # 通过 stdin 将 segment_data 输入给 ffmpeg
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-f', 'flv',          # 输入格式 flv
            '-i', 'pipe:0',       # 从 stdin 读取
            '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            output_file
        ]

        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        proc.communicate(input=bytes(segment_data))

        if proc.returncode == 0:
            print(f"  片段 {i+1}: {output_file} ({len(segment_data)/1024/1024:.2f} MB)")
        else:
            print(f"  片段 {i+1} ffmpeg 处理失败，返回码: {proc.returncode}")

    print(f"\n分割完成！输出目录: {output_dir}")


def find_avc_sequence_header(all_data, start_pos, end_pos):
    """
    在指定范围内查找 AVC Sequence Header
    返回完整的 Tag 数据（包含 header + data + PreviousTagSize）
    """
    pos = start_pos

    while pos + 11 <= end_pos:
        tag_type = all_data[pos]
        data_size = struct.unpack('>I', bytes([0]) + all_data[pos+1:pos+4])[0]

        # 查找视频帧
        if tag_type == 9:
            frame_info = all_data[pos + 11]
            codec_id = frame_info & 0x0F

            # AVC Codec
            if codec_id == 7:
                # AVC type 在 offset 12
                avc_type = all_data[pos + 12]

                # Sequence Header
                if avc_type == 0:
                    # 包含：Tag Header (11) + Data + PreviousTagSize (4)
                    tag_data = all_data[pos:pos + 11 + data_size + 4]
                    return tag_data

        pos += 11 + data_size + 4

    return None


def find_aac_sequence_header(all_data, start_pos, end_pos):
    """
    在指定范围内查找 AAC Audio Sequence Header
    返回完整的 Tag 数据（包含 header + data + PreviousTagSize）
    Tag type=8, SoundFormat=10 (AAC), AAC type=0
    """
    pos = start_pos

    while pos + 11 <= end_pos:
        tag_type = all_data[pos]
        data_size = struct.unpack('>I', bytes([0]) + all_data[pos+1:pos+4])[0]

        # 查找音频帧
        if tag_type == 8:
            sound_format = all_data[pos + 11] & 0xF0
            # AAC format = 10
            if sound_format == 0xA0:
                # AAC type 在 offset 12
                aac_type = all_data[pos + 12]
                # Audio Sequence Header
                if aac_type == 0:
                    # 包含：Tag Header (11) + Data + PreviousTagSize (4)
                    tag_data = all_data[pos:pos + 11 + data_size + 4]
                    return tag_data

        pos += 11 + data_size + 4

    return None


def fix_timestamps(data, is_first_segment=True):
    """
    修复 FLV 片段中的时间戳偏移
    FLV Tag Header: tag_type(1) + data_size(3) + timestamp(4) + stream_id(3)
    返回修复后的数据
    """
    pos = 0
    output_data = bytearray()

    # 收集所有帧的信息
    frames = []
    while pos + 11 <= len(data):
        tag_type = data[pos]
        data_size = struct.unpack('>I', bytes([0]) + data[pos+1:pos+4])[0]

        if tag_type in [8, 9]:  # 音频或视频帧
            timestamp = struct.unpack('>I', data[pos+4:pos+8])[0]
            frames.append({
                'type': tag_type,
                'size': data_size,
                'timestamp': timestamp,
                'data': bytes(data[pos:pos + 11 + data_size])
            })

        if pos + 11 + data_size + 4 > len(data):
            break
        pos += 11 + data_size + 4

    if not frames:
        return data

    # 找到第一个音视频帧中较小的时间戳作为基准
    base_timestamp = min(f['timestamp'] for f in frames if f['type'] in [8, 9])

    # 规范化所有帧的时间戳
    for frame in frames:
        new_ts = frame['timestamp'] - base_timestamp

        # 确保时间戳非负
        if new_ts < 0:
            new_ts = 0

        tag_data = bytearray(frame['data'])
        tag_data[4:8] = struct.pack('>I', new_ts)
        output_data.extend(tag_data)
        output_data.extend(struct.pack('>I', 11 + frame['size']))

    return bytes(output_data)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 split_flv.py <input.flv> [output_dir]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(input_file):
        print(f"文件不存在: {input_file}")
        sys.exit(1)

    split_and_fix_flv(input_file, output_dir)
