from falkordb import Graph

# (label, property) pairs that every MERGE/MATCH looks up by.
RANGE_INDEXES = [
    ("Repo", "name"),
    ("Person", "login"),
    ("Ticket", "key"),
    ("Issue", "key"),
    ("PR", "key"),
    ("Comment", "id"),
    ("File", "key"),
    ("Dir", "key"),
    ("Label", "name"),
    ("Release", "key"),
    ("Ticket", "created_at"),
    ("PR", "merged_at"),
]

FULLTEXT_INDEXES = [
    ("Ticket", ("title", "body")),
    ("Comment", ("body",)),
]


def _ignore_existing(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as e:  # FalkorDB raises if the index already exists
        if "already indexed" not in str(e).lower() and "already exists" not in str(e).lower():
            raise


def ensure_schema(g: Graph) -> None:
    for label, prop in RANGE_INDEXES:
        _ignore_existing(g.create_node_range_index, label, prop)
    for label, props in FULLTEXT_INDEXES:
        _ignore_existing(g.create_node_fulltext_index, label, *props)
