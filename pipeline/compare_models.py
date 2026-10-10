"""Generate one scene with several AI video models so they can be compared side by side.

Usage: python pipeline/compare_models.py scenes/rajaraja_test.json out/

Reads API keys from env: GEMINI_API_KEY, ATLAS_API_KEY, ELEVENLABS_API_KEY.
Each model runs independently; one failing does not stop the others.
"""
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests

POLL_EVERY = 10
TIMEOUT = 20 * 60

GEMINI = "https://generativelanguage.googleapis.com/v1beta"
ATLAS = "https://api.atlascloud.ai/api/v1/model"


def log(name, msg):
    print(f"[{name}] {msg}", flush=True)


def download(url, path, headers=None):
    with requests.get(url, headers=headers or {}, stream=True, timeout=300, allow_redirects=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path


# ---------------- Google Veo 3.1 ----------------
VEO_MODELS = ["veo-3.1-generate-preview", "veo-3.1-generate-001"]


def run_veo(scene, out_dir):
    name = "veo"
    key = os.environ["GEMINI_API_KEY"]
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    params = {
        "aspectRatio": scene["aspect_ratio"],
        "resolution": "1080p",
        "durationSeconds": scene["duration"],
        "personGeneration": "allow_all",
    }
    last_err = None
    op = None
    for model in VEO_MODELS:
        for dur in (scene["duration"], str(scene["duration"])):
            params["durationSeconds"] = dur
            body = {"instances": [{"prompt": scene["prompt"]}], "parameters": params}
            r = requests.post(f"{GEMINI}/models/{model}:predictLongRunning", headers=headers, json=body, timeout=60)
            if r.ok:
                op = r.json()["name"]
                log(name, f"started {model}: {op}")
                break
            last_err = f"{model} HTTP {r.status_code}: {r.text[:500]}"
            log(name, last_err)
            if r.status_code not in (400, 404):
                break
        if op:
            break
    if not op:
        raise RuntimeError(last_err)

    start = time.time()
    while True:
        time.sleep(POLL_EVERY)
        r = requests.get(f"{GEMINI}/{op}", headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("done"):
            break
        if time.time() - start > TIMEOUT:
            raise TimeoutError("Veo timed out")
        log(name, "still rendering...")
    if "error" in data:
        raise RuntimeError(json.dumps(data["error"])[:800])
    resp = data.get("response", {}).get("generateVideoResponse", {})
    samples = resp.get("generatedSamples") or []
    if not samples:
        raise RuntimeError("No video returned (possibly filtered): " + json.dumps(resp)[:800])
    uri = samples[0]["video"]["uri"]
    path = os.path.join(out_dir, "1_veo31.mp4")
    download(uri, path, {"x-goog-api-key": key})
    return path


# ---------------- Atlas Cloud (Seedance / Kling) ----------------
def run_atlas(name, filename, body, out_dir):
    key = os.environ["ATLAS_API_KEY"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = requests.post(f"{ATLAS}/generateVideo", headers=headers, json=body, timeout=60)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:800]}")
    pid = r.json()["data"]["id"]
    log(name, f"started {body['model']}: {pid}")
    start = time.time()
    while True:
        time.sleep(POLL_EVERY)
        r = requests.get(f"{ATLAS}/prediction/{pid}", headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()["data"]
        status = (data.get("status") or "").lower()
        if status in ("completed", "succeeded", "success"):
            break
        if status in ("failed", "error", "canceled", "cancelled"):
            raise RuntimeError(f"{status}: {data.get('error')}")
        if time.time() - start > TIMEOUT:
            raise TimeoutError(f"{name} timed out")
        log(name, f"status={status}")
    outputs = data.get("outputs") or data.get("output") or []
    url = outputs[0] if isinstance(outputs, list) else outputs
    if isinstance(url, dict):
        url = url.get("url") or url.get("video")
    path = os.path.join(out_dir, filename)
    download(url, path)
    return path


def run_seedance(scene, out_dir):
    body = {
        "model": "bytedance/seedance-2.0/text-to-video",
        "prompt": scene["prompt"],
        "duration": scene["duration"],
        "resolution": "1080p",
        "ratio": scene["aspect_ratio"],
        "generate_audio": True,
        "watermark": False,
    }
    return run_atlas("seedance", "2_seedance20.mp4", body, out_dir)


def run_kling(scene, out_dir):
    body = {
        "model": "kwaivgi/kling-v3.0-pro/text-to-video",
        "prompt": scene["prompt"],
        "negative_prompt": scene.get("negative_prompt", ""),
        "duration": scene["duration"],
        "aspect_ratio": scene["aspect_ratio"],
        "cfg_scale": 0.5,
        "sound": True,
    }
    return run_atlas("kling", "3_kling30pro.mp4", body, out_dir)


# ---------------- ElevenLabs narration ----------------
VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # "George" - warm documentary narrator (premade voice)


def run_narration(scene, out_dir):
    key = os.environ["ELEVENLABS_API_KEY"]
    last = None
    for model in ("eleven_v3", "eleven_multilingual_v2"):
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": scene["narration"], "model_id": model},
            timeout=120,
        )
        if r.ok:
            path = os.path.join(out_dir, "4_narration.mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            log("narration", f"done with {model}")
            return path
        last = f"{model} HTTP {r.status_code}: {r.text[:400]}"
        log("narration", last)
    raise RuntimeError(last)


def main():
    scene = json.load(open(sys.argv[1]))
    out_dir = sys.argv[2]
    only = [m.strip() for m in os.environ.get("MODELS", "veo,seedance,kling,narration").split(",") if m.strip()]
    os.makedirs(out_dir, exist_ok=True)
    jobs = {"veo": run_veo, "seedance": run_seedance, "kling": run_kling, "narration": run_narration}
    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {n: ex.submit(jobs[n], scene, out_dir) for n in only if n in jobs}
        for n, fut in futures.items():
            try:
                results[n] = {"ok": True, "file": fut.result()}
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                results[n] = {"ok": False, "error": str(e)[:1000]}
    json.dump(results, open(os.path.join(out_dir, "results.json"), "w"), indent=2)
    print(json.dumps(results, indent=2))
    if not any(r["ok"] for k, r in results.items() if k != "narration"):
        sys.exit(1)


if __name__ == "__main__":
    main()
