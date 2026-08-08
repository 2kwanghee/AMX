"""RFC 9457 problem details, matching contracts/openapi.yaml `Error`."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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
