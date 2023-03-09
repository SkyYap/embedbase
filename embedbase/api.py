import hashlib
import time
from pandas import DataFrame
import pandas as pd
import os
from functools import lru_cache
import typing
import logging
from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from embedbase.firebase_auth import enable_firebase_auth
from embedbase.models import (
    DeleteRequest,
    AddRequest,
    SearchRequest,
)
from fastapi.responses import JSONResponse
import urllib.parse
import numpy as np
from embedbase.db import VectorDatabase, batch_fetch
from embedbase.pinecone_db import Pinecone
from embedbase.supabase_db import Supabase
from embedbase.weaviate_db import Weaviate
from embedbase.settings import Settings, get_settings, VectorDatabaseEnum
import openai

from tenacity import retry
from tenacity.wait import wait_exponential
from tenacity.before import before_log
from tenacity.after import after_log
from tenacity.stop import stop_after_attempt
import requests
import uuid

settings = get_settings()
MAX_DOCUMENT_LENGTH = int(os.environ.get("MAX_DOCUMENT_LENGTH", "1000"))
PORT = os.environ.get("PORT", 8080)
UPLOAD_BATCH_SIZE = int(os.environ.get("UPLOAD_BATCH_SIZE", "100"))

logger = logging.getLogger("embedbase")
logger.setLevel(settings.log_level)
handler = logging.StreamHandler()
handler.setLevel(settings.log_level)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

middlewares = []
if settings.middlewares:
    from starlette.middleware import Middleware

    for i, m in enumerate(settings.middlewares):
        # import python file at path m
        # and add the first class found to the list

        try:
            logger.info(f"Importing middleware {m}")
            segments = m.split(".")
            logger.debug(f"Segments {segments}")
            module_name = ".".join(segments[0:-1])
            logger.debug(f"Module name {module_name}")
            class_name = segments[-1]
            logger.debug(f"Class name {class_name}")
            module = __import__(module_name, fromlist=[class_name])
            logger.debug(f"Module {module}")
            dirs = dir(module)
            logger.debug(f"Dirs {dirs}")
            middleware_class = getattr(module, class_name)
            logger.debug(f"Middleware class {middleware_class}")
            middlewares.append(Middleware(middleware_class))
            logger.info(f"Loaded middleware {m}")
        except Exception as e:
            logger.error(f"Error loading middleware {m}: {e}")


app = FastAPI(middleware=middlewares)

if settings.sentry:
    logger.info("Enabling Sentry")
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry,
        # Set traces_sample_rate to 1.0 to capture 100%
        # of transactions for performance monitoring.
        # We recommend adjusting this value in production,
        traces_sample_rate=0.2,
        environment=os.environ.get("ENVIRONMENT", "development"),
        _experiments={
            "profiles_sample_rate": 1.0,
        },
    )

if settings.auth == "firebase":
    logger.info("Enabling Firebase Auth")
    enable_firebase_auth(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_vector_database() -> VectorDatabase:
    if settings.vector_database == VectorDatabaseEnum.pinecone:
        return Pinecone(
            api_key=settings.pinecone_api_key,
            environment=settings.pinecone_environment,
            index_name=settings.pinecone_index,
        )
    elif settings.vector_database == VectorDatabaseEnum.supabase:
        return Supabase(
            url=settings.supabase_url,
            key=settings.supabase_key,
        )
    elif settings.vector_database == VectorDatabaseEnum.weaviate:
        return Weaviate()
    else:
        raise Exception(
            "Invalid vector database, it must be pinecone, supabase or weaviate"
        )


vector_database = get_vector_database()

openai.api_key = settings.openai_api_key
openai.organization = settings.openai_organization


@app.on_event("startup")
async def startup_event():
    logger.info(f"Detected an upload batch size of {UPLOAD_BATCH_SIZE}")


@lru_cache()
def no_batch_embed(sentence: str, _: Settings = Depends(get_settings)):
    """
    Compute the embedding for a given sentence
    """
    settings = get_settings()
    chunks = [sentence]
    if len(sentence) > 2000:
        chunks = [sentence[i : i + 2000] for i in range(0, len(sentence), 2000)]
    embeddings = embed(chunks, settings.model)
    if len(chunks) > 1:
        return np.mean([e["embedding"] for e in embeddings], axis=0).tolist()
    return embeddings[0]["embedding"]


@retry(
    wait=wait_exponential(multiplier=1, min=1, max=3),
    before=before_log(logger, logging.INFO),
    after=after_log(logger, logging.ERROR),
    stop=stop_after_attempt(3),
)
def embed(
    input: typing.List[str], model: str = "text-embedding-ada-002"
) -> typing.List[dict]:
    """
    Embed a list of sentences using OpenAI's API and retry on failure
    Only supports OpenAI's embedding models for now
    :param input: list of sentences to embed
    :param model: model to use
    :return: list of embeddings
    """
    return openai.Embedding.create(input=input, model=model)["data"]


def get_namespace(request: Request, vault_id: str) -> str:
    return f"{request.scope.get('uid')}/{vault_id}"


@app.get("/v1/{vault_id}/clear")
async def clear(
    request: Request,
    vault_id: str,
    _: Settings = Depends(get_settings),
):
    namespace = get_namespace(request, vault_id)

    await vector_database.clear(namespace=namespace)
    logger.info("Cleared index")
    return JSONResponse(status_code=200, content={})


@app.post("/v1/{vault_id}")
async def add(
    request: Request,
    vault_id: str,
    request_body: AddRequest,
    _: Settings = Depends(get_settings),
):
    """
    Refresh the embeddings for a given file
    """
    namespace = get_namespace(request, vault_id)

    documents = request_body.documents
    df = DataFrame(
        [doc.dict() for doc in documents if doc.data is not None],
        columns=[
            "id",
            "data",
            "embedding",
            "hash",
        ],
    )

    start_time = time.time()
    logger.info(f"Refreshing {len(documents)} embeddings")

    if not df.data.any():
        logger.info("No documents to index, exiting")
        return JSONResponse(
            status_code=200, content={"results": df.to_dict(orient="records")}
        )

    # add column "hash" based on "data"
    df.hash = df.data.apply(lambda x: hashlib.sha256(x.encode()).hexdigest())

    df_length = len(df)
    existing_hashes = []

    if df.id.any():
        logger.info(
            f"Checking embeddings computing necessity for {df_length} documents"
        )
        # filter out documents that didn't change by checking their hash
        # in the index metadata
        ids_to_fetch = df.id.apply(urllib.parse.quote).tolist()
        flat_existing_documents = await batch_fetch(
            vector_database, ids_to_fetch, namespace
        )
        # remove rows that have the same hash
        for doc in flat_existing_documents:
            existing_hashes.append(doc["id"])
        df = df[
            ~df.apply(
                lambda x: x["hash"] in existing_hashes,
                axis=1,
            )
        ]
    else:
        # generate ids using hash of uuid + time to avoid collisions
        df.id = df.apply(
            lambda x: hashlib.sha256(
                (str(uuid.uuid4()) + str(time.time())).encode()
            ).hexdigest(),
            axis=1,
        )

    diff = df_length - len(df)

    logger.info(f"Filtered out {diff} documents that didn't change at all")

    if not df.data.any():
        logger.info(
            "No documents to index found after filtering existing ones, exiting"
        )
        return JSONResponse(
            status_code=200,
            content={
                # embeddings, ids and data are returned
                "results": df.to_dict(orient="records"),
            },
        )

    # parallelize
    response = embed(df.data.tolist(), settings.model)
    df.embedding = [e["embedding"] for e in response]

    # average the embeddings over "embedding" column grouped by index, merge back into df
    s = (
        df.apply(lambda x: pd.Series(x["embedding"]), axis=1)
        .groupby(level=0)
        .mean()
        .reset_index()
        .drop("index", axis=1)
    )
    # # merge s column into a single column , ignore index
    df.embedding = s.apply(lambda x: x.tolist(), axis=1)
    # TODO: pinecone doesn't support this large of an input?
    await vector_database.update(
        df,
        namespace,
        batch_size=UPLOAD_BATCH_SIZE,
        save_clear_data=settings.save_clear_data,
    )

    logger.info(f"Indexed & uploaded {len(df)} sentences")
    end_time = time.time()
    logger.info(f"Indexed & uploaded in {end_time - start_time} seconds")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            # embeddings, ids and data are returned
            "results": df.to_dict(orient="records"),
        },
    )


@app.delete("/v1/{vault_id}")
async def delete(
    request: Request,
    vault_id: str,
    request_body: DeleteRequest,
    _: Settings = Depends(get_settings),
):
    """
    Delete a document from the index
    """
    namespace = get_namespace(request, vault_id)

    ids = request_body.ids
    logger.info(f"Deleting {len(ids)} documents")
    quoted_ids = [urllib.parse.quote(id) for id in ids]
    await vector_database.delete(ids=quoted_ids, namespace=namespace)
    logger.info(f"Deleted {len(ids)} documents")

    return JSONResponse(status_code=status.HTTP_200_OK, content={})


@app.post("/v1/{vault_id}/search")
async def semantic_search(
    request: Request,
    vault_id: str,
    request_body: SearchRequest,
    _: Settings = Depends(get_settings),
):
    """
    Search for a given query in the corpus
    """
    query = request_body.query
    namespace = get_namespace(request, vault_id)

    top_k = 5  # TODO might fail if index empty?
    if request_body.top_k > 0:
        top_k = request_body.top_k
    query_embedding = no_batch_embed(query)

    logger.info(f"Query {request_body.query} created embedding, querying index")

    query_response = await vector_database.search(
        top_k=top_k,
        vector=query_embedding,
        namespace=namespace,
    )

    similarities = []
    for match in query_response:
        decoded_id = urllib.parse.unquote(match["id"])
        logger.debug(f"ID: {decoded_id}")
        similarities.append(
            {
                "score": match["score"],
                "id": decoded_id,
                "data": match["data"],
                "hash": match["hash"], # TODO: probably shouldn't return this
                "embedding": match["embedding"],
            }
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"query": query, "similarities": similarities},
    )


# health check endpoint
@app.get("/health")
def health(request: Request):
    """
    Return the status of the API
    """
    logger.info("Health check")
    # get headers
    headers = request.headers
    # Handle here any business logic for ensuring you're application is healthy (DB connections, etc...)
    r = requests.post(
        f"http://0.0.0.0:{PORT}/v1/test",
        json={
            "documents": [],
        },
        # forward headers
        headers=headers,
    )
    r.raise_for_status()
    logger.info("Health check successful")

    return JSONResponse(status_code=200, content={})
