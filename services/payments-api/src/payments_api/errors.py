from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@dataclass(slots=True)
class Problem(Exception):
    status: int
    title: str
    detail: str
    type: str = "about:blank"

    def body(self, instance: str | None = None) -> dict[str, object]:
        body: dict[str, object] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if instance is not None:
            body["instance"] = instance
        return body


def problem_response(problem: Problem, instance: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.body(instance),
        media_type="application/problem+json",
    )


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    def handle_problem(request: Request, exc: Problem) -> JSONResponse:
        return problem_response(exc, request.url.path)

    @app.exception_handler(RequestValidationError)
    def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problem = Problem(
            status=400,
            title="Invalid request",
            detail="The request did not pass validation.",
        )
        body = problem.body(request.url.path)
        body["errors"] = exc.errors()
        return JSONResponse(
            status_code=400,
            content=jsonable_encoder(body),
            media_type="application/problem+json",
        )
