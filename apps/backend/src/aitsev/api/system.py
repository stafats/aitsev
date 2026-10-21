"""Health and model discovery endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from aitsev import __version__, catalog
from aitsev.api.deps import GatewayDep, OwnerDep, PrincipalDep, SessionDep, SettingsDep
from aitsev.schemas import CatalogEntryOut, CatalogOut, ModelRoleOut, ModelsOut

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(session: SessionDep) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            {"status": "degraded", "database": "unreachable", "version": __version__},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return JSONResponse({"status": "ok", "database": "ok", "version": __version__})


@router.get("/v1/models", response_model=ModelsOut)
async def models(settings: SettingsDep, gateway: GatewayDep, _owner: OwnerDep) -> ModelsOut:
    roles: list[tuple[str, str]] = [("planner", settings.planner_model)]
    roles += [("generator", m) for m in settings.generator_models]
    roles += [("reviewer", m) for m in settings.reviewer_models]
    roles.append(("judge", settings.judge_model))
    roles += [("fallback", m) for m in settings.fallback_models]
    try:
        providers = await gateway.list_models()
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Provider error: {exc}") from exc
    return ModelsOut(
        roles=[ModelRoleOut(role=r, model=m, available=gateway.is_available(m)) for r, m in roles],
        providers=providers,
    )


@router.get("/v1/models/catalog", response_model=CatalogOut)
async def model_catalog(
    settings: SettingsDep, gateway: GatewayDep, principal: PrincipalDep
) -> CatalogOut:
    """Models the signed-in user can choose to write their code, and which their plan includes."""
    plan_id = principal.account.plan if principal.account else None
    return CatalogOut(
        plan=plan_id,
        premium_allowed=catalog.tier_allowed(plan_id, "premium"),
        max_selected=catalog.MAX_SELECTED,
        upgrade_url=f"{settings.public_url.rstrip('/')}/app/#/account",
        models=[
            CatalogEntryOut(
                **m.model_dump(),
                available=gateway.is_available(m.id),
                allowed=catalog.tier_allowed(plan_id, m.tier),
            )
            for m in catalog.load(settings)
        ],
    )
