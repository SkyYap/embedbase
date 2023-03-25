import json
import asyncio
from typing import Coroutine, List, Optional

from pandas import DataFrame, Series

from embedbase.databases import VectorDatabase
from embedbase.utils import BatchGenerator


class Postgres(VectorDatabase):
    def __init__(
        self,
    ):
        """
        Implements a vector database using postgres
        """
        try:
            import psycopg
            from pgvector.psycopg import register_vector

            conn_str = f"postgresql://postgres:localdb@0.0.0.0/embedbase"
            self.conn = psycopg.connect(conn_str, dbname="embedbase")
            self.conn.autocommit = True
            self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            register_vector(self.conn)
            # self.conn.execute("DROP TABLE IF EXISTS documents")
            self.conn.execute(
                """
create table documents (
    id text primary key,
    data text,
    embedding vector (1536),
    hash text,
    dataset_id text,
    user_id text,
    metadata json,
    created_date TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"""
            )
            self.conn.execute(
                """
create index on documents
using ivfflat (embedding vector_cosine_ops)
with (lists = 100);
"""
            )
            self.conn.execute(
                """
create or replace function match_documents (
  query_embedding vector(1536),
  similarity_threshold float,
  match_count int,
  query_dataset_id text,
  query_user_id text default null
)
returns table (
  id text,
  data text,
  score float,
  hash text,
  embedding vector(1536),
  metadata json
)
language plpgsql
as $$
begin
  return query
  select
    documents.id,
    documents.data,
    (1 - (documents.embedding <=> query_embedding)) as similarity,
    documents.hash,
    documents.embedding,
    documents.metadata
  from documents
  where 1 - (documents.embedding <=> query_embedding) > similarity_threshold
    and query_dataset_id = documents.dataset_id
    and (query_user_id is null or query_user_id = documents.user_id)
  order by documents.embedding <=> query_embedding
  limit match_count;
end;
$$;"""
            )
            self.conn.execute(
                """
CREATE OR REPLACE VIEW distinct_datasets AS
SELECT dataset_id, user_id, COUNT(*) AS documents_count
FROM documents
GROUP BY dataset_id, user_id;
"""
            )

        except ImportError:
            raise ImportError(
                "Please install pgvector and psycopg with `pip install pgvector psycopg`"
            )
        except psycopg.OperationalError:
            raise psycopg.OperationalError(
                "Please install postgresql and create a database named embedbase"
            )
        except Exception as e:
            print(e)

    async def select(
        self,
        ids: List[str] = [],
        hashes: List[str] = [],
        dataset_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[dict]:
        # either ids or hashes must be provided
        assert ids or hashes, "ids or hashes must be provided"
        from psycopg import sql

        query = """
select id, data, embedding, hash, metadata
from documents
where
    {conditions}
"""
        conditions = []
        if ids:
            conditions.append(
                sql.SQL("id in ({})").format(sql.SQL(",").join(map(sql.Literal, ids)))
            )
        if hashes:
            conditions.append(
                sql.SQL("hash in ({})").format(
                    sql.SQL(",").join(map(sql.Literal, hashes))
                )
            )
        if dataset_id:
            conditions.append(
                sql.SQL("dataset_id = {}").format(sql.Literal(dataset_id))
            )
        if user_id:
            conditions.append(sql.SQL("user_id = {}").format(sql.Literal(user_id)))
        data = []
        results = self.conn.execute(
            sql.SQL(query).format(conditions=sql.SQL(" and ").join(conditions))
        )
        for row in results:
            data.append(
                {
                    "id": row[0],
                    "data": row[1],
                    "embedding": row[2],
                    "hash": row[3],
                    "metadata": row[4],
                }
            )
        return data

    async def update(
        self,
        df: DataFrame,
        dataset_id: str,
        user_id: Optional[str] = None,
        store_data: bool = True,
    ):
        def _d(row: Series):
            data = [
                row.id,
                row.data if store_data else None,
                row.embedding,
                row.hash,
                dataset_id,
                user_id,
                json.dumps(row.metadata),
            ]
            return data

        values = [tuple(_d(row)) for _, row in df.iterrows()]
        num_columns = len(values[0])
        placeholders = ", ".join(
            ["(" + ", ".join(["%s"] * num_columns) + ")"] * len(values)
        )
        flat_values = [item for sublist in values for item in sublist]
        q = f"""
            INSERT INTO documents(id, data, embedding, hash, dataset_id, user_id, metadata)
            VALUES {placeholders}
            ON CONFLICT (id) DO UPDATE SET
                data = excluded.data,
                embedding = excluded.embedding,
                hash = excluded.hash,
                dataset_id = excluded.dataset_id,
                user_id = excluded.user_id,
                metadata = excluded.metadata
        """

        with self.conn.cursor() as cur:
            cur.execute(q, flat_values)
            self.conn.commit()

    async def delete(
        self,
        ids: List[str],
        dataset_id: str,
        user_id: Optional[str] = None,
    ) -> None:
        req = "delete from documents where id in %s and dataset_id = %s", (
            tuple(ids),
            dataset_id,
        )
        if user_id:
            req += f" and user_id = {user_id}"
        return [dict(row) for row in self.conn.execute(req)]

    async def search(
        self,
        vector: List[float],
        top_k: Optional[int],
        dataset_id: str,
        user_id: Optional[str] = None,
    ) -> List[dict]:
        d = {
            "query_embedding": str(vector),
            "similarity_threshold": 0.1,  # TODO: make this configurable
            "match_count": top_k,
            "query_dataset_id": dataset_id,
            "query_user_id": user_id,
        }
        q = "select * from match_documents(%(query_embedding)s, %(similarity_threshold)s, %(match_count)s, %(query_dataset_id)s, %(query_user_id)s)"
        results = self.conn.execute(q, d)
        if results.rowcount == 0:
            return []
        data = []
        for row in results:
            # tuple to dict
            data.append(
                {
                    "id": row[0],
                    "data": row[1],
                    "similarity": row[2],
                    "hash": row[3],
                    "embedding": row[4],
                    "metadata": row[5],
                }
            )
        return data

    async def clear(self, dataset_id: str, user_id: Optional[str] = None) -> None:
        req = f"delete from documents where dataset_id = '{dataset_id}'"
        if user_id:
            req += f" and user_id = {user_id}"
        from psycopg import sql

        self.conn.execute(req)

    async def get_datasets(self, user_id: Optional[str] = None) -> List[dict]:
        req = "select * from distinct_datasets"
        if user_id:
            req += f" where user_id = {user_id}"
        return [dict(row) for row in self.conn.execute(req)]