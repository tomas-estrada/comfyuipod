"""Project management endpoints and models."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import ARRAY, Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session
from studio.database import Base, get_db


# Database Models
class Project(Base):
    """SQLAlchemy model for projects."""
    __tablename__ = "projects"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(String)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    collaborators = Column(ARRAY(UUID(as_uuid=True)))
    version = Column(String, default="1.0.0")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Pydantic Models
class ProjectCreate(BaseModel):
    """Request model for creating a project."""
    name: str
    description: str | None = None
    created_by: UUID


class ProjectResponse(BaseModel):
    """Response model for a project."""
    id: UUID
    name: str
    description: str | None
    version: str
    created_by: UUID
    collaborators: list[UUID]


# FastAPI Router
router = APIRouter()


@router.post("/projects", response_model=ProjectResponse)
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    """Create a new project."""
    db_project = Project(**project.dict())
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return db_project


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(db: Session = Depends(get_db)):
    """List all projects."""
    return db.query(Project).all()


@router.get("/projects/{id}", response_model=ProjectResponse)
def get_project(id: UUID, db: Session = Depends(get_db)):
    """Get a project by ID."""
    project = db.query(Project).filter(Project.id == id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.put("/projects/{id}", response_model=ProjectResponse)
def update_project(id: UUID, project: ProjectCreate, db: Session = Depends(get_db)):
    """Update a project by ID."""
    db_project = db.query(Project).filter(Project.id == id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    for key, value in project.dict().items():
        setattr(db_project, key, value)
    db.commit()
    db.refresh(db_project)
    return db_project


@router.delete("/projects/{id}")
def delete_project(id: UUID, db: Session = Depends(get_db)):
    """Delete a project by ID."""
    db_project = db.query(Project).filter(Project.id == id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    db.delete(db_project)
    db.commit()
    return {"status": "success"}