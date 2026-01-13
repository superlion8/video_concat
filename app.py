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
        
        # Log file size for debugging
        file_size = fp.stat().st_size
        print(f"Downloaded video {i}: {fp}, size: {file_size} bytes")
        
        if file_size < 1000:
            raise HTTPException(status_code=400, detail=f"Video {i} too small ({file_size} bytes), likely download failed")
        
        local_files.append(fp)

    out_file = workdir / "out.mp4"

    # Step 1: Preprocess each video to ensure consistent format
    # - Add silent audio if missing
    # - Keep original resolution, just normalize fps and pixel format
    preprocessed = []
    for i, fp in enumerate(local_files):
        prep_file = workdir / f"prep_{i}.mp4"
        
        # Check if video has audio stream using ffprobe
        probe_cmd = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(fp)]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        has_audio = "audio" in probe_result.stdout
        print(f"Video {i} has audio: {has_audio}")
        
        if has_audio:
            # Video has audio, simple re-encode
            prep_cmd = [
                "ffmpeg", "-y", "-i", str(fp),
                "-vf", "fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                str(prep_file)
            ]
        else:
            # No audio, add silent audio track
            prep_cmd = [
                "ffmpeg", "-y",
                "-i", str(fp),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", "fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                str(prep_file)
            ]
        
        print(f"Preprocessing video {i}: {' '.join(prep_cmd)}")
        p = subprocess.run(prep_cmd, capture_output=True, text=True)
        
        if p.returncode != 0:
            print(f"FFmpeg stderr: {p.stderr}")
            print(f"FFmpeg stdout: {p.stdout}")
            raise HTTPException(status_code=500, detail=f"Failed to preprocess video {i}: {p.stderr[-400:]}")
        
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