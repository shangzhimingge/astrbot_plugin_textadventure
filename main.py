import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.provider import Provider
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.star import star_map

from .story_card import render_story_cards


INPUT_MATCH = "textadventure_input"
STORY_SESSION = "textadventure_story_session"
START = "开始冒险"
END = "结束冒险"
FORCE_END = "强制结束冒险"
SAVE = "存档冒险"
LOAD = "读取冒险"
LIST_SAVES = "冒险存档"
LEGACY_SAVE_NAME = "旧版存档"
HELP = "冒险帮助"
ADMIN_END = "admin end"


def player_key(event: AstrMessageEvent) -> tuple[str, str]:
    return event.unified_msg_origin, event.get_sender_id()


def command_argument(event: AstrMessageEvent, command: str) -> str:
    match = event.get_extra(INPUT_MATCH)
    if match is not None and match[0] == command:
        return match[1].strip()
    parts = event.get_message_str().split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def validate_save_name(name: str) -> None:
    if len(name) > 40 or any(not (char.isalnum() or char in " -_.") for char in name):
        raise ValueError("存档名最多 40 字，可用文字、数字、空格、点、连字符和下划线。")


def adventure_save_path(key: tuple[str, str], name: str = LEGACY_SAVE_NAME) -> Path:
    key_bytes = json.dumps(key, ensure_ascii=False).encode("utf-8")
    owner = hashlib.sha256(key_bytes).hexdigest()
    directory = StarTools.get_data_dir("textadventure") / "saves"
    if name == LEGACY_SAVE_NAME:
        return directory / f"{owner}.json"
    slot = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return directory / owner / f"{slot}.json"


def adventure_save_paths(key: tuple[str, str]) -> list[Path]:
    legacy_path = adventure_save_path(key)
    paths = list(legacy_path.with_suffix("").glob("*.json"))
    if legacy_path.is_file():
        paths.append(legacy_path)
    return sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def write_adventure_save(key: tuple[str, str], history: list[dict[str, str]], name: str) -> str:
    if not name:
        base_name = datetime.now().strftime("冒险-%Y%m%d-%H%M%S")
        name = base_name
        suffix = 2
        while adventure_save_path(key, name).exists():
            name = f"{base_name}-{suffix}"
            suffix += 1

    path = adventure_save_path(key, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False,
    )
    temp_path = Path(temp.name)
    try:
        with temp:
            json.dump({"version": 2, "name": name, "history": history}, temp, ensure_ascii=False)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return name


def read_save_record(path: Path) -> tuple[str, dict]:
    with path.open(encoding="utf-8") as save_file:
        data = json.load(save_file)
    if not isinstance(data, dict):
        raise ValueError("无法识别冒险存档格式")
    if data.get("version") == 1:
        return LEGACY_SAVE_NAME, data
    name = data.get("name")
    if data.get("version") != 2 or not isinstance(name, str) or not name:
        raise ValueError("无法识别冒险存档格式")
    validate_save_name(name)
    return name, data


def list_adventure_saves(key: tuple[str, str]) -> list[str]:
    names = []
    for path in adventure_save_paths(key):
        try:
            name, _ = read_save_record(path)
        except (OSError, ValueError):
            logger.warning("跳过无法读取的文字冒险存档文件 %s", path.name)
            continue
        names.append(name)
    return names


def read_adventure_save(
    key: tuple[str, str], name: str = "",
) -> tuple[str, list[dict[str, str]]]:
    if name:
        path = adventure_save_path(key, name)
    else:
        paths = adventure_save_paths(key)
        if not paths:
            raise FileNotFoundError
        path = paths[0]

    saved_name, data = read_save_record(path)
    if name and name != saved_name:
        raise ValueError("存档名称与文件不匹配")

    history = data.get("history")
    if not isinstance(history, list) or len(history) < 3 or len(history) % 2 == 0:
        raise ValueError("存档缺少完整回合")

    for index, message in enumerate(history):
        role = "system" if index == 0 else ("user" if index % 2 else "assistant")
        if (
            not isinstance(message, dict)
            or message.get("role") != role
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError("存档中的对话记录格式错误")
    return saved_name, history


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
        self.story_output_mode = self.config.get("story_output_mode", "跟随 AstrBot")
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
        self.save_lock = asyncio.Lock()

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
            if command in {START, SAVE, LOAD}:
                event.set_extra(INPUT_MATCH, (command, args[0] if args else "", None))
                return True
            if action in {END, FORCE_END, LIST_SAVES, HELP, ADMIN_END}:
                event.set_extra(INPUT_MATCH, (action, "", None))
                return True

        if command in {START, END, FORCE_END, SAVE, LOAD, LIST_SAVES, HELP} or action == ADMIN_END:
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
            SAVE: self.save_adventure,
            LOAD: self.load_adventure,
            LIST_SAVES: self.list_saves,
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
        return await self.story_result(event, session, story)

    async def story_result(
        self, event: AstrMessageEvent, session: AdventureSession, story: str, saved_name: str = "",
    ):
        if session.closed:
            return
        player_id = event.get_sender_id()
        prefix = f"📖 已读取冒险存档「{saved_name}」\n\n" if saved_name else ""
        text_result = event.plain_result(
            f"{prefix}{story}\n\n[行动示例：{self.action_example}] (玩家ID: {player_id})"
        )
        mode = self.story_output_mode
        if mode == "图片":
            try:
                pages = await asyncio.to_thread(
                    render_story_cards, story, self.action_example, player_id,
                    (len(session.history) - 1) // 2, saved_name,
                )
                result = event.chain_result([Image.fromBytes(page) for page in pages])
            except Exception:
                logger.exception("渲染冒险日志图片失败，改用文字发送")
            else:
                if not session.closed:
                    event.set_extra(STORY_SESSION, session)
                    return result
                return

        if session.closed:
            return
        if mode in {"文字", "图片"}:
            await event.send(text_result)
            return

        event.set_extra(STORY_SESSION, session)
        return text_result

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
        async for result in self.start_game(event, command_argument(event, START)):
            yield result

    def adventure_provider(self) -> tuple[Provider | None, str]:
        provider_id = (self.config.get("llm_provider_id") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if isinstance(provider, Provider):
                return provider, ""
            return None, (
                "所选的冒险模型不可用。请在插件配置中重新选择已启用的聊天模型，"
                "或清空此项以使用 AstrBot 默认聊天模型。"
            )

        provider = self.context.get_using_provider()
        if provider is None:
            return None, "当前没有可用的 LLM 服务，请联系管理员。"
        return provider, ""

    async def start_game(self, event: AstrMessageEvent, theme: str):
        key = player_key(event)
        if key in self.active_game_sessions:
            yield event.plain_result(
                f"你已经有正在进行的冒险。行动示例：{self.action_example}。"
                f"发送 {self.trigger_prefix}{END} 可退出。"
            ).stop_event()
            return

        llm_provider, error = self.adventure_provider()
        if llm_provider is None:
            yield event.plain_result(error).stop_event()
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
            if len(session.history) > 1 and not session.closed:
                session.ready = True
                self.arm_timeout(event, session)
            event.stop_event()

    @filter.command(SAVE)
    async def save_adventure(self, event: AstrMessageEvent):
        """保存当前冒险。用法：存档冒险 [存档名]。"""
        key = player_key(event)
        try:
            name = command_argument(event, SAVE)
            try:
                validate_save_name(name)
                if name == LEGACY_SAVE_NAME:
                    raise ValueError(f"「{LEGACY_SAVE_NAME}」留给升级前的存档，请换一个名称。")
            except ValueError as error:
                yield event.plain_result(str(error))
                return

            async with self.save_lock:
                session = self.active_game_sessions.get(key)
                if session is None or not session.ready:
                    message = "当前没有可以存档的冒险，请在开场剧情生成后再存档。"
                else:
                    try:
                        saved_name = await asyncio.to_thread(
                            write_adventure_save, key, session.history.copy(), name,
                        )
                        message = (
                            f"💾 已存档「{saved_name}」。\n"
                            f"结束后可发送 {self.trigger_prefix}{LOAD} {saved_name} 继续。"
                        )
                    except (OSError, RuntimeError, ValueError):
                        logger.exception("写入文字冒险存档失败")
                        message = "存档失败，请联系管理员检查插件数据目录。"
            yield event.plain_result(message)
        finally:
            event.stop_event()

    @filter.command(LOAD)
    async def load_adventure(self, event: AstrMessageEvent):
        """读取此聊天中的存档。用法：读取冒险 [存档名]。"""
        key = player_key(event)
        session = None
        try:
            name = command_argument(event, LOAD)
            try:
                validate_save_name(name)
            except ValueError as error:
                yield event.plain_result(str(error))
                return

            if key in self.active_game_sessions:
                yield event.plain_result(
                    f"你已有正在进行的冒险，请先发送 {self.trigger_prefix}{END} 再读档。"
                )
                return

            llm_provider, error = self.adventure_provider()
            if llm_provider is None:
                yield event.plain_result(error)
                return

            session = AdventureSession(llm_provider, [])
            self.active_game_sessions[key] = session

            async def read_save():
                async with self.save_lock:
                    saved_name, history = await asyncio.to_thread(read_adventure_save, key, name)
                if not session.closed:
                    session.history = history
                    return await self.story_result(event, session, history[-1]["content"], saved_name)

            try:
                result = await session.run(read_save())
            except FileNotFoundError:
                if self.remove_session(key, session):
                    if name:
                        message = f"找不到存档「{name}」。发送 {self.trigger_prefix}{LIST_SAVES} 查看列表。"
                    else:
                        message = "当前聊天中还没有你的冒险存档。"
                    yield event.plain_result(message)
                return
            except (OSError, RuntimeError, ValueError):
                if self.remove_session(key, session):
                    logger.exception("读取文字冒险存档失败")
                    yield event.plain_result("无法读取冒险存档，请联系管理员检查存档文件。")
                return

            if result is not None and not session.closed:
                yield result
        finally:
            if session is not None and not session.closed:
                if session.history:
                    session.ready = True
                    self.arm_timeout(event, session)
                else:
                    self.remove_session(key, session)
            event.stop_event()

    @filter.command(LIST_SAVES)
    async def list_saves(self, event: AstrMessageEvent):
        """列出当前玩家在此聊天中的冒险存档。"""
        try:
            try:
                async with self.save_lock:
                    names = await asyncio.to_thread(list_adventure_saves, player_key(event))
            except (OSError, RuntimeError):
                logger.exception("列出文字冒险存档失败")
                yield event.plain_result("无法读取冒险存档列表，请联系管理员。")
                return

            if not names:
                yield event.plain_result("当前聊天中还没有你的冒险存档。")
                return

            lines = ["💾 此聊天中的冒险存档（最新在前）"]
            lines.extend(f"• {name}" for name in names)
            lines.append(f"发送 {self.trigger_prefix}{LOAD} 存档名 继续游玩。")
            yield event.plain_result("\n".join(lines))
        finally:
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
            f"- `{prefix}{SAVE} [存档名]` 保存进度，留空自动命名。\n"
            f"- `{prefix}{LIST_SAVES}` 查看此聊天中的存档。\n"
            f"- `{prefix}{LOAD} [存档名]` 读档，留空读取最新存档。\n"
            f"- `{prefix}{END}` 或 `{prefix}{FORCE_END}` 立即结束当前冒险。\n"
            f"- `{prefix}{ADMIN_END}` 由管理员结束所有冒险。"
        ).stop_event()

    async def terminate(self):
        """插件卸载时结束所有冒险。"""
        self.close_all_sessions()
