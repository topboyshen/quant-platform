"""Release identity: deploy-time RELEASE_* files, package version fallback."""
from __future__ import annotations


def test_release_info_reads_release_files(tmp_path, monkeypatch):
    (tmp_path / "backend").mkdir()
    (tmp_path / "RELEASE_NAME").write_text("0.3.19-test\n", encoding="utf-8")
    (tmp_path / "RELEASE_SHA").write_text("abc1234\n", encoding="utf-8")
    (tmp_path / "RELEASE_BASE").write_text("test base\n", encoding="utf-8")
    monkeypatch.setenv("QP_APP_DIR", str(tmp_path / "backend"))
    from qp.release import release_info
    assert release_info() == {"version": "0.3.19-test", "sha": "abc1234", "base": "test base"}


def test_release_info_falls_back_to_package_version(tmp_path, monkeypatch):
    monkeypatch.setenv("QP_APP_DIR", str(tmp_path / "missing" / "backend"))
    from qp import __version__
    from qp.release import release_info
    info = release_info()
    assert info["version"] == __version__
    assert info["sha"] == "" and info["base"] == ""
