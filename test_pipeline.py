import os
# os.environ['OPENCV_AVFOUNDATION_SKIP_AUTH'] = '1'
# Mac 카메라권한 문제 관해 환경변수 추가 (Windows 주석처리)

import cv2
import torch
import numpy as np
import time
import threading
import queue
import json
import platform
import subprocess

# Windows 전용 (Mac에서는 주석처리)
import win32com.client
import pythoncom

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
    import whisper  # openai-whisper
    import pyaudio
    WHISPER_AVAILABLE = True
    PYAUDIO_AVAILABLE = True
except ImportError:
    pass

# ==========================================
# Optimized MVP Test Pipeline: TTS (음성 안내) 버전
# ==========================================

class MVPTestPipeline:
    def __init__(self, web_mode=False):
        """
        web_mode=True: 웹 모드 (서버 STT/TTS 비활성화, 클라이언트에서 처리)
        web_mode=False: 로컬 모드 (서버 STT/TTS 활성화)
        """
        self.web_mode = web_mode
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

        # TTS 큐 및 스레드 초기화 (웹 모드가 아닐 때만)
        self.speech_queue = queue.Queue()
        if not web_mode:
            self.tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
            self.tts_thread.start()

        # STT (음성 인식) 초기화 (웹 모드가 아닐 때만)
        self.stt_thread = None
        if not web_mode and WHISPER_AVAILABLE and PYAUDIO_AVAILABLE:
            try:
                # Whisper 모델 로딩
                print("Whisper 'tiny' 모델 로딩 중...")
                # self.whisper_model = whisper.load_model("base")
                self.whisper_model = whisper.load_model("tiny")  # 빠른 처리
                self.stt_thread = threading.Thread(target=self._stt_worker, daemon=True)
                self.stt_thread.start()
            except Exception as e:
                print(f"STT 초기화 중 오류 발생: {e}")
        elif not web_mode:
            print(f"음성 명령이 비활성화되었습니다. (라이브러리 미설치)")
        
        # FollowUp Manager 초기화
        self.follow_up_mgr = FollowUpManager(self)

        # 시작 알림 (스피커 확인용) - 웹 모드가 아닐 때만
        if not web_mode:
            self.speak("시스템을 시작합니다.")

        # 음성 상태 관리
        self.announced_objects = {} # {label: last_seen_time}
        self.announce_timeout = 8.0 # 8초 동안 안 보이면 안내 목록에서 삭제 (다시 나타나면 말함)

        # 라벨별 쿨다운 (같은 종류 객체 반복 안내 방지)
        self.label_cooldown = {}  # {label: last_announce_time}
        self.cooldown_time = 5.0  # 5초 쿨다운

        # 웹 모드용 LLM 응답 캐시
        self.web_speech_cache = {}  # {entity_key: llm_generated_text}
        self.cache_lock = threading.Lock()

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
        
        # Velocity Tracking for Approach Detection
        self.entity_velocity_history = {} # {entity_key: (last_dist, last_time)}
        self.APPROACH_THRESHOLD_SPEED = 1.5 # m/s (approx 5.4 km/h)
        self.APPROACH_THRESHOLD_DIST = 4.0  # meters

    def _tts_worker(self):
        """별도 스레드에서 TTS 안내를 처리"""
        # Windows용 (주석해제)
        pythoncom.CoInitialize()
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        current_process = None  # Windows 모드에서도 초기화

        # Mac용 (1줄) 현재 실행 중인 TTS 프로세스
        # current_process = None

        while True:
            # 큐에서 데이터를 가져옴 (텍스트, 강제중지여부, follow_up여부)
            item = self.speech_queue.get()
            if item is None: break

            text, force_stop, is_follow_up = item

            # 뮤트 상태면 무시 (단, 강제 종료 안내는 예외)
            if self.is_muted and not force_stop:
                self.speech_queue.task_done()
                continue

            # force_stop이 True이면 현재 말하고 있는 것을 중지
            if force_stop and current_process and current_process.poll() is None:
                current_process.terminate()
                current_process = None

            print(f"[TTS 발화 시작] {text} (강제종료: {force_stop}, 후속: {is_follow_up})")
            try:
                # Windows 전용 (주석해제)
                speaker.Volume = self.volume
                flags = 2 if force_stop else 0
                speaker.Speak(text, flags)

                # Mac용: say 명령어 사용
                # 볼륨은 0~100을 0~1로 변환
                # volume_normalized = self.volume / 100.0
                # current_process = subprocess.Popen(
                #     ['say', '-v', 'Yuna', '-r', '200', text],
                #     stdout=subprocess.DEVNULL,
                #     stderr=subprocess.DEVNULL
                # )
                # current_process.wait() # 여기까지 주석처리

            except Exception as e:
                print(f"[TTS 오류] {e}")
            print(f"[TTS 발화 완료] {text}")

            self.speech_queue.task_done()

    def _stt_worker(self):
        """마이크 소리를 듣고 명령어를 인식하는 스레드 (엔터 키 방식)"""
        # Whisper
        import tempfile
        import wave

        p = pyaudio.PyAudio()
        stream = p.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1024)

        print("🎙️ 음성 인식 준비 완료.")
        print("📢 [엔터]를 누르면 3초간 녹음을 시작합니다.")

        while True:
            # 엔터 입력 대기
            input("🎤 녹음하려면 [엔터]를 누르세요...")

            print("🔴 녹음 중... (3초)")

            # 3초간 녹음
            frames = []
            for _ in range(0, int(16000 / 1024 * 3)):
                data = stream.read(1024, exception_on_overflow=False)
                frames.append(data)

            print("⌛ 인식 중...")

            # 임시 파일로 저장
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wf = wave.open(f.name, 'wb')
                wf.setnchannels(1)
                wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
                wf.setframerate(16000)
                wf.writeframes(b''.join(frames))
                wf.close()

                # Whisper로 인식
                # fp16=False: CPU에서 FP16 미지원 경고 제거
                result = self.whisper_model.transcribe(f.name, language="ko", fp16=False)
                text = result["text"].replace(" ", "")

                os.unlink(f.name)

            if not text:
                print("❌ 인식된 음성이 없습니다.")
                continue

            print(f"✅ 음성 인식 결과: {text}")
            self.handle_command(text)

    def handle_command(self, text):
        """음성 인식을 통해 들어온 텍스트를 분석하여 명령 수행"""
        # 명령어 판별 (공백 제거 후 비교)
        text = text.replace(" ", "")
        response_text = None  # 웹 모드용 응답 텍스트

        if "종료" in text or "종료해" in text or "종료해줘" in text or "시스템 종료" in text:
            response_text = "시스템을 종료합니다."
            self.speak(response_text, force_stop=True)
            self.running = False
        elif "시작" in text or "시작해줘" in text or "실행" in text or "실행해줘" in text:
            response_text = "시스템을 시작합니다."
            self.speak(response_text, force_stop=True)
        elif "조용히해" in text or "정지해" in text or "중지해" in text or "음소거" in text or "음소거해줘" in text:
            self.is_muted = True
            response_text = "음성 안내를 일시 정지합니다."
            self.speak(response_text, force_stop=True)
        elif "말해줘" in text or "다시말해" in text or "다시말해줘" in text or "음성안내시작" in text or "음성안내해줘" in text:
            self.is_muted = False
            response_text = "음성 안내를 시작합니다."
            self.speak(response_text)

        return response_text  # 웹 모드에서 클라이언트로 전달

    def speak(self, text, force_stop=False, is_follow_up=False):
        """안내 문구를 큐에 추가 (비동기)"""
        # 변경: 웹 모드일 때는 서버에서 TTS를 실행하지 않음 (클라이언트에서 처리)
        if self.web_mode:
            return

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
    
    # 모바일 카메라 및 음성
    def _generate_web_llm_async(self, entity_key, label, meters, pos_desc):
        """웹 모드용 비동기 LLM 생성 (별도 스레드에서 실행)"""
        try:
            # LLM으로 자연스러운 문장 생성
            explanation = self.follow_up_mgr.service.generate_explanation(label, meters, pos_desc)

            # 실패 시 기본 문장
            if not explanation:
                explanation = f"{pos_desc} {meters:.1f}미터에 {label}이 있으니 주의하세요."

            # 캐시에 저장
            with self.cache_lock:
                self.web_speech_cache[entity_key] = explanation
                print(f"[Web LLM] 캐시 저장: {entity_key} -> {explanation}")
        except Exception as e:
            print(f"[Web LLM] 오류: {e}")

    def process_web_frame(self, frame):
        """웹에서 전송된 프레임을 처리하고 결과 이미지 반환"""
        processed, _ = self.process_web_frame_with_speech(frame)
        return processed

    def process_web_frame_with_speech(self, frame):
        """웹에서 전송된 프레임을 처리하고 결과 이미지와 음성 텍스트 반환"""
        self.frame_count += 1
        current_time = time.time()
        speech_text = None

        # YOLO 및 깊이 추정
        if self.frame_count % self.frame_skip == 1 or self.last_depth_map is None:
            self.last_objects = self.stage2_yolo_optimized(frame)
            self.last_depth_map, self.last_depth_viz = self.stage3_depth_optimized(frame)

        display_frame = frame.copy()
        h, w = frame.shape[:2]
        roi_left = int(w * self.roi_x_min)
        roi_right = int(w * self.roi_x_max)

        # ROI 가이드 라인
        cv2.line(display_frame, (roi_left, 0), (roi_left, h), (0, 0, 255), 2)
        cv2.line(display_frame, (roi_right, 0), (roi_right, h), (0, 0, 255), 2)

        # 가장 가까운 물체 찾기
        closest_obj = None
        min_meters = float('inf')

        for obj in self.last_objects:
            b = obj['box']
            cx = (b[0] + b[2]) // 2
            cy = int(b[3] * 0.9)

            if roi_left <= cx <= roi_right:
                h_d, w_d = self.last_depth_map.shape
                cx_d, cy_d = max(0, min(cx, w_d-1)), max(0, min(cy, h_d-1))
                raw_val = self.last_depth_map[cy_d, cx_d]
                meters = self.raw_to_meters(raw_val)

                if meters < min_meters:
                    min_meters = meters
                    closest_obj = {
                        'label': obj['label'],
                        'box': b,
                        'meters': meters,
                        'cx': cx
                    }

        # 감지된 객체 데이터 (클라이언트 렌더링용)
        detection_data = None

        current_entities = set()
        if closest_obj and min_meters < 10.0:
            b = closest_obj['box']
            label_name = closest_obj['label']
            meters = closest_obj['meters']

            dist_bin = int(meters / self.DIST_BIN_SIZE)
            pos_bin = int((closest_obj['cx'] / w) / self.POS_BIN_SIZE)
            entity_key = (label_name, dist_bin, pos_bin)
            current_entities.add(entity_key)

            # 클라이언트 렌더링용 데이터 (numpy → Python 기본 타입 변환)
            detection_data = {
                'box': [int(b[0]), int(b[1]), int(b[2]), int(b[3])],
                'label': str(label_name),
                'distance': float(round(meters, 1)),
                'roi': [int(roi_left), int(roi_right)]
            }

            # 서버 시각화 (노트북 모드용)
            cv2.rectangle(display_frame, (b[0], b[1]), (b[2], b[3]), (0, 0, 255), 3)
            cv2.putText(display_frame, f"{label_name} {meters:.1f}m", (b[0], b[1]-10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

            # --- Rapid Approach Detection (Independent of Cooldown) ---
            if entity_key in self.entity_velocity_history:
                last_dist, last_time = self.entity_velocity_history[entity_key]
                dt = current_time - last_time
                if dt > 0:
                    approach_speed = (last_dist - meters) / dt # m/s
                    if meters < self.APPROACH_THRESHOLD_DIST and approach_speed >= self.APPROACH_THRESHOLD_SPEED:
                        warning_msg = f"위험! {label_name}이 매우 빠르게 접근 중입니다."
                        print(f"[RapidApproach] Speed: {approach_speed:.2f} m/s, Dist: {meters:.1f}m. Triggering Warning.")
                        # 변경: 웹 모드일 때는 speech_text로 반환, 아니면 speak 호출
                        if self.web_mode:
                            speech_text = warning_msg
                        else:
                            self.speak(warning_msg, force_stop=True)
            
            # Update velocity history every frame for tracking
            self.entity_velocity_history[entity_key] = (meters, current_time)

            # 음성 안내 (라벨 쿨다운 체크, 뮤트 상태 체크)
            # 라벨별 쿨다운 체크
            can_announce = True
            if label_name in self.label_cooldown:
                if (current_time - self.label_cooldown[label_name]) < self.cooldown_time:
                    can_announce = False  # 쿨다운 중

            # 변경: LLM 생성 시작 (쿨다운 갱신은 실제 재생할 때만)
            if can_announce and not self.is_muted:
                # label_cooldown 갱신은 하지 않음 (실제 재생할 때 갱신)
                pos_desc = "정면"
                if closest_obj['cx'] < roi_left + (roi_right - roi_left) * 0.3:
                    pos_desc = "왼쪽"
                elif closest_obj['cx'] > roi_left + (roi_right - roi_left) * 0.7:
                    pos_desc = "오른쪽"

                # 비동기 LLM 생성 시작 (별도 스레드)
                llm_thread = threading.Thread(
                    target=self._generate_web_llm_async,
                    args=(entity_key, label_name, meters, pos_desc),
                    daemon=True
                )
                llm_thread.start()

            # 변경: 라벨 쿨다운을 활용하여 중복 재생 방지
            # entity_key는 거리 변화에 따라 바뀌므로, label_name 기준으로 체크
            can_play = True
            if label_name in self.label_cooldown:
                # 마지막 재생 후 쿨다운 시간이 지나지 않았으면 재생 안 함
                if (current_time - self.label_cooldown[label_name]) < self.cooldown_time:
                    can_play = False

            if not self.is_muted and can_play:
                with self.cache_lock:
                    if entity_key in self.web_speech_cache:
                        speech_text = self.web_speech_cache[entity_key]
                        # 재생했으므로 announced_objects에 추가
                        self.announced_objects[entity_key] = current_time
                        # 라벨 쿨다운 갱신 (중복 재생 방지)
                        self.label_cooldown[label_name] = current_time
                        print(f"[Web TTS] 캐시에서 음성 재생: {speech_text}")
                    else:
                        # 캐시 없으면 기본 텍스트 (LLM 생성 중)
                        pos_desc = "정면"
                        if closest_obj['cx'] < roi_left + (roi_right - roi_left) * 0.3:
                            pos_desc = "왼쪽"
                        elif closest_obj['cx'] > roi_left + (roi_right - roi_left) * 0.7:
                            pos_desc = "오른쪽"
                        speech_text = f"{pos_desc} {meters:.1f}미터에 {label_name}"
                        # 기본 텍스트도 재생했으므로 쿨다운 갱신
                        self.label_cooldown[label_name] = current_time
                        print(f"[Web TTS] 기본 음성 재생: {speech_text}")

        # 오래된 객체 정리
        for entity_key in list(self.announced_objects.keys()):
            if entity_key not in current_entities:
                if current_time - self.announced_objects[entity_key] > self.announce_timeout:
                    del self.announced_objects[entity_key]
                    # Also clean up velocity history
                    if entity_key in self.entity_velocity_history:
                        del self.entity_velocity_history[entity_key]
                    # 캐시도 삭제
                    with self.cache_lock:
                        if entity_key in self.web_speech_cache:
                            del self.web_speech_cache[entity_key]
                            print(f"[Web LLM] 캐시 삭제: {entity_key}")

        return display_frame, speech_text, detection_data

    def run(self, headless=False):
        """
        headless=True: GUI 없이 실행 (Mac 웹 모드용)
        headless=False: GUI 창 표시 (Windows 또는 로컬 실행용)
        """
        # 먼저 내장 카메라(0) 시도, 실패하면 외장 카메라(1) 시도
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("내장 카메라를 열 수 없습니다. 외장 카메라를 시도합니다...")
            cap = cv2.VideoCapture(1)
            if not cap.isOpened():
                print("카메라를 열 수 없습니다.")
                print("Windows 설정에서 카메라 권한을 확인해주세요:")
                print("설정 > 개인정보 > 카메라 > 앱이 카메라에 액세스하도록 허용")
                return

        window_name_main = "MVP Test - Color (YOLO)"
        window_name_depth = "MVP Test - Depth (MiDaS)"

        # GUI 모드일 때만 창 생성
        if not headless:
            cv2.namedWindow(window_name_main)
            cv2.namedWindow(window_name_depth)

        print("\n=== 음성 안내(TTS)가 최적화된 MVP 파이프라인 시작 ===")

        # 시작 시 안내 음성 (웹 모드가 아닐 때만)
        if not self.web_mode:
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

                # --- Rapid Approach Detection (Independent of Cooldown) ---
                if entity_key in self.entity_velocity_history:
                    last_dist, last_time = self.entity_velocity_history[entity_key]
                    dt = current_time - last_time
                    if dt > 0:
                        approach_speed = (last_dist - meters) / dt
                        if meters < self.APPROACH_THRESHOLD_DIST and approach_speed >= self.APPROACH_THRESHOLD_SPEED:
                            warning_msg = f"위험! {label_name}이 매우 빠르게 접근 중입니다."
                            print(f"[RapidApproach] Speed: {approach_speed:.2f} m/s, Dist: {meters:.1f}m. Triggering Warning.")
                            self.speak(warning_msg, force_stop=True)
                
                self.entity_velocity_history[entity_key] = (meters, current_time)

                # --- 음성 안내 로직 (라벨 쿨다운 체크) ---
                # 라벨별 쿨다운 체크
                can_announce = True
                if label_name in self.label_cooldown:
                    if (current_time - self.label_cooldown[label_name]) < self.cooldown_time:
                        can_announce = False  # 쿨다운 중

                if can_announce:
                    # Mark as announced immediately to prevent duplicate triggers
                    self.announced_objects[entity_key] = current_time
                    self.label_cooldown[label_name] = current_time  # 쿨다운 시작

                    # Determine position description
                    pos_desc = "정면"
                    if closest_obj['cx'] < roi_left + (roi_right - roi_left) * 0.3:
                        pos_desc = "왼쪽"
                    elif closest_obj['cx'] > roi_left + (roi_right - roi_left) * 0.7:
                        pos_desc = "오른쪽"

                    # 즉시 안내 (모든 객체)
                    immediate_msg = f"{pos_desc} {meters:.1f}미터에 {label_name}"
                    self.speak(immediate_msg)

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
                        # Also clean up velocity history
                        if entity_key in self.entity_velocity_history:
                            del self.entity_velocity_history[entity_key]
                        # 만약 사라진 물체에 대한 후속 안내가 예약되어 있다면 취소
                        self.follow_up_mgr.cancel_pending()

            if should_log:
                last_log_time = current_time

            # 웹 스트리밍용으로 현재 프레임 저장
            with self.frame_lock:
                self.last_web_frame = display_frame.copy()

            # GUI 모드일 때만 화면 표시
            if not headless:
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
            else:
                # headless 모드에서는 CPU 부하 방지를 위해 짧은 대기
                time.sleep(0.03)

        cap.release()
        if not headless:
            cv2.destroyAllWindows()

if __name__ == "__main__":
    pipeline = MVPTestPipeline()
    pipeline.run()
