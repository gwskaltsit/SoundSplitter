import json

from soundsplitter.config.settings import Settings, SettingsStore, TargetSettings


def test_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    original = Settings(
        source_id="monitor-1",
        blocksize=512,
        theme="light",
        targets=[
            TargetSettings(index=3, name="Speakers", delay_ms=100, volume_db=-3.0),
            TargetSettings(index=5, name="Headphones"),
        ],
    )
    store.save(original)
    assert store.load() == original


def test_missing_file_returns_defaults(tmp_path):
    store = SettingsStore(tmp_path / "nope.json")
    assert store.load() == Settings()


def test_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    assert SettingsStore(path).load() == Settings()


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "light", "legacy_flag": True}), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.theme == "light"
    assert not hasattr(loaded, "legacy_flag")


def test_save_is_atomic_and_leaves_no_temp(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(theme="light"))
    assert json.loads(store.path.read_text())["theme"] == "light"
    assert list(tmp_path.glob("*.tmp")) == []
