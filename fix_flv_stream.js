#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const fsp = require("fs").promises;
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const TAG_TYPE_BYTES = [0x08, 0x09, 0x12];
const CHUNK_SIZE = 4 * 1024 * 1024;

function parseAacSampleRate(payload) {
  if (payload.length < 4) {
    return null;
  }

  const frequencies = [
    96000, 88200, 64000, 48000, 44100, 32000, 24000,
    22050, 16000, 12000, 11025, 8000, 7350,
  ];
  const frequencyIndex = ((payload[2] & 0x07) << 1) | (payload[3] >> 7);
  return frequencyIndex < frequencies.length ? frequencies[frequencyIndex] : null;
}

function updateProgress(label, position, total, lastPercent) {
  const percent = total === 0 ? 0 : Math.min(100, Math.floor((position * 100) / total));
  if (percent !== lastPercent) {
    process.stderr.write(`\r${label}: ${String(percent).padStart(3, " ")}%`);
  }
  return percent;
}

function finishProgress(label) {
  process.stderr.write(`\r${label}: 100%\n`);
}

function findCandidate(buffer, from) {
  let searchFrom = from;
  while (searchFrom < buffer.length) {
    let bestIndex = -1;
    for (const tagTypeByte of TAG_TYPE_BYTES) {
      const index = buffer.indexOf(tagTypeByte, searchFrom);
      if (index !== -1 && (bestIndex === -1 || index < bestIndex)) {
        bestIndex = index;
      }
    }
    if (bestIndex === -1) {
      break;
    }
    return bestIndex;
  }
  return -1;
}

async function iterTags(sourcePath, progressLabel, onTag) {
  const fileHandle = fs.openSync(sourcePath, "r");
  let lastPercent = -1;

  try {
    const stat = fs.fstatSync(fileHandle);
    const total = stat.size;
    const sizeLimit = total - 15;
    const readBuffer = Buffer.allocUnsafe(Math.min(CHUNK_SIZE, total));
    let carry = Buffer.alloc(0);
    let nextScan = 0;

    for (let position = 0; position < total; position += readBuffer.length) {
      const length = Math.min(readBuffer.length, total - position);
      const chunk = readBuffer.subarray(0, length);
      if (fs.readSync(fileHandle, chunk, 0, length, position) !== length) {
        throw new Error(`unexpected end of file: ${sourcePath}`);
      }

      const currentEnd = position + length;
      const buffer = carry.length === 0 ? chunk : Buffer.concat([carry, chunk]);
      const bufferBase = currentEnd - buffer.length;
      const scanStart = Math.max(0, nextScan - bufferBase);

      for (
        let index = findCandidate(buffer, scanStart);
        index !== -1;
        index = findCandidate(buffer, index + 1)
      ) {
        const tagOffset = bufferBase + index;
        if (tagOffset > sizeLimit) {
          continue;
        }

        if (progressLabel) {
          lastPercent = updateProgress(progressLabel, tagOffset, total, lastPercent);
        }

        const tagSize = buffer.readUIntBE(index + 1, 3);
        if (
          tagSize <= 0 ||
          tagSize > 10_000_000 ||
          tagOffset + 15 + tagSize > total
        ) {
          continue;
        }

        const previousIndex = index + 11 + tagSize;
        let previousSize;
        let payload;

        if (previousIndex + 4 <= buffer.length) {
          previousSize = buffer.readUInt32BE(previousIndex);
          payload = buffer.subarray(index + 11, index + 11 + tagSize);
        } else {
          const previousBuffer = Buffer.alloc(4);
          fs.readSync(
            fileHandle,
            previousBuffer,
            0,
            4,
            tagOffset + 11 + tagSize,
          );
          previousSize = previousBuffer.readUInt32BE(0);
          if (previousSize !== tagSize + 11) {
            continue;
          }

          payload = Buffer.allocUnsafe(tagSize);
          if (fs.readSync(fileHandle, payload, 0, tagSize, tagOffset + 11) !== tagSize) {
            continue;
          }
        }

        if (previousSize !== tagSize + 11) {
          continue;
        }

        const lowTimestamp = buffer.readUIntBE(index + 4, 3);
        const highTimestamp = buffer[index + 7];
        const timestamp = (highTimestamp << 24) | lowTimestamp;
        const streamId = buffer.readUIntBE(index + 8, 3);
        await onTag({
          type: buffer[index],
          size: tagSize,
          timestamp: timestamp >>> 0,
          streamId,
          offset: tagOffset,
          payload,
        });
      }

      nextScan = Math.max(nextScan, currentEnd - 15);
      carry = buffer.subarray(Math.max(0, buffer.length - 15));
    }

    if (progressLabel) {
      finishProgress(progressLabel);
    }
  } finally {
    fs.closeSync(fileHandle);
  }
}

function duplicatePrefixCount(keys) {
  if (keys.length === 0) {
    return 0;
  }

  const counts = new Map();
  for (const key of keys) {
    counts.set(key.key, (counts.get(key.key) || 0) + 1);
  }

  let count = 0;
  for (const key of keys) {
    if ((counts.get(key.key) || 0) < 2) {
      break;
    }
    count += 1;
  }
  return count;
}

async function inspectTags(sourcePath) {
  let audioPackets = 0;
  let videoPackets = 0;
  let sampleRate = null;
  let hasAudioConfig = false;
  let hasVideoConfig = false;
  const audioKeys = [];
  const videoKeys = [];

  await iterTags(sourcePath, "scanning", (tag) => {
    const payload = tag.payload;
    if (tag.type === 8 && payload.length > 0 && (payload[0] >> 4) === 10) {
      if (payload.length > 1 && payload[1] === 0) {
        hasAudioConfig = true;
        sampleRate = sampleRate || parseAacSampleRate(payload);
      } else if (payload.length > 1 && sampleRate) {
        audioPackets += 1;
        const digest = crypto.createHash("sha1").update(payload).digest("hex");
        audioKeys.push({ key: `${digest}:${tag.timestamp}`, timestamp: tag.timestamp });
      }
    } else if (tag.type === 9 && payload.length > 0 && (payload[0] & 0x0f) === 7) {
      if (payload.length > 1 && payload[1] === 0) {
        hasVideoConfig = true;
      } else if (payload.length >= 5) {
        videoPackets += 1;
        const digest = crypto.createHash("sha1").update(payload).digest("hex");
        videoKeys.push({ key: `${digest}:${tag.timestamp}`, timestamp: tag.timestamp });
      }
    }
  });

  const audioPrefixSkip = duplicatePrefixCount(audioKeys);
  const videoPrefixSkip = duplicatePrefixCount(videoKeys);
  audioPackets -= audioPrefixSkip;
  videoPackets -= videoPrefixSkip;

  if (audioKeys.length === 0 || videoKeys.length === 0) {
    throw new Error("not enough valid audio/video packets");
  }

  const base = Math.min(
    audioKeys[audioPrefixSkip].timestamp,
    videoKeys[videoPrefixSkip].timestamp,
  );

  return {
    audioPackets,
    videoPackets,
    sampleRate,
    hasAudioConfig,
    hasVideoConfig,
    audioPrefixSkip,
    videoPrefixSkip,
    base,
  };
}

function writeTag(fileHandle, tagType, timestamp, payload) {
  const size = payload.length;
  const timestampBytes = Buffer.alloc(4);
  timestampBytes.writeUInt32BE(timestamp, 0);

  const tag = Buffer.allocUnsafe(11 + size + 4);
  tag[0] = tagType;
  tag.writeUIntBE(size, 1, 3);
  tag.set(timestampBytes.subarray(1, 4), 4);
  tag.set(timestampBytes.subarray(0, 1), 7);
  tag.set(Buffer.from([0, 0, 0]), 8);
  payload.copy(tag, 11);
  tag.writeUInt32BE(size + 11, 11 + size);
  return fileHandle.write(tag);
}

async function writeCleanFlv(sourcePath, outputPath, info) {
  const outputHandle = await fsp.open(outputPath, "w");
  let wroteAudioConfig = false;
  let wroteVideoConfig = false;
  let started = false;
  let audioSeen = 0;
  let videoSeen = 0;

  try {
    await outputHandle.write(Buffer.from("FLV\x01\x05\x00\x00\x00\t", "binary"));
    await outputHandle.write(Buffer.from([0, 0, 0, 0]));

    await iterTags(sourcePath, "rewriting", async (tag) => {
      const payload = tag.payload;
      if (tag.type === 9 && payload.length > 0 && (payload[0] & 0x0f) === 7) {
        if (payload.length > 1 && payload[1] === 0) {
          if (!wroteVideoConfig) {
            await writeTag(outputHandle, 9, 0, payload);
            wroteVideoConfig = true;
          }
        } else if (payload.length >= 5) {
          if (videoSeen < info.videoPrefixSkip) {
            videoSeen += 1;
            return;
          }
          if (!started && (payload[0] >> 4) !== 1) {
            videoSeen += 1;
            return;
          }
          started = true;
          await writeTag(outputHandle, 9, Math.max(0, tag.timestamp - info.base), payload);
          videoSeen += 1;
        }
      } else if (tag.type === 8 && payload.length > 0 && (payload[0] >> 4) === 10) {
        if (payload.length > 1 && payload[1] === 0) {
          if (!wroteAudioConfig) {
            await writeTag(outputHandle, 8, 0, payload);
            wroteAudioConfig = true;
          }
        } else if (payload.length > 1) {
          if (audioSeen < info.audioPrefixSkip) {
            audioSeen += 1;
            return;
          }
          if (!started) {
            audioSeen += 1;
            return;
          }
          await writeTag(outputHandle, 8, Math.max(0, tag.timestamp - info.base), payload);
          audioSeen += 1;
        }
      }
    });
  } finally {
    await outputHandle.close();
  }
}

function runFfmpeg(args) {
  return new Promise((resolve, reject) => {
    const child = spawn("ffmpeg", args, { stdio: "inherit" });
    child.on("error", reject);
    child.on("exit", (code, signal) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`ffmpeg exited with ${signal || `code ${code}`}`));
      }
    });
  });
}

async function probeOutput(outputPath) {
  const child = spawn("ffprobe", [
    "-v", "error", "-show_streams", "-of", "json", outputPath,
  ], { stdio: ["ignore", "pipe", "pipe"] });

  let stdout = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
  });

  try {
    const [code] = await Promise.all([
      new Promise((resolve, reject) => {
        child.on("error", reject);
        child.on("exit", resolve);
      }),
      new Promise((resolve) => {
        child.stderr.on("end", resolve);
        child.stderr.resume();
      }),
    ]);
    if (code !== 0) {
      return null;
    }

    const streams = JSON.parse(stdout).streams || [];
    const video = streams.find((stream) => stream.codec_type === "video");
    const audio = streams.find((stream) => stream.codec_type === "audio");
    if (!video || !audio) {
      return null;
    }
    return [Number.parseFloat(video.duration || 0), Number.parseFloat(audio.duration || 0)];
  } catch (error) {
    child.kill();
    return null;
  }
}

async function repair(source, output) {
  const info = await inspectTags(source);
  console.log(
    `valid audio packets: ${info.audioPackets}, valid video packets: ${info.videoPackets}, sample rate: ${info.sampleRate}`,
  );

  const tempDir = await fsp.mkdtemp(path.join(path.dirname(output), "fix_flv_stream-"));
  const cleanFlv = path.join(tempDir, `${path.basename(source, path.extname(source))}.clean.flv`);
  try {
    await writeCleanFlv(source, cleanFlv, info);
    await runFfmpeg([
      "-hide_banner",
      "-v", "warning",
      "-stats",
      "-i", cleanFlv,
      "-map", "0:v:0",
      "-map", "0:a:0",
      "-c", "copy",
      "-movflags", "+faststart",
      "-y", output,
    ]);

    const durations = await probeOutput(output);
    if (!durations) {
      return false;
    }
    const [videoDuration, audioDuration] = durations;
    console.log(`video duration: ${videoDuration.toFixed(3)}s, audio duration: ${audioDuration.toFixed(3)}s`);
    return Math.abs(videoDuration - audioDuration) < 0.5;
  } finally {
    await fsp.rm(tempDir, { recursive: true, force: true });
  }
}

async function iterFlvFiles(directory) {
  const files = [];
  async function walk(currentPath) {
    const entries = await fsp.readdir(currentPath, { withFileTypes: true });
    for (const entry of entries) {
      const entryPath = path.join(currentPath, entry.name);
      if (entry.isDirectory()) {
        await walk(entryPath);
      } else if (entry.isFile() && path.extname(entry.name).toLowerCase() === ".flv") {
        files.push(entryPath);
      }
    }
  }

  await walk(directory);
  return files.sort();
}

async function removeOutput(output) {
  try {
    await fsp.unlink(output);
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
}

async function repairOne(source, output, overwrite) {
  try {
    await fsp.access(output);
    if (!overwrite) {
      console.log(`skip: ${path.basename(output)} already exists`);
      return true;
    }
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }

  try {
    if (!(await repair(source, output))) {
      await removeOutput(output);
      return false;
    }
  } catch (error) {
    await removeOutput(output);
    console.error(`error: ${error.message}`);
    return false;
  }
  return true;
}

async function repairDirectory(source, outputRoot, overwrite) {
  await fsp.mkdir(outputRoot, { recursive: true });
  const files = await iterFlvFiles(source);
  if (files.length === 0) {
    console.log("No FLV files found.");
    return 0;
  }

  const failed = [];
  for (const sourceFile of files) {
    const parsed = path.parse(sourceFile);
    const outputFile = path.join(outputRoot, `${parsed.name}_streamcopy.mp4`);
    console.log(`\n${sourceFile}`);
    if (!(await repairOne(sourceFile, outputFile, overwrite))) {
      failed.push(sourceFile);
    }
  }

  if (failed.length > 0) {
    console.log(`\n${failed.length} file(s) failed:`);
    for (const sourceFile of failed) {
      console.log(`  ${sourceFile}`);
    }
    return 1;
  }
  return 0;
}

function usage() {
  process.stderr.write([
    "usage: fix_flv_stream.js [--overwrite] source [output]",
    "",
    "Stream-parse an FLV, drop invalid tags, and stream-copy A/V into MP4.",
  ].join("\n") + "\n");
}

async function main() {
  const args = process.argv.slice(2);
  let overwrite = false;
  const positional = [];
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === "--overwrite") {
      overwrite = true;
    } else if (argument === "--help" || argument === "-h") {
      usage();
      return 0;
    } else if (argument.startsWith("--")) {
      throw new Error(`unrecognized argument: ${argument}`);
    } else {
      positional.push(argument);
    }
  }

  if (positional.length < 1 || positional.length > 2) {
    usage();
    process.stderr.write("error: expected one source and optional output\n");
    return 2;
  }

  const source = path.resolve(positional[0].replace(/^~(?=$|\/)/, os.homedir()));
  const sourceStat = await fsp.stat(source);

  if (sourceStat.isFile()) {
    const output = positional[1]
      ? path.resolve(positional[1].replace(/^~(?=$|\/)/, os.homedir()))
      : path.join(path.dirname(source), `${path.basename(source, path.extname(source))}_streamcopy.mp4`);
    return (await repairOne(source, output, overwrite)) ? 0 : 1;
  }

  if (sourceStat.isDirectory()) {
    const outputRoot = positional[1]
      ? path.resolve(positional[1].replace(/^~(?=$|\/)/, os.homedir()))
      : source;
    return await repairDirectory(source, outputRoot, overwrite);
  }

  usage();
  process.stderr.write(`error: not a file or directory: ${source}\n`);
  return 2;
}

module.exports = { iterTags, inspectTags };

if (require.main === module) {
  main().then((code) => {
    process.exitCode = code;
  }).catch((error) => {
    console.error(`error: ${error.message}`);
    process.exitCode = 2;
  });
}
