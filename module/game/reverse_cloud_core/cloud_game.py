from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
import threading
from typing import Any

from .config import normalize_core_config
from .dispatcher import DispatchConfig, Dispatcher, QUEUE_TYPE_COIN, QUEUE_TYPE_NORMAL
from .log import emit_log_callback
from .models import CloudGameConfig, Credentials, InputAction
from .session import ControlActionScript, GameSession, SessionConfig

__all__ = [
    "AccountState",
    "CloudGame",
    "CloudGameCallbacks",
    "CloudGameState",
    "QUEUE_TYPE_COIN",
    "QUEUE_TYPE_NORMAL",
]


@dataclass
class AccountState:
    """CloudGame 持有的账号运行态。

    字段说明：
        cookie: miHoYo 登录 Cookie。
        combo_token: 调度接口使用的 wrapped combo token。
        channel_token: SDK 登录所需的 channel token。
        sdk_login: SDK login 响应体，会话层优先使用它应答登录查询。
        open_id: 当前账号 ID，仅用于日志和状态观察。
    """

    cookie: str = ""
    combo_token: str = ""
    channel_token: str = ""
    sdk_login: dict | None = None
    open_id: str = ""


@dataclass
class CloudGameState:
    """UI/CLI 可读取的最新运行状态快照。"""

    latest_video_frame: Any = None
    latest_video_count: int = 0
    latest_ws_event: dict | None = None
    latest_status: str = ""
    latest_status_level: int = logging.INFO
    latest_dispatch_line: str = ""
    latest_dispatch_level: int = logging.INFO
    latest_finish_result: dict | None = None


@dataclass
class CloudGameCallbacks:
    """CloudGame 统一管理的事件回调。

    字段说明：
        on_status: 用户可见状态变化回调，接收 ``(message, level)``。
        on_dispatch_log: 调度日志回调，接收 ``(message, level)``。
        on_video_frame: 视频帧回调，接收 ``(image, count)``。
        on_ws_event: WebSocket 格式化事件回调，接收 event dict。
        on_input_ready: 输入通道可投递状态变化回调，接收 ``ready`` 布尔值。
    """

    on_status: Callable[[str, int], None] | None = None
    on_dispatch_log: Callable[[str, int], None] | None = None
    on_video_frame: Callable[[Any, int], None] | None = None
    on_ws_event: Callable[[dict], None] | None = None
    on_input_ready: Callable[[bool], None] | None = None


class CloudGame:
    """云游戏两阶段流程的公开门面。

    下层模块按协议边界拆分：``Dispatcher`` 同步完成排队和实例调度，
    ``GameSession`` 负责异步 WebSocket/WebRTC 生命周期。``CloudGame``
    负责串起两个阶段，缓存 UI/CLI 需要观察的最新状态，并提供
    ``send_input()``，调用方无需关心当前输入走游戏控制通道还是 RTC
    DataChannel。
    """

    def __init__(
        self,
        config: CloudGameConfig | None = None,
        credentials: Credentials | None = None,
        callbacks: CloudGameCallbacks | None = None,
    ) -> None:
        """创建云游戏客户端门面。

        参数:
            config: 完整运行配置；None 表示使用 ``CloudGameConfig`` 默认值。
            credentials: 初始账号凭据；None 表示稍后通过方法参数传入。
            callbacks: 统一事件回调；方法参数中的旧回调仍会临时覆盖。
        """
        config = config or CloudGameConfig()
        core_config = normalize_core_config(config.core_config)
        self.config = replace(config, core_config=core_config)
        self.account = AccountState(
            cookie="",
            combo_token="",
            channel_token="",
            sdk_login=None,
        )
        self.callbacks = callbacks or CloudGameCallbacks()
        self.state = CloudGameState()
        self.dispatcher = Dispatcher(self.dispatch_config)
        self.game_session: GameSession | None = None
        self._apply_credentials(credentials)

        self._video_frame_request = threading.Event()
        self._video_frame_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        self._video_frame_waiters_lock = threading.Lock()
        self._video_frame_callback_enabled = False

    @staticmethod
    def load_actions(path: str | None = None, click: str | None = None) -> list[dict]:
        """加载会话层可执行的定时输入脚本。

        参数:
            path: JSON 动作脚本路径；None 表示不从文件加载。
            click: 追加一个简单点击动作，格式为 ``"x,y"``。
        """
        return ControlActionScript.load(path, click)

    def _apply_credentials(self, credentials: Credentials | None) -> None:
        """把调用方传入的凭据合并到运行配置。

        参数:
            credentials: Cookie、combo token 和 channel token；None 表示不更新。
        """
        if credentials is None:
            return
        self.account.cookie = credentials.cookie
        self.account.combo_token = credentials.combo_token
        self.account.channel_token = credentials.channel_token
        self._reset_dispatcher()

    def _reset_dispatcher(self) -> None:
        """按当前账号态和运行配置重建统一 Dispatcher。"""
        old_dispatcher = getattr(self, "dispatcher", None)
        if old_dispatcher is not None:
            old_dispatcher.close()
        self.dispatcher = Dispatcher(self.dispatch_config)

    def _store_account_sync(self, account_sync: dict | None) -> None:
        """保存 Dispatcher 初始化返回的账号态，供后续会话复用。

        参数:
            account_sync: ``Dispatcher.init()`` 返回值；None 表示不更新。
        """
        if not account_sync:
            return
        self.account.sdk_login = account_sync.get("sdk_login") or self.account.sdk_login
        self.account.combo_token = account_sync.get("combo_token") or self.account.combo_token
        self.account.channel_token = account_sync.get("channel_token") or self.account.channel_token
        self.account.open_id = str(account_sync.get("open_id") or self.account.open_id or "")

    def _init_dispatcher(self, dispatcher: Dispatcher | None = None) -> dict:
        """初始化 Dispatcher，并把需要跨阶段复用的账号态写回运行配置。

        参数:
            dispatcher: 本次操作使用的调度器实例；None 表示使用统一实例。

        返回:
            ``Dispatcher.init()`` 返回的账号同步结果。
        """
        dispatcher = dispatcher or self.dispatcher
        account_sync = dispatcher.init()
        self._store_account_sync(account_sync)
        return account_sync

    @property
    def dispatch_config(self) -> DispatchConfig:
        """生成调度阶段专用配置。"""
        return DispatchConfig(
            max_polls=self.config.max_polls,
            queue_type=self.config.queue_type,
            node=self.config.node,
            speed_client_type=self.config.speed_client_type,
            cookie=self.account.cookie,
            combo_token=self.account.combo_token,
            core_config=self.config.core_config,
            root_dir=self.config.root_dir,
        )

    @property
    def session_config(self) -> SessionConfig:
        """生成 RTC 会话阶段专用配置。

        这里故意不设置 ``finish_result``：connect 调用方可能显式传入，
        也可能复用 ``state.latest_finish_result``；两者都没有时交给会话层报错。
        """
        return SessionConfig(
            max_seconds=self.config.max_seconds,
            snapshot_dir=self.config.snapshot_dir,
            snapshot_interval=self.config.snapshot_interval,
            video_frame_interval=self.config.video_frame_interval,
            control_actions=self.config.control_actions or [],
            ws_log_payload=self.config.ws_log_payload,
            ws_payload_limit=self.config.ws_payload_limit,
            color=self.config.color,
            cookie=self.account.cookie,
            combo_token=self.account.combo_token,
            channel_token=self.account.channel_token,
            sdk_login=self.account.sdk_login,
            clipboard_getter=self.config.clipboard_getter,
            core_config=self.config.core_config,
            video_frame_request_event=self._video_frame_request,
        )

    @staticmethod
    def _set_video_frame_result(future: asyncio.Future, result: tuple[Any, int]) -> None:
        """安全唤醒一个等待视频帧的 future。"""
        if not future.done():
            future.set_result(result)

    def _emit_dispatch_line(self, message: str, level: int = logging.INFO) -> None:
        """缓存最新调度日志，并转发给调用方回调。"""
        self.state.latest_dispatch_line = message
        self.state.latest_dispatch_level = level
        emit_log_callback(self.callbacks.on_dispatch_log, message, level)

    def _emit_status(self, message: str, level: int = logging.INFO) -> None:
        """缓存最新用户可见状态，并转发给调用方回调。"""
        self.state.latest_status = message
        self.state.latest_status_level = level
        emit_log_callback(self.callbacks.on_status, message, level)

    def _emit_video_frame(self, image, count: int) -> None:
        """缓存最新视频帧，供截图/预览读取，并转发给调用方回调。"""
        self.state.latest_video_frame = image
        self.state.latest_video_count = count
        with self._video_frame_waiters_lock:
            waiters = self._video_frame_waiters
            self._video_frame_waiters = []
        for loop, future in waiters:
            if not future.done():
                loop.call_soon_threadsafe(self._set_video_frame_result, future, (image, count))
        if self.callbacks.on_video_frame is not None:
            self.callbacks.on_video_frame(image, count)

    def _emit_ws_event(self, event: dict) -> None:
        """缓存最新格式化 WebSocket 事件，并转发给调用方回调。"""
        self.state.latest_ws_event = event
        if self.callbacks.on_ws_event is not None:
            self.callbacks.on_ws_event(event)

    def request_video_frame(self) -> bool:
        """请求会话层在下一帧视频到达时执行一次视频帧回调。

        返回:
            当前没有设置 ``callbacks.on_video_frame`` 时返回 False；成功发出请求
            返回 True。这个请求只控制应用层转图/回调，不影响底层 WebRTC
            视频轨道接收。
        """
        if not self._video_frame_callback_enabled:
            return False
        self._video_frame_request.set()
        return True

    async def capture_video_frame(self, timeout: float | None = 5.0) -> tuple[Any, int] | None:
        """请求下一帧视频并直接返回 ``(image, count)``。

        这个方法不要求调用方设置 ``callbacks.on_video_frame``。它会请求会话层
        在下一帧视频到达时转成图片，并等待该图片返回。未连接或超时时返回
        None。
        """
        if self.game_session is None:
            return None
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = (loop, future)
        with self._video_frame_waiters_lock:
            self._video_frame_waiters.append(waiter)
        self._video_frame_request.set()
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            with self._video_frame_waiters_lock:
                self._video_frame_waiters = [
                    pending for pending in self._video_frame_waiters if pending[1] is not future
                ]
            if not future.done():
                future.cancel()
            return None
        except asyncio.CancelledError:
            with self._video_frame_waiters_lock:
                self._video_frame_waiters = [
                    pending for pending in self._video_frame_waiters if pending[1] is not future
                ]
            raise


    def send_input(self, action: dict | InputAction) -> bool:
        """向当前活动会话排入一个输入动作。

        参数:
            action: 输入动作，支持原始 dict 或 ``InputAction``。鼠标坐标可传
                0.0-1.0 归一化坐标；如果传入大于 1 的像素坐标，会话层会按
                串流分辨率换算成 0.0-1.0，并最终夹紧到有效范围。

        返回:
            会话尚未准备好输入通道时返回 False；成功排入发送队列返回 True。
        """
        if self.game_session is None:
            return False
        if isinstance(action, InputAction):
            action = action.as_dict()
        return self.game_session.send_input(action)

    def dispatch(self, stop_event=None) -> dict:
        """同步执行调度流程并缓存 finish_result。

        参数:
            stop_event: 外部停止事件；被设置后调度等待会尽快中断。

        返回:
            服务端返回的 ``finish_result``，可保存后用于 connect-only 重连。

        调度阶段也会执行网页登录同步。若同步得到 SDK login 数据，会缓存在
        运行配置中，后续 ``connect()`` 可直接复用，避免重复登录交换。
        """
        dispatcher = self.dispatcher
        self._init_dispatcher(dispatcher)
        result = dispatcher.run(
            line_callback=lambda line, level: self._emit_dispatch_line(line, level),
            status_callback=lambda message, level: self._emit_status(message, level),
            stop_event=stop_event,
        )
        self.state.latest_finish_result = result
        return result

    def get_wallet_info(self) -> dict:
        """查询账号剩余时长。

        返回:
            ``summary`` 中包含星云币数量、折算分钟数、免费时长分钟数和
            畅玩卡剩余秒数；``data`` 保留服务端原始钱包数据。
        """
        dispatcher = self.dispatcher
        self._init_dispatcher(dispatcher)
        return dispatcher.wallet_info()

    def get_queue_estimate(self) -> dict:
        """查询正式 dispatch 前的普通队列和星云币优先队列预估信息。

        返回:
            ``normal`` 对应免费/普通队列，``coin`` 对应星云币优先队列。
            两者都会尽量提取 ``waiting_time_min``、队列长度和当前位置。
        """
        dispatcher = self.dispatcher
        self._init_dispatcher(dispatcher)
        return dispatcher.queue_estimate(
            line_callback=lambda line, level: self._emit_dispatch_line(line, level),
            status_callback=lambda message, level: self._emit_status(message, level),
        )

    async def connect(
        self,
        *,
        finish_result: dict | None = None,
        stop_event=None,
    ) -> None:
        """连接一个已经完成调度的云游戏实例。

        参数:
            finish_result: 调度完成结果；优先级高于 ``state.latest_finish_result``。
            stop_event: 外部停止事件；被设置后会话会主动关闭 WebSocket。

        如果没有缓存的 SDK login，但已有 Cookie，本方法会在启动 WebRTC
        会话前同步一次账号；这是从保存的 ``finish_result.json`` 直接连接时
        必要的补齐步骤。
        """
        if self.account.cookie and not self.account.sdk_login:
            # connect-only 可能来自新进程加载的 finish_result，此时没有
            # dispatch 阶段缓存下来的账号同步结果，需要在这里补一次。
            self._init_dispatcher()
        session_config = self.session_config
        session_config.finish_result = finish_result or self.state.latest_finish_result

        def on_input_ready(ready: bool) -> None:
            """通知外部观察者输入是否可投递。"""
            if self.callbacks.on_input_ready is not None:
                self.callbacks.on_input_ready(ready)

        session = GameSession(
            session_config,
            video_frame_callback=lambda image, count: self._emit_video_frame(image, count),
            ws_event_callback=lambda event: self._emit_ws_event(event),
            status_callback=lambda message, level: self._emit_status(message, level),
            stop_event=stop_event,
            input_ready_callback=on_input_ready,
        )
        self.game_session = session
        self._video_frame_callback_enabled = self.callbacks.on_video_frame is not None
        try:
            await session.run()
        finally:
            self.game_session = None
            self._video_frame_callback_enabled = False
            self._video_frame_request.clear()

    async def run(
        self,
        *,
        dispatch: bool = True,
        connect: bool = True,
        stop_event=None,
    ) -> dict | None:
        """按开关执行 dispatch 和/或 connect 的高层工作流。

        参数:
            dispatch: 是否先执行调度阶段。
            connect: 是否执行连接阶段。
            stop_event: 外部停止事件。

        返回:
            ``dispatch=True`` 时返回新的 ``finish_result``；否则返回 None。

        当 ``dispatch=False`` 且 ``connect=True`` 时，本方法等价于直接
        ``connect()``，依赖已有的 ``state.latest_finish_result`` 或由会话层报告缺失。
        """
        result = None
        if dispatch:
            result = self.dispatch(stop_event=stop_event)
        self.state.latest_finish_result = result
        if connect and not (stop_event is not None and stop_event.is_set()):
            await self.connect(
                finish_result=result,
                stop_event=stop_event,
            )
        return result
