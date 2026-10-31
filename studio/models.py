"""Model management endpoints and models."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import ARRAY, Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session
from studio.database import Base, get_db


# Database Models
class Model(Base):
    """SQLAlchemy model for models."""
    __tablename__ = "models"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(String)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    version = Column(String, default="1.0.0")
    file_path = Column(String)
    tags = Column(ARRAY(String))
    shared_with = Column(ARRAY(UUID(as_uuid=True)))
    created_at = Column(DateTime, default=datetime.utcnow)


# Pydantic Models
class ModelCreate(BaseModel):
    """Request model for creating a model."""
    name: str
    description: str | None = None
    created_by: UUID


class ModelResponse(BaseModel):
    """Response model for a model."""
    id: UUID
    name: str
    description: str | None
    version: str
    file_path: str
    tags: list[str]
    shared_with: list[UUID]


# FastAPI Router
router = APIRouter()


@router.post("/models", response_model=ModelResponse)
async def upload_model(
    name: str,
    description: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a new model."""
    file_path = f"models/{uuid.uuid4()}_{file.filename}"
    with open(file_path, "wb") as f:
        f.write(await file.read())

    # Replace with actual user ID
    db_model = Model(
        name=name, 
        description=description, 
        file_path=file_path, 
        created_by=uuid.UUID(int=0)
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return db_model


@router.get("/models", response_model=list[ModelResponse])
def list_models(db: Session = Depends(get_db)):
    """List all models."""
    return db.query(Model).all()


@router.get("/models/{id}", response_model=ModelResponse)
def get_model(id: UUID, db: Session = Depends(get_db)):
    """Get a model by ID."""
    model = db.query(Model).filter(Model.id == id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model