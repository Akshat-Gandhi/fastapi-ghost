"""A small FastAPI app used to exercise fastapi-ghost end to end.

Run:  TODO_TOKEN=dev-token uvicorn examples.todo_app.main:app --port 8000

It deliberately includes the situations Ghost should surface: bearer auth, request
validation (422), 404, 409 conflict, a declared 400, an undeclared 500 bug, and a slow endpoint.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

app = FastAPI(title="Todo demo", version="1.0.0")
todos: dict[int, dict] = {1: {"id": 1, "title": "write tests", "priority": 2, "done": False}}
_next_id = 2


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    priority: int = Field(default=1, ge=1, le=5)
    done: bool = False


class Todo(TodoIn):
    id: int


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    if authorization != f"Bearer {os.environ.get('TODO_TOKEN', 'dev-token')}":
        raise HTTPException(status_code=401, detail="Invalid or missing token")


router = APIRouter(prefix="/todos", dependencies=[Depends(require_token)])


@router.get("", response_model=list[Todo])
def list_todos():
    return list(todos.values())


@router.post("", response_model=Todo, status_code=201, responses={409: {"description": "Duplicate title"}})
def create_todo(todo: TodoIn):
    global _next_id
    if any(t["title"] == todo.title for t in todos.values()):
        raise HTTPException(status_code=409, detail="A todo with this title already exists")
    item = {"id": _next_id, **todo.model_dump()}
    todos[_next_id] = item
    _next_id += 1
    return item


@router.get("/{todo_id}", response_model=Todo, responses={404: {"description": "Not found"}})
def get_todo(todo_id: int):
    if todo_id not in todos:
        raise HTTPException(status_code=404, detail="Todo not found")
    return todos[todo_id]


@router.put("/{todo_id}", response_model=Todo, responses={404: {"description": "Not found"}})
def update_todo(todo_id: int, todo: TodoIn):
    if todo_id not in todos:
        raise HTTPException(status_code=404, detail="Todo not found")
    todos[todo_id] = {"id": todo_id, **todo.model_dump()}
    return todos[todo_id]


@router.delete("/{todo_id}", status_code=204, responses={404: {"description": "Not found"}})
def delete_todo(todo_id: int):
    if todo_id not in todos:
        raise HTTPException(status_code=404, detail="Todo not found")
    del todos[todo_id]


calc = APIRouter(prefix="/calc")


@calc.get("/divide", responses={400: {"description": "Division by zero"}})
def divide(a: float = Query(...), b: float = Query(...)):
    if b == 0:
        raise HTTPException(status_code=400, detail="Cannot divide by zero")
    return {"result": a / b}


@calc.get("/average")
def average(values: str = Query(..., description="comma separated numbers")):
    nums = [float(v) for v in values.split(",") if v]
    return {"result": sum(nums) / len(nums)}  # bug: ZeroDivisionError / ValueError -> unhandled 500


app.include_router(router)
app.include_router(calc)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/slow")
def slow():
    time.sleep(0.7)
    return {"ok": True}
