"""User authentication and authorization endpoints."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session
from studio.comfy_studio.models.rbac import (
    Role,
    Token,
    create_access_token,
    get_password_hash,
    verify_password,
)
from studio.database import Base, get_db


# Database Models
class User(Base):
    """SQLAlchemy model for users."""
    __tablename__ = "users"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default=Role.VIEWER)
    created_at = Column(DateTime, default=datetime.utcnow)


# Pydantic Models
class UserCreate(BaseModel):
    """Request model for creating a user."""
    username: str
    email: str
    password: str


class UserLogin(BaseModel):
    """Request model for user login."""
    username: str
    password: str


# FastAPI Router
router = APIRouter()


@router.post("/auth/register", response_model=Token)
def register(user: UserCreate, db: Session = Depends(get_db)):
    """Register a new user."""
    db_user = db.query(User).filter(User.username == user.username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    hashed_password = get_password_hash(user.password)
    db_user = User(username=user.username, email=user.email, hashed_password=hashed_password)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    access_token = create_access_token(
        data={"sub": user.username, "roles": [db_user.role]}
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/auth/login", response_model=Token)
def login(user: UserLogin, db: Session = Depends(get_db)):
    """Login a user and return a JWT token."""
    db_user = db.query(User).filter(User.username == user.username).first()
    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(
        data={"sub": user.username, "roles": [db_user.role]}
    )
    return {"access_token": access_token, "token_type": "bearer"}