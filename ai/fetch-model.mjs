#!/usr/bin/env node
/**
 * 本地语义搜索 · 模型语言档位 / hash 计算（仅部署准备阶段联网；运行时零外联）
 * ----------------------------------------------------------------------------
 * 用法：
 *   node shared/ai/fetch-model.mjs --list              # 查看可选语言档位（不联网）
 *   node shared/ai/fetch-model.mjs                     # 默认 zh-en 中英双语轻量档（~24MB）
 *   node shared/ai/fetch-model.mjs --lang multilingual# 全语言档（~118MB）
 *   node shared/ai/fetch-model.mjs --lang en           # 纯英文档（~23MB）
 *   node shared/ai/fetch-model.mjs --base https://hf-mirror.com   # 指定下载源
 *   node shared/ai/fetch-model.mjs --compute-hash     # 仅重算 manifest.hash（不联网、不改档位）
 *
 * 高级覆盖（自定义模型仓库）：
 *   --model Xenova/xxx --dim 768 --pooling mean --name my-profile
 *
 * 下载源自动探测：先 huggingface.co（8s 内不通）→ 自动回退 hf-mirror.com。
 * 只是「部署准备阶段」把文件抓回本地，运行时全部从同域 ./ai/ 加载。
 *
 * 下载内容（全部同域，运行时不外联）：
 *   model-v1/config.json
 *   model-v1/tokenizer.json
 *   model-v1/tokenizer_config.json
 *   model-v1/onnx/model_quantized.onnx   （q8 量化）
 *
 * 换档后 manifest.version / dim / pooling 会变，客户端检测到指纹变化会自动清空并重建向量库，
 * 无需手动干预；两个平台（marknote-ai / workbench-ai）各自独立重建。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import crypto from 'node:crypto';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TARGET = path.resolve(__dirname, 'model-v1');
const MANIFEST = path.resolve(__dirname, 'model-manifest.json');

/**
 * 语言档位——体积差异主要来自词表：
 *   multilingual-e5-small 的 XLM-R 词表 25 万 → q8 权重 118MB；
 *   bge-small-zh-v1.5 的 bert-base-chinese 词表 2.1 万 → q8 权重 24MB。
 */
const PROFILES = {
  'zh-en': {
    repo: 'Xenova/bge-small-zh-v1.5',
    version: 'v1-zh-en',
    dim: 512,
    lang: 'zh,en',
    pooling: 'cls',
    size: '~24MB',
    desc: '中英双语轻量档（默认）：中文语义检索准确；英文术语由词表子词 + 命令面板关键词匹配兜底'
  },
  multilingual: {
    repo: 'Xenova/multilingual-e5-small',
    version: 'v1-multi',
    dim: 384,
    lang: 'multilingual(100+)',
    pooling: 'mean',
    size: '~118MB',
    desc: '全语言档：100+ 语种跨语言语义，含真正的英文语义检索，但体积约为默认档 5 倍'
  },
  en: {
    repo: 'Xenova/all-MiniLM-L6-v2',
    version: 'v1-en',
    dim: 384,
    lang: 'en',
    pooling: 'mean',
    size: '~23MB',
    desc: '纯英文档：英文语义最好，但中文会退化为未知字符，不适合中文数据'
  }
};

const args = process.argv.slice(2);
const argVal = (k) => {
  const a = args.find((x) => x.startsWith(`--${k}=`));
  return a ? a.slice(k.length + 3) : null;
};
const computeOnly = args.includes('--compute-hash');
const listOnly = args.includes('--list');
const langKey = argVal('lang') || argVal('profile') || 'zh-en';
const baseArg = argVal('base');
const BASES = baseArg
  ? [baseArg.replace(/\/$/, '')]
  : ['https://huggingface.co', 'https://hf-mirror.com'];
const FILES = [
  'config.json',
  'tokenizer.json',
  'tokenizer_config.json',
  'onnx/model_quantized.onnx'
];

function resolveProfile() {
  const base = PROFILES[langKey];
  if (!base && !argVal('model')) {
    console.error(`✗ 未知语言档位「${langKey}」，可用：${Object.keys(PROFILES).join(' / ')}`);
    process.exit(1);
  }
  const p = Object.assign({}, base || {
    version: 'v1-custom', dim: 384, lang: 'custom', pooling: 'mean', size: '?', desc: '自定义模型'
  });
  if (argVal('model')) p.repo = argVal('model');
  if (argVal('dim')) p.dim = Number(argVal('dim'));
  if (argVal('pooling')) p.pooling = argVal('pooling');
  if (argVal('lang') && !PROFILES[langKey]) p.lang = argVal('lang');
  if (argVal('name')) p.version = argVal('name');
  p.key = langKey;
  return p;
}

function printProfiles() {
  console.log('可选语言档位（--lang <key>）：\n');
  for (const [k, p] of Object.entries(PROFILES)) {
    console.log(`  ${k.padEnd(14)} ${p.size.padEnd(9)} dim=${String(p.dim).padEnd(4)} pooling=${p.pooling.padEnd(5)} ${k === 'zh-en' ? '[默认]' : ''}`);
    console.log(`  ${''.padEnd(14)} ${p.desc}`);
    console.log(`  ${''.padEnd(14)} ${p.repo}\n`);
  }
}

// 轻量探测哪个源可达（大文件下载前先定源，避免每个文件都重复等待超时）
async function pickBase(repo) {
  let lastErr = null;
  for (const base of BASES) {
    try {
      const r = await fetch(`${base}/${repo}/resolve/main/config.json`, {
        signal: AbortSignal.timeout(8000)
      });
      if (r.ok) { console.log('✓ 下载源：' + base); return base; }
      lastErr = new Error(`${r.status} @ ${base}`);
    } catch (e) { lastErr = e; }
  }
  throw new Error('所有模型源均不可达：' + (lastErr && lastErr.message));
}

// 流式下载 + 进度
async function fetchBuf(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(300000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  if (!res.body) return Buffer.from(await res.arrayBuffer());
  const reader = res.body.getReader();
  const chunks = [];
  let got = 0, nextMark = 20 * 1048576;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(Buffer.from(value));
    got += value.length;
    if (got >= nextMark) { process.stdout.write(`  ${(got / 1048576).toFixed(0)}MB`); nextMark += 20 * 1048576; }
  }
  return Buffer.concat(chunks);
}

async function download(p) {
  const base = await pickBase(p.repo);
  fs.mkdirSync(path.join(TARGET, 'onnx'), { recursive: true });
  for (const f of FILES) {
    const dest = path.join(TARGET, f);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    process.stdout.write(`↓ ${f} ... `);
    try {
      const buf = await fetchBuf(`${base}/${p.repo}/resolve/main/${f}`);
      if (!buf.length) throw new Error('空文件');
      fs.writeFileSync(dest, buf);
      console.log(` ${(buf.length / 1048576).toFixed(1)} MB`);
    } catch (e) {
      console.error(`\n✗ ${f} 下载失败：${e.message}`);
      process.exit(1);
    }
  }
  console.log('✓ 模型已下载到', TARGET);
}

// 把档位写入 manifest（hash 由 computeHash 单独补，便于 --compute-hash 不改档位）
function writeProfile(p) {
  const m = fs.existsSync(MANIFEST) ? JSON.parse(fs.readFileSync(MANIFEST, 'utf8')) : {};
  m.version = p.version;
  m.profile = p.key;
  m.model = p.repo.split('/').pop();
  m.repo = p.repo;
  m.lang = p.lang;
  m.capabilities = {
    embedding: { dim: p.dim, lang: p.lang, pooling: p.pooling },
    summarize: { mode: 'extractive' },
    related: { threshold: 0.6 }
  };
  m.note = 'hash 为本地模型文件指纹（配置三件套全量 + onnx 首尾各 256KB 的 SHA-256）。换档位后 version/dim/pooling 变化，客户端自动清空并重建向量库；不一致时判定不可用，绝不降级到外部源。';
  fs.writeFileSync(MANIFEST, JSON.stringify(m, null, 2) + '\n');
  console.log(`✓ 档位 ${p.key} → manifest（dim=${p.dim}, pooling=${p.pooling}, lang=${p.lang}）`);
}

function concat(a, b) { return Buffer.concat([a, b]); }

async function computeHash() {
  if (!fs.existsSync(path.join(TARGET, 'onnx/model_quantized.onnx'))) {
    console.error('✗ model-v1/onnx/model_quantized.onnx 不存在，无法计算 hash');
    process.exit(1);
  }
  let acc = Buffer.alloc(0);
  for (const f of ['config.json', 'tokenizer.json', 'tokenizer_config.json']) {
    const p = path.join(TARGET, f);
    if (!fs.existsSync(p)) { console.error('✗ 缺少', f); process.exit(1); }
    acc = concat(acc, fs.readFileSync(p));
  }
  const onnx = fs.readFileSync(path.join(TARGET, 'onnx/model_quantized.onnx'));
  const head = onnx.subarray(0, 262144);
  const tail = onnx.subarray(Math.max(0, onnx.length - 262144));
  acc = concat(concat(acc, head), tail);
  const hash = crypto.createHash('sha256').update(acc).digest('hex');
  const m = JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
  m.hash = hash;
  fs.writeFileSync(MANIFEST, JSON.stringify(m, null, 2) + '\n');
  console.log('✓ 已写入 manifest.hash =', hash);
}

(async () => {
  if (listOnly) { printProfiles(); return; }
  if (computeOnly) { await computeHash(); return; }
  const p = resolveProfile();
  console.log(`▸ 语言档位：${p.key}（${p.lang}，${p.size}）· ${p.repo}`);
  await download(p);
  writeProfile(p);
  await computeHash();
})().catch((e) => { console.error(e); process.exit(1); });
