import os
import subprocess
import logging
import mimetypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, HTTPException
from supabase import create_client, Client, ClientOptions
import boto3
from botocore.config import Config
from tempfile import TemporaryDirectory
import shutil

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Video Processor is Running"}

# Supabase Setup (used for the "matches" database table only)
URL = os.environ.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not URL or not KEY:
    logger.error("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variables are missing!")

# The "matches" table lives in the "website" schema, not "public".
supabase: Client = create_client(URL, KEY, options=ClientOptions(schema="website"))

# Cloudflare R2 Setup (video/thumbnail/HLS storage)
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
# Public base URL the bucket is served from: either a custom domain
# connected to the bucket, or the bucket's r2.dev URL.
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL")
# Optional "folder" inside the bucket to upload into, e.g. "videos" or
# "matches/2026". R2 has no real folders -- this is just a key prefix.
R2_KEY_PREFIX = os.environ.get("R2_KEY_PREFIX", "competition_matches").strip("/")

R2_CONFIGURED = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL])
if not R2_CONFIGURED:
    logger.error("One or more R2_* environment variables are missing!")

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

HLS_CONTENT_TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}

def build_r2_key(*parts: str) -> str:
    segments = [p.strip("/") for p in parts if p]
    if R2_KEY_PREFIX:
        segments = [R2_KEY_PREFIX] + segments
    return "/".join(segments)

def upload_to_r2(local_path: str, key: str) -> str:
    if not R2_CONFIGURED:
        raise RuntimeError(
            "Cloudflare R2 is not configured: set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, and R2_PUBLIC_URL."
        )
    ext = os.path.splitext(local_path)[1].lower()
    content_type = HLS_CONTENT_TYPES.get(ext) or mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    r2.upload_file(local_path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": content_type})
    return f"{R2_PUBLIC_URL.rstrip('/')}/{key}"

def process_video_task(temp_video_path: str, match_id: int, match_name: str, has_manual_thumb: bool, manual_thumb_path: str = None):
    logger.info(f"--- Starting Processing for Match ID: {match_id} ({match_name}) ---")
    
    with TemporaryDirectory() as temp_dir:
        input_path = temp_video_path
        abr_dir = os.path.join(temp_dir, "abr")
        os.makedirs(abr_dir, exist_ok=True)

        try:
            # 1. Handle Thumbnail
            thumb_url = None
            if has_manual_thumb and manual_thumb_path and os.path.exists(manual_thumb_path):
                logger.info("Using manual thumbnail provided by user.")
                thumb_url = upload_to_r2(manual_thumb_path, build_r2_key(match_name, "thumbnail.jpg"))
            else:
                # Auto-generated thumbnail extraction is independent of the HLS
                # encode below (both only read the source file), so it runs on
                # a background thread while the HLS conversion proceeds.
                logger.info("No manual thumbnail. Generating one from video in the background...")
                thumb_local = os.path.join(temp_dir, "thumbnail.jpg")

                def generate_thumbnail():
                    return subprocess.run([
                        'ffmpeg', '-i', input_path, '-ss', '00:00:01.000',
                        '-vframes', '1', thumb_local
                    ], capture_output=True, text=True)

                thumb_executor = ThreadPoolExecutor(max_workers=1)
                thumb_future = thumb_executor.submit(generate_thumbnail)

            # 2. Generate HLS Assets (ABR)
            logger.info("Starting HLS conversion (720p & 480p)...")
            ffmpeg_cmd = [
                'ffmpeg', '-threads', '0', '-i', input_path,
                '-filter_complex', '[0:v]split=2[v1][v2];[v1]scale=w=1280:h=720[v1out];[v2]scale=w=854:h=480[v2out]',
                '-map', '[v1out]', '-map', '0:a', '-c:v:0', 'libx264', '-preset', 'veryfast', '-b:v:0', '2800k',
                '-map', '[v2out]', '-map', '0:a', '-c:v:1', 'libx264', '-preset', 'veryfast', '-b:v:1', '1400k',
                '-f', 'hls', '-hls_time', '10', '-hls_list_size', '0',
                '-master_pl_name', 'master.m3u8',
                '-hls_segment_filename', os.path.join(abr_dir, 'segment_%v_%03d.ts'),
                '-var_stream_map', 'v:0,a:0,name:720p v:1,a:1,name:480p',
                os.path.join(abr_dir, 'variant_%v.m3u8')
            ]
            
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                logger.error(f"FFmpeg HLS conversion failed: {result.stderr}")
                raise Exception("FFmpeg failed")

            logger.info("HLS conversion complete. Uploading segments...")

            # Pick up the auto-generated thumbnail once it's ready and upload it.
            if not has_manual_thumb:
                thumb_result = thumb_future.result()
                thumb_executor.shutdown(wait=True)

                if thumb_result.returncode == 0:
                    thumb_url = upload_to_r2(thumb_local, build_r2_key(match_name, "thumbnail.jpg"))
                    logger.info("Auto-generated thumbnail uploaded successfully.")
                else:
                    logger.error(f"FFmpeg thumbnail failed: {thumb_result.stderr}")

            # 1b. Update Thumbnail in Database
            if thumb_url:
                logger.info(f"Updating DB with thumbnail for Match {match_id}...")
                supabase.table("matches").update({"thumbnail": thumb_url}).eq("id", match_id).execute()

            # 3. Upload ABR files to R2 concurrently
            def upload_segment(file_name: str):
                file_path = os.path.join(abr_dir, file_name)
                upload_to_r2(file_path, build_r2_key(match_name, "abr", file_name))
                return file_name

            abr_files = os.listdir(abr_dir)
            upload_errors = []
            with ThreadPoolExecutor(max_workers=8) as upload_executor:
                futures = {upload_executor.submit(upload_segment, name): name for name in abr_files}
                for future in as_completed(futures):
                    file_name = futures[future]
                    try:
                        future.result()
                    except Exception as upload_exc:
                        logger.error(f"Failed to upload {file_name}: {upload_exc}")
                        upload_errors.append(file_name)

            if upload_errors:
                raise Exception(f"Failed to upload {len(upload_errors)} HLS file(s): {upload_errors}")

            logger.info(f"All {len(abr_files)} HLS files uploaded.")

            # 4. Update Database
            video_url = f"{R2_PUBLIC_URL.rstrip('/')}/{build_r2_key(match_name, 'abr', 'master.m3u8')}"
            
            update_data = {
                "video_url": video_url,
                "is_processing": False
            }
            if thumb_url:
                update_data["thumbnail"] = thumb_url

            supabase.table("matches").update(update_data).eq("id", match_id).execute()
            logger.info(f"--- Success! Match {match_id} is now LIVE ---")

        except Exception as e:
            logger.exception(f"CRITICAL ERROR processing match {match_id}: {str(e)}")
            try:
                supabase.table("matches").update({"is_processing": False}).eq("id", match_id).execute()
            except Exception:
                logger.exception(f"Also failed to reset is_processing flag for match {match_id}")
        finally:
            # Cleanup the temporary uploaded video file
            if os.path.exists(temp_video_path):
                os.remove(temp_video_path)
            if manual_thumb_path and os.path.exists(manual_thumb_path):
                os.remove(manual_thumb_path)

@app.post("/process-video")
async def process_video(
    background_tasks: BackgroundTasks,
    matchId: int = Form(...),
    matchName: str = Form(...),
    video: UploadFile = File(...),
    thumbnail: UploadFile = File(None)
):
    logger.info(f"Received request for Match: {matchName} (ID: {matchId})")
    
    # Create a persistent temp file for the video since the UploadFile stream will close
    temp_video = TemporaryDirectory().name + "_video.mp4"
    with open(temp_video, "wb") as buffer:
        shutil.copyfileobj(video.file, buffer)
    
    has_manual_thumb = False
    temp_thumb = None
    if thumbnail:
        has_manual_thumb = True
        temp_thumb = TemporaryDirectory().name + "_thumb.jpg"
        with open(temp_thumb, "wb") as buffer:
            shutil.copyfileobj(thumbnail.file, buffer)

    background_tasks.add_task(
        process_video_task, 
        temp_video, 
        matchId, 
        matchName, 
        has_manual_thumb, 
        temp_thumb
    )
    
    return {"message": "Upload successful. Processing started in background."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
