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

    # Build ffmpeg command using concat filter (handles different formats properly)
    # -i for each input file
    input_args = []
    filter_parts = []
    for i, fp in enumerate(local_files):
        input_args.extend(["-i", str(fp)])
        filter_parts.append(f"[{i}:v:0][{i}:a:0]")
    
    # Concat filter: scale all to same resolution, then concat
    n = len(local_files)
    filter_complex = f"{''.join(filter_parts)}concat=n={n}:v=1:a=1[outv][outa]"
    
    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        str(out_file)
    ]
    
    print(f"Running ffmpeg: {' '.join(cmd)}")
    p = subprocess.run(cmd, capture_output=True, text=True)
    
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