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

import enum
import json
import os
import random
import re
import time
import traceback
from collections import OrderedDict
from concurrent import futures
from datetime import datetime
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

import httpx
from openai import APIConnectionError, OpenAI

from logger import getJSONLogger

logger = getJSONLogger('recommendationservice-server')
MAX_RECOMMENDATIONS = 5
DEFAULT_OPENROUTER_MODEL = "openai/gpt-oss-120b:free"
CACHE_MAX_SIZE = 300
CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
GLOBAL_CACHE_TTL_SECONDS = 60 * 60  # 1 hour for global recommendations

# Rate limiting defaults
DEFAULT_TZ = "America/Los_Angeles"
DEFAULT_ACTIVE_HOURS = "9-17"
DEFAULT_RATE_LIMIT_PER_MINUTE = 2
DEFAULT_LLM_SAMPLE_RATE = 0.0001  # 0.01% - roughly 1 call/hour at 10k req/hour

# LLM mode defaults
DEFAULT_LOCAL_BASE_URL = "https://mock-openrouter:8443/api/v1"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LOCAL_MODELS = "anthropic/claude-sonnet-4.5,google/gemini-2.5-flash,openai/gpt-4o-mini,deepseek/deepseek-chat-v3-0324"

# Circuit breaker defaults
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_INITIAL_BACKOFF = 30  # seconds
CIRCUIT_BREAKER_MAX_BACKOFF = 86400  # 24 hours

product_catalog_stub = None
openrouter_client = None
recommendation_cache = None
llm_rate_limiter = None
global_recommendations_cache = None
openrouter_model = None
llm_mode = None
openrouter_local_models = None
local_client = None
openrouter_fallback_client = None
circuit_breaker = None


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


class GlobalRecommendationsCache:
    """Cache for global (non-personalized) recommendations used as fallback."""

    def __init__(self, ttl_seconds):
        self.ttl_seconds = ttl_seconds
        self._lock = Lock()
        self._recommendations = None
        self._timestamp = 0

    def get(self):
        with self._lock:
            if self._recommendations is None:
                return None
            if time.time() - self._timestamp > self.ttl_seconds:
                return None
            return list(self._recommendations)

    def set(self, recommendations):
        with self._lock:
            self._recommendations = list(recommendations)
            self._timestamp = time.time()


class LLMRateLimiter:
    """Rate limiter for LLM calls with time-based windows and sampling."""

    def __init__(self, active_hours_str, rate_limit_per_minute, sample_rate):
        self.active_windows = self._parse_active_hours(active_hours_str)
        self.rate_limit_per_minute = rate_limit_per_minute
        self.sample_rate = sample_rate
        self._lock = Lock()
        self._call_timestamps = []

    def _parse_time(self, time_str):
        """Parse time string like '9', '9:30', '16:45' into decimal hours."""
        time_str = time_str.strip()
        if ':' in time_str:
            parts = time_str.split(':')
            hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 else 0
            return hours + minutes / 60.0
        return float(time_str)

    def _parse_active_hours(self, hours_str):
        """Parse active hours string like '9-17', '8:30-16:30', or '9-11,15-19' into list of (start, end) tuples."""
        windows = []
        if not hours_str:
            return windows
        for window in hours_str.split(','):
            window = window.strip()
            if '-' not in window:
                continue
            # Split on first hyphen only to handle negative times edge case
            hyphen_idx = window.find('-', 1) if window.startswith('-') else window.find('-')
            if hyphen_idx == -1:
                continue
            start_str = window[:hyphen_idx]
            end_str = window[hyphen_idx + 1:]
            try:
                start = self._parse_time(start_str)
                end = self._parse_time(end_str)
                if 0 <= start < 24 and 0 <= end < 24:
                    windows.append((start, end))
            except (ValueError, IndexError):
                continue
        return windows

    def _is_within_active_hours(self):
        """Check if current local time is within any active window."""
        if not self.active_windows:
            return True  # No windows configured = always active
        now = datetime.now()
        current_time = now.hour + now.minute / 60.0
        for start, end in self.active_windows:
            if start <= end:
                # Normal window like 9-17 or 8:30-16:30
                if start <= current_time < end:
                    return True
            else:
                # Overnight window like 22-6
                if current_time >= start or current_time < end:
                    return True
        return False

    def _passes_sampling(self):
        """Check if this request passes probabilistic sampling."""
        return random.random() < self.sample_rate

    def _check_rate_limit(self):
        """Check if we're under the per-minute rate limit. Returns True if allowed."""
        now = time.time()
        with self._lock:
            # Remove timestamps older than 60 seconds
            self._call_timestamps = [ts for ts in self._call_timestamps if now - ts < 60]
            if len(self._call_timestamps) >= self.rate_limit_per_minute:
                return False
            self._call_timestamps.append(now)
            return True

    def should_call_llm(self):
        """
        Determine if an LLM call should be made.
        Returns tuple of (should_call: bool, reason: str).

        During active hours: use rate limiting (e.g., 2/min)
        Outside active hours: use sampling (e.g., ~1/hour)
        """
        if self._is_within_active_hours():
            # During active hours: rate limiting applies
            if not self._check_rate_limit():
                return False, "rate_limited"
            return True, "allowed"
        else:
            # Outside active hours: sampling applies
            if not self._passes_sampling():
                return False, "sampling_skip"
            return True, "allowed_via_sampling"


class CircuitState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class LocalCircuitBreaker:
    """Circuit breaker for local LLM endpoint with fallback to openrouter."""

    def __init__(self, failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                 initial_backoff=CIRCUIT_BREAKER_INITIAL_BACKOFF,
                 max_backoff=CIRCUIT_BREAKER_MAX_BACKOFF):
        self.failure_threshold = failure_threshold
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self._lock = Lock()
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.backoff_seconds = initial_backoff
        self.open_since = 0

    def should_use_local(self):
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                elapsed = time.time() - self.open_since
                if elapsed >= self.backoff_seconds:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker HALF_OPEN: probing local endpoint after %ds backoff",
                                int(self.backoff_seconds))
                    return True
                return False
            # HALF_OPEN — allow one probe
            return True

    def backoff_remaining(self):
        with self._lock:
            if self.state != CircuitState.OPEN:
                return 0
            elapsed = time.time() - self.open_since
            return max(0, int(self.backoff_seconds - elapsed))

    def record_success(self):
        with self._lock:
            if self.state != CircuitState.CLOSED:
                logger.info("Circuit breaker CLOSED: local endpoint recovered")
            self.state = CircuitState.CLOSED
            self.consecutive_failures = 0
            self.backoff_seconds = self.initial_backoff

    def record_failure(self):
        with self._lock:
            self.consecutive_failures += 1
            if self.state == CircuitState.HALF_OPEN:
                self.backoff_seconds = min(self.backoff_seconds * 2, self.max_backoff)
                self.state = CircuitState.OPEN
                self.open_since = time.time()
                logger.info("Circuit breaker OPEN: local probe failed, backoff increased to %ds",
                            int(self.backoff_seconds))
            elif self.consecutive_failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.open_since = time.time()
                logger.info("Circuit breaker OPEN: local endpoint failed %d times, falling back to openrouter for %ds",
                            self.consecutive_failures, int(self.backoff_seconds))


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
            if recommendation_cache:
                recommendation_cache.set(request.product_ids, remaining_ids)
            return remaining_ids

        # Check existing cache first (exact match for this cart)
        cached = recommendation_cache.get(request.product_ids) if recommendation_cache else None
        if cached:
            logger.info("Returning cached recommendations for product_ids=%s", sorted(request.product_ids))
            return cached

        # Determine client and model based on LLM mode and circuit breaker
        if llm_mode == "local" and circuit_breaker:
            use_local = circuit_breaker.should_use_local()
            if use_local:
                active_client = local_client
                active_model = random.choice(openrouter_local_models)
                skip_rate_limit = True
            else:
                active_client = openrouter_fallback_client
                active_model = openrouter_model
                skip_rate_limit = False
        else:
            active_client = openrouter_client
            active_model = openrouter_model
            skip_rate_limit = False

        # Check rate limiter (skipped in local mode when circuit is closed/half-open)
        if not skip_rate_limit:
            should_call, reason = llm_rate_limiter.should_call_llm() if llm_rate_limiter else (True, "no_limiter")
            if not should_call:
                cb_state = circuit_breaker.state.value if circuit_breaker else "N/A"
                cb_remaining = circuit_breaker.backoff_remaining() if circuit_breaker else 0
                logger.info("LLM call skipped (%s), using fallback recommendations [circuit_breaker=%s, backoff_remaining=%ds]",
                            reason, cb_state, cb_remaining)
                return self._get_fallback_recommendations(remaining_ids, excluded_ids)
        else:
            reason = "local_no_limit"

        # Log the LLM call with circuit breaker info when relevant
        if circuit_breaker and circuit_breaker.state != CircuitState.CLOSED:
            cb_state = circuit_breaker.state.value
            cb_remaining = circuit_breaker.backoff_remaining()
            logger.info("LLM call allowed (%s), requesting from OpenRouter [circuit_breaker=%s, backoff_remaining=%ds]",
                        reason, cb_state, cb_remaining)
        else:
            logger.info("LLM call allowed (%s), requesting from OpenRouter", reason)

        # Make the LLM call with circuit breaker error handling for local mode
        try:
            ai_ids = _request_openrouter_recommendations(
                client=active_client,
                model=active_model,
                catalog_payload=catalog_payload,
                excluded_ids=excluded_ids,
                user_id=request.user_id or "anonymous",
                max_results=MAX_RECOMMENDATIONS,
                allowed_ids=allowed_ids,
            )
        except (ConnectionError, OSError, APIConnectionError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if circuit_breaker and llm_mode == "local":
                circuit_breaker.record_failure()
                logger.warning("Local LLM call failed with connection error: %s", exc)
                # If we were using local, retry with fallback
                if skip_rate_limit:
                    return self._get_fallback_recommendations(remaining_ids, excluded_ids)
            raise
        else:
            if circuit_breaker and llm_mode == "local":
                circuit_breaker.record_success()

        if not ai_ids:
            raise RuntimeError("OpenRouter returned no valid product IDs")

        # Double-check items exist in catalog payload
        cleaned = [pid for pid in ai_ids if pid in allowed_ids]

        # Cache results
        if recommendation_cache:
            recommendation_cache.set(request.product_ids, cleaned)

        # Also update global cache for fallback use (without exclusions applied)
        if global_recommendations_cache:
            # Store all recommended IDs before filtering for global fallback
            global_recommendations_cache.set(cleaned)

        return cleaned

    def _get_fallback_recommendations(self, remaining_ids, excluded_ids):
        """Get fallback recommendations when LLM is not called."""
        # First try global cache (previous LLM responses)
        if global_recommendations_cache:
            global_recs = global_recommendations_cache.get()
            if global_recs:
                # Filter out excluded items and return
                filtered = [pid for pid in global_recs if pid not in excluded_ids]
                if filtered:
                    logger.info("Using global cache fallback, %d recommendations", len(filtered[:MAX_RECOMMENDATIONS]))
                    return filtered[:MAX_RECOMMENDATIONS]

        # Final fallback: random selection from remaining products
        logger.info("Using random fallback, selecting from %d products", len(remaining_ids))
        if len(remaining_ids) <= MAX_RECOMMENDATIONS:
            return remaining_ids
        return random.sample(remaining_ids, MAX_RECOMMENDATIONS)

    def Check(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.SERVING)

    def Watch(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.UNIMPLEMENTED)


def _init_openrouter_client(base_url=None):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY must be set for recommendationservice")
    if base_url is None:
        base_url = os.environ.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL)
    site_url = os.environ.get("OPENROUTER_SITE_URL")
    app_name = os.environ.get("OPENROUTER_APP_NAME", "recommendationservice")
    headers = {}
    if site_url:
        headers["HTTP-Referer"] = site_url
    if app_name:
        headers["X-Title"] = app_name
    logger.info("OpenRouter client initialized at %s", base_url)
    kwargs = dict(
        base_url=base_url,
        api_key=api_key,
        default_headers=headers or None,
    )
    # Skip TLS verification for internal services with self-signed certs
    if base_url and base_url.startswith("https://mock-openrouter"):
        kwargs["http_client"] = httpx.Client(verify=False)
    return OpenAI(**kwargs)


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
    client,
    model,
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
    completion = client.chat.completions.create(
        model=model,
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

    # Set timezone for time-based rate limiting (must be done before datetime calls)
    tz = os.environ.get('TZ', DEFAULT_TZ)
    if 'TZ' not in os.environ:
        os.environ['TZ'] = tz
        # Force libc to re-read TZ
        try:
            time.tzset()
        except AttributeError:
            pass  # tzset not available on Windows
    logger.info("Timezone set to: %s", tz)

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

    # Initialize LLM mode
    llm_mode = os.environ.get('LLM_MODE', 'local')
    openrouter_model = os.environ.get('OPENROUTER_MODEL', DEFAULT_OPENROUTER_MODEL)
    recommendation_cache = RecommendationCache(CACHE_MAX_SIZE, CACHE_TTL_SECONDS)

    # Initialize rate limiter (used in openrouter mode, and as fallback in local mode)
    active_hours = os.environ.get('LLM_ACTIVE_HOURS', DEFAULT_ACTIVE_HOURS)
    rate_limit = int(os.environ.get('LLM_RATE_LIMIT_PER_MINUTE', DEFAULT_RATE_LIMIT_PER_MINUTE))
    sample_rate = float(os.environ.get('LLM_SAMPLE_RATE', DEFAULT_LLM_SAMPLE_RATE))
    llm_rate_limiter = LLMRateLimiter(active_hours, rate_limit, sample_rate)
    logger.info("LLM rate limiter initialized: active_hours=%s, rate_limit=%d/min, sample_rate=%.4f",
                active_hours, rate_limit, sample_rate)

    if llm_mode == "local":
        # Parse local models list
        local_models_str = os.environ.get('OPENROUTER_LOCAL_MODELS', DEFAULT_LOCAL_MODELS)
        openrouter_local_models = [m.strip() for m in local_models_str.split(',') if m.strip()]
        if not openrouter_local_models:
            openrouter_local_models = [openrouter_model]

        # Initialize local client (mock-openrouter)
        local_base_url = os.environ.get('OPENROUTER_BASE_URL', DEFAULT_LOCAL_BASE_URL)
        local_client = _init_openrouter_client(base_url=local_base_url)

        # Initialize fallback client (real openrouter)
        openrouter_fallback_client = _init_openrouter_client(base_url=DEFAULT_OPENROUTER_BASE_URL)

        # Set default active client to local
        openrouter_client = local_client

        # Initialize circuit breaker
        circuit_breaker = LocalCircuitBreaker()

        logger.info("LLM_MODE=local: models=%s, local_url=%s, circuit_breaker=enabled",
                    openrouter_local_models, local_base_url)
    else:
        # openrouter mode: single client, existing behavior
        openrouter_client = _init_openrouter_client()
        logger.info("LLM_MODE=openrouter: model=%s", openrouter_model)

    # Initialize global recommendations cache for fallback
    global_recommendations_cache = GlobalRecommendationsCache(GLOBAL_CACHE_TTL_SECONDS)
    logger.info("Global recommendations cache initialized with TTL=%d seconds", GLOBAL_CACHE_TTL_SECONDS)

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
