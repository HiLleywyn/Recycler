"""Admin router package  -  aggregates all admin sub-routers."""
from __future__ import annotations

from fastapi import APIRouter

from api.v2.routers.admin.settings import router as settings_router
from api.v2.routers.admin.tokens import router as tokens_router
from api.v2.routers.admin.validators import router as validators_router
from api.v2.routers.admin.networks import router as networks_router
from api.v2.routers.admin.users import router as users_router
from api.v2.routers.admin.channels import router as channels_router
from api.v2.routers.admin.ai import router as ai_router
from api.v2.routers.admin.personas import router as personas_router
from api.v2.routers.admin.treasury import router as treasury_router
from api.v2.routers.admin.operations import router as operations_router
from api.v2.routers.admin.chain import router as chain_router
from api.v2.routers.admin.permissions import router as permissions_router
from api.v2.routers.admin.ops import router as ops_router
from api.v2.routers.admin.nft_gallery import router as nft_gallery_router
from api.v2.routers.admin.premium import router as premium_router

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(settings_router)
router.include_router(tokens_router)
router.include_router(validators_router)
router.include_router(networks_router)
router.include_router(users_router)
router.include_router(channels_router)
router.include_router(ai_router)
router.include_router(personas_router)
router.include_router(treasury_router)
router.include_router(operations_router)
router.include_router(chain_router)
router.include_router(permissions_router)
router.include_router(ops_router)
router.include_router(premium_router)
router.include_router(nft_gallery_router)
