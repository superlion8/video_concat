import os
import subprocess
import uuid
import shutil
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests

app = FastAPI()

# Enable CORS just in case
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Base directory for storing generated files
BASE_DIR = Path("/tmp/video_concat_files")
BASE_DIR.mkdir(parents=True, exist_ok=True)

# Mount the static directory to serve files
app.mount("/files", StaticFiles(directory=BASE_DIR), name="files")

class ConcatReq(BaseModel):
    urls: list[str]

def download(url: str, path: str):
    try:
        with requests.get(url, stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to download video: {url}")

@app.post("/concat")
def concat(req: ConcatReq, request: Request):
    if not req.urls:
        raise HTTPException(status_code=400, detail="No URLs provided")
    
    # Generate unique ID for this request
    req_id = uuid.uuid4().hex
    workdir = BASE_DIR / req_id
    workdir.mkdir(parents=True, exist_ok=True)

    local_files = []
    # Limit to 50 files to prevent abuse/timeouts
    for i, url in enumerate(req.urls[:50]):
        # Guess extension or default to .mp4
        ext = ".mp4" 
        if "." in url.split("/")[-1] and len(url.split("/")[-1].split(".")[-1]) < 5:
             ext = "." + url.split("/")[-1].split(".")[-1]
        
        fp = workdir / f"in_{i}{ext}"
        download(url, str(fp))
        local_files.append(fp)

    out_file = workdir / "out.mp4"

    # Step 1: Preprocess each video to ensure consistent format
    # - Add silent audio if missing
    # - Normalize to 1280x720, 30fps
    preprocessed = []
    for i, fp in enumerate(local_files):
        prep_file = workdir / f"prep_{i}.mp4"
        # Use a filter that:
        # 1. Scales video to 1280x720 (pad to keep aspect ratio)
        # 2. Sets fps to 30
        # 3. Adds silent audio if no audio stream exists
        prep_cmd = [
            "ffmpeg", "-y", "-i", str(fp),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v];"
            "[0:a]aresample=44100[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            str(prep_file)
        ]
        print(f"Preprocessing video {i}: {' '.join(prep_cmd)}")
        p = subprocess.run(prep_cmd, capture_output=True, text=True)
        
        # If preprocessing failed (likely no audio), try without audio mapping
        if p.returncode != 0:
            print(f"First attempt failed, trying with generated silent audio: {p.stderr[-200:]}")
            prep_cmd2 = [
                "ffmpeg", "-y", "-i", str(fp),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-filter_complex",
                "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v]",
                "-map", "[v]", "-map", "1:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                str(prep_file)
            ]
            p2 = subprocess.run(prep_cmd2, capture_output=True, text=True)
            if p2.returncode != 0:
                print(f"Preprocessing failed for {fp}: {p2.stderr}")
                raise HTTPException(status_code=500, detail=f"Failed to preprocess video {i}: {p2.stderr[-300:]}")
        
        preprocessed.append(prep_file)

    # Step 2: Create list file for concat demuxer
    list_txt = workdir / "list.txt"
    with open(list_txt, "w") as f:
        for fp in preprocessed:
            f.write(f"file '{fp.absolute()}'\n")

    # Step 3: Concat using demuxer (now all files have same format)
    concat_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_txt),
        "-c", "copy",
        str(out_file)
    ]
    print(f"Running concat: {' '.join(concat_cmd)}")
    p = subprocess.run(concat_cmd, capture_output=True, text=True)
    
    if p.returncode != 0:
        print(f"Concat failed: {p.stderr}")
        raise HTTPException(status_code=500, detail=f"Concat failed: {p.stderr[-500:]}")

    # Construct the download URL
    # request.base_url usually ends with /
    download_url = f"{request.base_url}files/{req_id}/out.mp4"
    
    return {
        "code": 0,
        "msg": "success",
        "url": download_url
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)