"""
=============================================================================
models.py — Modelos de datos (Pydantic v2)
=============================================================================
Define los esquemas de entrada y salida de la API.
Pydantic valida y sanitiza automáticamente los datos en tiempo de ejecución,
eliminando campos inesperados y convirtiendo tipos incorrectos.
=============================================================================
"""

from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator
import re


# ── Tipos permitidos ─────────────────────────────────────────────────────────

FileType = Literal["pdf", "epub", "mobi", "djvu", "azw3", "txt", "any"]

BookSite = Literal[
    "archive.org",
    "gutenberg.org",
    "openlibrary.org",
    "pdfdrive.com",
    "books.google.com",
    "worldcat.org",
    "any",
]

Language = Literal[
    "es", "en", "fr", "de", "pt", "it", "ru", "zh", "ja", "ar", "any"
]


# ── Solicitud de búsqueda ────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    """
    Parámetros de búsqueda recibidos desde el frontend.
    Todos los campos se validan estrictamente para prevenir inyecciones.
    """

    query: str = Field(
        ...,
        min_length=2,
        max_length=512,
        description="Término de búsqueda (título, autor, ISBN, tema)",
        examples=["Don Quijote de la Mancha", "Gabriel García Márquez"],
    )
    filetype: FileType = Field(default="any", description="Formato del archivo")
    site: BookSite = Field(default="any", description="Sitio de origen")
    language: Language = Field(default="any", description="Idioma del libro")
    author: Optional[str] = Field(
        default=None, max_length=150, description="Nombre del autor"
    )
    isbn: Optional[str] = Field(
        default=None, max_length=20, description="Número ISBN"
    )
    page: int = Field(default=1, ge=1, le=100, description="Página de resultados")
    limit: int = Field(default=20, ge=1, le=50, description="Resultados por página")

    @field_validator("query", "author", mode="before")
    @classmethod
    def sanitize_text(cls, v: Optional[str]) -> Optional[str]:
        """
        Elimina caracteres potencialmente peligrosos del texto libre.
        Se permiten letras, números, espacios, guiones, puntos,
        comas, comillas dobles y paréntesis (usados en dorks).
        """
        if v is None:
            return v
        # Elimina caracteres que no son seguros
        v = re.sub(r"[^\w\s\-.,:'\"()\[\]&@#éáíóúüñÉÁÍÓÚÜÑ]", "", v, flags=re.UNICODE)
        # Colapsa espacios múltiples
        return re.sub(r"\s+", " ", v).strip()

    @field_validator("isbn", mode="before")
    @classmethod
    def validate_isbn(cls, v: Optional[str]) -> Optional[str]:
        """
        Valida que el ISBN solo contenga dígitos, guiones y la letra X.
        """
        if v is None:
            return v
        cleaned = re.sub(r"[^0-9Xx\-]", "", v)
        if len(cleaned) not in (10, 13, 17):  # 17 = con guiones
            return None
        return cleaned


# ── Resultado individual de búsqueda ─────────────────────────────────────────

class BookResult(BaseModel):
    """
    Representación de un libro encontrado.
    """

    id: str
    title: str
    author: Optional[str] = None
    description: Optional[str] = None
    filetype: Optional[str] = None
    language: Optional[str] = None
    year: Optional[int] = None
    source_site: Optional[str] = None
    isbn: Optional[str] = None
    dork_url: str = Field(description="URL de búsqueda Google Dork generada")
    score: Optional[float] = Field(default=None, description="Relevancia 0–1")
    tags: list[str] = Field(default_factory=list)


# ── Respuesta de búsqueda ────────────────────────────────────────────────────

class SearchResponse(BaseModel):
    """
    Respuesta completa de la API de búsqueda.
    """

    query: str
    dork_query: str = Field(description="La consulta Google Dork generada")
    dork_url: str = Field(description="URL Google Dork lista para abrir")
    total_hits: int
    page: int
    limit: int
    processing_time_ms: int
    results: list[BookResult]
    suggestions: list[str] = Field(
        default_factory=list,
        description="Correcciones ortográficas sugeridas por Meilisearch",
    )


# ── Petición de indexación (admin) ───────────────────────────────────────────

class IndexBookRequest(BaseModel):
    """
    Cuerpo de la petición para indexar un nuevo libro (endpoint admin).
    """

    title: str = Field(..., min_length=1, max_length=500)
    author: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)
    filetype: Optional[str] = Field(default=None, max_length=10)
    language: Optional[str] = Field(default=None, max_length=5)
    year: Optional[int] = Field(default=None, ge=1000, le=2100)
    source_site: Optional[str] = Field(default=None, max_length=100)
    isbn: Optional[str] = Field(default=None, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("title", "author", "description", mode="before")
    @classmethod
    def sanitize(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return re.sub(r"[<>{}|\\^`]", "", v).strip()


# ── Health check ─────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "error"]
    version: str
    meilisearch_connected: bool
    uptime_seconds: float
