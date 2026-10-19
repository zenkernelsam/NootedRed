"""Check test-case planning without invoking VideoToolbox or GPU hardware."""
from types import SimpleNamespace

from run_video_encode_test import build_cases


def main():
    baseline = build_cases(SimpleNamespace(hardware=False, hardware_decode=False))
    assert baseline == [
        ("list", ["--list"]),
        ("software-h264", ["--software", "h264"]),
        ("software-hevc", ["--software", "hevc"]),
    ]

    decoder = build_cases(SimpleNamespace(hardware=False, hardware_decode=True))
    assert decoder == [
        ("list", ["--list"]),
        ("software-h264", ["--software", "h264", "--hardware-decode"]),
        ("software-hevc", ["--software", "hevc", "--hardware-decode"]),
    ]

    both = build_cases(SimpleNamespace(hardware=True, hardware_decode=True))
    assert len(both) == 7
    assert both[0] == ("list", ["--list"])
    assert all(options[-1] == "--hardware-decode" for _, options in both[1:])
    assert {label for label, _ in both[1:]} == {
        "software-h264", "software-hevc", "auto-h264", "auto-hevc", "hardware-h264", "hardware-hevc"
    }
    print("PASS: baseline unchanged; decoder-only and combined plans are separated; no hardware action executed")


if __name__ == "__main__":
    main()
