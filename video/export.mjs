// Frame-stepped export: seeks the GSAP timeline to each frame, screenshots, pipes to ffmpeg.
//   node video/export.mjs shots 5,15,30 outdir     -> PNGs for layout checks
//   node video/export.mjs video out.mp4 [fps]      -> full video
import { chromium } from 'file:///C:/Users/tommy/Somerset%20Systems/liquid-gardens/node_modules/playwright/index.mjs';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

const here = path.dirname(fileURLToPath(import.meta.url));
const url = pathToFileURL(path.join(here, 'lab-demo.html')).href + '?rec';
const [,, mode, arg, arg2] = process.argv;
const ffmpeg = process.env.FFMPEG || 'ffmpeg';

const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1280, height: 720 } });
await p.goto(url);
await p.waitForFunction(() => window.__ready, null, { timeout: 30000 });
await p.waitForTimeout(400);

if (mode === 'shots') {
  fs.mkdirSync(arg2, { recursive: true });
  for (const t of arg.split(',')) {
    await p.evaluate((t) => window.__demo.seek(t), +t);
    await p.screenshot({ path: path.join(arg2, `t${t}.png`) });
  }
} else {
  const fps = +(process.argv[4] || 30);
  const dur = await p.evaluate(() => window.__demo.duration);
  const n = Math.round(dur * fps);
  const ff = spawn(ffmpeg, ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-i', '-',
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-movflags', '+faststart', arg], { stdio: ['pipe', 'inherit', 'inherit'] });
  for (let i = 0; i < n; i++) {
    await p.evaluate((t) => window.__demo.seek(t), i / fps);
    const buf = await p.screenshot({ type: 'jpeg', quality: 92 });
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r));
    if (i % 300 === 0) console.log(`frame ${i}/${n}`);
  }
  ff.stdin.end();
  await new Promise((r) => ff.on('close', r));
}
await b.close();
