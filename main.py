import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import Provider
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star import star_map


INPUT_MATCH = "textadventure_input"
STORY_SESSION = "textadventure_story_session"
START = "开始冒险"
END = "结束冒险"
FORCE_END = "强制结束冒险"
HELP = "冒险帮助"
ADMIN_END = "admin end"


def player_key(event: AstrMessageEvent) -> tuple[str, str]:
    return event.unified_msg_origin, event.get_sender_id()


class AdventureSession:
    def __init__(self, provider: Provider, history: list[dict[str, str]]):
        self.provider = provider
        self.history = history
        self.ready = False
        self.closed = False
        self.turn_lock = asyncio.Lock()
        self.pending: asyncio.Task | None = None
        self.timer: asyncio.Task | None = None

    async def run(self, work):
        self.pending = asyncio.create_task(work)
        try:
            return await self.pending
        except asyncio.CancelledError:
            if not self.closed:
                raise
        finally:
            self.pending = None

    def cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def close(self) -> None:
        self.closed = True
        self.cancel_timer()
        if self.pending is not None and not self.pending.done():
            self.pending.cancel()


class AdventureInputFilter(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        metadata = star_map.get(__name__)
        if metadata is None or metadata.star_cls is None:
            return False
        return metadata.star_cls.match_input(event)


@register("textadventure", "YourName", "一个由 LLM 驱动的动态文字冒险小游戏", "1.4.1")
class TextAdventurePlugin(Star):
    """
    一个由LLM驱动的图文动态文字冒险游戏插件。
    剧情通过 AstrBot 的回复流程发送，可由输出插件按配置处理。
    核心参数（如超时时间、系统提示词）均可通过配置文件修改。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 从配置文件读取设置，并提供默认值
        self.default_adventure_theme = self.config.get("default_adventure_theme", "奇幻世界")
        self.session_timeout = max(0, self.config.get("session_timeout", 300))
        self.trigger_prefix = self.config.get("trigger_prefix", "/").strip() or "/"
        self.require_action_prefix = self.config.get("require_action_prefix", False)
        self.system_prompt_template = self.config.get(
            "system_prompt_template",
            "你是一位经验丰富的文字冒险游戏主持人(Game Master)。你将在一个'{game_theme}'主题下，根据玩家的行动实时生成独特且逻辑连贯的故事情节。"
            "你的目标是创造一个引人入胜、充满未知的故事。你的回复应包含：\n"
            "1. 对场景的生动描述。\n"
            "2. 玩家的当前状况。\n"
            "3. 引导玩家思考下一步行动，可以给出几个选项（例如：A. ... B. ...），或直接鼓励玩家自由探索。\n"
            "请确保故事风格一致，并避免重复。保持回复在200-300字左右，以便渲染为图片。"
        )

        # 增加日志记录，方便调试，确认配置是否加载成功
        logger.info("--- TextAdventurePlugin 配置加载 ---")
        logger.info(f"默认主题: {self.default_adventure_theme}")
        logger.info(f"会话超时: {self.timeout_hint}")
        logger.info(f"系统提示词模板: {self.system_prompt_template[:70]}...")
        logger.info("------------------------------------")

        self.active_game_sessions: dict[tuple[str, str], AdventureSession] = {}

    @property
    def action_example(self) -> str:
        prefix = self.trigger_prefix if self.require_action_prefix else ""
        return f"{prefix}检查宝箱"

    @property
    def timeout_hint(self) -> str:
        if self.session_timeout:
            return f"每回合有 {self.session_timeout} 秒的行动时间。"
        return "已关闭行动超时，可随时继续。"

    def match_input(self, event: AstrMessageEvent) -> bool:
        # WakingCheck may have removed AstrBot's prefix from event.message_str.
        text = event.get_message_str().strip()
        raw_text = (getattr(event.message_obj, "message_str", "") or text).strip()
        has_prefix = raw_text.startswith(self.trigger_prefix)
        action = raw_text[len(self.trigger_prefix):].strip() if has_prefix else text
        if not action:
            return False
        command, *args = action.split(maxsplit=1)

        if has_prefix:
            if command == START:
                event.set_extra(INPUT_MATCH, (START, args[0] if args else "", None))
                return True
            if action in {END, FORCE_END, HELP, ADMIN_END}:
                event.set_extra(INPUT_MATCH, (action, "", None))
                return True

        if command in {START, END, FORCE_END, HELP} or action == ADMIN_END:
            return False
        if self.require_action_prefix and not has_prefix:
            return False
        if raw_text.startswith("/") and not has_prefix:
            return False

        session = self.active_game_sessions.get(player_key(event))
        if session is None or not session.ready:
            return False
        event.set_extra(INPUT_MATCH, ("action", action, session))
        return True

    @filter.custom_filter(AdventureInputFilter, priority=-1)
    async def handle_adventure_input(self, event: AstrMessageEvent):
        kind, text, session = event.get_extra(INPUT_MATCH)
        if kind == "action":
            if not event.get_extra("handlers_parsed_params"):
                async for result in self.play_turn(event, text, session):
                    yield result
            return

        handler = {
            START: self.start_game,
            END: self.end_adventure,
            FORCE_END: self.end_adventure,
            HELP: self.cmd_adventure_help,
            ADMIN_END: self.cmd_admin_end_all_games,
        }[kind]
        results = handler(event, text) if kind == START else handler(event)
        async for result in results:
            yield result

    def arm_timeout(self, event: AstrMessageEvent, session: AdventureSession) -> None:
        if not self.session_timeout or session.closed:
            return

        async def expire():
            await asyncio.sleep(self.session_timeout)
            session.timer = None
            if self.remove_session(player_key(event), session):
                try:
                    await event.send(event.plain_result(
                        f"⏱️ 冒险超时，已自动结束。发送 {self.trigger_prefix}{START} 开启新的旅程。"
                    ))
                except Exception:
                    logger.exception("发送冒险超时提示失败")

        session.timer = asyncio.create_task(expire())

    def remove_session(self, key: tuple[str, str], session: AdventureSession) -> bool:
        if self.active_game_sessions.get(key) is not session:
            return False
        del self.active_game_sessions[key]
        session.close()
        return True

    @filter.on_decorating_result(priority=-1000)
    async def discard_closed_story(self, event: AstrMessageEvent):
        session = event.get_extra(STORY_SESSION)
        if session is not None and session.closed:
            event.clear_result()
            event.stop_event()

    async def make_story_result(self, event: AstrMessageEvent, session: AdventureSession, prompt: str):
        response = await session.provider.text_chat(
            prompt=prompt, session_id=event.get_session_id(), contexts=session.history,
        )
        if session.closed:
            return
        story = response.completion_text
        session.history.extend([
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": story},
        ])
        event.set_extra(STORY_SESSION, session)
        return event.plain_result(
            f"{story}\n\n[行动示例：{self.action_example}] (玩家ID: {event.get_sender_id()})"
        )

    async def play_turn(self, event: AstrMessageEvent, action: str, session: AdventureSession):
        async with session.turn_lock:
            if session.closed:
                return
            session.cancel_timer()

            async def respond():
                await event.send(event.plain_result("AI正在构思下一幕...请稍等片刻..."))
                if session.closed:
                    return
                return await self.make_story_result(event, session, action)

            try:
                story = await session.run(respond())
                if story is not None and not session.closed:
                    yield story
            except asyncio.CancelledError:
                self.remove_session(player_key(event), session)
                raise
            except Exception:
                if self.remove_session(player_key(event), session):
                    logger.exception("推进文字冒险失败")
                    await event.send(event.plain_result("冒险过程中发生错误，游戏已结束。"))
            finally:
                self.arm_timeout(event, session)
                event.stop_event()

    @filter.command(START)
    async def start_adventure(self, event: AstrMessageEvent):
        """开始一场动态文字冒险游戏。"""
        parts = event.get_message_str().split(maxsplit=1)
        theme = parts[1] if len(parts) == 2 else ""
        async for result in self.start_game(event, theme):
            yield result

    async def start_game(self, event: AstrMessageEvent, theme: str):
        key = player_key(event)
        if key in self.active_game_sessions:
            yield event.plain_result(
                f"你已经有正在进行的冒险。行动示例：{self.action_example}。"
                f"发送 {self.trigger_prefix}{END} 可退出。"
            ).stop_event()
            return

        provider_id = (self.config.get("llm_provider_id") or "").strip()
        if provider_id:
            llm_provider = self.context.get_provider_by_id(provider_id)
            if not isinstance(llm_provider, Provider):
                yield event.plain_result(
                    "所选的冒险模型不可用。请在插件配置中重新选择已启用的聊天模型，"
                    "或清空此项以使用 AstrBot 默认聊天模型。"
                ).stop_event()
                return
        else:
            llm_provider = self.context.get_using_provider()
            if llm_provider is None:
                yield event.plain_result(
                    "当前没有可用的 LLM 服务来开始冒险，请联系管理员。"
                ).stop_event()
                return

        game_theme = theme.strip() or self.default_adventure_theme
        try:
            system_prompt = self.system_prompt_template.format(game_theme=game_theme)
        except (KeyError, IndexError, ValueError):
            logger.error("系统提示词模板格式错误，将使用默认模板。")
            system_prompt = (
                f"你是一位经验丰富的文字冒险游戏主持人。请根据玩家的行动，"
                f"在「{game_theme}」主题下实时生成连贯的故事情节。"
            )

        session = AdventureSession(llm_provider, [
            {"role": "system", "content": system_prompt},
        ])
        self.active_game_sessions[key] = session

        async def open_adventure():
            await event.send(event.plain_result(
                "📜 动态文字冒险\n\n"
                "故事由 AI 实时生成，仅供娱乐。\n"
                f"行动示例：{self.action_example}。{self.timeout_hint}\n"
                f"发送 {self.trigger_prefix}{END} 可以退出。\n\n"
                "正在构筑你的冒险世界，请稍候……"
            ))
            if not session.closed:
                return await self.make_story_result(event, session, "故事开始了，我的第一个场景是什么？")

        story = None
        try:
            story = await session.run(open_adventure())
            if story is not None and not session.closed:
                yield story
        except asyncio.CancelledError:
            self.remove_session(key, session)
            raise
        except Exception:
            if self.remove_session(key, session):
                logger.exception("开始文字冒险失败")
                yield event.plain_result("无法开始冒险，请稍后重试。").stop_event()
        finally:
            if story is not None and not session.closed:
                session.ready = True
                self.arm_timeout(event, session)
            event.stop_event()

    @filter.command(END, alias={FORCE_END})
    async def end_adventure(self, event: AstrMessageEvent):
        """立即结束当前会话里的冒险。"""
        session = self.active_game_sessions.pop(player_key(event), None)
        if session is None:
            yield event.plain_result("你当前没有正在进行的冒险。").stop_event()
            return

        session.close()
        yield event.plain_result(
            f"✅ 冒险已结束。可以继续正常聊天，或发送 {self.trigger_prefix}{START} 开启新的旅程。"
        ).stop_event()

    def close_all_sessions(self) -> int:
        stopped_count = len(self.active_game_sessions)
        for session in self.active_game_sessions.values():
            session.close()
        self.active_game_sessions.clear()
        return stopped_count

    @filter.command(ADMIN_END)
    async def cmd_admin_end_all_games(self, event: AstrMessageEvent):
        """管理员命令：立即结束所有正在进行的文字冒险。"""
        if not event.is_admin():
            yield event.plain_result("❌ 权限不足，只有管理员可操作此命令。").stop_event()
            return

        stopped_count = self.close_all_sessions()
        if not stopped_count:
            yield event.plain_result("当前没有活跃的文字冒险游戏。").stop_event()
            return

        yield event.plain_result(
            f"✅ 已结束全部 {stopped_count} 个活跃的文字冒险。"
        ).stop_event()

    @filter.command(HELP)
    async def cmd_adventure_help(self, event: AstrMessageEvent):
        """显示文字冒险插件的用法。"""
        prefix = self.trigger_prefix
        yield event.plain_result(
            "📜 动态文字冒险帮助手册\n\n"
            f"- `{prefix}{START} [可选主题]` 开始一场新冒险。\n"
            f"- 冒险中发送「{self.action_example}」推进剧情。{self.timeout_hint}\n"
            f"- `{prefix}{END}` 或 `{prefix}{FORCE_END}` 立即结束当前冒险。\n"
            f"- `{prefix}{ADMIN_END}` 由管理员结束所有冒险。"
        ).stop_event()

    async def terminate(self):
        """插件卸载时结束所有冒险。"""
        self.close_all_sessions()
