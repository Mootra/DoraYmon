import json

import pytest

from doraymon.ops.napcat import NapcatConfigError, parse_endpoint, read_integration


def write(tmp_path, data, *, bom=False):
    path = tmp_path / "integration.json"
    text = json.dumps(data, ensure_ascii=False)
    path.write_text(("﻿" if bom else "") + text, encoding="utf-8")
    return path


def test_reads_host_port_and_token(tmp_path):
    path = write(tmp_path, {"wsUrl": "ws://172.29.16.1:3001", "wsToken": "tok", "account": "1"})
    endpoint = read_integration(path)
    assert (endpoint.host, endpoint.port, endpoint.token) == ("172.29.16.1", 3001, "tok")
    assert "tok" not in repr(endpoint)


def test_windows_files_with_bom_are_accepted(tmp_path):
    endpoint = read_integration(write(tmp_path, {"wsUrl": "ws://127.0.0.1:3001"}, bom=True))
    assert endpoint.token == ""


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"wsUrl": "http://127.0.0.1:3001"},
        {"wsUrl": "ws://127.0.0.1"},
        {"wsUrl": "ws://:3001"},
        {"wsUrl": "wss://127.0.0.1:3001"},
        {"wsUrl": "ws://127.0.0.1:0"},
        {"wsUrl": "ws://127.0.0.1:3001/onebot"},
        {"wsUrl": "ws://127.0.0.1:3001?access_token=secret"},
        {"wsUrl": "ws://user:secret@127.0.0.1:3001"},
        {"wsUrl": "ws://[bad:3001"},
        {"wsUrl": "ws://127.0.0.1:3001", "wsToken": 123},
        ["ws://127.0.0.1:3001"],
    ],
)
def test_rejects_unusable_files(tmp_path, data):
    with pytest.raises(NapcatConfigError):
        read_integration(write(tmp_path, data))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(NapcatConfigError):
        read_integration(tmp_path / "missing.json")


def test_bad_port_is_a_config_error():
    with pytest.raises(NapcatConfigError):
        parse_endpoint("ws://127.0.0.1:99999")
