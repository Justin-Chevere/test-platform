from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import DbSession
from app.models import Role, User
from app.schemas import UserCreate, UserOut, UserUpdate
from app.security import hash_password

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


def _get_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return user


def _active_admin_count(db: Session) -> int:
    query = select(func.count()).where(User.role == Role.ADMIN, User.is_active.is_(True))
    return db.scalar(query) or 0


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: DbSession) -> User:
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "username already in use") from exc
    db.refresh(user)
    return user


@router.get("", response_model=list[UserOut])
def list_users(db: DbSession) -> list[User]:
    return list(db.scalars(select(User).order_by(User.id)))


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, db: DbSession) -> User:
    """Change a user's role, or deactivate them: their tokens stop working at once."""
    user = _get_or_404(db, user_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)

    stays_admin = changes.get("role", user.role) == Role.ADMIN and changes.get(
        "is_active", user.is_active
    )
    if user.role == Role.ADMIN and user.is_active and not stays_admin:
        # Without an active admin, nobody could manage users through the API again.
        if _active_admin_count(db) == 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "cannot remove the last active admin")

    for field, value in changes.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user
