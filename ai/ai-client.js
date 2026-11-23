/*!
 * MarkNote 平台级「纯本地语义搜索」通用客户端（框架无关，零外联）
 * ----------------------------------------------------------------------------
 * 设计红线（最高优先级）：
 *   · 本文件及其加载的 transformers.min.js / onnxruntime wasm / model-v1/ 全部来自
 *     同域 ./ai/ 目录，运行时绝不访问任何外部 URL、CDN、遥测、云端 API。
 *   · 【不可变资产】运行时永不「检查更新」、永不「自动更新」：不比对远端版本、
 *     不请求任何版本/清单/更新端点、不下载或替换 ./ai/ 下任何文件。
 *     AI 资产唯一变更途径 = 构建期主动同步，或人工替换整个 ./ai/ 目录。
 *   · 模型推理在 Web Worker 内执行；跨 tab 用 navigator.locks 互斥加载。
 *   · 向量库按 dbNamespace 隔离（库名 {dbNamespace}-ai），各应用互不可见。
 *
 * 暴露：window.MN_AI = { createAIClient, util }
 *   util 为纯函数集合（cosine / splitSentences / rrf / mmrSelect），可在 Node 下单测。
 *
 * 加载方式（调用方负责）：本文件作为经典 <script src="./ai/ai-client.js"> 注入，
 * 不进任何打包器，以免被内联进主产物（保持主产物体积小、且多应用共用同一份）。
 */
(function (global) {
  'use strict';

  /* ==================== 不可变资产策略（冻结，禁止改成 true） ==================== */
  // workbench / marknote 共用本客户端；这份策略由 e2e 与 check:ai 直接读取断言。
  // 客户端里不存在、也永远不允许存在「检查更新 / 自动更新」的实现路径：
  // 没有版本端点、没有后台轮询、没有远端清单比对、没有静默下载。
  // 要换模型/换档位/换客户端，只有两条主动路径：构建期同步，或人工替换整个 ./ai/ 目录。
  const AI_POLICY = Object.freeze({
    autoUpdate: false,                     // 不自动更新模型 / wasm / 客户端
    updateCheck: false,                    // 不检查更新、不比对任何远端版本
    publicCdn: false,                      // 不连接公网 CDN / HF / 遥测端点
    sameOriginOnly: true,                  // 只加载同域 ./ai/ 文件
    mutableOnlyByDirectoryReplace: true    // 唯一变更途径：人工替换目录（或主动构建）
  });
  // 任何试图通过参数打开「自动更新 / 更新检查 / 远端清单」的调用一律拒绝。
  // 这不是一个可打开的开关，而是永久禁止——防止以后有人静默加回外联逻辑。
  const FORBIDDEN_OPTS = [
    'autoUpdate', 'checkUpdate', 'updateCheck', 'updateUrl',
    'manifestUrl', 'versionUrl', 'remoteBase', 'cdnBase'
  ];

  /* ==================== 推理后端 / 分块常量（与 embedBatch.js 同源） ==================== */
  // 与 shared/ai/embedBatch.js 的数值必须一致（check:ai 静态比对）；classic script 不进打包器，
  // 无法 import ESM，故在此保留镜像。只影响单次前向批量，不进模型指纹（后端切换不清向量库）。
  const BACKEND_KEY = 'ai-infer-backend';
  const EMBED_BATCH = 24;            // 单次前向批量上限，防 WASM 堆耗尽
  const EMBED_BATCH_WEBGPU = 48;     // WebGPU（fp16）内存压力小，可放宽
  const WEBGPU_TIMEOUT_MS = 15000;   // WebGPU 初始化超时，超时即回退 WASM/q8
  function normalizeBackendMode(mode) { return mode === 'wasm' ? 'wasm' : 'auto'; }
  /** 自检失败 reason → 可读中文提示。三台共用同一文案口径，避免界面只显示空白「资产不可用」。 */
  function selfCheckReasonText(reason) {
    const r = String(reason || 'unknown');
    if (r === 'file-protocol') return '以 file:// 方式打开时浏览器禁止读取本地模型文件；请用本地 HTTP 服务启动（npm run server:dist:start）后访问，不要双击 index.html';
    if (r === 'no-manifest') return '缺少 ./ai/model-manifest.json（本地 AI 资产不完整）';
    if (r.indexOf('missing:') === 0) return '缺少模型文件：' + r.slice(8);
    if (r === 'hash-mismatch') return '模型文件指纹与 manifest 不一致：请确认 ./ai/ 整套资产来自同一构建（模型文件与 manifest 需配套），或重新复制完整 dist 目录';
    if (r.indexOf('fetch-failed:') === 0) return '读取本地 AI 资产失败：' + r.slice(13);
    return '本地 AI 资产自检失败：' + r;
  }
  function readBackendMode() {
    try { return normalizeBackendMode(global.localStorage && global.localStorage.getItem(BACKEND_KEY)); }
    catch (e) { return 'auto'; }
  }
  function writeBackendMode(mode) {
    const v = normalizeBackendMode(mode);
    try { if (global.localStorage) global.localStorage.setItem(BACKEND_KEY, v); } catch (e) {}
    return v;
  }

  /* ============================ 纯函数工具（可单测） ============================ */
  const util = {};

  // 余弦相似度（已归一化向量相乘即可，但通用实现仍走点积/模长）
  util.cosine = function (a, b) {
    if (!a || !b || a.length !== b.length) return 0;
    let dot = 0, na = 0, nb = 0;
    for (let i = 0; i < a.length; i++) {
      const x = a[i], y = b[i];
      dot += x * y; na += x * x; nb += y * y;
    }
    if (na === 0 || nb === 0) return 0;
    return dot / (Math.sqrt(na) * Math.sqrt(nb));
  };

  // 中英文混合断句：以 。！？!?；; 及换行切分（保留分隔符语义），去除空串
  util.splitSentences = function (text) {
    const s = String(text || '');
    const parts = s.split(/(?<=[。！？!?；;\n])/);
    const out = [];
    for (let p of parts) {
      p = p.trim();
      if (p) out.push(p);
    }
    if (!out.length && s.trim()) out.push(s.trim());
    return out;
  };

  // RRF（Reciprocal Rank Fusion）融合多路排序，去除重复并标记来源。
  //   lists: [{ kind: 'keyword'|'semantic', items: [{ id, item, score? }] }]
  //   返回 [{ id, item, score, kw, semantic, semScore }]（按融合分降序）
  util.rrf = function (lists, k) {
    k = k || 60;
    const fused = new Map();
    for (const list of lists) {
      if (!list || !list.items) continue;
      list.items.forEach((it, rank) => {
        const contrib = 1 / (k + rank + 1);
        if (!fused.has(it.id)) {
          fused.set(it.id, { id: it.id, item: it.item, score: 0, kw: false, semantic: false, semScore: 0 });
        }
        const e = fused.get(it.id);
        e.score += contrib;
        if (list.kind === 'semantic') { e.semantic = true; e.semScore = it.score || 0; }
        else { e.kw = true; }
      });
    }
    const arr = Array.from(fused.values());
    arr.sort((x, y) => y.score - x.score);
    return arr;
  };

  // MMR 抽取式摘要：给定每句与「中心」的相似度 scores 与句间相似度矩阵 simMatrix，
  // 贪心选取 maxSentences 句，最大化 querySim - lambda * max(已选相似度)。
  //   返回选中句的原始下标（按选取顺序；调用方再按原序拼接）。
  util.mmrSelect = function (scores, simMatrix, maxSentences, lambda) {
    const n = scores.length;
    if (n === 0) return [];
    lambda = (lambda != null) ? lambda : 0.7;
    maxSentences = Math.max(1, Math.min(maxSentences || 3, n));
    const selected = [];
    const inSet = new Set();
    let first = 0;
    for (let i = 1; i < n; i++) if (scores[i] > scores[first]) first = i;
    selected.push(first); inSet.add(first);
    while (selected.length < maxSentences) {
      let best = -1, bestVal = -Infinity;
      for (let i = 0; i < n; i++) {
        if (inSet.has(i)) continue;
        let maxSim = 0;
        if (simMatrix) for (const j of inSet) maxSim = Math.max(maxSim, simMatrix[i][j] || 0);
        const val = scores[i] - lambda * maxSim;
        if (val > bestVal) { bestVal = val; best = i; }
      }
      if (best < 0) break;
      selected.push(best); inSet.add(best);
    }
    return selected;
  };

  // 分块前向：无论调用方一次传多少条，都按 batchSize 切片逐块走 worker，
  // 块间让出事件循环（await setTimeout(0)），结果按原顺序拼接。签名/返回值对调用方透明。
  //   runChunk(chunk) => Promise<vector[]>，见 embed()（等价于一次 worker embed 调用）。
  util.embedChunked = async function (texts, batchSize, runChunk) {
    const arr = Array.isArray(texts) ? texts : [texts];
    const size = Math.max(1, Math.floor(batchSize) || EMBED_BATCH);
    const out = new Array(arr.length);
    for (let i = 0; i < arr.length; i += size) {
      const chunk = arr.slice(i, i + size);
      const vecs = await runChunk(chunk);
      if (!vecs || vecs.length !== chunk.length) {
        throw new Error('[local-ai] embed 分块返回长度不匹配：期望 ' + chunk.length + '，实际 ' + (vecs ? vecs.length : 0));
      }
      for (let j = 0; j < chunk.length; j++) out[i + j] = vecs[j];
      if (i + size < arr.length) await new Promise((resolve) => setTimeout(resolve, 0));
    }
    return out;
  };

  // 后端初始化决策（纯函数，worker 内以 toString 注入；单测直接调用）。
  // 关键：auto 模式且 fp16 资产缺失时直接判 WASM 回退，worker 因此根本不进 WebGPU 探测分支，
  // 避免部署环境缺 model_fp16.onnx 时每次启动都白跑 15 秒超时。
  function backendFallbackPlan(mode, fp16Missing, hasGpu) {
    if (mode === 'wasm') return { gpu: false, reason: '已选择仅 WASM', code: 'wasm-only' };
    if (fp16Missing) return { gpu: false, reason: 'fp16 资产缺失，回退 WASM', code: 'fp16-missing' };
    if (!hasGpu) return { gpu: false, reason: 'WebGPU 不可用：无 navigator.gpu', code: 'no-gpu' };
    return { gpu: true, reason: '', code: '' };
  }
  util.backendFallbackPlan = backendFallbackPlan;

  /**
   * 纯 JS SHA-256（FIPS 180-4）→ 小写 hex，与 Web Crypto subtle.digest 输出逐字节一致。
   * 用途：非安全上下文（局域网 http，window.crypto.subtle 为 undefined）下资产指纹校验兜底，
   * 避免把「浏览器不提供 subtle」误判为「模型文件指纹不一致」。也作为可单测纯函数挂在此处。
   */
  util.jsSha256Hex = function jsSha256Hex(data) {
    const K = [
      0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
      0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
      0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
      0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
      0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
      0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
      0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
      0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
    ];
    const h = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    const bitLen = data.length * 8;
    // padding 后总长 = 向上取整到 64 的倍数（len 数据 + 1 字节 0x80 + 8 字节长度）。
    // 用 floor+1 会在 len+9 恰为 64 倍数时多算一块（如 len=55），必须 ceil。
    const total = Math.ceil((data.length + 9) / 64) * 64;
    const buf = new Uint8Array(total);
    buf.set(data); buf[data.length] = 0x80;
    const hi = Math.floor(bitLen / 0x100000000), lo = bitLen >>> 0;
    buf[total - 8] = (hi >>> 24) & 255; buf[total - 7] = (hi >>> 16) & 255;
    buf[total - 6] = (hi >>> 8) & 255; buf[total - 5] = hi & 255;
    buf[total - 4] = (lo >>> 24) & 255; buf[total - 3] = (lo >>> 16) & 255;
    buf[total - 2] = (lo >>> 8) & 255; buf[total - 1] = lo & 255;
    const w = new Uint32Array(64);
    for (let off = 0; off < total; off += 64) {
      for (let i = 0; i < 16; i++) {
        const j = off + i * 4;
        w[i] = ((buf[j] << 24) | (buf[j + 1] << 16) | (buf[j + 2] << 8) | buf[j + 3]) >>> 0;
      }
      for (let i = 16; i < 64; i++) {
        const x = w[i - 15], y = w[i - 2];
        const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      let a = h[0], b = h[1], cc = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
      for (let i = 0; i < 64; i++) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const ch = (e & f) ^ (~e & g);
        const t1 = (hh + S1 + ch + K[i] + w[i]) >>> 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const maj = (a & b) ^ (a & cc) ^ (b & cc);
        const t2 = (S0 + maj) >>> 0;
        hh = g; g = f; f = e; e = (d + t1) >>> 0;
        d = cc; cc = b; b = a; a = (t1 + t2) >>> 0;
      }
      h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + cc) >>> 0; h[3] = (h[3] + d) >>> 0;
      h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0; h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0;
    }
    return h.map(x => x.toString(16).padStart(8, '0')).join('');
  };

  /* ============================ 跨 tab 互斥锁 ============================ */
  function withLock(name, fn) {
    if (global.navigator && global.navigator.locks && global.navigator.locks.request) {
      return global.navigator.locks.request(name, () => fn());
    }
    return fn();
  }

  /* ============================ IndexedDB 封装 ============================ */
  function openDB(dbName) {
    return new Promise((resolve, reject) => {
      const req = global.indexedDB.open(dbName, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('embeddings')) {
          const s = db.createObjectStore('embeddings', { keyPath: 'note_id' });
          s.createIndex('nb_id', 'nb_id', { unique: false });
        }
        if (!db.objectStoreNames.contains('meta')) {
          db.createObjectStore('meta', { keyPath: 'k' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  function txDone(tx) {
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  }

  /* ============================ Worker 源码（module worker） ============================ */
  // transformers.min.js 是 ESM（内部用 import.meta.url 创建 ORT 子 worker），必须用
  // module worker + 动态 import()；classic worker 的 importScripts 会报 'import.meta outside a module'。
  const WORKER_SRC = [
    'self.onmessage = async function (e) {',
    '  var msg = e.data;',
    '  if (msg.type === "init") {',
    '    try {',
    // 离线红线：worker 内 fetch 与子 Worker 的 URL 只允许同源（或 blob:/data:）。
    // transformers / 模型 / wasm 一律不得来自公网 CDN；模型基址由主线程同源断言保证。
    '      var origin = new URL(msg.transformersUrl).origin;',
    '      if (origin !== new URL(msg.modelBase).origin) throw new Error("offline guard: AI assets must be same-origin");',
    '      var isLocal = function (u) {',
    '        var s = String((u && u.url) || u);',
    '        return s.indexOf(origin) === 0 || s.indexOf("blob:") === 0 || s.indexOf("data:") === 0 || s.charAt(0) === "/" || s.indexOf("./") === 0;',
    '      };',
    '      var rawFetch = self.fetch.bind(self);',
    '      self.fetch = function (u, o) {',
    // 模型文件用相对 ID（model-v1/tokenizer.json），统一重写到同域 ./ai/ 下；
    // 绝对 URL 只允许同源，任何 CDN / HF 直接抛错。
    '        var raw = String((u && u.url) || u);',
    '        if (!/^[a-z]+:/i.test(raw)) u = new URL(raw.replace(/^\\.\\//, ""), msg.modelBase).href;',
    '        if (!isLocal(u)) return Promise.reject(new Error("offline guard: blocked " + String((u && u.url) || u)));',
    '        return rawFetch(u, o);',
    '      };',
    '      var RawWorker = self.Worker;',
    '      self.Worker = function (u, o) {',
    '        if (!isLocal(u)) throw new Error("offline guard: blocked worker " + String((u && u.url) || u));',
    '        return new RawWorker(u, o);',
    '      };',
    '      var T = await import(msg.transformersUrl);',
    // 相对模型 ID 才被判为「本地」，红线 allowRemoteModels=false 可以恢复；
    // 零外联由上面的同源白名单（fetch + 子 Worker）硬保证。
    '      T.env.allowRemoteModels = false;',
    '      T.env.allowLocalModels = true;',
    '      T.env.useBrowserCache = false;',     // Cache API 不接受相对 URL 作 key；同域资源由浏览器 HTTP 缓存承担
    '      T.env.localModelPath = "./";',       // 保持本地路径为相对，由 fetch guard 重写到同域
    '      T.env.remoteHost = origin + "/";',   // 即使误走远程分支也只可能拼出同域 URL
    '      T.env.remotePathTemplate = "_offline_/{model}/resolve/{revision}/";',
    '      T.env.wasmPaths = msg.wasmPaths;',    // v2 口径（兼容）
    '      T.env.backends = T.env.backends || {};',
    '      T.env.backends.onnx = T.env.backends.onnx || {};',
    '      T.env.backends.onnx.wasm = Object.assign({}, T.env.backends.onnx.wasm, { wasmPaths: msg.wasmPaths, proxy: false });',
    // 推理后端：自动模式先探 WebGPU（fp16，15s 超时），任何异常/超时都销毁会话回退 WASM/q8。
    // 仅 WASM 模式行为与旧版完全一致；fp16 与 q8 输出同维同池化，向量库不因后端切换重建。
    // 主线程 selfCheck 已预检 fp16 资产：缺失时 msg.fp16Missing=true，plan.gpu=false，
    // 整个 WebGPU 探测（含 requestAdapter / 15s pipeline 超时）被跳过，秒回退 WASM。
    '      var backendMode = msg.backendMode || "auto";',
    '      var fallbackReason = "";',
    '      var fallbackCode = "";',
    '      var activeBackend = "wasm";',
    '      var gen = 0;',
    '      var withTimeout = function (p, ms) {',
    '        return Promise.race([p, new Promise(function (_, rej) { setTimeout(function () { rej(new Error("timeout")); }, ms); })]);',
    '      };',
    '      var extractor = null;',
    '      var gpu = self.navigator && self.navigator.gpu;',
    '      var plan = (' + backendFallbackPlan.toString() + ')(backendMode, !!msg.fp16Missing, !!gpu);',
    '      fallbackReason = plan.reason;',
    '      fallbackCode = plan.code;',
    '      if (plan.gpu) {',
    '        try {',
    '          var adapter = await gpu.requestAdapter();',
    '          if (!adapter) { fallbackReason = "WebGPU 不可用：无 adapter"; fallbackCode = "no-adapter"; }',
    '          else {',
    '            var mine = ++gen;',
    '            try {',
    '              var w = await withTimeout(T.pipeline("feature-extraction", msg.modelUrl, { device: "webgpu", dtype: "fp16" }), 15000);',
    '              if (mine !== gen) { try { w.dispose && w.dispose(); } catch (e2) {} }',
    '              else { extractor = w; activeBackend = "webgpu"; fallbackReason = ""; fallbackCode = ""; }',
    '            } catch (e2) {',
    '              gen++;',
    '              var em = String((e2 && e2.message) || e2);',
    '              fallbackReason = em === "timeout" ? "WebGPU 初始化超时（15 秒）" : ("WebGPU 初始化失败：" + em);',
    '              fallbackCode = em === "timeout" ? "timeout" : "init-error";',
    '            }',
    '          }',
    '        } catch (e3) {',
    '          fallbackReason = "WebGPU 探测失败：" + String((e3 && e3.message) || e3);',
    '          fallbackCode = "probe-error";',
    '        }',
    '      }',
    '      if (!extractor) {',
    '        extractor = await T.pipeline("feature-extraction", msg.modelUrl, { device: "wasm", dtype: msg.dtype || "q8" });',
    '      }',
    '      self._ext = extractor;',
    '      self.postMessage({ type: "ready", id: msg.id, backend: activeBackend, fallbackReason: activeBackend === "webgpu" ? "" : fallbackReason, fallbackCode: activeBackend === "webgpu" ? "" : fallbackCode });',
    '    } catch (err) {',
    '      self.postMessage({ type: "error", id: msg.id, message: String((err && err.message) || err) });',
    '    }',
    '    return;',
    '  }',
    '  if (msg.type === "embed") {',
    '    try {',
    '      var out = await self._ext(msg.texts, { pooling: msg.pooling || "mean", normalize: true });',
    '      var data = out.data;',
    '      var dim = out.dims[out.dims.length - 1];',
    '      var vecs = [];',
    '      for (var i = 0; i < msg.texts.length; i++) {',
    '        var a = Array.from(data.subarray(i * dim, (i + 1) * dim));',
    '        vecs.push(a);',
    '      }',
    '      self.postMessage({ type: "embed", id: msg.id, vectors: vecs });',
    '    } catch (err) {',
    '      self.postMessage({ type: "embed", id: msg.id, error: String((err && err.message) || err) });',
    '    }',
    '    return;',
    '  }',
    '};'
  ].join('\n');

  /* ============================ 客户端工厂 ============================ */
  function createAIClient(opts) {
    opts = opts || {};
    // ① 永久拒绝任何「自动更新 / 检查更新 / 远端清单」入口（不是开关，不给打开的可能）。
    for (const k of FORBIDDEN_OPTS) {
      if (opts[k]) throw new Error('[local-ai] 自动更新/更新检查已永久禁用（拒绝参数 ' + k + '）：AI 资产只能由人工替换 ./ai/ 目录变更');
    }
    const here = global.location ? global.location.href : 'http://localhost/';
    const hereOrigin = new URL(here).origin;
    const base = opts.base || './ai/';
    // ② 硬性同源断言：base 只能是同域相对/根路径，任何公网 CDN / HF / 外部域名直接拒绝。
    if (/^https?:\/\//i.test(base) || /^\/\//.test(base) || new URL(base, here).origin !== hereOrigin) {
      throw new Error('[local-ai] base 必须是同域路径（./ai/），禁止公网 CDN / 外部域名');
    }
    const dbNamespace = opts.dbNamespace || 'marknote';
    const model = opts.model || 'model-v1';
    const dtype = opts.dtype || 'q8';
    // 推理后端：默认「自动」（WebGPU 优先）；仅 WASM 模式走完全等同旧版的行为。
    // 后端不进 ensureModelFingerprint 的指纹：切换不触发向量库重建。
    let backendMode = normalizeBackendMode(opts.backend || readBackendMode());
    let backendInfo = { mode: backendMode, active: '', reason: backendMode === 'wasm' ? '已选择仅 WASM' : '', code: backendMode === 'wasm' ? 'wasm-only' : '' };
    const absBase = new URL(base, here).href;
    const transformersUrl = absBase + 'transformers.min.js';
    const wasmPaths = absBase + 'ort/';
    // 模型 ID 必须是相对路径：transformers.js v3 把任何 http(s) 模型 ID 都判为「远程」，
    // 即使 allowRemoteModels=false 也会拒绝同域 URL。worker 内再把相对路径重写到同域 modelBase。
    const modelId = model;
    const modelBase = absBase;
    const dbName = dbNamespace + '-ai';

    let state = 'off';           // off | unavailable | indexing | idle
    let worker = null;
    let workerReady = false;
    let readyPromise = null;
    let lastError = '';
    let manifest = null;
    let dim = 0;
    let pooling = 'mean';
    let db = null;
    let reqSeq = 0;
    const pending = new Map();
    const listeners = new Set();
    // 单测注入点：直接挂 mock worker，跳过资产自检（生产路径从不传此参数）。
    if (opts && opts._injectWorker) {
      worker = opts._injectWorker;
      workerReady = true;
      attachWorkerHandlers(worker);
    }
    // 单测注入点：mock IndexedDB，使 ready() 可在 Node 下验证 selfCheck → init 参数透传。
    if (opts && opts._injectDB) db = opts._injectDB;

    function setState(s) { state = s; listeners.forEach((fn) => { try { fn(s); } catch (e) {} }); }
    function onState(fn) { listeners.add(fn); return () => listeners.delete(fn); }
    function status() { return state; }

    // 主线程所有 AI 请求统一走这里：只放行同源 ./ai/ 下的静态文件。
    // 公网 CDN / HF / 任何「版本或更新」端点都会在请求发起前被拒绝；
    // worker 内另有同源白名单（fetch + 子 Worker），两层都封死。
    function localFetch(url, init) {
      const u = new URL(url, here);
      if (u.origin !== hereOrigin || !/\/ai\//.test(u.pathname)) {
        return Promise.reject(new Error('[local-ai] blocked non-local AI request: ' + u.href));
      }
      return global.fetch(u.href, init);
    }

    function postWorker(type, payload) {
      return new Promise((resolve, reject) => {
        if (!worker) { reject(new Error('worker not ready')); return; }
        const id = ++reqSeq;
        pending.set(id, { resolve, reject });
        worker.postMessage(Object.assign({ type, id }, payload));
      });
    }

    async function ensureDB() {
      if (!db) db = await openDB(dbName);
      return db;
    }

    // 本地可用性自检 —— 注意：这不是「检查更新」。
    // 只读当前 ./ai/ 目录里已经存在的文件，确认它们与 manifest 记录的 hash 一致，
    // 结果仅用于判断「能不能用」；不比对任何远端版本、不请求更新端点、不下载或替换文件。
    async function selfCheck() {
      // file:// 下浏览器禁止 fetch 本地文件（URL scheme "file" is not supported），
      // 先给出明确可操作原因，避免落到晦涩的 fetch-failed 且让界面显示空白报错。
      if (global.location && String(global.location.protocol || '').toLowerCase() === 'file:') {
        return { ok: false, reason: 'file-protocol' };
      }
      try {
        const res = await localFetch(absBase + 'model-manifest.json', { cache: 'no-cache' });
        if (!res.ok) return { ok: false, reason: 'no-manifest' };
        manifest = await res.json();
        const files = [
          model + '/config.json',
          model + '/tokenizer.json',
          model + '/tokenizer_config.json',
          model + '/onnx/model_quantized.onnx'
        ];
        for (const f of files) {
          const r = await localFetch(absBase + f, { method: 'GET', cache: 'no-cache' });
          if (!r.ok) return { ok: false, reason: 'missing:' + f, fp16Missing: false };
        }
        // auto 模式追加 fp16 预检（只 HEAD，不拉 48MB 权重）：缺失不判 unavailable（WASM/q8 仍可用），
        // 仅标记让 init 跳过 WebGPU 探测和 15 秒超时，秒回退并上报原因。
        let fp16Missing = false;
        if (normalizeBackendMode(backendMode) === 'auto') {
          const f16 = model + '/onnx/model_fp16.onnx';
          try {
            const r16 = await localFetch(absBase + f16, { method: 'HEAD', cache: 'no-cache' });
            fp16Missing = !r16.ok;
          } catch (e) { fp16Missing = true; }
        }
        const emb = (manifest.capabilities && manifest.capabilities.embedding) || {};
        dim = emb.dim || 0;
        pooling = emb.pooling || 'mean';
        const hash = manifest.hash;
        if (!hash || hash.indexOf('REPLACE') === 0) {
          // 模型已放置但未生成 hash：放行（文件已校验存在），提示用户跑 compute 脚本
          return { ok: true, finalized: false, fp16Missing };
        }
        let okHash = await verifyHash(files, '');
        if (!okHash) {
          // 缓存错配兜底：带一次性 query 穿透 SW/HTTP 缓存重取真实磁盘文件。
          // 旧版 SW 的缓存优先可能让 manifest 与权重来自不同版本，重取一致即放行。
          try {
            okHash = await verifyHash(files, String(Date.now()));
          } catch (e) { /* 穿透失败保持原判定 */ }
        }
        if (!okHash) return { ok: false, reason: 'hash-mismatch', fp16Missing };
        return { ok: true, finalized: true, fp16Missing };
      } catch (e) {
        return { ok: false, reason: 'fetch-failed:' + String(e && e.message || e), fp16Missing: false };
      }
    }

    // 轻量 hash：配置三件套全量 + onnx 首尾各 256KB，SHA-256 拼接。
    // bust 非空时给每个 URL 加一次性查询串：Cache API 的 cache.match 按完整 URL 匹配，
    // 不同 query 必然 miss → 强制穿透 SW 缓存与 HTTP 缓存拿到磁盘上的真实字节。
    // 用途：manifest 与权重若被分别缓存到不同版本，第一次校验会假性不一致；
    // 穿透重取一次仍不一致，才判定资产真的损坏。
    async function verifyHash(files, bust) {
      let acc = new Uint8Array(0);
      for (const f of files) {
        const url = absBase + f + (bust ? '?__aiv=' + bust : '');
        const r = await localFetch(url, { cache: 'no-cache' });
        if (!r.ok) return false;
        const bytes = new Uint8Array(await r.arrayBuffer());
        if (f.endsWith('.onnx')) {
          // 大权重只取首尾各 256KB；必须与 fetch-model.mjs 的 computeHash 字节口径完全一致。
          // 注意 ArrayBuffer.slice() 返回 ArrayBuffer，concat() 需要 Uint8Array 视图。
          const head = bytes.subarray(0, 262144);
          const tail = bytes.subarray(Math.max(0, bytes.length - 262144));
          acc = concat(acc, head); acc = concat(acc, tail);
        } else {
          acc = concat(acc, bytes);
        }
      }
      const hex = await sha256Hex(acc);
      return !!hex && hex === manifest.hash;
    }
    /**
     * SHA-256 摘要（小写 hex）。Web Crypto 的 subtle.digest 只在安全上下文（https / localhost）
     * 可用；局域网 http（如 http://192.168.x.x:8100）下 window.crypto.subtle 为 undefined，
     * 若据此 return false 会把「非安全上下文」误判为 hash-mismatch，导致另一台 server 上
     * 勾选知识库恒报「模型文件指纹与 manifest 不一致」，而本地 localhost 正常。
     * 这里优先用 subtle，缺失时回退到 util.jsSha256Hex（纯 JS，输出逐字节一致，强度不降低）。
     */
    async function sha256Hex(bytes) {
      const c = global.crypto && global.crypto.subtle;
      if (c && typeof c.digest === 'function') {
        try {
          const digest = await c.digest('SHA-256', bytes);
          return [...new Uint8Array(digest)].map((x) => x.toString(16).padStart(2, '0')).join('');
        } catch (e) { /* 落到纯 JS 实现 */ }
      }
      return util.jsSha256Hex(bytes);
    }
    function concat(a, b) {
      const out = new Uint8Array(a.length + b.length);
      out.set(a, 0); out.set(b, a.length);
      return out;
    }

    // 本地向量库与「当前 ./ai/ 目录模型」的初始化一致性对齐 —— 不是更新检查。
    // 仅在 client 首次 ready 时执行一次，比较的是本地 manifest 的描述信息（dim/pooling/hash），
    // 不联网、不查远端版本、不下载任何文件；作用是人工替换目录换模型后旧向量不会维度错配。
    // 放在共享客户端层，marknote-ai 与 workbench-ai 换档时都无需各自做迁移。
    async function ensureModelFingerprint() {
      const emb = (manifest && manifest.capabilities && manifest.capabilities.embedding) || {};
      const fp = [
        manifest && manifest.version, manifest && manifest.model,
        emb.dim, emb.pooling, manifest && manifest.hash
      ].join('|');
      const prev = await getMeta('model-fp');
      if (prev === fp) return;
      const d = await ensureDB();
      await new Promise((resolve, reject) => {
        const tx = d.transaction(['embeddings', 'meta'], 'readwrite');
        tx.objectStore('embeddings').clear();
        tx.objectStore('meta').put({ k: 'model-fp', v: fp });
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
    }

    // worker 消息/错误处理器：真实 worker 与单测注入的 mock worker 共用一套
    function attachWorkerHandlers(w) {
      w.onmessage = (e) => {
        const m = e.data;
        // init 失败消息也必须能 reject，否则 ready() 永久挂起
        if (m.type === 'error' && m.id == null) {
          pending.forEach((p) => p.reject(new Error(m.message || 'worker error')));
          pending.clear();
          return;
        }
        const p = pending.get(m.id);
        if (m.type === 'ready') {
          workerReady = true;
          backendInfo = {
            mode: backendMode,
            active: m.backend === 'webgpu' ? 'webgpu' : 'wasm',
            reason: m.fallbackReason || '',
            code: m.fallbackCode || ''
          };
        }
        if (!p) return;
        pending.delete(m.id);
        if (m.type === 'error' || m.error) p.reject(new Error(m.message || m.error));
        else p.resolve(m);
      };
      w.onerror = (e) => {
        pending.forEach((p) => p.reject(new Error('worker error: ' + (e.message || e.filename))));
        pending.clear();
      };
    }

    // 加载 worker + 模型（跨 tab 互斥），失败→unavailable
    function ready() {
      if (readyPromise) return readyPromise;
      readyPromise = (async () => {
        const check = await selfCheck();
        if (!check.ok) {
          // 把自检失败原因写进 lastError，供三台 lastError() 读取并展示可操作提示；
          // 此前只 setState('unavailable')，导致界面显示空白「本地 AI 资产不可用：」。
          lastError = selfCheckReasonText(check.reason);
          try { console.warn('[local-ai] selfCheck failed:', check.reason); } catch (_) {}
          setState('unavailable');
          return false;
        }
        try {
          await withLock('mn-ai-init-' + dbNamespace, async () => {
            await ensureModelFingerprint();
            if (workerReady) return;
            const blob = new Blob([WORKER_SRC], { type: 'text/javascript' });
            worker = new Worker(URL.createObjectURL(blob), { type: 'module' });
            attachWorkerHandlers(worker);
            await postWorker('init', { transformersUrl, wasmPaths, modelUrl: modelId, modelBase, dtype, pooling, backendMode, fp16Missing: !!check.fp16Missing });
            workerReady = true;
          });
          setState('idle');
          return true;
        } catch (e) {
          lastError = String((e && e.message) || e);
          try { console.warn('[local-ai] init failed:', lastError); } catch (_) {}
          setState('unavailable');
          return false;
        }
      })();
      return readyPromise;
    }

    // 向量入库 / 读取 / 删除（向量库独立，不进主 sqlite）
    async function putEmbedding(rec) {
      const d = await ensureDB();
      return new Promise((resolve, reject) => {
        const tx = d.transaction('embeddings', 'readwrite');
        tx.objectStore('embeddings').put({
          note_id: rec.note_id, nb_id: rec.nb_id || null,
          vector: rec.vector, mtime: rec.mtime || 0, sig: rec.sig || '', modelVer: rec.modelVer || (manifest && manifest.version) || ''
        });
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    }
    async function getEmbedding(note_id) {
      const d = await ensureDB();
      return new Promise((resolve, reject) => {
        const tx = d.transaction('embeddings', 'readonly');
        const r = tx.objectStore('embeddings').get(note_id);
        r.onsuccess = () => resolve(r.result || null);
        r.onerror = () => reject(r.error);
      });
    }
    async function deleteEmbedding(note_id) {
      const d = await ensureDB();
      return new Promise((resolve, reject) => {
        const tx = d.transaction('embeddings', 'readwrite');
        tx.objectStore('embeddings').delete(note_id);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    }
    async function getAllEmbeddings() {
      const d = await ensureDB();
      return new Promise((resolve, reject) => {
        const tx = d.transaction('embeddings', 'readonly');
        const r = tx.objectStore('embeddings').getAll();
        r.onsuccess = () => resolve(r.result || []);
        r.onerror = () => reject(r.error);
      });
    }
    async function getMeta(k) {
      const d = await ensureDB();
      return new Promise((resolve, reject) => {
        const tx = d.transaction('meta', 'readonly');
        const r = tx.objectStore('meta').get(k);
        r.onsuccess = () => resolve(r.result ? r.result.v : undefined);
        r.onerror = () => reject(r.error);
      });
    }
    async function setMeta(k, v) {
      const d = await ensureDB();
      return new Promise((resolve, reject) => {
        const tx = d.transaction('meta', 'readwrite');
        tx.objectStore('meta').put({ k, v });
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    }

    // 批量文本 → 向量（已 ready 后调用）
    // 根治 bad_alloc：入口内部强制分块——超过 24 条按 24 切片（WebGPU 放宽到 48），
    // 块间 await setTimeout(0) 让出事件循环与 WASM 堆，结果按原顺序拼回，调用方零感知。
    async function embed(texts) {
      if (!Array.isArray(texts)) texts = [texts];
      if (!texts.length) return [];
      const size = backendInfo.active === 'webgpu' ? EMBED_BATCH_WEBGPU : EMBED_BATCH;
      return util.embedChunked(texts, size, async (chunk) => {
        const res = await postWorker('embed', { texts: chunk });
        if (res.error) throw new Error(res.error);
        return res.vectors;
      });
    }

    // 语义搜索：用已索引向量与 query 向量比对，返回 topK（相似度>=threshold）。
    // 可选 o.scope = { nbIds:Set } 或 { noteIds:Set }：在打分循环内过滤，
    // topK 取「过滤后」的结果，绝不先取全局 topK 再裁掉（否则范围内会不足 topK）。
    // 未传 scope 时行为与旧版完全一致，/auto 等调用方零影响。
    async function semanticSearch(query, o) {
      o = o || {};
      const topK = o.topK || 10;
      const threshold = (o.threshold != null) ? o.threshold : 0.5;
      const sc = o.scope || null;
      const nbIds = sc && sc.nbIds;
      const noteIds = sc && sc.noteIds;
      const qv = (await embed([query]))[0];
      const all = await getAllEmbeddings();
      const scored = [];
      for (const e of all) {
        if (!e.vector) continue;
        if (nbIds && !nbIds.has(e.nb_id)) continue;
        if (noteIds && !noteIds.has(e.note_id)) continue;
        const s = util.cosine(qv, e.vector);
        if (s >= threshold) scored.push({ id: e.note_id, nb_id: e.nb_id, score: s });
      }
      scored.sort((a, b) => b.score - a.score);
      return scored.slice(0, topK);
    }

    // 相关笔记：与某笔记余弦 > threshold（默认 0.6）的其它笔记
    async function related(noteId, o) {
      o = o || {};
      const topK = o.topK || 10;
      const threshold = (o.threshold != null) ? o.threshold : 0.6;
      const mine = await getEmbedding(noteId);
      if (!mine || !mine.vector) return [];
      const all = await getAllEmbeddings();
      const scored = [];
      for (const e of all) {
        if (e.note_id === noteId || !e.vector) continue;
        const s = util.cosine(mine.vector, e.vector);
        if (s >= threshold) scored.push({ id: e.note_id, nb_id: e.nb_id, score: s });
      }
      scored.sort((a, b) => b.score - a.score);
      return scored.slice(0, topK);
    }

    // 抽取式摘要（中文按标点断句 → 句向量 → MMR 去冗余 → 按原序拼接）
    async function summarize(text, o) {
      o = o || {};
      const maxSentences = o.maxSentences || 3;
      const lambda = (o.lambda != null) ? o.lambda : 0.7;
      const sentences = util.splitSentences(text);
      if (sentences.length <= maxSentences) return sentences.join('');
      const vecs = await embed(sentences);
      const n = vecs.length;
      // 文档中心 = 句向量均值
      const dimV = vecs[0].length;
      const centroid = new Array(dimV).fill(0);
      for (const v of vecs) for (let i = 0; i < dimV; i++) centroid[i] += v[i] / n;
      const scores = vecs.map((v) => util.cosine(v, centroid));
      const simMatrix = [];
      for (let i = 0; i < n; i++) { simMatrix[i] = []; for (let j = 0; j < n; j++) simMatrix[i][j] = util.cosine(vecs[i], vecs[j]); }
      const idx = util.mmrSelect(scores, simMatrix, maxSentences, lambda);
      idx.sort((a, b) => a - b);
      return idx.map((i) => sentences[i]).join('');
    }

    function disable() { setState('off'); }

    function destroy() {
      if (worker) { try { worker.terminate(); } catch (e) {} worker = null; }
      workerReady = false; readyPromise = null;
    }

    // 推理后端切换：只销毁/重建 worker 会话，向量库（IndexedDB）完全不动。
    // 此前已就绪 → 立即按新后端重建；否则只落偏好，下次 ready()/开启 AI 时生效。
    async function setBackend(mode) {
      const v = writeBackendMode(mode);
      backendMode = v;
      backendInfo = { mode: v, active: '', reason: v === 'wasm' ? '已选择仅 WASM' : '', code: v === 'wasm' ? 'wasm-only' : '' };
      const wasReady = state === 'idle' || state === 'indexing';
      destroy();
      if (wasReady) await ready();
      return { ok: state !== 'unavailable', info: Object.assign({}, backendInfo) };
    }
    function backend() { return Object.assign({}, backendInfo); }

    return {
      base, dbNamespace, model, dbName,
      ready, status, onState, disable, destroy, setBackend, backend,
      lastError: () => lastError,
      selfCheck,
      embed, semanticSearch, related, summarize,
      putEmbedding, getEmbedding, deleteEmbedding, getAllEmbeddings,
      getMeta, setMeta
    };
  }

  // policy 冻结后暴露给页面与回归脚本：e2e / check:ai 直接读取断言，
  // 任何人把 autoUpdate / updateCheck / publicCdn 改成 true 都会导致验收失败。
  const API = {
    createAIClient, util, policy: AI_POLICY, selfCheckReasonText,
    // 供三台设置卡与单测读取；与 embedBatch.js 同源（check:ai 会静态核对数值）。
    constants: Object.freeze({ EMBED_BATCH, EMBED_BATCH_WEBGPU, MAX_EMBED_TEXT_CHARS: 4000, WEBGPU_TIMEOUT_MS }),
    backend: Object.freeze({ key: BACKEND_KEY, read: readBackendMode, write: writeBackendMode })
  };
  global.MN_AI = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
