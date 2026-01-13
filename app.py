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

    list_txt = workdir / "list.txt"
    with open(list_txt, "w") as f:
        for fp in local_files:
            # escaping quotes in filename if needed, but uuid/simple names are safe
            f.write(f"file '{fp.absolute()}'\n")

    out_file = workdir / "out.mp4"

    # 1. Try safe copy (only works if codecs/resolutions match)
    p = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_txt), "-c", "copy", str(out_file)],
        capture_output=True, text=True
    )
    
    # 2. Fallback to re-encoding if copy fails
    if p.returncode != 0:
        print(f"Copy concat failed, re-encoding. Error: {p.stderr}")
        p2 = subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_txt),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", 
                str(out_file)
            ],
            capture_output=True, text=True
        )
        if p2.returncode != 0:
            print(f"Re-encode failed: {p2.stderr}")
            # Clean up on failure? Maybe/Maybe not
            raise HTTPException(status_code=500, detail=f"Concat failed: {p2.stderr[-500:]}")

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