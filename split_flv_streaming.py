#!/usr/bin/env python3
"""
FLV 文件流式分割脚本
根据 onMetaData 检测结果，将包含多个片段的 FLV 文件分离
并自动修复时间戳偏移问题
支持流式处理，避免大文件内存占用
"""

import os
import struct
import subprocess


class StreamingFLVSplitter:
    def __init__(self, input_file, output_dir=None):
        self.input_file = input_file
        self.output_dir = output_dir or os.path.join(os.path.dirname(input_file), 'split_output')
        os.makedirs(self.output_dir, exist_ok=True)

        # 状态变量
        self.current_segment = 0
        self.script_tag_count = 0
        self.codec_headers_extracted = False
        self.avc_seq_header = None
        self.aac_seq_header = None
        self.ffmpeg_process = None
        self.current_output_file = None
        self.base_timestamp = None
        self.first_frame_in_segment = True

    def read_tag_header(self, f):
        """读取FLV Tag头部信息"""
        header = f.read(11)
        if len(header) < 11:
            return None

        tag_type = header[0]
        data_size = struct.unpack('>I', bytes([0]) + header[1:4])[0]
        timestamp = struct.unpack('>I', header[4:8])[0]

        return {
            'tag_type': tag_type,
            'data_size': data_size,
            'timestamp': timestamp,
            'header': header
        }

    def write_flv_header(self, proc):
        """写入FLV文件头"""
        with open(self.input_file, 'rb') as f:
            flv_header = f.read(9)
            f.read(4)  # PreviousTagSize0
            proc.stdin.write(flv_header)
            proc.stdin.write(struct.pack('>I', 0))

    def start_new_segment(self):
        """开始新段处理"""
        if self.ffmpeg_process:
            # 结束当前段的FFmpeg进程
            try:
                self.ffmpeg_process.stdin.close()
                self.ffmpeg_process.wait()
                if self.ffmpeg_process.returncode == 0:
                    print(f"  片段 {self.current_segment}: {self.current_output_file}")
                else:
                    print(f"  片段 {self.current_segment} ffmpeg 处理失败，返回码: {self.ffmpeg_process.returncode}")
            except:
                pass

        # 准备新段
        self.current_segment += 1
        base_name = os.path.splitext(os.path.basename(self.input_file))[0]
        self.current_output_file = os.path.join(self.output_dir, f"{base_name}_part{self.current_segment}.flv")

        # 启动新的FFmpeg进程
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-f', 'flv',
            '-i', 'pipe:0',
            '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            self.current_output_file
        ]

        self.ffmpeg_process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # 写入FLV头
        self.write_flv_header(self.ffmpeg_process)

        # 如果不是第一个段，添加codec headers
        if self.current_segment > 1 and self.codec_headers_extracted:
            if self.avc_seq_header:
                print(f"    包含 AVC Sequence Header")
                self.ffmpeg_process.stdin.write(self.avc_seq_header)
            if self.aac_seq_header:
                print(f"    包含 AAC Audio Sequence Header")
                self.ffmpeg_process.stdin.write(self.aac_seq_header)

        self.base_timestamp = None
        self.first_frame_in_segment = True

    def extract_codec_headers(self, full_tag_with_header, tag_type, frame_info):
        """从第一个段中提取codec headers"""
        if self.codec_headers_extracted or self.current_segment != 1:
            return

        data_size = len(full_tag_with_header) - 11  # 减去header长度

        # 查找AVC Sequence Header
        if tag_type == 9:  # 视频帧
            codec_id = frame_info & 0x0F
            if codec_id == 7:  # AVC Codec
                if data_size > 1:  # 确保有足够的数据
                    avc_type = full_tag_with_header[12]  # AVC type在offset 12
                    if avc_type == 0 and not self.avc_seq_header:  # Sequence Header
                        # 完整Tag数据: header + data + PreviousTagSize
                        previous_tag_size = struct.pack('>I', 11 + data_size)
                        self.avc_seq_header = full_tag_with_header + previous_tag_size

        # 查找AAC Audio Sequence Header
        elif tag_type == 8:  # 音频帧
            sound_format = frame_info & 0xF0
            if sound_format == 0xA0:  # AAC format
                if data_size > 1:  # 确保有足够的数据
                    aac_type = full_tag_with_header[12]  # AAC type在offset 12
                    if aac_type == 0 and not self.aac_seq_header:  # Audio Sequence Header
                        previous_tag_size = struct.pack('>I', 11 + data_size)
                        self.aac_seq_header = full_tag_with_header + previous_tag_size

        # 只有当我们确定已经处理了足够的数据后才标记为已提取
        # 这里我们不自动设置codec_headers_extracted，而是让外部逻辑控制

        # 检查是否已提取所有需要的headers（至少有一个）
        if (self.avc_seq_header is not None or self.aac_seq_header is not None):
            # 我们可以在这里设置，但为了安全起见，最好在处理完第一个段的前几个Tag后设置
            # 暂时不自动设置，让外部逻辑在适当时机设置
            pass

    def fix_timestamp_for_streaming(self, tag_header, timestamp):
        """流式修复时间戳"""
        if self.current_segment == 1:
            # 第一个段保持原始时间戳
            return timestamp

        # 找到段内第一个音视频帧的时间戳作为基准
        if self.base_timestamp is None and tag_header['tag_type'] in [8, 9]:
            self.base_timestamp = timestamp

        if self.base_timestamp is not None:
            new_ts = timestamp - self.base_timestamp
            if new_ts < 0:
                new_ts = 0
            return new_ts

        return timestamp

    def process_tag(self, tag_header, tag_data, previous_tag_size_data):
        """处理单个Tag"""
        tag_type = tag_header['tag_type']
        original_timestamp = tag_header['timestamp']

        # Script Tag检测 - 开始新段
        if tag_type == 18:
            self.script_tag_count += 1
            if self.script_tag_count > 1:
                # 在开始新段之前，确保codec headers已提取
                if self.current_segment == 1 and not self.codec_headers_extracted:
                    self.codec_headers_extracted = True
                self.start_new_segment()

        # 提取codec headers（仅在第一个段）
        if self.current_segment == 1 and tag_type in [8, 9]:
            frame_info = tag_data[0] if len(tag_data) > 0 else 0
            full_tag_data = tag_header['header'] + tag_data
            self.extract_codec_headers(full_tag_data, tag_type, frame_info)

        # 修复时间戳
        fixed_timestamp = self.fix_timestamp_for_streaming(tag_header, original_timestamp)

        # 更新Tag头部的时间戳
        new_header = bytearray(tag_header['header'])
        new_header[4:8] = struct.pack('>I', fixed_timestamp)

        # 写入到当前FFmpeg进程
        if self.ffmpeg_process and self.ffmpeg_process.stdin:
            try:
                self.ffmpeg_process.stdin.write(new_header)
                self.ffmpeg_process.stdin.write(tag_data)
                self.ffmpeg_process.stdin.write(previous_tag_size_data)
            except BrokenPipeError:
                # FFmpeg进程已结束
                pass

    def split_and_fix_flv(self):
        """流式分割和修复FLV文件"""
        print("开始流式处理FLV文件...")

        # 开始第一个段
        self.start_new_segment()

        with open(self.input_file, 'rb') as f:
            # 跳过FLV头
            flv_header = f.read(9)
            if len(flv_header) < 9:
                print("无效的FLV文件")
                return

            f.read(4)  # PreviousTagSize0

            tag_count = 0
            while True:
                tag_header = self.read_tag_header(f)
                if tag_header is None:
                    break

                # 读取Tag数据
                tag_data = f.read(tag_header['data_size'])
                if len(tag_data) < tag_header['data_size']:
                    break

                # 读取PreviousTagSize
                previous_tag_size_data = f.read(4)
                if len(previous_tag_size_data) < 4:
                    break

                # 处理Tag
                self.process_tag(tag_header, tag_data, previous_tag_size_data)
                tag_count += 1

                # 定期刷新输出（可选）
                if tag_count % 1000 == 0:
                    print(f"  已处理 {tag_count} 个Tag...")

        # 结束最后一个段
        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.stdin.close()
                self.ffmpeg_process.wait()
                if self.ffmpeg_process.returncode == 0:
                    print(f"  片段 {self.current_segment}: {self.current_output_file}")
                else:
                    print(f"  片段 {self.current_segment} ffmpeg 处理失败，返回码: {self.ffmpeg_process.returncode}")
            except:
                pass

        print(f"\n流式分割完成！找到 {self.script_tag_count} 个片段")
        print(f"输出目录: {self.output_dir}")


def split_and_fix_flv_streaming(input_file, output_dir=None):
    """
    流式分割 FLV 文件并修复时间戳偏移

    Args:
        input_file: 输入 FLV 文件路径
        output_dir: 输出目录
    """
    splitter = StreamingFLVSplitter(input_file, output_dir)
    splitter.split_and_fix_flv()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 split_flv_streaming.py <input.flv> [output_dir]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(input_file):
        print(f"文件不存在: {input_file}")
        sys.exit(1)

    split_and_fix_flv_streaming(input_file, output_dir)