from payments_api.routers.accounts import router as accounts_router
from payments_api.routers.transfers import router as transfers_router

__all__ = ["accounts_router", "transfers_router"]
