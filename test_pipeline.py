import cv2
import torch
import numpy as np
import time
import threading
import queue
import win32com.client
import pythoncom
import json
import os
from ultralytics import YOLO
from follow_up_service import FollowUpSpeechService

# ==========================================
# FollowUpManager: Handles scheduling and cancellation
# ==========================================
class FollowUpManager:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.service = FollowUpSpeechService()
        self.pending_timer = None
        self.lock = threading.Lock()
        self.current_context = None # (label, distance)
        
        # LLM Suppression Logic
        self.llm_call_history = {} # {entity_key: last_call_time}
        self.ALLOWED_CLASSES = {'사람', '자동차', '자전거'} # person, car, bicycle
        self.MAX_LLM_DIST = 4.0
        self.COOL_DOWN_SEC = 8.0 # 5-8 seconds requirement

    def cancel_pending(self):
        with self.lock:
            if self.pending_timer:
                print("[FollowUpMgr] Cancelling pending follow-up.")
                self.pending_timer.cancel()
                self.pending_timer = None
            self.current_context = None

    def schedule_follow_up(self, label, distance, position_desc, entity_key):
        """Schedules a follow-up with strict suppression gating."""
        # Layer 1: Class and Distance Gating
        if label not in self.ALLOWED_CLASSES:
            # print(f"[FollowUpMgr] LLD Suppressed: {label} is not in whitelist.")
            return

        if distance > self.MAX_LLM_DIST:
            # print(f"[FollowUpMgr] LLM Suppressed: distance {distance:.1f}m > {self.MAX_LLM_DIST}m.")
            return

        # Layer 2: Cool-down Gating
        current_time = time.time()
        last_call = self.llm_call_history.get(entity_key, 0)
        if (current_time - last_call) < self.COOL_DOWN_SEC:
            # print(f"[FollowUpMgr] LLM Suppressed: Cool-down active for {entity_key}.")
            return

        # Passed all gates - proceed to cancel pending and schedule new call
        self.cancel_pending()
        
        with self.lock:
            self.current_context = (label, distance)
            # Record call time to enforce cool-down
            self.llm_call_history[entity_key] = current_time
            
            # Reduced delay since immediate warning is removed
            self.pending_timer = threading.Timer(0.2, self._execute_follow_up, args=(label, distance, position_desc))
            self.pending_timer.start()
            print(f"[FollowUpMgr] Gating Passed. Triggering LLM for {label} at {distance:.1f}m")

    def _generate_rule_based_fallback(self, label, distance, position_desc):
        """Deterministic safety fallback when LLM is unavailable."""
        return f"{position_desc} {distance:.1f}미터에 {label}이 있으니 주의하세요."

    def _execute_follow_up(self, label, distance, position_desc):
        # Verification: Check if the situation is still relevant? 
        # (For this MVP, we rely on the cancellation being called by the loop if object is gone)
        
        # This function runs in a separate thread (Timer thread)
        # It calls the LLM, which is NOT in the detection loop.
        explanation = self.service.generate_explanation(label, distance, position_desc)
        
        # Fallback if LLM fails (explanation is None)
        if explanation is None:
            print("[FollowUpMgr] LLM API failure. Triggering rule-based fallback.")
            explanation = self._generate_rule_based_fallback(label, distance, position_desc)

        if explanation:
            print(f"[FollowUpMgr] Speech Output: {explanation}")
            # Only play if not cancelled during LLM call
            with self.lock:
                if self.current_context == (label, distance):
                    # Play the explanation through the pipeline's TTS worker
                    self.pipeline.speak(explanation, is_follow_up=True)
                else:
                    print("[FollowUpMgr] Context changed during wait/call, discarding result.")

# Whisper 및 PyAudio 설정
WHISPER_AVAILABLE = False
PYAUDIO_AVAILABLE = False
try:
    from faster_whisper import WhisperModel
    import pyaudio
    WHISPER_AVAILABLE = True
    PYAUDIO_AVAILABLE = True
except ImportError:
    pass

# ==========================================
# Optimized MVP Test Pipeline: TTS (음성 안내) 버전
# ==========================================

class MVPTestPipeline:
    def __init__(self):
        print("음성 지원 모드로 전환 중... 모델 로딩 중...")
        
        # 설정값
        self.inference_size = (320, 320)
        self.frame_skip = 3
        self.frame_count = 0
        self.K_DEPTH = 3000.0 
        self.running = False  # 제어용 플래그

        # 음성 안내 설정 (볼륨 및 뮤트)
        self.volume = 100  # 0 ~ 100
        self.is_muted = False

        # TTS 큐 및 스레드 초기화
        self.speech_queue = queue.Queue()
        self.tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self.tts_thread.start()

        # STT (음성 인식) 초기화
        self.stt_thread = None
        if WHISPER_AVAILABLE and PYAUDIO_AVAILABLE:
            try:
                # Whisper 'base' 모델 로딩 (CPU 사용 시 최적화)
                # 다국어 모델이므로 언어를 ko로 고정하면 더 정확함
                print("Whisper 'base' 모델 로딩 중...")
                self.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
                self.stt_thread = threading.Thread(target=self._stt_worker, daemon=True)
                self.stt_thread.start()
            except Exception as e:
                print(f"⚠️ STT 초기화 중 오류 발생: {e}")
        else:
            print(f"⚠️ 음성 명령이 비활성화되었습니다. (라이브러리 미설치)")
        
        # FollowUp Manager 초기화
        self.follow_up_mgr = FollowUpManager(self)

        # 시작 알림 (스피커 확인용)
        self.speak("시스템을 시작합니다.")

        # 음성 상태 관리
        self.announced_objects = {} # {label: last_seen_time}
        self.announce_timeout = 8.0 # 8초 동안 안 보이면 안내 목록에서 삭제 (다시 나타나면 말함)

        # 모델 로딩
        self.yolo_model = YOLO('yolov8n.pt') 
        self.depth_model_type = "MiDaS_small"
        self.midas = torch.hub.load("intel-isl/MiDaS", self.depth_model_type, trust_repo=True)
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        self.midas.to(self.device).eval()
        
        midas_transforms = torch.hub.load("intel-isl/MiDaS", "transforms", trust_repo=True)
        self.transform = midas_transforms.small_transform if self.depth_model_type == "MiDaS_small" else midas_transforms.dpt_transform

        self.last_objects = []
        self.last_depth_map = None
        self.last_depth_viz = None
        
        # 웹 스트리밍용 버퍼
        self.last_web_frame = None
        self.frame_lock = threading.Lock()

        # 한국어 클래스 맵
        self.class_names_ko = {
            'person': '사람', 'bicycle': '자전거', 'car': '자동차', 'motorcycle': '오토바이',
            'bus': '버스', 'truck': '트럭', 'traffic light': '신호등', 'stop sign': '정지 표지판',
            'bench': '벤치', 'dog': '개', 'cat': '고양이', 'backpack': '배낭', 'umbrella': '우산',
            'handbag': '핸드백', 'tie': '넥타이', 'suitcase': '여행가방', 'sports ball': '공',
            'bottle': '병', 'wine glass': '와인잔', 'cup': '컵', 'fork': '포크', 'knife': '칼',
            'spoon': '숟가락', 'bowl': '그릇', 'banana': '바나나', 'apple': '사과', 'sandwich': '샌드위치',
            'orange': '오렌지', 'broccoli': '브로콜리', 'carrot': '당근', 'hot dog': '핫도그', 'pizza': '피자',
            'donut': '도넛', 'cake': '케이크', 'chair': '의자', 'couch': '소파', 'potted plant': '화분',
            'bed': '침대', 'dining table': '식탁', 'toilet': '변기', 'tv': 'TV', 'laptop': '노트북',
            'mouse': '마우스', 'remote': '리모컨', 'keyboard': '키보드', 'cell phone': '핸드폰',
            'microwave': '전자레인지', 'oven': '오븐', '토스터': '토스터', 'sink': '싱크대',
            'refrigerator': '냉장고', 'book': '책', 'clock': '시계', 'vase': '꽃병', 'scissors': '가위',
            'teddy bear': '곰인형', 'hair drier': '헤어드라이어', 'toothbrush': '칫솔'
        }

        # Walking assistance ROI (Center 40%)
        self.roi_x_min = 0.3
        self.roi_x_max = 0.7

        # Spatial Bucketing for entity differentiation
        self.DIST_BIN_SIZE = 1.5   # meters
        self.POS_BIN_SIZE = 0.1    # 10% of frame width

    def _tts_worker(self):
        """별도 스레드에서 SAPI 엔진을 초기화하고 안내를 처리 (가장 확실한 윈도우 방식)"""
        pythoncom.CoInitialize()
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        
        while True:
            # 큐에서 데이터를 가져옴 (텍스트, 강제중지여부, follow_up여부)
            item = self.speech_queue.get()
            if item is None: break
            
            text, force_stop, is_follow_up = item
            
            # 뮤트 상태면 무시 (단, 강제 종료 안내는 예외)
            if self.is_muted and not force_stop:
                self.speech_queue.task_done()
                continue

            # 실시간 볼륨 적용
            speaker.Volume = self.volume

            # force_stop이 True이면 현재 말하고 있는 것과 밀려있는 큐를 모두 무시하고 즉시 말함
            # SAPI Flag: 2 (SVSFPurgeBeforeSpeak)
            flags = 2 if force_stop else 0
            
            print(f"[TTS 발화 시작] {text} (강제종료: {force_stop}, 후속: {is_follow_up})")
            try:
                speaker.Speak(text, flags)
            except Exception as e:
                print(f"[TTS 오류] {e}")
            print(f"[TTS 발화 완료] {text}")
            
            # 후속 안내가 아니고, 강제 중지가 아니면 Manager에게 완료 신호
            # (실제로는 speak() 호출 시 Manager를 호출하게 변경할 수 있음)
            self.speech_queue.task_done()

    def _stt_worker(self):
        """마이크 소리를 듣고 Whisper로 인식하는 스레드 (VAD 포함)"""
        CHUNK = 1024
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 16000
        SILENCE_THRESHOLD = 500  # 음성 감지 임계값 (환경에 따라 조절 필요)
        SILENCE_DURATION = 1.0   # 침묵 시간 (초)

        p = pyaudio.PyAudio()
        stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
        stream.start_stream()

        print("🎙️ Whisper STT 준비 완료. 명령을 기다립니다...")

        audio_buffer = []
        is_speaking = False
        silence_start = None

        while True:
            data = stream.read(CHUNK, exception_on_overflow=False)
            audio_data = np.frombuffer(data, dtype=np.int16)
            amplitude = np.abs(audio_data).mean()

            if amplitude > SILENCE_THRESHOLD:
                if not is_speaking:
                    is_speaking = True
                    print("🗣️ 말하는 중...")
                audio_buffer.append(audio_data)
                silence_start = None
            else:
                if is_speaking:
                    if silence_start is None:
                        silence_start = time.time()
                    
                    audio_buffer.append(audio_data)

                    # 일정 시간 이상 침묵 시 인식 시작
                    if time.time() - silence_start > SILENCE_DURATION:
                        print("⌛ 인식 중...")
                        # 오디오 데이터를 Whisper 형식으로 변환 (float32, 16kHz)
                        full_audio = np.concatenate(audio_buffer).astype(np.float32) / 32768.0
                        
                        segments, info = self.whisper_model.transcribe(full_audio, language="ko", beam_size=5)
                        text = "".join([segment.text for segment in segments]).strip()
                        
                        if text:
                            print(f"👂 Whisper 결과: {text}")
                            self.handle_command(text)
                        
                        # 버퍼 및 상태 초기화
                        audio_buffer = []
                        is_speaking = False
                        silence_start = None

    def handle_command(self, text):
        """음성 인식을 통해 들어온 텍스트를 분석하여 명령 수행"""
        # 명령어 판별 (공백 제거 후 비교)
        text = text.replace(" ", "")
        
        if "종료" in text:
            self.speak("시스템을 종료합니다.", force_stop=True)
            self.running = False
        elif "다시시작" in text or "다시실행" in text:
            self.speak("시스템을 다시 시작합니다.", force_stop=True)
        elif "볼륨올려" in text:
            self.volume = min(100, self.volume + 20)
            self.speak(f"볼륨을 올렸습니다. 현재 볼륨 {self.volume}")
        elif "볼륨내려" in text:
            self.volume = max(0, self.volume - 20)
            self.speak(f"볼륨을 내렸습니다. 현재 볼륨 {self.volume}")
        elif "조용히해" in text or "정지해" in text:
            self.is_muted = True
            self.speak("음성 안내를 일시 정지합니다.", force_stop=True)
        elif "말해줘" in text or "다시말해" in text:
            self.is_muted = False
            self.speak("음성 안내를 다시 시작합니다.")

    def speak(self, text, force_stop=False, is_follow_up=False):
        """안내 문구를 큐에 추가 (비동기)"""
        if force_stop:
            # 기존 큐에 쌓인 모든 메시지 무시하도록 큐 비우기 시도
            while not self.speech_queue.empty():
                try:
                    self.speech_queue.get_nowait()
                    self.speech_queue.task_done()
                except:
                    break
        self.speech_queue.put((text, force_stop, is_follow_up))

    def stage2_yolo_optimized(self, frame):
        results = self.yolo_model(frame, imgsz=320, verbose=False) 
        objects = []
        for r in results:
            boxes = r.boxes
            for box in boxes:
                b = box.xyxy[0].cpu().numpy().astype(int)
                cls_id = int(box.cls[0])
                model_label = self.yolo_model.names[cls_id]
                ko_label = self.class_names_ko.get(model_label, model_label)
                objects.append({'box': b, 'label': ko_label})
        return objects

    def stage3_depth_optimized(self, frame):
        small_frame = cv2.resize(frame, (256, 256)) 
        img = cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB)
        input_batch = self.transform(img).to(self.device)

        with torch.no_grad():
            prediction = self.midas(input_batch)
            prediction = torch.nn.functional.interpolate(
                prediction.unsqueeze(1),
                size=frame.shape[:2],
                mode="bicubic",
                align_corners=False,
            ).squeeze()

        depth_map = prediction.cpu().numpy()
        depth_min, depth_max = depth_map.min(), depth_map.max()
        depth_norm = (255 * (depth_map - depth_min) / (depth_max - depth_min + 1e-5)).astype(np.uint8)
        depth_color = cv2.applyColorMap(depth_norm, cv2.COLORMAP_MAGMA)
        
        return depth_map, depth_color

    def raw_to_meters(self, raw_val):
        if raw_val <= 0: return float('inf')
        meters = self.K_DEPTH / (raw_val + 1e-5)
        return meters

    def run(self):
        cap = cv2.VideoCapture(1) # 0은 내장카메라, 1은 외장카메라
        if not cap.isOpened():
            print("카메라를 열 수 없습니다.")
            return

        window_name_main = "MVP Test - Color (YOLO)"
        window_name_depth = "MVP Test - Depth (MiDaS)"
        cv2.namedWindow(window_name_main)
        cv2.namedWindow(window_name_depth)

        print("\n=== 음성 안내(TTS)가 최적화된 MVP 파이프라인 시작 ===")
        
        # 시작 시 안내 음성 추가 (웹에서 다시 시작할 때도 나옴)
        self.speak("보조 시스템 안내를 시작합니다.", force_stop=True)

        self.running = True
        last_log_time = 0
        log_interval = 6.0 

        while self.running:
            ret, frame = cap.read()
            if not ret: break
            
            self.frame_count += 1
            current_time = time.time()
            
            # --- 파이프라인 연산 ---
            if self.frame_count % self.frame_skip == 1 or self.last_depth_map is None:
                self.last_objects = self.stage2_yolo_optimized(frame)
                self.last_depth_map, self.last_depth_viz = self.stage3_depth_optimized(frame)
            
            display_frame = frame.copy()
            should_log = (current_time - last_log_time) >= log_interval

            # --- ROI 필터링 및 가장 가까운 물체 선택 ---
            h, w = frame.shape[:2]
            roi_left = int(w * self.roi_x_min)
            roi_right = int(w * self.roi_x_max)
            
            closest_obj = None
            min_meters = float('inf')

            for obj in self.last_objects:
                b = obj['box']
                cx = (b[0] + b[2]) // 2
                cy = int(b[3] * 0.9)
                
                # ROI 내부에 중심이 있는 경우에만 처리
                if roi_left <= cx <= roi_right:
                    h_d, w_d = self.last_depth_map.shape
                    cx_d, cy_d = max(0, min(cx, w_d-1)), max(0, min(cy, h_d-1))
                    
                    raw_val = self.last_depth_map[cy_d, cx_d]
                    meters = self.raw_to_meters(raw_val)
                    
                    # 가장 가까운 물체 갱신
                    if meters < min_meters:
                        min_meters = meters
                        closest_obj = {
                            'label': obj['label'],
                            'box': b,
                            'meters': meters,
                            'cx': cx
                        }

            # --- 시각화 및 안내 ---
            # ROI 가이드 라인 표시
            cv2.line(display_frame, (roi_left, 0), (roi_left, h), (0, 0, 255), 2)
            cv2.line(display_frame, (roi_right, 0), (roi_right, h), (0, 0, 255), 2)

            current_entities = set() # (label, dist_bin, pos_bin)
            if closest_obj and min_meters < 10.0:
                b = closest_obj['box']
                label_name = closest_obj['label']
                meters = closest_obj['meters']
                
                # Generate Spatial Composite Key
                dist_bin = int(meters / self.DIST_BIN_SIZE)
                pos_bin = int((closest_obj['cx'] / w) / self.POS_BIN_SIZE)
                entity_key = (label_name, dist_bin, pos_bin)
                
                current_entities.add(entity_key)

                # 시각화 (선택된 물체만 강조)
                cv2.rectangle(display_frame, (b[0], b[1]), (b[2], b[3]), (0, 0, 255), 3)
                cv2.putText(display_frame, f"TARGET: {label_name} {meters:.1f}m", (b[0], b[1]-10), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

                # --- 음성 안내 로직 ---
                if entity_key not in self.announced_objects:
                    # Mark as announced immediately to prevent duplicate triggers
                    self.announced_objects[entity_key] = current_time
                    
                    # Determine position description
                    pos_desc = "정면"
                    if closest_obj['cx'] < roi_left + (roi_right - roi_left) * 0.3:
                        pos_desc = "약간 왼쪽"
                    elif closest_obj['cx'] > roi_left + (roi_right - roi_left) * 0.7:
                        pos_desc = "약간 오른쪽"
                    
                    # Trigger natural warning (LLM-based) with strict gating
                    self.follow_up_mgr.schedule_follow_up(label_name, meters, pos_desc, entity_key)

                if should_log:
                    print(f"[보행 보조] 장애물 감지: {label_name} | 개체 키: {entity_key} | 거리: {meters:.1f}m")

            # 안내 상태 업데이트 (오랫동안 안 보인 사물은 목록에서 제거)
            for entity_key in list(self.announced_objects.keys()):
                if entity_key not in current_entities:
                    label_name = entity_key[0] # tuple (label, dist, pos)
                    # 감지 영역에서 사라짐 -> 안내 목록에서 삭제
                    if current_time - self.announced_objects[entity_key] > self.announce_timeout:
                        del self.announced_objects[entity_key]
                        # 만약 사라진 물체에 대한 후속 안내가 예약되어 있다면 취소
                        self.follow_up_mgr.cancel_pending()

            if should_log:
                last_log_time = current_time

            # 화면 표시

            # 화면 표시
            # 웹 스트리밍용으로 현재 프레임 저장
            with self.frame_lock:
                self.last_web_frame = display_frame.copy()

            cv2.imshow(window_name_main, display_frame)
            if self.last_depth_viz is not None:
                cv2.imshow(window_name_depth, self.last_depth_viz)
            
            # 종료 로직
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or key == 27: # Q나 ESC
                break
            
            # 창이 닫혔는지 확인
            if cv2.getWindowProperty(window_name_main, cv2.WND_PROP_VISIBLE) < 1:
                break

        cap.release()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    pipeline = MVPTestPipeline()
    pipeline.run()
