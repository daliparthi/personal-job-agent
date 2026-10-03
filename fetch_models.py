"""
One-time bundler: downloads Qwen2.5-0.5B-Instruct and the browser runtime libraries
into this folder so the app never pulls the model from the internet at runtime.

    py fetch_models.py                 # CPU model (ONNX, ~520 MB) + GPU builds (WebLLM q4f16 + q4f32, ~580 MB)
    py fetch_models.py --webllm q4f16  # only the f16 GPU build (~290 MB)
    py fetch_models.py --webllm        # CPU model only, no GPU builds
    py fetch_models.py --no-onnx       # GPU builds only (not recommended: the CPU model is the default engine)

Everything lands in ./models and ./static/vendor and is served by the local app.
Re-running is safe: files already present with the right size are skipped.
"""
import argparse
import io
import json
import ssl
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
VENDOR = ROOT / "static" / "vendor"

WEBLLM_VERSION = "0.2.85"
WEBLLM_LIB_PREFIX = "https://raw.githubusercontent.com/mlc-ai/binary-mlc-llm-libs/main/web-llm-models/v0_2_84/base/"
WEBLLM_BUILDS = {
    "q4f16": ("mlc-ai/Qwen2.5-0.5B-Instruct-q4f16_1-MLC", "Qwen2-0.5B-Instruct-q4f16_1_cs1k-webgpu.wasm"),
    "q4f32": ("mlc-ai/Qwen2.5-0.5B-Instruct-q4f32_1-MLC", "Qwen2-0.5B-Instruct-q4f32_1_cs1k-webgpu.wasm"),
}

TRANSFORMERS_VERSION = "4.3.0"
ONNX_REPO = "onnx-community/Qwen2.5-0.5B-Instruct"
ONNX_FILES = [
    "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "added_tokens.json", "vocab.json", "merges.txt",
    "onnx/model_quantized.onnx",  # int8, runs on CPU (WASM) and WebGPU
]

UA = {"User-Agent": "job-agent-fetch/1.0"}


def _ssl_context():
    # python.org builds on macOS ship without root certificates; certifi (installed with httpx) has them.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL = _ssl_context()


def _get(url, timeout=120):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout, context=SSL)


def download(url, dest: Path, expected_size=None):
    if dest.exists() and (expected_size is None or dest.stat().st_size == expected_size):
        print(f"  ok   {dest.relative_to(ROOT)}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _get(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else 0
            if pct // 10 != last:
                last = pct // 10
                print(f"  ...  {dest.name} {done / 1e6:.0f}/{total / 1e6:.0f} MB", flush=True)
    tmp.replace(dest)
    print(f"  got  {dest.relative_to(ROOT)}")


def hf_tree(repo, path=""):
    url = f"https://huggingface.co/api/models/{repo}/tree/main" + (f"/{path}" if path else "")
    with _get(url) as r:
        return json.load(r)


def fetch_webllm(builds):
    for key in builds:
        repo, lib = WEBLLM_BUILDS[key]
        name = repo.split("/")[1]
        print(f"[webllm] {name}")
        for item in hf_tree(repo):
            if item["type"] != "file" or item["path"] in (".gitattributes", "README.md"):
                continue
            download(f"https://huggingface.co/{repo}/resolve/main/{item['path']}",
                     MODELS / "webllm" / name / item["path"], item.get("size"))
        download(WEBLLM_LIB_PREFIX + lib, MODELS / "webllm" / "libs" / lib)


def npm_extract(pkg, version, members, dest_dir: Path):
    """Pull files out of an npm tarball without needing npm installed."""
    if all((dest_dir / Path(m).name).exists() for m in members):
        print(f"  ok   {pkg}@{version}")
        return
    short = pkg.split("/")[-1]
    with _get(f"https://registry.npmjs.org/{pkg}/-/{short}-{version}.tgz") as r:
        data = r.read()
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in members:
            src = tar.extractfile(f"package/{m}")
            (dest_dir / Path(m).name).write_bytes(src.read())
            print(f"  got  {(dest_dir / Path(m).name).relative_to(ROOT)}")


def npm_dep_version(pkg, version, dep):
    with _get(f"https://registry.npmjs.org/{pkg}/{version}") as r:
        spec = json.load(r)["dependencies"][dep]
    return spec.lstrip("^~=")


def fetch_onnx():
    print(f"[onnx] {ONNX_REPO}")
    sizes = {i["path"]: i.get("size") for i in hf_tree(ONNX_REPO) + hf_tree(ONNX_REPO, "onnx")}
    for f in ONNX_FILES:
        dest = MODELS / "onnx" / "Qwen2.5-0.5B-Instruct" / f
        if dest.with_name(dest.name + ".part1").exists():  # the repository stores it in parts; the app joins them
            print(f"  ok   {dest.relative_to(ROOT)} (in parts)")
            continue
        download(f"https://huggingface.co/{ONNX_REPO}/resolve/main/{f}", dest, sizes.get(f))
    print("[vendor] transformers.js + onnxruntime-web")
    npm_extract("@huggingface/transformers", TRANSFORMERS_VERSION,
                ["dist/transformers.min.js"], VENDOR / "transformers")
    ort = npm_dep_version("@huggingface/transformers", TRANSFORMERS_VERSION, "onnxruntime-web")
    npm_extract("onnxruntime-web", ort,
                ["dist/ort-wasm-simd-threaded.asyncify.mjs", "dist/ort-wasm-simd-threaded.asyncify.wasm"],
                VENDOR / "ort")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--webllm", nargs="*", choices=list(WEBLLM_BUILDS), default=list(WEBLLM_BUILDS),
                    help="which WebLLM (GPU) builds to bundle (default: both; none with an empty list)")
    ap.add_argument("--onnx", action="store_true", help=argparse.SUPPRESS)  # always on now; kept for old commands
    ap.add_argument("--no-onnx", action="store_true", help="skip the ONNX CPU model (the default engine)")
    args = ap.parse_args()

    print("[vendor] web-llm")
    npm_extract("@mlc-ai/web-llm", WEBLLM_VERSION, ["lib/index.js"], VENDOR / "web-llm")
    fetch_webllm(args.webllm)
    if not args.no_onnx:
        fetch_onnx()
    print("Done. Models are in ./models and libraries in ./static/vendor")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
