"""Render a full episode: narration -> matched-length clips -> assembled MP4 + subtitles.

Usage: python pipeline/render_episode.py episodes/ep01_stolen_king.json out/ep01

Env: GEMINI_API_KEY (unused for Kling), ATLAS_API_KEY, ELEVENLABS_API_KEY, GH_TOKEN, GITHUB_REPOSITORY,
     ONLY_SHOTS (optional comma list, e.g. "s01,s05") and SKIP_MUSIC=1.
Re-running reuses any clip/narration already present in the output folder.
"""
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(__file__))
from compare_models import run_atlas  # noqa: E402

UA = {"User-Agent": "videos-pipeline/1.0 (github.com/branavankasi-hash/videos)"}
EL = "https://api.elevenlabs.io/v1"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
W, H, FPS = 1920, 1080, 24
LEAD = 0.5   # seconds of picture before narration starts in each shot
TAIL = 0.8   # seconds of picture after narration ends


def sh(*cmd):
    subprocess.run(cmd, check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def has_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    return bool(out.strip())


def fill(text, chars):
    return re.sub(r"\{(\w+)\}", lambda m: chars.get(m.group(1), m.group(0)), text)


# ---------------- narration ----------------
def pick_tts_model(key):
    prefs = ["eleven_v4", "eleven_v3", "eleven_multilingual_v2"]
    try:
        r = requests.get(f"{EL}/models", headers={"xi-api-key": key}, timeout=30)
        ids = {m["model_id"] for m in r.json() if m.get("can_do_text_to_speech")}
        print("ElevenLabs TTS models:", sorted(ids))
        for p in prefs:
            if p in ids:
                return p
    except Exception as e:  # noqa: BLE001
        print("model list failed", e)
    return "eleven_multilingual_v2"


def narrate(ep, out):
    key = os.environ["ELEVENLABS_API_KEY"]
    model = pick_tts_model(key)
    print("Using TTS model", model)
    shots = ep["shots"]
    new = []
    for i, s in enumerate(shots):
        path = os.path.join(out, f"{s['id']}_vo.mp3")
        if os.path.exists(path):
            continue
        new.append(path)
        body = {
            "text": s["narration"], "model_id": model,
            "previous_text": shots[i - 1]["narration"] if i else None,
            "next_text": shots[i + 1]["narration"] if i + 1 < len(shots) else None,
        }
        for attempt in range(3):
            r = requests.post(f"{EL}/text-to-speech/{ep['voice_id']}?output_format=mp3_44100_128",
                              headers={"xi-api-key": key, "Content-Type": "application/json"}, json=body, timeout=180)
            if r.ok:
                open(path, "wb").write(r.content)
                break
            print(s["id"], "TTS", r.status_code, r.text[:300])
            if r.status_code == 400:
                body.pop("previous_text", None)
                body.pop("next_text", None)
            time.sleep(3)
        else:
            raise RuntimeError(f"narration failed for {s['id']}")
        print(s["id"], "narration", round(duration(path), 2), "s")
    return new


# ---------------- reference images (re-hosted on GitHub so the video API can fetch them) ----------------
def host_references(ep, out):
    refs = ep.get("references", {})
    if not refs:
        return {}
    repo = os.environ["GITHUB_REPOSITORY"]
    tag = f"refs-{ep['id']}"
    if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode != 0:
        sh("gh", "release", "create", tag, "--title", f"Reference images {ep['id']}", "--notes", "Start frames",
           "--prerelease")
    hosted = {}
    for name, url in refs.items():
        path = os.path.join(out, f"ref_{name}.jpg")
        for attempt in range(5):
            r = requests.get(url, headers=UA, timeout=60)
            if r.ok:
                break
            print("reference", name, r.status_code, r.text[:200])
            time.sleep(5 * (attempt + 1))
        r.raise_for_status()
        open(path, "wb").write(r.content)
        sh("gh", "release", "upload", tag, path, "--clobber")
        hosted[name] = f"https://github.com/{repo}/releases/download/{tag}/ref_{name}.jpg"
    return hosted


# ---------------- clips ----------------
def make_clip(ep, s, target, refs, out):
    path = os.path.join(out, f"{s['id']}_clip.mp4")
    if os.path.exists(path):
        return path
    if s.get("reuse"):
        r = requests.get(s["reuse"], timeout=300)
        r.raise_for_status()
        open(path, "wb").write(r.content)
        return path
    dur = max(3, min(15, math.ceil(target)))
    prompt = fill(s["prompt"], ep["characters"]) + " " + ep["style"]
    common = {"prompt": prompt[:2500], "negative_prompt": ep["negative_prompt"], "duration": dur,
              "cfg_scale": 0.5, "sound": True}
    if s.get("reference"):
        body = {"model": "kwaivgi/kling-v3.0-pro/image-to-video", "image": refs[s["reference"]], **common}
    else:
        body = {"model": "kwaivgi/kling-v3.0-pro/text-to-video", "aspect_ratio": ep["aspect_ratio"], **common}
    tmp = run_atlas(s["id"], f"{s['id']}_clip.tmp.mp4", body, out)
    os.replace(tmp, path)
    return path


# ---------------- assembly ----------------
def build_shot(ep, s, out):
    clip = os.path.join(out, f"{s['id']}_clip.mp4")
    vo = os.path.join(out, f"{s['id']}_vo.mp3")
    vo_len = duration(vo)
    target = round(vo_len + LEAD + TAIL, 2)
    clip_len = duration(clip)
    slow = max(1.0, target / clip_len)  # stretch gently if the clip is shorter than the narration
    vf = (f"setpts={slow:.4f}*PTS,scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},"
          f"trim=duration={target},setpts=PTS-STARTPTS")
    if s.get("title_card"):
        vf += (f",drawtext=fontfile={FONT}:text='{s['title_card']}':x=(w-text_w)/2:y=h*0.72:fontsize=110:"
               "fontcolor=white:shadowcolor=black@0.8:shadowx=4:shadowy=4:"
               f"alpha='if(lt(t,1),0,if(lt(t,2),t-1,if(lt(t,{target - 1.5}),1,max(0,({target}-t)/1.5))))'")
    if s is ep["shots"][0]:
        vf += ",fade=t=in:st=0:d=1"
    if s is ep["shots"][-1]:
        vf += f",fade=t=out:st={target - 2}:d=2"
    seg = os.path.join(out, f"{s['id']}_seg.mp4")
    amb_src = "[0:a]aresample=44100,volume=0.22," if has_audio(clip) else "anullsrc=r=44100:cl=stereo,"
    delay = int(LEAD * 1000)
    filt = (f"[0:v]{vf}[v];"
            f"{amb_src}apad,atrim=duration={target}[amb];"
            f"[1:a]aresample=44100,adelay={delay}|{delay},apad,atrim=duration={target}[vo];"
            f"[amb][vo]amix=inputs=2:duration=first:normalize=0[a]")
    sh("ffmpeg", "-v", "error", "-y", "-i", clip, "-i", vo, "-filter_complex", filt, "-map", "[v]", "-map", "[a]",
       "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", seg)
    return seg, target, vo_len


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def music_bed(ep, total, out):
    path = os.path.join(out, "music.mp3")
    if os.path.exists(path):
        return path
    key = os.environ["ELEVENLABS_API_KEY"]
    body = {
        "prompt": ("Cinematic historical documentary score, South Indian classical instruments: veena, "
                   "nadaswaram, mridangam, tanpura drone, with soft strings and low choir. Starts mysterious and "
                   "tense, builds to majestic in the middle, emotional and triumphant at the end. Instrumental."),
        "music_length_ms": int(min(600, total + 2) * 1000),
        "force_instrumental": True,
    }
    r = requests.post(f"{EL}/music", headers={"xi-api-key": key, "Content-Type": "application/json"},
                      json=body, timeout=600)
    if not r.ok:
        print("music failed", r.status_code, r.text[:300])
        return None
    open(path, "wb").write(r.content)
    save_work(ep, path)
    return path


def work_tag(ep):
    return f"work-{ep['id']}"


def restore_work(ep, out):
    """Download clips/narration saved by earlier runs, so paid-for clips are never generated twice."""
    tag = work_tag(ep)
    if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode != 0:
        sh("gh", "release", "create", tag, "--title", f"Work files {ep['id']}", "--notes",
           "Generated clips and narration kept between runs. Delete a file here to regenerate that shot.",
           "--prerelease")
        return
    subprocess.run(["gh", "release", "download", tag, "--dir", out, "--skip-existing"], check=False)


def save_work(ep, path):
    subprocess.run(["gh", "release", "upload", work_tag(ep), path, "--clobber"], check=False)


def main():
    ep = json.load(open(sys.argv[1]))
    out = sys.argv[2]
    os.makedirs(out, exist_ok=True)
    only = {x.strip() for x in os.environ.get("ONLY_SHOTS", "").split(",") if x.strip()}
    restore_work(ep, out)
    for sid in {x.strip() for x in os.environ.get("REDO_SHOTS", "").split(",") if x.strip()}:
        p = os.path.join(out, f"{sid}_clip.mp4")
        if os.path.exists(p):
            os.remove(p)
    for path in narrate(ep, out):
        save_work(ep, path)
    refs = host_references(ep, out)

    shots = [s for s in ep["shots"] if not only or s["id"] in only]
    errors = {}

    def job(s):
        target = duration(os.path.join(out, f"{s['id']}_vo.mp3")) + LEAD + TAIL
        existed = os.path.exists(os.path.join(out, f"{s['id']}_clip.mp4"))
        path = make_clip(ep, s, target, refs, out)
        if not existed:
            save_work(ep, path)
        return path

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {s["id"]: ex.submit(job, s) for s in shots}
        for sid, f in futs.items():
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                errors[sid] = str(e)[:500]
    if errors:
        json.dump(errors, open(os.path.join(out, "errors.json"), "w"), indent=2)
        print("Clip errors:", errors)
        sys.exit(1)

    if only:
        print("Partial run finished (clips only).")
        return

    segs, srt, t = [], [], 0.0
    for i, s in enumerate(ep["shots"], 1):
        seg, length, vo_len = build_shot(ep, s, out)
        segs.append(seg)
        srt.append(f"{i}\n{srt_time(t + LEAD)} --> {srt_time(t + LEAD + vo_len)}\n{s['narration']}\n")
        t += length
    open(os.path.join(out, f"{ep['id']}.srt"), "w").write("\n".join(srt))

    lst = os.path.join(out, "segments.txt")
    open(lst, "w").write("".join(f"file '{os.path.abspath(p)}'\n" for p in segs))
    joined = os.path.join(out, "joined.mp4")
    sh("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined)

    final = os.path.join(out, f"{ep['id']}.mp4")
    music = None if os.environ.get("SKIP_MUSIC") else music_bed(ep, t, out)
    if music:
        sh("ffmpeg", "-v", "error", "-y", "-i", joined, "-stream_loop", "-1", "-i", music, "-filter_complex",
           f"[1:a]volume=0.18,atrim=duration={t},afade=t=out:st={max(0, t - 4)}:d=4[m];"
           "[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
           "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", final)
    else:
        os.replace(joined, final)

    # YouTube-standard loudness (-14 LUFS)
    loud = final.replace(".mp4", ".loud.mp4")
    sh("ffmpeg", "-v", "error", "-y", "-i", final, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:v", "copy",
       "-c:a", "aac", "-b:a", "192k", loud)
    os.replace(loud, final)

    desc = [ep["title"], "", "Sources and image credits:"] + [f"- {c}" for c in ep.get("credits", [])]
    desc += ["", "This video uses AI-generated visuals and narration based on documented history."]
    open(os.path.join(out, "youtube_description.txt"), "w").write("\n".join(desc))
    print(f"Done: {final} ({t:.1f}s)")


if __name__ == "__main__":
    main()
