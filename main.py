import os
import shutil
import subprocess
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from tempfile import TemporaryDirectory

app = FastAPI()

# Supabase Setup
URL = os.environ.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
BUCKET = "competition_matches"
supabase: Client = create_client(URL, KEY)

class VideoJob(BaseModel):
    videoPath: str
    matchId: int
    matchName: str

def process_video_task(job: VideoJob):
    with TemporaryDirectory() as temp_dir:
        input_path = os.path.join(temp_dir, "input.mp4")
        abr_dir = os.path.join(temp_dir, "abr")
        os.makedirs(abr_dir, exist_ok=True)

        try:
            # 1. Download raw file from Supabase
            print(f"Downloading {job.videoPath}...")
            res = supabase.storage.from_(BUCKET).download(job.videoPath)
            with open(input_path, 'wb+') as f:
                f.write(res)

            # 2. Generate Thumbnail
            print("Generating thumbnail...")
            thumb_local = os.path.join(temp_dir, "thumbnail.jpg")
            subprocess.run([
                'ffmpeg', '-i', input_path, '-ss', '00:00:01.000', 
                '-vframes', '1', thumb_local
            ], check=True)

            # 3. Generate HLS Assets (ABR)
            print("Running FFmpeg HLS conversion...")
            ffmpeg_cmd = [
                'ffmpeg', '-i', input_path,
                '-filter_complex', '[0:v]split=2[v1][v2];[v1]scale=w=1280:h=720[v1out];[v2]scale=w=854:h=480[v2out]',
                '-map', '[v1out]', '-map', '0:a', '-b:v:0', '2800k',
                '-map', '[v2out]', '-map', '0:a', '-b:v:1', '1400k',
                '-f', 'hls', '-hls_time', '10', '-hls_list_size', '0',
                '-master_pl_name', 'master.m3u8',
                '-hls_segment_filename', os.path.join(abr_dir, 'segment_%v_%03d.ts'),
                '-var_stream_map', 'v:0,a:0,name:720p v:1,a:1,name:480p',
                os.path.join(abr_dir, 'variant_%v.m3u8')
            ]
            subprocess.run(ffmpeg_cmd, check=True)

            # 4. Upload Assets back to Supabase
            print("Uploading results to Supabase...")
            # Upload Thumbnail
            with open(thumb_local, 'rb') as f:
                supabase.storage.from_(BUCKET).upload(
                    path=f"{job.matchName}/thumbnail.jpg",
                    file=f,
                    file_options={"upsert": "true"}
                )

            # Upload ABR files
            for file_name in os.listdir(abr_dir):
                file_path = os.path.join(abr_dir, file_name)
                with open(file_path, 'rb') as f:
                    supabase.storage.from_(BUCKET).upload(
                        path=f"{job.matchName}/abr/{file_name}",
                        file=f,
                        file_options={"upsert": "true"}
                    )

            # 5. Update Database
            video_url = supabase.storage.from_(BUCKET).get_public_url(f"{job.matchName}/abr/master.m3u8")
            thumb_url = supabase.storage.from_(BUCKET).get_public_url(f"{job.matchName}/thumbnail.jpg")

            supabase.table("matches").update({
                "video_url": video_url,
                "thumbnail": thumb_url,
                "is_processing": False
            }).eq("id", job.matchId).execute()

            # 6. Cleanup temp raw file from Supabase
            supabase.storage.from_(BUCKET).remove([job.videoPath])
            print(f"Success! Match {job.matchId} processed.")

        except Exception as e:
            print(f"Error processing video: {str(e)}")
            supabase.table("matches").update({"is_processing": False}).eq("id", job.matchId).execute()

@app.post("/process-video")
async def process_video(job: VideoJob, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_video_task, job)
    return {"message": "Processing started in background"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
