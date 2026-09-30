import os
from psycopg_pool import ConnectionPool

pool = ConnectionPool(
    os.environ["DATABASE_URL"],
    min_size=5,
    max_size=20,
    open=False,
)
