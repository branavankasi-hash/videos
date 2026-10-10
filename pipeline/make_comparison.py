"""Join the generated clips into one labelled comparison video (Veo -> Seedance -> Kling)."""
import json
import os
import subprocess
import sys

LABELS = {
    "1_veo31.mp4": "Google Veo 3.1",
    "2_seedance20.mp4": "Seedance 2.0",
    "3_kling30pro.mp4": "Kling 3.0 Pro",
}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def has_audio(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", path],
        capture_output=True, text=True,
    ).stdout
    return bool(json.loads(out or "{}").get("streams"))


def main(out_dir):
    parts = []
    for fname, label in LABELS.items():
        src = os.path.join(out_dir, fname)
        if not os.path.exists(src):
            continue
        norm = os.path.join(out_dir, f"norm_{fname}")
        vf = (
            "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=24,"
            f"drawtext=fontfile={FONT}:text='{label}':x=40:y=40:fontsize=44:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=14"
        )
        cmd = ["ffmpeg", "-y", "-i", src]
        if not has_audio(src):
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
        cmd += ["-vf", vf, "-map", "0:v:0", "-map", "1:a:0" if not has_audio(src) else "0:a:0",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", norm]
        subprocess.run(cmd, check=True)
        parts.append(norm)
    if len(parts) < 2:
        print("Not enough clips for a comparison video.")
        return
    lst = os.path.join(out_dir, "list.txt")
    with open(lst, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                    os.path.join(out_dir, "0_COMPARISON_all_models.mp4")], check=True)
    for p in parts:
        os.remove(p)
    os.remove(lst)


if __name__ == "__main__":
    main(sys.argv[1])
