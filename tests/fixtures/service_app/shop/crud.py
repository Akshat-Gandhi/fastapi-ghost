from fastapi import HTTPException

from shop.errors import NotFoundError


async def get_by_id(item_id: int):
    if item_id < 0:
        raise HTTPException(status_code=400, detail="negative id")
    if item_id == 0:
        raise NotFoundError(item_id)
    return {"id": item_id}


def list_all():
    raise HTTPException(status_code=410, detail="listing retired")


def secret():
    raise HTTPException(status_code=500, detail="only reachable dynamically")


def never_called():
    raise HTTPException(status_code=507, detail="dead code")
