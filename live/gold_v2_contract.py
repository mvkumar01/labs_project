"""Stable contract shared by the Gold selector, runner and execution gates."""
VERSION = "gold_cci_v2"
ABI = "gold-cci-v2-di14-bb20-net3-live-v1"
ENGINE = "gold_cci"


def selected(version):
    return version == VERSION
