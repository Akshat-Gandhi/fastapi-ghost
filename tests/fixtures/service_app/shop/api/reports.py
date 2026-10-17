from fastapi import APIRouter

router = APIRouter(prefix="/reports")


@router.get("/summary")
def summary():
    return {}
