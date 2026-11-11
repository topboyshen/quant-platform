import contextlib
import json
import os
import unittest
from unittest import mock

import jwt


class _Cursor:
    def __init__(self, row=None, statements=None):
        self.row = row
        self.statements = statements if statements is not None else []
        self.lastrowid = None
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), tuple(params)))

    def fetchone(self):
        return self.row

    def close(self):
        return None


class _DB:
    def __init__(self, row=None, statements=None):
        self.row = row
        self.statements = statements if statements is not None else []

    def cursor(self):
        return _Cursor(self.row, self.statements)

    def commit(self):
        return None


def _connection(row=None, statements=None):
    @contextlib.contextmanager
    def _manager():
        yield _DB(row=row, statements=statements)

    return _manager()


def _sequence_connection(rows, statements=None):
    statements = statements if statements is not None else []

    class _SequenceCursor(_Cursor):
        def __init__(self):
            super().__init__(statements=statements)
            self.rows = list(rows)

        def fetchone(self):
            return self.rows.pop(0) if self.rows else None

    class _SequenceDB(_DB):
        def cursor(self):
            return _SequenceCursor()

    @contextlib.contextmanager
    def _manager():
        yield _SequenceDB(statements=statements)

    return _manager()


class SecurityHardeningTests(unittest.TestCase):
    def setUp(self):
        self._test_env = mock.patch.dict(os.environ, {
            'PERSONAL_MODE': 'true',
            'QUANTDINGER_DEPLOYMENT_MODE': 'personal',
            'SECRET_KEY': 'codex-isolated-test-secret-key-00000000000000000000000000000000',
            'SKIP_STARTUP_HOOKS': '1',
        })
        self._test_env.start()

    def tearDown(self):
        self._test_env.stop()

    def test_legacy_notification_read_guard_and_writer_markers(self):
        from app.routes.strategy import _safe_notification_message_for_response
        from app.services.portfolio_monitor import _trusted_html_payload_json

        attacks = [
            '<style>body{display:none}</style><img src=x onerror="alert(1)">',
            '<svg><animate onbegin="alert(1)"></animate></svg>',
            '<math><mtext><a href="javascript:alert(1)">x</a></mtext></math>',
        ]
        for payload_json in ('{}', '{broken-json'):
            for attack in attacks:
                escaped = _safe_notification_message_for_response({
                    'message': attack,
                    'payload_json': payload_json,
                })
                self.assertNotIn('<style>', escaped)
                self.assertNotIn('<img', escaped)
                self.assertNotIn('<svg', escaped)
                self.assertNotIn('<math', escaped)
                self.assertIn('&lt;', escaped)

        trusted_message = '&lt;img src=x&gt;<br>safe'
        trusted = _safe_notification_message_for_response({
            'message': trusted_message,
            'payload_json': _trusted_html_payload_json({'kind': 'test'}),
        })
        self.assertEqual(trusted, trusted_message)

    def test_login_alert_email_escapes_dynamic_html(self):
        from app.services import email_service as email_module
        from app.services import login_notify
        from app.services import signal_notifier as notifier_module
        from app.services import user_service as user_service_module

        attack = '<img src=x onerror="alert(1)">'
        user_service = mock.Mock()
        user_service.get_user_by_id.return_value = {
            'nickname': attack,
            'username': 'owner',
            'email': 'owner@example.invalid',
        }
        email_service = mock.Mock()
        email_service.is_configured.return_value = True

        with (
            mock.patch.object(user_service_module, 'get_user_service', return_value=user_service),
            mock.patch.object(email_module, 'get_email_service', return_value=email_service),
            mock.patch.object(notifier_module.SignalNotifier, '_notify_browser', return_value=(True, '')),
        ):
            login_notify._send_login_alerts(
                user_id=1,
                action='login_success',
                ip_address=attack,
                device_label=attack,
                geo={'location': attack},
                details={},
                is_new_device=True,
                is_new_region=True,
            )

        body = email_service.send_email.call_args.args[2]
        self.assertNotIn('<img src=x', body)
        self.assertIn('&lt;img src=x onerror=', body)

    def test_personal_mode_disables_oauth_before_service_calls(self):
        from flask import Flask
        from app.routes import auth as auth_route

        app = Flask(__name__)
        with app.test_request_context('/api/auth/oauth/google'):
            result = auth_route.oauth_google()
            response = result[0] if isinstance(result, tuple) else result
            status = result[1] if isinstance(result, tuple) else response.status_code
            self.assertEqual(status, 403)
            self.assertEqual(response.get_json()['msg'], 'OAuth is disabled in personal mode')

        with app.test_request_context('/api/auth/oauth/google/callback?code=x&state=y'):
            result = auth_route.oauth_google_callback()
            response = result[0] if isinstance(result, tuple) else result
            status = result[1] if isinstance(result, tuple) else response.status_code
            self.assertEqual(status, 403)
            self.assertEqual(response.get_json()['msg'], 'OAuth is disabled in personal mode')

    def test_agent_tokens_follow_owner_status_and_current_role(self):
        from app.utils import agent_auth

        owner = {
            'user_id': 1,
            'owner_id': 1,
            'user_status': 'active',
            'user_role': 'admin',
        }
        self.assertIsNone(agent_auth._agent_identity_rejection(owner, agent_auth.SCOPE_C))

        non_owner = {**owner, 'user_id': 2}
        self.assertEqual(
            agent_auth._agent_identity_rejection(non_owner, agent_auth.SCOPE_R),
            'personal_mode_owner_required',
        )

        inactive = {**owner, 'user_status': 'disabled'}
        self.assertEqual(
            agent_auth._agent_identity_rejection(inactive, agent_auth.SCOPE_R),
            'owner_account_inactive',
        )

        downgraded = {**owner, 'user_role': 'viewer'}
        self.assertEqual(
            agent_auth._agent_identity_rejection(downgraded, agent_auth.SCOPE_C),
            'admin_role_required',
        )

    def test_personal_oauth_rejects_account_creation_before_mutation(self):
        from app.services import oauth_service as oauth_module
        from app.services import user_service as user_service_module

        statements = []
        owner_service = mock.Mock()
        owner_service.get_first_user_id.return_value = 1
        oauth = oauth_module.OAuthService.__new__(oauth_module.OAuthService)
        oauth_info = {
            'provider': 'github',
            'provider_user_id': 'attacker-provider-id',
            'email': 'attacker@example.invalid',
            'name': 'attacker',
            'access_token': 'isolated-test-token',
        }

        with (
            mock.patch.object(user_service_module, 'get_user_service', return_value=owner_service),
            mock.patch.object(
                oauth_module,
                'get_db_connection',
                side_effect=lambda: _sequence_connection([None, None], statements),
            ),
        ):
            success, result = oauth.get_or_create_user_from_oauth(oauth_info)

        self.assertFalse(success)
        self.assertIn('owner', result['error'].lower())
        mutation_sql = [
            sql for sql, _ in statements
            if sql.upper().startswith(('INSERT ', 'UPDATE ', 'DELETE '))
        ]
        self.assertEqual(mutation_sql, [])

    def test_plain_browser_notification_sinks_encode_html(self):
        from app.services import signal_notifier as notifier_module
        from app.services import trading_executor as executor_module

        attack = '<img src=x onerror="alert(1)">\nsecond line'

        notifier_statements = []
        with mock.patch.object(
            notifier_module,
            'get_db_connection',
            side_effect=lambda: _connection(statements=notifier_statements),
        ):
            ok, _ = notifier_module.SignalNotifier()._notify_browser(
                strategy_id=None,
                symbol='TEST',
                signal_type='test',
                channels=['browser'],
                title='test',
                message=attack,
                payload={},
                user_id=1,
            )
        self.assertTrue(ok)
        stored_message = notifier_statements[-1][1][6]
        stored_payload = json.loads(notifier_statements[-1][1][7])
        self.assertNotIn('<img', stored_message)
        self.assertIn('&lt;img', stored_message)
        self.assertIn('<br>second line', stored_message)
        self.assertEqual(stored_payload['_rendering'], 'trusted_html_v1')

        executor_statements = []
        executor = executor_module.TradingExecutor.__new__(executor_module.TradingExecutor)
        with mock.patch.object(
            executor_module,
            'get_db_connection',
            side_effect=lambda: _connection(statements=executor_statements),
        ):
            executor._persist_browser_notification(
                strategy_id=1,
                symbol='TEST',
                signal_type='test',
                title='test',
                message=attack,
                payload={},
                user_id=1,
            )
        stored_message = executor_statements[-1][1][6]
        stored_payload = json.loads(executor_statements[-1][1][7])
        self.assertNotIn('<img', stored_message)
        self.assertIn('&lt;img', stored_message)
        self.assertIn('<br>second line', stored_message)
        self.assertEqual(stored_payload['_rendering'], 'trusted_html_v1')

    def test_html_and_telegram_reports_escape_untrusted_text(self):
        from app.services.portfolio_monitor import (
            _build_batch_telegram_report,
            _build_html_report,
            _build_telegram_report,
            _get_alert_message,
            _monitor_failure_content,
        )

        attack = '<img src=x onerror="globalThis.pwned=1"><script>alert(1)</script>'
        analysis = {
            'symbol': attack,
            'name': attack,
            'market': attack,
            'group_name': attack,
            'final_decision': attack,
            'confidence': attack,
            'current_price': attack,
            'entry_price': 1.0,
            'pnl': 0.0,
            'pnl_percent': 0.0,
            'quantity': attack,
            'side': 'long',
            'reasoning': attack + '\nsecond line',
            'trader_reasoning': attack,
            'overview_report': attack,
            'risk_report': attack,
        }

        html = _build_html_report([], [analysis], 'en-US', attack)
        self.assertNotIn('<img src=x', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;img src=x onerror=', html)
        self.assertIn('class="icon hold">H</div>', html)
        self.assertIn('<br>second line', html)

        telegram = _build_telegram_report([], [analysis], 'en-US', attack)
        self.assertNotIn('<img src=x', telegram)
        self.assertNotIn('<script>alert(1)</script>', telegram)
        self.assertIn('&lt;img src=x onerror=', telegram)

        batch = _build_batch_telegram_report([{
            'success': True,
            '_meta': {
                'monitor_name': attack,
                'position_analyses': [analysis],
            },
        }], 'en-US')
        self.assertNotIn('<img src=x', batch)
        self.assertIn('&lt;img src=x onerror=', batch)

        _, _, failure_html = _monitor_failure_content(attack, attack, 'en-US')
        self.assertNotIn('<img src=x', failure_html)
        self.assertIn('&lt;img src=x onerror=', failure_html)

        alert_html = _get_alert_message(
            'price_above', 'en-US', symbol=attack, current_price=1, threshold=1,
        )
        self.assertNotIn('<img src=x', alert_html)
        self.assertIn('&lt;img src=x onerror=', alert_html)

    def test_verify_token_uses_current_database_role(self):
        from app.utils import auth
        from app.utils import db as db_module

        row = {
            'username': 'current-name',
            'role': 'viewer',
            'status': 'active',
            'token_version': 7,
            'owner_id': 11,
        }
        with mock.patch.object(db_module, 'get_db_connection', return_value=_connection(row)):
            token = auth.generate_token(11, 'old-name', role='admin', token_version=7)
            payload = auth.verify_token(token)

        self.assertEqual(payload['sub'], 'current-name')
        self.assertEqual(payload['role'], 'viewer')
        self.assertEqual(payload['status'], 'active')

    def test_verify_token_rejects_inactive_or_missing_claims(self):
        from app.utils import auth
        from app.utils import db as db_module

        row = {
            'username': 'disabled-user',
            'role': 'admin',
            'status': 'disabled',
            'token_version': 2,
            'owner_id': 12,
        }
        with mock.patch.object(db_module, 'get_db_connection', return_value=_connection(row)):
            token = auth.generate_token(12, 'disabled-user', role='admin', token_version=2)
            self.assertIsNone(auth.verify_token(token))

        missing_version = jwt.encode(
            {
                'exp': 4102444800,
                'iat': 1,
                'sub': 'legacy-token',
                'user_id': 12,
            },
            os.environ['SECRET_KEY'],
            algorithm='HS256',
        )
        self.assertIsNone(auth.verify_token(missing_version))

    def test_verify_token_rejects_non_owner_and_database_failure(self):
        from app.utils import auth
        from app.utils import db as db_module

        token = auth.generate_token(22, 'secondary', role='admin', token_version=3)
        row = {
            'username': 'secondary',
            'role': 'admin',
            'status': 'active',
            'token_version': 3,
            'owner_id': 1,
        }
        with mock.patch.object(db_module, 'get_db_connection', return_value=_connection(row)):
            self.assertIsNone(auth.verify_token(token))

        with mock.patch.object(
            db_module,
            'get_db_connection',
            side_effect=RuntimeError('database unavailable'),
        ):
            self.assertIsNone(auth.verify_token(token))

    def test_personal_mode_uses_existing_environment_configuration(self):
        from app.utils import auth

        clean_env = dict(os.environ)
        clean_env.pop('PERSONAL_MODE', None)
        clean_env['QUANTDINGER_DEPLOYMENT_MODE'] = 'personal'
        with mock.patch.dict(os.environ, clean_env, clear=True):
            self.assertTrue(auth.personal_mode_enabled())
            os.environ['PERSONAL_MODE'] = 'false'
            self.assertFalse(auth.personal_mode_enabled())
            os.environ['PERSONAL_MODE'] = 'true'
            self.assertTrue(auth.personal_mode_enabled())

    def test_database_mode_never_calls_legacy_auth(self):
        from app.routes import auth as auth_route

        service = mock.Mock()
        service.authenticate.return_value = None
        with (
            mock.patch.object(auth_route, '_is_single_user_mode', return_value=False),
            mock.patch.object(auth_route, '_get_database_user_service', return_value=service),
            mock.patch.object(auth_route, 'authenticate_legacy') as legacy,
        ):
            self.assertIsNone(auth_route._authenticate_password_user('user', 'bad-password'))
        legacy.assert_not_called()

        broken = mock.Mock()
        broken.authenticate.side_effect = RuntimeError('database unavailable')
        with (
            mock.patch.object(auth_route, '_is_single_user_mode', return_value=False),
            mock.patch.object(auth_route, '_get_database_user_service', return_value=broken),
            mock.patch.object(auth_route, 'authenticate_legacy') as legacy,
        ):
            with self.assertRaises(RuntimeError):
                auth_route._authenticate_password_user('user', 'password')
        legacy.assert_not_called()

    def test_role_status_and_password_updates_revoke_tokens(self):
        from app.services import user_service as user_service_module

        statements = []
        service = user_service_module.UserService()
        with mock.patch.object(
            user_service_module,
            'get_db_connection',
            side_effect=lambda: _connection(statements=statements),
        ):
            self.assertTrue(service.update_user(5, {'role': 'viewer'}))
            role_sql = next(
                sql for sql, _ in statements if sql.startswith('UPDATE qd_users')
            )
            self.assertIn(
                'token_version = COALESCE(token_version, 1) + 1',
                role_sql,
            )
            self.assertTrue(any(
                "UPDATE qd_agent_tokens SET status = 'revoked'" in sql
                for sql, _ in statements
            ))

            statements.clear()
            self.assertTrue(service.update_user(5, {'status': 'disabled'}))
            status_sql = next(
                sql for sql, _ in statements if sql.startswith('UPDATE qd_users')
            )
            self.assertIn(
                'token_version = COALESCE(token_version, 1) + 1',
                status_sql,
            )
            self.assertTrue(any(
                "UPDATE qd_agent_tokens SET status = 'revoked'" in sql
                for sql, _ in statements
            ))

            statements.clear()
            self.assertTrue(service.update_user(5, {'nickname': 'safe-display-change'}))
            self.assertNotIn('token_version', statements[-1][0])

            statements.clear()
            with (
                mock.patch.object(service, 'ensure_password_changed_column'),
                mock.patch.object(service, 'hash_password', return_value='test-hash'),
            ):
                self.assertTrue(service.reset_password(5, 'new-password'))
            self.assertIn(
                'token_version = COALESCE(token_version, 1) + 1',
                statements[-1][0],
            )

            statements.clear()
            with self.assertRaises(RuntimeError):
                service.increment_token_version(999)
            self.assertIn(
                'COALESCE(token_version, 1) + 1',
                statements[-1][0],
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
