from fastapi import HTTPException

from shop.crud import get_by_id as fetch
from shop.errors import TeapotError


async def get_item(item_id: int):
    return await fetch(item_id)


class ItemService:
    def create(self, name: str):
        self._ensure_unique(name)
        return {"name": name}

    def _ensure_unique(self, name: str):
        if name == "dup":
            raise HTTPException(status_code=409, detail="duplicate")


service = ItemService()


def ping():
    return pong()


def pong():
    if False:
        ping()
    raise HTTPException(status_code=508, detail="loop detected")


def brew():
    raise TeapotError()
