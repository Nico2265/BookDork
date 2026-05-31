"""
=============================================================================
meilisearch_client.py — Integración con Meilisearch
=============================================================================
Meilisearch actúa como motor de búsqueda local con:
  • Corrección tipográfica automática (typo tolerance)
  • Búsqueda difusa (fuzzy matching) para errores ortográficos
  • Indexación de metadatos de libros
  • Sugerencias de términos correctos (spelling suggestions)
  • Filtrado por atributos (filetype, idioma, año…)

La búsqueda fluye así:
  1. El usuario escribe una consulta (con posibles errores)
  2. Meilisearch corrige y busca en el índice local → resultados rápidos
  3. El dork_engine genera la URL de Google con la consulta corregida
  4. El frontend muestra ambos resultados al usuario
=============================================================================
"""

import logging
import uuid
from typing import Optional

import meilisearch
from meilisearch.errors import MeilisearchApiError, MeilisearchCommunicationError

from .config import get_settings
from .models import BookResult, SearchRequest

logger = logging.getLogger("bookdork.meilisearch")


# ─────────────────────────────────────────────────────────────────────────────
# Configuración del índice de libros
# ─────────────────────────────────────────────────────────────────────────────

# Atributos en los que Meilisearch buscará texto
SEARCHABLE_ATTRIBUTES = [
    "title",       # Título del libro — mayor peso
    "author",      # Autor(es)
    "description", # Sinopsis/descripción
    "isbn",        # ISBN (exacto o con typos)
    "tags",        # Etiquetas temáticas
    "publisher",   # Editorial
]

# Atributos disponibles para filtrar con la sintaxis: "filetype = pdf"
FILTERABLE_ATTRIBUTES = [
    "filetype",
    "language",
    "year",
    "source_site",
    "tags",
]

# Atributos disponibles para ordenar resultados
SORTABLE_ATTRIBUTES = [
    "year",
    "title",
]

# Configuración de tolerancia tipográfica
# minWordSizeForTypos:
#   oneTypo  → palabras ≥ 5 letras admiten 1 error tipográfico
#   twoTypos → palabras ≥ 9 letras admiten 2 errores tipográficos
TYPO_TOLERANCE_CONFIG = {
    "enabled": True,
    "minWordSizeForTypos": {
        "oneTypo": 4,   # "harey" → "harry" (a partir de 4 letras)
        "twoTypos": 8,  # "mistkes" → "mistakes"
    },
}

# Atributos que se devuelven en la respuesta (ocultar campos internos)
DISPLAYED_ATTRIBUTES = [
    "id", "title", "author", "description",
    "filetype", "language", "year", "source_site",
    "isbn", "tags", "dork_url",
]


# ─────────────────────────────────────────────────────────────────────────────
# Cliente Meilisearch
# ─────────────────────────────────────────────────────────────────────────────

class MeilisearchBookClient:
    """
    Envuelve la librería oficial de Meilisearch con lógica específica para libros.

    Uso:
        client = MeilisearchBookClient()
        await client.initialize()          # Crea el índice si no existe
        results = client.search("quijote") # Búsqueda con typo tolerance
    """

    def __init__(self):
        settings = get_settings()

        # Cliente de solo lectura para búsquedas públicas.
        # Si MEILI_SEARCH_API_KEY no está configurada se usa la master key con aviso:
        # una clave de búsqueda restringida limita el daño si el proceso es comprometido.
        search_key = settings.MEILI_SEARCH_API_KEY or settings.MEILI_MASTER_KEY
        if not settings.MEILI_SEARCH_API_KEY:
            logger.warning(
                "MEILI_SEARCH_API_KEY no configurada — búsquedas usan master key. "
                "Crea una clave de solo lectura en Meilisearch: "
                "POST /keys con actions=['search']."
            )
        self._search_client = meilisearch.Client(
            url=settings.MEILI_HOST,
            api_key=search_key,
        )

        # Cliente con master key exclusivo para operaciones de administración
        # (create/update index, add documents, get stats).
        self._admin_client = meilisearch.Client(
            url=settings.MEILI_HOST,
            api_key=settings.MEILI_MASTER_KEY,
        )

        self._index_name = settings.MEILI_INDEX_NAME
        self._search_index = None   # índice accedido con clave de búsqueda
        self._admin_index  = None   # índice accedido con clave de admin

    # ── Inicialización ────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """
        Crea o actualiza el índice de libros con la configuración correcta.
        Usa la master key (admin) para configurar y la search key para leer.
        Retorna True si el índice está listo.

        IMPORTANTE: el handle de búsqueda se obtiene con `client.index(name)`
        (constructor local, sin llamada HTTP). NO usar `get_index()` con la
        search key — ese endpoint requiere permiso `indexes.get` que la clave
        de solo búsqueda no tiene, y produciría 403 invalid_api_key.
        """
        try:
            try:
                self._admin_index  = self._admin_client.get_index(self._index_name)
            except MeilisearchApiError as e:
                err_str = str(e)
                if "invalid_api_key" in err_str or "missing_authorization_header" in err_str:
                    logger.error(
                        "Master key rechazada por Meilisearch (HTTP 403/401). "
                        "Verifica que MEILI_MASTER_KEY en .env coincide con la "
                        "variable de entorno del servidor Meilisearch "
                        "(docker inspect <container> | grep MEILI_MASTER_KEY)."
                    )
                    return False
                if "index_not_found" in err_str:
                    # Índice no existe → crear con master key
                    task = self._admin_client.create_index(
                        self._index_name, {"primaryKey": "id"}
                    )
                    self._admin_client.wait_for_task(task.task_uid, timeout_in_ms=10000)
                    self._admin_index = self._admin_client.get_index(self._index_name)
                    logger.info("Índice '%s' creado.", self._index_name)
                else:
                    raise
            else:
                logger.info("Índice '%s' encontrado.", self._index_name)

            # Handle local para el cliente de búsqueda — no requiere llamada HTTP
            # y por tanto no depende de los permisos de la search key.
            self._search_index = self._search_client.index(self._index_name)

            self._configure_index()
            self._verify_search_key()
            return True

        except MeilisearchApiError as e:
            logger.error("Error de API Meilisearch al inicializar: %s", e)
            return False
        except MeilisearchCommunicationError as e:
            logger.error("No se puede conectar a Meilisearch: %s", e)
            return False

    def _verify_search_key(self) -> None:
        """
        Probe explícito: ejecuta una búsqueda vacía con la search key para
        detectar early una clave inválida o sin permiso `search` sobre el
        índice. Si falla, se registra un error accionable sin abortar el
        arranque (la indexación con master key sigue funcionando).
        """
        if self._search_index is None:
            return
        try:
            self._search_index.search("", {"limit": 0})
        except MeilisearchApiError as e:
            err_str = str(e)
            if "invalid_api_key" in err_str or "missing_authorization_header" in err_str:
                if get_settings().MEILI_SEARCH_API_KEY:
                    logger.error(
                        "MEILI_SEARCH_API_KEY rechazada por Meilisearch. "
                        "La clave debe existir en /keys con actions=['search'] "
                        "e indexes incluyendo '%s'. "
                        "Crear con: curl -H 'Authorization: Bearer <MASTER_KEY>' "
                        "-X POST http://localhost:7700/keys "
                        "-H 'Content-Type: application/json' "
                        "-d '{\"actions\":[\"search\"],\"indexes\":[\"%s\"],\"expiresAt\":null}'",
                        self._index_name, self._index_name,
                    )
                else:
                    # No definida → se está usando la master key como fallback,
                    # que sí tiene permisos: si esto falla, la master key es el problema.
                    logger.error(
                        "Probe de búsqueda falló con la master key como fallback — "
                        "verifica MEILI_MASTER_KEY."
                    )

    def _configure_index(self) -> None:
        """Aplica la configuración de atributos vía cliente admin."""
        if self._admin_index is None:
            return
        try:
            self._admin_index.update_searchable_attributes(SEARCHABLE_ATTRIBUTES)
            self._admin_index.update_filterable_attributes(FILTERABLE_ATTRIBUTES)
            self._admin_index.update_sortable_attributes(SORTABLE_ATTRIBUTES)
            self._admin_index.update_displayed_attributes(DISPLAYED_ATTRIBUTES)
            self._admin_index.update_typo_tolerance(TYPO_TOLERANCE_CONFIG)
            self._admin_index.update_ranking_rules([
                "words", "typo", "proximity", "attribute", "sort", "exactness",
            ])
            logger.info("Configuración del índice aplicada correctamente.")
        except MeilisearchApiError as e:
            logger.error("Error configurando índice: %s", e)

    # ── Búsqueda ──────────────────────────────────────────────────────────────

    def search(self, request: SearchRequest) -> dict:
        """
        Ejecuta una búsqueda usando la clave de solo lectura (search key).
        Si la key es comprometida, el atacante solo puede hacer búsquedas,
        no modificar el índice ni acceder a configuración.
        """
        if self._search_index is None:
            logger.warning("Índice no inicializado. Retornando vacío.")
            return {"hits": [], "estimatedTotalHits": 0, "processingTimeMs": 0}

        # Construir filtros dinámicamente (sintaxis Meilisearch)
        filters: list[str] = []
        if request.filetype and request.filetype != "any":
            filters.append(f'filetype = "{request.filetype}"')
        if request.language and request.language != "any":
            filters.append(f'language = "{request.language}"')
        if request.site and request.site != "any":
            filters.append(f'source_site = "{request.site}"')

        # Parámetros de la consulta
        search_params: dict = {
            "limit": request.limit,
            "offset": (request.page - 1) * request.limit,
            # Resaltar los términos encontrados (para el frontend)
            "attributesToHighlight": ["title", "author", "description"],
            "highlightPreTag": "<mark>",
            "highlightPostTag": "</mark>",
            # Mostrar fragmento relevante de la descripción
            "attributesToCrop": ["description"],
            "cropLength": 200,
            # Búsqueda difusa: "all" intenta hacer match con todos los tokens
            "matchingStrategy": "all",
        }

        # Añadir filtros si existen
        if filters:
            search_params["filter"] = " AND ".join(filters)

        try:
            result = self._search_index.search(request.query, search_params)
            return result
        except MeilisearchApiError as e:
            logger.error("Error en búsqueda Meilisearch: %s", e)
            return {"hits": [], "estimatedTotalHits": 0, "processingTimeMs": 0}

    def get_suggestions(self, query: str, limit: int = 5) -> list[str]:
        """Sugerencias ortográficas usando la clave de solo lectura."""
        if self._search_index is None:
            return []
        try:
            result = self._search_index.search(query, {
                "limit": limit,
                "matchingStrategy": "last",  # Más permisivo
                "attributesToRetrieve": ["title"],
            })
            # Devuelve los títulos encontrados como sugerencias
            return [hit.get("title", "") for hit in result.get("hits", []) if hit.get("title")]
        except MeilisearchApiError:
            return []

    # ── Indexación ────────────────────────────────────────────────────────────

    def index_book(self, book_data: dict) -> bool:
        """Indexa un libro usando la master key (operación de escritura)."""
        if self._admin_index is None:
            return False
        if "id" not in book_data or not book_data["id"]:
            book_data["id"] = str(uuid.uuid4())
        try:
            task = self._admin_index.add_documents([book_data])
            self._admin_client.wait_for_task(task.task_uid, timeout_in_ms=5000)
            logger.info("Libro indexado: '%s'", book_data.get("title", "?"))
            return True
        except MeilisearchApiError as e:
            logger.error("Error indexando libro: %s", e)
            return False

    def index_books_batch(self, books: list[dict]) -> bool:
        """Indexa múltiples libros en lote usando la master key."""
        if self._admin_index is None or not books:
            return False
        for book in books:
            if "id" not in book or not book["id"]:
                book["id"] = str(uuid.uuid4())
        try:
            task = self._admin_index.add_documents(books)
            self._admin_client.wait_for_task(task.task_uid, timeout_in_ms=30000)
            logger.info("Lote de %d libros indexado.", len(books))
            return True
        except MeilisearchApiError as e:
            logger.error("Error en indexación por lote: %s", e)
            return False

    # ── Utilidades ────────────────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        """Comprueba disponibilidad usando el cliente de búsqueda (menos privilegios)."""
        try:
            self._search_client.health()
            return True
        except MeilisearchCommunicationError:
            return False

    def get_stats(self) -> dict:
        """Estadísticas del índice usando la master key (solo para admins)."""
        if self._admin_index is None:
            return {}
        try:
            return self._admin_index.get_stats().__dict__
        except MeilisearchApiError:
            return {}


# ── Singleton ──────────────────────────────────────────────────────────────
_meili_client: Optional[MeilisearchBookClient] = None


def get_meili_client() -> MeilisearchBookClient:
    """Retorna la instancia singleton del cliente Meilisearch."""
    global _meili_client
    if _meili_client is None:
        _meili_client = MeilisearchBookClient()
    return _meili_client
