import os
# os.environ['OPENCV_AVFOUNDATION_SKIP_AUTH'] = '1'
# Mac 카메라권한 문제 관해 환경변수 추가 (Windows 주석처리)

from fastapi import FastAPI, Request, File, UploadFile, Response
from fastapi.responses import HTMLResponse, StreamingResponse
import numpy as np
from fastapi.templating import Jinja2Templates
from test_pipeline import MVPTestPipeline
import threading
import uvicorn
import cv2
import time
import tempfile
import whisper
from contextlib import asynccontextmanager

# Whisper 모델 로드 (웹 음성인식용)
whisper_model = whisper.load_model("tiny")

# 글로벌 파이프라인 인스턴스
# web_mode=False: 로컬 모드 (서버 STT/TTS 활성화, 노트북용)
# web_mode=True: 웹 모드 (클라이언트 STT/TTS, 모바일용)
# 변경: 웹 브라우저에서 TTS를 재생하기 위해 web_mode=True로 설정
pipeline = MVPTestPipeline(web_mode=True)
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
        # 변경: 웹 모드일 때는 run() 함수를 실행하지 않음 (클라이언트가 프레임 전송)
        if pipeline.web_mode:
            # 웹 모드: running 플래그만 설정, 실제 처리는 /process_frame에서
            pipeline.running = True
            return {"status": "started"}
        else:
            # 로컬 모드: run() 함수 실행 (서버 카메라 사용)
            # Window cv2 사용가능 (주석해제)
            pipeline_thread = threading.Thread(target=pipeline.run, daemon=True)

            # Mac에서 스레드 내 cv2 창 불가로 headless=True 사용
            # pipeline_thread = threading.Thread(target=lambda: pipeline.run(headless=True), daemon=True)

            pipeline_thread.start()
            return {"status": "started"}
    return {"status": "already_running"}

@app.post("/command")
async def process_command(request: Request):
    data = await request.json()
    text = data.get("text", "")
    print(f"[Web Test] 가상 음성 명령: {text}")
    response_text = pipeline.handle_command(text)
    return {"status": "command_processed", "text": text, "response": response_text}

@app.post("/stop")
async def stop_pipeline():
    if pipeline.running:
        print("[Web] 파이프라인 정지 요청")
        pipeline.running = False
        # 쌓인 음성 안내를 모두 취소하고 "정지합니다"를 즉시 말함
        pipeline.speak("시스템을 정지합니다.", force_stop=True)
        return {"status": "stopped"}
    return {"status": "already_stopped"}

@app.post("/voice")
async def voice_recognition(audio: UploadFile = File(...)):
    # 웹에서 녹음된 음성 파일을 받아서 Whisper로 인식
    try:
        # 임시 파일로 저장
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            content = await audio.read()
            f.write(content)
            temp_path = f.name

        # Whisper로 음성 인식
        result = whisper_model.transcribe(temp_path, language="ko", fp16=False)
        text = result["text"].strip()

        # 임시 파일 삭제
        os.unlink(temp_path)

        print(f"[Web] 음성 인식 결과: {text}")

        # 명령어 처리
        response_text = None
        if text:
            response_text = pipeline.handle_command(text)

        return {"status": "success", "text": text, "response": response_text}
    except Exception as e:
        print(f"[Web] 음성 인식 오류: {e}")
        return {"status": "error", "text": str(e)}

# 모바일 카메라 프레임 처리 엔드포인트
@app.post("/process_frame")
async def process_frame(frame: UploadFile = File(...)):
    try:
        # 이미지 데이터 읽기
        contents = await frame.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            return Response(content=b'', status_code=400)

        # 파이프라인으로 프레임 처리 (YOLO 등)
        # 변경: 음성 안내 텍스트 + 감지 데이터도 함께 반환
        processed_img, speech_text, detection_data = pipeline.process_web_frame_with_speech(img)

        # JSON으로 감지 데이터와 음성 텍스트 반환 (이미지는 클라이언트 렌더링)
        return {
            "detection": detection_data,
            "speech": speech_text,
            "frame_size": {"width": img.shape[1], "height": img.shape[0]}
        }

    except Exception as e:
        print(f"[Web] 프레임 처리 오류: {e}")
        return Response(content=b'', status_code=500)

if __name__ == "__main__":
    # 템플릿 디렉토리가 없으면 생성
    if not os.path.exists("templates"):
        os.makedirs("templates")
    # access_log=False로 반복되는 /process_frame 로그 숨김
    # 서버 시작 등 중요한 로그는 표시됨
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
