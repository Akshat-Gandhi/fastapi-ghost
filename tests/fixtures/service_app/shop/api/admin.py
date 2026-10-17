from fastapi import APIRouter, Depends

from shop.api import reports
from shop.deps import require_admin

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
router.include_router(reports.router)


@router.get("/stats")
def stats():
    return {}
