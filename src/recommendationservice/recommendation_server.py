#!/usr/bin/python
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import re
import time
import traceback
from collections import OrderedDict
from concurrent import futures
from decimal import Decimal
from threading import Lock

import googlecloudprofiler
from google.auth.exceptions import DefaultCredentialsError
import grpc

import demo_pb2
import demo_pb2_grpc
from grpc_health.v1 import health_pb2
from grpc_health.v1 import health_pb2_grpc

from opentelemetry import trace
from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient, GrpcInstrumentorServer
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

# Instrument httpx BEFORE importing OpenAI (which uses httpx internally)
HTTPXClientInstrumentor().instrument()

from openai import OpenAI

from logger import getJSONLogger

logger = getJSONLogger('recommendationservice-server')
MAX_RECOMMENDATIONS = 5
OPENROUTER_MODEL = "nex-agi/deepseek-v3.1-nex-n1:free"
CACHE_MAX_SIZE = 300
CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
product_catalog_stub = None
openrouter_client = None
recommendation_cache = None


class RecommendationCache:
    def __init__(self, max_entries, ttl_seconds):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = Lock()
        self._entries = OrderedDict()

    def get(self, product_ids):
        key = self._make_key(product_ids)
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            if now - entry["timestamp"] > self.ttl_seconds:
                self._entries.pop(key, None)
                return None
            # maintain LRU order
            self._entries.move_to_end(key)
            return list(entry["value"])

    def set(self, product_ids, product_ids_response):
        key = self._make_key(product_ids)
        with self._lock:
            self._entries[key] = {
                "value": list(product_ids_response),
                "timestamp": time.time()
            }
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def _make_key(self, product_ids):
        return tuple(sorted(product_ids))

def initStackdriverProfiling():
  project_id = None
  try:
    project_id = os.environ["GCP_PROJECT_ID"]
  except KeyError:
    # Environment variable not set
    pass

  for retry in range(1,4):
    try:
      if project_id:
        googlecloudprofiler.start(service='recommendation_server', service_version='1.0.0', verbose=0, project_id=project_id)
      else:
        googlecloudprofiler.start(service='recommendation_server', service_version='1.0.0', verbose=0)
      logger.info("Successfully started Stackdriver Profiler.")
      return
    except (BaseException) as exc:
      logger.info("Unable to start Stackdriver Profiler Python agent. " + str(exc))
      if (retry < 4):
        logger.info("Sleeping %d seconds to retry Stackdriver Profiler agent initialization"%(retry*10))
        time.sleep (1)
      else:
        logger.warning("Could not initialize Stackdriver Profiler after retrying, giving up")
  return

class RecommendationService(demo_pb2_grpc.RecommendationServiceServicer):
    def ListRecommendations(self, request, context):
        # fetch list of products from product catalog stub
        cat_response = product_catalog_stub.ListProducts(demo_pb2.Empty())
        try:
            prod_list = self._get_ai_recommendations(cat_response.products, request)
        except Exception as exc:
            logger.error("Failed to generate recommendations via OpenRouter: %s", exc)
            context.abort(grpc.StatusCode.INTERNAL, "Failed to generate recommendations")
            return demo_pb2.ListRecommendationsResponse()
        logger.info("[Recv ListRecommendations] product_ids=%s", prod_list)
        # build and return response
        response = demo_pb2.ListRecommendationsResponse()
        response.product_ids.extend(prod_list)
        return response

    def _get_ai_recommendations(self, catalog_products, request):
        if not openrouter_client:
            raise RuntimeError("OpenRouter client is not configured")
        catalog_payload = _serialize_catalog_for_ai(catalog_products)
        allowed_ids = {item["id"] for item in catalog_payload}
        excluded_ids = set(request.product_ids)
        remaining_ids = [
            product["id"]
            for product in catalog_payload
            if product["id"] not in excluded_ids
        ]
        if len(remaining_ids) <= MAX_RECOMMENDATIONS:
            logger.info("Returning short-circuited recommendations (remaining catalog <= limit)")
            return remaining_ids
        cached = recommendation_cache.get(request.product_ids) if recommendation_cache else None
        if cached:
            logger.info("Returning cached recommendations for product_ids=%s", sorted(request.product_ids))
            return cached
        ai_ids = _request_openrouter_recommendations(
            catalog_payload=catalog_payload,
            excluded_ids=excluded_ids,
            user_id=request.user_id or "anonymous",
            max_results=MAX_RECOMMENDATIONS,
            allowed_ids=allowed_ids,
        )
        if not ai_ids:
            raise RuntimeError("OpenRouter returned no valid product IDs")
        # double-check items exist in catalog payload
        cleaned = [pid for pid in ai_ids if pid in allowed_ids]
        if recommendation_cache:
            recommendation_cache.set(request.product_ids, cleaned)
        return cleaned

    def Check(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.SERVING)

    def Watch(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.UNIMPLEMENTED)


def _init_openrouter_client():
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY must be set for recommendationservice")
    site_url = os.environ.get("OPENROUTER_SITE_URL")
    app_name = os.environ.get("OPENROUTER_APP_NAME", "recommendationservice")
    headers = {}
    if site_url:
        headers["HTTP-Referer"] = site_url
    if app_name:
        headers["X-Title"] = app_name
    logger.info("OpenRouter support enabled with model %s", OPENROUTER_MODEL)
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers=headers or None,
    )


def _serialize_catalog_for_ai(products):
    serialized = []
    for product in products:
        serialized.append({
            "id": product.id,
            "name": product.name,
            "description": product.description,
            "categories": list(product.categories),
            "picture": product.picture,
            "price": _format_price(product.price_usd),
        })
    return serialized


def _format_price(money):
    if not money:
        return None
    units = Decimal(getattr(money, "units", 0))
    nanos = Decimal(getattr(money, "nanos", 0)) / Decimal("1000000000")
    amount = units + nanos
    return {
        "currency_code": getattr(money, "currency_code", ""),
        "amount": format(amount, "f"),
    }


def _request_openrouter_recommendations(
    *,
    catalog_payload,
    excluded_ids,
    user_id,
    max_results,
    allowed_ids,
):
    instructions = (
        "You are part of an e-commerce team. "
        "Given the product catalog, select up to {max_results} product IDs "
        "for recommendation. Respond ONLY with JSON containing "
        'a "product_ids" list.'
    ).format(max_results=max_results)
    user_payload = {
        "user_id": user_id,
        "products_in_cart": list(excluded_ids),
        "catalog": catalog_payload,
        "response_template": {"product_ids": ["product-id-1", "product-id-2"]},
    }
    completion = openrouter_client.chat.completions.create(
        model=OPENROUTER_MODEL,
        temperature=0.2,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    )
    choices = getattr(completion, "choices", None) or []
    if not choices:
        raise RuntimeError("OpenRouter response did not include any choices")
    content = choices[0].message.content
    text = _message_content_to_text(content)
    if not text:
        raise RuntimeError("OpenRouter returned empty content")
    parsed_ids = _extract_ids(text)
    cleaned = []
    for product_id in parsed_ids:
        if product_id in excluded_ids:
            continue
        if product_id not in allowed_ids:
            continue
        cleaned.append(product_id)
        if len(cleaned) >= max_results:
            break
    return cleaned


def _message_content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _extract_ids(text):
    text = text.strip()
    if text.startswith("```"):
        text = _strip_code_fences(text)
    parsed = _load_json_fragment(text)
    if isinstance(parsed, dict):
        candidate = parsed.get("product_ids") or parsed.get("productIds")
        if isinstance(candidate, list):
            return [str(item) for item in candidate]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    matches = re.findall(r"[A-Z0-9]{8,}", text)
    return matches


def _strip_code_fences(text):
    lines = [line.strip("`") for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _load_json_fragment(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        snippet = _find_json_snippet(text)
        if snippet:
            return json.loads(snippet)
    return None


def _find_json_snippet(text):
    start_list = text.find("[")
    end_list = text.rfind("]")
    if 0 <= start_list < end_list:
        return text[start_list : end_list + 1]
    start_obj = text.find("{")
    end_obj = text.rfind("}")
    if 0 <= start_obj < end_obj:
        return text[start_obj : end_obj + 1]
    return None


if __name__ == "__main__":
    logger.info("initializing recommendationservice")

    try:
      if "DISABLE_PROFILER" in os.environ:
        raise KeyError()
      else:
        logger.info("Profiler enabled.")
        initStackdriverProfiling()
    except KeyError:
        logger.info("Profiler disabled.")

    try:
      grpc_client_instrumentor = GrpcInstrumentorClient()
      grpc_client_instrumentor.instrument()
      grpc_server_instrumentor = GrpcInstrumentorServer()
      grpc_server_instrumentor.instrument()
      if os.environ["ENABLE_TRACING"] == "1":
        trace.set_tracer_provider(TracerProvider())
        otel_endpoint = os.getenv("COLLECTOR_SERVICE_ADDR", "localhost:4317")
        trace.get_tracer_provider().add_span_processor(
          BatchSpanProcessor(
              OTLPSpanExporter(
              endpoint = otel_endpoint,
              insecure = True
            )
          )
        )
    except (KeyError, DefaultCredentialsError):
        logger.info("Tracing disabled.")
    except Exception as e:
        logger.warn(f"Exception on Cloud Trace setup: {traceback.format_exc()}, tracing disabled.") 

    port = os.environ.get('PORT', "8080")
    catalog_addr = os.environ.get('PRODUCT_CATALOG_SERVICE_ADDR', '')
    if catalog_addr == "":
        raise Exception('PRODUCT_CATALOG_SERVICE_ADDR environment variable not set')
    logger.info("product catalog address: " + catalog_addr)
    channel = grpc.insecure_channel(catalog_addr)
    product_catalog_stub = demo_pb2_grpc.ProductCatalogServiceStub(channel)
    openrouter_client = _init_openrouter_client()
    recommendation_cache = RecommendationCache(CACHE_MAX_SIZE, CACHE_TTL_SECONDS)

    # create gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

    # add class to gRPC server
    service = RecommendationService()
    demo_pb2_grpc.add_RecommendationServiceServicer_to_server(service, server)
    health_pb2_grpc.add_HealthServicer_to_server(service, server)

    # start server
    logger.info("listening on port: " + port)
    server.add_insecure_port('[::]:'+port)
    server.start()

    # keep alive
    try:
         while True:
            time.sleep(10000)
    except KeyboardInterrupt:
            server.stop(0)
