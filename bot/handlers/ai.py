"""Диалог с ИИ-консультантом.

Агент один на процесс и создаётся лениво: настройки читаются при старте, а
провайдер поднимать без ключа незачем. Если AI_PROVIDER пуст, роутер
подключён, но ни один хендлер не сработает — кнопки в меню нет, а /ai честно
скажет, что консультант выключен.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ai.agent import Agent
from bot.ai.provider import build_provider
from bot.ai.tools import ToolContext
from bot.config import settings
from bot.keyboards.common import main_menu
from bot.states import AiChat

log = logging.getLogger(__name__)
router = Router(name="ai")

EXIT_TEXT = "⬅️ Выйти из чата"
_agent: Agent | None = None


def get_agent() -> Agent | None:
    global _agent
    if _agent is None:
        llm = build_provider(
            settings.ai_provider,
            credentials=settings.gigachat_credentials,
            model=settings.gigachat_model,
            verify_ssl=settings.gigachat_verify_ssl,
        )
        if llm is None:
            return None
        _agent = Agent(llm)
    return _agent


def set_agent(agent: Agent | None) -> None:
    """Для тестов: подменить агента на фейковый провайдер."""
    global _agent
    _agent = agent


def chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=EXIT_TEXT)]], resize_keyboard=True
    )


@router.message(Command("ai"))
@router.message(F.text == "🤖 Консультант")
async def start_chat(message: Message, state: FSMContext) -> None:
    agent = get_agent()
    if agent is None:
        await message.answer("Консультант пока выключен.")
        return
    agent.reset(message.from_user.id)
    await state.set_state(AiChat.talking)
    await message.answer(
        "Я консультант магазина: помогу подобрать товар, расскажу о наличии, "
        "положу в корзину. Спрашивайте.",
        reply_markup=chat_kb(),
    )


@router.message(StateFilter(AiChat.talking), F.text == EXIT_TEXT)
async def exit_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    agent = get_agent()
    if agent is not None:
        agent.reset(message.from_user.id)
    await message.answer("Вернул в меню.", reply_markup=main_menu(message.from_user.id in settings.admins))


@router.message(StateFilter(AiChat.talking), F.text)
async def talk(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    agent = get_agent()
    if agent is None:
        await state.clear()
        await message.answer("Консультант выключен.", reply_markup=main_menu())
        return
    # Ответ модели идёт секунды; «печатает…» — чтобы клиент не решил, что бот умер.
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    ctx = ToolContext(
        session=session,
        bot=bot,
        user_id=message.from_user.id,
        user_name=message.from_user.full_name or "",
    )
    reply = await agent.ask(ctx, message.text)
    # Модель пишет свободным текстом: символы «<» и «&» в её ответе уронят
    # parse_mode=HTML, поэтому шлём без разметки.
    await message.answer(reply, parse_mode=None, reply_markup=chat_kb())
