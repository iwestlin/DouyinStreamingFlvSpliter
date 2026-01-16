# 抖音直播flv分割工具

这是一个用于处理包含多个片段的（通过直播推流直接下载的）原始抖音直播flv文件的分割工具。它能够智能地检测和分割FLV文件中的多个视频段（比如PK），并自动修复时间戳偏移问题。

## 特性

- **可选流式**: 提供流式处理版本，逐个Tag读取，避免一次性加载整个文件到内存
- **智能分割**: 自动检测Script Tag（onMetaData）作为段边界
- **自动修复**:
  - 自动提取并注入AVC/AAC编解码器头
  - 修复时间戳偏移，确保每个段从0开始
- **FFmpeg集成**: 使用FFmpeg处理源数据，确保生成的片段时长准确

## 安装依赖

### 系统要求
- Python 3.6+
- FFmpeg (必须安装)

### 安装FFmpeg

**macOS (使用Homebrew):**
```bash
brew install ffmpeg
```

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install ffmpeg
```

**Windows:**
- 从 [FFmpeg官网](https://ffmpeg.org/download.html) 下载并添加到PATH

## 使用方法

### 基本用法
```bash
# 非流式处理版本（适用于小文件）｜ 此文件为claude code+minimaxi vibe coding生成
python3 split_flv.py input.flv [output_directory]

# 流式处理版本（推荐用于大文件）| 此文件为CC+qwen3-max基于上述文件生成
python3 split_flv_streaming.py input.flv [output_directory]
```

### 参数说明
- `input.flv`: 输入的FLV文件路径（必需）
- `output_directory`: 输出目录（可选，默认为 `split_output`）

## 输出结果  
工具会将输入的FLV文件分割成多个独立的FLV文件，每个分割后的文件都是完整的、可播放的FLV文件。

## 技术原理

### FLV文件结构
FLV文件由以下部分组成：
- **Header**: 9字节文件头
- **Body**: 包含多个Tag
  - **Script Tag (type 18)**: 包含元数据（如onMetaData）
  - **Video Tag (type 9)**: 视频帧数据
  - **Audio Tag (type 8)**: 音频帧数据

### 分割逻辑
1. **段检测**: 查找所有Script Tag作为段边界
2. **Codec Header提取**: 从第一个段中提取AVC Sequence Header和AAC Audio Sequence Header
3. **时间戳修复**: 为每个后续段重新计算时间戳，确保从0开始

## 常见问题

### Q: 为什么需要这个工具？
A: 许多屏幕录制软件或直播录制工具会生成包含多个片段的FLV文件，这些文件在播放时会出现问题。此工具可以将它们分割成独立的、可正常播放的视频文件。

### Q: 处理过程中出现FFmpeg错误怎么办？
A: 确保FFmpeg已正确安装并添加到系统PATH。可以运行 `ffmpeg -version` 来验证。

### Q: 如何处理没有Script Tag的文件？
A: 如果文件只包含一个片段（没有多个Script Tag），工具会直接输出原文件，无需分割。

### 文件结构
- `split_flv.py`: 炸内存版本
- `split_flv_streaming.py`: 流式处理版本（推荐使用）

## 其它工具  
本项目为作者vibe coding娱乐向，推荐使用 [https://rec.danmuji.org/user/toolbox/analyze-repair/](https://rec.danmuji.org/user/toolbox/analyze-repair/) 的

```bash
./BililiveRecorder.Cli tool fix "input.flv" "output.flv" --pipeline-settings '{"SplitOnScriptTag": true}'
```

作为替代
