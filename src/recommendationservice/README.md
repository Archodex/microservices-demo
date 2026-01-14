# Recommendation Service

This service provides product recommendations over gRPC. Every call sends the full product catalog to OpenRouter's `openai/gpt-oss-120b:free` model (via the official `openai` Python SDK with a custom base URL) so the AI service can return context-aware product IDs. An OpenRouter API key is now required; the service will exit during startup if the key is missing. To allow use of `openai/gpt-oss-120b:free`, OpenRouter [priacy settings](https://openrouter.ai/settings/privacy) must be updated to allow `Enable free endpoints that may publish prompts`

## Local development

```
cd src/recommendationservice
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Required environment

| Variable | Description |
| --- | --- |
| `PRODUCT_CATALOG_SERVICE_ADDR` | Host:port for the Go product catalog gRPC server (for example `localhost:3550`). |
| `OPENROUTER_API_KEY` | API key for https://openrouter.ai. The service will not start without this value. |
| `OPENROUTER_SITE_URL` | Optional Referer header recommended by OpenRouter for rate limiting context. |
| `OPENROUTER_APP_NAME` | Optional label for the `X-Title` header (defaults to `recommendationservice`). |
| `OPENROUTER_TIMEOUT_SECONDS` | Custom request timeout for OpenRouter calls. |

### Optional configuration

These variables have sensible defaults but can be overridden:

| Variable | Default | Description |
| --- | --- | --- |
| `OPENROUTER_MODEL` | `openai/gpt-oss-120b:free` | OpenRouter model to use for recommendations. |
| `TZ` | `America/Los_Angeles` | Timezone for active hours calculation. |
| `LLM_ACTIVE_HOURS` | `9-17` | Hours when rate limiting applies. Supports single ranges (`9-17`), half-hours (`8:30-16:30`), or multiple windows (`9-11,15-19`). |
| `LLM_RATE_LIMIT_PER_MINUTE` | `2` | Maximum LLM calls per minute during active hours. |
| `LLM_SAMPLE_RATE` | `0.0001` | Fraction of requests eligible for LLM calls outside active hours (0.0-1.0). Default 0.01% = ~1 call/hour at 10k requests/hour. |

**Behavior:**
- During active hours: Rate limited to `LLM_RATE_LIMIT_PER_MINUTE` calls/minute
- Outside active hours: Sampled at `LLM_SAMPLE_RATE` (~1 call/hour by default)

When LLM calls are skipped (rate limited or sampling miss), the service returns:
1. Previously cached global recommendations (filtered for the user's cart), or
2. Random product selection from the catalog.

### Kubernetes configuration override

In Kubernetes, these optional settings are read from the `llm-config-override` ConfigMap. Create it once per namespace to customize:

```bash
kubectl create configmap llm-config-override -n <namespace> \
  --from-literal=TZ=Asia/Tokyo \
  --from-literal=LLM_ACTIVE_HOURS=8:30-16:30 \
  --from-literal=LLM_SAMPLE_RATE=0.001 \
  --from-literal=OPENROUTER_MODEL=openai/gpt-oss-120b:free
```

This ConfigMap persists across `skaffold run` deployments. To update, use `kubectl edit` or delete and recreate.

## Local development

Run the product catalog service separately (`go run .` from `src/productcatalogservice`), then start the recommendation service:

```
export PRODUCT_CATALOG_SERVICE_ADDR=localhost:3550
export OPENROUTER_API_KEY=your-key
python recommendation_server.py
```

Use `client.py` (or grpcurl) to exercise the `ListRecommendations` RPC. The request payload sent to OpenRouter includes the full catalog, the active user, and the items already in the cart, and expects a JSON response containing product IDs to return to callers.
