import time
import threading
from test_pipeline import MVPTestPipeline

class MockPipeline(MVPTestPipeline):
    def __init__(self):
        # Skip heavy model loading for logic test
        self.speech_queue = __import__('queue').Queue()
        self.volume = 100
        self.is_muted = False
        self.announced_objects = {}
        self.announce_timeout = 5.0
        
        # We need FollowUpManager
        from test_pipeline import FollowUpManager
        self.follow_up_mgr = FollowUpManager(self)
        
        # Mock TTS worker that just prints
        self.tts_thread = threading.Thread(target=self._mock_tts_worker, daemon=True)
        self.tts_thread.start()

    def _mock_tts_worker(self):
        while True:
            item = self.speech_queue.get()
            if item is None: break
            text, force_stop, is_follow_up = item
            print(f">>> [TTS] Playing: {text} | FollowUp? {is_follow_up}")
            self.speech_queue.task_done()

def test_logic():
    print("--- Starting Two-Layer Speech Logic Test ---")
    pipeline = MockPipeline()
    
    # Simulate first detection
    print("\n1. Simulating detection: '사람' at 2.0m")
    pipeline.speak("사람 주의.")
    pipeline.follow_up_mgr.schedule_follow_up("사람", 2.0, "정면")
    
    time.sleep(0.5)
    
    # Simulate second detection (Cancellation test)
    print("\n2. New detection before previous follow-up: '자동차' at 5.0m")
    pipeline.speak("자동차 주의.", force_stop=True)
    pipeline.follow_up_mgr.schedule_follow_up("자동차", 5.0, "약간 오른쪽")
    
    # Wait for follow-up (1.5s delay + mock LLM time)
    print("\n3. Waiting for follow-up...")
    time.sleep(4.0)
    
    print("\n--- Test Finished ---")

if __name__ == "__main__":
    test_logic()
