#!/usr/bin/env python3
"""
通过图像分析检测PK场景
PK场景特征：左右分屏结构
依赖: Pillow (pip3 install pillow)
"""

from PIL import Image
import os
import subprocess
import sys
from pathlib import Path

def get_gray_array(img):
    """将图片转为灰度数组"""
    gray = img.convert('L')
    return list(gray.getdata())

def detect_split_screen(image_path):
    """
    检测图片是否存在左右分屏结构（PK场景特征）
    返回: (是否分屏, 分隔线位置比例)
    """
    try:
        img = Image.open(image_path)
    except Exception:
        return False, 0

    w, h = img.size
    gray_data = get_gray_array(img)

    # 方法1: 检测中间垂直分隔线
    mid_start = w // 3
    mid_end = 2 * w // 3

    best_line = None
    best_score = 0

    for x in range(mid_start, mid_end):
        left_vals = []
        right_vals = []
        for y in range(h):
            idx = y * w + x
            if idx > 0:
                left_vals.append(gray_data[idx - 1])
            if idx + 1 < len(gray_data):
                right_vals.append(gray_data[idx + 1])

        if left_vals and right_vals:
            diff = sum(abs(a - b) for a, b in zip(left_vals, right_vals)) / h
            if diff > best_score:
                best_score = diff
                best_line = x

    if best_score > 15:
        line_pos = best_line / w
        return True, line_pos

    # 方法2: 检测左右两侧的相似度
    thumb_w, thumb_h = 100, 100
    thumb = img.resize((thumb_w, thumb_h)).convert('L')
    thumb_data = list(thumb.getdata())

    left_data = thumb_data[:thumb_w * thumb_h // 2]
    right_data = thumb_data[thumb_w * thumb_h // 2:]

    left_avg = sum(left_data) / len(left_data)
    right_avg = sum(right_data) / len(right_data)
    brightness_diff = abs(left_avg - right_avg)

    mid_data = [thumb_data[i] for i in range(thumb_w * thumb_h // 2, thumb_w * thumb_h // 2 + thumb_w)]
    mid_avg = sum(mid_data) / len(mid_data)
    side_avg = (left_avg + right_avg) / 2

    if mid_avg < side_avg * 0.9 and brightness_diff > 20:
        return True, 0.5

    return False, 0


def extract_frames(video_path, output_dir, num_frames=6):
    """使用ffmpeg从视频中提取帧截图"""
    os.makedirs(output_dir, exist_ok=True)

    print(f"从视频提取截图: {video_path}")

    # 获取视频时长
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
        capture_output=True, text=True
    )
    try:
        duration = float(result.stdout.strip())
    except:
        duration = 60

    # 均匀采样
    interval = max(1, int(duration / num_frames))
    screenshots = []

    for i in range(num_frames):
        timestamp = i * interval
        output_path = os.path.join(output_dir, f"frame_{i:03d}.jpg")
        cmd = [
            'ffmpeg', '-ss', str(timestamp),
            '-i', video_path,
            '-frames:v', '1',
            '-q:v', '2',
            '-y', output_path
        ]
        subprocess.run(cmd, capture_output=True)
        if os.path.exists(output_path):
            screenshots.append(output_path)
            print(f"  提取帧 {i+1}/{num_frames}: {timestamp}s -> frame_{i:03d}.jpg")

    return screenshots


def check_pk_in_video(video_path, num_frames=6):
    """检测视频中是否包含PK场景"""
    output_dir = "/tmp/pk_screenshots"

    # 提取截图
    screenshots = extract_frames(video_path, output_dir, num_frames)

    if not screenshots:
        print("无法提取视频帧")
        return

    print(f"\n检测 {len(screenshots)} 张截图...\n")

    pk_count = 0

    for img_path in screenshots:
        is_split, pos = detect_split_screen(img_path)

        if is_split:
            pk_count += 1
            print(f"[PK]   {os.path.basename(img_path)}")
        else:
            print(f"[OK]   {os.path.basename(img_path)}")

    print(f"\n{'='*50}")
    print(f"总结: {pk_count}/{len(screenshots)} 张截图检测到PK分屏结构")

    is_pk = pk_count > len(screenshots) * 0.3

    if is_pk:
        print(f"结论: {video_path} 包含PK场景")
    else:
        print(f"结论: {video_path} 不包含PK场景（普通直播）")

    # 清理临时文件
    for f in screenshots:
        os.remove(f)
    os.rmdir(output_dir)

    return is_pk


def check_pk_in_screenshots(screenshot_dir):
    """检测目录下所有截图是否存在PK分屏"""
    path = Path(screenshot_dir)
    images = sorted(path.glob("*.jpg")) + sorted(path.glob("*.png"))

    if not images:
        print(f"未找到截图文件: {screenshot_dir}")
        return

    print(f"检测 {len(images)} 张截图...\n")

    pk_count = 0

    for img_path in images:
        is_split, pos = detect_split_screen(str(img_path))

        if is_split:
            pk_count += 1
            print(f"[PK]   {img_path.name}")
        else:
            print(f"[OK]   {img_path.name}")

    print(f"\n{'='*50}")
    print(f"总结: {pk_count}/{len(images)} 张截图检测到PK分屏结构")

    if pk_count > len(images) * 0.3:
        print("结论: 该视频包含PK场景")
    else:
        print("结论: 该视频不包含PK场景（普通直播）")


def check_and_delete_pk_videos(directory, num_frames=6, dry_run=True):
    """检测目录下所有视频，删除包含PK场景的文件"""
    dir_path = Path(directory)
    videos = sorted(dir_path.glob("*.flv"))

    if not videos:
        print(f"目录下未找到flv文件: {directory}")
        return

    print(f"检测 {len(videos)} 个视频文件...\n")

    pk_files = []
    non_pk_files = []

    for video_path in videos:
        print(f"检查: {video_path.name}")
        is_pk = check_pk_in_video(video_path, num_frames)

        if is_pk:
            pk_files.append(video_path)
            print(f"  -> 包含PK场景，将删除")
        else:
            non_pk_files.append(video_path)
            print(f"  -> 普通直播，保留")

    print(f"\n{'='*50}")
    print(f"总结: {len(pk_files)}/{len(videos)} 个文件包含PK场景")

    if pk_files:
        print(f"\n将删除 {len(pk_files)} 个文件:")
        for f in pk_files:
            print(f"  - {f.name}")

        if not dry_run:
            print("\n正在删除...")
            for f in pk_files:
                os.remove(f)
                print(f"  已删除: {f.name}")
            print("删除完成")
        else:
            print("\n(使用 --dry-run 仅预览)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  检测并删除:     python3 check_pk.py /path/to/dir")
        print("  仅预览不删除:   python3 check_pk.py /path/to/dir --dry-run")
        # print("  指定帧数:       python3 check_pk.py /path/to/dir 10")
        sys.exit(1)

    directory = sys.argv[1]
    dry_run = '--dry-run' in sys.argv
    num_frames = 2

    # for arg in sys.argv[2:]:
    #     if arg.isdigit():
    #         num_frames = int(arg)

    if not os.path.isdir(directory):
        print(f"目录不存在: {directory}")
        sys.exit(1)

    check_and_delete_pk_videos(directory, num_frames, dry_run)
