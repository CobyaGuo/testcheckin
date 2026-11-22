# 本地 AI / OCR 资产版本清单

> 全部资产 vendor 化落 `dist/ai/`（构建时由 vite 插件从 `shared/ai/` 原样拷贝），
> 运行时同源加载，**零外联**：禁止 CDN / 网络下载 / 更新检查（`npm run check:ai` 静态审计）。

## AI 语义（已有）

| 组件 | 版本 | 来源 | 位置 |
| --- | --- | --- | --- |
| transformers.js | 见 model-manifest.json | 本地 vendor | `ai/transformers.min.js` |
| onnxruntime-web (ort) | 见 model-manifest.json | 本地 vendor | `ai/ort/` |
| embedding 模型（q8 / WASM） | model-v1 | `Xenova/bge-small-zh-v1.5` onnx quantized，约 22.9MB | `ai/model-v1/onnx/model_quantized.onnx` |
| embedding 模型（fp16 / WebGPU） | model-v1 | 同仓库 fp16，约 45.3MB，`--fp16` 构建期一次性下载 | `ai/model-v1/onnx/model_fp16.onnx` |

## OCR（图片文字识别，MarkNote 专用）

| 组件 | 版本 | 来源 | 位置 |
| --- | --- | --- | --- |
| tesseract.js | 5.1.1 | npm pack `tesseract.js@5.1.1` | `ai/ocr/tesseract/tesseract.min.js`、`ai/ocr/tesseract/worker.min.js` |
| tesseract.js-core | 5.1.1 | npm pack `tesseract.js-core@5.1.1`（仅 LSTM wasm：simd + 非 simd） | `ai/ocr/tesseract/tesseract-core-*-lstm.wasm(.js)` |
| eng 语言包 | tessdata_fast 4.0.0 | `https://tessdata.projectnaptha.com/4.0.0/eng.traineddata.gz`（构建期一次性获取） | `ai/ocr/eng.traineddata.gz`（10.4 MB） |
| chi_sim 语言包 | tessdata_fast 4.0.0 | `https://tessdata.projectnaptha.com/4.0.0/chi_sim.traineddata.gz`（构建期一次性获取） | `ai/ocr/chi_sim.traineddata.gz`（19.2 MB） |

说明：

- 语言包只带 `chi_sim` + `eng` 两份；`eng` 默认加载，文本 CJK 占比超阈值（18%）时懒加载 `chi_sim`。
- 运行时加载入口：`src/marknote/ocrClient.js`（路径全部同源推导，`gzip: true`、`cacheMethod: 'none'`、`workerBlobURL: false`）。
- OCR 总体积约 33 MB，全部在 `ai/ocr/` 子目录，不进单文件 bundle；SW 不预缓存 `ai/`，运行时命中才缓存。
- 升级引擎 / 语言包时：更新本清单版本号 + 重新 vendor 对应文件，运行时路径与文件名保持不变。
