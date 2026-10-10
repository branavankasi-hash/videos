"""Make master still images (start frames) with Google's image model, guided by real reference photos.

Usage: python pipeline/make_stills.py episodes/ep01_stolen_king.json out/stills
Reads ep["stills"]: {name: {"prompt": "...", "style_refs": [url, ...]}} and writes out/stills/<name>.png.
Then uploads them to the release refs-<episode id> so the video model can use them as start frames.
"""
import base64
import json
import os
import subprocess
import sys

import requests

KEY = os.environ["GEMINI_API_KEY"]
API = "https://generativelanguage.googleapis.com/v1beta"
UA = {"User-Agent": "videos-pipeline/1.0 (github.com/branavankasi-hash/videos)"}
PREFERRED = ["gemini-3-pro-image-preview", "gemini-3-pro-image", "gemini-2.5-flash-image", "gemini-2.5-flash-image-preview"]


def pick_model():
    r = requests.get(f"{API}/models", params={"pageSize": 1000}, headers={"x-goog-api-key": KEY}, timeout=60)
    r.raise_for_status()
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    print("image-capable candidates:", [n for n in names if "image" in n])
    for p in PREFERRED:
        if p in names:
            return p
    imgs = [n for n in names if "image" in n]
    if not imgs:
        raise RuntimeError("No image model available on this key")
    return imgs[0]


def fetch(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return base64.b64encode(r.content).decode()


def generate(model, prompt, refs, path, out):
    parts = [{"text": prompt}]
    for u in refs:
        if u.startswith("still:"):
            data = base64.b64encode(open(os.path.join(out, f"still_{u[6:]}.png"), "rb").read()).decode()
            parts.append({"inline_data": {"mime_type": "image/png", "data": data}})
        else:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": fetch(u)}})
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}}}
    r = requests.post(f"{API}/models/{model}:generateContent", headers={"x-goog-api-key": KEY}, json=body, timeout=300)
    if not r.ok:
        print("error", r.status_code, r.text[:800])
        body["generationConfig"].pop("imageConfig")
        r = requests.post(f"{API}/models/{model}:generateContent", headers={"x-goog-api-key": KEY}, json=body, timeout=300)
    r.raise_for_status()
    for cand in r.json().get("candidates", []):
        for p in cand.get("content", {}).get("parts", []):
            data = p.get("inlineData") or p.get("inline_data")
            if data:
                open(path, "wb").write(base64.b64decode(data["data"]))
                return path
    raise RuntimeError("No image returned: " + r.text[:800])


def main():
    ep = json.load(open(sys.argv[1]))
    out = sys.argv[2]
    os.makedirs(out, exist_ok=True)
    only = {x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()}
    model = pick_model()
    print("Using", model)
    tag = f"refs-{ep['id']}"
    if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode != 0:
        subprocess.run(["gh", "release", "create", tag, "--title", f"Reference images {ep['id']}",
                        "--notes", "Start frames", "--prerelease"], check=True)
    for name, spec in ep.get("stills", {}).items():
        if only and name not in only:
            continue
        path = os.path.join(out, f"still_{name}.png")
        needs = [r[6:] for r in spec.get("style_refs", []) if r.startswith("still:")]
        for n in needs:
            if not os.path.exists(os.path.join(out, f"still_{n}.png")):
                subprocess.run(["gh", "release", "download", tag, "-p", f"still_{n}.png", "--dir", out], check=True)
        generate(model, spec["prompt"], spec.get("style_refs", []), path, out)
        subprocess.run(["gh", "release", "upload", tag, path, "--clobber"], check=True)
        print("made", path)


if __name__ == "__main__":
    main()
