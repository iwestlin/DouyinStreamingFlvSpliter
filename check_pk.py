#!/usr/bin/env python3
"""
通过图像分析检测PK场景
PK场景特征：上下黑边
依赖: Pillow (pip3 install pillow)
"""

from PIL import Image
import os
import subprocess
import sys
from pathlib import Path
import mimetypes

def is_video_file(file_path):
    """使用 mimetypes 检测文件类型"""
    mime_type, _ = mimetypes.guess_type(str(file_path))
    return mime_type is not None and mime_type.startswith('video/')

def get_gray_array(img):
    """将图片转为灰度数组"""
    gray = img.convert('L')
    return list(gray.getdata())

def detect_split_screen(image_path):
    """
    检测图片是否存在PK场景
    PK视频特征：1:1正方形画面在9:16竖屏设备上播放时，上下会有单色填充块
    返回: (是否PK, 0)
    """
    try:
        img = Image.open(image_path)
    except Exception:
        return False, 0

    w, h = img.size
    gray_data = get_gray_array(img)

    # 跳过纯黑/过暗的画面（开场黑屏等）
    avg_brightness = sum(gray_data) / len(gray_data)
    if avg_brightness < 20:
        return False, 0

    # 检测上下单色填充块（像素变化很小）
    # 使用缩略图加速计算
    thumb = img.resize((w // 4, h // 4)).convert('L')
    thumb_data = list(thumb.getdata())
    tw, th = w // 4, h // 4

    # 检测上填充块
    top_fill = 0
    top_end_y = 0  # 记录上填充块的结束位置
    for y in range(th):
        row = thumb_data[y * tw:(y + 1) * tw]
        row_avg = sum(row) / len(row)
        row_std = (sum((x - row_avg) ** 2 for x in row) / len(row)) ** 0.5
        if row_std < 10:  # 颜色均匀
            top_fill += 1
            top_end_y = y
        else:
            break

    # 检测下填充块
    bottom_fill = 0
    bottom_start_y = th  # 记录下填充块的开始位置
    for y in range(th - 1, -1, -1):
        row = thumb_data[y * tw:(y + 1) * tw]
        row_avg = sum(row) / len(row)
        row_std = (sum((x - row_avg) ** 2 for x in row) / len(row)) ** 0.5
        if row_std < 10:
            bottom_fill += 1
            bottom_start_y = y
        else:
            break

    total_fill = top_fill + bottom_fill
    fill_ratio = total_fill / th

    # 判定PK：上下填充块占比超过15% 且有明显的水平分割线
    if fill_ratio > 0.15 and top_fill > 0 and bottom_fill > 0:
        if check_horizontal_split_line(thumb_data, tw, th, top_fill, bottom_start_y):
            return True, 0
        return False, 0

    return False, 0


def check_horizontal_split_line(gray_data, w, h, top_fill, bottom_start_y):
    """
    检测上下纯色块与中间内容区域之间是否有明显的水平分割线
    PK场景中，纯色填充块和正常画面之间应该有明显的边界
    """
    if h < 4 or top_fill >= h or bottom_start_y <= 0:
        return False

    # 检查上分割线：上填充块的最后一行与中间内容的第一行之间
    top_line_y = top_fill - 1
    top_fill_row = gray_data[top_line_y * w:(top_line_y + 1) * w]
    content_row = gray_data[(top_fill) * w:(top_fill + 1) * w]

    top_fill_avg = sum(top_fill_row) / len(top_fill_row)
    content_avg = sum(content_row) / len(content_row)
    top_diff = abs(top_fill_avg - content_avg)

    # 检查下分割线：中间内容的最后一行与下填充块的第一行之间
    content_bottom_row = gray_data[(bottom_start_y - 1) * w:bottom_start_y * w]
    bottom_fill_row = gray_data[bottom_start_y * w:(bottom_start_y + 1) * w]

    content_bottom_avg = sum(content_bottom_row) / len(content_bottom_row)
    bottom_fill_avg = sum(bottom_fill_row) / len(bottom_fill_row)
    bottom_diff = abs(content_bottom_avg - bottom_fill_avg)

    # PK场景判定：上下都有明显的分割线（亮度差异明显）
    return top_diff > 50 and bottom_diff > 50


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

    # 均匀采样，确保timestamp不超出视频时长
    interval = max(1, int(duration / num_frames))
    screenshots = []

    for i in range(num_frames):
        timestamp = min(i * interval, duration - 0.1)
        output_path = os.path.join(output_dir, f"frame_{i:03d}.jpg")
        cmd = [
            'ffmpeg',
            '-ss', str(timestamp),
            '-i', video_path,
            '-frames:v', '1',
            '-q:v', '10',
            '-y', output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if os.path.exists(output_path):
            screenshots.append(output_path)
            print(f"  提取帧 {i+1}/{num_frames}: {timestamp}s -> frame_{i:03d}.jpg")
        elif i == 0:
            # 如果第一帧就失败，尝试用select滤镜提取第一帧
            cmd_select = [
                'ffmpeg',
                '-i', video_path,
                '-vf', 'select=eq(n\\,0)',
                '-frames:v', '1',
                '-q:v', '10',
                '-y', output_path
            ]
            subprocess.run(cmd_select, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(output_path):
                screenshots.append(output_path)
                print(f"  提取帧 1/1: 0s -> frame_000.jpg (备用方法)")

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
    # videos = sorted(dir_path.glob("*.flv"))
    videos = sorted(
        p for p in dir_path.iterdir() 
        if is_video_file(p)
    )

    if not videos:
        print(f"目录下未找到视频文件: {directory}")
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
            size_mb = f.stat().st_size / (1024 * 1024)
            print(f"  - {f.name} ({size_mb:.1f} MB)")

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
