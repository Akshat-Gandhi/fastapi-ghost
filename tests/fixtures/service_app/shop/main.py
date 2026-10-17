from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

import shop.legacy as legacy
from shop.api import admin, items
from shop.deps import check_maintenance
from shop.errors import NotFoundError, QuotaError

app = FastAPI(dependencies=[Depends(check_maintenance)])


@app.exception_handler(QuotaError)
async def quota_handler(request, exc):
    return JSONResponse(status_code=429, content={"detail": "quota exceeded"})


async def not_found_handler(request, exc):
    return JSONResponse({"detail": "gone"}, status_code=status.HTTP_404_NOT_FOUND)


app.add_exception_handler(NotFoundError, not_found_handler)


async def ping():
    raise HTTPException(status_code=504, detail="upstream timeout")


app.include_router(items.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.add_api_route("/ping", ping, methods=["GET"])
app.add_api_route("/legacy/{rid}", legacy.get_report, methods=["GET"])
