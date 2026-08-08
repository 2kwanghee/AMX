"""RFC 9457 problem details, matching contracts/openapi.yaml `Error`."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError


class ApiError(Exception):
    def __init__(self, status: int, title: str, code: str, detail: str | None = None):
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.code = code
        self.detail = detail


def not_found(resource: str) -> ApiError:
    # Deliberately identical whether the resource is absent or belongs to
    # another tenant: a 403 here would confirm the existence of a foreign id.
    return ApiError(404, "Not Found", f"{resource}.not_found", f"No such {resource} in this tenant.")


def conflict(code: str, detail: str) -> ApiError:
    return ApiError(409, "Conflict", code, detail)


def bad_request(code: str, detail: str) -> ApiError:
    return ApiError(400, "Bad Request", code, detail)


def not_implemented(code: str, detail: str) -> ApiError:
    return ApiError(501, "Not Implemented", code, detail)


def scrub_validation_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """Keep the shape of a validation failure, drop the payload that caused it.

    FastAPI's own 422 body echoes each error's `input` — for this API that is
    the credential set on `POST /accounts` or the authorization code on
    `:oauth-complete`, so the default handler would put plaintext secrets in a
    response body and in any access log that records one (§7). `ctx` goes too:
    it carries constraint context that can quote the offending value.

    An allowlist, not a blocklist: only `loc`, `msg` and `type` survive, so a
    future pydantic version that adds another value-bearing key cannot reopen
    this. `loc` is field position and `msg`/`type` are constraint text, none of
    which depend on what the caller sent.
    """
    scrubbed = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        scrubbed.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    return scrubbed


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle(_: Request, exc: ApiError) -> JSONResponse:
        body = {
            "type": "about:blank",
            "title": exc.title,
            "status": exc.status,
            "code": exc.code,
        }
        if exc.detail:
            body["detail"] = exc.detail
        return JSONResponse(
            status_code=exc.status, content=body, media_type="application/problem+json"
        )

    # Registered explicitly to override FastAPI's own RequestValidationError
    # handler (installed at construction time), which echoes the raw input back.
    # The pydantic.ValidationError handler below is a scrubbing backstop for a
    # bare ValidationError raised anywhere in app code; note it does NOT catch
    # response-model failures — FastAPI raises ResponseValidationError there,
    # which is not a subclass of pydantic.ValidationError.
    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "type": "about:blank",
                "title": "Unprocessable Content",
                "status": 422,
                "code": "request.invalid",
                "detail": "The request body or parameters failed validation.",
                "errors": scrub_validation_errors(exc.errors()),
            },
            media_type="application/problem+json",
        )

    @app.exception_handler(ValidationError)
    async def _handle_validation(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "type": "about:blank",
                "title": "Internal Server Error",
                "status": 500,
                "code": "response.invalid",
                "detail": "The server produced a response that failed validation.",
                "errors": scrub_validation_errors(exc.errors()),
            },
            media_type="application/problem+json",
        )
