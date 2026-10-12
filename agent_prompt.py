"""Reuse the frame/prompt workflow with account-authenticated platform transports."""
import json
from openai_prompt import OpenAIPromptClient, RESPONSES_URL, TRANSCRIPTIONS_URL
from agent_platforms import PlatformError


class AgentPromptClient(OpenAIPromptClient):
    def __init__(self, session, model, *, transcription_api_key=""):
        self.session, self.model = session, model
        self.chatgpt_plan = False
        self.api_key = ""
        self.transcription_api_key = transcription_api_key

    def _post(self, url, data, content_type, timeout=180, credential=None):
        if url == RESPONSES_URL:
            draft = self.session.complete(json.loads(data))
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(draft, ensure_ascii=False)}]}]}
        if url == TRANSCRIPTIONS_URL and self.transcription_api_key:
            return super()._post(url, data, content_type, timeout, credential=self.transcription_api_key)
        raise PlatformError("该平台调用不支持此操作。")
