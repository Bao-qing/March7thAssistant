import asyncio
import concurrent.futures
import io
import logging
import os
import threading
import time
import uuid
from typing import Optional

from PIL import Image

from module.config import Config
from module.game.base import GameControllerBase
from module.game.reverse_cloud_core import CloudGame, CloudGameCallbacks, QUEUE_TYPE_COIN, QUEUE_TYPE_NORMAL
from module.game.reverse_cloud_core.models import CloudGameConfig, Credentials
from module.logger import Logger


class ReverseCloudGameController(GameControllerBase):
    """基于 sr2 逆向协议的云游戏控制器。

    这个类负责把 March7th 现有的 ``GameControllerBase`` 接口桥接到
    ``module.game.reverse_cloud_core.CloudGame``：

    - March7th 仍然通过 ``start_game_process`` / ``enter_cloud_game`` /
      ``stop_game`` 管理游戏生命周期。
    - 自动化输入通过 ``get_input_handler`` 返回 ``ReverseCloudInput``，
      最终调用 sr2 core 的 ``send_input``。
    - 截图不注册持续视频帧回调，而是在 ``take_screenshot`` 时调用
      ``capture_video_frame`` 按需取一帧，避免空闲时持续转图。
    - 由于逆向云游戏没有本地窗口，窗口句柄相关接口只提供兼容占位。
    """

    def __init__(self, cfg: Config, logger: Optional[Logger] = None) -> None:
        super().__init__(script_path=cfg.script_path, logger=logger)
        self.cfg = cfg
        self.logger = logger
        self.queue_type = QUEUE_TYPE_COIN if self.cfg.get_value("cloud_game_use_paid_time", "") else QUEUE_TYPE_NORMAL
        # sr2 的核心门面。start_game_process 中创建，enter_cloud_game 中启动。
        self.core: CloudGame | None = None
        # 账号凭据只保存 Cookie；新版 sr2 core 会在 dispatcher.init 中生成 combo token。
        self.credentials: Credentials | None = None
        # stop_event 传给 sr2 core，用于从 March7th 主线程请求停止后台会话。
        self.stop_event: threading.Event | None = None
        # 线程异常时会置位，用于唤醒 enter_cloud_game 的等待流程。
        self.ready_event = threading.Event()
        # sr2 core 内部使用 asyncio；这里放到独立线程，避免阻塞 March7th 主流程。
        self.thread: threading.Thread | None = None
        # 最近一次按需截图拿到的帧。仅用于状态观察和调试，不做持续更新。
        self.latest_frame: Image.Image | None = None
        self.latest_frame_count = 0
        # 后台线程或截图协程捕获到的异常会放这里，供上层统一感知。
        self.last_error: BaseException | None = None
        # 云游戏 SDK 查询剪贴板时读取这个值；copy() 会同步更新它。
        self.clipboard_text = ""
        # latest_frame 可能被截图线程和主线程读写，使用锁保护。
        self._lock = threading.Lock()

    def _load_sr2_credentials(self) -> Credentials:
        """读取逆向云游戏登录凭据。

        优先使用环境变量 ``CLOUD_GAME_COOKIE``，便于临时调试；否则读取官方
        配置项 ``reverse_cloud_cookie``。新版 sr2 core 不要求调用方手动提供
        combo token，后续会在账号同步阶段自动生成。
        """
        cookie = os.environ.get("CLOUD_GAME_COOKIE", self.cfg.get_value("reverse_cloud_cookie", ""))
        if not cookie:
            raise RuntimeError("云游戏 Cookie 为空，请先配置 reverse_cloud_cookie")
        return Credentials(cookie=cookie)

    def _get_or_create_device_id(self) -> str:
        """读取或生成稳定 device_id。

        逆向协议需要设备 ID。配置为空时生成 UUID 并写回 ``config.yaml``，
        之后每次运行复用同一个值，避免频繁改变设备画像。
        """
        device_id = str(self.cfg.get_value("reverse_cloud_device_id", "") or "").strip()
        if device_id:
            return device_id
        device_id = str(uuid.uuid4())
        self.cfg.set_value("reverse_cloud_device_id", device_id)
        self.log_info(f"已生成云游戏 device_id：{device_id}")
        return device_id

    def _get_clipboard_text(self) -> str:
        """供 sr2 SDK game-data 回调读取剪贴板文本。"""
        return self.clipboard_text

    def _build_core(self) -> CloudGame:
        """创建 sr2 CloudGame 实例。

        这里故意只向 core_config 覆盖 ``device_profile.device_id``，其余设备、
        浏览器、协议、画质参数全部使用 sr2 core 默认配置。

        不注册 ``on_video_frame``，也不设置 ``video_frame_interval``：
        视频帧只在 ``take_screenshot`` 调用 ``capture_video_frame`` 时转图。
        """
        device_id = self._get_or_create_device_id()
        return CloudGame(
            CloudGameConfig(
                core_config={"device_profile": {"device_id": device_id}},
                clipboard_getter=self._get_clipboard_text,
                queue_type=self.queue_type,
            ),
            credentials=self.credentials,
            callbacks=CloudGameCallbacks(
                on_status=self._on_status,
                on_dispatch_log=self._on_dispatch_line,
            ),
        )

    def _log_with_level(self, message: str, level: int = logging.INFO) -> None:
        """把 Python logging level 映射到 March7th 自己的 Logger 方法。"""
        if self.logger is None:
            return
        if level >= logging.CRITICAL:
            self.logger.critical(message)
        elif level >= logging.ERROR:
            self.logger.error(message)
        elif level >= logging.WARNING:
            self.logger.warning(message)
        elif level <= logging.DEBUG:
            self.logger.debug(message)
        else:
            self.logger.info(message)

    def _on_status(self, message: str, level: int = logging.INFO) -> None:
        """接收 sr2 core 的用户可见状态日志。"""
        self._log_with_level(f"云游戏：{message}", level)

    def _on_dispatch_line(self, line: str, level: int = logging.INFO) -> None:
        """接收 sr2 dispatcher 阶段日志。"""
        self._log_with_level(f"云游戏调度：{line}", level)

    async def _run_core(self) -> None:
        """运行 sr2 高层流程：先调度实例，再连接 WebRTC 会话。"""
        assert self.core is not None
        assert self.stop_event is not None
        await self.core.run(
            dispatch=True,
            connect=True,
            stop_event=self.stop_event,
        )

    def _thread_main(self) -> None:
        """后台线程入口。

        sr2 core 是 asyncio 实现；March7th 现有任务是同步流程，所以这里用
        独立线程运行事件循环。异常不会直接跨线程抛给主流程，先保存到
        ``last_error``，再由 ``enter_cloud_game`` / ``take_screenshot`` 读取。
        """
        try:
            asyncio.run(self._run_core())
        except BaseException as exc:
            self.last_error = exc
            self.log_error(f"云游戏线程退出：{type(exc).__name__}: {exc}")
            self.ready_event.set()

    def start_game_process(self, headless=None) -> bool:
        """初始化逆向云游戏控制器。

        这里只创建凭据、core、stop_event，并清理上一轮状态；真正的调度和
        WebRTC 连接在 ``enter_cloud_game`` 中启动。这样保持和原浏览器云游戏
        控制器相同的两阶段调用习惯。
        """
        try:
            # 已经有活跃后台线程时直接复用，避免重复调度/重复连接。
            if self.thread is not None and self.thread.is_alive():
                return True
            self.credentials = self._load_sr2_credentials()
            self.core = self._build_core()
            self.stop_event = threading.Event()
            self.ready_event.clear()
            self.last_error = None
            with self._lock:
                self.latest_frame = None
                self.latest_frame_count = 0
            self.log_info("云游戏控制器已初始化")
            return True
        except Exception as exc:
            self.log_error(f"初始化云游戏失败：{exc}")
            return False

    def enter_cloud_game(self) -> bool:
        """启动后台云游戏流程，并等待 WebRTC 首帧到达。

        sr2 的 ``GameSession.wait_for_video_connected`` 会在 video track
        真正取到首帧后返回。这里把等待协程投递到后台会话自己的事件循环，
        确认画面链路已经连通；后续截图仍由 ``take_screenshot`` 按需取帧。
        """
        if self.core is None or self.stop_event is None:
            if not self.start_game_process():
                return False

        # 获取剩余时长
        wallet = self.core.get_wallet_info()
        summary = wallet.get("summary") or {}
        self.logger.info(
            f"剩余时长: 星云币={summary.get('coin_num')} 个(约 {summary.get('coin_minutes')} 分钟), 免费={summary.get('free_time_minutes')} 分钟, 畅玩卡={summary.get('play_card_remaining_sec')} 秒"
        )

        # 获取排队信息
        queue_estimate = self.core.get_queue_estimate()
        normal = queue_estimate.get("normal") or {}
        coin = queue_estimate.get("coin") or {}
        node = queue_estimate.get("node") or {}
        self.logger.info(
            f"预计排队: 节点={node.get('node_name') or node.get('node_id')}, 普通队列={normal.get('queue_len') or normal.get('queue_length')} 人/约 {normal.get('waiting_time_min')} 分钟, 星云币队列={coin.get('queue_len') or coin.get('queue_length')} 人/约 {coin.get('waiting_time_min')} 分钟"
        )

        self.log_info(f"正在进入云游戏，队列：{'星云币' if self.queue_type == QUEUE_TYPE_COIN else '普通队列'}...")

        # 后台线程只启动一次；重复 enter 时复用正在运行的会话。
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._thread_main, name="ReverseCloudGame", daemon=True)
            self.thread.start()

        try:
            timeout = max(30, int(self.cfg.get_value("cloud_game_max_queue_time", 60)) * 60)
        except (TypeError, ValueError):
            timeout = 60 * 60

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # 调度、登录同步或 WebRTC 连接异常会被后台线程写入 last_error。
            if self.last_error is not None:
                self.log_error(f"云游戏启动失败：{self.last_error}")
                return False
            # sr2 core 创建 GameSession 并进入 run 后，才能拿到会话自己的 asyncio loop。
            session = self.core.game_session if self.core is not None else None
            if session is not None and session.loop is not None:
                remaining = max(0.1, deadline - time.monotonic())
                # 等待首帧必须在 GameSession 所属事件循环执行，不能在当前同步线程直接 await。
                future = asyncio.run_coroutine_threadsafe(session.wait_for_video_connected(), session.loop)
                try:
                    if not future.result(timeout=remaining):
                        self.log_error("云游戏会话已结束，未收到首帧")
                        return False
                    self.log_info("游戏画面已连接")
                    return True
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    self.log_error("等待云游戏首帧超时")
                    return False
                except Exception as exc:
                    self.log_error(f"等待云游戏首帧失败：{exc}")
                    return False
            if self.thread is not None and not self.thread.is_alive():
                self.log_error("云游戏线程已退出")
                return False
            time.sleep(0.2)
        self.log_error("等待云游戏首帧超时")
        return False

    def stop_game(self) -> bool:
        """请求停止云游戏并等待后台线程退出。"""
        try:
            if self.stop_event is not None:
                # 通知 sr2 dispatcher/session 尽快中断等待和关闭连接。
                self.stop_event.set()
            if self.thread is not None and self.thread.is_alive():
                # 限时等待，避免网络阻塞导致 March7th 退出流程卡死。
                self.thread.join(timeout=10)
            self.log_info("云游戏已停止")
            return True
        except Exception as exc:
            self.log_error(f"停止云游戏失败：{exc}")
            return False

    def get_window_handle(self) -> int:
        """返回窗口句柄兼容占位。

        逆向云游戏没有本地窗口；返回非 0 值仅用于满足上层
        ``is_game_running`` / ``switch_to_game`` 这类窗口式判断。
        """
        return 1 if self.is_game_running() else 0

    def get_resolution(self):
        """返回自动化逻辑使用的固定逻辑分辨率。"""
        return 1920, 1080

    def switch_to_game(self) -> bool:
        """逆向云游戏无窗口可切换，返回当前会话是否运行。"""
        return self.is_game_running()

    def is_game_running(self) -> bool:
        """判断后台云游戏线程是否仍在运行。"""
        return self.thread is not None and self.thread.is_alive() and self.stop_event is not None and not self.stop_event.is_set()

    def is_in_game(self) -> bool:
        """判断是否已经创建 sr2 GameSession。"""
        return self.is_game_running() and self.core is not None and self.core.game_session is not None

    def get_input_handler(self):
        """返回逆向云游戏输入适配器。"""
        from module.automation.reverse_cloud_input import ReverseCloudInput
        return ReverseCloudInput(self, self.logger)

    def send_input(self, action) -> bool:
        """向 sr2 当前会话发送输入动作。"""
        if self.core is None:
            return False
        return self.core.send_input(action)

    def copy(self, text: str) -> None:
        """设置云游戏剪贴板文本。

        ``clipboard_text`` 用于应答云游戏 SDK 的剪贴板读取请求；
        ``InputAction.clipboard`` 会主动把文本发给当前会话；系统剪贴板只是
        兼容保底，方便本地调试或其他路径使用。
        """
        self.clipboard_text = text
        if self.core is not None:
            from module.game.reverse_cloud_core.models import InputAction
            self.core.send_input(InputAction.clipboard(text))
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception as exc:
            self.log_warning(f"设置剪贴板失败：{exc}")

    def _capture_video_frame(self, timeout: float = 10.0):
        """同步包装 sr2 的异步按需捕帧接口。

        ``CloudGame.capture_video_frame`` 是协程。March7th 截图入口是同步方法，
        因此这里临时开一个线程运行事件循环，等待下一帧被 sr2 转成 PIL 图。
        """
        if self.core is None:
            return None
        result_holder = {"result": None, "error": None}

        def run_capture():
            """在线程内运行独立事件循环，避免阻塞调用方线程。"""
            try:
                result_holder["result"] = asyncio.run(self.core.capture_video_frame(timeout=timeout))
            except BaseException as exc:
                result_holder["error"] = exc

        thread = threading.Thread(target=run_capture, name="ReverseCloudFrameCapture", daemon=True)
        thread.start()
        thread.join(timeout=timeout + 1)
        if thread.is_alive():
            self.log_error("云游戏截图等待超时")
            return None
        if result_holder["error"] is not None:
            raise result_holder["error"]
        return result_holder["result"]

    def take_screenshot(self, crop=(0, 0, 1, 1), prefer_frame=True) -> bytes | tuple[bytes, tuple[int, int]] | None:
        """按需获取一帧云游戏画面并返回 PNG 字节。

        返回格式保持和浏览器云游戏控制器兼容：
        ``(png_bytes, (source_width, source_height))``。
        ``Screenshot.take_screenshot`` 会根据源分辨率继续计算 crop 后的位置。
        """
        try:
            capture_result = self._capture_video_frame(timeout=10)
        except BaseException as exc:
            self.log_error(f"云游戏截图失败：{exc}")
            return None

        if not capture_result:
            if self.last_error is not None:
                self.log_error(f"云游戏截图失败：{self.last_error}")
            return None
        frame, frame_count = capture_result
        with self._lock:
            self.latest_frame = frame.copy()
            self.latest_frame_count = frame_count

        # crop 是比例坐标，沿用现有自动化约定：
        # (left_ratio, top_ratio, width_ratio, height_ratio)。
        source_width, source_height = frame.size
        left = int(source_width * crop[0])
        top = int(source_height * crop[1])
        width = int(source_width * crop[2])
        height = int(source_height * crop[3])
        screenshot = frame.crop((left, top, left + width, top + height))

        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG")
        return buffer.getvalue(), (source_width, source_height)

    def check_cloud_game_interruptions(self) -> None:
        """把后台连接异常同步暴露给任务流程。"""
        if self.last_error is not None:
            raise RuntimeError(f"云游戏连接异常：{self.last_error}") from self.last_error
