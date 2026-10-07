from __future__ import annotations

from fastapi import APIRouter, Request

from ..models.project import CreateProjectRequest, ProjectResponse

router = APIRouter(tags=["projects"])


@router.post("/projects", response_model=ProjectResponse, status_code=201)
def create_project(request: Request, body: CreateProjectRequest | None = None):
    store = request.app.state.store
    project = store.create(body.title if body else None)
    return ProjectResponse.from_project(project)


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(request: Request):
    # The (large) analysis is omitted from the list view; use GET /projects/{id}.
    return [
        ProjectResponse.from_project(p, include_analysis=False)
        for p in request.app.state.store.list_projects()
    ]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, request: Request):
    return ProjectResponse.from_project(request.app.state.store.get(project_id))
