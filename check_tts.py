import pyttsx3

def test_tts():
    print("TTS 엔진 초기화 중...")
    try:
        engine = pyttsx3.init()
        
        # 사용 가능한 목소리 목록 출력
        voices = engine.getProperty('voices')
        print(f"\n[시스템 정보] 발견된 목소리 수: {len(voices)}")
        
        for i, voice in enumerate(voices):
            print(f"목소리 #{i}: {voice.name} (언어: {voice.languages})")
        
        # 테스트 문구
        test_text = "안녕하세요. 시각장애인 보조 시스템 테스트입니다. 소리가 들리시나요?"
        print(f"\n[테스트] 다음 문장을 읽습니다: '{test_text}'")
        
        engine.say(test_text)
        engine.runAndWait()
        print("\n[성공] 음성 출력이 완료되었습니다.")
        
    except Exception as e:
        print(f"\n[오류] TTS 작동 중 문제 발생: {e}")

if __name__ == "__main__":
    test_tts()
