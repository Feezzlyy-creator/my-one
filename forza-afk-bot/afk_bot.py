"""AFK-бот для Forza Horizon 6.

Крутит по кругу сценарий из config.toml — газ, руль, нажатия кнопок — через
виртуальный геймпад Xbox 360 (vgamepad) или клавиатуру (SendInput).

    python afk_bot.py              # обычный запуск
    python afk_bot.py --dry-run    # ничего не нажимать, только показать действия

Горячие клавиши по умолчанию: F8 — пауза/продолжить, F10 — остановить.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
TICK = 0.05  # как часто проверять горячие клавиши и фокус окна, сек

BUTTONS = ("A", "B", "X", "Y", "LB", "RB", "START", "BACK", "LS", "RS",
           "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")

DEFAULTS = {
    "input": "gamepad",
    "window_title": "Forza Horizon",
    "require_focus": True,
    "start_delay": 5,
    "max_cycles": 0,
    "max_minutes": 0,
}
DEFAULT_HOTKEYS = {"pause": "F8", "stop": "F10"}
DEFAULT_KEYMAP = {
    "throttle": "w", "brake": "s", "steer_left": "a", "steer_right": "d",
    "A": "enter", "B": "backspace", "X": "x", "Y": "y", "START": "esc", "BACK": "tab",
    "DPAD_UP": "up", "DPAD_DOWN": "down", "DPAD_LEFT": "left", "DPAD_RIGHT": "right",
}


def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def fail(msg: str):
    sys.exit(f"Ошибка: {msg}")


def fmt_duration(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(seconds))


class StopBot(Exception):
    """Бота остановили горячей клавишей или сработал лимит."""


@dataclass(frozen=True)
class InputState:
    """Что сейчас зажато на «контроллере»."""
    throttle: float = 0.0  # газ, 0..1
    brake: float = 0.0     # тормоз, 0..1
    steer: float = 0.0     # руль, -1 (влево) .. 1 (вправо)
    buttons: frozenset[str] = frozenset()

    def describe(self) -> str:
        parts = []
        if self.throttle:
            parts.append(f"газ {self.throttle:g}")
        if self.brake:
            parts.append(f"тормоз {self.brake:g}")
        if self.steer:
            parts.append(f"руль {self.steer:+g}")
        parts += sorted(self.buttons)
        return ", ".join(parts) or "всё отпущено"


IDLE = InputState()


# ── Win32 ────────────────────────────────────────────────────────────────────

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):  # нужен только для правильного размера INPUT
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _U)]


INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]


def _scan_codes() -> dict[str, int]:
    """Скан-коды (set 1). Стрелки — «расширенные» клавиши с префиксом E0."""
    codes = {"esc": 0x01, "backspace": 0x0E, "tab": 0x0F, "enter": 0x1C, "ctrl": 0x1D,
             "shift": 0x2A, "alt": 0x38, "space": 0x39,
             "up": 0xE048, "left": 0xE04B, "right": 0xE04D, "down": 0xE050}
    for row, first in (("1234567890", 0x02), ("qwertyuiop", 0x10), ("asdfghjkl", 0x1E), ("zxcvbnm", 0x2C)):
        codes.update((ch, first + i) for i, ch in enumerate(row))
    return codes


SCAN_CODES = _scan_codes()


def send_key(code: int, up: bool) -> None:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if code > 0xFF:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(wScan=code & 0xFF, dwFlags=flags))
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def key_is_down(vk: int) -> bool:
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def foreground_window_title() -> str:
    hwnd = _user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(_user32.GetWindowTextLengthW(hwnd) + 1)
    _user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def hotkey_vk(name: str) -> int:
    key = name.strip().upper()
    if key[:1] == "F" and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        return 0x6F + int(key[1:])  # VK_F1 = 0x70
    fail(f"горячая клавиша {name!r} не поддерживается, используйте F1–F12")


# ── Способы ввода ────────────────────────────────────────────────────────────

class DryRunBackend:
    """Ничего не нажимает, только пишет в лог — для проверки сценария."""

    def __init__(self):
        self.last = IDLE

    def apply(self, state: InputState) -> None:
        if state != self.last and state != IDLE:
            log(f"    ввод: {state.describe()}")
        self.last = state

    def tick(self, now: float) -> None:
        pass

    def close(self) -> None:
        pass


class GamepadBackend:
    """Виртуальный геймпад Xbox 360 через vgamepad (драйвер ViGEmBus)."""

    def __init__(self):
        try:
            import vgamepad as vg
        except ImportError:
            fail("не установлен vgamepad — запустите start.bat (он поставит его сам)\n"
                 "или выполните: python -m pip install -r requirements.txt\n"
                 "Можно и без него: input = \"keyboard\" в config.toml")
        b = vg.XUSB_BUTTON
        self.buttons = {
            "A": b.XUSB_GAMEPAD_A, "B": b.XUSB_GAMEPAD_B, "X": b.XUSB_GAMEPAD_X, "Y": b.XUSB_GAMEPAD_Y,
            "LB": b.XUSB_GAMEPAD_LEFT_SHOULDER, "RB": b.XUSB_GAMEPAD_RIGHT_SHOULDER,
            "START": b.XUSB_GAMEPAD_START, "BACK": b.XUSB_GAMEPAD_BACK,
            "LS": b.XUSB_GAMEPAD_LEFT_THUMB, "RS": b.XUSB_GAMEPAD_RIGHT_THUMB,
            "DPAD_UP": b.XUSB_GAMEPAD_DPAD_UP, "DPAD_DOWN": b.XUSB_GAMEPAD_DPAD_DOWN,
            "DPAD_LEFT": b.XUSB_GAMEPAD_DPAD_LEFT, "DPAD_RIGHT": b.XUSB_GAMEPAD_DPAD_RIGHT,
        }
        try:
            self.pad = vg.VX360Gamepad()
        except Exception as e:
            fail(f"не удалось создать виртуальный геймпад ({e}).\n"
                 "Проверьте, что установлен драйвер ViGEmBus, или используйте input = \"keyboard\".")

    def apply(self, state: InputState) -> None:
        pad = self.pad
        pad.reset()
        for name in state.buttons:
            pad.press_button(button=self.buttons[name])
        pad.right_trigger_float(value_float=float(state.throttle))
        pad.left_trigger_float(value_float=float(state.brake))
        pad.left_joystick_float(x_value_float=float(state.steer), y_value_float=0.0)
        pad.update()

    def tick(self, now: float) -> None:
        pass

    def close(self) -> None:
        self.apply(IDLE)


class KeyboardBackend:
    """Клавиатура через SendInput со скан-кодами — их видят и игры на DirectInput.

    Газ, тормоз и руль на клавиатуре цифровые, поэтому значения меньше 1
    имитируются ШИМ: клавиша зажата соответствующую долю каждого периода.
    """

    PWM_PERIOD = 0.4  # сек

    def __init__(self, keys: dict[str, int]):
        self.keys = keys
        self.state = IDLE
        self.down: set[int] = set()

    def apply(self, state: InputState) -> None:
        self.state = state
        self.tick(time.monotonic())

    def tick(self, now: float) -> None:
        s = self.state
        phase = (now % self.PWM_PERIOD) / self.PWM_PERIOD
        actions = set(s.buttons)
        if phase < s.throttle:
            actions.add("throttle")
        if phase < s.brake:
            actions.add("brake")
        if phase < abs(s.steer):
            actions.add("steer_right" if s.steer > 0 else "steer_left")
        want = {self.keys[a] for a in actions}
        for code in self.down - want:
            send_key(code, up=True)
        for code in want - self.down:
            send_key(code, up=False)
        self.down = want

    def close(self) -> None:
        self.apply(IDLE)


# ── Конфиг ───────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            loaded = tomllib.load(f)
    except FileNotFoundError:
        fail(f"не найден файл настроек {path}")
    except tomllib.TOMLDecodeError as e:
        fail(f"в {path.name} ошибка синтаксиса: {e}")

    cfg = DEFAULTS | loaded
    cfg["hotkeys"] = DEFAULT_HOTKEYS | loaded.get("hotkeys", {})
    cfg["keyboard"] = DEFAULT_KEYMAP | loaded.get("keyboard", {})
    if cfg["input"] not in ("gamepad", "keyboard"):
        fail(f"input должен быть \"gamepad\" или \"keyboard\", а не {cfg['input']!r}")
    validate_cycle(cfg.get("cycle"))
    return cfg


def validate_cycle(steps: list[dict] | None) -> None:
    if not steps:
        fail("в config.toml нет ни одного шага [[cycle]]")
    for i, step in enumerate(steps, 1):
        where = f"шаг {i} в [[cycle]]"
        action = step.get("action")
        if action not in ("drive", "press", "wait"):
            fail(f"{where}: action должен быть drive, press или wait, а не {action!r}")
        if action in ("drive", "wait") and not isinstance(step.get("duration"), (int, float)):
            fail(f"{where}: не указан duration (сколько секунд)")
        if action == "press" and "button" not in step:
            fail(f"{where}: не указана кнопка button")
        for key, low in (("throttle", 0), ("brake", 0), ("steer", -1)):
            if not low <= step.get(key, 0) <= 1:
                fail(f"{where}: {key} должен быть от {low} до 1")
        for button in step_buttons(step):
            if button not in BUTTONS:
                fail(f"{where}: неизвестная кнопка {button!r}, доступны: {' '.join(BUTTONS)}")


def step_buttons(step: dict) -> list[str]:
    return list(step.get("hold", [])) + ([step["button"]] if "button" in step else [])


def resolve_keymap(keymap: dict[str, str], steps: list[dict]) -> dict[str, int]:
    """Скан-коды для всех действий, которые встречаются в сценарии."""
    needed = {"throttle", "brake", "steer_left", "steer_right"}
    for step in steps:
        needed.update(step_buttons(step))
    codes = {}
    for action in sorted(needed):
        key = str(keymap.get(action, "")).lower()
        if key not in SCAN_CODES:
            fail(f"[keyboard] {action} = {key!r}: такую клавишу бот не знает.\n"
                 f"Доступны: {' '.join(SCAN_CODES)}")
        codes[action] = SCAN_CODES[key]
    return codes


def cycle_length(steps: list[dict]) -> float:
    total = 0.0
    for step in steps:
        if step["action"] == "press":
            total += step.get("count", 1) * (step.get("hold_time", 0.15) + step.get("interval", 1.0))
        else:
            total += step["duration"]
    return total


# ── Исполнитель сценария ─────────────────────────────────────────────────────

class Runner:
    def __init__(self, backend, cfg: dict, use_hotkeys: bool, check_focus: bool):
        self.backend = backend
        self.state = IDLE
        self.paused = False
        self.use_hotkeys = use_hotkeys
        self.check_focus = check_focus
        self.window = cfg["window_title"].lower()
        self.pause_key = cfg["hotkeys"]["pause"]
        self.vk_pause = hotkey_vk(cfg["hotkeys"]["pause"])
        self.vk_stop = hotkey_vk(cfg["hotkeys"]["stop"])
        self.was_down: dict[int, bool] = {}

    def run(self, steps: list[dict], start_delay: float, max_cycles: int, max_minutes: float) -> None:
        log(f"старт через {start_delay} с — переключитесь в окно игры")
        self.sleep(start_delay)
        started = time.monotonic()
        n = 0
        while True:
            n += 1
            total = f" из {max_cycles}" if max_cycles else ""
            log(f"── цикл {n}{total}, бот работает {fmt_duration(time.monotonic() - started)}")
            for step in steps:
                self.run_step(step)
            if max_cycles and n >= max_cycles:
                raise StopBot(f"сделано циклов: {n}")
            if max_minutes and time.monotonic() - started >= max_minutes * 60:
                raise StopBot(f"прошло {max_minutes} мин")

    def run_step(self, step: dict) -> None:
        note = f" — {step['note']}" if "note" in step else ""
        if step["action"] == "drive":
            state = InputState(step.get("throttle", 0.0), step.get("brake", 0.0), step.get("steer", 0.0),
                               frozenset(step.get("hold", ())))
            log(f"еду {step['duration']} с ({state.describe()}){note}")
            self.set(state)
            self.sleep(step["duration"])
            self.set(IDLE)
        elif step["action"] == "press":
            count = step.get("count", 1)
            log(f"жму {step['button']} ×{count}{note}")
            for _ in range(count):
                self.set(InputState(buttons=frozenset({step["button"]})))
                self.sleep(step.get("hold_time", 0.15))
                self.set(IDLE)
                self.sleep(step.get("interval", 1.0))
        else:
            log(f"жду {step['duration']} с{note}")
            self.sleep(step["duration"])

    def set(self, state: InputState) -> None:
        self.state = state
        self.backend.apply(state)

    def sleep(self, seconds: float) -> None:
        """Ждёт seconds, следя за горячими клавишами и фокусом. Время на паузе не засчитывается."""
        end = time.monotonic() + seconds
        while True:
            end += self._wait_while_blocked()
            now = time.monotonic()
            if now >= end:
                return
            self.backend.tick(now)
            time.sleep(min(TICK, end - now))

    def _wait_while_blocked(self) -> float:
        """На паузе или без фокуса отпускает всё и ждёт. Возвращает время простоя."""
        reason = self._block_reason()
        if not reason:
            return 0.0
        started = time.monotonic()
        self.backend.apply(IDLE)
        logged = None
        while reason:
            if reason != logged:
                log(reason)
                logged = reason
            time.sleep(TICK)
            reason = self._block_reason()
        log("продолжаю")
        self.backend.apply(self.state)
        return time.monotonic() - started

    def _block_reason(self) -> str | None:
        if self.use_hotkeys:
            if self._pressed(self.vk_stop):
                raise StopBot("остановлено горячей клавишей")
            if self._pressed(self.vk_pause):
                self.paused = not self.paused
        if self.paused:
            return f"пауза — нажмите {self.pause_key}, чтобы продолжить"
        if self.check_focus and self.window not in foreground_window_title().lower():
            return "окно игры не активно — кнопки отпущены, жду"
        return None

    def _pressed(self, vk: int) -> bool:
        down = key_is_down(vk)
        was = self.was_down.get(vk, False)
        self.was_down[vk] = down
        return down and not was


def main() -> None:
    parser = argparse.ArgumentParser(description="AFK-бот для Forza Horizon 6")
    parser.add_argument("-c", "--config", type=Path, default=Path(__file__).with_name("config.toml"),
                        help="файл настроек (по умолчанию config.toml рядом со скриптом)")
    parser.add_argument("--dry-run", action="store_true", help="ничего не нажимать, только показывать действия")
    parser.add_argument("--input", choices=("gamepad", "keyboard"), help="перебить input из конфига")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.input:
        cfg["input"] = args.input
    steps = cfg["cycle"]
    keys = resolve_keymap(cfg["keyboard"], steps) if cfg["input"] == "keyboard" else None

    if args.dry_run:
        backend = DryRunBackend()
    elif not IS_WINDOWS:
        fail("управлять игрой бот умеет только в Windows; здесь можно проверить сценарий с --dry-run")
    elif cfg["input"] == "gamepad":
        backend = GamepadBackend()
    else:
        backend = KeyboardBackend(keys)

    runner = Runner(backend, cfg, use_hotkeys=IS_WINDOWS,
                    check_focus=IS_WINDOWS and cfg["require_focus"] and not args.dry_run)
    mode = "проверка (--dry-run)" if args.dry_run else cfg["input"]
    log(f"Forza AFK-бот: ввод — {mode}, шагов — {len(steps)}, цикл ≈ {fmt_duration(cycle_length(steps))}")
    if IS_WINDOWS:
        log(f"{cfg['hotkeys']['pause']} — пауза/продолжить, {cfg['hotkeys']['stop']} — стоп")
    try:
        runner.run(steps, cfg["start_delay"], cfg["max_cycles"], cfg["max_minutes"])
    except StopBot as e:
        log(f"стоп: {e}")
    except KeyboardInterrupt:
        log("стоп: Ctrl+C")
    finally:
        backend.close()


if __name__ == "__main__":
    main()
