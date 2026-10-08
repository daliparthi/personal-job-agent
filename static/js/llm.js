// Qwen2.5-0.5B-Instruct running entirely in this browser, loaded from the files bundled in ./models.
//   CPU  ONNX via WebAssembly -> models/onnx/...    the default; works in every browser
//   GPU  WebLLM via WebGPU    -> models/webllm/...  used only when this browser has a real, working GPU
// If the GPU path fails (no WebGPU, software-only adapter, load error, or a lost device mid-run),
// the CPU model takes over automatically.
//
// Optionally (Settings > AI engine) Ollama, an OpenAI-compatible server or Anthropic answers instead, through this
// server (/api/llm/*, which holds the API key). The bundled model is then never loaded unless that engine fails:
// before the first word of a reply, the bundled model takes over just like the CPU does for the GPU.

const ORIGIN = location.origin;
export const EXTERNAL_ENGINES = { ollama: "Ollama", openai: "OpenAI", anthropic: "Anthropic" };

async function problem(r) {
  try { return (await r.json()).detail || `${r.status} ${r.statusText}`; } catch { return `${r.status} ${r.statusText}`; }
}

/** Resolve on the next task (not microtask), giving the browser a chance to repaint. Not throttled in background tabs. */
const nextTask = () => new Promise((resolve) => {
  const ch = new MessageChannel();
  ch.port1.onmessage = resolve;
  ch.port2.postMessage(0);
});

class AsyncQueue {
  constructor() { this.items = []; this.waiters = []; this.closed = false; }
  push(v) { const w = this.waiters.shift(); w ? w({ value: v, done: false }) : this.items.push(v); }
  close() { this.closed = true; this.waiters.splice(0).forEach((w) => w({ value: undefined, done: true })); }
  next() {
    if (this.items.length) return Promise.resolve({ value: this.items.shift(), done: false });
    if (this.closed) return Promise.resolve({ value: undefined, done: true });
    return new Promise((r) => this.waiters.push(r));
  }
  [Symbol.asyncIterator]() { return this; }
}

export class LocalLLM {
  constructor() {
    this.state = "idle"; // idle | loading | ready | error
    this.label = "";
    this.backend = null;
    this.engineName = "";
    this.listeners = new Set();
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(text, progress) { this.listeners.forEach((fn) => fn({ state: this.state, label: this.label, text, progress })); }

  /** preference: "auto", "onnx" or an external engine (EXTERNAL_ENGINES); model: that engine's model name. */
  async load(bundled, preference = "auto", model = "") {
    if (this.state === "ready") return;
    if (this.loading) return this.loading;
    this.loading = this._load(bundled, preference, model).finally(() => { this.loading = null; });
    return this.loading;
  }

  /** Why the GPU can't be used, or null when it can (plus the build to load). */
  async _probeGpu(bundled) {
    const builds = bundled.webllm || {};
    if (!bundled.webllm_js || !Object.keys(builds).length) return { reason: "the GPU model is not bundled" };
    if (!navigator.gpu) return { reason: "this browser has no WebGPU" };
    let adapter = null;
    try { adapter = await navigator.gpu.requestAdapter(); } catch { adapter = null; }
    if (!adapter) return { reason: "no GPU adapter" };
    if (adapter.info?.isFallbackAdapter || adapter.isFallbackAdapter) return { reason: "only a software GPU is available" };
    const f16 = adapter.features.has("shader-f16");
    const key = f16 && builds.q4f16 ? "q4f16" : builds.q4f32 ? "q4f32" : null;
    if (!key) return { reason: "this GPU lacks shader-f16 and the f32 build is not bundled" };
    return { key, build: builds[key] };
  }

  async _load(bundled, preference, model) {
    this.state = "loading";
    this.bundled = bundled;
    this.gpuNote = "";
    try {
      if (preference in EXTERNAL_ENGINES) {
        this.emit(`Connecting to ${EXTERNAL_ENGINES[preference]}…`, 0);
        try {
          await this._loadRemote(preference, model);
          this.state = "ready";
          this.emit("Ready", 1);
          return;
        } catch (e) {
          console.warn("External model unavailable, using the bundled one:", e);
          this.gpuNote = `${EXTERNAL_ENGINES[preference]} not used: ${String(e.message || e).slice(0, 160)}`;
          preference = "auto";
        }
      }
      if (preference !== "onnx") {
        this.emit("Checking for a GPU…", 0);
        const gpu = await this._probeGpu(bundled);
        if (gpu.key) {
          try {
            await this._loadWebLLM(gpu.build);
            this.label = `GPU · WebGPU ${gpu.key}`;
            this.state = "ready";
            this.emit("Ready", 1);
            return;
          } catch (e) {
            console.warn("GPU model failed, using CPU:", e);
            this.gpuNote = `GPU failed to load (${String(e.message || e).slice(0, 120)})`;
            await this._unloadWebLLM();
          }
        } else this.gpuNote = `GPU not used: ${gpu.reason}`;
      }
      await this._loadCpu();
      this.state = "ready";
      this.emit("Ready", 1);
    } catch (e) {
      this.state = "error";
      this.label = String(e.message || e);
      this.emit(this.label, 0);
      throw e;
    }
  }

  async _loadRemote(engine, model) {
    if (!model) throw new Error("no model chosen in Settings");
    const r = await fetch("/api/llm/models");
    if (!r.ok) throw new Error(await problem(r));
    const { models } = await r.json();
    if (!models.includes(model)) throw new Error(`the server has no model called "${model}"`);
    this.backend = "remote";
    this.engineName = EXTERNAL_ENGINES[engine];
    this.label = `${this.engineName} · ${model}`;
  }

  async _loadCpu() {
    if (!this.bundled?.onnx) throw new Error("The CPU model is not bundled (run: py fetch_models.py).");
    this.emit("Loading the CPU model…", 0);
    await this._loadOnnx();
    this.label = `CPU · ${this.threads} thread${this.threads > 1 ? "s" : ""}`;
  }

  async _unloadWebLLM() {
    try { await this.engine?.unload(); } catch { /* already gone */ }
    try { this.worker?.terminate(); } catch { /* ignore */ }
    this.engine = null;
    this.worker = null;
  }

  async _loadWebLLM(build) {
    const webllm = await import("/static/vendor/web-llm/index.js");
    const appConfig = {
      model_list: [{
        model: `${ORIGIN}/models/webllm/${build.model_id}/resolve/main/`,
        model_id: build.model_id,
        model_lib: `${ORIGIN}/models/webllm/libs/${build.lib}`,
        low_resource_required: true,
        overrides: { context_window_size: 4096 },
      }],
    };
    const worker = new Worker("/static/js/llm-worker.js", { type: "module" });
    this.worker = worker;
    this.engine = await webllm.CreateWebWorkerMLCEngine(worker, build.model_id, {
      appConfig,
      initProgressCallback: (r) => this.emit(r.text, r.progress),
    });
    this.backend = "webllm";
  }

  async _loadOnnx() {
    const T = await import("/static/vendor/transformers/transformers.min.js");
    T.env.allowRemoteModels = false;
    T.env.allowLocalModels = true;
    T.env.useBrowserCache = false; // the files are already served from this computer; no second copy in the browser
    // Must be a relative path: transformers.js only probes local files for non-URL paths.
    T.env.localModelPath = "/models/onnx/";
    T.env.backends.onnx.wasm.wasmPaths = `${ORIGIN}/static/vendor/ort/`;
    // Several threads need cross-origin isolation (the server sends COOP/COEP headers); otherwise one thread.
    // The model runs on this page's thread: its multi-threaded runtime cannot start its threads inside a worker.
    this.threads = self.crossOriginIsolated ? Math.max(1, Math.min(8, (navigator.hardwareConcurrency || 4) - 2)) : 1;
    T.env.backends.onnx.wasm.numThreads = this.threads;
    const id = "Qwen2.5-0.5B-Instruct";
    let shown = -1;
    const progress = (p) => {
      const pct = Math.round(p.progress || 0);
      if (p.status === "progress" && p.total && pct !== shown) { shown = pct; this.emit(`Loading ${p.file} ${pct}%`, pct / 100); }
    };
    this.T = T;
    this.tokenizer = await T.AutoTokenizer.from_pretrained(id, { progress_callback: progress });
    this.model = await T.AutoModelForCausalLM.from_pretrained(id, { dtype: "q8", device: "wasm", progress_callback: progress });
    // Hand control back to the browser before every step so the live view repaints between words.
    const forward = this.model.forward.bind(this.model);
    this.model.forward = async (...args) => { await nextTask(); return forward(...args); };
    this.backend = "onnx";
  }

  /** Stream text deltas for a chat. A GPU failure before the first word switches to the CPU model and retries. */
  async *stream(messages, opts = {}) {
    if (this.state !== "ready") throw new Error("Model not loaded");
    if (this.backend === "remote") {
      let yielded = false;
      try {
        for await (const d of this._streamRemote(messages, opts)) { yielded = true; yield d; }
        return;
      } catch (e) {
        if (yielded) throw e;
        console.warn("External model failed, switching to the bundled one:", e);
        const note = `${this.engineName} stopped working (${String(e.message || e).slice(0, 160)}); using the bundled model`;
        this.state = "loading";
        this.backend = null;
        await this._load(this.bundled, "auto", "");
        this.gpuNote = note;
      }
    }
    if (this.backend === "webllm") {
      let yielded = false;
      try {
        for await (const d of this._streamWebLLM(messages, opts)) { yielded = true; yield d; }
        return;
      } catch (e) {
        if (yielded) throw e;
        console.warn("GPU generation failed, switching to the CPU model:", e);
        this.state = "loading";
        this.gpuNote = `GPU stopped working (${String(e.message || e).slice(0, 120)})`;
        await this._unloadWebLLM();
        await this._loadCpu();
        this.state = "ready";
        this.emit("Ready", 1);
      }
    }
    yield* this._streamOnnx(messages, opts);
  }

  async *_streamRemote(messages, { temperature = 0.2, max_tokens = 160 }) {
    this.abort = new AbortController();
    try {
      const r = await fetch("/api/llm/chat", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: this.abort.signal,
        body: JSON.stringify({ messages, temperature, max_tokens }),
      });
      if (!r.ok) throw new Error(await problem(r));
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const text = decoder.decode(value, { stream: true });
        if (text) yield text;
      }
    } catch (e) {
      if (e.name === "AbortError") return; // Stop was pressed: keep what arrived
      throw e;
    }
  }

  async *_streamWebLLM(messages, { temperature = 0.2, max_tokens = 160 }) {
    const chunks = await this.engine.chat.completions.create({
      messages, stream: true, temperature, top_p: 0.9, max_tokens, frequency_penalty: 0.2,
    });
    for await (const c of chunks) {
      const d = c.choices?.[0]?.delta?.content;
      if (d) yield d;
    }
  }

  async *_streamOnnx(messages, { temperature = 0.2, max_tokens = 160 }) {
    const T = this.T;
    const inputs = this.tokenizer.apply_chat_template(messages, { add_generation_prompt: true, return_dict: true });
    const queue = new AsyncQueue();
    this.stopper = new T.InterruptableStoppingCriteria();
    const streamer = new T.TextStreamer(this.tokenizer, {
      skip_prompt: true, skip_special_tokens: true, callback_function: (t) => queue.push(t),
    });
    const run = this.model.generate({
      // Greedy decoding at low temperatures: the 8-bit CPU build drifts more than the GPU build when sampling.
      ...inputs, max_new_tokens: max_tokens, do_sample: temperature > 0.5, temperature: Math.max(temperature, 0.01),
      top_p: 0.9, repetition_penalty: 1.1, streamer, stopping_criteria: this.stopper,
    }).catch((e) => console.warn(e)).finally(() => queue.close());
    for await (const t of queue) if (t) yield t;
    await run;
  }

  interrupt() {
    if (this.backend === "remote") this.abort?.abort();
    else if (this.backend === "webllm") this.engine?.interruptGenerate();
    else this.stopper?.interrupt();
  }
}
