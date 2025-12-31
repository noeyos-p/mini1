from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from test_pipeline import MVPTestPipeline
import threading
import uvicorn
import os
import cv2
import time
from contextlib import asynccontextmanager

# 글로벌 파이프라인 인스턴스
pipeline = MVPTestPipeline()
pipeline_thread = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 (할 일 없음)
    yield
    # 서버 종료 시: 파이프라인 강제 정지
    print("[Web] 서버 종료 중... 파이프라인 정지")
    pipeline.running = False

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

def generate_frames():
    """웹 스트리밍을 위한 이미지 인코딩 생성기"""
    # 최초 시작 시 잠시 대기 (카메라 초기화 시간 고려)
    timeout = 10.0
    start_time = time.time()
    while not pipeline.running and (time.time() - start_time) < timeout:
        time.sleep(0.5)

    while True:
        # 파이프라인이 중지되면 루프 종료
        if not pipeline.running:
            break

        if pipeline.last_web_frame is not None:
            with pipeline.frame_lock:
                # 이미지를 JPEG로 변환
                ret, buffer = cv2.imencode('.jpg', pipeline.last_web_frame)
                frame = buffer.tobytes()
            
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            
            # FPS 조절 (너무 빠르면 CPU 부하)
            time.sleep(0.04)
        else:
            time.sleep(0.1)

@app.get("/", response_class=HTMLResponse)
async def read_item(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(generate_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.post("/start")
async def start_pipeline():
    global pipeline_thread
    if not pipeline.running:
        print("[Web] 파이프라인 시작 요청")
        pipeline_thread = threading.Thread(target=pipeline.run, daemon=True)
        pipeline_thread.start()
        return {"status": "started"}
    return {"status": "already_running"}

@app.post("/command")
async def process_command(request: Request):
    data = await request.json()
    text = data.get("text", "")
    print(f"[Web Test] 가상 음성 명령: {text}")
    pipeline.handle_command(text)
    return {"status": "command_processed", "text": text}

@app.post("/stop")
async def stop_pipeline():
    if pipeline.running:
        print("[Web] 파이프라인 정지 요청")
        pipeline.running = False
        # 쌓인 음성 안내를 모두 취소하고 "정지합니다"를 즉시 말함
        pipeline.speak("시스템을 정지합니다.", force_stop=True)
        return {"status": "stopped"}
    return {"status": "already_stopped"}

if __name__ == "__main__":
    # 템플릿 디렉토리가 없으면 생성
    if not os.path.exists("templates"):
        os.makedirs("templates")
    uvicorn.run(app, host="0.0.0.0", port=8000)
