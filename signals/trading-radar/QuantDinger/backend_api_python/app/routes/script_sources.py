"""Authenticated storage for Strategy IDE script sources."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.utils.auth import login_required
from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)
script_sources_blp = Blueprint("script_sources", __name__)

_JSON_FIELDS = frozenset({"param_schema", "template_params", "metadata"})
_SOURCE_COLUMNS = (
    "id, user_id, name, description, code, asset_type, template_key, "
    "param_schema, template_params, metadata, status, created_at, updated_at"
)
_SUMMARY_COLUMNS = (
    "id, user_id, name, description, asset_type, template_key, "
    "param_schema, template_params, metadata, status, created_at, updated_at"
)


class SourcePayloadError(ValueError):
    """Raised when a script-source request payload is invalid."""


def _close_cursor(cursor) -> None:
    close = getattr(cursor, "close", None)
    if callable(close):
        close()


def _json_object(value: Any, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourcePayloadError(f"{field} must be an object")
    return value


def _text(value: Any, field: str, *, max_length: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SourcePayloadError(f"{field} must be a string")
    result = value.strip() if field in {"name", "asset_type", "template_key"} else value
    if required and not result.strip():
        raise SourcePayloadError(f"{field} is required")
    if len(result) > max_length:
        raise SourcePayloadError(f"{field} is too long")
    return result


def _validate_payload(payload: Any, *, creating: bool) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise SourcePayloadError("JSON object required")

    values: Dict[str, Any] = {}
    if creating or "name" in payload:
        values["name"] = _text(payload.get("name"), "name", max_length=200, required=True)
    if creating or "description" in payload:
        values["description"] = _text(
            payload.get("description"),
            "description",
            max_length=50_000,
        )
    if creating or "code" in payload:
        values["code"] = _text(
            payload.get("code"),
            "code",
            max_length=1_000_000,
            required=True,
        )
    if creating or "asset_type" in payload:
        asset_type = _text(
            payload.get("asset_type") or "script",
            "asset_type",
            max_length=32,
            required=True,
        )
        if asset_type not in {"script", "portfolio_strategy"}:
            raise SourcePayloadError("asset_type must be script or portfolio_strategy")
        values["asset_type"] = asset_type
    if creating or "template_key" in payload:
        values["template_key"] = _text(
            payload.get("template_key"),
            "template_key",
            max_length=160,
        )
    for field in _JSON_FIELDS:
        if creating or field in payload:
            values[field] = _json_object(payload.get(field), field)
    return values


def _positive_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SourcePayloadError(f"{field} must be a positive integer") from exc
    if result <= 0:
        raise SourcePayloadError(f"{field} must be a positive integer")
    return result


def _decode_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _source_to_dict(row: Any) -> Dict[str, Any] | None:
    if not row:
        return None
    source = dict(row)
    for field in _JSON_FIELDS:
        source[field] = _decode_json(source.get(field))
    for field in ("created_at", "updated_at"):
        value = source.get(field)
        if isinstance(value, (date, datetime)):
            source[field] = value.isoformat()
    code_hidden = bool(source["metadata"].get("code_hidden"))
    source["code_hidden"] = code_hidden
    if code_hidden and "code" in source:
        source["code"] = ""
    return source


def _list_sources(user_id: int, *, asset_type: str = "", limit: int = 200):
    query = f"SELECT {_SUMMARY_COLUMNS} FROM qd_strategy_sources WHERE user_id=%s"
    params: list[Any] = [int(user_id)]
    if asset_type:
        if asset_type not in {"script", "portfolio_strategy"}:
            raise SourcePayloadError("asset_type must be script or portfolio_strategy")
        query += " AND asset_type=%s"
        params.append(asset_type)
    query += " ORDER BY updated_at DESC, id DESC LIMIT %s"
    params.append(max(1, min(int(limit), 500)))

    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(query, tuple(params))
            return [_source_to_dict(row) for row in (cursor.fetchall() or [])]
        finally:
            _close_cursor(cursor)


def _get_source(source_id: int, user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM qd_strategy_sources WHERE id=%s AND user_id=%s",
                (int(source_id), int(user_id)),
            )
            return _source_to_dict(cursor.fetchone())
        finally:
            _close_cursor(cursor)


def _create_source(user_id: int, payload: Any):
    values = _validate_payload(payload, creating=True)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                INSERT INTO qd_strategy_sources (
                    user_id, name, description, code, asset_type, template_key,
                    param_schema, template_params, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                RETURNING {_SOURCE_COLUMNS}
                """,
                (
                    int(user_id),
                    values["name"],
                    values["description"],
                    values["code"],
                    values["asset_type"],
                    values["template_key"],
                    json.dumps(values["param_schema"], ensure_ascii=False),
                    json.dumps(values["template_params"], ensure_ascii=False),
                    json.dumps(values["metadata"], ensure_ascii=False),
                ),
            )
            row = cursor.fetchone()
            conn.commit()
            return _source_to_dict(row)
        finally:
            _close_cursor(cursor)


def _update_source(source_id: int, user_id: int, payload: Any):
    values = _validate_payload(payload, creating=False)
    if not values:
        return _get_source(source_id, user_id)

    assignments = []
    params: list[Any] = []
    for field, value in values.items():
        if field in _JSON_FIELDS:
            assignments.append(f"{field}=%s::jsonb")
            params.append(json.dumps(value, ensure_ascii=False))
        else:
            assignments.append(f"{field}=%s")
            params.append(value)
    assignments.append("updated_at=NOW()")
    params.extend((int(source_id), int(user_id)))

    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                UPDATE qd_strategy_sources
                SET {', '.join(assignments)}
                WHERE id=%s AND user_id=%s
                RETURNING {_SOURCE_COLUMNS}
                """,
                tuple(params),
            )
            row = cursor.fetchone()
            conn.commit()
            return _source_to_dict(row)
        finally:
            _close_cursor(cursor)


def _delete_source(source_id: int, user_id: int) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM qd_strategy_sources WHERE id=%s AND user_id=%s RETURNING id",
                (int(source_id), int(user_id)),
            )
            deleted = cursor.fetchone() is not None
            conn.commit()
            return deleted
        finally:
            _close_cursor(cursor)


@script_sources_blp.route("/script-sources", methods=["GET"])
@login_required
def list_script_sources():
    try:
        limit = _positive_int(request.args.get("limit", 200), "limit")
        sources = _list_sources(
            g.user_id,
            asset_type=str(request.args.get("asset_type") or "").strip(),
            limit=limit,
        )
        return jsonify({"code": 1, "msg": "success", "data": sources})
    except SourcePayloadError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": []}), 400
    except Exception as exc:
        logger.error("list_script_sources failed: %s", exc, exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to load script sources", "data": []}), 500


@script_sources_blp.route("/script-sources/detail", methods=["GET"])
@login_required
def get_script_source():
    try:
        source_id = _positive_int(request.args.get("id"), "id")
        source = _get_source(source_id, g.user_id)
        if not source:
            return jsonify({"code": 0, "msg": "Script source not found", "data": None}), 404
        return jsonify({"code": 1, "msg": "success", "data": source})
    except SourcePayloadError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    except Exception as exc:
        logger.error("get_script_source failed: %s", exc, exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to load script source", "data": None}), 500


@script_sources_blp.route("/script-sources/create", methods=["POST"])
@login_required
def create_script_source():
    try:
        source = _create_source(g.user_id, request.get_json(silent=True))
        return jsonify({"code": 1, "msg": "success", "data": source})
    except SourcePayloadError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    except Exception as exc:
        logger.error("create_script_source failed: %s", exc, exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to create script source", "data": None}), 500


@script_sources_blp.route("/script-sources/update", methods=["PUT"])
@login_required
def update_script_source():
    try:
        source_id = _positive_int(request.args.get("id"), "id")
        source = _update_source(source_id, g.user_id, request.get_json(silent=True))
        if not source:
            return jsonify({"code": 0, "msg": "Script source not found", "data": None}), 404
        return jsonify({"code": 1, "msg": "success", "data": source})
    except SourcePayloadError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    except Exception as exc:
        logger.error("update_script_source failed: %s", exc, exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to update script source", "data": None}), 500


@script_sources_blp.route("/script-sources/delete", methods=["DELETE"])
@login_required
def delete_script_source():
    try:
        source_id = _positive_int(request.args.get("id"), "id")
        if not _delete_source(source_id, g.user_id):
            return jsonify({"code": 0, "msg": "Script source not found", "data": None}), 404
        return jsonify({"code": 1, "msg": "success", "data": None})
    except SourcePayloadError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    except Exception as exc:
        logger.error("delete_script_source failed: %s", exc, exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to delete script source", "data": None}), 500
