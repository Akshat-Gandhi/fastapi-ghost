from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/v1")


def _check(x):
    if x < 0:
        raise HTTPException(status_code=400, detail="negative")


@router.get("/items/{item_id}")
async def get_item(item_id: int):
    if item_id == 0:
        raise HTTPException(404, "Item not found")
    _check(item_id)
    return {}


@router.post("/items")
def create(name: str):
    if name == "dup":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{name} exists")

    def inner():
        raise HTTPException(status_code=403, detail="nope")

    inner()
