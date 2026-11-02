"""Stable contract shared by the CRUDEOILM combination's selector, runner and execution gates."""
VERSION = "crudem_combo_v1"
ABI = "crudem-combo-c8-live-v1"
ENGINE = "crudem_combo"


def selected(version):
    return version == VERSION
