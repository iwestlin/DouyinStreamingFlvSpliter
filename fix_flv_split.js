#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const DISCONTINUITY_MS = 1000;
const MAX_TAG_SIZE = 10_000_000;
const SAMPLE_SECONDS = 6.0;
const SAMPLE_MAX_BYTES = 12_000_000;
const TAG_TYPE_BYTES = [0x08, 0x09, 0x12];
const VIDEO = 9;
const AUDIO = 8;
const SCRIPT = 18;
const FLV_HEADER = Buffer.from([0x46, 0x4c, 0x56, 0x01, 0x05, 0x00, 0x00, 0x00, 0x09]);
const PREV_TAG = Buffer.from([0x00, 0x00, 0x00, 0x00]);
const HIGH_PROFILE = new Set([100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135]);
const CHROMA_CROP_X = { 1: 2, 2: 2, 3: 1 };
const CHROMA_CROP_Y = { 1: 2, 2: 1, 3: 1 };

class Piece {
  constructor(start) {
    this.start = start;
    this.end = 0;
    this.vhash = "";
    this.ahash = "";
    this.vcfg = null;
    this.acfg = null;
    this.res = "?";
    this.ts_min = null;
    this.ts_max = null;
    this.video = 0;
    this.audio = 0;
    this.keyframes = [];
  }

  get codec() {
    return `${this.vhash}/${this.ahash}`;
  }

  get media() {
    return this.video + this.audio;
  }
}

class BitReader {
  constructor(data) {
    this.data = data;
    this.pos = 0;
  }

  bit() {
    if (this.pos >> 3 >= this.data.length) {
      throw new Error("bit reader out of range");
    }
    const value = (this.data[this.pos >> 3] >> (7 - (this.pos & 7))) & 1;
    this.pos += 1;
    return value;
  }

  bits(count) {
    let value = 0;
    for (let index = 0; index < count; index += 1) {
      value = value * 2 + this.bit();
    }
    return value;
  }

  ue() {
    let zeros = 0;
    while (this.bit() === 0) {
      zeros += 1;
    }
    if (zeros === 0) {
      return 0;
    }
    return 2 ** zeros - 1 + this.bits(zeros);
  }

  se() {
    const value = this.ue();
    return value & 1 ? (value + 1) / 2 : -(value / 2);
  }
}

function unescapeAvcc(data) {
  const out = [];
  let index = 0;
  while (index < data.length) {
    if (
      index + 2 < data.length &&
      data[index] === 0 &&
      data[index + 1] === 0 &&
      data[index + 2] === 3
    ) {
      out.push(0, 0);
      index += 3;
    } else {
      out.push(data[index]);
      index += 1;
    }
  }
  return Buffer.from(out);
}

function avcResolution(payload) {
  try {
    if (payload.length < 6) {
      return "?";
    }
    const record = payload.subarray(5);
    if (record.length < 8) {
      return "?";
    }
    const numSps = record[5] & 0x1f;
    if (numSps === 0) {
      return "?";
    }
    const length = (record[6] << 8) | record[7];
    if (record.length < 8 + length) {
      return "?";
    }
    const sps = record.subarray(8, 8 + length);
    if (sps.length < 4 || (sps[0] & 0x1f) !== 7) {
      return "?";
    }
    const reader = new BitReader(unescapeAvcc(sps.subarray(1)));
    const profile = reader.bits(8);
    reader.bits(8);
    reader.bits(8);
    reader.ue();
    let chromaFormatIdc = 1;
    let separateColourPlane = 0;
    if (HIGH_PROFILE.has(profile)) {
      chromaFormatIdc = reader.ue();
      if (chromaFormatIdc === 3) {
        separateColourPlane = reader.bit();
      }
      reader.ue();
      reader.ue();
      reader.bit();
      if (reader.bit()) {
        const count = chromaFormatIdc !== 3 ? 8 : 12;
        for (let index = 0; index < count; index += 1) {
          if (reader.bit()) {
            const size = index < 6 ? 16 : 64;
            let last = 8;
            let next = 8;
            for (let step = 0; step < size; step += 1) {
              if (next !== 0) {
                next = (last + reader.se() + 256) % 256;
              }
              last = next === 0 ? last : next;
            }
          }
        }
      }
    }
    reader.ue();
    const pocType = reader.ue();
    if (pocType === 0) {
      reader.ue();
    } else if (pocType === 1) {
      reader.bit();
      reader.se();
      reader.se();
      const count = reader.ue();
      for (let index = 0; index < count; index += 1) {
        reader.se();
      }
    }
    reader.ue();
    reader.bit();
    const widthMbs = reader.ue() + 1;
    const heightUnits = reader.ue() + 1;
    const frameMbsOnly = reader.bit();
    if (!frameMbsOnly) {
      reader.bit();
    }
    reader.bit();
    let cropLeft = 0;
    let cropRight = 0;
    let cropTop = 0;
    let cropBottom = 0;
    if (reader.bit()) {
      cropLeft = reader.ue();
      cropRight = reader.ue();
      cropTop = reader.ue();
      cropBottom = reader.ue();
    }
    let width = widthMbs * 16;
    let height = heightUnits * 16 * (2 - frameMbsOnly);
    let cropUnitX;
    let cropUnitY;
    if (chromaFormatIdc === 0 || separateColourPlane) {
      cropUnitX = 1;
      cropUnitY = 2 - frameMbsOnly;
    } else {
      cropUnitX = CHROMA_CROP_X[chromaFormatIdc];
      cropUnitY = CHROMA_CROP_Y[chromaFormatIdc] * (2 - frameMbsOnly);
    }
    width -= cropUnitX * (cropLeft + cropRight);
    height -= cropUnitY * (cropTop + cropBottom);
    return `${width}x${height}`;
  } catch (error) {
    return "?";
  }
}

function findCandidate(buffer, from, end) {
  let best = -1;
  for (const tagTypeByte of TAG_TYPE_BYTES) {
    const index = buffer.indexOf(tagTypeByte, from);
    if (index !== -1 && index < end && (best === -1 || index < best)) {
      best = index;
    }
  }
  return best;
}

function* iterTags(buffer, start, end) {
  const limit = end - 15;
  for (
    let index = findCandidate(buffer, start, end);
    index !== -1;
    index = findCandidate(buffer, index + 1, end)
  ) {
    if (index > limit) {
      continue;
    }
    const tagType = buffer[index];
    const tagSize = buffer.readUIntBE(index + 1, 3);
    if (tagSize <= 0 || tagSize > MAX_TAG_SIZE || index + 15 + tagSize > end) {
      continue;
    }
    if (buffer.readUInt32BE(index + 11 + tagSize) !== tagSize + 11) {
      continue;
    }
    const timestamp = buffer[index + 7] * 0x1000000 + buffer.readUIntBE(index + 4, 3);
    yield { tagType, timestamp, offset: index, tagSize };
  }
}

function encodeTag(tagType, timestamp, payload) {
  const size = payload.length;
  const stamp = Buffer.allocUnsafe(4);
  stamp.writeUInt32BE(Math.max(0, Math.trunc(timestamp)) % 0x100000000, 0);
  const head = Buffer.allocUnsafe(11);
  head[0] = tagType;
  head.writeUIntBE(size, 1, 3);
  head[4] = stamp[1];
  head[5] = stamp[2];
  head[6] = stamp[3];
  head[7] = stamp[0];
  head[8] = 0;
  head[9] = 0;
  head[10] = 0;
  const tail = Buffer.allocUnsafe(4);
  tail.writeUInt32BE(size + 11, 0);
  return Buffer.concat([head, payload, tail]);
}

function updateProgress(label, position, total, lastPercent) {
  const percent = total <= 0 ? 0 : Math.min(100, Math.floor((position * 100) / total));
  if (percent !== lastPercent) {
    process.stderr.write(`\r${label}: ${String(percent).padStart(3, " ")}%`);
  }
  return percent;
}

function finishProgress(label) {
  process.stderr.write(`\r${label}: 100%\n`);
}

function sha1Short(payload) {
  return crypto.createHash("sha1").update(payload).digest("hex").slice(0, 8);
}

function scan(buffer) {
  const pieces = [];
  const configs = new Map();
  let audioPayload = null;
  let vhash = "";
  let ahash = "";
  let vcfg = null;
  let acfg = null;
  let res = "?";
  const size = buffer.length;
  const limit = size - 15;
  let current = new Piece(0);
  let lastTimestamp = null;
  let lastPercent = -1;

  for (
    let offset = findCandidate(buffer, 0, size);
    offset !== -1;
    offset = findCandidate(buffer, offset + 1, size)
  ) {
    lastPercent = updateProgress("scanning", offset, size, lastPercent);
    if (offset > limit) {
      continue;
    }
    const tagType = buffer[offset];
    const tagSize = buffer.readUIntBE(offset + 1, 3);
    if (tagSize <= 0 || tagSize > MAX_TAG_SIZE || offset + 15 + tagSize > size) {
      continue;
    }
    if (buffer.readUInt32BE(offset + 11 + tagSize) !== tagSize + 11) {
      continue;
    }
    const timestamp = buffer[offset + 7] * 0x1000000 + buffer.readUIntBE(offset + 4, 3);
    const header = buffer[offset + 11];
    const packetType = tagSize >= 2 ? buffer[offset + 12] : -1;
    const isVcfg = tagType === VIDEO && (header & 0x0f) === 7 && packetType === 0;
    const isAcfg = tagType === AUDIO && (header >> 4) === 10 && packetType === 0;

    let newVideo = false;
    let newAudio = false;
    if (isVcfg) {
      const payload = Buffer.from(buffer.subarray(offset + 11, offset + 11 + tagSize));
      const digest = sha1Short(payload);
      if (!configs.has(digest)) {
        configs.set(digest, { payload, res: avcResolution(payload) });
      }
      if (digest !== vhash) {
        vhash = digest;
        vcfg = payload;
        res = configs.get(digest).res;
        newVideo = true;
      }
    }
    if (isAcfg) {
      const payload = Buffer.from(buffer.subarray(offset + 11, offset + 11 + tagSize));
      const digest = sha1Short(payload);
      if (digest !== ahash) {
        ahash = digest;
        acfg = payload;
        audioPayload = payload;
        newAudio = true;
      }
    }

    const regressed =
      lastTimestamp !== null && timestamp < lastTimestamp - DISCONTINUITY_MS;
    if (
      (tagType === SCRIPT || regressed || newVideo || newAudio) &&
      offset !== current.start
    ) {
      current.end = offset;
      if (current.media) {
        pieces.push(current);
      }
      current = new Piece(offset);
    }

    current.vhash = vhash;
    current.ahash = ahash;
    current.vcfg = vcfg;
    current.acfg = acfg;
    current.res = res;

    if (isVcfg || isAcfg) {
      continue;
    }

    if (tagType === VIDEO && (header & 0x0f) === 7) {
      current.video += 1;
      if (current.ts_min === null || timestamp < current.ts_min) {
        current.ts_min = timestamp;
      }
      if (current.ts_max === null || timestamp > current.ts_max) {
        current.ts_max = timestamp;
      }
      if ((header >> 4) === 1) {
        current.keyframes.push(timestamp);
      }
      lastTimestamp = timestamp;
    } else if (tagType === AUDIO && (header >> 4) === 10) {
      current.audio += 1;
      if (current.ts_min === null || timestamp < current.ts_min) {
        current.ts_min = timestamp;
      }
      if (current.ts_max === null || timestamp > current.ts_max) {
        current.ts_max = timestamp;
      }
      lastTimestamp = timestamp;
    }
  }

  finishProgress("scanning");
  current.end = size;
  if (current.media) {
    pieces.push(current);
  }
  return { pieces, configs, audioPayload };
}

function buildSample(buffer, piece, videoCfg, audioCfg) {
  let keyframe = null;
  for (const tag of iterTags(buffer, piece.start, piece.end)) {
    if (
      tag.tagType === VIDEO &&
      (buffer[tag.offset + 11] & 0x0f) === 7 &&
      buffer[tag.offset + 12] !== 0 &&
      (buffer[tag.offset + 11] >> 4) === 1
    ) {
      keyframe = { timestamp: tag.timestamp, offset: tag.offset };
      break;
    }
  }
  if (keyframe === null) {
    return null;
  }

  const parts = [FLV_HEADER, PREV_TAG, encodeTag(VIDEO, 0, videoCfg)];
  if (audioCfg) {
    parts.push(encodeTag(AUDIO, 0, audioCfg));
  }

  const limitBytes = keyframe.offset + SAMPLE_MAX_BYTES;
  for (const tag of iterTags(buffer, keyframe.offset, piece.end)) {
    if (tag.offset > limitBytes || tag.timestamp - keyframe.timestamp > SAMPLE_SECONDS * 1000) {
      break;
    }
    parts.push(
      encodeTag(
        tag.tagType,
        tag.timestamp - keyframe.timestamp,
        buffer.subarray(tag.offset + 11, tag.offset + 11 + tag.tagSize),
      ),
    );
  }
  return Buffer.concat(parts);
}

let tempCounter = 0;

function trialDecode(sample, scratch) {
  if (sample === null) {
    return [null, 0];
  }
  tempCounter += 1;
  const tempPath = path.join(scratch, `sample-${process.pid}-${tempCounter}.flv`);
  fs.writeFileSync(tempPath, sample);
  try {
    const result = spawnSync(
      "ffprobe",
      [
        "-v", "error", "-count_frames",
        "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames",
        "-of", "csv=p=0", tempPath,
      ],
      { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
    );
    if (result.error) {
      return [null, 0];
    }
    const stderr = result.stderr || "";
    const errors = stderr.split(/\r?\n/).filter((line) => line.trim() !== "").length;
    const text = (result.stdout || "").trim();
    const frames = /^\d+$/.test(text) ? Number.parseInt(text, 10) : 0;
    return [errors, frames];
  } finally {
    try {
      fs.unlinkSync(tempPath);
    } catch (error) {
      if (error.code !== "ENOENT") {
        throw error;
      }
    }
  }
}

function rankLess(a, b) {
  const errorsA = a.errors === null ? 1 << 30 : a.errors;
  const errorsB = b.errors === null ? 1 << 30 : b.errors;
  if (errorsA !== errorsB) {
    return errorsA < errorsB;
  }
  return a.frames > b.frames;
}

function assignConfigs(buffer, pieces, configs, audioCfg, log) {
  if (configs.size === 0) {
    return;
  }
  const keys = [...configs.keys()];
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "fixflv_trial_"));
  try {
    const targets = pieces.filter((piece) => piece.video > 0);
    let lastPercent = -1;
    for (let index = 0; index < targets.length; index += 1) {
      const piece = targets[index];
      lastPercent = updateProgress("probing", index, targets.length, lastPercent);
      const hint = configs.has(piece.vhash) ? piece.vhash : keys[0];
      const order = [hint, ...keys.filter((key) => key !== hint)];
      const results = [];
      for (const key of order) {
        const sample = buildSample(buffer, piece, configs.get(key).payload, audioCfg);
        const [errors, frames] = trialDecode(sample, scratch);
        results.push({ key, errors, frames });
        if (errors === 0 && frames > 0) {
          break;
        }
      }
      const good = results.filter((item) => item.errors === 0 && item.frames > 0);
      let best;
      if (good.length > 0) {
        best = good.reduce((a, b) => (b.frames > a.frames ? b : a));
      } else {
        const valid = results.filter((item) => item.frames > 0);
        const pool = valid.length > 0 ? valid : results;
        best = pool.reduce((a, b) => (rankLess(b, a) ? b : a));
        if (best.key !== hint) {
          for (const item of pool) {
            if (item.key === hint) {
              best = item;
              break;
            }
          }
        }
      }
      piece.vhash = best.key;
      piece.vcfg = configs.get(best.key).payload;
      piece.res = configs.get(best.key).res;
      if (best.key !== hint) {
        log(
          `  piece @${piece.start} ts ${piece.ts_min}..${piece.ts_max}: config -> ${best.key} (${piece.res})`,
        );
      }
    }
    if (targets.length > 0) {
      finishProgress("probing");
    }
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

function plan(pieces) {
  const ordered = pieces
    .filter((piece) => piece.media > 0 && piece.ts_min !== null)
    .sort((a, b) => (a.ts_min - b.ts_min) || (a.ts_max - b.ts_max) || (a.start - b.start));
  const runs = [];
  let current = [];
  for (const piece of ordered) {
    if (current.length > 0 && piece.codec !== current[0].codec) {
      runs.push(current);
      current = [];
    }
    current.push(piece);
  }
  if (current.length > 0) {
    runs.push(current);
  }
  return [ordered, runs];
}

function runSpan(pieces) {
  return [pieces[0].ts_min, Math.max(...pieces.map((piece) => piece.ts_max))];
}

function writeRun(buffer, run, index, stem, outputDir, overwrite, log) {
  const config = run[0];
  const padded = String(index).padStart(2, "0");
  const name = `${stem}.part${padded}.${config.res}.${config.vhash}.flv`;
  const output = path.join(outputDir, name);
  if (fs.existsSync(output) && !overwrite) {
    log(`  skip existing ${name}`);
    return [output, true];
  }

  const [firstTs, lastTs] = runSpan(run);
  const fd = fs.openSync(output, "w");
  let written = 0;
  let outEnd = 0;
  const skipped = [];
  try {
    fs.writeSync(fd, FLV_HEADER);
    fs.writeSync(fd, PREV_TAG);
    if (config.vcfg) {
      fs.writeSync(fd, encodeTag(VIDEO, 0, config.vcfg));
    }
    if (config.acfg) {
      fs.writeSync(fd, encodeTag(AUDIO, 0, config.acfg));
    }

    const label = `writing part${padded}`;
    const totalBytes = run.reduce((sum, piece) => sum + (piece.end - piece.start), 0);
    let doneBytes = 0;
    let lastPercent = -1;
    let prevMax = null;
    let cursor = 0;

    for (const piece of run) {
      if (prevMax !== null && piece.ts_max <= prevMax) {
        skipped.push(`dup@${piece.start}`);
        continue;
      }
      const effective = prevMax === null ? piece.ts_min : Math.max(piece.ts_min, prevMax);
      let keyframe = null;
      for (const candidate of piece.keyframes) {
        if (candidate >= effective) {
          keyframe = candidate;
          break;
        }
      }
      if (keyframe === null) {
        skipped.push(`nokf@${piece.start}`);
        continue;
      }

      let started = false;
      for (const tag of iterTags(buffer, piece.start, piece.end)) {
        lastPercent = updateProgress(
          label,
          doneBytes + (tag.offset - piece.start),
          totalBytes,
          lastPercent,
        );
        if (tag.tagType === SCRIPT || tag.tagSize < 2) {
          continue;
        }
        const header = buffer[tag.offset + 11];
        const packetType = buffer[tag.offset + 12];
        let outTimestamp;
        if (tag.tagType === VIDEO) {
          if ((header & 0x0f) !== 7 || packetType === 0) {
            continue;
          }
          if (!started) {
            if ((header >> 4) !== 1 || tag.timestamp < keyframe) {
              continue;
            }
            started = true;
          }
          if (tag.timestamp < effective) {
            continue;
          }
          outTimestamp = cursor + (tag.timestamp - keyframe);
        } else if (tag.tagType === AUDIO) {
          if ((header >> 4) !== 10 || packetType === 0) {
            continue;
          }
          if (!started || tag.timestamp < keyframe) {
            continue;
          }
          outTimestamp = cursor + (tag.timestamp - keyframe);
        } else {
          continue;
        }
        fs.writeSync(
          fd,
          encodeTag(
            tag.tagType,
            outTimestamp,
            buffer.subarray(tag.offset + 11, tag.offset + 11 + tag.tagSize),
          ),
        );
        outEnd = Math.max(outEnd, outTimestamp);
        written += 1;
      }

      doneBytes += piece.end - piece.start;
      cursor = outEnd + 1;
      prevMax = prevMax === null ? piece.ts_max : Math.max(prevMax, piece.ts_max);
    }
    finishProgress(label);
  } finally {
    fs.closeSync(fd);
  }

  if (written === 0) {
    try {
      fs.unlinkSync(output);
    } catch (error) {
      if (error.code !== "ENOENT") {
        throw error;
      }
    }
    log("  dropped empty run (no keyframe)");
    return [null, false];
  }
  log(
    `  ${name}  ts ${firstTs}..${lastTs}  -> 0..${outEnd} (${(outEnd / 1000).toFixed(1)}s, ${written} packets)` +
      (skipped.length > 0 ? `  skipped: ${skipped.join(",")}` : ""),
  );
  return [output, true];
}

function remux(flvPath, mp4Path) {
  const result = spawnSync(
    "ffmpeg",
    [
      "-hide_banner", "-v", "warning", "-stats",
      "-i", flvPath,
      "-map", "0:v:0", "-map", "0:a:0",
      "-c", "copy", "-movflags", "+faststart",
      "-y", mp4Path,
    ],
    { stdio: "inherit" },
  );
  return !result.error && result.status === 0;
}

function expandHome(value) {
  return value.replace(/^~(?=$|\/)/, os.homedir());
}

function usage() {
  process.stderr.write(
    [
      "usage: fix_flv_split.js source [-o OUTDIR] [--dry-run] [--mp4] [--overwrite]",
      "",
      "Split a mixed-configuration live FLV into playable parts.",
    ].join("\n") + "\n",
  );
}

function main() {
  const args = process.argv.slice(2);
  let outputDirArg = null;
  let dryRun = false;
  let mp4 = false;
  let overwrite = false;
  const positional = [];
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === "-o" || argument === "--output-dir") {
      index += 1;
      if (index >= args.length) {
        usage();
        process.stderr.write("error: expected a value for --output-dir\n");
        return 2;
      }
      outputDirArg = args[index];
    } else if (argument === "--dry-run") {
      dryRun = true;
    } else if (argument === "--mp4") {
      mp4 = true;
    } else if (argument === "--overwrite") {
      overwrite = true;
    } else if (argument === "--help" || argument === "-h") {
      usage();
      return 0;
    } else if (argument.startsWith("-") && argument !== "-") {
      usage();
      process.stderr.write(`error: unrecognized argument: ${argument}\n`);
      return 2;
    } else {
      positional.push(argument);
    }
  }

  if (positional.length !== 1) {
    usage();
    process.stderr.write("error: expected exactly one source file\n");
    return 2;
  }

  const source = path.resolve(expandHome(positional[0]));
  if (!fs.existsSync(source) || !fs.statSync(source).isFile()) {
    process.stderr.write(`error: not a file: ${source}\n`);
    return 2;
  }
  const outputDir = outputDirArg
    ? path.resolve(expandHome(outputDirArg))
    : path.dirname(source);
  const stem = path.basename(source, path.extname(source));
  const log = (message) => console.log(message);

  const buffer = fs.readFileSync(source);
  const { pieces, configs, audioPayload } = scan(buffer);
  if (pieces.length === 0) {
    process.stderr.write("error: no media packets found\n");
    return 2;
  }

  log(`${path.basename(source)}: ${pieces.length} piece(s), ${configs.size} distinct video config(s)`);
  assignConfigs(buffer, pieces, configs, audioPayload, log);

  const [, runs] = plan(pieces);
  log(`plan: ${runs.length} part(s)`);
  runs.forEach((run, index) => {
    const [firstTs, lastTs] = runSpan(run);
    log(
      `  part${String(index + 1).padStart(2, "0")}: ${run[0].res} cfg=${run[0].vhash}  ` +
        `ts ${firstTs}..${lastTs} (${((lastTs - firstTs) / 1000).toFixed(1)}s)  [${run.length} piece(s)]`,
    );
  });

  if (dryRun) {
    return 0;
  }

  fs.mkdirSync(outputDir, { recursive: true });
  for (let index = 0; index < runs.length; index += 1) {
    const [output] = writeRun(buffer, runs[index], index + 1, stem, outputDir, overwrite, log);
    if (output !== null && mp4) {
      const parsed = path.parse(output);
      const mp4Path = path.join(parsed.dir, `${parsed.name}.mp4`);
      log(`  remuxing ${path.basename(mp4Path)} ...`);
      if (!remux(output, mp4Path)) {
        process.stderr.write(`  error: ffmpeg remux failed for ${path.basename(output)}\n`);
        return 1;
      }
    }
  }
  return 0;
}

module.exports = { scan, plan, writeRun, iterTags, encodeTag, avcResolution };

if (require.main === module) {
  try {
    process.exitCode = main();
  } catch (error) {
    console.error(`error: ${error.message}`);
    process.exitCode = 2;
  }
}
