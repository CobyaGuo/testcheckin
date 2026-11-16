# shared/ai — 平台级「纯本地语义搜索」包

> 最高优先级约束：**运行时严禁任何外部调用与下载。**
> 断网状态下全部功能可用；模型、推理运行时、词表全部同域放置于 `dist/ai/`。

## 目录约定

```
shared/ai/                       （build 时整体拷进 dist/ai/，四台产物共用）
├── ai-client.js                 # 框架无关通用客户端（IIFE，挂 window.MN_AI），不进打包器
├── transformers.min.js          # @huggingface/transformers@3.8.1 的 dist 产物（vendor）
├── ort/                         # onnxruntime-web 运行时 wasm/glue（vendor，CPU 推理）
│   ├── ort-wasm-simd-threaded.wasm        # 单线程 SIMD（无需 COOP/COEP，兼容所有静态托管）
│   ├── ort-wasm-simd-threaded.jsep.wasm   # 多线程 JSEP（需跨域隔离头，有则更快）
│   └── *.mjs / *.min.mjs 等胶水
├── model-v1/                    # 默认 zh-en 中英双语轻量档（部署准备阶段由 fetch-model.mjs 取得）
│   ├── config.json / tokenizer.json / tokenizer_config.json
│   └── onnx/model_quantized.onnx (q8/WASM) + model_fp16.onnx (fp16/WebGPU，--fp16 下载)
├── model-manifest.json          # {version,hash,profile,lang,capabilities,backends}；hash 不符→unavailable
├── embedBatch.js                # 三台共用的分块/后端常量（24/48/4000/15s、ai-infer-backend）
└── fetch-model.mjs              # 仅部署准备阶段联网：选择语言档位 / 下载 q8 / --fp16 / 计算 hash
```

## 零外联怎么保证

1. **transformers / ort wasm / 模型** 全部放在 `dist/ai/`，不进 vite 打包、不内联、不加入 SW 预缓存
   （SW 仅在运行时命中才缓存）。运行时只发同域 `./ai/*` 请求。
2. worker 启动前给 `self.fetch` 与 `self.Worker`（子 worker）套一层 **同源白名单**：
   任何非 `location.origin` / `blob:` / `data:` 的 URL 直接抛错；相对路径统一重写到同域 `./ai/` 下。
3. `transformers.min.js` 是 ESM（内部用 `import.meta.url` 建 ORT 子 worker），必须用
   **module worker + 动态 `import()`**；classic worker 的 `importScripts` 会报 `import.meta outside a module`。
4. 加载后覆盖 v2 / v3 两套路径口径（3.8.1 实际读 `env.backends.onnx.wasm.wasmPaths`，
   只设 v2 的 `env.wasmPaths` 会落到库内置的 `cdn.jsdelivr.net` 默认值）：
   ```js
   env.allowRemoteModels = false;         // 模型 ID 用相对名 'model-v1' 才被判为「本地」
   env.allowLocalModels  = true;
   env.localModelPath    = './';          // 保持相对，由 fetch 守卫重写到同域
   env.useBrowserCache   = false;         // Cache API 不接受相对 URL 作 key
   env.remoteHost        = <同域 origin>; // 即使误走远程分支也拼不出外部域名
   env.wasmPaths         = <同域>/ai/ort/;                 // v2 兼容
   env.backends.onnx.wasm.wasmPaths = <同域>/ai/ort/;      // v3 实际生效
   ```
   > 注意：v3 把**任何 http(s) 模型 ID** 都判为 remote，所以不能把绝对同域 URL 传给 `pipeline()`；
   > 传相对 ID + 白名单重写，才能同时满足「本地加载」与「零外联」。
5. 推理在 **module Web Worker** 内，跨 tab 用 `navigator.locks` 互斥。
6. 启动 `selfCheck()` 读 `model-manifest.json` 并校验文件存在 + hash；不符→`unavailable`，**不降级任何外源**。

> vendor 库 `transformers.min.js` 内部保留了官方默认域名常量，但上述第 2、3 条已让它们永远不会被使用。

## 不可变资产：永不检查更新 / 永不自动更新

`./ai/` 是**部署时人工放置的不可变资产**。workbench 与 marknote 运行时：

- **不检查更新**：没有版本端点、没有远端清单比对、没有后台轮询、没有「是否有新版」请求。
  `selfCheck()` 只是读当前目录已有文件做本地完整性校验（判断能不能用），不是更新检查。
- **不自动更新**：不下载、不替换、不生成 `./ai/` 下任何文件；`model-manifest.json` 只描述本地文件。
- **不连公网**：`createAIClient()` 硬性断言 `base` 必须同源；`FORBIDDEN_OPTS`
  （`autoUpdate` / `checkUpdate` / `updateCheck` / `updateUrl` / `manifestUrl` / `versionUrl` /
  `remoteBase` / `cdnBase`）传入即抛错。主线程 `localFetch()` 与 worker 内同源白名单双层拦截，
  公网 CDN / HuggingFace 请求永远发不出去。
- 冻结策略 `window.MN_AI.policy = { autoUpdate:false, updateCheck:false, publicCdn:false, sameOriginOnly:true }`
  由 e2e 与 `check:ai` 直接断言，任何人改成 `true` 都会导致验收失败。

**允许、且仅有的两条变更途径（都是主动行为，运行时不会自己触发）：**

1. 构建期主动同步：`npm run build` 把 `shared/ai/` 原样拷进 `dist/ai/`
   （换语言档位用 `fetch-model.mjs --lang <档位>` 后再构建）。
2. 人工替换整个 `dist/ai/`（或 `shared/ai/`）目录 → 下次启用 AI 读到的就是新文件。
   注意：SW 对同域资源是缓存优先，若只替换静态文件而不重新构建，浏览器可能仍命中旧 SW 缓存，
   需要清一次站点数据或硬刷新；正常重新发版时 SW 缓存名随构建必变，旧缓存会在 activate 阶段清掉。

> 边界：向量索引会随业务数据自动增量更新（见下节），也会在人工换模型后按**本地** manifest 的
> dim/pooling/hash 对齐维度——这些是数据层行为，无任何网络请求，不等于 AI 资产被自动更新。

## 多应用共用

- 两个应用（`marknote.html` / `index.html` 工作台）引用同一份 `dist/ai/ai-client.js`；
  `auto.html` / `perf.html` 独立台未接入 AI。
- 向量库按 `dbNamespace` 隔离：`marknote-ai` / `workbench-ai`，互不可见。
- 模型与 ort wasm 同源同路径 → 浏览器 HTTP/SW 缓存共享，第二个应用首次加载无需重新下载。
- 主构建默认清空 `dist`，故 `vite.config.js` 与 `vite.marknote.config.js` 各自带 `copyAiAssets()`
  在 `closeBundle` 阶段重拷；auto/perf 构建 `emptyOutDir:false` 不会抹掉。

## 语言档位（部署准备阶段，只需一次，需联网）

```bash
node shared/ai/fetch-model.mjs --list              # 查看可选档位
node shared/ai/fetch-model.mjs                     # 默认 zh-en 中英双语轻量档（~23MB，bge-small-zh-v1.5）
node shared/ai/fetch-model.mjs --lang multilingual # 全语言档（~118MB，multilingual-e5-small）
node shared/ai/fetch-model.mjs --lang en           # 纯英文档（~23MB，all-MiniLM-L6-v2）
node shared/ai/fetch-model.mjs --compute-hash      # 仅重算 hash（不联网、不改档位）
```

- 下载源自动探测 `huggingface.co` → 回退 `hf-mirror.com`；这只是**构建前**把文件抓回本地，运行时零外联。
- 换档位后 `version / dim / pooling / hash` 变化，客户端检测到「模型指纹」变化会**自动清空并重建**向量库。
- 默认 `zh-en` 档中文语义检索准确；英文术语由词表子词 + 命令面板关键词匹配兜底。
  需要真正的英文语义检索时切 `multilingual` 档（代价是体积约 5 倍）。
- 模型权重随仓库提交（`shared/ai/model-v1/`，约 23MB），build 时由 `copyAiAssets()` 自动进入
  `dist/ai/model-v1/`；**clone 后无需联网即可构建出完整离线产物**。
  换档位时重新执行一次脚本覆盖该目录（会同步改写 manifest 的 version/dim/pooling/hash）。

## 索引自动更新

内容签名（`textSig`）+ 数据签名双保险，不依赖业务对象是否有 `updatedAt`：

- **正文改动** → 向量记录里存文本指纹，指纹变化即重算该条向量。
- **新增 / 删除 / 恢复 / 导入 / 重命名** → MarkNote 用轻量 SQL 签名（条数 + max/sum mtime）轮询，
  Workbench 用 `getRev()` + 复盘内容签名轮询；删除的向量由 `active` 集合级联清理。
- 编辑器保存即时通知（防抖 1.2s），另有 2～2.5s 轮询兜底所有写路径；手动「重建索引」仍可用。

## 回归验收

```bash
npm run build
npm run check:ai                              # 静态红线：源码里没有更新检查实现
node tests/e2e/suite/e2e-ai-offline.mjs        # 浏览器行为：零外联 + 更新入口被拒
```

`check:ai` 断言 `AI_POLICY` 三个 false、更新参数永久拒绝、所有主线程 fetch 收敛到同源白名单、
两个平台注入地址恒为 `./ai/ai-client.js`。e2e 套件自带同域静态服务，断言模型四件套与 hash
通过、512 维推理与语义检索可用、ort wasm 从同域加载、外部域名与公网 CDN 请求均为 0、
`policy` 声明成立且公网 base / `checkUpdate` 参数被硬性拒绝、无 404 / 504 offline。

## 容量提示

`ort/` 约 44MB（jsep + simd 双 wasm，jsep 是 WebGPU 载体，务必保留）；默认 `zh-en` 模型 q8 约 23MB、
fp16（WebGPU 专用）约 45MB，全语言档约 118MB。fp16 与 q8 输出同维同池化，manifest 的 hash 基线
只基于 q8；后端切换（localStorage `ai-infer-backend`）只重建 worker，不重建向量库。`client.embed()`
入口强制分块（WASM 24 / WebGPU 48，块间让出事件循环），从根上防 WASM 堆耗尽 `bad_alloc`。
SW 运行时缓存会缓存这些同域资源；若内网静态托管对 `Content-Type` 有要求，确保 `.wasm` 以
`application/wasm` 提供（多线程 jsep 变体需要 `COOP: same-origin` + `COEP: require-corp` 才能用
SharedArrayBuffer；不发送这两个头时自动回退到单线程 simd wasm，功能不受影响，仅稍慢）。
