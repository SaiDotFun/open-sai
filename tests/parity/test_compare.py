from pathlib import Path

from compare import Entry, Normalize, first_difference, snapshot
from parity import Observation, differences


def test_log_ids_preserve_joins_and_sequence(tmp_path: Path) -> None:
    def records(run: str, calls: list[str], times: list[str]) -> list[dict]:
        return [
            {"runId": run, "callId": call, "seq": seq, "ts": stamp, "event": event}
            for seq, (call, stamp, event) in enumerate(
                zip(
                    calls,
                    times,
                    ("call.start", "keeper.request", "call.end"),
                    strict=True,
                ),
                start=1,
            )
        ]

    times = ["2026-11-24T01:00:00Z", "2026-11-24T01:00:01Z", "2026-11-24T01:00:02Z"]
    reference = Normalize(tmp_path).log_records(
        records("a" * 32, ["b" * 12] * 3, times)
    )
    candidate = records(
        "c" * 32, ["d" * 12] * 3, [stamp.replace("01:", "02:", 1) for stamp in times]
    )
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        is None
    )

    candidate[1]["callId"] = "e" * 12
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        == "logs[1].callId"
    )
    candidate[1]["callId"] = "d" * 12
    candidate.reverse()
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        == "logs[0].event"
    )


def test_json_compares_keys_and_types() -> None:
    assert first_difference({"x": 1, "y": [2]}, {"y": [2.0], "x": 1}, "stdout") is None
    assert first_difference({"x": 1}, {"x": True}, "stdout") == "stdout.x type"
    assert first_difference({"x": 1}, {}, "stdout") == "stdout keys"


def test_snapshot_reports_mode_and_semantic_config_changes(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("enabled = true\n", encoding="utf-8")
    config.chmod(0o600)
    normalize = Normalize(tmp_path.parent)
    reference = Observation(0, {}, (), snapshot(tmp_path, normalize))

    config.write_text("# fixture\nenabled=true\n", encoding="utf-8")
    assert (
        differences(reference, Observation(0, {}, (), snapshot(tmp_path, normalize)))
        == []
    )

    config.chmod(0o644)
    config.write_text("enabled = false\n", encoding="utf-8")
    changed = Observation(0, {}, (), snapshot(tmp_path, normalize))
    assert differences(reference, changed) == [
        "config.toml mode 0600 to 0644",
        "config.toml.enabled",
    ]
    assert changed.files["config.toml"] == Entry("file", 0o644, {"enabled": False})
