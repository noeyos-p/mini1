import requests
import json

class FollowUpSpeechService:
    def __init__(self, api_key="YOUR_OPENROUTER_API_KEY"):
        self.api_key = api_key
        self.url = "https://openrouter.ai/api/v1/chat/completions"
        self.model = "deepseek/deepseek-r1-0528:free"

    def _sanitize_for_tts(self, text):
        """
        Sanitizes text for TTS:
        1) Takes only the first line
        2) Removes quotation marks
        3) Trims whitespace
        """
        if not text:
            return ""
        # First line only
        line = text.split('\n')[0]
        # Remove quotes
        line = line.replace('"', '').replace("'", "")
        return line.strip()

    def generate_explanation(self, object_label, distance, position_desc):
        """
        Generates a natural, calm follow-up explanation.
        Args:
            object_label (str): The detected object (e.g., '사람', '자동차').
            distance (float): Distance in meters.
            position_desc (str): Relative position (e.g., '정면', '약간 왼쪽').
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/google/antigravity", # Optional
        }

        # prompt design based on user requirements
        prompt = (
            f"Situation:\n"
            f"A visually impaired person is walking.\n"
            f"A {object_label} is located about {distance:.1f} meters in the {position_desc} direction.\n\n"
            f"Role:\n"
            f"Generate a walking assistance voice instruction.\n\n"
            f"Rules:\n"
            f"1. Output exactly ONE sentence.\n"
            f"2. Use Korean language only.\n"
            f"3. Do NOT repeat the immediate warning (e.g., \"{object_label} 주의\").\n"
            f"4. Do NOT output explanations, translations, reasoning, or meta comments.\n"
            f"5. Do NOT use asterisks (*), brackets, quotation marks, or English words.\n"
            f"6. Do NOT use emotional expressions, metaphors, or sound descriptions.\n"
            f"7. Use a calm, clear, and directive tone suitable for walking guidance.\n\n"
            f"Output format examples:\n"
            f"- {position_desc} {distance:.1f}미터에 {object_label}이 있으니 주의하세요.\n"
            f"- {position_desc} 쪽에 {object_label}이 있어 방향을 조금 조절해 주세요.\n\n"
            f"Output:"
        )

        data = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }

        try:
            print(f"[FollowUp] Requesting explanation for {object_label}...")
            response = requests.post(self.url, headers=headers, data=json.dumps(data), timeout=10)
            response.raise_for_status()
            
            result = response.json()
            raw_text = result['choices'][0]['message']['content'].strip()
            
            # Clean up potential thinking/reasoning tags if present in some experimental models
            if "</think>" in raw_text:
                raw_text = raw_text.split("</think>")[-1].strip()
            
            # Sanitization for TTS
            sanitized = self._sanitize_for_tts(raw_text)
            return sanitized
        except Exception as e:
            print(f"[FollowUp] Error calling DeepSeek: {e}")
            return None

if __name__ == "__main__":
    # Quick test if run directly
    service = FollowUpSpeechService() # Will fail without key, but for structure check
    # print(service.generate_explanation("사람", 2.5, "정면"))
