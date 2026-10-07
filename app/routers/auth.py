from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.auth import CurrentUser, LoginGuardDep, authenticate, client_address
from app.config import get_settings
from app.db import DbSession
from app.models import User
from app.schemas import PasswordChange, Token, UserOut
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_for(user: User) -> Token:
    return Token(
        access_token=create_access_token(user),
        expires_in=get_settings().access_token_minutes * 60,
    )


@router.post("/token", response_model=Token)
def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    db: DbSession,
    guard: LoginGuardDep,
) -> Token:
    """Exchange a username and password for a bearer token (OAuth2 password flow)."""
    client = client_address(request)
    retry_after = guard.retry_after(form.username, client)
    if retry_after:
        # Answered before the password is checked, so a blocked attacker learns nothing
        # from more guesses, not even when one of them is right.
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed logins, try again later",
            headers={"Retry-After": str(retry_after)},
        )

    user = authenticate(db, form.username, form.password)
    if user is None:
        guard.record_failure(form.username, client)
        # One message for every failure, so it never confirms that a username exists.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Only the account's count is cleared. Clearing the client's too would let an
    # attacker reset their own count by logging into an account they control.
    guard.clear_account(form.username)
    return _token_for(user)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user


@router.post("/password", response_model=Token)
def change_password(
    payload: PasswordChange,
    user: CurrentUser,
    request: Request,
    db: DbSession,
    guard: LoginGuardDep,
) -> Token:
    """Change your own password. Every other session ends; this one gets a new token."""
    # The same limits as logging in. Otherwise a stolen token could guess the current
    # password here as fast as it liked, and then lock the real owner out.
    client = client_address(request)
    retry_after = guard.retry_after(user.username, client)
    if retry_after:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed attempts, try again later",
            headers={"Retry-After": str(retry_after)},
        )
    if not verify_password(payload.current_password, user.password_hash):
        guard.record_failure(user.username, client)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the current password is incorrect")

    user.password_hash = hash_password(payload.new_password)
    user.token_version += 1  # retires every token issued before now, this one included
    db.commit()
    db.refresh(user)
    return _token_for(user)
