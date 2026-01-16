#!/usr/bin/env python3
"""
FLV 文件分割脚本
根据 onMetaData 检测结果，将包含多个片段的 FLV 文件分离
并自动修复时间戳偏移问题
"""

import os
import struct

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
        if i == 0:
            # 第一个片段：到第二个 Script Tag
            next_offset = scripts[1]['offset'] if len(scripts) > 1 else len(all_data)
            segment_data = bytearray(all_data[:next_offset])
        else:
            # 后续片段：需要包含前一个片段的 AVC Sequence Header
            segment_start = script['offset']

            # 查找前一个片段的 AVC Sequence Header (Tag type=9, AVC type=0)
            avc_seq_header = find_avc_sequence_header(all_data, scripts[i-1]['offset'], segment_start)

            if avc_seq_header:
                print(f"    包含前一个片段的 AVC Sequence Header")
                segment_data = bytearray(avc_seq_header) + bytearray(all_data[segment_start:])
            else:
                segment_data = bytearray(all_data[segment_start:])

        # 修复时间戳偏移（跳过非关键帧，重写时间戳）
        segment_data = fix_timestamps(segment_data, is_first_segment=(i == 0))

        # 添加 FLV 头
        segment_data = flv_header + struct.pack('>I', 0) + segment_data

        output_file = os.path.join(output_dir, f"{base_name}_part{i+1}.flv")
        with open(output_file, 'wb') as f:
            f.write(segment_data)

        print(f"  片段 {i+1}: {output_file} ({len(segment_data)/1024/1024:.2f} MB)")

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


def fix_timestamps(data, is_first_segment=True):
    """
    修复 FLV 片段中的时间戳偏移
    FLV Tag Header: tag_type(1) + data_size(3) + timestamp(4) + stream_id(3)
    返回修复后的数据
    """
    pos = 0
    first_ts = None
    found_first_key = False
    output_data = bytearray()

    while pos + 11 <= len(data):
        tag_type = data[pos]
        data_size = struct.unpack('>I', bytes([0]) + data[pos+1:pos+4])[0]

        # 跳过所有视频非关键帧，直到找到第一个关键帧
        if tag_type == 9:
            frame_info = data[pos + 11]
            frame_type = (frame_info >> 4) & 0x0F
            if not found_first_key:
                if frame_type != 1:  # 不是关键帧，跳过
                    if pos + 11 + data_size + 4 > len(data):
                        break
                    pos += 11 + data_size + 4
                    continue
                else:
                    found_first_key = True

        # 时间戳在偏移 4 的位置，4 字节
        timestamp = struct.unpack('>I', data[pos+4:pos+8])[0]

        # 记录第一个音视频帧的时间戳作为偏移量
        if first_ts is None and tag_type in [8, 9]:
            first_ts = timestamp

        # 减去偏移量并添加到输出
        if first_ts is not None and tag_type in [8, 9]:
            new_ts = timestamp - first_ts
            tag_data = bytearray(data[pos:pos + 11 + data_size])
            tag_data[4:8] = struct.pack('>I', new_ts)
            output_data.extend(tag_data)
            output_data.extend(struct.pack('>I', 11 + data_size))  # PreviousTagSize

        # 移动到下一个 tag
        if pos + 11 + data_size + 4 > len(data):
            break
        pos += 11 + data_size + 4

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
