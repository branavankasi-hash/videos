"""Search Wikimedia Commons for real reference photos and build a numbered contact sheet.

Usage: python pipeline/find_references.py "search terms" out/refs
Writes out/refs/candidates.json and out/refs/contact_sheet.jpg (numbered thumbnails).
"""
import json
import os
import subprocess
import sys

import requests

UA = {"User-Agent": "videos-pipeline/1.0 (github.com/branavankasi-hash/videos)"}
API = "https://commons.wikimedia.org/w/api.php"


def search(query, limit=40):
    params = {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo",
        "iiprop": "url|size|extmetadata", "iiurlwidth": 1920,
    }
    r = requests.get(API, params=params, headers=UA, timeout=60)
    r.raise_for_status()
    pages = sorted(r.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        ii = p["imageinfo"][0]
        w, h = ii["width"], ii["height"]
        meta = ii.get("extmetadata", {})
        out.append({
            "title": p["title"], "width": w, "height": h,
            "url": ii.get("thumburl") or ii["url"],
            "license": meta.get("LicenseShortName", {}).get("value", ""),
            "artist": meta.get("Artist", {}).get("value", ""),
        })
    return out


def main():
    queries = sys.argv[1].split("|")
    out_dir = sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    cands, seen = [], set()
    for q in queries:
        for c in search(q.strip()):
            if c["title"] in seen or c["width"] < 1200:
                continue
            seen.add(c["title"])
            cands.append(c)
    thumbs = []
    for i, c in enumerate(cands[:48]):
        c["n"] = i
        path = os.path.join(out_dir, f"t{i:02d}.jpg")
        try:
            img = requests.get(c["url"], headers=UA, timeout=60)
            img.raise_for_status()
            open(path + ".src", "wb").write(img.content)
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-i", path + ".src", "-vf",
                "scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:(ow-iw)/2:(oh-ih)/2,"
                f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='{i}':x=10:y=10:"
                "fontsize=40:fontcolor=yellow:box=1:boxcolor=black@0.7",
                "-frames:v", "1", path], check=True)
            os.remove(path + ".src")
            thumbs.append(path)
        except Exception as e:  # noqa: BLE001
            print("skip", c["title"], e)
    json.dump(cands[:48], open(os.path.join(out_dir, "candidates.json"), "w"), indent=2)
    # 6 x N grid
    while len(thumbs) % 6:
        blank = os.path.join(out_dir, f"blank{len(thumbs)}.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=480x270", "-frames:v", "1", blank], check=True)
        thumbs.append(blank)
    args = []
    for t in thumbs:
        args += ["-i", t]
    rows = len(thumbs) // 6
    layout = "|".join(f"{c * 480}_{r * 270}" for r in range(rows) for c in range(6))
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, "-filter_complex",
                    f"xstack=inputs={len(thumbs)}:layout={layout}", "-frames:v", "1", "-q:v", "3",
                    os.path.join(out_dir, "contact_sheet.jpg")], check=True)


if __name__ == "__main__":
    main()
