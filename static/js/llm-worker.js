// Runs Qwen2.5-0.5B (WebLLM / WebGPU) off the main thread so the UI stays smooth while it generates.
import { WebWorkerMLCEngineHandler } from "/static/vendor/web-llm/index.js";

const handler = new WebWorkerMLCEngineHandler();
self.onmessage = (msg) => handler.onmessage(msg);
