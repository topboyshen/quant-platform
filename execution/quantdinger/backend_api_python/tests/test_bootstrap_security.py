"""Deployment bootstrap safeguards for administrator credentials and JWT secret."""

import contextlib
from unittest import mock

import pytest

from app.config.settings import Config
from app.services import user_service as user_service_module
from app.services.user_service import UserService


_PUBLIC_DEFAULT_SECRET = "quantdinger-secret-key-change-me"


def _connection_with_row(row):
    class Cursor:
        def execute(self, sql, params=()):
            return None

        def fetchone(self):
            return row

        def close(self):
            return None

    class DB:
        def cursor(self):
            return Cursor()

    @contextlib.contextmanager
    def connection():
        yield DB()

    return connection()


def _connection_with_user_count(count):
    return _connection_with_row({"count": count})


@pytest.mark.parametrize("secret", ["", _PUBLIC_DEFAULT_SECRET])
def test_non_testing_secret_key_validation_rejects_public_or_empty_secret(monkeypatch, secret):
    monkeypatch.setenv("SECRET_KEY", secret)

    with pytest.raises(RuntimeError, match="SECRET_KEY") as exc_info:
        Config.validate_runtime_security()

    assert "SECRET_KEY" in str(exc_info.value)


def test_testing_secret_key_validation_keeps_public_default_compatible(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", _PUBLIC_DEFAULT_SECRET)

    Config.validate_runtime_security(testing=True)


def test_non_testing_secret_key_validation_accepts_existing_custom_secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "existing-deployment-specific-secret")

    Config.validate_runtime_security()


def test_app_factory_rejects_public_default_secret_before_startup(monkeypatch):
    from app import create_app

    monkeypatch.setenv("SECRET_KEY", _PUBLIC_DEFAULT_SECRET)

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


def test_empty_database_refuses_to_create_admin_with_default_password(monkeypatch):
    service = UserService()
    monkeypatch.setenv("ADMIN_PASSWORD", UserService.BOOTSTRAP_DEFAULT_PASSWORD)
    monkeypatch.setattr(service, "ensure_password_changed_column", lambda: None)
    monkeypatch.setattr(service, "create_user", lambda data: pytest.fail("must not create admin"))
    monkeypatch.setattr(
        user_service_module,
        "get_db_connection",
        lambda: _connection_with_user_count(0),
    )

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD") as exc_info:
        service.ensure_admin_exists()

    assert UserService.BOOTSTRAP_DEFAULT_PASSWORD not in str(exc_info.value)


def test_empty_database_refuses_to_create_admin_with_empty_password(monkeypatch):
    service = UserService()
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setattr(service, "ensure_password_changed_column", lambda: None)
    monkeypatch.setattr(service, "create_user", lambda data: pytest.fail("must not create admin"))
    monkeypatch.setattr(
        user_service_module,
        "get_db_connection",
        lambda: _connection_with_user_count(0),
    )

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        service.ensure_admin_exists()


def test_empty_database_still_bootstraps_with_explicit_admin_password(monkeypatch):
    service = UserService()
    created = []
    monkeypatch.setenv("ADMIN_USER", "deployment-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "explicitly-configured-password")
    monkeypatch.setattr(service, "ensure_password_changed_column", lambda: None)
    monkeypatch.setattr(service, "create_user", lambda data: created.append(data))
    monkeypatch.setattr(
        user_service_module,
        "get_db_connection",
        lambda: _connection_with_user_count(0),
    )

    service.ensure_admin_exists()

    assert len(created) == 1
    assert created[0]["role"] == "admin"
    assert created[0]["status"] == "active"


def test_existing_database_does_not_require_bootstrap_password_change(monkeypatch):
    service = UserService()
    monkeypatch.setenv("ADMIN_PASSWORD", UserService.BOOTSTRAP_DEFAULT_PASSWORD)
    monkeypatch.setattr(service, "ensure_password_changed_column", lambda: None)
    monkeypatch.setattr(service, "create_user", lambda data: pytest.fail("must not create admin"))
    monkeypatch.setattr(service, "sync_bootstrap_admin_password_from_env", lambda: None)
    monkeypatch.setattr(
        user_service_module,
        "get_db_connection",
        lambda: _connection_with_user_count(1),
    )

    service.ensure_admin_exists()


def test_login_does_not_issue_token_while_bootstrap_default_is_active():
    from flask import Flask
    from app.routes import auth as auth_route
    from app.services import security_service as security_service_module

    app = Flask(__name__)
    security = mock.Mock()
    security.verify_turnstile.return_value = (True, "")
    security.check_login_allowed.return_value = (True, "")
    user = {
        "id": 1,
        "username": "deployment-admin",
        "role": "admin",
        "status": "active",
    }

    with (
        app.test_request_context(
            "/api/auth/login",
            method="POST",
            json={"username": "deployment-admin", "password": "attempted-password"},
        ),
        mock.patch.object(security_service_module, "get_security_service", return_value=security),
        mock.patch.object(auth_route, "_authenticate_password_user", return_value=user),
        mock.patch.object(auth_route, "_personal_owner_allowed", return_value=True),
        mock.patch.object(auth_route, "_userinfo_must_change_initial_password", return_value=True),
        mock.patch.object(auth_route, "_rotate_login_token_version") as rotate_token,
        mock.patch.object(auth_route, "generate_token") as generate_token,
    ):
        result = auth_route.login()

    response = result[0] if isinstance(result, tuple) else result
    status = result[1] if isinstance(result, tuple) else response.status_code
    assert status == 403
    assert "ADMIN_PASSWORD" in response.get_json()["msg"]
    rotate_token.assert_not_called()
    generate_token.assert_not_called()


def test_app_factory_does_not_swallow_bootstrap_security_error(monkeypatch):
    from app import create_app
    from app.services.user_service import BootstrapSecurityError
    from app.utils import db as db_module

    service = mock.Mock()
    service.ensure_admin_exists.side_effect = BootstrapSecurityError(
        "Set a non-default ADMIN_PASSWORD"
    )
    monkeypatch.setenv("SECRET_KEY", "existing-deployment-specific-secret")
    monkeypatch.setattr(db_module, "init_database", lambda: None)
    monkeypatch.setattr(db_module, "get_db_type", lambda: "postgresql")
    monkeypatch.setattr(user_service_module, "get_user_service", lambda: service)

    with pytest.raises(BootstrapSecurityError, match="ADMIN_PASSWORD"):
        create_app()


def test_failed_env_password_repair_keeps_default_admin_login_blocked(monkeypatch):
    service = UserService()
    password_hash = service.hash_password(UserService.BOOTSTRAP_DEFAULT_PASSWORD)
    monkeypatch.setenv("ADMIN_PASSWORD", "explicitly-configured-password")
    monkeypatch.setattr(service, "get_first_user_id", lambda: 1)
    monkeypatch.setattr(service, "ensure_password_changed_column", lambda: None)
    monkeypatch.setattr(service, "_sync_bootstrap_admin_password_from_env", lambda user_id, password_hash: False)
    monkeypatch.setattr(
        user_service_module,
        "get_db_connection",
        lambda: _connection_with_row({
            "password_hash": password_hash,
            "password_changed_at": "2026-06-06 00:00:00",
        }),
    )

    assert service.must_change_initial_password(1) is True
