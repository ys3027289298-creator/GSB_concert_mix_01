import json
import unittest

from concert import core
from concert.core import ConcertMixer


class MutedPeakTest(unittest.TestCase):
    def test_muted_channel_excluded_from_bus_peak(self):
        mixer = ConcertMixer(channels=3)
        mixer.set_mute(1, True)
        mixer.sample()
        expected = mixer.channel_out(0) + mixer.channel_out(2)
        self.assertEqual(mixer.peak_bus, expected)
        self.assertEqual(mixer.channels[1]["peak"], 0)

    def test_unmute_rejoins_bus(self):
        mixer = ConcertMixer(channels=2)
        mixer.set_mute(0, True)
        mixer.sample()
        muted_peak = mixer.peak_bus
        mixer.set_mute(0, False)
        mixer.sample()
        self.assertEqual(mixer.peak_bus,
                         muted_peak + mixer.channel_out(0))


class SceneManualGainTest(unittest.TestCase):
    def test_manual_gain_survives_scene_switch(self):
        mixer = ConcertMixer(channels=3)
        mixer.turn_knob(0, 15)
        manual_gain = mixer.channels[0]["gain"]
        self.assertEqual(mixer.apply(core.CMD_SCENE, value="高潮"),
                         core.MSG_OK)
        self.assertEqual(mixer.channels[0]["gain"], manual_gain)
        self.assertEqual(mixer.channels[1]["gain"], 80)

    def test_non_manual_channels_take_preset(self):
        mixer = ConcertMixer(channels=2)
        mixer.switch_scene("高潮")
        self.assertEqual([c["gain"] for c in mixer.channels], [80, 80])


class DuplicateKnobTest(unittest.TestCase):
    def test_same_input_id_applied_once(self):
        mixer = ConcertMixer(channels=2)
        before = mixer.channels[0]["gain"]
        self.assertEqual(mixer.turn_knob(0, 10, input_id="k1"),
                         core.MSG_OK)
        self.assertEqual(mixer.turn_knob(0, 10, input_id="k1"),
                         core.MSG_DUPLICATE)
        self.assertEqual(mixer.channels[0]["gain"], before + 10)

    def test_distinct_input_ids_both_apply(self):
        mixer = ConcertMixer(channels=2)
        mixer.turn_knob(0, 10, input_id="a")
        mixer.turn_knob(0, 10, input_id="b")
        self.assertEqual(mixer.channels[0]["gain"],
                         core.DEFAULT_GAIN + 20)


class GainGuardTest(unittest.TestCase):
    def test_gain_cannot_exceed_max(self):
        mixer = ConcertMixer(channels=1)
        mixer.channels[0]["gain"] = core.MAX_GAIN - 5
        self.assertEqual(mixer.turn_knob(0, 20), core.MSG_GAIN_HIGH)
        self.assertEqual(mixer.channels[0]["gain"], core.MAX_GAIN)

    def test_gain_cannot_drop_below_min(self):
        mixer = ConcertMixer(channels=1)
        mixer.channels[0]["gain"] = 5
        self.assertEqual(mixer.turn_knob(0, -20), core.MSG_GAIN_LOW)
        self.assertEqual(mixer.channels[0]["gain"], core.MIN_GAIN)


class ClockPauseTest(unittest.TestCase):
    def test_clock_frozen_while_paused(self):
        mixer = ConcertMixer(channels=1)
        mixer.apply(core.CMD_PAUSE)
        frozen = mixer.clock
        self.assertEqual(mixer.advance_clock(), core.MSG_PAUSE_BLOCKED)
        self.assertEqual(mixer.apply(core.CMD_TICK),
                         core.MSG_PAUSE_BLOCKED)
        self.assertEqual(mixer.clock, frozen)

    def test_clock_advances_after_resume(self):
        mixer = ConcertMixer(channels=1)
        mixer.apply(core.CMD_PAUSE)
        mixer.apply(core.CMD_RESUME)
        mixer.advance_clock()
        self.assertEqual(mixer.clock, core.TIME_STEP)


class UndoSceneTest(unittest.TestCase):
    def test_undo_restores_previous_version(self):
        mixer = ConcertMixer(channels=2)
        mixer.switch_scene("高潮")
        gains_high = [c["gain"] for c in mixer.channels]
        mixer.switch_scene("开场")
        self.assertEqual(mixer.undo_scene(), core.MSG_OK)
        self.assertEqual(mixer.scene, "高潮")
        self.assertEqual([c["gain"] for c in mixer.channels], gains_high)

    def test_undo_restores_manual_gains(self):
        mixer = ConcertMixer(channels=2)
        mixer.turn_knob(0, 7)
        manual = mixer.channels[0]["gain"]
        mixer.switch_scene("高潮")
        mixer.undo_scene()
        self.assertEqual(mixer.channels[0]["gain"], manual)

    def test_undo_without_history_rejected(self):
        mixer = ConcertMixer(channels=1)
        self.assertEqual(mixer.undo_scene(), core.MSG_NO_HISTORY)


class FaultBusTest(unittest.TestCase):
    def test_faulty_channel_removed_from_bus(self):
        mixer = ConcertMixer(channels=3)
        self.assertEqual(mixer.apply(core.CMD_FAULT, index=1),
                         core.MSG_FAULT)
        self.assertNotIn(1, mixer.bus)
        mixer.sample()
        expected = mixer.channel_out(0) + mixer.channel_out(2)
        self.assertEqual(mixer.peak_bus, expected)

    def test_knob_blocked_on_faulty_channel(self):
        mixer = ConcertMixer(channels=2)
        mixer.report_fault(0)
        before = mixer.channels[0]["gain"]
        self.assertEqual(mixer.turn_knob(0, 10),
                         core.MSG_CHANNEL_FAULT)
        self.assertEqual(mixer.channels[0]["gain"], before)

    def test_repair_rejoins_bus(self):
        mixer = ConcertMixer(channels=2)
        mixer.report_fault(0)
        self.assertEqual(mixer.repair_channel(0), core.MSG_REPAIRED)
        self.assertIn(0, mixer.bus)
        self.assertFalse(mixer.channels[0]["fault"])

    def test_repair_without_fault_rejected(self):
        mixer = ConcertMixer(channels=1)
        self.assertEqual(mixer.repair_channel(0), core.MSG_NO_FAULT)


class PeakPersistenceTest(unittest.TestCase):
    def test_peak_kept_after_load(self):
        mixer = ConcertMixer(channels=3)
        mixer.sample()
        peak = mixer.peak_bus
        restored = ConcertMixer.load(json.loads(json.dumps(mixer.save())))
        self.assertEqual(restored.peak_bus, peak)
        self.assertEqual([c["peak"] for c in restored.channels],
                         [c["peak"] for c in mixer.channels])

    def test_roundtrip_state_equal(self):
        mixer = ConcertMixer(channels=3)
        mixer.turn_knob(0, 10, input_id="x")
        mixer.switch_scene("高潮")
        mixer.sample()
        mixer.report_fault(2)
        restored = ConcertMixer.load(json.loads(json.dumps(mixer.save())))
        self.assertEqual(restored.save(), mixer.save())
        self.assertEqual(set(mixer.save()), set(core.SAVE_KEYS))


class IllegalCommandTest(unittest.TestCase):
    def test_unknown_command_rejected(self):
        mixer = ConcertMixer(channels=2)
        snapshot = mixer.save()
        self.assertEqual(mixer.apply("砸掉混音台"), core.MSG_INVALID)
        self.assertEqual(mixer.save(), snapshot)

    def test_empty_command_rejected(self):
        mixer = ConcertMixer(channels=2)
        self.assertEqual(mixer.apply(""), core.MSG_INVALID)

    def test_bad_index_rejected(self):
        mixer = ConcertMixer(channels=2)
        self.assertEqual(mixer.turn_knob(9, 10), core.MSG_BAD_INDEX)
        self.assertEqual(mixer.turn_knob(-1, 10), core.MSG_BAD_INDEX)
        self.assertEqual(mixer.apply(core.CMD_MUTE, index=5),
                         core.MSG_BAD_INDEX)

    def test_unknown_scene_rejected(self):
        mixer = ConcertMixer(channels=1)
        self.assertEqual(mixer.switch_scene("返场"), core.MSG_NO_SCENE)
        self.assertEqual(mixer.history, [])


class EmptyDataTest(unittest.TestCase):
    def test_load_empty_save_rejected(self):
        with self.assertRaises(ValueError):
            ConcertMixer.load({})

    def test_load_none_rejected(self):
        with self.assertRaises(ValueError):
            ConcertMixer.load(None)

    def test_load_missing_keys_rejected(self):
        with self.assertRaises(ValueError):
            ConcertMixer.load({"clock": 10, "scene": "开场"})


class CapacityBoundaryTest(unittest.TestCase):
    def test_channels_cannot_exceed_capacity(self):
        mixer = ConcertMixer(channels=0)
        results = [mixer.add_channel() for _ in range(core.MAX_CHANNELS + 1)]
        self.assertEqual(results[:-1], [core.MSG_OK] * core.MAX_CHANNELS)
        self.assertEqual(results[-1], core.MSG_CHANNELS_FULL)
        self.assertEqual(len(mixer.channels), core.MAX_CHANNELS)
        self.assertEqual(len(mixer.bus), core.MAX_CHANNELS)

    def test_gain_boundary_exact_max_ok(self):
        mixer = ConcertMixer(channels=1)
        mixer.channels[0]["gain"] = core.MAX_GAIN - core.KNOB_STEP
        self.assertEqual(mixer.turn_knob(0, core.KNOB_STEP),
                         core.MSG_OK)
        self.assertEqual(mixer.channels[0]["gain"], core.MAX_GAIN)


class DuplicateInputTest(unittest.TestCase):
    def test_repeated_sample_idempotent_peak(self):
        mixer = ConcertMixer(channels=2)
        mixer.sample()
        first = mixer.peak_bus
        mixer.sample()
        self.assertEqual(mixer.peak_bus, first)
        self.assertEqual(len(mixer.log), 2)

    def test_duplicate_fault_report_does_not_corrupt_bus(self):
        mixer = ConcertMixer(channels=2)
        mixer.report_fault(0)
        mixer.report_fault(0)
        self.assertEqual(mixer.bus, [1])


class DemoTest(unittest.TestCase):
    def test_fixed_demo_runs(self):
        mixer = core.run_demo()
        self.assertIsInstance(mixer, ConcertMixer)
        self.assertGreater(mixer.clock, 0)

    def test_demo_is_deterministic(self):
        first = core.run_demo().save()
        second = core.run_demo().save()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
