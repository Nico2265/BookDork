"""
seed_books.py — Carga 15 libros de ejemplo en Meilisearch
Ejecutar desde la carpeta raíz del proyecto:
    python seed_books.py
"""
import meilisearch

# ── Conexión ──────────────────────────────────────────────────────────────────
MEILI_HOST = "http://localhost:7700"
MEILI_KEY  = "changeme_strong_master_key"
INDEX_NAME = "books"

client = meilisearch.Client(MEILI_HOST, MEILI_KEY)

# ── Crear índice ──────────────────────────────────────────────────────────────
try:
    client.create_index(INDEX_NAME, {"primaryKey": "id"})
    print(f"✓ Índice '{INDEX_NAME}' creado.")
except Exception:
    print(f"✓ Índice '{INDEX_NAME}' ya existe.")

index = client.index(INDEX_NAME)

# ── Configuración de búsqueda ─────────────────────────────────────────────────
index.update_searchable_attributes(["title", "author", "description", "tags"])
index.update_filterable_attributes(["filetype", "language", "source_site"])
index.update_typo_tolerance({"enabled": True, "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 8}})
print("✓ Configuración aplicada.")

# ── Libros de ejemplo ─────────────────────────────────────────────────────────
BOOKS = [
    {"id": "1",  "title": "El ingenioso hidalgo don Quijote de la Mancha", "author": "Miguel de Cervantes", "description": "La primera novela moderna. Alonso Quijano enloquece leyendo libros de caballerías y se convierte en caballero andante junto a Sancho Panza.", "filetype": "pdf",  "language": "es", "year": 1605, "source_site": "archive.org",    "isbn": "978-84-206-8068-9", "tags": ["clásico", "español", "novela"]},
    {"id": "2",  "title": "Cien años de soledad",                          "author": "Gabriel García Márquez", "description": "Obra maestra del realismo mágico. La saga de la familia Buendía en el mítico pueblo de Macondo.", "filetype": "epub", "language": "es", "year": 1967, "source_site": "openlibrary.org", "isbn": "978-84-397-2038-0", "tags": ["realismo mágico", "Colombia", "Nobel"]},
    {"id": "3",  "title": "Fundación",                                     "author": "Isaac Asimov",           "description": "El Imperio Galáctico colapsa. Hari Seldon y su Fundación intentan preservar el conocimiento humano.", "filetype": "epub", "language": "es", "year": 1951, "source_site": "openlibrary.org", "isbn": "978-84-450-7253-4", "tags": ["ciencia ficción", "space opera", "clásico"]},
    {"id": "4",  "title": "Pride and Prejudice",                           "author": "Jane Austen",            "description": "Elizabeth Bennet navigates love and class in Regency England. A timeless romantic novel.", "filetype": "epub", "language": "en", "year": 1813, "source_site": "gutenberg.org",   "isbn": "978-0-14-143951-8", "tags": ["romance", "british literature", "classic"]},
    {"id": "5",  "title": "La metamorfosis",                               "author": "Franz Kafka",            "description": "Gregor Samsa amanece convertido en un insecto gigante. Relato expresionista sobre la alienación.", "filetype": "pdf",  "language": "es", "year": 1915, "source_site": "archive.org",    "isbn": "978-84-206-8069-6", "tags": ["expresionismo", "clásico"]},
    {"id": "6",  "title": "Clean Code",                                    "author": "Robert C. Martin",       "description": "Principles and practices of writing clean, maintainable code. Essential for every software developer.", "filetype": "pdf",  "language": "en", "year": 2008, "source_site": "archive.org",    "isbn": "978-0-13-235088-4", "tags": ["programación", "software", "ingeniería"]},
    {"id": "7",  "title": "Frankenstein",                                  "author": "Mary Shelley",           "description": "Victor Frankenstein creates a sapient creature. One of the earliest science fiction novels.", "filetype": "pdf",  "language": "en", "year": 1818, "source_site": "gutenberg.org",   "isbn": "978-0-14-143947-1", "tags": ["gothic", "science fiction", "horror"]},
    {"id": "8",  "title": "Historia del tiempo",                           "author": "Stephen Hawking",        "description": "El Big Bang, agujeros negros y la relatividad explicados para el público general.", "filetype": "pdf",  "language": "es", "year": 1988, "source_site": "openlibrary.org", "isbn": "978-84-8432-411-7", "tags": ["ciencia", "cosmología", "física"]},
    {"id": "9",  "title": "El arte de la guerra",                          "author": "Sun Tzu",                "description": "Tratado de estrategia militar chino. Aplicable a negocios, deportes y vida cotidiana.", "filetype": "pdf",  "language": "es", "year": -500, "source_site": "archive.org",    "isbn": "978-84-672-2338-5", "tags": ["estrategia", "filosofía", "clásico"]},
    {"id": "10", "title": "Moby Dick",                                     "author": "Herman Melville",        "description": "The obsessive quest of Captain Ahab for the giant white whale that bit off his leg.", "filetype": "epub", "language": "en", "year": 1851, "source_site": "gutenberg.org",   "isbn": "978-0-14-243723-4", "tags": ["adventure", "american literature", "classic"]},
    {"id": "11", "title": "The Adventures of Sherlock Holmes",             "author": "Arthur Conan Doyle",     "description": "Twelve short stories featuring the brilliant detective Sherlock Holmes and his companion Dr. Watson.", "filetype": "epub", "language": "en", "year": 1892, "source_site": "gutenberg.org",   "isbn": "978-0-14-304070-3", "tags": ["mystery", "detective", "british"]},
    {"id": "12", "title": "Alice's Adventures in Wonderland",              "author": "Lewis Carroll",          "description": "Alice falls through a rabbit hole into a fantasy world of anthropomorphic creatures.", "filetype": "epub", "language": "en", "year": 1865, "source_site": "gutenberg.org",   "isbn": "978-0-14-143976-1", "tags": ["fantasy", "children", "classic"]},
    {"id": "13", "title": "Python Programming",                            "author": "John Zelle",             "description": "Introduction to computer science and programming using Python. For beginners.", "filetype": "pdf",  "language": "en", "year": 2016, "source_site": "archive.org",    "isbn": "978-1-59028-000-0", "tags": ["programación", "python", "educación"]},
    {"id": "14", "title": "Ulysses",                                       "author": "James Joyce",            "description": "Modernist novel following Leopold Bloom through Dublin. One of the greatest literary works.", "filetype": "pdf",  "language": "en", "year": 1922, "source_site": "gutenberg.org",   "isbn": "978-0-19-280551-7", "tags": ["modernism", "irish", "classic"]},
    {"id": "15", "title": "El principito",                                 "author": "Antoine de Saint-Exupéry","description": "Un aviador conoce a un principito venido de un asteroide lejano. Fábula filosófica universal.", "filetype": "epub", "language": "es", "year": 1943, "source_site": "openlibrary.org", "isbn": "978-84-204-8388-5", "tags": ["fábula", "filosofía", "clásico"]},
]

# ── Indexar ───────────────────────────────────────────────────────────────────
task = index.add_documents(BOOKS)
client.wait_for_task(task.task_uid, timeout_in_ms=15000)

stats = index.get_stats()
print(f"✓ {stats.number_of_documents} libros indexados correctamente.")
print("  Ahora busca en http://localhost:8000")
 