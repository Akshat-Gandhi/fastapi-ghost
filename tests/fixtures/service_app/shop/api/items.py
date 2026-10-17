from fastapi import APIRouter

import shop.crud as crud
from shop import services
from shop.deps import CurrentUser
from shop.errors import QuotaError
from shop.lookup import find

router = APIRouter(prefix="/items")

HANDLERS = {}


@router.get("/")
def list_items():
    return crud.list_all()


@router.post("/")
def create_item(name: str):
    return services.service.create(name)


@router.get("/quota")
def quota():
    raise QuotaError()


@router.get("/teapot")
def teapot():
    return services.brew()


@router.get("/loop")
def loop():
    return services.ping()


@router.get("/pick")
def pick():
    return find("x")


@router.get("/dyn/{name}")
def dynamic(name: str):
    handler = getattr(crud, name)
    handler()
    HANDLERS[name]()


@router.get("/{item_id}")
async def read_item(item_id: int, user: CurrentUser):
    return await services.get_item(item_id)
