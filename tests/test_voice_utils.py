import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("future_main", ROOT / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DummyPyAudio:
    def __init__(self, devices):
        self._devices = devices

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, index):
        return self._devices[index]


def test_select_microphone_device_prefers_default_input():
    devices = [
        {"index": 0, "maxInputChannels": 0, "isDefaultInput": False},
        {"index": 1, "maxInputChannels": 2, "isDefaultInput": True},
        {"index": 2, "maxInputChannels": 2, "isDefaultInput": False},
    ]

    assert module.select_microphone_device(DummyPyAudio(devices)) == 1


def test_select_microphone_device_falls_back_to_first_input():
    devices = [
        {"index": 0, "maxInputChannels": 2, "isDefaultInput": False},
        {"index": 1, "maxInputChannels": 0, "isDefaultInput": False},
    ]

    assert module.select_microphone_device(DummyPyAudio(devices)) == 0


def test_detect_wake_word_matches_future_phrase():
    assert module.detect_wake_word("Future, can you hear me?")


def test_detect_wake_word_rejects_other_text():
    assert not module.detect_wake_word("Please help me with this")
