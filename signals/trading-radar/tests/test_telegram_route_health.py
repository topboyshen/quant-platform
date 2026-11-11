from scripts import check_telegram_route


def test_read_secret_only_accepts_top_level_key(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "proxies:\n  - secret: nested\nsecret: 'controller-token'\n",
        encoding="utf-8",
    )

    assert check_telegram_route._read_secret(config) == "controller-token"


def test_choose_fastest_ignores_failed_probes():
    assert check_telegram_route._choose_fastest([None, 450, 220, None]) == 2


def test_choose_fastest_returns_none_when_all_failed():
    assert check_telegram_route._choose_fastest([None, None]) is None


def test_check_route_switches_to_verified_fastest_candidate(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("secret: controller-token\n", encoding="utf-8")
    monkeypatch.setenv("MIHOMO_CONFIG", str(config))
    monkeypatch.setattr(
        check_telegram_route,
        "_get_proxies",
        lambda _controller, _secret: {
            "Telegram": {"all": ["old", "new"], "now": "old"},
        },
    )
    monkeypatch.setattr(
        check_telegram_route,
        "_probe",
        lambda _controller, _secret, member: None if member == "old" else 120,
    )
    selections = []
    monkeypatch.setattr(
        check_telegram_route,
        "_select",
        lambda _controller, _secret, group, member: selections.append((group, member)),
    )

    assert check_telegram_route.check_route() == 0
    assert selections == [("Telegram", "new")]


def test_check_route_rolls_back_when_selected_candidate_fails_verification(
    monkeypatch,
    tmp_path,
):
    config = tmp_path / "config.yaml"
    config.write_text("secret: controller-token\n", encoding="utf-8")
    monkeypatch.setenv("MIHOMO_CONFIG", str(config))
    monkeypatch.setattr(
        check_telegram_route,
        "_get_proxies",
        lambda _controller, _secret: {
            "Telegram": {"all": ["old", "new"], "now": "old"},
        },
    )
    new_probes = iter([100, None])

    def probe(_controller, _secret, member):
        return None if member == "old" else next(new_probes)

    monkeypatch.setattr(check_telegram_route, "_probe", probe)
    selections = []
    monkeypatch.setattr(
        check_telegram_route,
        "_select",
        lambda _controller, _secret, group, member: selections.append((group, member)),
    )

    assert check_telegram_route.check_route() == 1
    assert selections == [("Telegram", "new"), ("Telegram", "old")]
