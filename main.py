import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)


def player_key(event: AstrMessageEvent) -> tuple[str, str]:
    return event.unified_msg_origin, event.get_sender_id()


class AdventureSession(SessionFilter):
    def __init__(self, start_event: AstrMessageEvent):
        self.start_event = start_event
        self.player = player_key(start_event)
        self.waiter_id = f"textadventure:{id(self)}"
        self.closed = False
        self.pending: asyncio.Task | None = None
        self.waiter: asyncio.Task | None = None

    def filter(self, event: AstrMessageEvent) -> str:
        # AstrBot first calls the filter with the start event to register the waiter.
        if event is self.start_event:
            return self.waiter_id
        if self.closed or player_key(event) != self.player:
            return ""

        # The waiter runs before command handlers. Let commands reach AstrBot.
        message = " ".join(event.get_message_str().split())
        command = message.partition(" ")[0]
        if (
            event.get_extra("handlers_parsed_params")
            or message.startswith("/")
            or command in {"开始冒险", "结束冒险", "强制结束冒险", "冒险帮助"}
            or message == "admin end"
        ):
            return ""
        return self.waiter_id

    async def run(self, work):
        self.pending = asyncio.create_task(work)
        try:
            return await self.pending
        except asyncio.CancelledError:
            if not self.closed:
                raise
        finally:
            self.pending = None

    def close(self) -> None:
        self.closed = True
        for task in (self.pending, self.waiter):
            if task is not None and not task.done():
                task.cancel()


@register("textadventure", "YourName", "一个将故事文字渲染为图片的动态冒险小游戏", "1.4.1")
class TextAdventurePlugin(Star):
    """
    一个由LLM驱动的图文动态文字冒险游戏插件。
    此版本将AI生成的故事文字直接渲染为图片进行展示，并包含健壮的会话管理。
    核心参数（如超时时间、系统提示词）均可通过配置文件修改。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 从配置文件读取设置，并提供默认值
        self.default_adventure_theme = self.config.get("default_adventure_theme", "奇幻世界")
        self.session_timeout = self.config.get("session_timeout", 300)
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
        logger.info(f"会话超时: {self.session_timeout} 秒")
        logger.info(f"系统提示词模板: {self.system_prompt_template[:70]}...")
        logger.info("------------------------------------")

        self.active_game_sessions: dict[tuple[str, str], AdventureSession] = {}

    @filter.command("开始冒险")
    async def start_adventure(self, event: AstrMessageEvent, theme: str = ""):
        """开始一场动态文字冒险游戏。用法：/开始冒险 [主题]。"""
        key = player_key(event)
        user_id = event.get_sender_id()
        if key in self.active_game_sessions:
            yield event.plain_result(
                "您已经有一个正在进行的冒险了！\n"
                "如需继续，请直接输入您的行动。\n"
                "如需结束，请发送 /结束冒险 或 /强制结束冒险。"
            ).stop_event()
            return

        llm_provider = self.context.get_using_provider()
        if not llm_provider:
            yield event.plain_result(
                f"抱歉，当前没有可用的LLM服务来开始冒险。请联系管理员。(玩家ID: {user_id})"
            ).stop_event()
            return

        game_theme = theme.strip() if theme else self.default_adventure_theme
        try:
            system_prompt = self.system_prompt_template.format(game_theme=game_theme)
        except (KeyError, IndexError, ValueError):
            logger.error("系统提示词模板格式错误，将使用默认模板。")
            system_prompt = (
                f"你是一位经验丰富的文字冒险游戏主持人。请根据玩家的行动，"
                f"在「{game_theme}」主题下实时生成连贯的故事情节。"
            )

        history = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "故事开始了，我的第一个场景是什么？"},
        ]
        session = AdventureSession(event)
        self.active_game_sessions[key] = session

        async def send_story(reply_event: AstrMessageEvent, story: str):
            history.append({"role": "assistant", "content": story})
            await reply_event.send(reply_event.plain_result(
                f"{story}\n\n**[提示: 请直接输入你的行动]** (玩家ID: {user_id})"
            ))

        async def open_adventure():
            await event.send(event.plain_result(
                "📜 **动态图文冒险 - 游戏须知** 📜\n\n"
                "故事由 AI 实时生成，仅供娱乐。"
                "你可以直接输入行动，例如「向左走」或「检查宝箱」。\n"
                f"每回合有 {self.session_timeout} 秒的行动时间。"
                "随时发送 /结束冒险 或 /强制结束冒险 即可退出。\n\n"
                "正在构筑您的冒险世界，请稍候..."
            ))
            if session.closed:
                return
            response = await llm_provider.text_chat(
                prompt="", session_id=event.get_session_id(), contexts=history,
            )
            if not session.closed:
                await send_story(event, response.completion_text)

        async def play_turn(controller: SessionController, action_event: AstrMessageEvent):
            player_action = action_event.get_message_str().strip()
            if not player_action:
                await action_event.send(action_event.plain_result(
                    f"你静静地站着，什么也没做。要继续冒险，请输入你的行动。\n(玩家ID: {user_id})"
                ))
                return

            await action_event.send(action_event.plain_result("AI正在构思下一幕...请稍等片刻..."))
            if session.closed or controller.future.done():
                return
            history.append({"role": "user", "content": player_action})
            response = await llm_provider.text_chat(
                prompt="", session_id=action_event.get_session_id(), contexts=history,
            )
            if not session.closed and not controller.future.done():
                await send_story(action_event, response.completion_text)

        @session_waiter(timeout=self.session_timeout, record_history_chains=False)
        async def adventure_waiter(controller: SessionController, action_event: AstrMessageEvent):
            if session.closed:
                return
            await session.run(play_turn(controller, action_event))
            if not session.closed and not controller.future.done():
                controller.keep(timeout=self.session_timeout, reset_timeout=True)

        end_message = None
        try:
            await session.run(open_adventure())
            if session.closed:
                return
            session.waiter = asyncio.create_task(adventure_waiter(event, session_filter=session))
            await session.waiter
        except asyncio.CancelledError:
            if not session.closed:
                raise
        except asyncio.TimeoutError:
            if not session.closed:
                end_message = "⏱️ **冒险超时！** 游戏已自动结束。使用 /开始冒险 开启新的旅程。"
        except Exception:
            if not session.closed:
                logger.exception(f"玩家 {user_id} 的文字冒险发生错误")
                end_message = f"冒险过程中发生错误，游戏已结束。\n(玩家ID: {user_id})"
        finally:
            session.close()
            if self.active_game_sessions.get(key) is session:
                del self.active_game_sessions[key]
            event.stop_event()
        if end_message:
            yield event.plain_result(end_message).stop_event()

    @filter.command("结束冒险", alias={"强制结束冒险"})
    async def end_adventure(self, event: AstrMessageEvent):
        """立即结束当前会话里的冒险。"""
        session = self.active_game_sessions.pop(player_key(event), None)
        if session is None:
            yield event.plain_result("您当前没有正在进行的冒险。").stop_event()
            return

        session.close()
        yield event.plain_result(
            "✅ **冒险已结束。** 可以继续正常聊天，或用 /开始冒险 开启新的旅程。"
        ).stop_event()

    def close_all_sessions(self) -> int:
        stopped_count = len(self.active_game_sessions)
        for session in self.active_game_sessions.values():
            session.close()
        self.active_game_sessions.clear()
        return stopped_count

    @filter.command("admin end")
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

    @filter.command("冒险帮助")
    async def cmd_adventure_help(self, event: AstrMessageEvent):
        """显示文字冒险插件的用法。"""
        yield event.plain_result(
            "📜 **动态文字冒险 - 帮助手册** 📜\n\n"
            "- `/开始冒险 [可选主题]`：开始一场新冒险。\n"
            "- 冒险中直接输入行动，例如「调查那个奇怪的符号」。\n"
            "- `/结束冒险` 或 `/强制结束冒险`：立即结束当前冒险。\n"
            "- `/admin end`：管理员结束所有正在进行的冒险。"
        ).stop_event()

    async def terminate(self):
        """插件卸载时结束所有冒险。"""
        self.close_all_sessions()
