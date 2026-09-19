"""Слой над LLM-провайдером: один метод, один формат ответа.

Провайдер меняется строкой в .env, а всё остальное — инструменты, цикл агента,
обработчики — про конкретный API не знает. Так тесты идут на FakeProvider без
сети, а GPT/Claude через ProxyAPI подключаются ещё одним классом на 40 строк.

Формат сообщений — как у GigaChat/OpenAI: список dict с role/content, для
результата функции role="function" и name. Единый формат держим свой, а не
SDK-шный, иначе смена провайдера потянет за собой agent.py.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

Message = dict[str, Any]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Reply:
    """Либо текст для пользователя, либо просьба вызвать инструмент — не оба."""

    text: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    # Непрозрачные данные провайдера, которые надо вернуть ему с вызовом
    # (Gemini 3: thoughtSignature). Агент их не трактует, только протаскивает.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_name)


class LLM(Protocol):
    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Reply: ...


class GigaChatProvider:
    """Официальный SDK `gigachat`. Токен доступа SDK обновляет сам по ключу
    авторизации, поэтому здесь нет ни OAuth, ни таймеров.

    Клиент создаётся лениво: импорт SDK при старте бота без ключа ронял бы
    и тех, кому ИИ вообще не нужен.
    """

    def __init__(self, credentials: str, model: str, verify_ssl: bool = True,
                 timeout: float = 30.0) -> None:
        self._credentials = credentials
        self._model = model
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            from gigachat import GigaChat

            self._client = GigaChat(
                credentials=self._credentials,
                model=self._model,
                verify_ssl_certs=self._verify_ssl,
                timeout=self._timeout,
            )
        return self._client

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Reply:
        from gigachat.models import (
            ChatCompletionRequest,
            ChatFunctionSpecification,
            ChatMessage,
            ChatTool,
        )

        specs = [
            ChatFunctionSpecification(name=t.name, description=t.description, parameters=t.parameters)
            for t in tools
        ]
        request = ChatCompletionRequest(
            messages=[ChatMessage(**m) for m in messages],
            tools=[ChatTool(functions={"specifications": specs})] if specs else None,
            temperature=0.3,
        )
        response = await self._get_client().achat.create(request)
        message = response.messages[0]

        call = message.function_call
        if call is None:
            for part in message.content or []:
                if getattr(part, "function_call", None) is not None:
                    call = part.function_call
                    break
        if call is not None:
            args = call.arguments
            # SDK отдаёт arguments то dict'ом, то строкой JSON — зависит от модели.
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    log.warning("GigaChat прислал невалидные аргументы: %r", args)
                    args = {}
            return Reply(tool_name=call.name, tool_args=dict(args or {}))

        text = ""
        if isinstance(message.content, str):
            text = message.content
        else:
            text = "".join(getattr(p, "text", "") or "" for p in message.content or [])
        return Reply(text=text.strip())


class GeminiProvider:
    """Gemini по REST через httpx — без SDK намеренно: google-genai требует
    pydantic >= 2.12, aiogram 3.15 — < 2.10, вместе не живут. Для отладки
    поведения агента на живой модели; клиентам из РФ не предлагать (VPN, карта).

    Роли Gemini — user/model, результат функции идёт как functionResponse
    в реплике user; system — отдельным полем. Конвертируем из нашего формата
    на входе, чтобы agent.py об этом не знал.
    """

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, model: str = "gemini-3.5-flash-lite", timeout: float = 30.0) -> None:
        self._key = api_key
        self._model = model
        self._timeout = timeout

    @staticmethod
    def _convert(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
        system = ""
        contents: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                system = m["content"]
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif role == "assistant":
                call = m.get("function_call")
                if call:
                    part = {"functionCall": {"name": call["name"], "args": call["arguments"]}}
                    # Без подписи мысли Gemini 3 отвечает 400 на следующий запрос.
                    sig = (call.get("raw") or {}).get("thoughtSignature")
                    if sig:
                        part["thoughtSignature"] = sig
                else:
                    part = {"text": m["content"]}
                contents.append({"role": "model", "parts": [part]})
            elif role == "function":
                contents.append({"role": "user", "parts": [{"functionResponse": {
                    "name": m["name"], "response": {"result": m["content"]}}}]})
        return system, contents

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Reply:
        import httpx

        system, contents = self._convert(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": 0.3},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tools
            ]}]
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            # Ключ в заголовке, не в query: httpx печатает URL в исключениях и логах.
            r = await client.post(
                self.URL.format(model=self._model),
                headers={"x-goog-api-key": self._key},
                json=body,
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
        parts = r.json()["candidates"][0]["content"].get("parts", [])
        for p in parts:
            if "functionCall" in p:
                fc = p["functionCall"]
                raw = {"thoughtSignature": p["thoughtSignature"]} if p.get("thoughtSignature") else {}
                return Reply(tool_name=fc["name"], tool_args=dict(fc.get("args") or {}), raw=raw)
        return Reply(text="".join(p.get("text", "") for p in parts).strip())


class FakeProvider:
    """Провайдер по сценарию для тестов: отдаёт заранее заданные ответы по
    порядку и запоминает всё, что ему прислали, — по этому и проверяем, что
    результаты инструментов реально доходят до модели."""

    def __init__(self, script: list[Reply]) -> None:
        self._script = list(script)
        self.calls: list[list[Message]] = []
        self.tools_seen: list[int] = []

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Reply:
        self.calls.append([dict(m) for m in messages])
        self.tools_seen.append(len(tools))
        if not self._script:
            return Reply(text="(сценарий кончился)")
        return self._script.pop(0)


def build_provider(name: str, **kwargs: Any) -> LLM | None:
    """Пустое имя — фича выключена, бот работает как до неё."""
    name = (name or "").strip().lower()
    if not name:
        return None
    if name == "gigachat":
        if not kwargs.get("credentials"):
            raise ValueError("AI_PROVIDER=gigachat, но GIGACHAT_CREDENTIALS пуст")
        return GigaChatProvider(
            credentials=kwargs["credentials"],
            model=kwargs.get("model") or "GigaChat-2",
            verify_ssl=kwargs.get("verify_ssl", True),
        )
    if name == "gemini":
        if not kwargs.get("gemini_api_key"):
            raise ValueError("AI_PROVIDER=gemini, но GEMINI_API_KEY пуст")
        return GeminiProvider(api_key=kwargs["gemini_api_key"], model=kwargs.get("gemini_model") or "gemini-3.5-flash-lite")
    raise ValueError(f"Неизвестный AI_PROVIDER: {name}")
