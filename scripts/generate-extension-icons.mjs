import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../extensions/chrome-bridge");

function crc32(data) {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, body) {
  const label = Buffer.from(type);
  const length = Buffer.alloc(4);
  length.writeUInt32BE(body.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([label, body])));
  return Buffer.concat([length, label, body, checksum]);
}

function segmentDistance(px, py, ax, ay, bx, by) {
  const vx = bx - ax, vy = by - ay;
  const t = Math.max(0, Math.min(1, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)));
  return Math.hypot(px - (ax + vx * t), py - (ay + vy * t));
}

function pixel(x, y) {
  const nx = x * 2 - 1, ny = y * 2 - 1;
  const radius = Math.hypot(nx, ny);
  const corner = Math.max(Math.abs(nx) - .67, 0) ** 2 + Math.max(Math.abs(ny) - .67, 0) ** 2;
  if (Math.sqrt(corner) > .31) return [0, 0, 0, 0];
  const shade = Math.max(0, 1 - Math.hypot(nx + .22, ny + .27) / 1.7);
  let red = 6 + shade * 18, green = 17 + shade * 33, blue = 30 + shade * 47;
  const ring = Math.abs(radius - .69);
  const ringGlow = Math.exp(-ring * ring / .008) * .3;
  const ringCore = Math.exp(-ring * ring / .0008) * .85;
  const mix = Math.min(1, ringGlow + ringCore);
  red = red * (1 - mix) + 62 * mix;
  green = green * (1 - mix) + 215 * mix;
  blue = blue * (1 - mix) + 188 * mix;
  const points = [[-.55, .02],[-.28,.02],[-.14,-.28],[.08,.34],[.22,.01],[.55,.01]];
  let distance = 2;
  for (let i = 0; i < points.length - 1; i++) {
    distance = Math.min(distance, segmentDistance(nx, ny, ...points[i], ...points[i + 1]));
  }
  const lineGlow = Math.exp(-distance * distance / .009) * .35;
  const lineCore = Math.exp(-distance * distance / .0014) * .96;
  const line = Math.min(1, lineGlow + lineCore);
  red = red * (1 - line) + 229 * line;
  green = green * (1 - line) + 255 * line;
  blue = blue * (1 - line) + 252 * line;
  return [red, green, blue, 255].map(Math.round);
}

function icon(size) {
  const scale = 4;
  const rows = [];
  for (let y = 0; y < size; y++) {
    const row = Buffer.alloc(1 + size * 4);
    for (let x = 0; x < size; x++) {
      const sum = [0, 0, 0, 0];
      for (let sy = 0; sy < scale; sy++) for (let sx = 0; sx < scale; sx++) {
        const rgba = pixel((x + (sx + .5) / scale) / size, (y + (sy + .5) / scale) / size);
        for (let channel = 0; channel < 4; channel++) sum[channel] += rgba[channel];
      }
      for (let channel = 0; channel < 4; channel++) row[1 + x * 4 + channel] = Math.round(sum[channel] / scale ** 2);
    }
    rows.push(row);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header[8] = 8;
  header[9] = 6;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", header), chunk("IDAT", zlib.deflateSync(Buffer.concat(rows))), chunk("IEND", Buffer.alloc(0)),
  ]);
}

for (const size of [16, 32, 48, 128]) fs.writeFileSync(path.join(root, `icon-${size}.png`), icon(size));
