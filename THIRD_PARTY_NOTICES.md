# Third-party files included in this repository

Job Agent includes the following third-party model files and browser libraries so that it runs fully offline.
Their license texts are in [`licenses/`](licenses/). `fetch_models.py` shows where each one comes from and can
download them again.

| Files | What | Source | License |
|---|---|---|---|
| `models/onnx/Qwen2.5-0.5B-Instruct/` | Qwen2.5-0.5B-Instruct, ONNX 8-bit build (CPU engine). `onnx/model_quantized.onnx` is stored as `.part1`–`.part6` because of GitHub's 100 MB file limit; the app joins the parts when it serves the file. | [onnx-community/Qwen2.5-0.5B-Instruct](https://huggingface.co/onnx-community/Qwen2.5-0.5B-Instruct), converted from [Qwen/Qwen2.5-0.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct) | Apache-2.0 (Qwen) — [`licenses/Qwen2.5-0.5B-Instruct.LICENSE.txt`](licenses/Qwen2.5-0.5B-Instruct.LICENSE.txt) |
| `models/webllm/Qwen2.5-0.5B-Instruct-q4f16_1-MLC/`, `…-q4f32_1-MLC/` | Qwen2.5-0.5B-Instruct, 4-bit WebLLM builds (GPU engine) | [mlc-ai on Hugging Face](https://huggingface.co/mlc-ai/Qwen2.5-0.5B-Instruct-q4f16_1-MLC), converted from Qwen/Qwen2.5-0.5B-Instruct | Apache-2.0 (Qwen) — same file |
| `models/webllm/libs/*.wasm` | WebGPU model libraries for the builds above | [mlc-ai/binary-mlc-llm-libs](https://github.com/mlc-ai/binary-mlc-llm-libs) | Published by the MLC project for use with WebLLM; that repository does not state a license (MLC LLM itself is Apache-2.0) |
| `static/vendor/web-llm/index.js` | WebLLM 0.2.85 | [@mlc-ai/web-llm](https://www.npmjs.com/package/@mlc-ai/web-llm) | Apache-2.0 — [`licenses/web-llm.LICENSE.txt`](licenses/web-llm.LICENSE.txt) |
| `static/vendor/transformers/transformers.min.js` | Transformers.js 4.3.0 | [@huggingface/transformers](https://www.npmjs.com/package/@huggingface/transformers) | Apache-2.0 — [`licenses/transformers.js.LICENSE.txt`](licenses/transformers.js.LICENSE.txt) |
| `static/vendor/ort/*` | ONNX Runtime Web (WebAssembly build) | [onnxruntime-web](https://www.npmjs.com/package/onnxruntime-web) | MIT — [`licenses/onnxruntime-web.LICENSE.txt`](licenses/onnxruntime-web.LICENSE.txt) |

Qwen2.5 is developed by the Qwen team, Alibaba Cloud. The files are unchanged apart from the split described above.
