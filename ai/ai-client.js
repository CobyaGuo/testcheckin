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
    '      var extractor = await T.pipeline("feature-extraction", msg.modelUrl, { device: "wasm", dtype: msg.dtype || "q8" });',
    '      self._ext = extractor;',
    '      self.postMessage({ type: "ready", id: msg.id });',
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
          if (!r.ok) return { ok: false, reason: 'missing:' + f };
        }
        const emb = (manifest.capabilities && manifest.capabilities.embedding) || {};
        dim = emb.dim || 0;
        pooling = emb.pooling || 'mean';
        const hash = manifest.hash;
        if (!hash || hash.indexOf('REPLACE') === 0) {
          // 模型已放置但未生成 hash：放行（文件已校验存在），提示用户跑 compute 脚本
          return { ok: true, finalized: false };
        }
        const okHash = await verifyHash(files);
        if (!okHash) return { ok: false, reason: 'hash-mismatch' };
        return { ok: true, finalized: true };
      } catch (e) {
        return { ok: false, reason: 'fetch-failed:' + String(e && e.message || e) };
      }
    }

    // 轻量 hash：配置三件套全量 + onnx 首尾各 256KB，SHA-256 拼接
    async function verifyHash(files) {
      const c = global.crypto && global.crypto.subtle;
      if (!c) return false;
      let acc = new Uint8Array(0);
      for (const f of files) {
        const r = await localFetch(absBase + f, { cache: 'no-cache' });
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
      const digest = await c.digest('SHA-256', acc);
      const hex = [...new Uint8Array(digest)].map((x) => x.toString(16).padStart(2, '0')).join('');
      return hex === manifest.hash;
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

    // 加载 worker + 模型（跨 tab 互斥），失败→unavailable
    function ready() {
      if (readyPromise) return readyPromise;
      readyPromise = (async () => {
        const check = await selfCheck();
        if (!check.ok) { setState('unavailable'); return false; }
        try {
          await withLock('mn-ai-init-' + dbNamespace, async () => {
            await ensureModelFingerprint();
            if (workerReady) return;
            const blob = new Blob([WORKER_SRC], { type: 'text/javascript' });
            worker = new Worker(URL.createObjectURL(blob), { type: 'module' });
            worker.onmessage = (e) => {
              const m = e.data;
              // init 失败消息也必须能 reject，否则 ready() 永久挂起
              if (m.type === 'error' && m.id == null) {
                pending.forEach((p) => p.reject(new Error(m.message || 'worker error')));
                pending.clear();
                return;
              }
              const p = pending.get(m.id);
              if (m.type === 'ready') workerReady = true;
              if (!p) return;
              pending.delete(m.id);
              if (m.type === 'error' || m.error) p.reject(new Error(m.message || m.error));
              else p.resolve(m);
            };
            worker.onerror = (e) => {
              pending.forEach((p) => p.reject(new Error('worker error: ' + (e.message || e.filename))));
              pending.clear();
            };
            await postWorker('init', { transformersUrl, wasmPaths, modelUrl: modelId, modelBase, dtype, pooling });
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
    async function embed(texts) {
      if (!Array.isArray(texts)) texts = [texts];
      if (!texts.length) return [];
      const res = await postWorker('embed', { texts });
      if (res.error) throw new Error(res.error);
      return res.vectors;
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

    return {
      base, dbNamespace, model, dbName,
      ready, status, onState, disable, destroy,
      lastError: () => lastError,
      selfCheck,
      embed, semanticSearch, related, summarize,
      putEmbedding, getEmbedding, deleteEmbedding, getAllEmbeddings,
      getMeta, setMeta
    };
  }

  // policy 冻结后暴露给页面与回归脚本：e2e / check:ai 直接读取断言，
  // 任何人把 autoUpdate / updateCheck / publicCdn 改成 true 都会导致验收失败。
  const API = { createAIClient, util, policy: AI_POLICY };
  global.MN_AI = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
