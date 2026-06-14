import time

from module.automation.input_base import InputBase
from module.game.reverse_cloud_core.models import InputAction


class ReverseCloudInput(InputBase):
    KEY_ALIAS_MAP = {
        "escape": "esc",
        "return": "enter",
        "control": "ctrl",
        "left control": "ctrl",
        "right control": "ctrl",
        "left ctrl": "ctrl",
        "right ctrl": "ctrl",
        "option": "alt",
        "left alt": "alt",
        "right alt": "alt",
        "left shift": "shift",
        "right shift": "shift",
        "win": "windows",
        "command": "windows",
        "arrowup": "up",
        "arrowdown": "down",
        "arrowleft": "left",
        "arrowright": "right",
    }

    KEY_CODE_MAP = {
        "esc": 27,
        "enter": 13,
        "space": 32,
        "tab": 9,
        "backspace": 8,
        "delete": 46,
        "shift": 16,
        "ctrl": 17,
        "alt": 18,
        "windows": 91,
        "up": 38,
        "down": 40,
        "left": 37,
        "right": 39,
    }

    KEY_CODE_MAP.update({chr(i): ord(chr(i).upper()) for i in range(97, 123)})
    KEY_CODE_MAP.update({str(i): 48 + i for i in range(10)})
    KEY_CODE_MAP.update({f"f{i}": 111 + i for i in range(1, 13)})

    def __init__(self, cloud_game, logger):
        self.cloud_game = cloud_game
        self.logger = logger
        self.last_x = 0
        self.last_y = 0

    def _send(self, action: InputAction | dict) -> bool:
        if isinstance(action, InputAction):
            action = action.as_dict()
        if not self.cloud_game.send_input(action):
            self.logger.warning(f"逆向云游戏输入发送失败：{action.get('type')}")
            return False
        return True

    def _normalize_key_name(self, key):
        normalized = str(key or "").strip().lower()
        return self.KEY_ALIAS_MAP.get(normalized, normalized)

    def _key_code(self, key):
        normalized = self._normalize_key_name(key)
        return self.KEY_CODE_MAP.get(normalized)

    def mouse_click(self, x, y):
        self.mouse_down(x, y)
        self.mouse_up()
        self.logger.debug(f"鼠标点击 ({x}, {y})")

    def mouse_down(self, x, y):
        self.last_x, self.last_y = x, y
        if self._send(InputAction.mouse_down("left", x, y)):
            self.logger.debug(f"鼠标按下 ({x}, {y})")

    def mouse_up(self):
        if self._send(InputAction.mouse_up("left", self.last_x, self.last_y)):
            self.logger.debug(f"鼠标释放 ({self.last_x}, {self.last_y})")

    def mouse_move(self, x, y):
        self.last_x, self.last_y = x, y
        if self._send(InputAction.mouse_move(x, y)):
            self.logger.debug(f"鼠标移动 ({x}, {y})")

    def mouse_scroll(self, count, direction=-1, pause=True):
        if direction == 0:
            return
        # sr2 协议使用 wheel_delta；保持现有语义：direction=-1 向下滚。
        delta = -120 if direction < 0 else 120
        for _ in range(count):
            self._send(InputAction.scroll(delta))
            if pause:
                time.sleep(0.02)
        self.logger.debug(f"滚轮滚动 count={count} direction={direction}")

    def press_key(self, key, wait_time=0.2):
        key_code = self._key_code(key)
        if key_code is None:
            self.logger.error(f"未知按键：{key}")
            return
        self.press_key_down(key)
        time.sleep(wait_time)
        self.press_key_up(key)
        self.logger.debug(f"按键按下：{key}, 持续 {wait_time}s")

    def press_key_down(self, key):
        key_code = self._key_code(key)
        if key_code is None:
            self.logger.error(f"未知按键：{key}")
            return
        if self._send(InputAction.key_down(key_code)):
            self.logger.debug(f"按键按下：{key}")

    def press_key_up(self, key):
        key_code = self._key_code(key)
        if key_code is None:
            self.logger.error(f"未知按键：{key}")
            return
        if self._send(InputAction.key_up(key_code)):
            self.logger.debug(f"按键释放：{key}")

    def secretly_press_key(self, key, wait_time=0.2):
        key_code = self._key_code(key)
        if key_code is None:
            self.logger.error("未知按键")
            return
        self._send(InputAction.key_down(key_code))
        time.sleep(wait_time)
        self._send(InputAction.key_up(key_code))
        self.logger.debug(f"按键按下, 持续 {wait_time}s")

    def press_mouse(self, wait_time=0.2):
        self._send(InputAction.mouse_down("left", self.last_x, self.last_y))
        time.sleep(wait_time)
        self._send(InputAction.mouse_up("left", self.last_x, self.last_y))
        self.logger.debug(f"按下鼠标左键 ({self.last_x}, {self.last_y})")

    def secretly_write(self, text, interval=0.1):
        if self._send(InputAction.ime(text)):
            self.logger.debug("键盘输入 ***")
            return
        for ch in text:
            key_code = self._key_code(ch)
            if key_code is None:
                self.logger.warning("secretly_write 跳过不支持字符")
                continue
            self._send(InputAction.key_down(key_code))
            self._send(InputAction.key_up(key_code))
            time.sleep(interval)
        self.logger.debug("键盘输入 ***")
