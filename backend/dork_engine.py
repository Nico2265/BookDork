"""
=============================================================================
dork_engine.py — Motor de construcción de Google Dorks para libros
=============================================================================
Construye consultas Google Dork especializadas para localizar libros
digitales (PDF, EPUB, MOBI, DJVU) usando operadores avanzados de Google.

Operadores usados (Google Dork List Master):
  • filetype:/ext:  — Filtra por tipo de archivo
  • site:           — Limita a un sitio específico
  • intitle:        — Término en el título de la página
  • intext:         — Término en el cuerpo del texto
  • inurl:          — Término en la URL
  • -inurl:         — Excluye URLs con el término
  • -site:          — Excluye un sitio
  • "..."           — Búsqueda de frase exacta
  • OR              — Operador lógico OR
  • intitle:"index of"   — Directorios con listado habilitado
  • intitle:"parent directory" — Directorio padre accesible
=============================================================================
"""

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from .config import get_settings


# ─────────────────────────────────────────────────────────────────────────────
# Catálogos
# ─────────────────────────────────────────────────────────────────────────────

OPEN_BOOK_SITES: dict[str, str] = {
    "archive.org":         "Internet Archive (millones de libros libres)",
    "gutenberg.org":       "Project Gutenberg (dominio público)",
    "openlibrary.org":     "Open Library (catálogo universal)",
    "pdfdrive.com":        "PDF Drive (buscador de PDFs)",
    "oapen.org":           "OAPEN (libros académicos acceso abierto)",
    "doabooks.org":        "DOAB (directorio de libros abiertos)",
    "standardebooks.org":  "Standard Ebooks (clásicos con tipografía cuidada)",
    "openstax.org":        "OpenStax (libros de texto gratuitos)",
    "books.google.com":    "Google Books (previews y libros gratuitos)",
    "worldcat.org":        "WorldCat (catálogo de bibliotecas mundiales)",
    "manybooks.net":       "ManyBooks (ebooks gratuitos)",
    "academia.edu":        "Academia.edu (papers y libros académicos)",
    "researchgate.net":    "ResearchGate (publicaciones científicas)",
}

BOOK_FILETYPES: dict[str, str] = {
    "pdf":  "PDF (Portable Document Format)",
    "epub": "EPUB (Electronic Publication)",
    "mobi": "MOBI (Kindle)",
    "djvu": "DjVu (documentos escaneados)",
    "azw3": "AZW3 (Kindle avanzado)",
    "txt":  "TXT (texto plano)",
}

# Sitios de compra/paywall a excluir de resultados
_EXCLUDED_SITES = [
    "amazon.com",
    "goodreads.com",
    "barnesandnoble.com",
    "ebay.com",
    "scribd.com",
    "chegg.com",
]

# Patrones de URL que indican página de pago/login
_EXCLUDED_URL_PATTERNS = [
    "login",
    "register",
    "signup",
    "checkout",
    "cart",
]

# Patrones de directorio abierto (Google Dork clásico)
_OPEN_DIRECTORY_SIGNALS = [
    'intitle:"index of"',
    'intitle:"parent directory"',
]


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass de resultado Dork
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DorkQuery:
    """Representa una consulta Google Dork lista para usar."""
    raw_query: str
    google_url: str
    strategy: str
    description: str
    operators_used: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Constructor principal de Dorks
# ─────────────────────────────────────────────────────────────────────────────

class BookDorkEngine:
    """
    Genera consultas Google Dork especializadas para encontrar libros.

    Uso:
        engine = BookDorkEngine()
        dork = engine.build_primary(query="Don Quijote", author="Cervantes")
        print(dork.google_url)
    """

    def __init__(self):
        self.settings = get_settings()

    # ── Sanitización ─────────────────────────────────────────────────────────

    def _sanitize(self, text: str) -> str:
        cleaned = re.sub(r"[^\w\s\-.'éáíóúüñÉÁÍÓÚÜÑ]", " ", text, flags=re.UNICODE)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _quote_phrase(self, text: str) -> str:
        safe = self._sanitize(text).replace('"', "")
        return f'"{safe}"'

    def _build_google_url(self, query: str) -> str:
        params: dict[str, str] = {"q": query}
        if self.settings.DORK_SAFE_SEARCH:
            params["safe"] = "active"
        base = self.settings.GOOGLE_BASE_URL
        return f"{base}?{urllib.parse.urlencode(params)}"

    def _shopping_exclusions(self, count: int = 3) -> list[str]:
        return [f"-site:{s}" for s in _EXCLUDED_SITES[:count]]

    # ── Estrategias ───────────────────────────────────────────────────────────

    def strategy_direct_pdf(
        self,
        query: str,
        author: Optional[str] = None,
        filetype: Optional[str] = None,
    ) -> DorkQuery:
        """
        Estrategia principal: título exacto entre comillas + apellido sin comillas + filetype:.
        - Sin intitle: (no funciona con PDFs directos).
        - Autor sin comillas y solo apellido: evita fallar cuando el PDF
          usa "V. Frittelli", "Frittelli, V." u otras variantes.
        - Sin exclusiones de sitio: -site: reduce demasiado los resultados.
        """
        parts: list[str] = []
        operators: list[str] = []

        parts.append(self._quote_phrase(query))
        operators.append("phrase")

        if author:
            # Solo apellido (última palabra) sin comillas para mayor cobertura
            last_name = self._sanitize(author).split()[-1] if author.strip() else ""
            if len(last_name) > 2:
                parts.append(last_name)
                operators.append("author-lastname")

        target_ft = filetype if filetype and filetype not in ("any", "") else "pdf"
        parts.append(f"filetype:{target_ft}")
        operators.append(f"filetype:{target_ft}")

        raw = " ".join(parts)
        fmt = target_ft.upper()
        return DorkQuery(
            raw_query=raw,
            google_url=self._build_google_url(raw),
            strategy="direct_pdf",
            description=f"Búsqueda directa del archivo {fmt} del libro",
            operators_used=operators,
        )

    def strategy_filetype(
        self,
        query: str,
        filetype: Optional[str] = None,
        site: Optional[str] = None,
    ) -> DorkQuery:
        """
        Estrategia por tipo de archivo con intitle: y PDF primero.
        Técnica: intitle:"título" (filetype:pdf OR filetype:epub) site:X -inurl:login
        """
        parts: list[str] = []
        operators: list[str] = []

        parts.append(f'intitle:{self._quote_phrase(query)}')
        operators.append("intitle")

        if filetype and filetype != "any":
            if filetype == "pdf":
                parts.append("filetype:pdf")
            else:
                parts.append(f"(filetype:{filetype} OR filetype:pdf)")
            operators.append(f"filetype:{filetype}")
        else:
            parts.append("(filetype:pdf OR filetype:epub OR filetype:mobi OR filetype:djvu)")
            operators.append("filetype:multi-pdf-first")

        if site and site != "any":
            parts.append(f"site:{site}")
            operators.append("site")
        else:
            parts.extend(self._shopping_exclusions(2))
            operators.append("-site:shopping")

        parts.append("-inurl:login")
        parts.append("-inurl:register")
        operators.append("-inurl:exclusions")

        raw = " ".join(parts)
        return DorkQuery(
            raw_query=raw,
            google_url=self._build_google_url(raw),
            strategy="filetype",
            description="Búsqueda por tipo de archivo con PDF prioritario",
            operators_used=operators,
        )

    def strategy_open_library(
        self,
        query: str,
        author: Optional[str] = None,
    ) -> DorkQuery:
        """
        Estrategia en bibliotecas digitales abiertas.
        Técnica: intitle:"título" "autor" (site:archive.org OR site:gutenberg.org …)
        """
        parts: list[str] = []
        operators: list[str] = []

        parts.append(f'intitle:{self._quote_phrase(query)}')
        operators.append("intitle")

        if author:
            parts.append(self._quote_phrase(author))
            operators.append("author-phrase")

        top_sites = [
            "archive.org",
            "gutenberg.org",
            "openlibrary.org",
            "oapen.org",
            "standardebooks.org",
            "pdfdrive.com",
        ]
        site_clause = " OR ".join(f"site:{s}" for s in top_sites)
        parts.append(f"({site_clause})")
        operators.append("site:open-libraries")

        raw = " ".join(parts)
        return DorkQuery(
            raw_query=raw,
            google_url=self._build_google_url(raw),
            strategy="open_library",
            description="Busca en las principales bibliotecas digitales abiertas",
            operators_used=operators,
        )

    def strategy_index_of(
        self,
        query: str,
        filetype: Optional[str] = None,
    ) -> DorkQuery:
        """
        Estrategia de directorios abiertos (dork clásico).
        Técnica: (intitle:"index of" OR intitle:"parent directory") "título" (".pdf" OR ".epub")
        """
        parts: list[str] = []
        operators: list[str] = []

        dir_clause = '(intitle:"index of" OR intitle:"parent directory")'
        parts.append(dir_clause)
        operators.append("intitle:directory-listing")

        safe_query = self._sanitize(query)
        parts.append(f'"{safe_query}"')
        operators.append("phrase")

        if filetype and filetype != "any":
            parts.append(f'".{filetype}"')
        else:
            parts.append('(".pdf" OR ".epub" OR ".mobi")')
        operators.append("ext-in-listing")

        parts.append("-inurl:login")
        operators.append("-inurl:login")

        raw = " ".join(parts)
        return DorkQuery(
            raw_query=raw,
            google_url=self._build_google_url(raw),
            strategy="index_of",
            description="Busca en directorios web abiertos (index of / parent directory)",
            operators_used=operators,
        )

    def strategy_isbn(self, isbn: str) -> DorkQuery:
        """
        Estrategia por ISBN — identificador único de edición.
        Técnica: "ISBN" intitle:"isbn" filetype:pdf
        """
        parts: list[str] = []
        operators: list[str] = []

        parts.append(self._quote_phrase(isbn))
        operators.append("isbn-phrase")

        parts.append('intext:isbn')
        operators.append("intext:isbn")

        parts.append("(filetype:pdf OR filetype:epub)")
        operators.append("filetype:multi")

        raw = " ".join(parts)
        return DorkQuery(
            raw_query=raw,
            google_url=self._build_google_url(raw),
            strategy="isbn",
            description="Busca por número ISBN único del libro",
            operators_used=operators,
        )

    def strategy_google_books(
        self,
        query: str,
        author: Optional[str] = None,
    ) -> DorkQuery:
        """
        Estrategia en Google Books.
        Técnica: site:books.google.com intitle:"título" inurl:/books/edition/
        """
        parts: list[str] = []
        operators: list[str] = []

        parts.append("site:books.google.com")
        operators.append("site:google-books")

        parts.append(f'intitle:{self._quote_phrase(query)}')
        operators.append("intitle")

        if author:
            parts.append(f'intext:{self._quote_phrase(author)}')
            operators.append("intext:author")

        parts.append("inurl:/books/edition/")
        operators.append("inurl:edition")

        raw = " ".join(parts)
        return DorkQuery(
            raw_query=raw,
            google_url=self._build_google_url(raw),
            strategy="google_books",
            description="Busca en Google Books (previews y libros gratuitos)",
            operators_used=operators,
        )

    def strategy_academic(
        self,
        query: str,
        author: Optional[str] = None,
    ) -> DorkQuery:
        """
        Estrategia en repositorios académicos y universitarios.
        Técnica: "título" "autor" (site:.edu OR site:.ac.uk …) (filetype:pdf OR filetype:epub)
        """
        parts: list[str] = []
        operators: list[str] = []

        parts.append(self._quote_phrase(query))
        operators.append("phrase")

        if author:
            parts.append(self._quote_phrase(author))
            operators.append("author-phrase")

        academic_domains = (
            "site:.edu OR site:.ac.uk OR site:.edu.mx OR "
            "site:.edu.ar OR site:.edu.br OR site:.edu.co OR "
            "site:.edu.pe OR site:.edu.cl OR site:.ac.nz"
        )
        parts.append(f"({academic_domains})")
        operators.append("site:academic-domains")

        parts.append("(filetype:pdf OR filetype:epub)")
        operators.append("filetype:multi")

        raw = " ".join(parts)
        return DorkQuery(
            raw_query=raw,
            google_url=self._build_google_url(raw),
            strategy="academic",
            description="Busca en repositorios de universidades y centros académicos",
            operators_used=operators,
        )

    def strategy_download_page(
        self,
        query: str,
        author: Optional[str] = None,
    ) -> DorkQuery:
        """
        Estrategia apuntando a páginas de descarga directa.
        Técnica: intitle:"título" inurl:(download|pdf|ebook|book) -site:amazon
        """
        parts: list[str] = []
        operators: list[str] = []

        parts.append(f'intitle:{self._quote_phrase(query)}')
        operators.append("intitle")

        if author:
            parts.append(self._quote_phrase(author))
            operators.append("author-phrase")

        parts.append('inurl:(download OR pdf OR ebook OR book)')
        operators.append("inurl:download-hints")

        parts.append("(filetype:pdf OR filetype:epub OR filetype:mobi)")
        operators.append("filetype:multi")

        parts.extend(self._shopping_exclusions(3))
        operators.append("-site:shopping")

        raw = " ".join(parts)
        return DorkQuery(
            raw_query=raw,
            google_url=self._build_google_url(raw),
            strategy="download_page",
            description="Apunta directamente a páginas de descarga de libros",
            operators_used=operators,
        )

    # ── API principal ─────────────────────────────────────────────────────────

    def build_primary(
        self,
        query: str,
        filetype: Optional[str] = None,
        site: Optional[str] = None,
        author: Optional[str] = None,
        isbn: Optional[str] = None,
    ) -> DorkQuery:
        """
        Consulta Dork principal según los parámetros disponibles.
        Prioridad: ISBN > sitio específico > formato específico > PDF directo.
        """
        if isbn:
            return self.strategy_isbn(isbn)

        if site and site != "any":
            return self.strategy_filetype(query, filetype or "pdf", site)

        if filetype and filetype not in ("any", "pdf", ""):
            return self.strategy_filetype(query, filetype)

        # Por defecto: búsqueda directa PDF (mejor para encontrar archivos)
        return self.strategy_direct_pdf(query, author, filetype)

    def build_all(
        self,
        query: str,
        filetype: Optional[str] = None,
        site: Optional[str] = None,
        author: Optional[str] = None,
        isbn: Optional[str] = None,
    ) -> list[DorkQuery]:
        """
        Genera todas las estrategias disponibles, PDF primero.
        """
        strategies: list[DorkQuery] = [
            self.strategy_direct_pdf(query, author, filetype),
            self.strategy_open_library(query, author),
            self.strategy_filetype(query, filetype, site),
            self.strategy_index_of(query, filetype),
            self.strategy_download_page(query, author),
            self.strategy_academic(query, author),
            self.strategy_google_books(query, author),
        ]

        if isbn:
            strategies.insert(0, self.strategy_isbn(isbn))

        return strategies
