# model-v1（语义搜索模型权重 · 当前档位：zh-en）

本目录由 `node shared/ai/fetch-model.mjs --lang zh-en`（默认）在**部署准备阶段**生成，构建时
由 `copyAiAssets()` 原样拷进 `dist/ai/model-v1/`：

- `config.json`
- `tokenizer.json`
- `tokenizer_config.json`
- `onnx/model_quantized.onnx`（q8 量化，约 23MB，WASM 后端）
- `onnx/model_fp16.onnx`（fp16，约 45MB，WebGPU 后端；由 `--fp16` 单独下载，不进 hash 基线）

当前档位：**zh-en 中英双语轻量档**，`Xenova/bge-small-zh-v1.5`，dim=512，pooling=cls，
词表 2.1 万（bert-base-chinese）。中文语义检索准确，英文术语由子词 + 关键词匹配兜底。

运行时全部从同域 `./ai/model-v1/` 加载，**绝不访问 huggingface.co、CDN 或任何外部地址**；
worker 内有同源白名单硬拦截。

未放置模型时，AI 增强包自检判定为「不可用」，入口隐藏，主功能零影响。
换语言档位见 `shared/ai/README.md`；换档后客户端按模型指纹自动清空并重建向量库。
