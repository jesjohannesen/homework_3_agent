"""OpenRouter chat client. Text in, JSON out. The model gets NO tools -- it can only propose text."""
import json
import time

from .net import NetError, TransportError, http, retry


class LLMError(Exception):
    pass


def extract_json(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise LLMError("no JSON object in model output")
    try:
        return json.loads(text[a:b + 1])
    except ValueError as e:
        raise LLMError(f"bad JSON from model: {e}")


class LLM:
    def __init__(self, base, key, model, *, sleep=time.sleep, timeout=90, log=None):
        self.base, self._key, self.model, self.sleep, self.timeout = base.rstrip("/"), key, model, sleep, timeout
        self.log = log or (lambda *a, **k: None)
        self.last_usage = {}

    def _chat(self, messages, temperature, max_tokens):
        body = json.dumps({"model": self.model, "messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens, "response_format": {"type": "json_object"}}).encode()
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "X-Title": "HW3 agent"}

        def once():
            try:
                status, _, raw = http("POST", self.base + "/chat/completions", headers, body, self.timeout)
            except TransportError as e:
                raise NetError(f"llm transport: {e}", retryable=True)
            if status == 429 or status >= 500:
                raise NetError(f"llm HTTP {status}", status, retryable=True)
            if status >= 400:
                raise NetError(f"llm HTTP {status}", status)
            try:
                d = json.loads(raw)
                self.last_usage = d.get("usage", {})
                return d["choices"][0]["message"].get("content") or ""
            except (ValueError, KeyError, IndexError):
                raise NetError("llm malformed response", retryable=True)

        return retry(once, attempts=4, sleep=self.sleep, on_retry=lambda n, d, e: self.log("llm_retry", n=n, why=str(e)))

    def chat_json(self, system, user, temperature=0.8, max_tokens=2000, tries=2):
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for i in range(tries):
            try:
                return extract_json(self._chat(msgs, temperature, max_tokens))
            except LLMError as e:
                self.log("llm_bad_output", try_=i + 1, why=str(e))
        raise LLMError("model never returned valid JSON")
