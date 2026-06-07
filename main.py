import os
import subprocess
import logging
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, HTTPException
from supabase import create_client, Client
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

# Supabase Setup
URL = os.environ.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
BUCKET = "competition_matches"

if not URL or not KEY:
    logger.error("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variables are missing!")

supabase: Client = create_client(URL, KEY)

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
                with open(manual_thumb_path, 'rb') as f:
                    supabase.storage.from_(BUCKET).upload(
                        path=f"{match_name}/thumbnail.jpg",
                        file=f,
                        file_options={"upsert": "true"}
                    )
                thumb_url = supabase.storage.from_(BUCKET).get_public_url(f"{match_name}/thumbnail.jpg")
            else:
                logger.info("No manual thumbnail. Generating one from video...")
                thumb_local = os.path.join(temp_dir, "thumbnail.jpg")
                result = subprocess.run([
                    'ffmpeg', '-i', input_path, '-ss', '00:00:01.000', 
                    '-vframes', '1', thumb_local
                ], capture_output=True, text=True)
                
                if result.returncode == 0:
                    with open(thumb_local, 'rb') as f:
                        supabase.storage.from_(BUCKET).upload(
                            path=f"{match_name}/thumbnail.jpg",
                            file=f,
                            file_options={"upsert": "true"}
                        )
                    thumb_url = supabase.storage.from_(BUCKET).get_public_url(f"{match_name}/thumbnail.jpg")
                    logger.info("Auto-generated thumbnail uploaded successfully.")
                else:
                    logger.error(f"FFmpeg thumbnail failed: {result.stderr}")

            # 1b. Update Thumbnail immediately in Database
            if thumb_url:
                logger.info(f"Updating DB with thumbnail for Match {match_id} immediately...")
                supabase.table("matches").update({"thumbnail": thumb_url}).eq("id", match_id).execute()

            # 2. Generate HLS Assets (ABR)
            logger.info("Starting HLS conversion (720p & 480p)...")
            ffmpeg_cmd = [
                'ffmpeg', '-i', input_path,
                '-filter_complex', '[0:v]split=2[v1][v2];[v1]scale=w=1280:h=720[v1out];[v2]scale=w=854:h=480[v2out]',
                '-map', '[v1out]', '-map', '0:a', '-c:v:0', 'libx264', '-b:v:0', '2800k',
                '-map', '[v2out]', '-map', '0:a', '-c:v:1', 'libx264', '-b:v:1', '1400k',
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

            # 3. Upload ABR files to Supabase
            for file_name in os.listdir(abr_dir):
                file_path = os.path.join(abr_dir, file_name)
                with open(file_path, 'rb') as f:
                    supabase.storage.from_(BUCKET).upload(
                        path=f"{match_name}/abr/{file_name}",
                        file=f,
                        file_options={"upsert": "true"}
                    )
            
            logger.info(f"All {len(os.listdir(abr_dir))} HLS files uploaded.")

            # 4. Update Database
            video_url = supabase.storage.from_(BUCKET).get_public_url(f"{match_name}/abr/master.m3u8")
            
            update_data = {
                "video_url": video_url,
                "is_processing": False
            }
            if thumb_url:
                update_data["thumbnail"] = thumb_url

            supabase.table("matches").update(update_data).eq("id", match_id).execute()
            logger.info(f"--- Success! Match {match_id} is now LIVE ---")

        except Exception as e:
            logger.error(f"CRITICAL ERROR processing match {match_id}: {str(e)}")
            supabase.table("matches").update({"is_processing": False}).eq("id", match_id).execute()
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
