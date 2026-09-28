"""混音台演出后端。

状态不变式（修复后的一致状态）：
1. 峰值只统计未静音且挂在总线上的通道；静音或故障通道被跳过。
2. 手动增益属于通道本身，切换场景只覆盖未手动调整过的通道，
   手动增益不会丢失。
3. 旋钮输入按 input_id 去重：相同 input_id 重复提交只生效一次。
4. 增益钳制在 [0, 100]，超限输入被保护并给出提示。
5. 演出时钟只在非暂停状态下推进；暂停期间读数完全冻结。
6. 场景切换前整体快照压栈，撤销恢复的是切换前那一版（深度快照）。
7. 通道故障时立即从总线移除，修复后才重新挂回。
8. 峰值计数器写入存档，读档后保留，不会归零。
"""
from __future__ import annotations

import copy
import json

SAVE_KEYS = (
    "channels", "bus", "scenes", "scene", "manual", "history",
    "peak_bus", "clock", "paused", "seen_inputs", "log",
)

MAX_CHANNELS = 8
MIN_GAIN = 0
MAX_GAIN = 100
DEFAULT_GAIN = 50
KNOB_STEP = 10
TIME_STEP = 30

MSG_OK = "命令已执行"
MSG_INVALID = "未知命令"
MSG_BAD_INDEX = "通道编号无效"
MSG_NO_CHANNEL = "通道不存在"
MSG_CHANNELS_FULL = "通道容量已满"
MSG_CHANNEL_FAULT = "通道故障，操作被阻止"
MSG_MUTED = "通道已静音"
MSG_UNMUTED = "通道已取消静音"
MSG_GAIN_LOW = "增益已到下限"
MSG_GAIN_HIGH = "增益已到上限"
MSG_DUPLICATE = "重复输入已忽略"
MSG_NO_SCENE = "场景不存在"
MSG_NO_HISTORY = "没有可撤销的场景"
MSG_SCENE_SAVED = "场景已保存"
MSG_FAULT = "通道故障，已从总线移除"
MSG_REPAIRED = "通道已修复并挂回总线"
MSG_NO_FAULT = "通道没有故障"
MSG_PAUSED = "演出已暂停"
MSG_RESUMED = "演出已继续"
MSG_PAUSE_BLOCKED = "演出暂停中，时钟不推进"

CMD_KNOB = "转动旋钮"
CMD_MUTE = "静音"
CMD_UNMUTE = "取消静音"
CMD_SCENE = "切换场景"
CMD_SAVE_SCENE = "保存场景"
CMD_UNDO_SCENE = "撤销场景"
CMD_SAMPLE = "采样"
CMD_FAULT = "报告故障"
CMD_REPAIR = "修复通道"
CMD_ADD_CHANNEL = "添加通道"
CMD_PAUSE = "暂停演出"
CMD_RESUME = "继续演出"
CMD_TICK = "推进时钟"


class ConcertMixer:
    def __init__(self, channels=4):
        channels = max(0, min(MAX_CHANNELS, channels))
        self.channels = [
            {"name": "CH%d" % (i + 1), "gain": DEFAULT_GAIN,
             "level": 60, "muted": False, "fault": False,
             "peak": 0}
            for i in range(channels)
        ]
        # bus 保存挂在总线上的通道下标；故障会真正从这里摘除。
        self.bus = list(range(channels))
        self.scenes = {
            "开场": {"gains": [60] * channels + [0] * (MAX_CHANNELS - channels),
                     "muted": [False] * MAX_CHANNELS},
            "高潮": {"gains": [80] * channels + [0] * (MAX_CHANNELS - channels),
                     "muted": [False] * MAX_CHANNELS},
        }
        self.scene = "开场"
        self.manual = [False] * channels
        self.history = []
        self.peak_bus = 0
        self.clock = 0
        self.paused = False
        self.seen_inputs = []
        self.log = []

    # ---- 基础查询 -----------------------------------------------------
    def active_indices(self):
        """总线上、未静音、未故障的通道下标。"""
        return [i for i in self.bus
                if not self.channels[i]["muted"]
                and not self.channels[i]["fault"]]

    def channel_out(self, index):
        channel = self.channels[index]
        return channel["level"] * channel["gain"] // 100

    def bus_level(self):
        total = 0
        for index in self.active_indices():
            total += self.channel_out(index)
        return total

    # ---- 旋钮与静音 ---------------------------------------------------
    def turn_knob(self, index, delta=KNOB_STEP, input_id=None):
        # 去重先于一切：重复 input_id 绝不重复应用，也不改任何状态。
        if input_id is not None:
            if input_id in self.seen_inputs:
                return MSG_DUPLICATE
            self.seen_inputs.append(input_id)
        if not isinstance(index, int) or isinstance(index, bool):
            return MSG_BAD_INDEX
        if index < 0 or index >= len(self.channels):
            return MSG_BAD_INDEX
        channel = self.channels[index]
        if channel["fault"]:
            return MSG_CHANNEL_FAULT
        new_gain = channel["gain"] + delta
        if new_gain < MIN_GAIN:
            channel["gain"] = MIN_GAIN
            self.manual[index] = True
            return MSG_GAIN_LOW
        if new_gain > MAX_GAIN:
            channel["gain"] = MAX_GAIN
            self.manual[index] = True
            return MSG_GAIN_HIGH
        channel["gain"] = new_gain
        self.manual[index] = True
        return MSG_OK

    def set_mute(self, index, muted):
        if not isinstance(index, int) or isinstance(index, bool):
            return MSG_BAD_INDEX
        if index < 0 or index >= len(self.channels):
            return MSG_BAD_INDEX
        channel = self.channels[index]
        if channel["fault"]:
            return MSG_CHANNEL_FAULT
        channel["muted"] = muted
        return MSG_MUTED if muted else MSG_UNMUTED

    # ---- 通道与故障 ---------------------------------------------------
    def add_channel(self, level=60):
        if len(self.channels) >= MAX_CHANNELS:
            return MSG_CHANNELS_FULL
        index = len(self.channels)
        self.channels.append(
            {"name": "CH%d" % (index + 1), "gain": DEFAULT_GAIN,
             "level": level, "muted": False, "fault": False, "peak": 0})
        self.bus.append(index)
        self.manual.append(False)
        for scene in self.scenes.values():
            scene["gains"][index] = DEFAULT_GAIN
            scene["muted"][index] = False
        return MSG_OK

    def report_fault(self, index):
        if not isinstance(index, int) or isinstance(index, bool) \
                or index < 0 or index >= len(self.channels):
            return MSG_BAD_INDEX
        channel = self.channels[index]
        channel["fault"] = True
        if index in self.bus:
            self.bus.remove(index)
        return MSG_FAULT

    def repair_channel(self, index):
        if not isinstance(index, int) or isinstance(index, bool) \
                or index < 0 or index >= len(self.channels):
            return MSG_BAD_INDEX
        channel = self.channels[index]
        if not channel["fault"]:
            return MSG_NO_FAULT
        channel["fault"] = False
        if index not in self.bus:
            self.bus.append(index)
            self.bus.sort()
        return MSG_REPAIRED

    # ---- 采样与峰值 ---------------------------------------------------
    def sample(self):
        for index in self.active_indices():
            out = self.channel_out(index)
            if out > self.channels[index]["peak"]:
                self.channels[index]["peak"] = out
        level = self.bus_level()
        if level > self.peak_bus:
            self.peak_bus = level
        self.log.append(level)
        return MSG_OK

    # ---- 场景 ---------------------------------------------------------
    def _snapshot(self):
        return {
            "scene": self.scene,
            "gains": [c["gain"] for c in self.channels],
            "muted": [c["muted"] for c in self.channels],
            "manual": list(self.manual),
        }

    def switch_scene(self, name):
        if name not in self.scenes:
            return MSG_NO_SCENE
        # 切换前整体压栈：撤销永远回到“切换前那一版”。
        self.history.append(copy.deepcopy(self._snapshot()))
        preset = self.scenes[name]
        for index, channel in enumerate(self.channels):
            if self.manual[index]:
                continue  # 手动增益跨场景保留
            channel["gain"] = preset["gains"][index]
            channel["muted"] = preset["muted"][index]
        self.scene = name
        return MSG_OK

    def undo_scene(self):
        if not self.history:
            return MSG_NO_HISTORY
        snapshot = self.history.pop()
        for index, channel in enumerate(self.channels):
            channel["gain"] = snapshot["gains"][index]
            channel["muted"] = snapshot["muted"][index]
        self.manual = list(snapshot["manual"])
        self.scene = snapshot["scene"]
        return MSG_OK

    def save_scene(self, name):
        self.scenes[name] = {
            "gains": [c["gain"] for c in self.channels]
                     + [0] * (MAX_CHANNELS - len(self.channels)),
            "muted": [c["muted"] for c in self.channels]
                     + [False] * (MAX_CHANNELS - len(self.channels)),
        }
        return MSG_SCENE_SAVED

    # ---- 演出时钟 -----------------------------------------------------
    def advance_clock(self, seconds=TIME_STEP):
        if self.paused:
            return MSG_PAUSE_BLOCKED
        self.clock += seconds
        return MSG_OK

    # ---- 命令分发 -----------------------------------------------------
    def apply(self, command, index=None, value=None, input_id=None):
        if command == CMD_KNOB:
            return self.turn_knob(index, KNOB_STEP if value is None else value,
                                  input_id)
        if command == CMD_MUTE:
            return self.set_mute(index, True)
        if command == CMD_UNMUTE:
            return self.set_mute(index, False)
        if command == CMD_SCENE:
            return self.switch_scene(value)
        if command == CMD_SAVE_SCENE:
            return self.save_scene(value)
        if command == CMD_UNDO_SCENE:
            return self.undo_scene()
        if command == CMD_SAMPLE:
            return self.sample()
        if command == CMD_FAULT:
            return self.report_fault(index)
        if command == CMD_REPAIR:
            return self.repair_channel(index)
        if command == CMD_ADD_CHANNEL:
            return self.add_channel(60)
        if command == CMD_PAUSE:
            self.paused = True
            return MSG_PAUSED
        if command == CMD_RESUME:
            self.paused = False
            return MSG_RESUMED
        if command == CMD_TICK:
            return self.advance_clock(
                TIME_STEP if value is None else value)
        return MSG_INVALID

    # ---- 存档 ---------------------------------------------------------
    def save(self):
        return {key: copy.deepcopy(getattr(self, key)) for key in SAVE_KEYS}

    @classmethod
    def load(cls, data):
        if not data:
            raise ValueError("存档数据为空")
        missing = [key for key in SAVE_KEYS if key not in data]
        if missing:
            raise ValueError("存档缺少键: %s" % ",".join(missing))
        mixer = cls(channels=0)
        mixer.channels = copy.deepcopy(data["channels"])
        mixer.bus = [int(i) for i in data["bus"]]
        mixer.scenes = copy.deepcopy(data["scenes"])
        mixer.scene = data["scene"]
        mixer.manual = [bool(v) for v in data["manual"]]
        mixer.history = copy.deepcopy(data["history"])
        mixer.peak_bus = int(data["peak_bus"])
        mixer.clock = int(data["clock"])
        mixer.paused = bool(data["paused"])
        mixer.seen_inputs = list(data["seen_inputs"])
        mixer.log = list(data["log"])
        return mixer


def run_demo():
    """固定输入演示：脚本化命令序列，输出确定，不依赖外部状态。"""
    mixer = ConcertMixer(channels=4)
    script = [
        (CMD_KNOB, 0, 20, None),
        (CMD_KNOB, 0, 20, "knob-1"),
        (CMD_KNOB, 0, 20, "knob-1"),
        (CMD_KNOB, 1, -10, None),
        (CMD_MUTE, 2, None, None),
        (CMD_SAMPLE, None, None, None),
        (CMD_SCENE, None, "高潮", None),
        (CMD_SAMPLE, None, None, None),
        (CMD_UNDO_SCENE, None, None, None),
        (CMD_FAULT, 3, None, None),
        (CMD_SAMPLE, None, None, None),
        (CMD_REPAIR, 3, None, None),
        (CMD_PAUSE, None, None, None),
        (CMD_TICK, None, None, None),
        (CMD_RESUME, None, None, None),
        (CMD_TICK, None, None, None),
        (CMD_UNMUTE, 2, None, None),
        (CMD_SAMPLE, None, None, None),
    ]
    for command, index, value, input_id in script:
        result = mixer.apply(command, index=index, value=value,
                             input_id=input_id)
        print("[%s] 时钟%02d:%02d 总线峰值=%d -> %s"
              % (command, mixer.clock // 60, mixer.clock % 60,
                 mixer.peak_bus, result))
    state = mixer.save()
    restored = ConcertMixer.load(json.loads(json.dumps(state)))
    print("存档:", {key: state[key] for key in
                    ("scene", "clock", "paused", "peak_bus", "bus")})
    print("读档峰值保持:", restored.peak_bus == mixer.peak_bus)
    return mixer


if __name__ == "__main__":
    run_demo()
