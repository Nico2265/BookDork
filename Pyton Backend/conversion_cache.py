"""
=============================================================================
conversion_cache.py — Caché persistente de conversiones a Markdown
=============================================================================
Evita reconvertir documentos idénticos. Clave: SHA-256 del contenido binario.

ID de libro generado:  {isbn|hash8}-{lang}-{topic}
  Ejemplo:              9780262035613-en-ia
  Ejemplo sin ISBN:     a3f2b1c9-es-programacion

Almacenamiento:
  {CACHE_DIR}/{sha256}.md     → Markdown del documento
  {CACHE_DIR}/index.db        → SQLite WAL con metadatos completos

Thread-safety: lock explícito + SQLite WAL para lecturas/escrituras concurrentes.
=============================================================================
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bookdork.cache")

# ─────────────────────────────────────────────────────────────────────────────
# Patrones y clasificación
# ─────────────────────────────────────────────────────────────────────────────

_ISBN_RE = re.compile(
    r"(?:ISBN[-–\s]?(?:13|10)?[-–:\s]*)?"
    r"((?:97[89][-\s]?)?(?:\d[-\s]?){9}[\dX])",
    re.IGNORECASE,
)

_EDITION_RE = re.compile(
    r'(\d+)(?:st|nd|rd|th)\s+[Ee]dition'
    r'|[Ee]dition\s+(\d+)'
    r'|(First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth)\s+[Ee]dition'
    r'|(\d+[aª°])\s*[Ee]d(?:ición|ition)?\.?',
    re.IGNORECASE,
)

_TOPIC_KW: dict[str, list[str]] = {
    # ── IA / Machine Learning ──────────────────────────────────────────────────
    # Solo frases que NO aparecen en libros de programación general.
    # Se incluyen librerías ML específicas para que libros prácticos
    # de IA (con código Python/TensorFlow) no queden en 'programacion'.
    "ia":           ["artificial intelligence", "machine learning", "deep learning",
                     "neural network", "transformer model", "inteligencia artificial",
                     "natural language processing", "large language model",
                     "reinforcement learning", "computer vision", "generative ai",
                     "word embeddings", "fine-tuning", "aprendizaje automático",
                     "ciencia de datos", "gradient descent", "backpropagation",
                     "convolutional neural", "recurrent neural", "attention mechanism",
                     "support vector machine", "random forest", "model training",
                     "feature engineering", "overfitting", "underfitting",
                     "redes neuronales artificiales", "clasificación automática",
                     "llm", "gpt-4", "chatgpt", "llama model",
                     # Librerías ML/DL específicas (señal fuerte de libro de IA)
                     "tensorflow", "pytorch", "scikit-learn", "sklearn",
                     "keras", "huggingface", "langchain", "openai api",
                     "torch.nn", "tf.keras", "xgboost", "lightgbm",
                     "dataset training", "validation set", "test set accuracy",
                     "epoch", "batch size", "learning rate", "loss function"],

    # ── Programación ──────────────────────────────────────────────────────────
    # Términos que aparecen casi exclusivamente en libros técnicos de código
    "programacion": ["python", "javascript", "typescript", "programming",
                     "software engineering", "software development", "algorithm",
                     "data structure", "design pattern", "clean code", "refactoring",
                     "programación", "código fuente", "coding",
                     "object-oriented", "functional programming",
                     "database", "sql query", "rest api", "api design",
                     "backend development", "frontend development", "full stack",
                     "docker", "kubernetes", "devops", "microservices",
                     "unit testing", "test-driven", "continuous integration",
                     "version control", "debugging", "deployment",
                     "linked list", "binary tree", "hash table", "big o notation",
                     "time complexity", "space complexity",
                     "recursion", "dynamic programming",
                     "c++", "golang", "kotlin", "rust language",
                     "framework", "library", "package manager",
                     "web development", "operating system",
                     "estructura de datos", "compilador", "sistemas operativos",
                     "arquitectura de software", "paradigma de programación",
                     "bucle", "estructura de control",
                     # Web / diseño / UX
                     "html", "css", "html5", "css3",
                     "web design", "web page", "website", "webpage",
                     "usability", "user experience", "ux design",
                     "user interface", "responsive design", "accessibility",
                     "diseño web", "experiencia de usuario",
                     # Seguridad / hacking
                     "cybersecurity", "cyber security", "hacking",
                     "penetration testing", "ethical hacking",
                     "network security", "information security",
                     "vulnerability", "exploit", "malware",
                     "seguridad informática", "ciberseguridad",
                     # Cloud / infra
                     "cloud computing", "aws", "azure", "google cloud",
                     "serverless", "infrastructure as code",
                     # Datos
                     "data pipeline", "data engineering", "etl",
                     "data warehouse", "data lake", "apache spark",
                     "pandas", "numpy", "jupyter"],

    # ── Matemáticas ───────────────────────────────────────────────────────────
    "matematicas":  ["calculus", "álgebra lineal", "algebra lineal",
                     "probability theory", "statistics textbook",
                     "mathematics", "matemáticas", "topology",
                     "análisis matemático", "cálculo diferencial",
                     "ecuaciones diferenciales", "estadística matemática",
                     "probabilidad", "teoría de números", "geometría diferencial",
                     "trigonometría", "linear algebra textbook",
                     "combinatoria", "teoría de grafos", "análisis real",
                     "álgebra abstracta", "cálculo integral"],

    # ── Física ────────────────────────────────────────────────────────────────
    "fisica":       ["physics", "quantum mechanics", "classical mechanics",
                     "thermodynamics", "electromagnetism", "física",
                     "relatividad", "feynman lectures", "optics",
                     "electrodinámica", "partículas subatómicas", "astrofísica",
                     "mecánica cuántica", "termodinámica", "cosmología",
                     "gravitación", "plasma physics", "fotónica",
                     "ley de newton", "campo eléctrico", "campo magnético"],

    # ── Economía ──────────────────────────────────────────────────────────────
    # QUITADOS: "management", "trade", "business", "capital", "startup"
    # (aparecen constantemente en libros de programación como "memory management",
    # "trade-off", "business logic", "capital letter", "startup time")
    "economia":     ["economics", "macroeconomics", "microeconomics",
                     "monetary policy", "fiscal policy", "inflation rate",
                     "gross domestic product", "gdp growth",
                     "supply and demand", "free trade agreement",
                     "stock market", "financial markets", "financial crisis",
                     "asset allocation", "portfolio management",
                     "venture capital fund", "private equity",
                     "economía", "finanzas corporativas", "mercado financiero",
                     "inversión financiera", "bolsa de valores",
                     "contabilidad financiera", "análisis económico",
                     "política monetaria", "deuda pública",
                     "tipo de interés", "desempleo", "recesión económica",
                     "marketing mix", "estrategia empresarial",
                     "cadena de suministro", "comercio internacional"],

    # ── Biología ──────────────────────────────────────────────────────────────
    "biologia":     ["biology", "genetics", "evolution", "molecular biology",
                     "biología", "genética", "dna strand", "protein synthesis",
                     "ecología", "botánica", "zoología", "microbiología",
                     "anatomía", "fisiología", "biotecnología",
                     "adn", "ácido desoxirribonucleico",
                     "organismo vivo", "especie biológica", "ecosistema",
                     "célula eucariota", "célula procariota",
                     "reproducción celular", "selección natural"],

    # ── Historia ──────────────────────────────────────────────────────────────
    "historia":     ["history", "civilization", "ancient history",
                     "medieval period", "world war", "historia",
                     "civilización", "sapiens", "revolución francesa",
                     "revolución industrial", "roman empire", "geopolítica",
                     "arqueología", "renacimiento", "colonialism",
                     "segunda guerra mundial", "primera guerra mundial",
                     "imperio romano", "monarquía absoluta",
                     "república romana", "edad media", "edad antigua"],

    # ── Psicología ────────────────────────────────────────────────────────────
    # QUITADOS: "behavior", "cognitive", "memoria", "motivación", "mente"
    # Estos causan clasificar libros de programación como psicología:
    # "behavior-driven development", "cognitive complexity", "memory management"
    "psicologia":   ["psychology", "psychotherapy", "psychiatry",
                     "clinical psychology", "psychological disorder",
                     "psicología", "psicoanálisis", "terapia psicológica",
                     "salud mental", "mental health disorder",
                     "trastorno mental", "ansiedad clínica",
                     "depresión clínica", "terapia cognitiva conductual",
                     "freud", "jung", "kahneman", "skinner",
                     "neuroscience research", "social psychology",
                     "personality disorder", "emotional intelligence",
                     "mindfulness therapy", "trauma psicológico",
                     "bienestar psicológico", "terapia de grupo"],

    # ── Filosofía ─────────────────────────────────────────────────────────────
    "filosofia":    ["philosophy", "philosophical", "metaphysics",
                     "epistemology", "ontology", "filosofía", "ética filosófica",
                     "moral philosophy", "plato", "aristotle", "kant", "nietzsche",
                     "existencialismo", "racionalismo", "empirismo",
                     "fenomenología", "hermenéutica", "pragmatismo",
                     "stoicism", "utilitarianism", "deontología",
                     "filosofía política", "filosofía de la mente"],

    # ── Literatura ────────────────────────────────────────────────────────────
    "literatura":   ["novel", "poetry", "fiction", "narrative literature",
                     "novela", "poesía", "cuento literario", "literatura",
                     "ficción", "crónica literaria", "ensayo literario",
                     "storyline", "plot twist", "escritura creativa",
                     "teatro", "drama literario", "prosa",
                     "verso", "antología", "obra literaria"],

    # ── Química ───────────────────────────────────────────────────────────────
    "quimica":      ["chemistry", "chemical reaction", "molecule",
                     "atomic structure", "química", "molécula",
                     "reacción química", "química orgánica", "química inorgánica",
                     "espectroscopía", "polímeros", "nanotecnología",
                     "enlace químico", "tabla periódica", "estequiometría",
                     "oxidación", "reducción química", "catalizador"],

    # ── Medicina ──────────────────────────────────────────────────────────────
    "medicina":     ["medicine", "clinical", "disease", "medical diagnosis",
                     "treatment protocol", "medicina", "clínico",
                     "enfermedad", "diagnóstico médico", "farmacología",
                     "anatomía médica", "fisiología humana", "patología",
                     "cirugía", "salud pública", "healthcare",
                     "paciente", "síntoma", "terapéutica",
                     "epidemiología", "vacuna", "virus patógeno"],

    # ── Derecho ───────────────────────────────────────────────────────────────
    # IMPORTANTE: NO usar "law" ni "legal" solos — aparecen en los avisos de
    # copyright de todos los libros ("Copyright Law", "legal permission").
    "derecho":      ["criminal law", "civil law", "common law", "labor law",
                     "constitutional law", "international law", "law school",
                     "legal system", "legal proceedings", "legal liability",
                     "legal rights", "rule of law",
                     "legislation", "court ruling", "court decision",
                     "derecho", "jurídico", "contrato legal",
                     "derecho penal", "derecho civil", "derecho laboral",
                     "derecho procesal", "derechos humanos",
                     "tribunal", "juicio", "código civil",
                     "código penal", "jurisprudencia", "abogado",
                     "ley orgánica", "norma jurídica", "sentencia judicial",
                     "recurso de apelación", "constitución política"],
}

# Regex de detección de código fuente — señal fuerte de libro técnico de programación
_CODE_RE = re.compile(
    r"```[a-z]*\n"                           # cerca de código markdown
    r"|~~~[a-z]*\n"                          # bloque con tildes
    r"|\bdef\s+\w+\s*\("                     # Python: def func(
    r"|\bclass\s+\w+[\s:(]"                  # definición de clase
    r"|\bimport\s+\w"                        # import statement
    r"|\bfrom\s+\w+\s+import\b"             # from X import
    r"|#include\s*<"                         # C/C++ include
    r"|public\s+(?:class|static|void|int)\b" # Java/C#
    r"|\bconsole\.log\s*\("                  # JavaScript
    r"|\bprintf?\s*\("                       # C/Python print
    r"|\bpip\s+install\b"                    # pip
    r"|\bnpm\s+(?:install|run|start)\b"      # npm
    r"|\bgit\s+(?:commit|push|clone|pull)\b" # git
    r"|\bif\s+__name__"                      # Python main guard
    r"|\bfunction\s+\w+\s*\("               # JS/PHP function
    r"|\b(?:var|let|const)\s+\w+\s*="       # JS variable declaration
    r"|\breturn\s+(?:true|false|null|self|this|new\s+\w)"  # OOP returns
    r"|\bvoid\s+\w+\s*\("                   # Java/C void method
    r"|\bpublic\s+\w+\s+\w+\s*\("          # Java method signature
    r"|\bsyntax\s+error\b"                   # error de compilación
    r"|\bstack\s+(?:overflow|trace)\b"       # errores de runtime
    r"|\bexception\s+handling\b",            # manejo de excepciones
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────

_cache_instance: Optional["ConversionCache"] = None
_cache_init_lock = threading.Lock()


def get_cache(cache_dir: Optional[str] = None) -> "ConversionCache":
    """Devuelve la instancia singleton del caché (crea si no existe)."""
    global _cache_instance
    if _cache_instance is None:
        with _cache_init_lock:
            if _cache_instance is None:
                from .config import get_settings
                d = cache_dir or get_settings().CACHE_DIR
                _cache_instance = ConversionCache(Path(d))
    return _cache_instance


# ─────────────────────────────────────────────────────────────────────────────
# Clase principal
# ─────────────────────────────────────────────────────────────────────────────

class ConversionCache:
    """
    Caché thread-safe de conversiones Markdown.
    Backstore: archivos .md planos + SQLite (WAL) para metadatos.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db   = sqlite3.connect(str(cache_dir / "index.db"), check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA temp_store=MEMORY")
        self._init_schema()
        logger.info("Caché de conversiones inicializado en '%s'.", cache_dir)

    # ── Esquema ───────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        # Step 1: create the table (safe if already exists, no index yet)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS conversions (
                file_hash      TEXT PRIMARY KEY,
                book_id        TEXT NOT NULL,
                isbn           TEXT,
                language       TEXT,
                topic          TEXT,
                filename       TEXT,
                file_size      INTEGER,
                char_count     INTEGER,
                word_count     INTEGER,
                engine         TEXT,
                created_at     TEXT NOT NULL,
                accessed_at    TEXT NOT NULL,
                title          TEXT,
                author         TEXT,
                year           INTEGER,
                edition        TEXT,
                download_count INTEGER DEFAULT 0
            );
        """)
        self._db.commit()

        # Step 2: migrate columns that may be absent in older databases
        for col, coldef in [
            ("title",          "TEXT"),
            ("author",         "TEXT"),
            ("year",           "INTEGER"),
            ("edition",        "TEXT"),
            ("download_count", "INTEGER DEFAULT 0"),
            ("cover_url",      "TEXT"),
        ]:
            try:
                self._db.execute(f"ALTER TABLE conversions ADD COLUMN {col} {coldef}")
                self._db.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Step 3: create indexes after all columns are guaranteed to exist
        self._db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_book_id        ON conversions(book_id);
            CREATE INDEX IF NOT EXISTS idx_isbn           ON conversions(isbn) WHERE isbn IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_topic          ON conversions(topic);
            CREATE INDEX IF NOT EXISTS idx_lang           ON conversions(language);
            CREATE INDEX IF NOT EXISTS idx_download_count ON conversions(download_count);
            CREATE INDEX IF NOT EXISTS idx_created_at     ON conversions(created_at);
        """)

    # ── Hashing ───────────────────────────────────────────────────────────────

    @staticmethod
    def hash_content(content: bytes) -> str:
        """SHA-256 del contenido binario del archivo."""
        return hashlib.sha256(content).hexdigest()

    # ── Lectura ───────────────────────────────────────────────────────────────

    def get(self, file_hash: str) -> Optional[dict]:
        """
        Devuelve la conversión cacheada o None si no existe / fue borrada.
        Actualiza accessed_at como efecto colateral (frecuencia de acceso).
        """
        with self._lock:
            row = self._db.execute(
                "SELECT book_id, isbn, language, topic, filename, char_count, word_count "
                "FROM conversions WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()

        if row is None:
            return None

        md_path = self._dir / f"{file_hash}.md"
        if not md_path.exists():
            self._evict(file_hash)
            return None

        book_id, isbn, language, topic, filename, char_count, word_count = row
        markdown = md_path.read_text(encoding="utf-8")

        with self._lock:
            self._db.execute(
                "UPDATE conversions SET accessed_at = ? WHERE file_hash = ?",
                (_now(), file_hash),
            )
            self._db.commit()

        stem = Path(filename).stem if filename else book_id
        logger.info("Cache HIT  — hash=%s  book_id=%s", file_hash[:8], book_id)
        return {
            "success":           True,
            "cache_hit":         True,
            "book_id":           book_id,
            "original_filename": filename,
            "md_filename":       f"{stem}.md",
            "markdown":          markdown,
            "char_count":        char_count,
            "word_count":        word_count,
        }

    # ── Escritura ─────────────────────────────────────────────────────────────

    def put(
        self,
        file_hash: str,
        markdown: str,
        filename: str,
        file_size: int,
        isbn: Optional[str]      = None,
        language: Optional[str]  = None,
        topic: Optional[str]     = None,
        engine: Optional[str]    = None,
        title: Optional[str]     = None,
        author: Optional[str]    = None,
        year: Optional[int]      = None,
        edition: Optional[str]   = None,
        cover_url: Optional[str] = None,
    ) -> str:
        """
        Almacena una conversión nueva y devuelve el book_id generado.
        Preserva download_count si el registro ya existía (idempotencia segura).
        """
        book_id = make_book_id(file_hash, isbn, language, topic)
        (self._dir / f"{file_hash}.md").write_text(markdown, encoding="utf-8")

        now = _now()
        with self._lock:
            self._db.execute(
                """INSERT OR REPLACE INTO conversions
                   (file_hash, book_id, isbn, language, topic, filename, file_size,
                    char_count, word_count, engine, created_at, accessed_at,
                    title, author, year, edition, cover_url, download_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                           COALESCE((SELECT download_count FROM conversions WHERE file_hash=?),0))""",
                (
                    file_hash, book_id, isbn, language, topic, filename, file_size,
                    len(markdown), len(markdown.split()),
                    engine, now, now,
                    title, author, year, edition, cover_url,
                    file_hash,
                ),
            )
            self._db.commit()

        logger.info("Cache STORE — hash=%s  book_id=%s  engine=%s", file_hash[:8], book_id, engine)
        return book_id

    # ── The Info Vault ────────────────────────────────────────────────────────

    def list_books(
        self,
        topic: Optional[str]    = None,
        year: Optional[int]     = None,
        edition: Optional[str]  = None,
        period: Optional[str]   = None,
        limit: int              = 24,
        offset: int             = 0,
    ) -> dict:
        """
        List cached books sorted by download_count DESC, with optional filters.
        period: "today" | "week" | "biweek" | "month"  (filters by created_at)
        edition: "na" matches books with no edition info; any other value does LIKE match.
        """
        conditions: list[str] = []
        params: list = []

        if topic:
            conditions.append("topic = ?")
            params.append(topic)
        if year:
            conditions.append("year = ?")
            params.append(int(year))
        if edition:
            if edition.lower() == "na":
                conditions.append("(edition IS NULL OR edition = '')")
            else:
                conditions.append("edition LIKE ?")
                params.append(f"%{edition}%")
        if period:
            delta_days = {"today": 1, "week": 7, "biweek": 14, "month": 30}.get(period)
            if delta_days:
                from datetime import timedelta
                since = (datetime.now(timezone.utc) - timedelta(days=delta_days)).isoformat()
                conditions.append("created_at >= ?")
                params.append(since)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._lock:
            (total,) = self._db.execute(
                f"SELECT COUNT(*) FROM conversions {where}", params
            ).fetchone()
            rows = self._db.execute(
                f"""SELECT book_id, title, author, year, edition, topic, language,
                           char_count, word_count, download_count, created_at, isbn, filename,
                           cover_url
                    FROM conversions {where}
                    ORDER BY download_count DESC, created_at DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()

        books = []
        for r in rows:
            fname = r[12] or ""
            books.append({
                "book_id":        r[0],
                "title":          r[1] or (Path(fname).stem if fname else r[0]),
                "author":         r[2],
                "year":           r[3],
                "edition":        r[4],
                "topic":          r[5] or "general",
                "language":       r[6],
                "char_count":     r[7],
                "word_count":     r[8],
                "download_count": r[9],
                "created_at":     r[10],
                "isbn":           r[11],
                "cover_url":      r[13],
            })
        return {"total": int(total), "books": books}

    def increment_download(self, book_id: str) -> None:
        """Increment the download counter for a vault book."""
        with self._lock:
            self._db.execute(
                "UPDATE conversions "
                "SET download_count = download_count + 1, accessed_at = ? "
                "WHERE book_id = ?",
                (_now(), book_id),
            )
            self._db.commit()

    def get_by_book_id(self, book_id: str) -> Optional[tuple[str, str]]:
        """Return (markdown_content, filename) for a book, or None if not found."""
        with self._lock:
            row = self._db.execute(
                "SELECT file_hash, filename FROM conversions WHERE book_id = ?",
                (book_id,),
            ).fetchone()

        if row is None:
            return None
        file_hash, filename = row
        md_path = self._dir / f"{file_hash}.md"
        if not md_path.exists():
            return None
        return md_path.read_text(encoding="utf-8"), filename or f"{book_id}.md"

    # ── Estadísticas ──────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            (total,) = self._db.execute("SELECT COUNT(*) FROM conversions").fetchone()
        disk_bytes = sum(f.stat().st_size for f in self._dir.glob("*.md"))
        return {"entries": int(total), "disk_bytes": disk_bytes}

    # ── Reclasificación masiva ────────────────────────────────────────────────

    def reclassify_all(self) -> dict:
        """
        Reclasifica todos los libros del caché usando el algoritmo actualizado.
        Lee cada .md y vuelve a correr classify_topic() + make_book_id().
        Actualiza 'topic' y 'book_id' en SQLite.
        Devuelve {"checked": N, "updated": M}.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT file_hash, book_id, isbn, language, topic, filename FROM conversions"
            ).fetchall()

        checked = 0
        updated = 0
        for file_hash, old_book_id, isbn, language, old_topic, filename in rows:
            md_path = self._dir / f"{file_hash}.md"
            if not md_path.exists():
                continue
            checked += 1
            try:
                md = md_path.read_text(encoding="utf-8")
            except OSError:
                continue

            new_topic   = classify_topic(filename or "", md)
            new_book_id = make_book_id(file_hash, isbn, language, new_topic)

            if new_topic != old_topic:
                with self._lock:
                    self._db.execute(
                        "UPDATE conversions SET topic = ?, book_id = ? WHERE file_hash = ?",
                        (new_topic, new_book_id, file_hash),
                    )
                    self._db.commit()
                logger.info(
                    "Reclassified %s: %s → %s", (filename or file_hash[:8]), old_topic, new_topic
                )
                updated += 1

        logger.info("reclassify_all: checked=%d updated=%d", checked, updated)
        return {"checked": checked, "updated": updated}

    # ── Backfill de metadatos (title / author vacíos) ────────────────────────

    def reextract_metadata_all(self) -> dict:
        """
        Re-extrae title y author para TODAS las entradas del caché.
        Siempre re-ejecuta extract_author_from_text() para sobreescribir valores
        incorrectos de backfills anteriores. Si el extractor no halla autor,
        conserva el valor previo (protege autores correctos no detectados por patrones).
        Devuelve {"checked": N, "updated": M}.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT file_hash, filename, title, author FROM conversions"
            ).fetchall()

        checked = 0
        updated = 0
        for file_hash, filename, old_title, old_author in rows:
            md_path = self._dir / f"{file_hash}.md"
            if not md_path.exists():
                continue
            checked += 1
            try:
                md = md_path.read_text(encoding="utf-8")
            except OSError:
                continue

            # Título: derivado del filename — siempre determinista y seguro de reescribir
            new_title = clean_title(filename or "") or old_title

            # Autor: siempre re-ejecutar el extractor para corregir backfills erróneos;
            # solo conservar el valor anterior si el extractor no encuentra nada.
            extracted = extract_author_from_text(filename or "", md)
            if extracted is not None:
                new_author = extracted
            elif old_author and (old_author.isupper() or '\n' in old_author):
                new_author = None   # limpia valores ALL-CAPS o multi-linea
            else:
                new_author = old_author

            if new_title != old_title or new_author != old_author:
                with self._lock:
                    self._db.execute(
                        "UPDATE conversions SET title = ?, author = ? WHERE file_hash = ?",
                        (new_title, new_author, file_hash),
                    )
                    self._db.commit()
                logger.info(
                    "Metadata updated — %s: title=%r author=%r",
                    (filename or file_hash[:8])[:40], new_title, new_author,
                )
                updated += 1

        logger.info("reextract_metadata_all: checked=%d updated=%d", checked, updated)
        return {"checked": checked, "updated": updated}

    # ── Helpers internos ──────────────────────────────────────────────────────

    def _evict(self, file_hash: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM conversions WHERE file_hash = ?", (file_hash,))
            self._db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de metadatos (usados por main.py tras la conversión)
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_book_id(
    file_hash: str,
    isbn: Optional[str],
    language: Optional[str],
    topic: Optional[str],
) -> str:
    """
    Construye el ID semántico único del libro.
    Formato:  {isbn|hash8}-{lang}-{topic}
    Ejemplo:  9780262035613-en-ia  /  a3f2b1c9-es-programacion
    """
    prefix = isbn if isbn else file_hash[:8]
    lang   = (language or "und")[:5].lower()
    cat    = re.sub(r"[^a-z0-9]", "", (topic or "general").lower())[:15]
    return f"{prefix}-{lang}-{cat}"


def extract_isbn(text: str) -> Optional[str]:
    """
    Extrae y valida el primer ISBN-13 o ISBN-10 encontrado en el texto.
    Devuelve solo dígitos (sin guiones), o None si no se encuentra.
    """
    for m in _ISBN_RE.finditer(text[:10_000]):
        raw = re.sub(r"[-–\s]", "", m.group(1))
        if _valid_isbn(raw):
            return raw
    return None


def _valid_isbn(raw: str) -> bool:
    if len(raw) == 13 and raw.isdigit():
        w   = [1 if i % 2 == 0 else 3 for i in range(12)]
        chk = (10 - sum(int(d) * x for d, x in zip(raw, w)) % 10) % 10
        return chk == int(raw[12])
    if len(raw) == 10:
        total = sum(
            (10 - i) * (10 if d.upper() == "X" else int(d))
            for i, d in enumerate(raw)
        )
        return total % 11 == 0
    return False


def detect_language(text: str) -> str:
    """
    Detecta el idioma predominante del texto.
    Requiere: pip install langdetect
    Retorna código ISO 639-1 (ej. 'es', 'en') o 'und' si falla.
    """
    try:
        from langdetect import detect
        return detect(text[:3_000]) or "und"
    except Exception:
        return "und"


def clean_title(filename: str) -> str:
    """Derives a readable book title from a filename (strips year, edition, brackets)."""
    stem = Path(filename).stem if filename else ""
    stem = re.sub(r'\b\d+(?:st|nd|rd|th)\s+edition\b', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\b(?:first|second|third|fourth|fifth|sixth|seventh)\s+edition\b', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]', '', stem)
    stem = re.sub(r'\[.*?\]', '', stem)
    stem = re.sub(r'[-_]+', ' ', stem)
    return re.sub(r'\s{2,}', ' ', stem).strip() or (Path(filename).stem if filename else "Unknown")


def extract_year_from_text(filename: str, text_sample: str) -> Optional[int]:
    """Extract publication year from filename or leading markdown content."""
    m = re.search(r'[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]', filename)
    if m:
        return int(m.group(1))
    for pat in [
        r'(?:copyright|©|published|first\s+published|edition)\s*(?:in\s+)?((?:19|20)\d{2})',
        r'^#.*?((?:19|20)\d{2})',
    ]:
        m = re.search(pat, text_sample[:3_000], re.IGNORECASE | re.MULTILINE)
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2025:
                return y
    return None


def extract_author_from_text(filename: str, text_sample: str) -> Optional[str]:
    """
    Extrae el nombre del autor desde el nombre de archivo o el contenido markdown.

    Orden de intentos (de mayor a menor fiabilidad):
      1. Patrón "Título - Autor" / "Autor - Título" en el nombre de archivo.
      2. Patrón "___Autor" al final del nombre de archivo (triple guion bajo).
      3. Patrón "by Autor" al final del nombre de archivo.
      4. Marcadores explícitos en el texto — sólo una línea, sin cruzar saltos.

    Se usa [a-zA-Z'] en el componente de nombre para soportar apellidos
    compuestos (McFarland, MacDonald, O'Brien, J.) sin truncar.
    El separador _SEP evita cruzar líneas para no capturar ciudades,
    subtítulos ni créditos de portada que aparecen a continuación.
    """
    # Palabra completa (>=1 char tras mayusc.) O inicial con punto "X."
    _N   = r"(?:[A-Z][a-zA-Z']+|[A-Z]\.)"
    # Separador entre nombres — NO cruza saltos de linea
    _SEP = r"(?:[^\S\n]+(?:and|&|y)[^\S\n]+|[^\S\n]*,[^\S\n]*|[^\S\n]+)"
    _NAMES = rf"({_N}(?:{_SEP}{_N}){{0,5}})"

    _SKIP = {"edition", "press", "publishing", "published", "university",
             "chapter", "contents", "isbn", "copyright", "reserved",
             "introduction", "preface", "foreword", "table", "volume",
             "acknowledgments", "third", "second", "first", "fourth", "fifth"}

    # Palabras de titulos/subtitulos que no aparecen en nombres de personas
    _NON_NAME = {
        "the", "approach", "modern", "hacking", "ethical", "strategy",
        "innovative", "digital", "products", "guide", "complete",
        "handbook", "manual", "textbook", "fundamentals", "principles",
        "that", "people", "want", "devise", "make", "think", "things",
        "applications", "concepts", "theory", "practice", "using",
        "learning", "mastering", "advanced", "beginner", "missing",
        "revisited", "essential", "practical", "professional",
    }

    stem_raw = Path(filename).stem if filename else ""
    stem     = stem_raw.replace('_', ' ')

    # -- 1. "[Author Name]" al final del filename (formato BookFi/PDFDrive)
    _KNOWN_SITES = {"bookfi", "pdfdrive", "libgen", "z-lib", "bookzz", "org"}
    m_bracket = re.search(r'\[([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,4})\]\s*$', stem_raw)
    if m_bracket:
        cand = m_bracket.group(1).strip()
        words_cand = cand.split()
        if (not any(w.lower() in _KNOWN_SITES for w in words_cand)
                and not any(w.lower() in _SKIP for w in words_cand)
                and len(cand) > 4):
            return cand

    # -- 2. "Title - Author" o "Author - Title" separados por ' - '
    if ' - ' in stem:
        for part in reversed(stem.split(' - ')):
            part  = part.strip()
            words = [w for w in part.split() if w and w[0].isalpha()]
            if (1 <= len(words) <= 5
                    and all(w[0].isupper() for w in words)
                    and not any(w.lower() in _SKIP for w in words)):
                return part

    # -- 3. "___Autor" al final del filename (ej. "Macro___Mankiw.pdf")
    if '___' in stem_raw:
        last  = stem_raw.split('___')[-1].replace('_', ' ').strip()
        words = [w for w in last.split() if w and w[0].isalpha()]
        if (1 <= len(words) <= 5
                and all(w[0].isupper() for w in words)
                and not any(w.lower() in _SKIP for w in words)):
            return ' '.join(words)

    # -- 4. "by Autor" al final del nombre de archivo
    m = re.search(
        r'\bby\s+([A-Z][a-zA-Z]+(?:[^\S\n]+[A-Z][a-zA-Z]+){0,4})\s*[\[\s]',
        stem, re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).strip()
        if len(candidate) > 4:
            return candidate

    # -- 5. Escaneo de portada: primera seccion (hasta "---"), ultima linea valida
    _SCAN_SKIP = _SKIP | _NON_NAME
    _CONNECTORS = {"and", "&", "y", "e", "und", "et"}
    first_section = text_sample.split('\n---')[0][:600]
    scan_lines = [ln.strip() for ln in first_section.splitlines() if ln.strip()]
    last_valid: Optional[str] = None
    for line in scan_lines:
        line_core = re.split(r',\s*[A-Z]{2,}', line)[0].strip()
        words = line_core.split()
        if not (2 <= len(words) <= 5):
            continue
        if line_core.isupper():
            continue
        non_conn = [w for w in words if w.lower() not in _CONNECTORS]
        if not non_conn:
            continue
        # Reject lines where any word is ALL-CAPS and longer than 2 chars (acronyms)
        if any(w.isupper() and len(w) > 2 for w in non_conn):
            continue
        if not all(w[0].isupper() for w in non_conn if w and w[0].isalpha()):
            continue
        if any(w.lower() in _SCAN_SKIP for w in words):
            continue
        if not all(re.match(r"^[A-Za-z][a-zA-Z']*\.?$", w) for w in words):
            continue
        if not any(len(w) > 3 for w in non_conn):
            continue
        last_valid = line_core
    if last_valid:
        return last_valid

    # -- 5. Marcadores explicitos en texto (primeros 1 500 chars)
    sample = text_sample[:1_500]
    for pat in [
        rf'^[Bb]y\s+{_NAMES}\s*$',
        rf'[Aa]uthors?[:\s]+{_NAMES}\s*$',
        rf'[Ww]ritten\s+by\s+{_NAMES}\s*$',
        rf'[Ee]dited\s+by\s+{_NAMES}\s*$',
    ]:
        m = re.search(pat, sample, re.MULTILINE)
        if m:
            candidate = m.group(1).split('\n')[0].strip(' ,.')
            if (4 < len(candidate) < 80
                    and candidate.split()[0].lower() not in _SKIP
                    and not candidate.isupper()
                    and not any(w.lower() in _NON_NAME for w in candidate.split())):
                return candidate

    return None


def extract_edition_from_text(filename: str, text_sample: str) -> Optional[str]:
    """Extract edition string from filename or early content (e.g. '2nd', 'Third')."""
    combined = filename + " " + text_sample[:2_000]
    m = _EDITION_RE.search(combined)
    if m:
        return next(g for g in m.groups() if g is not None).strip()
    return None


def classify_topic(title: str, text_sample: str) -> str:
    """
    Clasifica el documento en una categoría temática.

    Estrategia en 3 capas:
      1. Detección de código fuente → bono directo a 'programacion'.
         Los libros de programación casi siempre contienen bloques de código,
         imports, definiciones de funciones o comandos de terminal.
      2. Puntuación por palabras clave (título ×8, texto primeros 6 000 chars).
         Las keywords de cada tema son frases específicas que minimizan
         falsos positivos entre categorías.
      3. La categoría ganadora es la de mayor puntuación total.

    Falsos positivos eliminados en esta versión:
      • "behavior" y "cognitive" ya no puntúan para psicología
        (aparecen constantemente en "BDD", "cognitive complexity", "OOP behavior").
      • "management", "trade", "business" ya no puntúan para economía
        (se confundían con "memory management", "trade-off", "business logic").
    """
    title_lower  = title.lower()

    # Estrategia de muestreo: incluir primeros 200 chars (título/subtítulo del
    # libro en el markdown) + saltar chars 200-500 (aviso de copyright típico:
    # "All rights reserved", "Copyright Law", "legal permission") + continuar
    # desde char 500 hasta 6 700.  Así se captura el título del libro Y se
    # evita que el copyright contamine el clasificador con falsos positivos
    # de 'derecho' (era el bug que hacía que HTML&CSS → derecho).
    head   = text_sample[:200].lower()
    body   = text_sample[500:6_700].lower()
    sample_lower = head + " " + body

    # Título con peso ×8: es la señal más fiable y concisa del tema
    hay = (title_lower + " ") * 8 + sample_lower

    # ── Capa 1: detección de código fuente ────────────────────────────────────
    code_hits  = len(_CODE_RE.findall(sample_lower))
    code_bonus = min(code_hits * 5, 80)   # cap en 80 pts para no aplastar todo

    # ── Capa 2 + 3: puntuación por categoría ─────────────────────────────────
    scores: dict[str, int] = {}
    for topic, kws in _TOPIC_KW.items():
        score = sum(hay.count(kw) for kw in kws)
        if topic == "programacion":
            score += code_bonus
        scores[topic] = score

    best_score = max(scores.values(), default=0)
    # Umbral mínimo: si nada supera 4 pts (menos que un keyword en el título),
    # devolver "general" en vez de asignar categoría por ruido estadístico.
    if best_score < 4:
        return "general"
    return max(scores, key=lambda t: scores[t])
