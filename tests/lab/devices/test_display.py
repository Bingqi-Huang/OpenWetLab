"""Every device names itself and every key reading in Chinese, in one place (spec.py)."""

from wetlab.lab.devices import DEVICE_LABELS, ISLAND, PROPS
from wetlab.lab.protocol import describe_step


def test_every_key_prop_has_a_display_entry_and_nothing_else() -> None:
    for dev in ISLAND:
        assert dev.spec.LABEL
        assert set(dev.spec.PROPS) == set(dev.spec.KEY_PROPS), dev.spec.NAME
        assert all(p.label for p in dev.spec.PROPS.values())
    assert DEVICE_LABELS["pcr"] == "PCR仪"
    assert PROPS["shaker"]["ElmState"].values[1] == "夹紧"


def test_wait_until_is_described_with_chinese_reading_names_and_meanings() -> None:
    text = describe_step({"wait_until": {"device": "pcr", "state": {"PcrState": "Idle"}, "max_seconds": 1800}})
    assert text == "等待 PCR仪 运行状态变为空闲（最长 30 分钟）"
    assert describe_step({"skill": "pcr_close", "args": {}}) == "关闭 PCR 仪仓门"
